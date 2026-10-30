"""İŞÇİ 2: canlı olan hesaba qoşulur və sandıq (envelope) hadisəsini dinləyir.

TikTokLive reposunun öz klienti və EnvelopeEvent hadisəsi istifadə olunur.
Yalnız oxuyur: heç nə açmır, giriş etmir, mesaj göndərmir.

Qeyd: TikTokLive-ın connect()/close() metodları sinxron run() üçün nəzərdə tutulub və
işləyən loop içində "This event loop is already running" xətası verir. Ona görə
bloklamayan start() istifadə olunur, bağlanma isə _safe_close ilə təhlükəsiz edilir.
"""
import asyncio
import base64
import json
import re
import time

import httpx
from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, EnvelopeEvent
from TikTokLive.events.proto_events import EVENT_MAPPINGS
from TikTokLive.client.errors import AgeRestrictedError

import alerts
import config
import chest_format
from log import log
from settings import settings


def _proxy():
    url = config.next_chest_proxy()
    return httpx.Proxy(url) if url else None


def _append(name: str, line: str) -> None:
    with open(config.RESULTS_DIR / name, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


QUICK_IDLE = config.QUICK_IDLE   # sürətli rejimdə: sandıq görünmürsə bu qədər saniyə sonra bitir (.env: QUICK_IDLE)
QUICK_AFTER_FOUND = 4   # sandıq tapılandan sonra bu qədər saniyə gözlə (əlavə sandıq üçün)
_SEEN_ENV: set = set()  # bütün dinləmələr üzrə artıq görülmüş sandıq ID-ləri
_SESSION_WARNED = {"done": False}
REPROBE_EVERY = config.REPROBE_EVERY      # uzun dinləmədə otaq məlumatı (room/info) bu qədər saniyədən bir yenidən yoxlanır
WATCH = ("envelope", "capsule", "pin", "box", "treasure", "chest", "guide", "toast", "notice", "prompt")


EXIST_KEYS = ("envelope", "treasure", "chest", "capsule", "box", "redpacket", "red_packet", "lucky", "reward", "welfare", "drop", "bag")


class _Wrap:
    def __init__(self, d: dict) -> None:
        self._d = d

    def to_dict(self) -> dict:
        return {"envelopeInfo": self._d, "display": "ROOM_INFO"}


def _find_chests(d, path: str = "") -> list:
    """Otaq məlumatında sandıq/zərf adlı açarları tapır: [(yol, qiymət)]."""
    res: list = []
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{path}.{k}" if path else str(k)
            if any(w in str(k).lower() for w in EXIST_KEYS) and v:
                res.append((p, v))
            else:
                res += _find_chests(v, p)
    elif isinstance(d, (list, tuple)):
        for i, v in enumerate(d[:50]):
            res += _find_chests(v, f"{path}[{i}]")
    return res


SNIFF_KEYS = ("envelope", "treasure", "redpacket", "red_packet", "chest", "capsule")
_DEC = json.JSONDecoder()


def _json_blobs(text: str) -> list:
    """Mətnin içindən JSON obyektlərini çıxarır (InRoomBanner/Pin kimi mesajlarda JSON mətn kimi gəlir)."""
    out, i = [], 0
    while True:
        i = text.find("{", i)
        if i < 0 or len(out) >= 8:
            return out
        try:
            obj, end = _DEC.raw_decode(text, i)
            out.append(obj)
            i = end
        except ValueError:
            i += 1


def _sniff_payload(meth: str, payload: bytes) -> list:
    """Bütün server mesajlarının (tanınan/tanınmayan) içində sandıq izi axtarır.
    Qaytarır: [dict] — sandıq kimi görünən obyektlər (envelopeInfo formatına salınmağa hazır)."""
    if not payload:
        return []
    text = payload.decode("utf-8", "ignore")
    low = text.lower()
    meth_hit = any(w in meth.lower() for w in SNIFF_KEYS + ("pin", "banner", "room"))
    if not meth_hit and not any(w in low for w in SNIFF_KEYS):
        return []
    items: list = []
    for obj in _json_blobs(text):
        for path, val in _find_chests(obj):
            if not any(w in path.lower() for w in SNIFF_KEYS):
                continue
            for it in (val if isinstance(val, (list, tuple)) else [val]):
                if isinstance(it, dict):
                    items.append(it)
    return items


def _scan_response(ev, username: str, methods: dict, unknown: dict, seen_resp: set, on_hit=None) -> None:
    """Serverdən gələn BÜTÜN mesaj növlərini (method) sayır, tanınmayanların xam məzmununu results/unknown_raw.txt-ə yazır."""
    raw = getattr(ev, "raw", None)
    if raw is None or id(raw) in seen_resp:
        return
    seen_resp.add(id(raw))
    for m in getattr(raw, "messages", None) or []:
        meth = str(getattr(m, "method", "") or "?")
        methods[meth] = methods.get(meth, 0) + 1
        if on_hit is not None and "EnvelopeMessage" not in meth:  # EnvelopeEvent özü ayrıca tutulur
            try:
                for it in _sniff_payload(meth, bytes(getattr(m, "payload", b"") or b"")):
                    on_hit(meth, it)
            except Exception:
                pass
        if any(w in meth for w in ("Pin", "Banner", "Envelope", "Treasure", "Welfare", "Goody")):
            try:  # diaqnostika: sandıq izi daşıya bilən mesajların xam məzmunu (results/pin_raw.txt, 300 KB-a qədər)
                f = config.RESULTS_DIR / "pin_raw.txt"
                if not (f.exists() and f.stat().st_size > 300_000):
                    pl = bytes(getattr(m, "payload", b"") or b"")
                    rec = {"vaxt": time.strftime("%H:%M:%S"), "ad": username, "method": meth, "bayt": len(pl),
                           "metnler": [x.decode("ascii", "ignore") for x in re.findall(rb"[ -~]{4,}", pl)[:20]],
                           "b64": base64.b64encode(pl[:1500]).decode()}
                    with open(f, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass
        if meth in EVENT_MAPPINGS:
            continue
        unknown[meth] = unknown.get(meth, 0) + 1
        if unknown[meth] > 2:
            continue
        try:
            f = config.RESULTS_DIR / "unknown_raw.txt"
            if f.exists() and f.stat().st_size > 300_000:
                continue
            payload = bytes(getattr(m, "payload", b"") or b"")
            strs = [x.decode("ascii", "ignore") for x in re.findall(rb"[ -~]{5,}", payload)[:12]]
            rec = {"vaxt": time.strftime("%H:%M:%S"), "ad": username, "method": meth, "bayt": len(payload), "metnler": strs,
                   "b64": base64.b64encode(payload[:900]).decode()}
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _dump_room(username: str, room_info) -> None:
    try:
        f = config.RESULTS_DIR / "room_info_raw.txt"
        if f.exists() and f.stat().st_size > 300_000:
            return
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"vaxt": time.strftime("%H:%M:%S"), "ad": username, "room_info": room_info}, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _dump_other(username: str, name: str, ev) -> None:
    """Diaqnostika: sandıqa bənzər digər hadisələrin xam məlumatı results/other_raw.txt-ə (300 KB-a qədər)."""
    try:
        f = config.RESULTS_DIR / "other_raw.txt"
        if f.exists() and f.stat().st_size > 300_000:
            return
        rec = {"vaxt": time.strftime("%H:%M:%S"), "ad": username, "hadise": name, "raw": chest_format._to_dict(ev)}
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _dump_raw(username: str, info: dict) -> None:
    """Diaqnostika: hər sandıq hadisəsinin xam məlumatı results/chest_raw.txt-ə yazılır (fayl 300 KB-dan böyüməz)."""
    try:
        f = config.RESULTS_DIR / "chest_raw.txt"
        if f.exists() and f.stat().st_size > 300_000:
            return
        rec = {"vaxt": time.strftime("%H:%M:%S"), "ad": username, "coins": info["coins"], "can_open": info["can_open"], "raw": info.get("raw")}
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


async def _safe_close(client) -> None:
    """Websocket-i bağla və HTTP sessiyasını bağla. Heç bir xəta işçini çökdürməsin."""
    try:
        await asyncio.wait_for(client.disconnect(), timeout=10)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
        pass
    try:
        await client._web.close()
    except (asyncio.CancelledError, Exception):
        pass


_SIGN_WARNED = {"done": False}


async def _envelope_api(client, room_id: str, env_id: str, username: str):
    """Sandığın jeton/nəfər dəyərini imzalı HTTP sorğusu ilə almağa çalışır (yol təxminidir: results/envelope_api.txt-də cavab saxlanır).
    Uğurlu olarsa chest_format.extract nəticəsini, əks halda None qaytarır."""
    from TikTokLive.client.web.web_settings import WebDefaults
    base = WebDefaults.tiktok_webcast_url.rstrip("/") + "/"
    for path in ("envelope/detail/", "envelope/list/"):
        try:
            r = await asyncio.wait_for(
                client._web.get(url=base + path, extra_params={"room_id": str(room_id), "envelope_id": env_id}, sign_url=True), timeout=12)
            body = r.text or ""
            _append("envelope_api.txt", f"{time.strftime('%H:%M:%S')} {username} {path} {r.status_code} {body[:1500]!r}")
            try:
                data = json.loads(body)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("data"):
                data = data["data"]
            i = chest_format.extract(_Wrap(data if isinstance(data, dict) else {"items": data}))
            if i["coins"] and i["can_open"]:
                return i
        except Exception as exc:
            _append("envelope_api.txt", f"{time.strftime('%H:%M:%S')} {username} {path} XƏTA {type(exc).__name__}: {str(exc)[:200]}")
            if "SIGN" in f"{type(exc).__name__}{exc}".upper() and not _SIGN_WARNED["done"]:
                _SIGN_WARNED["done"] = True
                log("[SANDIQ] ⚠ imza serveri cavab vermir (SIGN_NOT_200): .env-də EULER_API_KEY-i yoxlayın — sandıq dəyəri imzasız alınmır")
    return None


async def _listen(username: str, room_id: str, stats, force: bool = False, listen_seconds: int | None = None, out: dict | None = None, quick: bool = False, proxy_url: str | None = None, count: bool = True) -> int:
    """Bir yayımı dinləyir. Görülən sandıq sayını qaytarır. proxy_url: None=köhnə növbəli proxy, ''=birbaşa, 'http://..'=verilmiş."""
    listen_seconds = int(listen_seconds or settings["listen_seconds"])
    notify_max = int(settings["notify_max"])
    proxy = _proxy() if proxy_url is None else (httpx.Proxy(proxy_url) if proxy_url else None)
    kw = {}
    if config.SESSION_OK and config.WS_PLATFORM == "mobile":  # təcrübə: mobil qoşulma (sessiya tələb edir)
        from TikTokLive.client.web.routes.fetch_signed_websocket import WebcastPlatform
        kw["platform"] = WebcastPlatform.MOBILE
    client = TikTokLiveClient(unique_id=f"@{username}", web_proxy=proxy, ws_proxy=proxy, **kw)
    if config.SESSION_OK:  # giriş cookie-si → 18+ yayımların otaq məlumatı və mesajları açılır
        try:
            client._web.set_session(config.SESSION_ID, config.TT_TARGET_IDC)
        except Exception as exc:
            log(f"[SANDIQ] @{username} → sessiya təyin olunmadı ({type(exc).__name__})")
    elif config.SESSION_PROBLEM and not _SESSION_WARNED["done"]:
        _SESSION_WARNED["done"] = True
        log(f"[SANDIQ] ⚠ {config.SESSION_PROBLEM}")
    found = {"n": 0, "at": None}

    seen_types: dict = {}
    methods: dict = {}
    unknown: dict = {}
    seen_resp: set = set()
    if out is not None:
        out["types"] = seen_types
        out["methods"] = methods
        out["unknown"] = unknown
    _orig_emit = client.emit
    hit_cb = {"fn": None}

    def _emit(name, *a, **k):
        try:
            seen_types[str(name)] = seen_types.get(str(name), 0) + 1
            if str(name) == "WebsocketResponseEvent" and a:
                _scan_response(a[0], username, methods, unknown, seen_resp, lambda me, it: hit_cb["fn"] and hit_cb["fn"](me, it))
            if a and seen_types[str(name)] <= 3 and any(w in str(name).lower() for w in WATCH):
                _dump_other(username, str(name), a[0])
        except Exception:
            pass
        return _orig_emit(name, *a, **k)

    client.emit = _emit

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent):
        log(f"[SANDIQ] @{username} → 🔌 yayıma qoşuldum, {listen_seconds}s dinləyirəm")

    seen_keys: set = set()
    sent = {"n": 0}

    def _register(info: dict, tag: str = "") -> bool:
        """Sandıq məlumatını qeyd et və bildiriş göndər. Yeni sandıqdırsa True."""
        key = info["env_id"] or (info["ts"], info["coins"], info["can_open"])
        if key in seen_keys or key in _SEEN_ENV:
            return False  # eyni sandıq hadisəsinin təkrarı
        seen_keys.add(key)
        if info["env_id"]:
            _SEEN_ENV.add(key)
        found["n"] += 1
        if found["at"] is None:
            found["at"] = time.monotonic()
        stats.chest_users[username] = found["n"]
        log(f"[SANDIQ] @{username} → 📦 SANDIQ #{found['n']}{tag}: {chest_format.describe(info)}")
        if not stats.chest_debug_logged:  # bir dəfə: sahələrin düzgün oxunduğunu yoxlamaq üçün
            stats.chest_debug_logged = True
            log(f"[SANDIQ] (diaqnostika) timestamp={info['ts']} istifadəçilər={info['names'][:5]}")
        record = stats.add_chest(username, info)
        _append("chests.txt", f"{username} {room_id} {info['coins']} {info['can_open']} {info['ratio']}")
        if force or sent["n"] < notify_max:
            sent["n"] += 1
            alerts.chest_alert(username, info, found["n"], record, force=force)
        return True

    @client.on(EnvelopeEvent)
    async def on_envelope(event: EnvelopeEvent):
        info = chest_format.extract(event)
        _dump_raw(username, info)
        if info["ratio"] is None and not info["coins"]:  # jeton yoxdur (adətən HIDE = sandıq yayımdan silindi)
            if not info["env_id"]:
                log(f"[SANDIQ] @{username} → (boş sandıq hadisəsi keçildi: display={info.get('display') or '-'})")
                return
            # envelope_id var → sandıq YAYIMDA VAR İDİ (qoşulmazdan əvvəl qoyulub, dəyəri push ilə gəlmir).
            # Dəyəri imzalı API ilə almağa çalış; alınmasa da "sandıq var idi" kimi qeyd et və yayımda qal.
            det = await _envelope_api(client, room_id, info["env_id"], username)
            if det:
                info.update(coins=det["coins"], can_open=det["can_open"], ratio=det["ratio"])
                _register(info, " (API ilə dəyər alındı)")
            else:
                info["existed"] = True
                _register(info, " (yayımdan silindi, dəyər oxunmadı)")
            return
        _register(info)  # nəfər sayı gəlməyibsə də jeton varsa sandıq göstərilir (ratio olmadan)

    def on_hit(meth: str, item: dict) -> None:
        info = chest_format.extract(_Wrap(item))
        if info["ratio"] is None:
            return
        if _register(info, f" ({meth} içində, əvvəldən olan)"):
            _dump_other(username, f"SNIFF:{meth}", _Wrap(item))

    hit_cb["fn"] = on_hit

    async def probe_room() -> None:
        """Qoşulandan sonra otaq məlumatını (room/info) yoxla: əvvəldən olan sandıq orada ola bilər."""
        try:
            ri = await asyncio.wait_for(client._web.fetch_room_info(), timeout=12)
        except Exception as exc:
            note = f"otaq məlumatı alınmadı ({type(exc).__name__})"
            if isinstance(exc, AgeRestrictedError):
                if config.SESSION_OK:
                    note += " — 18+ yayımdır, sessiya göndərilib amma TikTok yenə rədd etdi: SESSION_ID köhnəlib/yanlışdır (tiktok.com-a yenidən daxil olub 'sessionid' cookie-sini yeniləyin)"
                elif config.SESSION_ID:
                    note += " — 18+ yayımdır; TT_TARGET_IDC boş olduğu üçün sessiya istifadə olunmur"
                else:
                    note += " — 18+ yayımdır; .env-də SESSION_ID + TT_TARGET_IDC verilsə otaq məlumatı da oxunar. Websocket mesajları yenə skan olunur"
            log(f"[SANDIQ] @{username} → {note}")
            if out is not None:
                out["probe"] = note
            return
        _dump_room(username, ri)
        if out is not None and isinstance(ri, dict):
            out["room_keys"] = list(ri.keys())[:40]
        hits = _find_chests(ri)
        new = 0
        for path, val in hits:
            for item in (val if isinstance(val, (list, tuple)) else [val]):
                if isinstance(item, dict):
                    info = chest_format.extract(_Wrap(item))
                    if info["ratio"] is not None and _register(info, " (əvvəldən olan)"):
                        new += 1
        note = (f"otaqda sandıq açarları: {', '.join(p for p, _ in hits[:6])}" if hits else "otaq məlumatında sandıq açarı tapılmadı")
        if hits and not new:
            note += " (jeton/nəfər oxunmadı)"
        log(f"[SANDIQ] @{username} → {note}")
        if out is not None:
            out["probe"] = note

    if count:
        stats.chest_active += 1
        stats.listening_now.add(username)
    log(f"[SANDIQ] @{username} → qoşulmağa çalışıram (room {room_id})")
    try:
        # room_id artıq məlumdur: otaq axtarışı və canlı yoxlaması təkrarlanmır (bloku azaldır)
        task = await client.start(room_id=int(room_id), fetch_live_check=False)
        if settings["probe_room"]:
            await probe_room()
        t0 = time.monotonic()
        last_probe = time.monotonic()
        while True:
            left = listen_seconds - (time.monotonic() - t0)
            if left <= 0:
                break
            done, _ = await asyncio.wait({task}, timeout=min(1.0, left))
            if task in done:
                log(f"[SANDIQ] @{username} → yayım özü bitdi")
                if not task.cancelled() and task.exception():
                    raise task.exception()
                break
            if settings["probe_room"] and listen_seconds >= 45 and time.monotonic() - last_probe >= REPROBE_EVERY:
                last_probe = time.monotonic()  # otağı müntəzəm yoxla (sandıq sonradan qoyula bilər)
                await probe_room()
            if quick:  # sürətli rejim: sandıq tapıldısa 2s, tapılmadısa QUICK_IDLE saniyə sonra bitir
                now = time.monotonic()
                if found["at"] is not None and now - found["at"] >= QUICK_AFTER_FOUND:
                    break
                if found["at"] is None and now - t0 >= QUICK_IDLE:
                    log(f"[SANDIQ] @{username} → sandıq yoxdur, tez bitirdim")
                    break
    except Exception as exc:
        stats.chest_errors += 1
        log(f"[SANDIQ] @{username} → ⚠ qoşulma xətası: {type(exc).__name__}: {exc}")
    finally:
        await _safe_close(client)
        if count:
            stats.chest_active -= 1
            stats.listening_now.discard(username)
            stats.listened += 1
        try:  # diaqnostika: hansı hadisə növləri gəldi
            top = sorted(seen_types.items(), key=lambda kv: -kv[1])
            log(f"[SANDIQ] @{username} → gələn hadisələr: " + (", ".join(f"{k} {v}" for k, v in top[:10]) or "heç nə"))
            with open(config.RESULTS_DIR / "events_seen.txt", "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {username} " + json.dumps(seen_types, ensure_ascii=False) + "\n")
        except Exception:
            pass
        if found["n"]:
            log(f"[SANDIQ] @{username} → bitdi: 📦 {found['n']} sandıq hadisəsi görüldü")
        else:
            log(f"[SANDIQ] @{username} → bitdi: sandıq görünmədi")
    return found["n"]


# ---- "isti yayımlar": sandıq çıxan yayıma davamlı qoşulu qal ----
HOT_MAX = config.HOT_MAX          # eyni anda maksimum davamlı izlənən yayım
HOT_HOURS = config.HOT_HOURS      # bir yayım ən çox neçə saat izlənsin
_HOT: dict = {}                   # username -> asyncio.Task


async def _hot_watch(username: str, room_id: str, stats, force: bool = False, hours: float | None = None) -> None:
    """Yayımda qalır: yeni sandıq qoyulan an tutulur (sandıq ~5 dəq yaşayır, qısa dinləmə onu qaçırırdı)."""
    end = time.monotonic() + (hours if hours is not None else HOT_HOURS) * 3600
    log(f"[SANDIQ] @{username} → 🔥 {'əl ilə' if force else 'isti'} yayım: davamlı izlənir ({len(_HOT)}/{HOT_MAX})")
    try:
        while time.monotonic() < end:
            t = time.monotonic()
            left = max(int(end - t), 30)
            await _listen(username, room_id, stats, force=force, listen_seconds=min(600, left))
            if time.monotonic() - t < 60:  # tez bitdi: yayım bağlanıb və ya qoşula bilmir
                break
    except Exception as exc:
        log(f"[SANDIQ] @{username} → isti izləmə xətası: {type(exc).__name__}: {exc}")
    finally:
        _HOT.pop(username, None)
        log(f"[SANDIQ] @{username} → isti izləmə bitdi")


def _hot_add(username: str, room_id: str, stats) -> None:
    if HOT_MAX <= 0 or username in _HOT or len(_HOT) >= HOT_MAX:
        return
    _HOT[username] = asyncio.create_task(_hot_watch(username, room_id, stats))


def watch_manual(username: str, room_id: str, stats, minutes: int | None = None) -> bool:
    """/yoxla ad: canlı yayımda bir müddət qal ki, sonradan (məs. qoşulandan 1 dəq sonra) qoyulan sandıq da tutulsun.
    HOT_MAX limitinə düşmür; bildirişlər filtrsiz (force) gedir. Artıq izlənirsə False."""
    minutes = config.MANUAL_WATCH_MIN if minutes is None else minutes
    if minutes <= 0 or username in _HOT:
        return False
    _HOT[username] = asyncio.create_task(_hot_watch(username, room_id, stats, force=True, hours=minutes / 60))
    return True


async def listen_once(username: str, room_id: str, stats, listen_seconds: int | None = None, out: dict | None = None, quick: bool = False) -> int:
    """Telegram /yoxla üçün: növbəsiz, tək dinləmə."""
    try:
        return await _listen(username, room_id, stats, force=True, listen_seconds=listen_seconds, out=out, quick=quick)
    except Exception as exc:
        log(f"[SANDIQ] @{username} → ⚠ gözlənilməz xəta: {type(exc).__name__}: {exc}")
        return 0


async def run(queue: asyncio.Queue, stats, ctl) -> None:
    while True:
        await ctl.wait_if_paused()
        item = await queue.get()
        if item is None:
            return
        if ctl.stop_requested:
            continue  # dayandırılıb: qalan növbə atılır
        username, room_id = item
        try:
            n = await _listen(username, room_id, stats)
            if n:
                _hot_add(username, room_id, stats)
        except Exception as exc:  # son müdafiə: bir yayım digərlərini dayandırmasın
            log(f"[SANDIQ] @{username} → ⚠ gözlənilməz xəta: {type(exc).__name__}: {exc}")
