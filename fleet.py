"""ORDU (fleet): canlıya qoşulan çoxlu işçi. API YOXDUR (PirateTok: imza serveri / açar tələb etmir).

  Böyük işçi (worker_scanner)  ->  queue (ad, room_id)  ->  Dispatcher  ->  IP işçiləri
  Hər IP işçisi = 1 proxy (ayrı IP). Bir IP en çox FLEET_PER_IP canlı saxlayır.
  Hər canlı üçün ayrı tapşırıq: yayım BİTƏNƏ qədər qoşulu qalır (qopsa yenidən qoşulur, proxy ölsə başqasına keçir).
  Sandıq (envelope) hadisəsi gələn kimi Telegram-a göndərilir (alerts.chest_alert).
"""
import asyncio
import collections
import html
import json
import random
import subprocess
import sys
import time
from pathlib import Path

from piratetok_live.auth.ttwid import fetch_ttwid
from piratetok_live.errors import TtwidMissingError
from piratetok_live import (
    EventType,
    HostNotOnlineError,
    TikTokLiveClient,
    UserNotFoundError,
    check_online,
)
from piratetok_live import fetch_room_info
from piratetok_live.http.ua import random_ua

import alerts
import telegram_notify
import chest_format
import luckybag
import worker_chest
import config
from settings import settings
from log import err_kind, log, short_err
from rate_limit import PROXY_DOWN, proxy_down


class TtwidPool:
    """Bir proxy/IP üçün ttwid-i bütün qoşulmalar arasında bölüşdürür (1000 canlı = ~20 sorğu).
    - Lock: ttwid yoxdursa/bitibsə yalnız BİR sorğu gedir, qalanları nəticəni gözləyir (əvvəl hamısı eyni anda sorğu atırdı).
    - Uğursuzluqda soyuma: TikTok ttwid vermirsə, IP-ni təkrar sorğularla yormur (20s, 40s, 80s ... max 300s).
    - Soyuma/uğursuzluq zamanı köhnə (DEVICE_BLOCKED olmayan) ttwid işlənməyə davam edir.
    DEVICE_BLOCKED verən ttwid atılır."""

    def __init__(self, per: int | None = None) -> None:
        self.per = per or config.TTWID_REUSE
        self.cur, self.uses, self.bad = None, 0, set()
        self._lock: asyncio.Lock | None = None
        self._fail_until, self._fail_n = 0.0, 0

    def _usable(self) -> bool:
        return self.cur is not None and self.cur[0] not in self.bad

    async def get(self, proxy: str, timeout: float):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not self._usable() or self.uses >= self.per:
                if time.monotonic() < self._fail_until:
                    if not self._usable():
                        raise TtwidMissingError(config.TTWID_ATTEMPTS)   # soyuma: sorğu atmadan bildir
                else:
                    ua = random_ua()
                    try:
                        ttwid = await asyncio.to_thread(
                            fetch_ttwid, timeout, proxy=proxy, user_agent=ua,
                            attempts=config.TTWID_ATTEMPTS, retry_delay=config.TTWID_RETRY_DELAY)
                    except TtwidMissingError:
                        self._fail_n += 1
                        self._fail_until = time.monotonic() + min(config.TTWID_FAIL_COOLDOWN * 2 ** (self._fail_n - 1), 300)
                        if not self._usable():
                            raise
                    else:
                        self.cur, self.uses, self._fail_n = (ttwid, ua), 0, 0
            self.uses += 1
            return self.cur

    def mark_bad(self, ttwid: str) -> None:
        self.bad.add(ttwid)
        if len(self.bad) > 200:
            self.bad.clear()


# yalnız bu mesajlar decode olunur: sandıq + yayım bitməsi (chat/like/gift və s. CPU-nu yeyir)
WANTED_METHODS = {"WebcastEnvelopeMessage", "WebcastControlMessage"}


class IPWorker:
    """Bir proxy (bir IP) = bir işçi."""

    def __init__(self, idx: int, proxy: str) -> None:
        self.idx, self.proxy = idx, proxy
        self.lives: set[str] = set()
        self.fails = 0
        self.cool_until = 0.0
        self.pool = TtwidPool()
        self.opens = 0          # uğurlu WebSocket qoşulma sayı
        self.errs = 0           # qoşulma xətaları (heç vaxt açılmadan)
        self.premium = "@" in proxy                      # login:parollu proxy = ödənişli, daha çox canlı saxlayır
        self.only_one = True                             # Fleet qurulanda düzəlir (öz IP-nin payı üçün)
        self.retired = False                             # yeniləmədən sonra siyahıda olmayan və heç vaxt işləməyən proxy kənarlaşdırılır
        base = config.FLEET_START_DELAY if proxy else config.FLEET_DIRECT_START_DELAY
        self.g = {"base": base, "delay": base, "next": 0.0, "blk": 0.0}   # HƏR proxy-nin öz qoşulma qapısı (bir-birini gözləmir)
        self._sem: asyncio.Semaphore | None = None       # bu proxy üzərindən eyni anda gedən əl sıxmalar

    @property
    def cap(self) -> int:
        if not self.proxy:
            return config.FLEET_DIRECT_CAP if self.only_one else min(config.FLEET_DIRECT_CAP, config.FLEET_DIRECT_SHARE)
        return config.FLEET_PER_IP_PREMIUM if self.premium else config.FLEET_PER_IP

    @property
    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(config.FLEET_PER_PROXY_CONNECT if self.proxy else max(config.FLEET_PER_PROXY_CONNECT, config.FLEET_DIRECT_CONNECT))
        return self._sem

    @property
    def name(self) -> str:
        if not self.proxy:
            return "IP#direct"
        tail = self.proxy.split("@")[-1]
        return f"IP#{self.idx}[{tail[:28]}]"

    def free(self) -> int:
        if self.retired or time.monotonic() < self.cool_until or proxy_down(self.proxy):   # dincəlir və ya skanerdə ölü çıxıb
            return 0
        return self.cap - len(self.lives)

    @property
    def short(self) -> str:
        """Telegram üçün: parolsuz host:port."""
        return (self.proxy.split("@")[-1].split("://")[-1] or "birbaşa IP")

    def state(self) -> str:
        if self.retired or time.monotonic() < self.cool_until or proxy_down(self.proxy):
            return "down"
        return "up"

    def bad(self) -> None:
        self.fails += 1
        # Telefonun öz IP-si (proxy yoxdur): bütün canlılar buna bağlıdır, ona görə uzun dincəlmə YOX, qısa fasilə
        limit = config.FLEET_PROXY_MAX_FAILS if self.proxy else config.FLEET_DIRECT_MAX_FAILS
        cool = config.FLEET_PROXY_COOLDOWN if self.proxy else config.FLEET_DIRECT_COOLDOWN
        if self.fails >= limit:
            self.cool_until = time.monotonic() + cool
            self.fails = 0
            log(f"[ORDU] 🚫 {self.name} ardıcıl xəta verdi → {cool}s dincəlir")

    def good(self) -> None:
        self.fails = 0


class _Raw:
    def __init__(self, d: dict) -> None:
        self._d = d

    def to_dict(self) -> dict:
        return self._d


class Fleet:
    def __init__(self, stats, ctl) -> None:
        self.stats, self.ctl = stats, ctl
        urls = list(config.PROXIES) or [""]          # proxy yoxdursa: tək birbaşa IP
        if config.PROXY_FALLBACK or (config.PROXIES and config.INCLUDE_DIRECT):
            urls = [""] + urls                       # 0-cı işçi = telefonun öz IP-si (fallback: əsas; all: proxy-lərlə yanaşı)
        self.workers = [IPWorker(i, p) for i, p in enumerate(urls)]
        for w in self.workers:
            w.only_one = len(self.workers) == 1 or config.PROXY_FALLBACK
        self._csem_n = max(config.FLEET_CONNECT_PARALLEL, min(len(self.workers) * 3, 600))
        self.viewers: dict[str, int] = {}            # ad -> son bilinən izləyici sayı
        self.tasks: dict[str, asyncio.Task] = {}      # ad -> tapşırıq
        self.where: dict[str, IPWorker] = {}
        self.seen_env: set = set()
        self.backlog: asyncio.Queue = asyncio.Queue()
        # qoşulmalar arası fasilə AYRI-AYRI: öz IP yavaş (blok olmasın), proxy-lər sürətli. Blok olanda artır, uğurda azalır.
        self.gate = {"direct": {"base": config.FLEET_DIRECT_START_DELAY, "delay": config.FLEET_DIRECT_START_DELAY, "next": 0.0},
                     "proxy": {"base": config.FLEET_START_DELAY, "delay": config.FLEET_START_DELAY, "next": 0.0}}
        self.clients: dict = {}                       # ad -> aktiv TikTokLiveClient (öz IP-yə geri köçürmək üçün)
        self.migrating: set = set()                   # öz IP-yə qaytarılan canlılar (tez-qopma sayğacı sıfırlansın)
        self._lb = luckybag.Dedupe(config.LUCKYBAG_COOLDOWN)
        self.room_user: dict[str, str] = {}           # otaq_id -> istifadəçi adı (kəşf/izləmə üçün)
        self._disc_last = 0.0
        self._lbm = luckybag.Dedupe(1800.0)           # "ehtimal" səviyyəsi: eyni mesaj növü üçün 30 dəq-də bir xəbər
        luckybag.load_learned(config.RESULTS_DIR / "luckybag_learned.json")
        luckybag.load_notified(config.RESULTS_DIR / "luckybag_notified.json")
        self._storm_until = 0.0
        self._csem = asyncio.Semaphore(max(config.FLEET_CONNECT_PARALLEL, min(len(self.workers) * 3, 600)))   # eyni anda neçə qoşulma əl sıxması (telefon CPU-su boğulmasın)
        self.ctr = {"open": 0, "fail": 0}                                # son hesabat dövründə uğurlu/uğursuz qoşulmalar
        self.err_hist: dict = {}
        self.waiting_n = 0
        self.manual: dict[str, int] = {}              # Telegram /qosul ilə verilənlər: ad -> admin chat_id

    # ---------- köməkçi ----------
    def tracking(self, username: str) -> bool:
        return username.lower() in self.tasks

    def capacity(self) -> int:
        return sum(w.cap for w in self.workers)

    def pick(self, exclude=()) -> IPWorker | None:
        """Ən yaxşı proxy: əvvəl qoşulma uğuru olanlar, sonra sınanmamışlar, ən axırda yalnız xəta verənlər."""
        cands = [x for x in self.workers if x.free() > 0 and x not in exclude]
        if not cands:
            return None
        if (config.PROXY_FALLBACK or config.FLEET_DIRECT_FIRST) and not self.workers[0].proxy and self.workers[0] in cands:
            d = self.workers[0]
            if self._gate_wait(d) <= config.FLEET_DIRECT_QUEUE_MAX or len(cands) == 1:
                return d                             # öz IP işləyir, yer var, növbə qısadır: əvvəl o (əsas heyət)
            cands = [x for x in cands if x is not d]   # öz IP-nin növbəsi dolu: ehtiyat proxy-lər doldursun
        def score(x):
            if config.FLEET_BALANCE and len(self.workers) > 1:
                # bərabər bölgü: xəta verib heç açılmayanlar axırda, qalanlar arasında ən az canlısı olan
                return (1 if (x.opens == 0 and x.errs > 0) else 0, len(x.lives) / max(x.cap, 1), x.errs)
            tier = 0 if x.opens > 0 else (1 if x.errs == 0 else 2)
            return (tier, len(x.lives) if x.opens == 0 else -x.free(), x.errs)
        return min(cands, key=score)

    def _bind(self, username: str, w: IPWorker) -> None:
        old = self.where.get(username)
        if old:
            old.lives.discard(username)
        w.lives.add(username)
        self.where[username] = w

    def join(self, username: str, room_id: str, cid: int) -> str:
        """Telegram /qosul: canlını dərhal ordunun tapşırığına əlavə et. 'ok' | 'already'."""
        key = username.lower()
        self.manual[key] = cid
        if key in self.tasks:
            return "already"
        self.tasks[key] = asyncio.create_task(self.hold(username, room_id))
        return "ok"

    def _g(self, w: "IPWorker") -> dict:
        g = w.g
        # son blokdan 60s keçibsə fasilə normaya qayıdır (əks halda öz IP "yavaş" sayılıb bir daha seçilmirdi)
        if g["delay"] > g["base"] and time.monotonic() - g.get("blk", 0.0) > 60:
            g["delay"] = g["base"]
        return g

    async def _gate(self, w: "IPWorker") -> None:
        """Qoşulma cəhdləri bu qapıdan keçir. Öz IP və proxy-lər AYRI növbədədir (proxy 403-ləri öz IP-ni yavaşlatmasın)."""
        g = self._g(w)
        now = time.monotonic()
        slot = max(now, g["next"])
        # jitter (±35%): qoşulmalar bərabər "robot" ritmi ilə getməsin; blok fırtınasında bütün qapılar yavaşlayır
        g["next"] = slot + g["delay"] * random.uniform(0.65, 1.35) * (2.0 if now < self._storm_until else 1.0)
        if slot > now:
            await asyncio.sleep(slot - now)

    def _gate_wait(self, w: "IPWorker") -> float:
        return max(0.0, self._g(w)["next"] - time.monotonic())

    def _on_block(self, w: "IPWorker") -> None:
        now = time.monotonic()
        self._blk_times = [t for t in getattr(self, "_blk_times", []) if now - t < 60] + [now]
        if len(self._blk_times) >= 8 and now >= self._storm_until:      # 60 san-də 8+ blok = fırtına: 2 dəq hamı yavaşlayır
            self._storm_until = now + 120
            log(f"[ORDU] 🌩 blok fırtınası ({len(self._blk_times)}/60s): qoşulmalar 2 dəqiqəlik 2× yavaşladıldı")
        g = self._g(w)
        g["blk"] = time.monotonic()
        # öz IP üçün tavan 6s (< FLEET_DIRECT_QUEUE_MAX): yoxsa növbə həmişə "dolu" görünür və canlılar ona qayıtmır
        g["delay"] = min(g["delay"] * 1.5, 20.0 if w.proxy else 6.0)

    def _on_ok(self, w: "IPWorker") -> None:
        g = self._g(w)
        g["delay"] = max(g["base"], g["delay"] * 0.85)

    # ---------- /izle: izlənən hesablar ----------
    def watching(self, name: str) -> bool:
        return name.lower() in {x.lower() for x in settings["watch"]}

    async def _watch_check(self, name: str) -> None:
        tried: list = []
        for _ in range(3):
            ups = ([w for w in self.workers if w.state() == "up" and w not in tried]
                   or [w for w in self.workers if w not in tried] or self.workers)
            w = self.workers[0] if (config.PROXY_FALLBACK and self.workers[0] in ups) else random.choice(ups)
            try:
                res = await asyncio.to_thread(check_online, name, config.SCAN_TIMEOUT, w.proxy, random_ua())
            except (HostNotOnlineError, UserNotFoundError):
                w.good()
                return
            except Exception:
                tried.append(w)                # öz IP blokdadırsa: növbəti cəhd proxy ilə; öz IP-yə də xəta yazılır (dincəlsin)
                if not w.proxy:
                    w.bad()
                continue
            w.good()
            if self.tracking(name):
                return
            telegram_notify.notify_admins(
                f"🟢 <b>@{name}</b> yayıma başladı — qoşuluram…", html=True,
                button_url=f"https://www.tiktok.com/@{name}/live", button_text="🔴 Yayıma keç")
            self.join(name, res.room_id, config.ADMIN_IDS[0] if config.ADMIN_IDS else 0)
            return

    async def _watch_loop(self) -> None:
        await asyncio.sleep(10)
        sem = asyncio.Semaphore(5)

        async def one(n: str) -> None:
            async with sem:
                await self._watch_check(n)

        while True:
            names = [n for n in settings["watch"] if not self.tracking(n)]
            if names:
                await asyncio.gather(*(one(n) for n in names), return_exceptions=True)
            await asyncio.sleep(config.WATCH_EVERY)

    def cool_info(self) -> tuple[int, float]:
        """(dincəlməkdə olan proxy sayı, ən tez qayıdana qədər saniyə). Bloklanan proxy gözləyir, vaxtı çatanda özü işə düşür."""
        now = time.monotonic()
        left = []
        for w in self.workers:
            t = max(w.cool_until, PROXY_DOWN.get(w.proxy, 0.0) if w.proxy else 0.0) - now
            if t > 0:
                left.append(t)
        return len(left), (min(left) if left else 0.0)

    def snapshot(self) -> dict:
        """Telegram paneli üçün: hansı proxy neçə canlıya qoşulub."""
        per = []
        for w in self.workers:
            per.append({"name": w.short, "lives": len(w.lives), "state": w.state(),
                        "cap": w.cap, "premium": w.premium})
        per.sort(key=lambda d: (-d["lives"], d["name"]))
        used = [d for d in per if d["lives"] > 0]
        return {
            "workers": len(per),
            "up": sum(1 for d in per if d["state"] == "up"),
            "down": sum(1 for d in per if d["state"] == "down"),
            "used": len(used),
            "lives": sum(d["lives"] for d in per),
            "capacity": sum(d["cap"] for d in per if d["state"] == "up"),
            "tasks": len(self.tasks),
            "open": len(self.stats.fleet_open),
            "proven": sum(1 for w in self.workers if w.opens > 0),
            "waiting": self.waiting_n,
            "cooling": self.cool_info()[0],
            "back_in": self.cool_info()[1],
            "premium": sum(1 for w in self.workers if w.premium),
            "direct": any(not w.proxy for w in self.workers),
            "top": per[:6],
        }

    # ---------- bir canlıda qalıcı qoşulma ----------
    async def hold(self, username: str, room_id: str) -> None:
        key = username.lower()
        st = self.stats
        self.room_user[str(room_id)] = username
        st.chest_active += 1
        st.listening_now.add(username)
        found = {"n": 0}
        short_fails = 0
        fails_n = 0
        ended_votes = 0       # "yayım bitib" cavabı: bir yalan proxy cavabı canlını atmasın, FLEET_ENDED_VOTES dəfə təsdiq lazımdır
        giveup_logged = False
        tried: set = set()    # bu canlı üçün qoşulma sınanıb xəta verən proxy-lər
        t_start = time.monotonic()
        try:
            while not self.ctl.stop_requested:
                w = self.where.get(key)
                if w is None or w.state() == "down" or w in tried:   # yoxdur / dincəlir / xəta verib → yenisini seç
                    w = self.pick(exclude=tried)
                    if w is None:
                        if tried:
                            tried.clear()                # hamısı sınandı: yenidən başla
                        await asyncio.sleep(5)
                        continue
                self._bind(username, w)
                opened = {"v": False}
                conn_err = {"v": False}

                t0 = time.monotonic()
                if config.FLEET_ENGINE == "tiktoklive":
                    # Euler imzası + TikTokLive (v12 məntiqi). Yayım bitənə / 6 saata qədər dinləyir.
                    try:
                        n = await worker_chest._listen(username, room_id, self.stats, listen_seconds=6 * 3600,
                                                       proxy_url=w.proxy, count=False)
                        found["n"] += n or 0
                        opened["v"] = True
                        w.good()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        w.bad()
                        log(f"[ORDU] @{username} → ⚠ {err_kind(exc)}: {short_err(exc, 80)} ({w.name})")
                else:
                    await self._gate(w)
                    cur = {"t": ""}
                    sem = {"held": False, "pheld": False}

                    def _rel(_s=sem, _w=w) -> None:
                        if _s["held"]:
                            _s["held"] = False
                            self._csem.release()
                        if _s["pheld"]:
                            _s["pheld"] = False
                            _w.sem.release()

                    async def _src(_w=w, _c=cur):
                        t = await _w.pool.get(_w.proxy, config.FLEET_CONNECT_TIMEOUT)
                        _c["t"] = t[0]
                        return t

                    def on_err(evt, _w=w, _c=cur) -> None:
                        if type(evt.data).__name__ == "DeviceBlockedError":
                            _w.pool.mark_bad(_c["t"])

                    client = (TikTokLiveClient(username).max_retries(1).stale_timeout(75)
                              .timeout(config.FLEET_CONNECT_TIMEOUT)
                              .room_id_hint(room_id).ttwid_source(_src).only_methods(WANTED_METHODS))
                    client.on("error")(on_err)
                    self.clients[key] = client
                    if w.proxy:
                        client.proxy(w.proxy)
                    client.user_agent(random_ua())

                    def on_env(evt, _u=username, _found=found) -> None:
                        self._on_envelope(_u, room_id, evt.data, _found)

                    def on_end(evt, _c=client) -> None:
                        _c.disconnect()

                    def on_open(evt, _w=w, _u=username) -> None:
                        opened["v"] = True
                        _w.opens += 1
                        _w.errs = 0
                        _w.good()
                        self.ctr["open"] += 1
                        _rel()
                        self._on_ok(_w)
                        if key not in st.fleet_open:
                            st.fleet_open.add(key)
                            log(f"[ORDU] ✅ @{_u} qoşuldu, sandıq dinlənir ({_w.name}) | qoşulu: {len(st.fleet_open)}")
                            mcid = self.manual.pop(key, None)
                            if mcid:
                                telegram_notify.post(mcid, f"✅ <b>@{_u}</b> yayımına qoşuldum ({_w.short}).\nSandıq çıxsa bura gələcək.", html=True)

                    client.on("wss_open")(on_open)

                    client.on(EventType.envelope)(on_env)
                    if config.LUCKYBAG:
                        def on_unk(evt, _u=username, _found=found) -> None:
                            d = evt.data if isinstance(evt.data, dict) else {}
                            tier = luckybag.classify(d.get("method", ""), d.get("payload"))
                            if tier:
                                self._on_luckybag(_u, room_id, d, _found, tier)
                        client.on(EventType.unknown)(on_unk)
                    client.on(EventType.live_ended)(on_end)
                    try:
                        await w.sem.acquire()                 # bir proxy-yə eyni anda az əl sıxma (proxy boğulmasın)
                        sem["pheld"] = True
                        await self._csem.acquire()            # ümumi əl sıxma yükü məhduddur; açılan kimi (on_open) buraxılır
                        sem["held"] = True
                        asyncio.get_running_loop().call_later(config.FLEET_CONNECT_TIMEOUT * 2 + 15, _rel)
                        await client.connect()                # yayım bitənə / qopana qədər bloklayır
                    except (HostNotOnlineError, UserNotFoundError):
                        ended_votes += 1
                        if ended_votes >= config.FLEET_ENDED_VOTES:
                            log(f"[ORDU] @{username} → yayım bitdi ({w.name})")
                            return
                        # tək cavab kifayət deyil (proxy yalan deyə bilər): başqa IP ilə təkrar yoxla, canlını atma
                        tried.add(w)
                        self.where.pop(key, None)
                        w.lives.discard(username)
                        await asyncio.sleep(2)
                        continue
                    except asyncio.CancelledError:
                        client.disconnect()
                        raise
                    except Exception as exc:
                        conn_err["v"] = True
                        self.ctr["fail"] += 1
                        k = err_kind(exc)
                        self.err_hist[k] = self.err_hist.get(k, 0) + 1
                        if "Blocked" in type(exc).__name__ or "403" in str(exc):
                            self._on_block(w)
                        log(f"[ORDU] @{username} → ⚠ {err_kind(exc)}: {short_err(exc, 80)} ({w.name})")
                    finally:
                        _rel()
                        st.fleet_open.discard(key)

                if not opened["v"]:
                    # heç vaxt açılmadı = proxy (və ya TikTok bu IP-ni) işləmir: proxy-ni cəzalandır, canlını başqa proxy-yə ver.
                    # Bu, "yayım bitdi" deyil, ona görə giveup sayğacına düşmür.
                    w.errs += 1
                    fails_n += 1
                    if fails_n == 8 and key in self.manual:
                        telegram_notify.post(self.manual[key], f"⏳ <b>@{username}</b>: 8 proxy sınandı, hələ qoşula bilmədi. Cəhd davam edir (qoşulanda bildirəcəm).", html=True)
                    w.bad()
                    tried.add(w)
                    self.where.pop(key, None)
                    w.lives.discard(username)
                    # proxy yoxdursa (tək IP) təkrar-təkrar sıxmayaq: cəhdlər arası artan fasilə
                    await asyncio.sleep(1 if config.PROXIES else min(60, 5 * fails_n))
                    continue
                tried.clear()
                fails_n = 0
                ended_votes = 0

                lived = time.monotonic() - t0
                if key in self.migrating:                  # öz IP-yə qaytarılma: "tez qopdu" sayılmasın
                    self.migrating.discard(key)
                    short_fails = 0
                else:
                    short_fails = short_fails + 1 if lived < 40 else 0
                if short_fails >= config.FLEET_GIVEUP_AFTER:
                    # ƏVVƏL: canlı atılırdı. İNDİ: atılmır, yalnız cəhdlər arası gözləmə (yayım bitibsə yuxarıdakı səsvermə onsuz da çıxarır)
                    short_fails = config.FLEET_GIVEUP_AFTER
                    if not giveup_logged:
                        giveup_logged = True
                        log(f"[ORDU] @{username} → tez-tez qopur, atmıram: 30s fasilələrlə qoşulmağa davam edirəm")
                    await asyncio.sleep(30)
                # hələ canlıdırmı?
                try:
                    await asyncio.to_thread(check_online, username, config.SCAN_TIMEOUT, w.proxy, random_ua())
                except (HostNotOnlineError, UserNotFoundError):
                    ended_votes += 1
                    if ended_votes >= config.FLEET_ENDED_VOTES:
                        log(f"[ORDU] @{username} → yayım bitdi")
                        return
                except Exception:
                    pass                                   # yoxlanmadı: yenidən qoşulmağa çalış
                await asyncio.sleep(min(30, 2 + 3 * short_fails))
        finally:
            self.clients.pop(key, None)
            self.migrating.discard(key)
            st.fleet_open.discard(key)
            self.manual.pop(key, None)
            w2 = self.where.pop(key, None)
            if w2:
                w2.lives.discard(username)
            self.tasks.pop(key, None)
            st.chest_active -= 1
            st.listening_now.discard(username)
            st.listened += 1
            st.live_list.pop(username, None)
            mins = (time.monotonic() - t_start) / 60
            log(f"[ORDU] @{username} → çıxdı: {mins:.0f} dəq qaldı, {found['n']} sandıq")
            if self.watching(username) and mins >= 0.5:
                telegram_notify.notify_admins(f"🏁 <b>@{username}</b> yayımı bitdi: {mins:.0f} dəq izlədim, <b>{found['n']}</b> sandıq gördüm.", html=True)

    # ---------- sandıq ----------
    def _on_envelope(self, username: str, room_id: str, data: dict, found: dict) -> None:
        try:
            info = chest_format.extract(_Raw(data if isinstance(data, dict) else {}))
            if info["ratio"] is None and not info["coins"]:
                return                                      # boş/HIDE hadisəsi
            key = info["env_id"] or (username, info["ts"], info["coins"], info["can_open"])
            if key in self.seen_env:
                return
            self.seen_env.add(key)
            if len(self.seen_env) > 50000:
                self.seen_env.clear()
            found["n"] += 1
            self.stats.chest_users[username] = self.stats.chest_users.get(username, 0) + 1
            record = self.stats.add_chest(username, info)
            log(f"[ORDU] @{username} → 📦 SANDIQ: {chest_format.describe(info)}")
            with open(config.RESULTS_DIR / "chests.txt", "a", encoding="utf-8") as fh:
                fh.write(f"{username} {room_id} {info['coins']} {info['can_open']} {info['ratio']}\n")
            asyncio.get_running_loop().create_task(self._send_chest(username, room_id, info, found["n"], record))
        except Exception as exc:
            log(f"[ORDU] sandıq emalı xətası: {type(exc).__name__}: {exc}")

    async def viewers_of(self, username: str, room_id: str) -> int | None:
        """Canlıdakı izləyici sayı: ordunun proxy-si ilə otaq məlumatı sorğusu (qısa timeout), alınmasa birbaşa IP ilə."""
        key = username.lower()
        w = self.where.get(key)
        proxies = [w.proxy] if w is not None else []
        if "" not in proxies:
            proxies.append("")
        for px in proxies:
            try:
                info = await asyncio.wait_for(
                    asyncio.to_thread(fetch_room_info, room_id, config.VIEWERS_TIMEOUT, "", px, random_ua()),
                    config.VIEWERS_TIMEOUT + 1)
                if info.viewers > 0:
                    self.viewers[key] = info.viewers
                    return info.viewers
            except Exception:
                continue
        return self.viewers.get(key)

    async def _send_chest(self, username: str, room_id: str, info: dict, n: int, record: bool) -> None:
        try:
            v = await self.viewers_of(username, room_id)
            if v:
                info["viewers"] = v
            else:
                info["viewers_unknown"] = True
            log(f"[ORDU] @{username} → 👁 canlıda {v if v else '?'} nəfər")
            alerts.chest_alert(username, info, n, record, force=self.watching(username))
        except Exception as exc:
            log(f"[ORDU] sandıq bildirişi xətası: {type(exc).__name__}: {exc}")
            alerts.chest_alert(username, info, n, record, force=self.watching(username))

    async def _send_luckybag(self, username: str, room_id: str, info: dict, n: int) -> None:
        try:
            v = await self.viewers_of(username, room_id)
            if v:
                info["viewers"] = v
        except Exception:
            pass
        alerts.luckybag_alert(username, info, n, force=self.watching(username), tier=info.get("tier", "sure"))

    def _on_luckybag(self, username: str, room_id: str, d: dict, found: dict, tier: str = "sure") -> None:
        try:
            info = luckybag.extract(d.get("method", ""), d.get("payload"))
            info["tier"] = tier
            sure = tier == "sure"
            # xam payload faylа: dəqiq üçün 20, ehtimal üçün 3 dəfə (hər yayımda)
            rk = "lb_raw" if sure else "lbm_raw"
            if found.get(rk, 0) < (20 if sure else 3):
                found[rk] = found.get(rk, 0) + 1
                with open(config.RESULTS_DIR / "luckybag_raw.txt", "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%F %T')} {tier} {username} {room_id} {info['method']} nums={info['nums']} refs={info['refs']} str={info['strings'][:3]} hex={info['hex'][:800]}\n")
            if sure:
                # refId hər mesajda dəyişir → açar yalnız (yayımçı, növ): eyni yayımda cooldown ərzində BİR xəbər
                if not self._lb.fresh((username, info["kind"])):
                    return
            else:
                # təsdiqsiz: atılmır, amma spam olmasın — eyni mesaj növü bütün yayımlar üçün 30 dəq-də 1, eyni yayımçı üçün 10 dəq-də 1
                if not self._lbm.fresh(("m", info["method"])) or not self._lb.fresh((username, "maybe")):
                    return
            found["lb"] = found.get("lb", 0) + 1
            if sure:
                self.stats.lb_sure += 1
                self.stats.lb_hosts.add(username)
            else:
                self.stats.lb_maybe += 1
            log(f"[ORDU] @{username} → {'🛍 LUCKY BAG' if sure else '🟡 EHTİMAL Lucky Bag'}: {info['method']} refs={info['refs']} {info['strings'][:2]}")
            with open(config.RESULTS_DIR / "luckybag.txt", "a", encoding="utf-8") as fh:
                fh.write(f"{tier} {username} {room_id} {info['method']} {','.join(info['refs'])}\n")
            if sure and config.LUCKYBAG_AUTOWATCH and self.autowatch(username, "Lucky Bag göstərdi"):
                telegram_notify.notify_admins(f"👁 <b>@{html.escape(username)}</b> Lucky Bag göstərdi → izləməyə alındı (yayımı bitsə də qayıdanda qoşulacağam).", html=True)
            if sure and not found.get("lb_rec"):
                found["lb_rec"] = True
                luckybag.record_after(username, room_id, config.RESULTS_DIR, 180)       # 3 dəq bütün mesajları yaz (jeton/nəfər sahələri üçün)
            if info["kind"] != "refresh" or info["refs"] or not sure:
                asyncio.get_running_loop().create_task(self._send_luckybag(username, room_id, info, found["lb"]))
        except Exception as exc:
            log(f"[ORDU] lucky bag emalı xətası: {type(exc).__name__}: {exc}")

    # ---------- pulsuz proxy-lərin avtomatik yenilənməsi ----------
    def save_proven(self) -> int:
        """Real qoşulmada işləmiş proxy-ləri (opens>0) results/proxies_proven.json-a yaz: proxy_auto onları yaddaşda saxlayır."""
        f = Path(__file__).resolve().parent / "results" / "proxies_proven.json"
        try:
            cur = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cur = {}
        now = time.time()
        n = 0
        for w in self.workers:
            if w.proxy and w.opens > 0 and not w.premium:
                cur[w.proxy] = now
                n += 1
        for u in [u for u, ts in cur.items() if now - ts > 3 * 24 * 3600]:
            del cur[u]
        try:
            f.parent.mkdir(exist_ok=True)
            f.write_text(json.dumps(cur), encoding="utf-8")
        except OSError:
            pass
        return n

    def reload_proxies(self) -> tuple[int, int]:
        """results/proxies_good.txt-dəki yeni proxy-ləri işçi kimi əlavə edir; siyahıdan düşən və heç vaxt işləməyənləri kənarlaşdırır."""
        root = Path(__file__).resolve().parent
        try:
            good = [l.strip() for l in (root / "results" / "proxies_good.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
        except OSError:
            return 0, 0
        gset = set(good)
        have = {w.proxy: w for w in self.workers if w.proxy}
        added = 0
        for u in good:
            w = have.get(u)
            if w is not None:
                w.retired = False
                continue
            nw = IPWorker(len(self.workers), u)
            nw.only_one = False
            self.workers.append(nw)
            added += 1
        retired = 0
        for u, w in have.items():
            if u not in gset and not w.lives and w.opens == 0 and not w.premium and not w.retired:
                w.retired = True
                retired += 1
        return added, retired

    async def _proxy_refresh_loop(self) -> None:
        """Hər PROXY_REFRESH_MIN dəqiqədən bir: təzə pulsuz siyahıları endir → TikTok-a qarşı yoxla → işləyənləri ordu-ya əlavə et."""
        if config.PROXY_REFRESH_MIN <= 0:
            return
        root = Path(__file__).resolve().parent
        await asyncio.sleep(min(config.PROXY_REFRESH_MIN * 60 // 2, 600))
        last = time.monotonic()
        while True:
            due = time.monotonic() - last >= config.PROXY_REFRESH_MIN * 60
            up = sum(1 for w in self.workers if w.proxy and not w.retired and w.state() == "up")
            if not due and not (up < 8 and time.monotonic() - last >= 480):      # az işləyən proxy qalıbsa gözləmə: təcili yenilə
                try:
                    if int(time.monotonic()) % 300 < 60:
                        self.save_proven()                                       # ~5 dəqiqədən bir: real işləyən proxy-ləri yadda saxla
                except Exception:
                    pass
                await asyncio.sleep(60)
                continue
            self.save_proven()
            last = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(sys.executable, str(root / "proxy_auto.py"), "--refresh", cwd=str(root),
                                                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=1500)
                except asyncio.TimeoutError:
                    proc.kill()
                    out = b""
                    log("[ORDU] ⚠ proxy yeniləməsi çox çəkdi, dayandırıldı")
                tail = [l for l in out.decode("utf-8", "ignore").splitlines() if l.startswith(("🌐", "1-ci", "2-ci", "=== proxies.txt"))]
                added, retired = self.reload_proxies()
                active = sum(1 for w in self.workers if w.proxy and not w.retired)
                log(f"[ORDU] 🔄 proxy yeniləndi: +{added} yeni işçi, {retired} köhnə kənarlaşdırıldı, aktiv proxy {active} | " + " | ".join(tail[-3:]))
            except Exception as exc:
                log(f"[ORDU] proxy yeniləmə xətası: {type(exc).__name__}: {exc}")

    # ---------- Lucky Bag hostları: izləmə + kəşf ----------
    def autowatch(self, username: str, why: str) -> bool:
        """Lucky Bag göstərən yayımçı /izle siyahısına düşür: yayımı bitsə də qayıdanda ordu yenə qoşulur, bildirişlər həmişə gəlir."""
        have = [x.lower() for x in settings["watch"]]
        if username.lower() in have or len(have) >= 300:
            return False
        settings.set("watch", have + [username.lower()])
        log(f"[ORDU] 👁 @{username} izləməyə alındı ({why})")
        return True

    def adopt_hosts(self, method: str) -> list:
        """/canta_elave ilə təsdiqlənən mesaj növünü artıq göndərmiş bütün yayımçıları izləməyə al."""
        names = []
        for rid in luckybag.rooms_of(method):
            u = self.room_user.get(str(rid))
            if u and self.autowatch(u, f"{method} göndərib"):
                names.append(u)
        return names

    async def _discover_loop(self) -> None:
        """Hər 60 san-də: az yayımda, amma təkrar-təkrar gələn naməlum mesaj növləri → adminə (hər növ üçün 1 dəfə, 3 dəq-də ən çox 1)."""
        await asyncio.sleep(120)
        while True:
            try:
                if config.LUCKYBAG_DISCOVER and time.monotonic() - self._disc_last > 180:
                    for m, rooms, total, hinted in luckybag.suspects(len(self.stats.fleet_open)):
                        if m in luckybag.NOTIFIED:
                            continue
                        luckybag.mark_notified(m)
                        self._disc_last = time.monotonic()
                        inf = luckybag.sample_of(m) or {}
                        who = [self.room_user.get(str(r), "?") for r in list(rooms)[:5]]
                        text = ("🔎 <b>Naməlum nadir mesaj növü</b>" + (" 🟡 (adı çantaya oxşayır)" if hinted else "") + "\n━━━━━━━━━━━━━━━━\n"
                                f"📡 <code>{html.escape(m)}</code>\n📊 {len(rooms)} yayımda, cəmi ×{total}\n"
                                f"👤 {', '.join('@' + html.escape(x) for x in who)}\n"
                                + (f"🏷 {html.escape(' | '.join(inf.get('strings', [])[:3]))[:160]}\n" if inf.get("strings") else "")
                                + (f"🔢 {inf.get('nums', [])[:8]}\n" if inf.get("nums") else "")
                                + "━━━━━━━━━━━━━━━━\n✅ Bu hədiyyə çantasıdırsa:\n<code>/canta_elave " + html.escape(m) + "</code>\n(bu yayımçılar da avtomatik izləməyə düşəcək)")
                        telegram_notify.notify_admins(text, html=True)
                        break
            except Exception as exc:
                log(f"[ORDU] kəşf xətası: {type(exc).__name__}: {exc}")
            await asyncio.sleep(60)

    # ---------- paylayıcı ----------
    async def run(self, queue: asyncio.Queue) -> None:
        log(f"[ORDU] 🪖 {len(self.workers)} IP işçisi, tutum {self.capacity()} canlı "
            f"({'birbaşa IP + ehtiyat proxy' if config.PROXY_FALLBACK else ('proxy' if config.PROXIES else 'birbaşa IP, proxy YOXDUR')})")
        if self.capacity() < config.FLEET_TARGET_LIVES:
            log(f"[ORDU] ⚠ tutum {self.capacity()} < hədəf {config.FLEET_TARGET_LIVES}: proxy əlavə edin və ya FLEET_PER_IP_PREMIUM artırın")
        log(f"[ORDU] 🎯 eyni anda maksimum canlı hədd: {config.FLEET_MAX_LIVES} | real tutum: {min(self.capacity(), config.FLEET_MAX_LIVES)}")
        try:
            telegram_notify.notify_admins(
                f"🚀 <b>Ordu başladı</b>\n🎯 Maksimum canlı: <b>{config.FLEET_MAX_LIVES}</b>\n"
                f"🪖 IP işçisi: {len(self.workers)} | tutum: {min(self.capacity(), config.FLEET_MAX_LIVES)}", html=True)
        except Exception:
            pass
        wd = asyncio.create_task(self._watchdog())
        wl = asyncio.create_task(self._watch_loop())
        dl = asyncio.create_task(self._discover_loop())
        pr = asyncio.create_task(self._proxy_refresh_loop())
        rb = asyncio.create_task(self._rebalance_loop())
        waiting = collections.deque()
        closing = False                                   # skan bitdi, amma yer gözləyən canlılar hamısı qoşulana qədər davam
        rp = asyncio.create_task(self._report_loop())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1)
                except asyncio.TimeoutError:
                    item = "tick"
                # tapılan canlılar DƏRHAL götürülür (növbədə yığılıb qalmasın)
                while True:
                    if item is None:
                        closing = True
                    elif item != "tick":
                        waiting.append(item)
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                # yer olduqca ardıcıl yerləşdir; yer qurtardısa qalanlar gözləyir (hər canlı üçün bütün siyahını təkrar TARAMIRIQ)
                placed = 0
                free_total = sum(max(0, w.free()) for w in self.workers) if waiting else 0   # bir dəfə hesabla (hər canlı üçün pick() yox)
                while waiting and len(self.tasks) < config.FLEET_MAX_LIVES:
                    username, room_id = waiting[0]
                    if self.tracking(username):
                        waiting.popleft()
                        continue
                    if free_total <= 0:
                        break
                    free_total -= 1
                    waiting.popleft()
                    self.tasks[username.lower()] = asyncio.create_task(self.hold(username, room_id))
                    placed += 1
                    if placed % 25 == 0:
                        await asyncio.sleep(0)   # qoşulmaların sürətini _gate() və semafor tənzimləyir
                self.waiting_n = len(waiting)
                if len(waiting) > 50 and int(time.time()) % 60 < 3:
                    log(f"[ORDU] ⚠ {len(waiting)} canlı yer gözləyir: proxy əlavə edin və ya FLEET_PER_IP / FLEET_MAX_LIVES artırın")
                if closing and (not waiting or self.ctl.stop_requested):
                    break
            # skan bitdi (tək dövr rejimi): qalan yayımların bitməsini gözlə
            while self.tasks and not self.ctl.stop_requested:
                await asyncio.sleep(5)
        finally:
            wd.cancel()
            wl.cancel()
            dl.cancel()
            pr.cancel()
            try:
                self.save_proven()
            except Exception:
                pass
            rb.cancel()
            rp.cancel()
            for t in list(self.tasks.values()):
                t.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def _report_loop(self) -> None:
        """30 saniyədən bir vəziyyət sətri: darboğazı (proxy ölü / yük / blok) görmək üçün."""
        while True:
            await asyncio.sleep(30)
            try:
                up = sum(1 for w in self.workers if w.proxy and w.state() == "up")
                n_prox = sum(1 for w in self.workers if w.proxy)
                busy = self._csem_n - self._csem._value
                top = ", ".join(f"{k}:{v}" for k, v in sorted(self.err_hist.items(), key=lambda kv: -kv[1])[:4]) or "-"
                log(f"[ORDU] 📊 qoşulu {len(self.stats.fleet_open)} | tapşırıq {len(self.tasks)} | gözləyən {self.waiting_n} | "
                    f"qoşulur {busy}/{self._csem_n} | proxy {up}/{n_prox} aktiv | "
                    f"son 30s: ✅{self.ctr['open']} ❌{self.ctr['fail']} | xətalar: {top}")
                self.ctr["open"] = self.ctr["fail"] = 0
                self.err_hist.clear()
                self._dump_methods()
            except Exception as exc:
                log(f"[ORDU] hesabat xətası: {type(exc).__name__}: {exc}")

    def _dump_methods(self) -> None:
        """Serverdən gələn BÜTÜN mesaj növləri (ad: say) → results/methods_seen.txt. Lucky Bag-in real adını tapmaq üçün."""
        try:
            from piratetok_live.connection.wss import METHOD_SEEN
            rows = sorted(METHOD_SEEN.items(), key=lambda kv: -kv[1])
            with open(config.RESULTS_DIR / "methods_seen.txt", "w", encoding="utf-8") as fh:
                fh.write("\n".join(f"{v}\t{k}" for k, v in rows) + "\n")
            from piratetok_live.connection.wss import SAMPLES
            with open(config.RESULTS_DIR / "method_samples.txt", "w", encoding="utf-8") as fh:
                fh.write("\n".join(f"{k}\t{v}" for k, v in SAMPLES.items()) + "\n")
        except Exception:
            pass

    async def _rebalance_loop(self) -> None:
        """Öz IP sakatlıqdan qayıdanda (dincəlmə bitib, yer var, qoşulma növbəsi qısadır) ehtiyat proxy-lərdəki
        canlıları ona geri köçürür: əsas heyət meydana qayıdır, ehtiyatlar skamyaya."""
        if not config.PROXY_FALLBACK:
            return
        d = self.workers[0]
        while True:
            await asyncio.sleep(config.FLEET_REBALANCE_EVERY)
            try:
                if d.state() != "up" or d.free() <= 0 or self._gate_wait(d) > config.FLEET_DIRECT_QUEUE_MAX:
                    continue
                movable = [k for k, w in list(self.where.items())
                           if w is not d and k in self.clients and k in self.stats.fleet_open]
                g = self._g(d)
                room = max(1, int((config.FLEET_DIRECT_QUEUE_MAX - self._gate_wait(d)) / max(g["delay"], 0.1)))   # növbəyə sığan qədər (təkrar köçürmə olmasın)
                n = max(0, min(d.free(), config.FLEET_REBALANCE_BATCH, room, len(movable)))
                for k in movable[:n]:
                    w = self.where.pop(k, None)
                    if w:
                        w.lives.discard(k)
                    self.migrating.add(k)
                    self.clients[k].disconnect()
                if n:
                    log(f"[ORDU] ↩ {n} canlı ehtiyat proxy-dən öz IP-yə qaytarılır")
            except Exception as exc:
                log(f"[ORDU] geri qaytarma xətası: {type(exc).__name__}: {exc}")

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(5)
            if self.ctl.stop_requested:
                for t in list(self.tasks.values()):
                    t.cancel()


_FLEET: Fleet | None = None


def snapshot() -> dict | None:
    return _FLEET.snapshot() if _FLEET else None


def join(username: str, room_id: str, cid: int) -> str:
    """'ok' | 'already' | 'nofleet'"""
    return _FLEET.join(username, room_id, cid) if _FLEET else "nofleet"


def watch_state(username: str) -> str:
    """'open' (qoşulu) | 'joining' (qoşulmağa çalışır) | 'idle' (yayım gözlənilir)"""
    if is_open(username):
        return "open"
    return "joining" if tracking(username) else "idle"


def adopt_hosts(method: str) -> list:
    return _FLEET.adopt_hosts(method) if _FLEET else []


def is_open(username: str) -> bool:
    return bool(_FLEET and username.lower() in _FLEET.stats.fleet_open)


def tracking(username: str) -> bool:
    return bool(_FLEET and _FLEET.tracking(username))


async def run(queue: asyncio.Queue, stats, ctl) -> None:
    global _FLEET
    _FLEET = Fleet(stats, ctl)
    try:
        await _FLEET.run(queue)
    finally:
        _FLEET = None
