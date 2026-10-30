"""Euler Stream API ilə canlı təsdiqi (pulsuz planda işləyən /webcast/fetch yolu).

GET /webcast/fetch?room_id=<id>&client=ttlive-python  (başlıq: X-Api-Key)
Otaq canlıdırsa, Euler websocket üçün ilk protobuf cavabını (200) qaytarır. Canlı deyilsə, JSON xəta gəlir.

Qeyd: hər çağırış gündəlik imza limitindən (pulsuz planda 2500) bir sorğu yeyir. Sandıq işçiləri də eyni
limitdən istifadə edir, ona görə təsdiq sorğularına EULER_VERIFY_BUDGET qədər yuxarı hədd qoyulub.
"""
import asyncio
import time

import httpx

import config
from log import log

_AUTH_FAIL_LIMIT = 3
_OFFLINE_HINTS = ("not live", "not currently live", "isn't live", "is not live", "offline", "ended", "finished", "not_live")


class EulerClient:
    def __init__(self) -> None:
        self.enabled = bool(config.EULER_API_KEY)
        self._http: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(config.EULER_CONCURRENCY)
        self._cool_until = 0.0
        self._auth_fails = 0
        self.used = 0
        self._budget_warned = False
        self._unknown_logged = 0

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=config.EULER_BASE_URL,
                headers={"X-Api-Key": config.EULER_API_KEY, "User-Agent": "tt-live-chest-bot/1.0"},
                timeout=config.EULER_TIMEOUT,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _disable(self, why: str) -> None:
        if self.enabled:
            self.enabled = False
            log(f"[EULER] ⛔ söndürüldü: {why} PirateTok ilə davam.")

    async def verify_room(self, username: str, room_id: str) -> str:
        """'live' | 'offline' | 'unknown' | 'disabled'. Yalnız aydın "canlı deyil" cavabı 'offline' sayılır."""
        if not self.enabled:
            return "disabled"
        if self.used >= config.EULER_VERIFY_BUDGET:
            if not self._budget_warned:
                self._budget_warned = True
                log(f"[EULER] təsdiq büdcəsi bitdi ({config.EULER_VERIFY_BUDGET}); qalan canlılar 'təsdiqsiz' qalacaq (limit sandıq işçilərinə qalır)")
            return "unknown"

        wait = self._cool_until - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

        async with self._sem:
            self.used += 1
            try:
                r = await self._client().get(
                    "/webcast/fetch",
                    params={"client": "ttlive-python", "room_id": str(room_id), "client_enter": "true"},
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                log(f"[EULER] @{username} → şəbəkə xətası: {type(exc).__name__}")
                return "unknown"

        if r.status_code == 429:
            try:
                retry = float(r.headers.get("Retry-After", "10"))
            except ValueError:
                retry = 10.0
            self._cool_until = time.monotonic() + min(max(retry, 1.0), 120.0)
            log(f"[EULER] rate limit (429), {retry:.0f}s gözlənilir")
            return "unknown"

        body = r.content or b""
        if r.status_code == 200 and body:
            if body[:1] != b"{":
                self._auth_fails = 0
                return "live"  # protobuf: otaq canlıdır
            try:
                data = r.json()
            except ValueError:
                return "live"
            if not (isinstance(data, dict) and (data.get("ok") is False or "message" in data and "code" in data and data.get("code") != 200)):
                self._auth_fails = 0
                return "live"
        else:
            try:
                data = r.json()
            except ValueError:
                data = {}

        msg = str(data.get("message", "")) if isinstance(data, dict) else ""
        low = msg.lower()

        if r.status_code in (401, 402, 403):
            if "plan" in low:
                self._disable(msg[:120])
                return "disabled"
            self._auth_fails += 1
            log(f"[EULER] HTTP {r.status_code}: açar problemi ({self._auth_fails}/{_AUTH_FAIL_LIMIT}) {msg[:100]}")
            if self._auth_fails >= _AUTH_FAIL_LIMIT:
                self._disable("açar etibarsızdır.")
            return "unknown"

        if any(h in low for h in _OFFLINE_HINTS):
            return "offline"

        if self._unknown_logged < 5:
            self._unknown_logged += 1
            log(f"[EULER] @{username} → tanınmayan cavab (HTTP {r.status_code}): {body[:200]!r}")
        return "unknown"
