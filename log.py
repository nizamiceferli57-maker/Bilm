"""Konsol çıxışı: vaxt damğası + dərhal göstərmə. Son sətirlər /log əmri üçün yaddaşda saxlanır."""
import logging
import re
import sys
import time
from collections import deque

LINES: deque = deque(maxlen=300)

_CURL_TAIL = re.compile(r"\.?\s*See https://curl\.se\S* first for more details\.?")
_CURL_PREFIX = re.compile(r"Failed to perform, ")


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    LINES.append(line)
    print(line, flush=True)


def short_err(exc: BaseException, limit: int = 140) -> str:
    """curl xətasının uzun quyruğunu (libcurl linki) kəsir."""
    s = _CURL_PREFIX.sub("", _CURL_TAIL.sub("", str(exc))).strip()
    return s[:limit]


def err_kind(exc: BaseException) -> str:
    """Xətanı qısa kateqoriyaya çevirir (aqreqat hesabat üçün)."""
    s = str(exc)
    m = re.search(r"CONNECT tunnel failed, response (\d+)", s)
    if m:
        return f"tunel {m.group(1)}"
    for key, name in (
        ("(28)", "timeout"),
        ("(56)", "qopdu/reset"),
        ("(97)", "socks"),
        ("(7)", "qoşulmadı"),
        ("(35)", "TLS"),
        ("(60)", "TLS"),
        ("(52)", "boş cavab"),
        ("(5)", "DNS"),
        ("(6)", "DNS"),
    ):
        if key in s:
            return name
    return type(exc).__name__


class _LogHandler(logging.Handler):
    """Kitabxanaların (piratetok və s.) xəbərdarlıqlarını eyni formatda, təkrarlanmadan göstərir."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._last: dict = {}
        self._hidden: dict = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = _CURL_TAIL.sub("", record.getMessage())
            msg = _CURL_PREFIX.sub("", msg)
            key = msg.split(":")[0][:40]
            now = time.monotonic()
            if now - self._last.get(key, 0) < 30:      # eyni tip xəbərdarlıq 30s-də bir dəfə
                self._hidden[key] = self._hidden.get(key, 0) + 1
                return
            extra = self._hidden.pop(key, 0)
            self._last[key] = now
            tail = f" (+{extra} oxşar gizlədildi)" if extra else ""
            log(f"[{record.name.split('.')[-1]}] ⚠ {msg[:160]}{tail}")
        except Exception:
            pass


def install_logging() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(_LogHandler())
    root.setLevel(logging.WARNING)


def ensure_unbuffered() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
