"""Əl ilə bildiriş: /hd (hədiyyə çantası) və /sd (sandıq).

Admin məlumatları özü verir (link, canlıda neçə nəfər, jeton, qazana bilən sayı); bot yayıma QOŞULMUR,
ordu/skanla heç bir əlaqəsi yoxdur — yalnız mesaj hazırlayıb adminə və bütün aktiv kanallara/qruplara göndərir.
"""
import html
import re
import time

import chest_format
import telegram_notify
from settings import settings

NAME_RE = re.compile(r"^[A-Za-z0-9._]{1,24}$")
_AT_RE = re.compile(r"@([A-Za-z0-9._]{1,24})")

# addımlar: (açar, sual)
STEPS = [
    ("user", "🔗 <b>Canlı yayımın linkini</b> yazın (məs. <code>https://www.tiktok.com/@ad/live</code>) və ya sadəcə <b>istifadəçi adını</b> (<code>ad</code> / <code>@ad</code>)."),
    ("viewers", "👁 Canlıda <b>indi neçə nəfər</b> var?"),
    ("coins", "🪙 Neçə <b>jeton</b>?"),
    ("can_open", "👥 Neçə <b>nəfər qazana bilər</b>?"),
]
TITLES = {"hd": "🛍 <b>HƏDİYYƏ ÇANTASI</b> mesajı hazırlanır", "sd": "🎁 <b>SANDIQ</b> mesajı hazırlanır"}


def parse_user(text: str) -> str | None:
    """Link və ya ad → TikTok istifadəçi adı (tapılmasa None)."""
    t = (text or "").strip()
    m = _AT_RE.search(t)
    if m:
        return m.group(1)
    t = t.strip("/ ")
    return t if NAME_RE.match(t) else None


def parse_int(text: str) -> int | None:
    """'1 250', '1,250', '1250' → 1250. Mənfi/sıfır/yararsız → None."""
    digits = re.sub(r"[\s,._]", "", (text or "").strip())
    if digits.isdigit() and 0 < int(digits) < 10 ** 10:
        return int(digits)
    return None


def parse_answer(key: str, text: str):
    return parse_user(text) if key == "user" else parse_int(text)


def error_text(key: str) -> str:
    if key == "user":
        return "❌ Link/ad tanınmadı. Məs. <code>https://www.tiktok.com/@ad/live</code> və ya <code>@ad</code> yazın (ləğv: /legv)."
    return "❌ Rəqəm yazın (məs. <code>250</code>). Ləğv: /legv"


def _info(data: dict) -> dict:
    coins, p = data["coins"], data["can_open"]
    return {"coins": coins, "can_open": p, "ratio": round(coins / p, 2), "viewers": data["viewers"], "existed": False}


def chest_text(data: dict) -> str:
    """Sandıq: həmişəki sandıq bildirişi ilə eyni format (yalnız avtomatik 'sandıq #N' sayğacı çıxarılır)."""
    text = chest_format.telegram_text(data["user"], _info(data), 0)
    return text.replace(" • sandıq #0", "")


def bag_text(data: dict) -> str:
    """Hədiyyə çantası: Lucky Bag başlığı + sandıq ilə eyni məlumat sətirləri."""
    u = html.escape(data["user"])
    info = _info(data)
    c, p, r, v = info["coins"], info["can_open"], info["ratio"], info["viewers"]
    bar = "━━━━━━━━━━━━━━━━"
    lines = [
        "🛍️✨ <b>LUCKY BAG (HƏDİYYƏ ÇANTASI)!</b> ✨🛍️", bar, f"👤 <b>@{u}</b>",
        f"🪙 Jeton: <b>{c}</b>",
        f"👥 Qazana bilər: <b>{p} nəfər</b>",
        f"📊 Ratio: <b>{r:.2f}</b> jeton/nəfər",
        f"🧮 <code>{c} ÷ {p} = {r:.2f}</code>",
        f"🎯 Səviyyə: <b>{chest_format.tier(r)}</b>",
        f"👁 Canlıda indi: <b>{v:,}</b> nəfər".replace(",", " "),
    ]
    if v >= p:
        lines.append(f"🎲 Qazanma şansı: ~<b>{p / v * 100:.0f}%</b> ({p} nəfər / {v:,} izləyici)".replace(",", " "))
    lines += [bar, f"🕒 {time.strftime('%H:%M:%S')}", f'🔴 <a href="https://www.tiktok.com/@{u}/live">Canlı yayıma keç</a>', "⚡ Tez ol, yayıma keç!"]
    return "\n".join(lines)


def send(kind: str, data: dict) -> int:
    """Mesajı adminlərə və BÜTÜN aktiv kanal/qruplara göndərir (filtr və /sessiz nəzərə alınmır). Kanal sayını qaytarır."""
    text = chest_text(data) if kind == "sd" else bag_text(data)
    kw = dict(html=True, button_url=f"https://www.tiktok.com/@{data['user']}/live", button_text="🔴 Canlı yayıma keç")
    telegram_notify.notify_admins(text, **kw)
    chans = settings.active_channels("chest")
    for ch in chans:
        ch["sent"] = ch.get("sent", 0) + 1
        telegram_notify.post(ch["id"], text, gate=False, **kw)
    return len(chans)
