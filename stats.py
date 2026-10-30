"""Ümumi sayğaclar və hesablamalar (skaner + sandıq işçiləri + bot panel burdan oxuyur)."""
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Stats:
    total: int = 0
    scanned: int = 0
    live: int = 0
    offline: int = 0
    notfound: int = 0
    errors: int = 0
    blocked_hits: int = 0
    invalid: int = 0
    scan_interval: float = 0.0
    scan_workers: int = 0
    euler_verified: int = 0
    euler_rejected: int = 0
    euler_unverified: int = 0
    listened: int = 0
    chest_active: int = 0
    chest_errors: int = 0
    chest_total: int = 0
    scan_finished: bool = False
    lb_sure: int = 0                                # dəqiq Lucky Bag xəbərləri
    lb_maybe: int = 0                               # ehtimal (təsdiqsiz) xəbərləri
    lb_hosts: set = field(default_factory=set)      # Lucky Bag göstərmiş yayımçılar
    chest_users: dict = field(default_factory=dict)
    chest_best: list = field(default_factory=list)  # (ratio, ad, coins, can_open)
    chest_log: deque = field(default_factory=lambda: deque(maxlen=50))  # ən son sandıqlar
    live_list: dict = field(default_factory=dict)   # ad -> {room, ts, note}
    listening_now: set = field(default_factory=set)
    fleet_open: set = field(default_factory=set)    # WebSocket həqiqətən açılmış canlılar
    chest_debug_logged: bool = False
    started_at: float = field(default_factory=time.monotonic)
    scanned0: int = 0
    scan_times: deque = field(default_factory=lambda: deque(maxlen=2000))
    block_times: deque = field(default_factory=lambda: deque(maxlen=100))
    cooldown_until: float = 0.0
    record: tuple | None = None  # (ratio, ad, coins, can_open) - ən yüksək ratio

    # ---------- qeydlər ----------
    def mark_scanned(self) -> None:
        self.scan_times.append(time.monotonic())

    def mark_block(self) -> None:
        self.block_times.append(time.monotonic())

    def add_chest(self, username: str, info: dict) -> bool:
        """Sandığı qeyd edir. Yeni rekord olarsa True qaytarır."""
        self.chest_total += 1
        self.chest_log.appendleft(
            {"ts": time.time(), "user": username, "coins": info["coins"], "can_open": info["can_open"], "ratio": info["ratio"]}
        )
        if info["ratio"] is None:
            return False
        self.chest_best.append((info["ratio"], username, info["coins"], info["can_open"]))
        self.chest_best.sort(reverse=True)
        del self.chest_best[10:]
        if self.record is None or info["ratio"] > self.record[0]:
            first = self.record is None
            self.record = (info["ratio"], username, info["coins"], info["can_open"])
            return not first  # ilk sandıq "rekord" sayılmır
        return False

    # ---------- hesablamalar ----------
    @property
    def unchecked(self) -> int:
        return max(self.total - self.scanned, 0)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def rate_per_min(self, window: float = 300.0) -> float:
        """Son `window` saniyədə yoxlanan ad sayı / dəqiqə (blok gözləmələri də daxildir → real sürət)."""
        now = time.monotonic()
        n = sum(1 for t in self.scan_times if now - t <= window)
        if n < 3:
            return 0.0
        span = min(window, max(now - self.started_at, 1.0))
        return n / span * 60.0

    def eta(self) -> str:
        rate = self.rate_per_min()
        if rate <= 0:
            return "?"
        return fmt_duration(self.unchecked / rate * 60.0)

    def recent_blocks(self, window: float = 600.0) -> int:
        now = time.monotonic()
        return sum(1 for t in self.block_times if now - t <= window)

    def health(self) -> tuple[str, str]:
        """(emoji, mətn) — TikTok blok vəziyyəti."""
        now = time.monotonic()
        if self.scan_finished:
            return "✅", "Skan bitib"
        if self.cooldown_until > now:
            return "🟡", f"Soyumada ({int(self.cooldown_until - now)}s)"
        if self.recent_blocks(600) >= 4:
            return "🔴", "Tez-tez blok (sürəti azaldın)"
        if self.recent_blocks(600) >= 1:
            return "🟠", "Yaxınlarda blok olub"
        return "🟢", "Stabil, blok yoxdur"

    def status_line(self) -> str:
        return (
            f"📊 Yoxlanıb {self.scanned}/{self.total} | "
            f"🟢 canlı {self.live} | ⚪ offline {self.offline} | ❌ tapılmadı {self.notfound} | "
            f"⏳ yoxlanılmamış {self.unchecked} | ⚠ xəta {self.errors} | "
            f"✔ Euler təsdiq {self.euler_verified}/təkzib {self.euler_rejected} | "
            f"👥 paralel {self.scan_workers} | 🚫 blok {self.blocked_hits} | ETA {self.eta()} | "
            f"🔌 dinlənir {self.chest_active} | 👂 dinlənib {self.listened}/{self.live} | "
            f"📦 sandıqlı yayım {len(self.chest_users)}"
        )


def fmt_duration(secs: float) -> str:
    """Aydın vahidlər: 2 gün 3 saat • 5 saat 12 dəq • 7 dəq 30 san • 40 san."""
    secs = int(max(secs, 0))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d} gün {h} saat"
    if h:
        return f"{h} saat {m} dəq"
    if m:
        return f"{m} dəq {s} san"
    return f"{s} san"
