"""Lucky Bag (hədiyyə çantası) hadisələri.

shortTouch        — Lucky Bag interfeys hadisəsi; refId çantanın identifikatorunu daşıya bilər.
ecShortItemRefresh — Lucky Bag yenilənməsi hadisəsi.
WebcastGoodyBagMessage — TikTok serverindən gələn Hədiyyə Çantası (GoodyBag) hadisəsi.

Bu mesajların .proto sxemi kitabxanada yoxdur, ona görə payload ümumi protobuf (wire format) oxuyucusu ilə açılır:
bütün rəqəm/mətn sahələri çıxarılır, refId namizədləri (uzun ID-lər) ayrıca göstərilir, xam payload
results/luckybag_raw.txt faylına yazılır (sxemi dəqiqləşdirmək üçün).
"""
import asyncio
import hashlib
import html
import json
import re
import time
from pathlib import Path

KEYS = ("shorttouch", "ecshortitem", "luckybag", "lucky", "giftbag", "fortunebag", "blindbox", "welfarebag", "ecbag", "goodybag", "goodiebag", "goody")      # metod adında axtarılan hissələr (kiçik hərf, '_' atılır)


# Lucky Bag OLMAYAN, amma adında/məzmununda təsadüfən oxşar söz olan mesajlar (məs. WebcastLinkMessage = yayımçıların bağlantısı/co-host)
BLOCK = ("linkmessage", "linkmic", "linklayer", "battle", "multiguest", "guestmessage", "roomuser", "memberm", "chatmessage", "likemessage", "socialmessage")


def _blocked(method: str) -> bool:
    m = (method or "").lower().replace("_", "")
    return any(b in m for b in BLOCK)


def _norm(m: str) -> str:
    return (m or "").lower().replace("_", "")


LEARNED: dict = {}                 # normallaşmış ad -> orijinal ad (/canta ilə öyrədilmiş və ya /canta_elave ilə əlavə olunmuş)
_FILE: Path | None = None


def _sync_wss() -> None:
    try:
        from piratetok_live.connection import wss
        wss.LEARNED.clear()
        wss.LEARNED.update(LEARNED.keys())
        wss.METHOD_HOOK = _hook_method if _RE_METHOD is not None else None
        wss.PAYLOAD_HOOK = _hook_payload if (_RE_STRONG is not None or _RE_WEAK is not None) else None
        wss.STRONG_HOOK = _hook_strong if _RE_STRONG is not None else None
    except Exception:
        pass


def _save() -> None:
    if _FILE is not None:
        try:
            _FILE.write_text(json.dumps(sorted(LEARNED.values()), ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


def load_learned(path) -> None:
    global _FILE
    _FILE = Path(path)
    try:
        for name in json.loads(_FILE.read_text(encoding="utf-8")):
            LEARNED[_norm(name)] = name
    except (OSError, ValueError):
        pass
    _sync_wss()


def learn(method: str) -> bool:
    k = _norm(method)
    if not k or k in LEARNED:
        return False
    LEARNED[k] = method
    _save()
    _sync_wss()
    return True


def forget(method: str) -> bool:
    k = _norm(method)
    if LEARNED.pop(k, None) is None:
        return False
    _save()
    _sync_wss()
    return True


def is_lucky(method: str) -> bool:
    m = _norm(method)
    if m in LEARNED:
        return True                                   # istifadəçinin öyrətdiyi ad həmişə əsasdır
    if _RE_METHOD is not None:
        return not _blocked(method) and bool(_RE_METHOD.search(m))
    return not _blocked(method) and any(k in m for k in KEYS)


# məzmunda yalnız UZUN, birmənalı sözlər axtarılır ("lucky"/"goody" tək halda ləqəblərdə də çıxır → yalan siqnal)
_PAY = (b"luckybag", b"lucky_bag", b"goodybag", b"goody_bag", b"goodiebag", b"giftbag", b"gift_bag", b"blindbox", b"blind_box",
        b"welfarebag", b"fortunebag", b"shorttouch", b"short_touch", b"ecshortitem")


def payload_lucky(payload, method: str = "") -> bool:
    """Məzmunda DƏQİQ (strong) Lucky Bag ifadəsi var?"""
    if _blocked(method):
        return False
    if _RE_STRONG is not None:
        t = _text(payload)
        return bool(t) and bool(_RE_STRONG.search(t))
    try:
        low = bytes(payload or b"").lower()
    except Exception:
        return False
    return bool(low) and any(k in low for k in _PAY)


_TEXTY = frozenset({"webcastroommessage"})
_WEAK = (b"lucky", b"goody", b"ecbag", b"giftbag", b"bag_")


def classify(method: str, payload) -> str | None:
    """'sure' = dəqiq (öyrənilmiş ad / ad açar sözü / məzmunda birmənalı söz); 'maybe' = ehtimal (məzmunda zəif iz, atılmır, amma az-az göndərilir)."""
    if is_lucky(method):
        return "sure"
    if _blocked(method):
        return None
    if payload_lucky(payload, method):
        return "maybe" if _norm(method) in _TEXTY else "sure"       # RoomMessage (adi elan mətni) → təsdiqsiz
    if _RE_WEAK is not None:
        t = _text(payload)
        return "maybe" if t and _RE_WEAK.search(t) else None
    try:
        low = bytes(payload or b"").lower()
    except Exception:
        return None
    return "maybe" if low and any(k in low for k in _WEAK) else None


def _varint(b: bytes, i: int):
    r = s = 0
    while i < len(b):
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if not c & 0x80:
            return r, i
        s += 7
        if s > 63:
            break
    raise ValueError("varint")


def _walk(b: bytes, out: list, depth: int = 0) -> None:
    """Bütün (sahə_nömrəsi, dəyər) cütlərini out-a yığır; iç-içə mesajlara girir."""
    i = 0
    while i < len(b):
        tag, i = _varint(b, i)
        f, wt = tag >> 3, tag & 7
        if f == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, i = _varint(b, i)
            out.append((f, v))
        elif wt == 1:
            out.append((f, int.from_bytes(b[i:i + 8], "little")))
            i += 8
        elif wt == 5:
            out.append((f, int.from_bytes(b[i:i + 4], "little")))
            i += 4
        elif wt == 2:
            n, i = _varint(b, i)
            chunk = b[i:i + n]
            if len(chunk) != n:
                raise ValueError("len")
            i += n
            sub: list = []
            if depth < 4 and chunk:
                try:
                    _walk(chunk, sub, depth + 1)
                except Exception:
                    sub = []
            if sub:
                out.extend(sub)
            else:
                try:
                    s = chunk.decode("utf-8")
                    if s and all(ch.isprintable() for ch in s):
                        out.append((f, s))
                except UnicodeDecodeError:
                    pass
        else:
            raise ValueError("wiretype")


def extract(method: str, payload) -> dict:
    payload = bytes(payload or b"")
    fields: list = []
    try:
        _walk(payload, fields)
    except Exception:
        pass
    refs: list = []
    strs: list = []
    for f, v in fields:
        if isinstance(v, int) and v >= 10 ** 8:
            refs.append(str(v))
        elif isinstance(v, str):
            if v.isdigit() and len(v) >= 8:
                refs.append(v)
            elif len(v) >= 3 and v not in strs:
                strs.append(v)
    refs = list(dict.fromkeys(refs))
    nums = [v for _, v in fields if isinstance(v, int) and 1 <= v < 10 ** 7][:12]
    return {
        "method": method, "refs": refs[:6], "strings": strs[:6], "nfields": len(fields), "nums": nums,
        "hex": payload.hex(), "sig": hashlib.md5(payload).hexdigest()[:10],
        "kind": "touch" if "touch" in method.lower() else "refresh" if "refresh" in method.lower() else "other",
    }


def telegram_text(username: str, info: dict, n: int = 1) -> str:
    u = html.escape(username)
    bar = "━━━━━━━━━━━━━━━━"
    maybe = info.get("tier") == "maybe"
    title = ("🟡 <b>EHTİMAL: Lucky Bag?</b> (təsdiqsiz)" if maybe else
             "🛍️✨ <b>LUCKY BAG (HƏDİYYƏ ÇANTASI)!</b> ✨🛍️" if info["kind"] != "refresh" else "🔄 <b>Lucky Bag yeniləndi</b>")
    lines = [title, bar, f"👤 <b>@{u}</b>", f"📡 Hadisə: <code>{html.escape(info['method'])}</code>"]
    if info["refs"]:
        lines.append("🆔 refId: " + ", ".join(f"<code>{html.escape(r)}</code>" for r in info["refs"][:3]))
    if info["strings"]:
        lines.append("🏷️ Mətn: " + html.escape(" | ".join(info["strings"][:3]))[:200])
    if info.get("viewers"):
        lines.append(f"👁 Canlıda indi: <b>{info['viewers']:,}</b> nəfər".replace(",", " "))
    if info.get("nums"):
        lines.append("🔢 Ədədlər: <code>" + html.escape(", ".join(str(x) for x in info["nums"][:10])) + "</code>")
    if maybe:
        lines += [bar, f"✅ Düzdürsə təsdiqlə: <code>/canta_elave {html.escape(info['method'])}</code>", "❌ Səhvdirsə heç nə etmə (bu növ 30 dəq təkrar gəlməz)"]
    else:
        lines += [f"🔢 Bu yayımda: <b>{n}</b>-ci Lucky Bag hadisəsi", bar, "⚡ Tez ol, yayıma keç!"]
    return "\n".join(lines)


class Dedupe:
    """Eyni çanta üçün təkrar xəbər getməsin (cooldown saniyə)."""

    def __init__(self, cooldown: float = 600.0) -> None:
        self.cd, self.seen = cooldown, {}

    def fresh(self, key) -> bool:
        now = time.monotonic()
        if len(self.seen) > 20000:
            self.seen = {k: t for k, t in self.seen.items() if now - t < self.cd}
        t = self.seen.get(key)
        if t is not None and now - t < self.cd:
            return False
        self.seen[key] = now
        return True


# ----------------------------------------------------------------------------- 1000+ açar söz (luckybag_keywords.txt)
def _trie_regex(words):
    """Yüzlərlə sözü TEK sürətli regex-ə yığır (trie). Yalnız 'var/yox' axtarışı üçün."""
    trie: dict = {}
    for w in words:
        d = trie
        for ch in w:
            d = d.setdefault(ch, {})
        d[""] = 1

    def rx(d: dict) -> str:
        if "" in d:
            return ""
        alts = [re.escape(k) + rx(v) for k, v in sorted(d.items())]
        return alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"

    return re.compile(rx(trie)) if words else None


_RE_STRONG = _RE_WEAK = _RE_METHOD = None
KW_COUNT = {"strong": 0, "weak": 0, "method": 0}


def load_keywords(path=None) -> None:
    global _RE_STRONG, _RE_WEAK, _RE_METHOD
    path = Path(path) if path else Path(__file__).with_name("luckybag_keywords.txt")
    sec, lists = None, {"strong": [], "weak": [], "method": []}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("## "):
                sec = line[3:].strip()
            elif line and not line.startswith("#") and sec in lists:
                lists[sec].append(line.lower())
    except OSError:
        pass
    lists["strong"] += [k.decode() for k in _PAY]                  # köhnə daxili siyahı həmişə əlavə olunur
    lists["weak"] += [k.decode() for k in _WEAK]
    lists["method"] += list(KEYS)
    for k in lists:
        lists[k] = sorted({_norm(w) if k == "method" else w for w in lists[k] if w})
        KW_COUNT[k] = len(lists[k])
    _RE_STRONG, _RE_WEAK, _RE_METHOD = (_trie_regex(lists["strong"]), _trie_regex(lists["weak"]), _trie_regex(lists["method"]))
    _sync_wss()


def _text(payload) -> str:
    try:
        return bytes(payload or b"")[:3000].decode("utf-8", "ignore").lower()
    except Exception:
        return ""


def _hook_method(m_norm: str) -> bool:
    return not _blocked(m_norm) and bool(_RE_METHOD and _RE_METHOD.search(m_norm))


def _hook_strong(payload) -> bool:
    t = _text(payload)
    return bool(t) and bool(_RE_STRONG and _RE_STRONG.search(t))


def _hook_payload(payload) -> bool:
    t = _text(payload)
    return bool(t) and bool((_RE_STRONG and _RE_STRONG.search(t)) or (_RE_WEAK and _RE_WEAK.search(t)))


# ----------------------------------------------------------------------------- öyrənmə rejimi
COMMON = frozenset(_norm(x) for x in (
    "WebcastChatMessage", "WebcastLikeMessage", "WebcastMemberMessage", "WebcastGiftMessage", "WebcastSocialMessage",
    "WebcastRoomUserSeqMessage", "WebcastEmoteChatMessage", "WebcastRoomMessage", "WebcastControlMessage", "WebcastEnvelopeMessage",
    "WebcastLinkMessage", "WebcastLinkMicBattle", "WebcastLinkMicArmies", "WebcastLinkLayerMessage", "WebcastRoomPinMessage",
    "WebcastLiveIntroMessage", "WebcastBarrageMessage", "WebcastInRoomBannerMessage", "WebcastSystemMessage", "WebcastRankUpdateMessage",
    "WebcastRoomVerifyMessage", "WebcastImDeleteMessage", "WebcastSubNotifyMessage", "WebcastQuestionNewMessage",
))
SESSIONS: dict = {}                # str(room_id) -> Learner


def _wss():
    from piratetok_live.connection import wss
    return wss


class Learner:
    """/canta @ad: 20 san baza (adi mesajlar) → istifadəçi çantanı qoyur → bazada OLMAYAN yeni mesaj növləri tapılır, ən az təkrarlananlar öyrənilir."""
    BASE = 20
    WINDOW = 120
    AUTO_MAX = 4
    AUTO_COUNT = 40

    def __init__(self, username: str, room_id: str, outdir, say) -> None:
        self.username, self.room_id, self.say = username, str(room_id), say
        self.out = Path(outdir) / f"canta_ogren_{username}.txt"
        self.t0 = time.monotonic()
        self.phase = "base"
        self.base: dict = {}
        self.new: dict = {}
        self.lines: list = []
        self.stop = asyncio.Event()
        self.total = 0

    def feed(self, method: str, payload: bytes) -> None:
        self.total += 1
        t = time.monotonic() - self.t0
        k = _norm(method)
        if self.phase == "base":
            self.base[k] = self.base.get(k, 0) + 1
            return
        if k in self.base:
            return
        e = self.new.get(k)
        if e is None:
            e = self.new[k] = {"method": method, "n": 0, "t": t - self.BASE, "info": extract(method, payload)}
        e["n"] += 1
        if len(self.lines) < 600:
            inf = extract(method, payload) if e["n"] > 1 else e["info"]
            self.lines.append(f"+{t - self.BASE:6.1f}s {method} size={len(payload)} nums={inf['nums']} str={inf['strings'][:4]} refs={inf['refs'][:3]} hex={payload[:60].hex()}")

    async def run(self) -> str:
        _wss().CAPTURE[self.room_id] = self.feed
        try:
            await self.say(f"🧪 <b>@{html.escape(self.username)}</b>: {self.BASE} saniyə baza yığıram (adi mesajlar). Hələ çantanı QOYMA…")
            await asyncio.sleep(self.BASE)
            self.phase = "live"
            await self.say(f"✅ Baza hazırdır ({len(self.base)} adi növ, {self.total} mesaj).\n🛍 <b>İNDİ hədiyyə çantasını qoy!</b> {self.WINDOW} saniyə dinləyirəm.\nTez bitirmək üçün: /canta_bit")
            try:
                await asyncio.wait_for(self.stop.wait(), self.WINDOW)
            except asyncio.TimeoutError:
                pass
        finally:
            _wss().CAPTURE.pop(self.room_id, None)
            SESSIONS.pop(self.room_id, None)
        return self.finish()

    def finish(self) -> str:
        cands = sorted(self.new.values(), key=lambda e: (e["n"], e["t"]))
        try:
            with open(self.out, "w", encoding="utf-8") as fh:
                fh.write(f"# @{self.username} room={self.room_id} baza={sorted(self.base)}\n")
                for e in cands:
                    fh.write(f"# YENİ {e['method']} x{e['n']} ilk=+{e['t']:.1f}s {e['info']['strings'][:4]} nums={e['info']['nums']}\n")
                fh.write("\n".join(self.lines) + "\n")
        except OSError:
            pass
        if not cands:
            return ("❌ Baza bitəndən sonra YENİ mesaj növü gəlmədi. Çanta bu yayımda başqa yolla göstərilir (məs. adi mesajın içində) "
                    "və ya hələ qoyulmadı. Fayl: results/" + self.out.name)
        added, rows = [], []
        for e in cands:
            auto = len(added) < self.AUTO_MAX and e["n"] <= self.AUTO_COUNT and _norm(e["method"]) not in COMMON and not _blocked(e["method"])
            if auto and learn(e["method"]):
                added.append(e["method"])
            inf = e["info"]
            rows.append(f"{'✅' if e['method'] in added else '▫️'} <code>{html.escape(e['method'])}</code> ×{e['n']} (+{e['t']:.0f}s)"
                        + (f"\n    🏷 {html.escape(' | '.join(inf['strings'][:3]))[:140]}" if inf["strings"] else "")
                        + (f"\n    🔢 {inf['nums'][:8]}" if inf["nums"] else ""))
        head = "🎓 <b>Öyrənmə bitdi</b>\n━━━━━━━━━━━━━━━━"
        tail = (f"\n━━━━━━━━━━━━━━━━\n✅ Öyrənildi (bundan sonra bütün yayımlarda Lucky Bag sayılacaq): <b>{len(added)}</b>\n"
                "Səhv olanı silmək: <code>/canta_sil ad</code> • siyahı: /cantalar")
        return head + "\n" + "\n".join(rows[:12]) + tail


def start_learn(username: str, room_id: str, outdir, say) -> "Learner | None":
    if str(room_id) in SESSIONS:
        return None
    ln = Learner(username, room_id, outdir, say)
    SESSIONS[str(room_id)] = ln
    return ln


def stop_learn() -> int:
    n = 0
    for ln in list(SESSIONS.values()):
        if ln.phase == "live":
            ln.stop.set()
            n += 1
    return n


_REC_ACTIVE: set = set()


def record_after(username: str, room_id: str, outdir, secs: int = 180) -> None:
    """Dəqiq Lucky Bag aşkarlandıqdan sonra bu yayımın bütün mesajlarını 3 dəq faylа yaz (jeton/nəfər sahələrini tapmaq üçün)."""
    rid = str(room_id)
    try:
        wss = _wss()
    except Exception:
        return
    if rid in wss.CAPTURE or len(_REC_ACTIVE) >= 5:
        return
    path = Path(outdir) / f"luckybag_after_{username}.txt"
    t0 = time.monotonic()
    n = {"v": 0}

    def cb(method: str, payload: bytes) -> None:
        if n["v"] >= 400:
            return
        n["v"] += 1
        inf = extract(method, payload)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"+{time.monotonic() - t0:6.1f}s {method} size={len(payload)} nums={inf['nums']} str={inf['strings'][:4]} refs={inf['refs'][:3]} hex={payload[:80].hex()}\n")

    def done() -> None:
        wss.CAPTURE.pop(rid, None)
        _REC_ACTIVE.discard(rid)

    _REC_ACTIVE.add(rid)
    wss.CAPTURE[rid] = cb
    asyncio.get_running_loop().call_later(secs, done)


def seen_methods(top: int = 25) -> list:
    """Bütün yayımlardan gələn mesaj növləri (adi olmayanlar): Lucky Bag adını kəşf etmək üçün."""
    try:
        seen = dict(_wss().METHOD_SEEN)
    except Exception:
        return []
    rows = [(m, c) for m, c in seen.items() if _norm(m) not in COMMON]
    rows.sort(key=lambda r: r[1])
    return rows[:top]


# ----------------------------------------------------------------------------- avtomatik kəşf (özün çanta qoymadan)
NOTIFIED: set = set()


def suspects(open_rooms: int, hint_only: bool = False) -> list:
    """Nadir mesaj növləri: az sayda yayımda, amma həmin yayımda təkrar-təkrar gələnlər (çanta hostları belə mesaj göndərir).
    Qaytarır: [(method, rooms:dict, total, hinted)] — ən şübhəli əvvəl."""
    try:
        wss = _wss()
    except Exception:
        return []
    lim = max(3, int(0.05 * max(open_rooms, 1)))
    out = []
    for m, rooms in list(wss.METHOD_ROOMS.items()):
        k = _norm(m)
        if k in COMMON or k in LEARNED or _blocked(m):
            continue
        n_rooms, total, top = len(rooms), sum(rooms.values()), max(rooms.values())
        if n_rooms > lim or top < 2:
            continue
        hinted = any(h in k for h in ("bag", "lucky", "goody", "touch", "shop", "ecom", "item", "welfare", "blind", "fortune", "box", "drop", "capsule", "coupon", "toast", "notice"))
        if hint_only and not hinted:
            continue
        out.append((m, dict(rooms), total, hinted))
    out.sort(key=lambda r: (not r[3], len(r[1]), -r[2]))
    return out


def sample_of(method: str):
    try:
        sm = _wss().METHOD_SAMPLES.get(method) or []
    except Exception:
        return None
    return extract(method, sm[-1][1]) if sm else None


def rooms_of(method: str) -> dict:
    try:
        return dict(_wss().METHOD_ROOMS.get(method) or {})
    except Exception:
        return {}


load_keywords()


# ----------------------------------------------------------------------------- yaddaş və diaqnostika
_NOTIF_FILE: Path | None = None


def load_notified(path) -> None:
    """Artıq adminə bildirilmiş şübhəli növlər restartdan sonra təkrar göndərilməsin."""
    global _NOTIF_FILE
    _NOTIF_FILE = Path(path)
    try:
        NOTIFIED.update(json.loads(_NOTIF_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def mark_notified(method: str) -> None:
    NOTIFIED.add(method)
    if _NOTIF_FILE is not None:
        try:
            _NOTIF_FILE.write_text(json.dumps(sorted(NOTIFIED), ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


def dump_methods(path) -> int:
    """Bütün gələn mesaj növləri (nadirlər əvvəl): otaq sayı, cəmi, nümunə sətirləri. /metodlar bu faylı göndərir."""
    try:
        wss = _wss()
        rooms_all = dict(wss.METHOD_ROOMS)
        samples = dict(wss.METHOD_SAMPLES)
    except Exception:
        return 0
    rows = []
    for m, rooms in rooms_all.items():
        rows.append((len(rooms), -sum(rooms.values()), m, rooms))
    rows.sort()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"# {time.strftime('%F %T')} — mesaj növü | yayım sayı | cəmi | ən çox bir yayımda | nümunə\n")
        for n, neg, m, rooms in rows:
            sm = samples.get(m) or []
            inf = extract(m, sm[-1][1]) if sm else {"strings": [], "nums": []}
            tag = "LEARNED" if _norm(m) in LEARNED else ("COMMON" if _norm(m) in COMMON else ("KEY" if is_lucky(m) else ""))
            fh.write(f"{m} | rooms={n} | total={-neg} | top={max(rooms.values())} | {tag} | str={inf['strings'][:3]} nums={inf['nums'][:6]}"
                     + (f" | hex={sm[-1][1][:48].hex()}" if sm else "") + "\n")
    return len(rows)
