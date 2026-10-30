"""Pulsuz proxy siyahılarını internetdən endirir (proxy_sources.txt) → proxy_lists/auto_fetched.txt (sxem://ip:port).
Yalnız endirir, YOXLAMIR: yoxlamanı proxy_auto.py / proxy_check_all.py edir.
"""
import random
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "proxy_sources.txt"
OUT = ROOT / "proxy_lists" / "auto_fetched.txt"
LINE = re.compile(r"^(?:(?P<s>[a-z0-9]+)://)?(?P<h>\d{1,3}(?:\.\d{1,3}){3}):(?P<p>\d{2,5})\b")
OK_SCHEMES = {"http": "http", "https": "http", "socks4": "socks4", "socks5": "socks5", "socks5h": "socks5"}
PRIVATE = ("0.", "10.", "127.", "192.168.", "169.254.", "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.30.", "172.31.")


def load_sources() -> list:
    out = []
    try:
        for line in SOURCES.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) >= 2 and parts[1].startswith("http"):
                out.append((parts[0].strip().lower(), parts[1].strip(), int(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() else 0))
    except OSError:
        pass
    return out


def fetch_one(src) -> list:
    default, url, cap = src
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read(12_000_000).decode("utf-8", "ignore")
    except Exception:
        return []
    res = []
    for line in text.splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        sch = OK_SCHEMES.get((m.group("s") or default).lower())
        host, port = m.group("h"), int(m.group("p"))
        if not sch or host.startswith(PRIVATE) or not (1 <= port <= 65535) or any(int(x) > 255 for x in host.split(".")):
            continue
        res.append(f"{sch}://{host}:{port}")
    if cap and len(res) > cap:
        res = random.sample(res, cap)
    return res


def fetch_all(force: bool = False, max_age: float = 1200.0) -> int:
    """Qaytarır: auto_fetched.txt-dəki proxy sayı. Fayl təzədirsə (20 dəq) təkrar endirmir."""
    OUT.parent.mkdir(exist_ok=True)
    if OUT.exists() and not force and time.time() - OUT.stat().st_mtime < max_age:
        return sum(1 for _ in OUT.open(encoding="utf-8"))
    srcs = load_sources()
    if not srcs:
        return 0
    with ThreadPoolExecutor(12) as ex:
        lists = list(ex.map(fetch_one, srcs))
    seen, out = set(), []
    for lst in lists:
        for u in lst:
            if u not in seen:
                seen.add(u)
                out.append(u)
    ok_src = sum(1 for l in lists if l)
    if out:
        random.shuffle(out)
        OUT.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"🌐 {ok_src}/{len(srcs)} mənbədən {len(out)} təzə proxy endirildi", flush=True)
    return len(out)


if __name__ == "__main__":
    fetch_all(force=True)
