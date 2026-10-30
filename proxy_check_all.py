"""Böyük proxy siyahısını (76 min sətir) yoxlayır, işləyənləri MÖVCUD proxies.txt ilə birləşdirib yazır.

İstifadə (botun qovluğunda, venv aktiv):  python proxy_check_all.py data.txt [saxla=600]

- Hər IP yoxlanır. Bir IP-nin çox portu varsa (port-skan zibili) ən məşhur 30 port sınanır, ilk işləyəndə dayanır.
- 1-ci mərhələ: TikTok-a çıxır? 2-ci mərhələ: ttwid verirmi? (botun real ehtiyacı)
- Nəticə proxies.txt-ə (köhnə proxy-lərlə birləşmiş, IP-yə görə təkrarsız) yazılır.
"""
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests

SRC = sys.argv[1] if len(sys.argv) > 1 else "data.txt"
MAX_KEEP = int(sys.argv[2]) if len(sys.argv) > 2 else 600
OUT = os.environ.get("PROXY_OUT") or "proxies.txt"      # nəticə faylı (proxy_auto.py results/proxies_good.txt verir)
PER_IP_CAP = 30
MAX_SEC = float(os.environ.get("PROXY_CHECK_MAX_SEC", "600"))     # 1-ci mərhələyə max vaxt (qalanlar növbəti başlanğıcda davam edir)
TESTED = os.environ.get("PROXY_TESTED", "")                      # yoxlanmış host-ların yazıldığı fayl (təkrar yoxlanmasın)
T0 = time.monotonic()
STOP = threading.Event()
TIMEOUT = 7
THREADS = int(os.environ.get("PROXY_THREADS", "200"))
FIRST_N = int(os.environ.get("PROXY_FIRST_N", "0"))      # faylın ilk N sətri (təzə + əvvəlki yaxşılar) birinci yoxlanır
BAD_PORTS = {21, 22, 23, 25, 110, 143, 465, 587, 993, 995}
GOOD = {"http": {8080, 80, 3128, 3129, 8888, 8081, 8082, 8085, 999, 8089, 443, 3125},
        "https": {443, 8443, 8080, 3128},
        "socks4": {4145, 1080, 4153, 9050, 10808},
        "socks5": {1080, 1081, 4145, 10808, 9050, 1088, 8080, 8888}}


def parse(line):
    line = line.strip()
    if not line or line.startswith("#") or "://" not in line:
        return None
    scheme, rest = line.split("://", 1)
    if rest.count(":") != 1:
        return None
    host, port = rest.split(":")
    if not port.isdigit() or not host.replace(".", "").isdigit():
        return None
    port = int(port)
    if host.startswith(("0.", "127.", "10.", "192.168.")) or port in BAD_PORTS or not (1 <= port <= 65535):
        return None
    return scheme, host, port


def fetch(url):
    t = time.monotonic()
    try:
        r = requests.get("https://www.tiktok.com/robots.txt", proxies={"http": url, "https": url},
                         timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and "User-agent" in r.text:
            return time.monotonic() - t
    except Exception:
        pass
    return None


def ttwid_ok(url):
    for _ in range(3):
        try:
            r = requests.get("https://www.tiktok.com/@tiktok", impersonate="chrome", timeout=12,
                             proxies={"http": url, "https": url})
            if r.status_code == 200 and r.cookies.get("ttwid"):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


STATE = {"done": 0, "found": 0}
LOCK = threading.Lock()


def test_host(item):
    host, ports = item
    if STOP.is_set():
        return None
    result = None
    for scheme, port in ports:
        url = f"{scheme}://{host}:{port}"
        dt = fetch(url)
        if dt is not None:
            result = (dt, url, host)
            break
    with LOCK:
        STATE["done"] += 1
        STATE.setdefault("tested", []).append(host)
        if result:
            STATE["found"] += 1
            with open("proxy_stage1_ok.txt", "a", encoding="utf-8") as f:
                f.write(f"{result[0]:.2f} {result[1]}\n")
        if STATE["found"] >= MAX_KEEP * 2 or time.monotonic() - T0 > MAX_SEC:
            if not STOP.is_set():
                print(f"  ⏱ 1-ci mərhələ dayandı (işləyən {STATE['found']}, vaxt {time.monotonic() - T0:.0f}s); qalanlar növbəti dəfə yoxlanacaq", flush=True)
            STOP.set()
    return result


def main():
    groups, seen, prio = {}, set(), set()
    for i, line in enumerate(open(SRC, encoding="utf-8", errors="ignore")):
        p = parse(line)
        if p and p not in seen:
            seen.add(p)
            groups.setdefault(p[1], []).append((p[0], p[2]))
            if i < FIRST_N:
                prio.add(p[1])
    items = []
    for host, lst in groups.items():
        lst.sort(key=lambda sp: (0 if sp[1] in GOOD.get(sp[0], ()) else 1, 0 if sp[0] in ("http", "https") else 1))
        items.append((host, lst[:PER_IP_CAP]))
    random.shuffle(items)                                                   # vaxt qurtarsa nümunə təsadüfi olsun
    items.sort(key=lambda it: (0 if it[0] in prio else 1, 0 if it[1][0][1] in GOOD.get(it[1][0][0], ()) else 1))   # əvvəl təzə/yaxşılar, sonra tipik portlar
    total = len(items)
    attempts = sum(len(l) for _, l in items)
    print(f"{total} ayrı IP, ən çox {attempts} cəhd yoxlanır...", flush=True)
    open("proxy_stage1_ok.txt", "w").close()

    stop = threading.Event()

    def progress():
        while not stop.wait(30):
            print(f"  yoxlanıb {STATE['done']}/{total} | işləyən {STATE['found']}", flush=True)

    threading.Thread(target=progress, daemon=True).start()
    with ThreadPoolExecutor(THREADS) as ex:
        res = [r for r in ex.map(test_host, items) if r]
    stop.set()
    if TESTED:
        with open(TESTED, "a", encoding="utf-8") as f:
            f.write("\n".join(STATE.get("tested", [])) + "\n")
    if os.environ.get("PROXY_RUN_LOG"):
        with open(os.environ["PROXY_RUN_LOG"], "w", encoding="utf-8") as f:       # YALNIZ bu dəfə yoxlanmış hostlar (yaddaşda "uğursuz" saymaq üçün)
            f.write("\n".join(STATE.get("tested", [])) + "\n")
    res.sort()
    print(f"1-ci mərhələ: {len(res)}/{total} IP TikTok-a çıxır. ttwid yoxlanır...", flush=True)

    pool = res[:MAX_KEEP * 3]
    with ThreadPoolExecutor(min(40, THREADS)) as ex:
        flags = list(ex.map(lambda c: ttwid_ok(c[1]), pool))
    good = [c for c, ok in zip(pool, flags) if ok]
    print(f"2-ci mərhələ: {len(good)}/{len(pool)} ttwid verir.", flush=True)
    new = good[:MAX_KEEP] if good else res[:MAX_KEEP]
    if not good and new:
        print("⚠ Heç biri ttwid vermədi, 1-ci testdən keçənlər əlavə olunur.")

    merged, hosts = [], set()
    try:
        old = [l.strip() for l in open(OUT, encoding="utf-8") if l.strip()]
    except OSError:
        old = []
    for u in old + [u for _, u, _ in new]:
        h = u.split("://", 1)[-1].rsplit(":", 1)[0]
        if h not in hosts:
            hosts.add(h)
            merged.append(u)
    open(OUT, "w", encoding="utf-8").write("\n".join(merged) + ("\n" if merged else ""))
    print(f"✅ yeni: {len(new)}, köhnə: {len(old)}, cəmi {OUT}: {len(merged)}", flush=True)
    for dt, u, _ in new[:10]:
        print(f"   {dt:4.1f}s  {u}")


if __name__ == "__main__":
    main()
