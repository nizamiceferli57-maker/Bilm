"""Sandıq hadisəsindən məlumatı çıxarır və Telegram mesajını (HTML) hazırlayır."""
import html
import time

import config


def _to_dict(event) -> dict:
    for name in ("to_dict", "to_pydict"):
        fn = getattr(event, name, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return {}


def _walk(d, path=""):
    if isinstance(d, dict):
        for k, v in d.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(d, (list, tuple)):
        for i, v in enumerate(d[:30]):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, d


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


COIN_KEYS = ("coin", "diamond")
PEOPLE_KEYS = ("can_open", "people", "open_num", "open_count", "user_count", "usercount")
TS_KEYS = ("timestamp", "send_time", "sendtime")


def extract(event) -> dict:
    """EnvelopeEvent -> {coins, can_open, ratio, ts, names, raw}.
    1) köhnə sahələr (treasure_box_data), 2) olmasa bütün sahələrdə ada görə axtarış (TikTokLive 7.x sxemi dəyişib)."""
    data = getattr(event, "treasure_box_data", None)
    coins = _num(getattr(data, "coins", 0))
    can_open = _num(getattr(data, "can_open", 0))
    ts = _num(getattr(data, "timestamp", 0))
    raw = _to_dict(event)
    if not coins or not can_open:
        for path, val in _walk(raw):
            key = path.lower().split(".")[-1]
            n = _num(val)
            if n <= 0 or isinstance(val, (bool, str)) and not str(val).isdigit():
                continue
            if not coins and any(w in key for w in COIN_KEYS):
                coins = n
            elif not can_open and any(w in key for w in PEOPLE_KEYS) and n < 100000:
                can_open = n
            elif not ts and any(w in key for w in TS_KEYS):
                ts = n
    env = raw.get("envelopeInfo") or raw.get("envelope_info") or {}
    if isinstance(env, dict):
        coins = coins or _num(env.get("diamondCount", env.get("diamond_count")))
        can_open = can_open or _num(env.get("peopleCount", env.get("people_count")))
        unpack_at = _num(env.get("unpackAt", env.get("unpack_at")))
        env_id = str(env.get("envelopeId", env.get("envelope_id", "")) or "")
        sender = str(env.get("sendUserName", env.get("send_user_name", "")) or "")
        business = str(env.get("businessType", env.get("business_type", "")) or "")
    else:
        unpack_at, env_id, sender, business = 0, "", "", ""
    unpack_in = unpack_at - int(time.time()) if unpack_at > 1_000_000_000 else None
    ratio = round(coins / can_open, 2) if coins > 0 and can_open > 0 else None
    names: list[str] = []
    try:  # yalnız diaqnostika üçün: sandıqla əlaqəli istifadəçilər
        for u3 in event.treasure_box_user.user2.user3:
            u = u3.user4.user
            names.append(u.nickname or u.unique_id)
    except AttributeError:
        pass
    return {
        "coins": coins, "can_open": can_open, "ratio": ratio, "ts": ts, "names": names, "raw": raw,
        "env_id": env_id, "sender": sender, "business": business, "unpack_in": unpack_in, "display": str(raw.get("display", "")),
    }


def describe(info: dict) -> str:
    """Konsol/xülasə üçün qısa mətn: '20🪙/16👥 = 1.25'."""
    if info["ratio"] is None:
        return "məlumat yoxdur"
    return f"{info['coins']}🪙/{info['can_open']}👥 = {info['ratio']:.2f}"


def tier(ratio: float | None) -> str:
    if ratio is None:
        return "❔ Məlumat yoxdur"
    if ratio >= config.RATIO_GREAT:
        return "💎 ƏLA"
    if ratio >= config.RATIO_GOOD:
        return "🔥 YAXŞI"
    return "🙂 NORMAL"


def telegram_text(username: str, info: dict, n: int, record: bool = False) -> str:
    """Sandıq bildirişi (HTML). username yalnız hərf/rəqəm/./_ olduğu üçün təhlükəsizdir, yenə də escape olunur."""
    u = html.escape(username)
    bar = "━━━━━━━━━━━━━━━━"
    lines = ["📦 <b>SANDIQ VAR İDİ</b> (yayımdan silindi)" if info.get("existed") else "🎁✨ <b>SANDIQ TAPILDI!</b> ✨🎁"]
    if record:
        lines.append("🏆🔥 <b>YENİ REKORD!</b> 🔥🏆")
    lines += [bar, f"👤 <b>@{u}</b>"]
    if info["ratio"] is not None:
        c, p, r = info["coins"], info["can_open"], info["ratio"]
        lines += [
            f"🪙 Jeton: <b>{c}</b>",
            f"👥 Qazana bilər: <b>{p} nəfər</b>",
            f"📊 Ratio: <b>{r:.2f}</b> jeton/nəfər",
            f"🧮 <code>{c} ÷ {p} = {r:.2f}</code>",
            f"🎯 Səviyyə: <b>{tier(r)}</b>",
        ]
        if info.get("sender"):
            lines.append(f"🎁 Göndərən: <b>{html.escape(info['sender'])}</b>")
        left = info.get("unpack_in")
        if left is not None:  # unpackAt: sandığın açılma vaxtı (adətən qoyulandan ~5 dəq sonra)
            lines.append(f"⏳ Açılmasına: <b>{max(left, 0) // 60} dəq {max(left, 0) % 60} san</b>" if left > 0 else "⏳ Açılma vaxtı çatıb")
    else:
        lines.append("ℹ️ Bu yayımda sandıq qoyulur, amma dəyəri (jeton/nəfər) əvvəlcədən gəlmədi — yayımı izləyirəm, növbətini tutacağam" if info.get("existed") else "ℹ️ Sandıq məlumatı (jeton/nəfər) gəlmədi")
    viewers = info.get("viewers")
    if viewers:
        lines.insert(len(lines) - 0, f"👁 Canlıda indi: <b>{viewers:,}</b> nəfər".replace(",", " "))
        p = info.get("can_open") or 0
        if p and viewers >= p:
            lines.append(f"🎲 Qazanma şansı: ~<b>{p / viewers * 100:.0f}%</b> ({p} nəfər / {viewers:,} izləyici)".replace(",", " "))
    elif info.get("viewers_unknown"):
        lines.append("👁 Canlıdakı say: alınmadı")
    lines += [
        bar,
        f"🕒 {time.strftime('%H:%M:%S')} • sandıq #{n}",
        f'🔴 <a href="https://www.tiktok.com/@{u}/live">Canlı yayıma keç</a>',
    ]
    return "\n".join(lines)
