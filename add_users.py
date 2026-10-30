"""Yeni ad siyahılarını users.txt-yə birləşdirir (təkrarları və yararsız adları atır).

İstifadə:  python add_users.py siyahi1.txt siyahi2.txt ...
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USERS = ROOT / "users.txt"
VALID = re.compile(r"^[a-z0-9._]{1,24}$")


def read_names(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        name = line.strip().lstrip("@").strip()
        if name:
            out.append(name)
    return out


def main(files: list[str]) -> None:
    import os
    if os.environ.get("USERS_LOCKED") == "1":
        print("ad siyahısı kilidlidir (USERS_LOCKED=1): əlavə edilmədi"); return
    if not files:
        sys.exit("İstifadə: python add_users.py siyahi1.txt [siyahi2.txt ...]")
    existing = read_names(USERS) if USERS.exists() else []
    seen = {n.lower() for n in existing}
    added, dup, bad = [], 0, []
    for f in files:
        p = Path(f).expanduser()
        if not p.exists():
            print(f"⚠ tapılmadı: {p}")
            continue
        for name in read_names(p):
            key = name.lower()
            if key in seen:
                dup += 1
            elif not VALID.match(key):
                bad.append(name)
            else:
                seen.add(key)
                added.append(name)
    if added:
        with open(USERS, "a", encoding="utf-8") as fh:
            if USERS.exists() and USERS.stat().st_size and not USERS.read_text(encoding="utf-8").endswith("\n"):
                fh.write("\n")
            fh.write("\n".join(added) + "\n")
    print(f"✅ yeni əlavə olundu: {len(added)} | təkrar (atıldı): {dup} | yararsız (atıldı): {len(bad)}")
    print(f"users.txt indi: {len(existing) + len(added)} ad")
    if bad:
        print("yararsız nümunələr:", bad[:10])


if __name__ == "__main__":
    main(sys.argv[1:])
