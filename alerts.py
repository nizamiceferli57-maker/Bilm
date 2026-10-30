"""Bildirişlərin marşrutu: sandıq → admin(lər) + kanallar (filtrə görə), blok xəbərdarlığı, son hesabat."""
import time

import chest_format
import config
import luckybag
import telegram_notify
from settings import settings

_last_block_alert = float("-inf")


def chest_alert(username: str, info: dict, n: int, record: bool, force: bool = False) -> None:
    text = chest_format.telegram_text(username, info, n, record)
    kw = dict(
        html=True,
        button_url=f"https://www.tiktok.com/@{username}/live",
        button_text="🔴 Canlı yayıma keç",
    )
    if force or not settings["muted"]:
        telegram_notify.notify_admins(text, **kw)
    ratio = info["ratio"]
    viewers = info.get("viewers") or 0
    # HƏR kanalın öz filtri var (ratio / jeton / izləyici sayı). force: əl ilə verilən ad → filtrə baxmadan göndər
    for ch in settings.active_channels("chest"):
        ok = force or (
            ratio is not None
            and ratio >= settings.ch_get(ch, "min_ratio")
            and info["coins"] >= settings.ch_get(ch, "min_coins")
            and (not viewers or viewers >= settings.ch_get(ch, "min_viewers"))   # say bilinmirsə süzgəc keçirir
        )
        if ok:
            ch["sent"] = ch.get("sent", 0) + 1
            telegram_notify.post(ch["id"], text, **kw)


def luckybag_alert(username: str, info: dict, n: int, force: bool = False, tier: str = "sure") -> None:
    text = luckybag.telegram_text(username, info, n)
    kw = dict(html=True, button_url=f"https://www.tiktok.com/@{username}/live", button_text="🔴 Canlı yayıma keç")
    if force or not settings["muted"] or tier == "maybe":
        telegram_notify.notify_admins(text, **kw)         # ehtimal səviyyəsi: yalnız admin (susdurulsa da gəlir, nadir olur)
    if tier == "maybe":
        return
    for ch in settings.active_channels("chest"):
        if force or settings.ch_get(ch, "luckybag"):
            telegram_notify.post(ch["id"], text, **kw)


def block_check(stats) -> None:
    """Son 10 dəqiqədə 4+ blok olubsa admin-ə (15 dəqiqədə bir dəfə) xəbər ver."""
    global _last_block_alert
    if not settings["alert_block"]:
        return
    now = time.monotonic()
    if stats.recent_blocks(600) >= 4 and now - _last_block_alert > 900:
        _last_block_alert = now
        telegram_notify.notify_admins(
            "🚨 <b>TikTok tez-tez blok edir!</b>\n"
            f"🚫 Son 10 dəq: <b>{stats.recent_blocks(600)}</b> blok\n"
            "🐢 Məsləhət: /surat → <b>Təhlükəsiz</b> və ya proxy əlavə edin.",
            html=True,
        )


def scan_started(stats, sched, resume_note: str, n_proxies: int, euler_on: bool, todo: int) -> None:
    text = "\n".join(
        [
            "🚀 <b>SKAN BAŞLADI</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"📋 Siyahı: <b>{stats.total:,}</b> ad  •  🔎 Yoxlanacaq: <b>{todo:,}</b>".replace(",", " "),
            f"♻️ {resume_note}",
            (f"⚡ Sürət: <b>{n_proxies}</b> proxy × <b>{max(1, round(sched.parallel_limit / max(len(sched.lanes), 1)))}</b> paralel • <b>{sched.avg_interval:.1f}s</b> interval"
             if n_proxies else f"⚡ Sürət: <b>{sched.parallel_limit}</b> paralel • <b>{sched.avg_interval:.1f}s</b> interval"),
            f"🌐 Bağlantı: <b>{'birbaşa' if not n_proxies else str(n_proxies) + ' proxy (xətalı olanlar avtomatik dincəlir)'}</b>",
            f"✔ Euler: <b>{'açıq' if euler_on else 'söndürülü'}</b>",
            f"📢 Kanal: <b>{len(settings.active_channels())}</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            "📊 /status • 📡 /panel • ⏸ /pauza",
        ]
    )
    telegram_notify.notify_admins(text, html=True)
