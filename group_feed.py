"""Qrupa/kanala canlı haqqında YALNIZ SAY məlumatı: neçə nəfər yoxlanıb, neçə nəfər canlı, neçə nəfər offline.

Canlı adları siyahı kimi göndərilmir. Mesaj hər dəfə yenisi atılmır — eyni mesaj redaktə olunur (qrup təmiz qalır);
mesaj silinibsə və ya redaktə alınmırsa yenisi göndərilir. Hesabat, status, xəta və s. qrupa getmir (yalnız admin-ə).
Sandıqlı canlı xəbəri alerts.py-dən ayrıca gəlir.
"""
import time

import config
import telegram_notify as tg
import views
from log import log
from settings import settings

_LAST = {"text": ""}


def text_of(ctl) -> str:
    s = ctl.stats
    pct = (s.scanned / s.total * 100) if s.total else 0.0
    n = views.num
    return "\n".join([
        "📊 <b>İZLƏMƏ MƏLUMATI</b>",
        views.LINE,
        f"👥 Yoxlanıb: <b>{n(s.scanned)}</b> / {n(s.total)}  ({pct:.0f}%)",
        f"🟢 Canlı: <b>{n(s.live)}</b>",
        f"⚪ Offline: <b>{n(s.offline)}</b>",
        f"❌ Tapılmadı: <b>{n(s.notfound)}</b>",
        f"🔌 İndi izlənir: <b>{n(len(s.fleet_open))}</b>",
        views.LINE,
        f"🕒 {time.strftime('%H:%M')}",
    ])


async def summary(ctl, force: bool = False) -> int:
    """Say məlumatını bütün aktiv qrup/kanallara göndər (əvvəlki mesajı redaktə et). Qaytarır: yenilənən qrup sayı."""
    s = ctl.stats
    chans = settings.active_channels("chest")
    if not chans or not s.scanned:
        return 0
    text = text_of(ctl)
    body = text.rsplit("\n", 2)[0]                       # saat sətirindən başqa məzmun dəyişməyibsə təkrar redaktə etmə
    if not force and body == _LAST["text"]:
        return 0
    _LAST["text"] = body
    done = 0
    for ch in chans:
        mid = ch.get("sum_id")
        ok = False
        if mid:
            ok, _err = await tg.edit_message(ch["id"], int(mid), text)
        if not ok:
            new_id = await tg.send_message(ch["id"], text, silent=True)
            if new_id:
                ch["sum_id"] = new_id
                settings.save()
                ok = True
        done += 1 if ok else 0
    if done:
        log(f"[qrup] say məlumatı yeniləndi ({done} qrup/kanal)")
    return done


_ = config  # noqa
