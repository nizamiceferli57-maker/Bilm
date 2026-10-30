"""Pulsuz proxy avtomatikası (baslat_hamisi.sh və bot özü çağırır).

  1) Ödənişli/loginli (user:parol@) proxy-ləri silir (premium bitib). Saxlamaq üçün:  KEEP_LOGIN_PROXIES=1
  2) Təzə pulsuz siyahıları internetdən endirir (proxy_sources.txt → proxy_lists/auto_fetched.txt)
  3) Yoxlanmamışları + əvvəlki yaxşıları TikTok-a qarşı yoxlayır (vaxt büdcəsi ilə, ən təzələr əvvəl)
  4) İşləyənlər results/proxies_memory.json-da YADDA saxlanır (3 ardıcıl yoxlamada keçməyən/3 gün işləməyən silinir); proxies.txt = yaddaşdakı pulsuzlar

Rejimlər:  python proxy_auto.py            (başlanğıc)
           python proxy_auto.py --refresh  (bot işləyərkən arxa fon: yüngül, qısa büdcə)
           --force (hamısını təzədən)   --nofetch (endirmə)
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import proxy_fetch  # noqa: E402
import proxy_import  # noqa: E402

DEAD_USERS = ("onn1dz2gr7ma",)
KEEP_LOGIN = os.environ.get("KEEP_LOGIN_PROXIES", "0").strip().lower() in ("1", "true", "yes")
GOOD = ROOT / "results" / "proxies_good.txt"
TOCHECK = ROOT / "results" / "_to_check.txt"
STAMP = ROOT / "results" / ".proxy_check_stamp"
TESTED = ROOT / "results" / "proxies_tested.txt"
FETCHED = ROOT / "proxy_lists" / "auto_fetched.txt"
MEMORY = ROOT / "results" / "proxies_memory.json"          # işləyən proxy-lərin YADDAŞI: {url: {ok, fail, last_ok, proven}}
PROVEN = ROOT / "results" / "proxies_proven.json"          # ordu real qoşulmada işlətdiyi proxy-lər (fleet yazır): {url: vaxt}
RUNLOG = ROOT / "results" / "_tested_this_run.txt"
MAX_FAILS = 3                                              # ardıcıl 3 yoxlamada keçməyən proxy yaddaşdan silinir
MAX_IDLE = 3 * 24 * 3600                                   # 3 gün heç işləməyən silinir
MEM_LIMIT = 800
ENOUGH = 300                       # bu qədər yaxşı proxy varsa yeni siyahıya baxmırıq (yalnız köhnələr təzədən sınanır)
STATIC_CAP = 8000                  # statik (köhnə) siyahılardan bir yoxlamada ən çox bu qədər
TESTED_TTL = 24 * 3600             # yoxlanmış host yaddaşı 24 saatdan sonra silinir (pulsuz proxy-lər dirilə bilər)
MAX_AGE = 12 * 3600
SOCKS_PORTS = {1080, 1081, 1085, 1088, 4145, 4153, 5678, 9000, 9050, 10808, 10800, 20000, 40001}
KEEP = "400"


def read(f: Path) -> list[str]:
    try:
        return [l.strip() for l in f.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip() and not l.startswith("#")]
    except OSError:
        return []


def write(f: Path, lines: list[str]) -> None:
    f.parent.mkdir(exist_ok=True)
    f.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def purge() -> None:
    removed = 0
    for f in [ROOT / "proxies.txt", GOOD, *ROOT.glob("proxies_import*.txt"), ROOT / "results" / "dead_proxies.txt"]:
        if not f.exists():
            continue
        lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        keep = [l for l in lines if not any(u in l for u in DEAD_USERS) and (KEEP_LOGIN or "@" not in l)]
        if len(keep) != len(lines):
            removed += len(lines) - len(keep)
            write(f, [l for l in keep if l.strip()])
    if removed:
        print(f"🗑 premium/loginli proxy silindi: {removed} sətir", flush=True)


def key(u: str) -> str:
    return u.split("://", 1)[-1].rsplit("@", 1)[-1]


def host_of(u: str) -> str:
    return key(u).rsplit(":", 1)[0]


def sources() -> tuple[list[str], set]:
    """(namizədlər, təzə_endirilənlər). Təzələr əvvəldə, sonra statik siyahılar."""
    files = [FETCHED, ROOT / "get.txt", ROOT / "proxies_import_free.txt"]
    # kiçik siyahılar əvvəl yoxlanır (90 min sətirlik data.txt kiçik siyahıları batırmasın); Downloads-dakı fayllar ən əvvəl
    files += sorted((f for f in (ROOT / "proxy_lists").glob("*.txt") if f.name != FETCHED.name), key=lambda f: f.stat().st_size)
    files = [f for f in proxy_import.candidates([]) if f.name in ("Free_Proxy_List.txt", "http.txt", "get.txt", "data.txt", "proxy.txt", "proxies.txt")] + files
    seen, out, fresh = set(), [], set()

    def add(p: str, is_fresh: bool) -> None:
        if "@" not in p and p not in seen:
            seen.add(p)
            out.append(p)
            if is_fresh:
                fresh.add(p)

    for f in files:
        if not f.exists():
            continue
        is_fresh = f.name == FETCHED.name
        text = f.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            line = line.strip()
            if line and "://" not in line and line.count(":") == 1 and not line.startswith("#"):
                host, port = line.split(":")                  # scheme-siz ip:port → port tipinə görə 2-3 ehtimal
                if port.isdigit():
                    for sch in (("socks5", "socks4", "http") if int(port) in SOCKS_PORTS else ("http", "socks5")):
                        add(f"{sch}://{host}:{port}", is_fresh)
        for p in proxy_import.parse_text(text):
            add(p, is_fresh)
    return out, fresh


def load_mem() -> dict:
    try:
        return json.loads(MEMORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_memory(passing: set, ran_hosts: set, good_old: list) -> None:
    """Yoxlamadan keçənlər yadda saxlanır; əvvəl yaxşı olub bu dəfə yoxlanıb keçməyənlər 'uğursuz' sayılır (3 dəfə → silinir).
    Real qoşulmada işləyənlər (proven) həmişə saxlanır."""
    now = time.time()
    mem = load_mem()
    for u in good_old:                                   # köhnə yaxşı siyahı yaddaşa daxil edilir (ilk dəfə)
        mem.setdefault(u, {"ok": 1, "fail": 0, "last_ok": now})
    for u in passing:
        e = mem.setdefault(u, {"ok": 0, "fail": 0, "last_ok": now})
        e["ok"] = e.get("ok", 0) + 1
        e["fail"] = 0
        e["last_ok"] = now
    for u, e in mem.items():
        if u not in passing and host_of(u) in ran_hosts:
            e["fail"] = e.get("fail", 0) + 1
    try:
        for u, ts in json.loads(PROVEN.read_text(encoding="utf-8")).items():
            e = mem.setdefault(u, {"ok": 0, "fail": 0, "last_ok": ts})
            e["fail"] = 0
            e["last_ok"] = max(e.get("last_ok", 0), float(ts))
            e["proven"] = True
    except (OSError, ValueError, AttributeError):
        pass
    for u in list(mem):
        e = mem[u]
        recent_proof = e.get("proven") and now - e.get("last_ok", 0) < 24 * 3600
        if (e.get("fail", 0) >= MAX_FAILS and not recent_proof) or now - e.get("last_ok", 0) > MAX_IDLE:
            del mem[u]
    keep = sorted(mem, key=lambda u: (not mem[u].get("proven"), -mem[u].get("last_ok", 0)))[:MEM_LIMIT]
    mem = {u: mem[u] for u in keep}
    MEMORY.parent.mkdir(exist_ok=True)
    MEMORY.write_text(json.dumps(mem), encoding="utf-8")
    write(GOOD, keep)                                    # GOOD = yaddaş (ordu və rebuild bunu oxuyur)


def rebuild() -> None:
    auth = [l for l in read(ROOT / "proxies.txt") if "@" in l] if KEEP_LOGIN else []
    good = read(GOOD)
    seen, out = set(), []
    for u in auth + good:
        k = key(u)
        if k not in seen:
            seen.add(k)
            out.append(u)
    write(ROOT / "proxies.txt", out)
    print(f"=== proxies.txt: {len(out)} proxy ({len(auth)} loginli, {len(good)} yoxlanmış pulsuz) ===", flush=True)


def main() -> None:
    argv = sys.argv[1:]
    force, refresh, nofetch = "--force" in argv, "--refresh" in argv, "--nofetch" in argv
    purge()
    if not nofetch:
        try:
            proxy_fetch.fetch_all(force=force)
        except Exception as exc:
            print(f"⚠ endirmə xətası: {type(exc).__name__}: {exc}", flush=True)
    if TESTED.exists() and (force or time.time() - TESTED.stat().st_mtime > TESTED_TTL):
        TESTED.unlink()
    src, fresh = sources()
    good_old = read(GOOD)
    tested = set(read(TESTED))
    new = [u for u in src if host_of(u) not in tested]
    new_fresh = [u for u in new if u in fresh]
    new_static = [u for u in new if u not in fresh][:STATIC_CAP]
    if len(good_old) >= ENOUGH and not force:
        new_fresh, new_static = [], []              # kifayət qədər yaxşı var: yeni siyahıya vaxt itirmə
    try:
        age = time.time() - float(STAMP.read_text().split()[-1])
    except (OSError, ValueError, IndexError):
        age = 10 ** 9
    limit = 20 * 60 if refresh else MAX_AGE
    if not new_fresh and not new_static and ((age < limit and not force) or not good_old):
        print(f"✔ yoxlanacaq yeni proxy yoxdur / yoxlama təzədir ({len(good_old)} yaxşı proxy), keçilir", flush=True)
        update_memory(set(), set(), good_old)           # yalnız real qoşulmada işləyənləri (proven) yaddaşa qat
        rebuild()
        return
    prio = new_fresh + good_old                      # təzələr və köhnə yaxşılar birinci yoxlanır
    tocheck = list(dict.fromkeys(prio + new_static))
    write(TOCHECK, tocheck)
    budget = os.environ.get("PROXY_CHECK_MAX_SEC") or ("300" if refresh else "420")
    print(f"🔎 {len(new_fresh)} təzə + {len(new_static)} köhnə siyahıdan + {len(good_old)} əvvəlki yaxşı proxy yoxlanır (max ~{int(float(budget)) // 60} dəq)...", flush=True)
    NEW = GOOD.with_suffix(".new")                    # nəticə əvvəl müvəqqəti fayla yazılır: yarımçıq qalsa (Ctrl+C və s.) köhnə yaxşı siyahı İTMİR
    NEW.unlink(missing_ok=True)
    RUNLOG.unlink(missing_ok=True)
    env = dict(os.environ, PROXY_OUT=str(NEW), PROXY_TESTED=str(TESTED), PROXY_RUN_LOG=str(RUNLOG), PROXY_FIRST_N=str(len(prio)),
               PROXY_CHECK_MAX_SEC=str(budget), PROXY_THREADS=os.environ.get("PROXY_THREADS") or ("80" if refresh else "200"))
    try:
        r = subprocess.run([sys.executable, str(ROOT / "proxy_check_all.py"), str(TOCHECK), KEEP], cwd=ROOT, env=env)
        ok = r.returncode == 0 and NEW.exists() and NEW.stat().st_size > 0
    except KeyboardInterrupt:
        ok = False
        print("⏹ yoxlama dayandırıldı, əvvəlki yaxşı siyahı saxlanır")
    passing = set(read(NEW)) if ok else set()
    ran = set(read(RUNLOG)) if RUNLOG.exists() else set()
    NEW.unlink(missing_ok=True)
    if ok or ran:
        update_memory(passing, ran if ran else set(), good_old)
        if ok:
            STAMP.write_text(f"{time.time()}")
    else:
        print("⚠ yoxlama alınmadı, əvvəlki yaxşı proxy yaddaşı saxlanır")
    rebuild()


if __name__ == "__main__":
    main()
