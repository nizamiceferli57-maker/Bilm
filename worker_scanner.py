"""İŞÇİ 1: users.txt-dəki adları yoxlayır, canlı olanları növbəyə qoyur.

Gücləndirilmiş məntiq:
  1. Ad siyahısı təmizlənir: @ silinir, böyük/kiçik hərf təkrarları atılır, TikTok formatına
     uyğun olmayan adlar results/invalid.txt-ə yazılır.
  2. Əsas yoxlama PirateTok check_online.
  3. PirateTok "canlı" deyərsə, room_id Euler /webcast/fetch ilə təsdiqlənir (EULER_VERIFY_LIVE=1). Euler aydın
     şəkildə "canlı deyil" desə, dinləməyə göndərilmir. Cavab aydın deyilsə, nəticə "təsdiqsiz" saxlanır.
  4. Paralel işçi sayı 4-dən başlayır, hər blokda 1 azalır (min 1) və blok olmayan səviyyədə qalır (rate_limit.py).
     Proxy siyahısı varsa, hər proxy ayrıca kanal kimi işləyir və biri bloklananda digərləri davam edir.
  5. Xəta/blok qalan adlar üçün əlavə dövrlər (SCAN_RETRY_ROUNDS). Sonda da qalanlar results/failed.txt-ə
     yazılır və scanned.txt-ə düşmür, növbəti işə salmada yenidən yoxlanır.
"""
import asyncio
import random
import re
import time

from piratetok_live.errors import PirateTokError
from piratetok_live.http.ua import random_ua
from piratetok_live.http.transport import proxies_for
from piratetok_live import (
    AgeRestrictedError,
    HostNotOnlineError,
    TikTokBlockedError,
    UserNotFoundError,
    check_online,
)

import config
import fleet
import telegram_notify
from alerts import block_check, scan_started
from euler_client import EulerClient
from log import err_kind, log, short_err
from rate_limit import Lane, Scheduler, proxy_down
from settings import settings

_VALID_NAME = re.compile(r"^[a-z0-9._]{1,24}$")
_PASS = {"n": 0}   # neçənci dövr (ilk dövrdə arxivləmə və xəbərdarlıq)


def load_usernames() -> tuple[list[str], list[str]]:
    """(təmiz adlar, yararsız adlar). Təkrarlar böyük/kiçik hərfə baxmadan atılır."""
    seen, good, bad = set(), [], []
    skip = settings.ignored()  # Telegram /istisna ilə seçilən adlar skan olunmur
    for line in config.USERS_FILE.read_text(encoding="utf-8").splitlines():
        name = line.strip().lstrip("@").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen or key in skip:
            continue
        seen.add(key)
        (good if _VALID_NAME.match(key) else bad).append(name)
    return good, bad


def load_done() -> set[str]:
    """Davam etmək üçün: əvvəl yoxlananları keç."""
    f = config.RESULTS_DIR / "scanned.txt"
    if not f.exists():
        return set()
    return {l.strip().lower() for l in f.read_text(encoding="utf-8").splitlines() if l.strip()}


def _archive_scanned() -> str:
    """Sıfırdan başlama: köhnə scanned.txt silinmir, results/archive/ qovluğuna köçürülür."""
    f = config.RESULTS_DIR / "scanned.txt"
    if not f.exists() or f.stat().st_size == 0:
        return "sıfırdan başlayır"
    n = sum(1 for _ in f.open(encoding="utf-8"))
    arch = config.RESULTS_DIR / "archive"
    arch.mkdir(exist_ok=True)
    dest = arch / f"scanned-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    f.replace(dest)
    return f"sıfırdan başlayır; köhnə scanned.txt ({n} ad) → results/archive/{dest.name}"


def _append(name: str, line: str) -> None:
    with open(config.RESULTS_DIR / name, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


class _ErrAgg:
    """Proxy/şəbəkə xətalarını hər ad üçün ayrı sətirlə yox, 30 saniyədə bir yığcam hesabatla göstərir."""

    def __init__(self) -> None:
        self.kinds: dict[str, int] = {}
        self.last = time.monotonic()

    def note(self, kind: str) -> None:
        self.kinds[kind] = self.kinds.get(kind, 0) + 1

    def flush(self, sched, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last < 30:
            return
        self.last = now
        if not self.kinds:
            return
        total = sum(self.kinds.values())
        top = ", ".join(f"{k} {v}" for k, v in sorted(self.kinds.items(), key=lambda kv: -kv[1])[:4])
        self.kinds = {}
        h = sched.health() if sched else None
        px = f" | proxy: işləyir {h['up']}/{h['total']}, dincəlir {h['down']}" if h and sched.lanes[0].proxy else ""
        log(f"[SKAN] ⚠ son 30s-də {total} şəbəkə xətası ({top}){px} — xətalı proxy-lər avtomatik dincəlir, ad təkrar yoxlanır")


_AGG = _ErrAgg()


async def _tiktok_check(username: str, lane) -> tuple[str, str | None]:
    """('live', room_id) | ('offline', None) | ('notfound', None) | ('blocked', None) | ('error', None)
    | ('proxyfail', None): proxy/şəbəkə qoşulma xətası (ad haqqında heç nə bilinmir)"""
    try:
        res = await asyncio.to_thread(check_online, username, config.SCAN_TIMEOUT, lane.proxy, lane.ua)
        return "live", res.room_id
    except HostNotOnlineError:
        return "offline", None
    except UserNotFoundError:
        return "notfound", None
    except TikTokBlockedError:
        return "blocked", None
    except AgeRestrictedError:
        return "error", None  # yaşa məhdudiyyətli: room_id çıxmır, növbəti dövrdə təkrar yoxlanacaq
    except PirateTokError as exc:
        log(f"[SKAN] @{username} → ⚠ xəta: {type(exc).__name__}: {short_err(exc)}")
        return "error", None
    except Exception as exc:
        # curl_cffi: ConnectionError / Timeout / ProxyError və s. → proxy və ya şəbəkə problemi
        _AGG.note(err_kind(exc))
        if lane.proxy and "402" in str(exc) and "tunnel" in str(exc).lower():
            # 402 = proxy provayderi icazə vermir (trafik limiti bitib / ödəniş): 30 dəq dincəlir, loga bir dəfə yazılır
            from rate_limit import PROXY_DOWN
            if PROXY_DOWN.get(lane.proxy, 0.0) < time.monotonic():
                log(f"[SKAN] 💳 proxy 402 (limit/ödəniş?): {lane.proxy.split('@')[-1][:40]} — 30 dəq dincəlir")
            PROXY_DOWN[lane.proxy] = time.monotonic() + 1800
        return ("proxyfail" if lane.proxy else "error"), None


async def resolve(username: str, euler: EulerClient, stats, sched: Scheduler) -> tuple[str, str | None, str]:
    """Bir adı tam yoxlayır. Qaytarır: (status, room_id, euler_qeydi).
    status: live | offline | notfound | blocked | error
    euler_qeydi: '' | 'təsdiq' | 'təsdiqsiz' | 'təkzib'
    """
    lane = await sched.acquire()
    issued = time.monotonic()
    status, room_id = await _tiktok_check(username, lane)
    note = ""
    if status == "blocked":
        stats.blocked_hits += 1
        stats.mark_block()
        sched.report(lane, "blocked", issued)
        stats.cooldown_until = max(stats.cooldown_until, lane.cooldown_until)
        block_check(stats)
    elif status == "proxyfail":
        sched.report(lane, "proxyfail", issued)
        status = "error"
    elif status == "error":
        sched.report(lane, "neutral", issued)
    else:
        sched.report(lane, "ok", issued)
    stats.scan_interval = sched.avg_interval
    stats.scan_workers = sched.parallel_limit
    _AGG.flush(sched)

    if status in ("blocked", "error"):
        return status, None, note

    # Təsdiq: PirateTok "canlı" dedi → Euler /webcast/fetch ilə room_id-ni yoxla
    if status == "live" and euler.enabled and config.EULER_VERIFY_LIVE:
        verdict = await euler.verify_room(username, room_id)
        if verdict == "live":
            note = "təsdiq"
            stats.euler_verified += 1
        elif verdict == "offline":
            note = "təkzib"
            stats.euler_rejected += 1
            return "offline", None, note
        elif verdict == "unknown":
            note = "təsdiqsiz"
            stats.euler_unverified += 1
    return status, room_id, note


def _make_sched() -> tuple[Scheduler, list[str]]:
    proxies = []
    for p in config.PROXIES:
        try:
            proxies_for(p)
            proxies.append(p)
        except ValueError as exc:
            log(f"[SKAN] ⚠ proxy keçildi: {exc}")
    sched = Scheduler(proxies, random_ua)
    # Telegram /surat ilə seçilmiş sürət yadda qalır; seçilməyibsə loqda sınanmış "Normal" (1 paralel, 1.2s)
    default_w = config.SCAN_PER_LANE if (proxies and not config.PROXY_FALLBACK) else 1
    sp = settings["speed"] or {"workers": default_w, "interval": config.SCAN_SPACING if proxies else 1.2}
    sched.set_speed(int(sp["workers"]), float(sp["interval"]))
    if settings["fixed_rate"]:
        sched.set_fixed(float(settings["fixed_rate"]))
    return sched, proxies


async def check_one(username: str, ctl) -> tuple[str, str | None, str]:
    """Telegram /yoxla: bir adı dərhal yoxla. (status, room_id, euler_qeydi)"""
    own_euler = ctl.euler is None
    euler = ctl.euler or EulerClient()
    sched = ctl.sched or _make_sched()[0]
    try:
        return await resolve(username, euler, ctl.stats, sched)
    finally:
        if own_euler:
            await euler.close()


async def priority_check(username: str, ctl) -> tuple[str, str | None, str]:
    """Telegram-dan verilen ad: sıraya və soyumaya baxmadan dərhal yoxlanır (3 cəhd).
    Skanerin ortaq planlaşdırıcısına toxunmur, ona görə skanı yavaşlatmır və cəzalandırmır.
    (status, room_id, euler_qeydi)"""
    own_euler = ctl.euler is None
    euler = ctl.euler or EulerClient()
    try:
        status, room_id = "error", None
        for attempt in range(3):
            alive = [p for p in config.PROXIES if not proxy_down(p)] or list(config.PROXIES)
            proxy = random.choice(alive) if alive else ""
            if config.PROXY_FALLBACK and attempt == 0:
                proxy = ""          # fallback rejimi: əvvəl öz IP, alınmasa proxy
            lane = Lane(proxy=proxy, ua=random_ua(), interval=0.0, base_interval=0.0, level=1, max_level=1)
            status, room_id = await _tiktok_check(username, lane)
            if status in ("live", "offline", "notfound"):
                break
            await asyncio.sleep(1.5)
        if status == "proxyfail":
            status = "error"
        if status in ("blocked", "error"):
            return status, None, ""
        note = ""
        if status == "live" and euler.enabled and config.EULER_VERIFY_LIVE:
            verdict = await euler.verify_room(username, room_id)
            if verdict == "offline":
                return "offline", None, "təkzib"
            note = {"live": "təsdiq", "unknown": "təsdiqsiz"}.get(verdict, "")
        return status, room_id, note
    finally:
        if own_euler:
            await euler.close()


async def run(queue: asyncio.Queue, stats, n_chest_workers: int, ctl, resume: bool = False) -> None:
    config.RESULTS_DIR.mkdir(exist_ok=True)
    try:
        await _run(queue, stats, ctl, resume)
    finally:
        # Nə olursa olsun sandıq işçilərinə "bitdi" siqnalı (aşağıda), ctl təmizlənir
        ctl.sched = None
        if ctl.euler is not None:
            await ctl.euler.close()
            ctl.euler = None
        for _ in range(n_chest_workers):
            await queue.put(None)
        stats.scan_finished = True
        log("[SKAN] Siyahı bitdi. Qalan canlı yayımlar dinlənir...")


async def _run(queue: asyncio.Queue, stats, ctl, resume: bool) -> None:
    names, invalid = load_usernames()
    for bad in invalid:
        _append("invalid.txt", bad)
    if resume:
        done = load_done()
        resume_note = f"davam rejimi: əvvəl yoxlanıb {len(done)}"
    else:
        done = set()
        resume_note = _archive_scanned() if _PASS["n"] == 0 else "yeni dövr"
    stats.total = len(names)
    stats.invalid = len(invalid)
    stats.scanned = sum(1 for n in names if n.lower() in done)
    todo = [n for n in names if n.lower() not in done and not fleet.tracking(n)]  # ordunun artıq izlədikləri keçilir
    stats.scanned0 = stats.scanned
    stats.started_at = time.monotonic()
    # Sıra: 1) əvvəl canlı tapılanlar (live.txt)  2) kanaldan gələnlər (results/kanal_users.txt)  3) qalanlar
    def _names_in(fname: str) -> set[str]:
        f = config.RESULTS_DIR / fname
        if not f.exists():
            return set()
        return {ln.split()[0].lower() for ln in f.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()}

    try:
        prev, kan = _names_in("live.txt"), _names_in("kanal_users.txt")
        t1 = [n for n in todo if n.lower() in prev]
        t2 = [n for n in todo if n.lower() in kan and n.lower() not in prev]
        t3 = [n for n in todo if n.lower() not in prev and n.lower() not in kan]
        todo = t1 + t2 + t3
        if t1 or t2:
            resume_note += f"; əvvəl canlı olan {len(t1)} ad, sonra kanaldan {len(t2)} ad birinci yoxlanır"
    except Exception:
        pass

    euler = EulerClient()
    sched, proxies = _make_sched()
    ctl.euler, ctl.sched = euler, sched
    log(
        f"[SKAN] Siyahıda {stats.total} düzgün ad, yararsız {len(invalid)}, indi yoxlanacaq {len(todo)} ({resume_note}) "
        f"| Euler: {'AÇIQ' if euler.enabled else 'söndürülü (EULER_API_KEY yoxdur)'} "
        f"| Kanal: {len(sched.lanes)} ({'birbaşa + ehtiyat proxy' if sched.fallback else ('proxy' if proxies else 'birbaşa')}), başlanğıc paralel {sched.parallel_limit}, interval {sched.avg_interval:.1f}s"
    )

    if _PASS["n"] == 0:
        scan_started(stats, sched, resume_note, len(proxies), euler.enabled, len(todo))
    _PASS["n"] += 1
    stats.scan_finished = False

    held: list = []   # "skan bitəndən sonra qoşul" rejimi: tapılan canlılar burada yığılır

    async def flush_held(why: str = "siyahı yoxlanıb bitdi") -> None:
        if not held:
            return
        items = list(held)
        held.clear()
        if ctl.stop_requested:
            return
        log(f"[SKAN] 🔌 {why} → {len(items)} canlıya qoşulma başlayır")
        telegram_notify.notify_admins(f"🔌 <b>Tapılan {len(items)} canlıya qoşulma başlayır…</b> ({why})", html=True)
        for it in items:
            await queue.put(it)

    async def handle(username: str) -> str | None:
        """Uğurlu bitirsə None, yoxsa 'blocked'/'error' qaytarır (sonra təkrar yoxlanacaq)."""
        status, room_id, note = "error", None, ""
        for attempt in range(config.SCAN_NAME_ATTEMPTS):
            await ctl.wait_if_paused()
            if ctl.stop_requested:
                return "error"
            status, room_id, note = await resolve(username, euler, stats, sched)
            if status not in ("blocked", "error"):
                break
            if attempt + 1 < config.SCAN_NAME_ATTEMPTS:
                await asyncio.sleep(1.5 * (attempt + 1) + random.random())

        if status in ("blocked", "error"):
            return status

        stats.scanned += 1
        stats.mark_scanned()
        _append("scanned.txt", username.lower())
        tag = f" [Euler: {note}]" if note else ""
        prefix = f"[SKAN {stats.scanned}/{stats.total}] @{username}"
        if status == "live" and fleet.tracking(username):
            return None   # ordu onsuz da bu yayımdadır
        if status == "live":
            stats.live += 1
            stats.live_list[username] = {"room": room_id, "ts": time.time(), "note": note}
            _append("live.txt", f"{username} {room_id} {note or '-'}")
            log(f"{prefix} → 🟢 CANLI (room {room_id}){tag} — sandıq dinləməyə göndərildi")
            if settings["join_after_scan"]:
                held.append((username, room_id))
            else:
                await queue.put((username, room_id))
        elif status == "offline":
            stats.offline += 1
            log(f"{prefix} → ⚪ offline{tag}")
        elif status == "notfound":
            stats.notfound += 1
            _append("notfound.txt", username)
            log(f"{prefix} → ❌ hesab tapılmadı{tag}")
        return None

    async def run_pass(batch: list[str]) -> list[str]:
        """İşçi hovuzu: sched.parallel_limit-dən yuxarı nömrəli işçilər "park" olur, blokdan sonra səviyyə enir."""
        pending: asyncio.Queue = asyncio.Queue()
        for n in batch:
            pending.put_nowait(n)
        failed_now: list[str] = []

        async def worker(idx: int) -> None:
            while True:
                while idx >= min(sched.parallel_limit, config.SCAN_MAX_PARALLEL):
                    if pending.empty() or ctl.stop_requested:
                        return
                    await asyncio.sleep(0.5)
                await ctl.wait_if_paused()
                if ctl.stop_requested:
                    return
                if held and not settings["join_after_scan"]:
                    await flush_held("«skandan sonra qoşul» söndürüldü")   # düymə söndürüləndə yığılanlar dərhal buraxılır
                try:
                    name = pending.get_nowait()
                except asyncio.QueueEmpty:
                    await flush_held("əsas yoxlama bitdi")                  # qalan təkrar dövrləri gözləmə
                    return
                if await handle(name) is not None:
                    failed_now.append(name)

        await asyncio.gather(*(worker(i) for i in range(min(sched.worker_cap, config.SCAN_MAX_PARALLEL))))
        return failed_now

    try:
        failed = await run_pass(todo)
        for rnd in range(1, config.SCAN_RETRY_ROUNDS + 1):
            if not failed or ctl.stop_requested:
                break
            log(f"[SKAN] Dövr {rnd}/{config.SCAN_RETRY_ROUNDS}: xəta/blok olan {len(failed)} ad yenidən yoxlanılır ({config.SCAN_ROUND_PAUSE}s sonra)")
            await asyncio.sleep(config.SCAN_ROUND_PAUSE)
            failed = await run_pass(failed)
    finally:
        await flush_held()    # nə olursa olsun tapılan canlılar itməsin

    _AGG.flush(sched, force=True)
    if ctl.stop_requested:
        log("[SKAN] 🛑 İstifadəçi dayandırdı. Yoxlananlar results/scanned.txt-dədir (/baslat davam ilə davam edin).")
        return
    stats.errors = len(failed)
    fail_file = config.RESULTS_DIR / "failed.txt"
    if failed:
        fail_file.write_text("\n".join(failed) + "\n", encoding="utf-8")
        log(f"[SKAN] ⚠ {len(failed)} ad bütün cəhdlərdən sonra yoxlanmadı → results/failed.txt (--resume ilə davam edəndə yenidən yoxlanacaq)")
    elif fail_file.exists():
        fail_file.unlink()
