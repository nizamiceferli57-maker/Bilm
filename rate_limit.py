"""Skaner üçün planlaşdırıcı: paralel işçi sayı blokdan blok enir, kanal başına sürət tənzimlənir.

Məntiq (hər kanal = proxy və ya birbaşa bağlantı):
- Başlanğıcda SCAN_START_WORKERS (default 4) paralel işçi işləyir.
- TikTok blokladıqca paralel sayı 1 azalır (4 → 3 → 2 → 1) və blok olmayan səviyyədə davam edilir.
  Bu səviyyədə qalır; geri qalxmaq üçün SCAN_RECOVER_AFTER (uğurlu sorğu sayı) verilə bilər, default söndürülüdür.
- Səviyyə artıq 1-dirsə və blok davam edirsə, sorğular arası interval artır və soyuma ardıcıl blokda 2 qat böyüyür.
- Sorğular arasında kiçik təsadüfi interval (SCAN_SPACING) var, hər kanalın sabit User-Agent-i var.
- Proxy siyahısı varsa, hər proxy ayrıca səviyyəyə malikdir; ümumi paralel say onların cəmidir.
"""
import asyncio
import random
import time
from dataclasses import dataclass

import config
from log import log

# proxy -> "ölüdür" vaxtı (monotonic). Skaner yazır, ordu (fleet.py) oxuyur: ölü proxy-yə yeni canlı verilmir.
PROXY_DOWN: dict[str, float] = {}
PROXY_FAIL_LIMIT = 3          # ardıcıl bu qədər şəbəkə xətasından sonra proxy dincəlir
PROXY_DOWN_BASE = 60          # ilk dincəlmə (s); hər təkrarda 2 qat, ən çox PROXY_DOWN_MAX
PROXY_DOWN_MAX = 900


DEAD_AFTER = 3          # pulsuz proxy bu qədər dincəlmədən sonra HEÇ BİR uğurlu sorğu verməyibsə: ölü sayılır


def retire(proxy: str, why: str) -> None:
    """Ölü pulsuz proxy bu işdə bir daha işlənmir və results/dead_proxies.txt-ə yazılır (növbəti başlanğıcda yüklənmir)."""
    from log import log
    PROXY_DOWN[proxy] = time.monotonic() + 10 ** 7
    try:
        with open(config.RESULTS_DIR / "dead_proxies.txt", "a", encoding="utf-8") as fh:
            fh.write(proxy + "\n")
    except OSError:
        pass
    log(f"[SKAN] 💀 proxy ölü sayıldı ({why}), artıq işlənmir: {proxy.split('@')[-1][:40]}")


def proxy_down(proxy: str) -> bool:
    return bool(proxy) and PROXY_DOWN.get(proxy, 0.0) > time.monotonic()


@dataclass
class Lane:
    proxy: str
    ua: str
    interval: float
    base_interval: float
    level: int
    max_level: int
    name: str = ""
    next_at: float = 0.0
    cooldown_until: float = 0.0
    cooldown_started: float = 0.0
    ok_streak: int = 0
    block_streak: int = 0
    floor_blocks: int = 0
    blocks: int = 0
    requests: int = 0
    fail_streak: int = 0      # ardıcıl şəbəkə/proxy xətası
    down_count: int = 0       # neçə dəfə dincəlməyə göndərilib (ardıcıl)
    proxy_fails: int = 0      # cəmi proxy xətası
    oks: int = 0              # cəmi uğurlu sorğu


class Scheduler:
    def __init__(self, proxies: list[str], make_ua) -> None:
        self.fallback = bool(proxies) and config.PROXY_FALLBACK
        direct_too = bool(proxies) and not self.fallback and config.INCLUDE_DIRECT
        sources = ([""] + list(proxies)) if (self.fallback or direct_too) else (proxies or [""])   # 0-cı kanal = telefonun öz IP-si
        self._on_proxies = False
        start = max(1, config.SCAN_START_WORKERS)
        if proxies and not self.fallback:
            start = max(start, config.SCAN_PER_LANE)       # hər proxy paralel işləyir: ümumi sürət = proxy sayı × paralel
        self.lanes = [
            Lane(
                proxy=p,
                ua=make_ua(),
                interval=config.SCAN_SPACING,
                base_interval=config.SCAN_SPACING,
                level=start,
                max_level=start,
                name=(_mask(p) if p else "birbaşa"),
            )
            for p in sources
        ]
        self._lock = asyncio.Lock()
        self.fixed_rate: float | None = None  # ad/saniyə; təyin olunubsa sürət heç vaxt dəyişmir

    @property
    def parallel_limit(self) -> int:
        """Hazırda icazə verilən ümumi paralel işçi sayı."""
        if self.fallback:
            now = time.monotonic()
            d = self.lanes[0]
            if d.cooldown_until <= now:
                return d.level
            live = [l for l in self.lanes[1:] if l.cooldown_until <= now]
            return sum(l.level for l in live) if live else d.level
        return sum(l.level for l in self.lanes)

    def cooling(self) -> tuple[int, float]:
        """(soyuyan kanal sayı, ən tez qayıdana qədər saniyə) — bloklanan proxy-lər gözləyib özü qayıdır."""
        now = time.monotonic()
        left = [l.cooldown_until - now for l in self.lanes if l.cooldown_until > now]
        return len(left), (min(left) if left else 0.0)

    def _pick(self) -> Lane:
        """Fallback rejimi: birbaşa IP işləyirsə yalnız o; bloklanıbsa ehtiyat proxy-lər; bərpa olunanda geri qayıdır."""
        now = time.monotonic()
        if self.fallback:
            d = self.lanes[0]
            if d.cooldown_until <= now:
                if self._on_proxies:
                    self._on_proxies = False
                    log("[SKAN] 🔙 birbaşa IP bərpa olundu → ehtiyat proxy-lərdən geri qayıdıldı")
                return d
            ready = [l for l in self.lanes[1:] if l.cooldown_until <= now]
            if ready:
                if not self._on_proxies:
                    self._on_proxies = True
                    log(f"[SKAN] 🔀 birbaşa IP bloklandı → {len(ready)} ehtiyat proxy işə düşdü")
                return min(ready, key=lambda l: l.next_at)
        return min(self.lanes, key=lambda l: max(l.next_at, l.cooldown_until))

    @property
    def max_parallel(self) -> int:
        return sum(l.max_level for l in self.lanes)

    @property
    def worker_cap(self) -> int:
        """İşçi hovuzunun ölçüsü: /surat ilə paralel say sonradan artırıla bilsin deyə ehtiyatla yaradılır."""
        return len(self.lanes) * max(config.SCAN_WORKER_CAP, max(l.max_level for l in self.lanes))

    def set_fixed(self, rate: float | None) -> None:
        """Sabit sürət (Telegram /sabit): ümumi `rate` ad/saniyə, dəqiq fasilə, blokda yavaşlamır."""
        if not rate or rate <= 0:
            self.fixed_rate = None
            return
        self.fixed_rate = float(rate)
        interval = len(self.lanes) / self.fixed_rate  # bütün kanallar cəmi = rate
        for l in self.lanes:
            l.max_level = l.level = max(1, config.SCAN_WORKER_CAP)  # paralel sayı yox, fasilə sürəti təyin edir
            l.base_interval = l.interval = interval
            l.block_streak = l.floor_blocks = l.ok_streak = 0

    def set_speed(self, workers: int, interval: float) -> None:
        """İş zamanı sürəti dəyiş (Telegram /surat). Soyuma davam edir, səviyyə və interval yenilənir."""
        self.fixed_rate = None
        workers = max(1, min(workers, config.SCAN_WORKER_CAP))
        interval = max(0.2, min(interval, config.SCAN_MAX_SPACING))
        for l in self.lanes:
            l.max_level = workers
            l.level = workers
            l.base_interval = interval
            l.interval = interval
            l.block_streak = 0
            l.floor_blocks = 0
            l.ok_streak = 0

    @property
    def avg_interval(self) -> float:
        return sum(l.interval for l in self.lanes) / len(self.lanes)

    async def acquire(self) -> Lane:
        async with self._lock:
            lane = self._pick()
            now = time.monotonic()
            ready = max(lane.next_at, lane.cooldown_until, now)
            jitter = 0.0 if self.fixed_rate else random.random() * 0.6
            lane.next_at = ready + lane.interval * (1.0 + jitter)
            lane.requests += 1
        wait = ready - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        # gözləyərkən kanal bloklanıbsa, soyumanın bitməsini gözlə
        while lane.cooldown_until > time.monotonic():
            await asyncio.sleep(max(lane.cooldown_until - time.monotonic(), 0.05))
        return lane

    def health(self) -> dict:
        """Skan kanallarının vəziyyəti (Telegram paneli və aqreqat log üçün)."""
        now = time.monotonic()
        total = len(self.lanes)
        down = sum(1 for l in self.lanes if l.proxy and PROXY_DOWN.get(l.proxy, 0.0) > now)
        blocked = sum(1 for l in self.lanes if l.cooldown_until > now) - down
        proven = sum(1 for l in self.lanes if l.oks > 0)
        return {"total": total, "down": down, "blocked": max(blocked, 0), "proven": proven,
                "up": total - down}

    def report(self, lane: Lane, status: str, issued_at: float) -> None:
        """status: 'ok' | 'blocked' | 'neutral' | 'proxyfail' (proxy/şəbəkə qoşulma xətası)."""
        now = time.monotonic()
        if status == "proxyfail":
            lane.ok_streak = 0
            lane.proxy_fails += 1
            if issued_at < lane.cooldown_started:
                return  # soyumadan əvvəl göndərilmiş sorğunun gec gələn xətası, təkrar sayma
            lane.fail_streak += 1
            if lane.proxy and lane.fail_streak >= PROXY_FAIL_LIMIT:
                delay = float(min(PROXY_DOWN_BASE * (2 ** lane.down_count), PROXY_DOWN_MAX))
                lane.down_count += 1
                lane.fail_streak = 0
                lane.cooldown_started = now
                lane.cooldown_until = now + delay
                PROXY_DOWN[lane.proxy] = now + delay
                if lane.oks == 0 and lane.down_count >= DEAD_AFTER and "@" not in lane.proxy:
                    retire(lane.proxy, f"{lane.proxy_fails} xəta, 0 uğur")
                    lane.cooldown_until = now + 10 ** 7
            return
        if status == "ok":
            lane.oks += 1
            lane.fail_streak = 0
            lane.down_count = 0
            if lane.proxy:
                PROXY_DOWN.pop(lane.proxy, None)
            lane.block_streak = 0
            lane.floor_blocks = 0
            lane.ok_streak += 1
            if self.fixed_rate:
                return
            if lane.interval > lane.base_interval and lane.ok_streak >= 25:
                lane.interval = max(lane.base_interval, lane.interval * 0.9)
                lane.ok_streak = 0
            elif config.SCAN_RECOVER_AFTER > 0 and lane.level < lane.max_level and lane.ok_streak >= config.SCAN_RECOVER_AFTER:
                lane.level += 1
                lane.ok_streak = 0
                log(f"[SKAN] ✅ uzun müddət blok yoxdur ({lane.name}): paralel {lane.level - 1} → {lane.level}")
        elif status == "blocked":
            lane.ok_streak = 0
            lane.fail_streak = 0   # TikTok cavab verdi: proxy işləyir, sadəcə bloklanıb
            if issued_at < lane.cooldown_started:
                return  # bu sorğu soyumadan əvvəl göndərilib, təkrar cəzalandırma
            if self.fixed_rate:  # sabit rejim: sürət dəyişmir, yalnız sabit qısa soyuma
                lane.blocks += 1
                delay = float(config.BLOCK_BACKOFF)
                lane.cooldown_started = now
                lane.cooldown_until = now + delay
                log(f"[SKAN] ⏳ blok ({lane.name}): sabit sürət {self.fixed_rate:g} ad/san saxlanır, {delay:.0f}s soyuma")
                return
            lane.block_streak += 1
            lane.blocks += 1
            if lane.level > 1:
                old = lane.level
                lane.level = max(1, lane.level // 2) if lane.level > 4 else lane.level - 1   # yüksək paralelliyi tez endir
                delay = float(config.BLOCK_BACKOFF)
                what = f"paralel {old} → {lane.level}"
            else:
                lane.floor_blocks += 1
                lane.interval = min(config.SCAN_MAX_SPACING, lane.interval * 1.6)
                delay = float(min(config.BLOCK_BACKOFF * (2 ** (lane.floor_blocks - 1)), config.BLOCK_BACKOFF_MAX))
                what = f"paralel 1 (minimum), interval {lane.interval:.2f}s"
            lane.cooldown_started = now
            lane.cooldown_until = now + delay
            log(f"[SKAN] ⏳ blok ({lane.name}): {what}, {delay:.0f}s soyuma")


def _mask(proxy: str) -> str:
    """Proxy-ni loga yazanda parolu gizlət."""
    if "@" in proxy:
        scheme, _, rest = proxy.partition("://")
        return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"
    return proxy
