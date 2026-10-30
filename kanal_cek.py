"""Telegram kanalındakı BÜTÜN postlardan (köhnələr də) TikTok linklərini oxuyub istifadəçi adlarını çıxarır.

Nəticə:  results/kanal_users.txt  (hər sətirdə bir ad, təkrarsız)
Sonra .txt faylı idarə botuna göndərilir (bot users.txt-ə əlavə edir və canlılara dərhal qoşulur).

Bot API kanalın köhnə postlarını oxuya bilmir, ona görə burada ÖZ Telegram hesabınla (Telethon) giriş edilir.
Qurulum (bir dəfə):  pip install telethon
  api_id və api_hash:  https://my.telegram.org → API development tools
İstifadə:
  python kanal_cek.py @kanal_adi            # bütün köhnə postlar → .txt → bota
  python kanal_cek.py @kanal_adi --izle     # sonra açıq qalır, yeni postları da göndərir
  python kanal_cek.py https://t.me/+xxxx    # özəl kanal (hesabın üzv olmalıdır)
Qeyd: giriş edən hesab botun ADMİNİ olmalıdır (TELEGRAM_CHAT_ID), yoxsa bot faylı qəbul etmir.
"""
import asyncio
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
RES = ROOT / "results"
RES.mkdir(exist_ok=True)
LINK = re.compile(r"tiktok\.com/@([A-Za-z0-9._]+)")


def load_env() -> dict:
    env = dict(os.environ)
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def names_of(msg) -> list[str]:
    urls = [msg.raw_text or ""]
    for e in (msg.entities or []):
        u = getattr(e, "url", None)
        if u:
            urls.append(u)
    for row in (msg.buttons or []):
        for b in row:
            if getattr(b, "url", None):
                urls.append(b.url)
    out = []
    for m in LINK.finditer("\n".join(urls)):
        n = m.group(1).rstrip(".")
        if n:
            out.append(n)
    return out


def bot_username(token: str) -> str:
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=15) as r:
            return json.load(r)["result"]["username"]
    except Exception:
        return ""


async def main() -> None:
    try:
        from telethon import TelegramClient, events
    except ImportError:
        sys.exit("Əvvəl:  pip install telethon")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    watch = "--izle" in sys.argv
    if not args:
        sys.exit(__doc__)
    env = load_env()
    api_id = env.get("TG_API_ID") or input("api_id: ").strip()
    api_hash = env.get("TG_API_HASH") or input("api_hash: ").strip()
    client = TelegramClient(str(ROOT / "kanal"), int(api_id), api_hash)
    await client.start()                       # ilk dəfə telefon nömrəsi + kod soruşur
    src = args[0]
    ent = await client.get_entity(int(src) if src.lstrip("-").isdigit() else src)
    bot = bot_username(env.get("TELEGRAM_BOT_TOKEN", ""))

    async def deliver(names: list[str], label: str) -> None:
        if not names:
            return
        path = RES / f"{label}.txt"
        path.write_text("\n".join(names) + "\n", encoding="utf-8")
        if bot:
            await client.send_file(bot, str(path), caption=f"{label}: {len(names)} ad")
            print(f"📤 {len(names)} ad botu @{bot}-a göndərildi")
        else:
            import userlist
            r = userlist.add_names(names)
            print(f"⚠ bot adı tapılmadı; users.txt-ə birbaşa yazıldı: +{r['added']}")

    seen: dict[str, None] = {}
    count = 0
    async for m in client.iter_messages(ent, limit=None):
        count += 1
        for n in names_of(m):
            seen.setdefault(n, None)
        if count % 500 == 0:
            print(f"… {count} post oxundu, {len(seen)} ad")
    uniq: dict[str, str] = {}
    for n in seen:
        uniq.setdefault(n.lower(), n)
    names = sorted(uniq.values(), key=str.lower)
    (RES / "kanal_users.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    print(f"✅ {count} post oxundu → {len(names)} təkrarsız ad → results/kanal_users.txt")
    await deliver(names, "kanal_users")

    if watch:
        known = set(uniq)

        @client.on(events.NewMessage(chats=ent))
        async def _new(ev):
            fresh = [n for n in names_of(ev.message) if n.lower() not in known]
            for n in fresh:
                known.add(n.lower())
            if fresh:
                await deliver(sorted(set(fresh)), "kanal_yeni")

        print("👁 Yeni postlar izlənir (dayandırmaq: Ctrl+C)")
        await client.run_until_disconnected()
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
