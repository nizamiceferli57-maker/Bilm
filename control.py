"""Botun ümumi idarəetmə vəziyyəti: fasilə / davam / dayandır / yenidən başlat. Telegram əmrləri bunu dəyişir."""
import asyncio
import time

from stats import Stats


class Control:
    def __init__(self) -> None:
        self.stats = Stats()
        self.phase = "idle"          # idle | running | paused | stopping | done
        self._gate = asyncio.Event()  # set = işləyir, clear = fasilə
        self._gate.set()
        self.stop_requested = False
        self.start_event = asyncio.Event()
        self.shutdown = asyncio.Event()
        self.resume_next = False
        self.queue: asyncio.Queue | None = None
        self.sched = None
        self.euler = None
        self.boot = time.monotonic()
        self.cycle = 0
        self.last_error = ""
        self.next_rescan = 0.0               # növbəti siyahı yoxlamasının vaxtı (time.time())
        self.rescan_now = asyncio.Event()    # "indi yenidən yoxla" düyməsi gözləməni kəsir

    # ---- vəziyyət ----
    @property
    def paused(self) -> bool:
        return not self._gate.is_set()

    @property
    def active(self) -> bool:
        return self.phase in ("running", "paused", "stopping")

    def uptime(self) -> float:
        return time.monotonic() - self.boot

    def pause(self) -> bool:
        if self.phase != "running":
            return False
        self._gate.clear()
        self.phase = "paused"
        return True

    def resume(self) -> bool:
        if self.phase != "paused":
            return False
        self._gate.set()
        self.phase = "running"
        return True

    def request_stop(self) -> bool:
        if not self.active:
            return False
        self.stop_requested = True
        self.rescan_now.set()   # 30 dəqiqəlik gözləmə dayansın
        self.phase = "stopping"
        self._gate.set()  # fasilədə olanlar da çıxa bilsin
        return True

    def request_start(self, resume: bool = False) -> bool:
        if self.active:
            return False
        self.resume_next = resume
        self.start_event.set()
        return True

    async def wait_if_paused(self) -> None:
        await self._gate.wait()

    def new_cycle(self) -> Stats:
        self.cycle += 1
        self.stats = Stats()
        self.stop_requested = False
        self._gate.set()
        self.phase = "running"
        self.start_event.clear()
        return self.stats
