"""Proxy idxalı: müxtəlif formatlı faylları (ip:port:user:pass, CSV, scheme://...) proxies.txt-ə çevirib BİRLƏŞDİRİR.
Yoxlama YOXDUR (istifadəçi artıq yoxlayıb). Təkrarlar atılır, mövcud proxies.txt silinmir.

İstifadə:  python proxy_import.py [fayl1 fayl2 ...]
Fayl verilməsə: Downloads qovluğundakı proxyscrape_premium_http_proxies.txt, Free_Proxy_List.txt və
bot qovluğundakı proxies_import*.txt faylları götürülür.
"""
import csv
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_IP = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _ok_hp(host: str, port: str) -> bool:
    return bool(_IP.match(host)) and port.isdigit() and 1 <= int(port) <= 65535


def _scheme(p: str) -> str:
    p = (p or "").strip().lower()
    if p.startswith("socks5"):
        return "socks5"
    if p.startswith("socks4"):
        return "socks4"
    return "http"        # http və https (CONNECT) → http://


def parse_text(text: str) -> list[str]:
    out: list[str] = []
    text = text.lstrip("\ufeff")
    first = text.splitlines()[0].lower() if text.strip() else ""
    if "ip" in first and "port" in first and "," in first:          # CSV (Free_Proxy_List)
        for r in csv.DictReader(io.StringIO(text)):
            host, port = (r.get("ip") or "").strip(), (r.get("port") or "").strip()
            if _ok_hp(host, port):
                out.append(f"{_scheme(r.get('protocols', ''))}://{host}:{port}")
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:                                           # scheme://[user:pass@]host:port
            sch, rest = line.split("://", 1)
            hp = rest.rsplit("@", 1)[-1]
            if ":" in hp and _ok_hp(*hp.split(":", 1)):
                out.append(f"{_scheme(sch)}://{rest}")
            continue
        parts = line.split(":")
        if len(parts) == 4 and _ok_hp(parts[0], parts[1]):          # ip:port:user:pass (proxyscrape premium)
            out.append(f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}")
        elif len(parts) == 2 and _ok_hp(*parts):                    # ip:port
            out.append(f"http://{parts[0]}:{parts[1]}")
    return out


def candidates(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(a).expanduser() for a in argv]
    homes = [Path.home() / "storage" / "downloads", Path.home() / "storage" / "shared" / "Download", Path("/sdcard/Download"), Path.home() / "Downloads"]
    names = ["Free_Proxy_List.txt", "http.txt", "get.txt", "data.txt", "proxy.txt", "proxies.txt"]      # Downloads-a atdığın proxy faylları avtomatik götürülür
    found = [h / n for h in homes for n in names if (h / n).exists()]
    found += sorted(ROOT.glob("proxies_import*.txt"))
    return found


def main() -> None:
    dest = ROOT / "proxies.txt"
    have = [l.strip() for l in dest.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")] if dest.exists() else []
    seen = {h.rsplit("@", 1)[-1].split("://")[-1] for h in have}     # host:port görə təkrarsız
    added, total_in = 0, 0
    for f in candidates(sys.argv[1:]):
        if not f.exists():
            print(f"  ⚠ tapılmadı: {f}")
            continue
        items = parse_text(f.read_text(encoding="utf-8", errors="ignore"))
        n = 0
        for p in items:
            key = p.rsplit("@", 1)[-1].split("://")[-1]
            if key in seen:
                continue
            seen.add(key)
            have.append(p)
            n += 1
        total_in += len(items)
        added += n
        print(f"  ✅ {f.name}: {len(items)} proxy oxundu, {n} yeni əlavə olundu")
    dest.write_text("\n".join(have) + "\n", encoding="utf-8")
    prem = sum(1 for h in have if "@" in h)
    print(f"=== proxies.txt: cəmi {len(have)} proxy (premium {prem}, pulsuz {len(have) - prem}) | bu dəfə +{added} ===")


if __name__ == "__main__":
    main()
