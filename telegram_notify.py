"""Telegram Bot API: admin(lər)ə və kanallara göndərmə (növbə ilə, 429-a davamlı), redaktə, fayl göndərmə.

Mesajlar çat başına ayrıca növbədə göndərilir: sandıq işçisi göndərməni gözləmir, kanal limiti (dəqiqədə ~20)
aşılsa Telegram-ın "retry_after" müddəti qədər gözlənilir və mesaj itmir.
"""
import asyncio
import re
import time

import httpx

import config
import group_gate
from log import log
from settings import settings

_client: httpx.AsyncClient | None = None
_transport = None  # yalnız testlər üçün
_queues: dict = {}
_tasks: list = []
_last_send: dict = {}
BOT: dict = {}  # getMe nəticəsi: id, username


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN)


def has_admin() -> bool:
    return enabled() and bool(config.ADMIN_IDS)


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=40, transport=_transport)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def call(method: str, payload: dict | None = None, *, files: dict | None = None, retries: int = 3, timeout: float = 40):
    """(ok, data). ok=True: data = 'result'. ok=False: data = xəta mətni."""
    if not enabled():
        return False, "token yoxdur"
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    err = ""
    for attempt in range(retries):
        try:
            if files:
                r = await _http().post(url, data=payload or {}, files=files, timeout=timeout)
            else:
                r = await _http().post(url, json=payload or {}, timeout=timeout)
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            err = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        if data.get("ok"):
            return True, data.get("result")
        err = str(data.get("description", r.status_code))
        mig = (data.get("parameters") or {}).get("migrate_to_chat_id")
        if mig:
            err += f" [migrate_to_chat_id={mig}]"      # qrup supergroup-a çevrilib: yeni id
        if r.status_code == 429:
            wait = float((data.get("parameters") or {}).get("retry_after", 5))
            await asyncio.sleep(min(wait + 0.5, 120))
            continue
        break  # 400/403 və s.: təkrarın mənası yoxdur
    return False, err


def _payload(chat_id, text: str, html: bool, keyboard, button_url: str, button_text: str, silent: bool) -> dict:
    p: dict = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    if html:
        p["parse_mode"] = "HTML"
    if silent:
        p["disable_notification"] = True
    if keyboard:
        p["reply_markup"] = {"inline_keyboard": keyboard}
    elif button_url:
        p["reply_markup"] = {"inline_keyboard": [[{"text": button_text or "Aç", "url": button_url}]]}
    return p


async def send_message(chat_id, text: str, *, html: bool = True, keyboard=None, button_url: str = "", button_text: str = "", silent: bool = False):
    """Dərhal göndər, message_id qaytar (uğursuzsa None). Əmr cavabları üçün."""
    ok, data = await call("sendMessage", _payload(chat_id, text, html, keyboard, button_url, button_text, silent))
    if not ok:
        log(f"[telegram] göndərilmədi ({chat_id}): {data}")
        return None
    return data.get("message_id")


async def edit_message(chat_id, message_id: int, text: str, *, html: bool = True, keyboard=None):
    """(ok, xəta). 'message is not modified' uğurlu sayılır."""
    p = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000], "disable_web_page_preview": True}
    if html:
        p["parse_mode"] = "HTML"
    p["reply_markup"] = {"inline_keyboard": keyboard or []}
    ok, data = await call("editMessageText", p, retries=2)
    if not ok and "not modified" in str(data):
        return True, ""
    return ok, ("" if ok else str(data))


async def send_document(chat_id, path, caption: str = ""):
    try:
        content = path.read_bytes()
    except OSError as exc:
        return False, str(exc)
    return await call(
        "sendDocument",
        {"chat_id": str(chat_id), "caption": caption[:900]},
        files={"document": (path.name, content)},
        timeout=90,
    )


async def download_file(file_id: str, max_bytes: int = 5_000_000) -> bytes | None:
    ok, f = await call("getFile", {"file_id": file_id})
    if not ok or not f.get("file_path"):
        return None
    if int(f.get("file_size") or 0) > max_bytes:
        return None
    try:
        r = await _http().get(f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{f['file_path']}", timeout=60)
        return r.content if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


# ---------- növbəli (fire-and-forget) göndərmə ----------
async def _sender(chat_id) -> None:
    q = _queues[chat_id]
    gap = 0.05 if (isinstance(chat_id, int) and chat_id > 0) else 1.2  # kanal/qrup: daha yavaş
    while True:
        payload = await q.get()
        wait = _last_send.get(chat_id, 0) + gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        ok, data = await call("sendMessage", payload)
        _last_send[chat_id] = time.monotonic()
        if not ok and "parse entities" in str(data).lower() and payload.get("parse_mode"):
            plain = {k: v for k, v in payload.items() if k != "parse_mode"}          # HTML xətası: etiketsiz düz mətnlə təkrar (mesaj itmir)
            plain["text"] = re.sub(r"<[^>]+>", "", payload.get("text", ""))
            ok, data = await call("sendMessage", plain)
            if ok:
                q.task_done()
                continue
        if not ok:
            m = re.search(r"migrate_to_chat_id=(-?\d+)", str(data))
            if m:                                           # qrup supergroup oldu → id yenilənir, mesaj itmir
                new_id = int(m.group(1))
                for ch in settings["channels"]:
                    if str(ch["id"]) == str(chat_id):
                        ch["id"] = new_id
                settings.save()
                log(f"[telegram] qrup id dəyişdi {chat_id} → {new_id}, mesaj yenidən göndərilir")
                payload = dict(payload, chat_id=new_id)
                ok, data = await call("sendMessage", payload)
                if ok:
                    q.task_done()
                    continue
            log(f"[telegram] göndərilmədi ({chat_id}): {data}")
            _warn_admin_send_fail(chat_id, str(data))
        q.task_done()


_warned: dict = {}


def _warn_admin_send_fail(chat_id, err: str) -> None:
    """Qrup/kanala yazmaq alınmırsa admin-ə (10 dəqiqədə bir dəfə) səbəbi və həlli yaz."""
    if isinstance(chat_id, int) and chat_id > 0:
        return                                              # admin-in öz çatı
    low = err.lower()
    if not any(k in low for k in ("rights", "forbidden", "kicked", "not found", "administrator", "can't", "cannot", "blocked", "permission")):
        return
    now = time.monotonic()
    if now - _warned.get(chat_id, -1e9) < 600:
        return
    _warned[chat_id] = now
    title = str(chat_id)
    for ch in settings["channels"]:
        if str(ch["id"]) == str(chat_id):
            title = ch.get("title") or title
            ch["err"] = err[:120]
    notify_admins(
        f"❌ <b>«{title}»</b> qrup/kanalına yaza bilmirəm.\n<code>{err[:150]}</code>\n"
        "👉 Botu orada <b>admin</b> edin və «Mesaj göndər» icazəsini verin. Admin etdiyiniz an özüm təsdiq edib bildirişlərə davam edəcəm.\n"
        "🔎 Yoxlamaq üçün: /kanal_yoxla", html=True)


async def probe(chat_id):
    """Qrupa/kanala yazmaq olurmu? Səssiz nöqtə göndərir və dərhal silir: qrupda HEÇ NƏ qalmır. (ok, xəta)"""
    ok, data = await call("sendMessage", {"chat_id": chat_id, "text": "·", "disable_notification": True}, retries=1)
    if not ok:
        return False, str(data)
    try:
        await call("deleteMessage", {"chat_id": chat_id, "message_id": data.get("message_id")}, retries=1)
    except Exception:
        pass
    return True, ""


def _is_group_target(chat_id) -> bool:
    """Admin-in öz çatı deyil, qeydli kanal/qrupdur?"""
    if chat_id in config.ADMIN_IDS or str(chat_id) in {str(a) for a in config.ADMIN_IDS}:
        return False
    return any(str(ch["id"]) == str(chat_id) for ch in settings["channels"])


def post(chat_id, text: str, *, html: bool = True, keyboard=None, button_url: str = "", button_text: str = "", silent: bool = False, gate: bool = True) -> None:
    if not enabled():
        return
    if gate and not group_gate.is_open() and _is_group_target(chat_id):
        group_gate.drop()                                   # qapı bağlıdır: qrupa/kanala getmir
        return
    if chat_id not in _queues:
        _queues[chat_id] = asyncio.Queue()
        _tasks.append(asyncio.create_task(_sender(chat_id)))
    _queues[chat_id].put_nowait(_payload(chat_id, text, html, keyboard, button_url, button_text, silent))


def notify_admins(text: str, **kw) -> None:
    for cid in config.ADMIN_IDS:
        post(cid, text, **kw)


def notify_channels(text: str, mode: str = "chest", **kw) -> None:
    """mode='chest': bütün aktiv kanallar. mode='all': yalnız 'hamısı' rejimli kanallar (hesabatlar)."""
    for ch in settings.active_channels(mode):
        post(ch["id"], text, **kw)


async def send(text: str, html: bool = False, button_url: str = "", button_text: str = "") -> bool:
    """Köhnə interfeys: admin(lər)ə göndər."""
    if not has_admin():
        return False
    notify_admins(text, html=html, button_url=button_url, button_text=button_text)
    return True


async def flush(timeout: float = 15) -> None:
    """Növbədəki mesajlar göndərilsin (proses bağlanmazdan əvvəl)."""
    async def _wait():
        for q in list(_queues.values()):
            await q.join()
    try:
        await asyncio.wait_for(_wait(), timeout)
    except (asyncio.TimeoutError, Exception):
        pass


async def shutdown() -> None:
    await flush()
    for t in _tasks:
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    _queues.clear()
    await close()
