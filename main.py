"""TikTok canlı + sandıq botu (admin Telegram botu ilə idarə olunur).

  İşçi 1 (worker_scanner): users.txt -> canlı olanlar
  İşçi 2 (worker_chest):   canlıya qoşul -> sandıq hadisəsi
  Admin botu (bot_admin):  yalnız ADMIN_IDS-ə cavab verir; status/panel/kanal/sürət/pauza və s.
"""
import asyncio
import sys
import time
from pathlib import Path

# Repolar vendor/ qovluğundadır; pip install etməyə ehtiyac qalmasın deyə yola əlavə olunur.
ROOT = Path(__file__).resolve().parent
for sub in ("vendor/TikTokLive", "vendor/live-py"):
    sys.path.insert(0, str(ROOT / sub))

import alerts  # noqa: E402
import config  # noqa: E402
import fleet  # noqa: E402
import group_feed  # noqa: E402
import telegram_notify  # noqa: E402
import views  # noqa: E402
import worker_chest  # noqa: E402
import worker_scanner  # noqa: E402
from bot_admin import AdminBot  # noqa: E402
from control import Control  # noqa: E402
from log import ensure_unbuffered, install_logging, log  # noqa: E402
from settings import settings  # noqa: E402

__all__ = ["main"]


async def console_status(ctl: Control, stop: asyncio.Event) -> None:
    """Konsolda hər CONSOLE_STATUS_EVERY saniyədən bir canlı sayğac."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.CONSOLE_STATUS_EVERY)
        except asyncio.TimeoutError:
            log(ctl.stats.status_line())


async def reporter(ctl: Control, stop: asyncio.Event) -> None:
    """Ara hesabat: interval Telegram-dan (/hesabat_vaxt) dəyişdirilə bilir."""
    elapsed = 0
    feed_elapsed = 0
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        elapsed += 5
        feed_elapsed += 5
        if config.GROUP_LIVE_DIGEST_MIN > 0 and feed_elapsed >= config.GROUP_LIVE_DIGEST_MIN * 60:
            feed_elapsed = 0
            try:
                await group_feed.summary(ctl)    # qrupa YALNIZ say məlumatı (neçə canlı / neçə offline), eyni mesaj redaktə olunur
            except Exception as exc:
                log(f"[qrup] say məlumatı xətası: {type(exc).__name__}: {exc}")
        every = settings["report_every"]
        if every > 0 and elapsed >= every:
            elapsed = 0
            text = views.status_html(ctl, "⏱ <b>ARA HESABAT</b>")
            log("Telegram-a ara hesabat göndərilir")
            telegram_notify.notify_admins(text, html=True, keyboard=views.status_kb(ctl))


async def run_cycle(ctl: Control, resume: bool) -> None:
    stats = ctl.new_cycle()
    queue: asyncio.Queue = asyncio.Queue()
    ctl.queue = queue
    stop = asyncio.Event()
    n_chest = 0 if config.FLEET else config.CHEST_CONCURRENCY
    asyncio.get_running_loop().set_default_executor(__import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=config.EXECUTOR_THREADS))

    log("=" * 60)
    log(
        f"Skan #{ctl.cycle} başladı | Rejim: {'davam' if resume else 'sıfırdan'} | Sandıq paralel: {n_chest} | "
        f"Dinləmə: {settings['listen_seconds']}s | Proxy: {len(config.PROXIES) or 'yoxdur'} | "
        f"Telegram: {'var' if telegram_notify.has_admin() else 'yoxdur'} | Kanal: {len(settings.active_channels())}"
    )
    log("=" * 60)

    report_task = asyncio.create_task(reporter(ctl, stop))
    console_task = asyncio.create_task(console_status(ctl, stop))
    try:
        if config.FLEET:
            async def scanner_loop() -> None:
                first = True
                try:
                    while not ctl.stop_requested:
                        await worker_scanner.run(queue, stats, 0, ctl, resume if first else False)
                        first = False
                        if not config.CONTINUOUS or ctl.stop_requested:
                            break
                        wait = max(1, int(settings["rescan_minutes"])) * 60
                        ctl.next_rescan = time.time() + wait
                        ctl.rescan_now.clear()
                        log(f"[SKAN] dövr bitdi, {wait // 60} dəq sonra siyahı yenidən yoxlanacaq (canlılar ordunun əlindədir)")
                        telegram_notify.notify_admins(f"✅ <b>Siyahı yoxlandı.</b> 🔁 Növbəti yoxlama: <b>{wait // 60} dəq</b> sonra (/yenile_vaxt dəq • düymə: 🔁 İndi yoxla)", html=True)
                        try:
                            await asyncio.wait_for(ctl.rescan_now.wait(), timeout=wait)
                        except asyncio.TimeoutError:
                            pass
                        ctl.next_rescan = 0.0
                finally:
                    await queue.put(None)
            results = await asyncio.gather(scanner_loop(), fleet.run(queue, stats, ctl), return_exceptions=True)
        else:
            results = await asyncio.gather(
                worker_scanner.run(queue, stats, n_chest, ctl, resume),
                *(worker_chest.run(queue, stats, ctl) for _ in range(n_chest)),
                return_exceptions=True,
            )
        for r in results:
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                ctl.last_error = f"{type(r).__name__}: {r}"
                log(f"❌ İşçi xətası: {ctl.last_error}")
    finally:
        stop.set()
        await asyncio.gather(report_task, console_task, return_exceptions=True)
        ctl.queue = None

    stopped = ctl.stop_requested
    ctl.phase = "done"
    stats.scan_finished = True
    title = "🛑 <b>SKAN DAYANDIRILDI</b>" if stopped else "🏁 <b>YEKUN HESABAT — SKAN BİTDİ</b>"
    log("=" * 60)
    log(stats.status_line())
    if settings["alert_done"] or stopped:
        text = views.status_html(ctl, title)
        telegram_notify.notify_admins(text, html=True, keyboard=views.status_kb(ctl))
        if stats.chest_users:
            lines = [f"📦 <b>Sandıq olan yayımlar</b> ({len(stats.chest_users)})", "━━━━━━━━━━━━━━━━━━━━"]
            for u, n in list(stats.chest_users.items())[:40]:
                lines.append(f'🎁 <a href="https://www.tiktok.com/@{views.esc(u)}/live">@{views.esc(u)}</a> — {n} hadisə')
            telegram_notify.notify_admins("\n".join(lines), html=True)
    await telegram_notify.flush(10)


async def _guard(factory, name: str) -> None:
    """Fon tapşırığı xəta ilə çöksə, 5 saniyədən sonra yenidən qaldırılır."""
    while True:
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"[{name}] ⚠ {type(exc).__name__}: {exc} — 5s sonra yenidən")
            await asyncio.sleep(5)


_WS_BUG = {"n": 0, "t": 0.0}


def _loop_exception_handler(loop, context: dict) -> None:
    """websockets kitabxanasının bilinən xətası: proxy el sıxışmasında qopanda `response` None olur və
    AttributeError ilə traceback çıxır. Zərərsizdir (qoşulma onsuz da yenilənir), ona görə qısa sətirlə əvəz edilir."""
    exc = context.get("exception")
    msg = str(context.get("message", ""))
    if "_call_connection_lost" in msg or (isinstance(exc, AttributeError) and "status_code" in str(exc)):
        _WS_BUG["n"] += 1
        now = time.monotonic()
        if now - _WS_BUG["t"] > 60:
            what = f"{type(exc).__name__}: {str(exc)[:80]}" if exc else "?"
            log(f"[WS] ℹ proxy bağlantısı qopdu ({_WS_BUG['n']} dəfə, {what}) — qoşulma avtomatik yenilənir")
            _WS_BUG["t"] = now
        return
    loop.default_exception_handler(context)


def _raise_fd_limit() -> None:
    """3000 açıq bağlantı (+ proxy/skan soketləri) üçün fayl deskriptor limiti (Android-də default 1024 azdır)."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 32768 if hard == resource.RLIM_INFINITY else min(hard, 32768)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        now = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        log(f"[SİSTEM] fayl limiti: {now}")
        need = config.FLEET_MAX_LIVES + 1500
        if now < need:
            log(f"[SİSTEM] ⚠ fayl limiti ({now}) {config.FLEET_MAX_LIVES} canlı üçün azdır (lazım ~{need}). Termux-da əl ilə:  ulimit -n 32768  — alınmasa canlı sayı limitdə ilişəcək")
    except Exception as exc:
        log(f"[SİSTEM] fayl limiti artırılmadı ({exc}); əl ilə: ulimit -n 32768")


async def main() -> None:
    ensure_unbuffered()
    _raise_fd_limit()
    install_logging()
    asyncio.get_running_loop().set_exception_handler(_loop_exception_handler)
    if not config.USERS_FILE.exists():
        sys.exit(f"{config.USERS_FILE} tapılmadı")

    ctl = Control()
    interactive = telegram_notify.has_admin() and not config.EXIT_WHEN_DONE
    log("=" * 60)
    log("TikTok canlı + sandıq botu işə düşdü")
    if config.TELEGRAM_BOT_TOKEN and not config.ADMIN_IDS:
        log("⚠ ADMIN_IDS / TELEGRAM_CHAT_ID boşdur: bot HEÇ KİMƏ cavab verməyəcək və bildiriş göndərməyəcək")
    log(f"Admin: {config.ADMIN_IDS or 'yoxdur'} | Kanal: {len(settings['channels'])} | Rejim: {'Telegram ilə idarə' if interactive else 'tək dəfəlik'}")
    log("=" * 60)

    bg: list[asyncio.Task] = []
    if telegram_notify.has_admin() and config.ADMIN_BOT:
        bot = AdminBot(ctl)
        bg.append(asyncio.create_task(_guard(bot.run, "BOT")))
        bg.append(asyncio.create_task(_guard(bot.panel_loop, "PANEL")))

    resume = config.RESUME
    run_now = config.AUTO_START or not interactive
    if telegram_notify.has_admin() and not run_now:
        telegram_notify.notify_admins(
            "🤖 <b>Bot onlayn</b>  💤\nSkan gözləyir. Başlatmaq üçün /baslat yazın.",
            html=True,
            keyboard=views.menu_kb(ctl),
        )

    try:
        while True:
            if run_now:
                await run_cycle(ctl, resume)
                if not interactive or ctl.shutdown.is_set():
                    break
                log("[BOT] 💤 Skan bitdi. Yeni əmr gözlənilir (Telegram: /baslat, /kapat)")
            waiters = {asyncio.create_task(ctl.start_event.wait()), asyncio.create_task(ctl.shutdown.wait())}
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for w in waiters:
                w.cancel()
            if ctl.shutdown.is_set():
                break
            resume, run_now = ctl.resume_next, True
    finally:
        if telegram_notify.has_admin():
            telegram_notify.notify_admins("⏻ <b>Bot söndürüldü.</b> 👋", html=True)
        await telegram_notify.shutdown()
        for t in bg:
            t.cancel()
        await asyncio.gather(*bg, return_exceptions=True)
        _ = alerts  # noqa


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Dayandırıldı. results/scanned.txt saxlanılıb, növbəti işə salma (--resume) davam edəcək.")
