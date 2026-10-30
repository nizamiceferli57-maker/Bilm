"""Diaqnostika: əvvəldən qoyulmuş sandığın TikTok-dan HANSI yolla gəldiyini tapmaq üçün.

İstifadə (sandığı artıq olan canlı yayımda):
    python probe_chest.py istifadeci_adi [saniye]

Nə edir: yayıma qoşulur, ilkin (qoşulma anındakı) mesajların HAMISINI və otaq məlumatını (room/info)
results/probe_<ad>.json faylına tam yazır. Heç nə açmır/göndərmir. Faylı mənə göndərin —
sandığın hansı mesajda/açarda olduğunu görüb botu dəqiq ona görə düzəldəcəm.
"""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor" / "TikTokLive"))

KEYS = ("envelope", "treasure", "chest", "capsule", "box", "redpacket", "red_packet", "lucky", "welfare", "reward",
        "diamond", "coin", "people", "unpack", "countdown", "pin", "banner")


def _varint(b: bytes, i: int):
    r = s = 0
    while i < len(b):
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if not c & 0x80:
            return r, i
        s += 7
        if s > 70:
            break
    raise ValueError("varint")


def wire(b: bytes, depth: int = 0):
    """Protobuf baytlarını sxemsiz açır: {sahə_nömrəsi: [qiymətlər]}. Alınmazsa None."""
    out, i = {}, 0
    try:
        while i < len(b):
            tag, i = _varint(b, i)
            f, wt = tag >> 3, tag & 7
            if f == 0:
                return None
            if wt == 0:
                v, i = _varint(b, i)
            elif wt == 1:
                v, i = int.from_bytes(b[i:i + 8], "little"), i + 8
            elif wt == 5:
                v, i = int.from_bytes(b[i:i + 4], "little"), i + 4
            elif wt == 2:
                n, i = _varint(b, i)
                chunk = b[i:i + n]
                if len(chunk) != n:
                    return None
                i += n
                v = _leaf(chunk, depth)
            else:
                return None
            out.setdefault(str(f), []).append(v)
    except ValueError:
        return None
    return out or None


def _leaf(chunk: bytes, depth: int):
    try:
        s = chunk.decode("utf-8")
        if s and all(c.isprintable() or c in "\r\n\t" for c in s):
            if s.lstrip().startswith(("{", "[")):
                try:
                    return {"json": json.loads(s)}
                except ValueError:
                    pass
            return s
    except UnicodeDecodeError:
        pass
    if depth < 6:
        sub = wire(chunk, depth + 1)
        if sub:
            return sub
    return {"bytes": chunk[:48].hex(), "len": len(chunk)}


def hits(obj, path=""):
    """Sandıqa bənzər açar/mətnləri olan yolları qaytarır."""
    res = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if any(w in str(k).lower() for w in KEYS):
                res.append(p)
            res += hits(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:40]):
            res += hits(v, f"{path}[{i}]")
    elif isinstance(obj, str) and any(w in obj.lower() for w in KEYS[:9]):
        res.append(f"{path} = {obj[:80]!r}")
    return res


CANDIDATES = [  # TikTok webcast daxili sorğuları üçün TƏXMİNİ yollar: hansının sandıq qaytardığını tapmaq üçün (çoxu 404 verəcək)
    "room/enter/", "room/enter_info/", "room/info/", "room/info_by_user/", "room/get_latest_room/", "room/continue/",
    "room/treasure_box/", "room/treasure_box/info/", "room/envelope/", "room/envelope/list/", "room/get_envelope/",
    "room/luckymoney/", "room/redpacket/", "room/pin/", "room/pin/list/",
    "treasure_box/info/", "treasure_box/list/", "treasure_box/get/", "treasure_box/detail/", "treasure_box/room_info/",
    "envelope/list/", "envelope/info/", "envelope/get/", "envelope/detail/", "envelope/room/", "envelope/get_list/",
    "luckymoney/list/", "luckymoney/info/", "redpacket/list/", "redpacket/info/",
    "gift/treasure_box/", "interaction/treasure_box/", "ranklist/treasure_box/",
]
CHEST_RE = ("envelope", "treasure", "unpack", "people_count", "diamond_count", "luckymoney", "redpacket")


async def probe_api(client, rec: dict, envelope_id: str = "") -> None:
    """Əvvəldən qoyulmuş sandığı qaytara biləcək HTTP sorğularını yoxlayır. Nəticə rec['api_probe']-a yazılır."""
    from TikTokLive.client.web.web_settings import WebDefaults
    base = WebDefaults.tiktok_webcast_url.rstrip("/") + "/"
    room_id = str(client._web.params.get("room_id") or "")
    out = rec.setdefault("api_probe", [])
    for path in CANDIDATES:
        for method, sign in (("GET", False), ("POST", False), ("GET", True)):
            extra = {"room_id": room_id, "anchor_id": str(rec.get("owner_id", ""))}
            if envelope_id:
                extra["envelope_id"] = envelope_id
            entry = {"path": path, "method": method, "signed": sign}
            try:
                fn = client._web.get if method == "GET" else client._web.post
                r = await asyncio.wait_for(fn(url=base + path, extra_params=extra, sign_url=sign), timeout=12)
                body = r.text or ""
                low = body.lower()
                hit = [w for w in CHEST_RE if w in low]
                entry.update(status=r.status_code, bytes=len(body), hit=hit, head=body[:(3000 if hit else 300)])
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            out.append(entry)
            await asyncio.sleep(0.3)
    print("api yoxlaması bitdi. sandıq izi olanlar:", [f"{e['method']} {e['path']}" for e in out if e.get("hit")] or "yoxdur")


async def main(username: str, seconds: int, mobile: bool = False, api: bool = False) -> None:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import EnvelopeEvent

    username = username.lstrip("@")
    kw = {}
    if mobile:  # mobil platforma: qoşulanda əvvəlki sandıq göndərilə bilər (sessiya tələb edir)
        from TikTokLive.client.web.routes.fetch_signed_websocket import WebcastPlatform
        kw["platform"] = WebcastPlatform.MOBILE
    client = TikTokLiveClient(unique_id=f"@{username}", **kw)
    try:  # bot ilə eyni sessiya (18+ yayımlar üçün)
        sys.path.insert(0, str(ROOT))
        import config
        if config.SESSION_OK:
            client._web.set_session(config.SESSION_ID, config.TT_TARGET_IDC)
        elif config.SESSION_PROBLEM:
            print("⚠", config.SESSION_PROBLEM)
    except Exception as exc:
        print("sessiya təyin olunmadı:", exc)
    rec = {"platform": "mobile" if mobile else "web", "user": username, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "messages": [], "envelope_events": [],
           "room_info": None, "room_info_hits": [], "method_counts": {}}
    seen = set()
    t0 = time.monotonic()
    orig = client.emit

    def emit(name, *a, **k):
        try:
            if str(name) == "WebsocketResponseEvent" and a:
                raw = getattr(a[0], "raw", None)
                if raw is not None and id(raw) not in seen:
                    seen.add(id(raw))
                    for m in getattr(raw, "messages", None) or []:
                        meth = str(getattr(m, "method", "?"))
                        rec["method_counts"][meth] = rec["method_counts"].get(meth, 0) + 1
                        pl = bytes(getattr(m, "payload", b"") or b"")
                        tree = wire(pl)
                        # ilk bağlantı mesajları + sandıq izi olanlar tam yazılır, qalanlar qısa
                        h = hits(tree) if tree else []
                        full = (time.monotonic() - t0 < 6) or bool(h) or any(w in meth.lower() for w in KEYS)
                        rec["messages"].append({
                            "t": round(time.monotonic() - t0, 1), "is_first": bool(getattr(raw, "is_first", False)),
                            "method": meth, "bytes": len(pl), "hits": h[:15],
                            **({"tree": tree, "b64": base64.b64encode(pl[:4000]).decode()} if full else {}),
                        })
            elif str(name) == "EnvelopeEvent" and a:
                try:
                    rec["envelope_events"].append({"t": round(time.monotonic() - t0, 1), "raw": a[0].to_dict()})
                except Exception as exc:
                    rec["envelope_events"].append({"error": repr(exc)})
        except Exception as exc:
            rec.setdefault("errors", []).append(repr(exc))
        return orig(name, *a, **k)

    client.emit = emit
    print(f"@{username} yayımına qoşulur...")
    try:
        task = await client.start()
        try:
            ri = await asyncio.wait_for(client._web.fetch_room_info(), timeout=15)
            rec["room_info"] = ri
            rec["room_info_hits"] = hits(ri)[:60]
            print(f"room/info alındı, sandıq izləri: {len(rec['room_info_hits'])}")
        except Exception as exc:
            rec["room_info_error"] = f"{type(exc).__name__}: {exc}"
            print("room/info alınmadı:", rec["room_info_error"])
        if api:
            try:
                rec["owner_id"] = (rec.get("room_info") or {}).get("owner_user_id_str", "")
                await probe_api(client, rec)
            except Exception as exc:
                rec["api_probe_error"] = f"{type(exc).__name__}: {exc}"
        await asyncio.wait({task}, timeout=seconds)
    except Exception as exc:
        rec["connect_error"] = f"{type(exc).__name__}: {exc}"
        print("Qoşulma xətası:", rec["connect_error"])
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
            await client._web.close()
        except Exception:
            pass
    out = ROOT / "results" / f"probe_{username}{'_mobile' if mobile else ''}{'_api' if api else ''}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    trace = sorted({h for m in rec["messages"] for h in m["hits"]})[:15]
    print(f"Hazırdır: {out}")
    print(f"mesajlar: {len(rec['messages'])}, EnvelopeEvent: {len(rec['envelope_events'])}")
    print("mesaj növləri:", ", ".join(f"{k} {v}" for k, v in sorted(rec['method_counts'].items(), key=lambda x: -x[1])[:12]))
    if trace:
        print("sandıq izi olan yerlər:\n  " + "\n  ".join(trace))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 40, "mobile" in sys.argv[2:], "api" in sys.argv[2:]))
