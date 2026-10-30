"""users.txt-yə yeni adlar əlavə etmək (add_users.py və Telegram /istifadeci_elave üçün ortaq)."""
import os
import re

import config

VALID = re.compile(r"^[a-z0-9._]{1,24}$")


def parse_names(text: str) -> list[str]:
    """Mətndən adları çıxar: sətir/boşluq/vergül ilə ayrılmış, @ və t.me/tiktok linkləri təmizlənir."""
    out = []
    for tok in re.split(r"[\s,;]+", text):
        tok = tok.strip().strip("<>()[]")
        if not tok:
            continue
        m = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", tok)
        if m:
            tok = m.group(1)
        tok = tok.lstrip("@").strip()
        if tok:
            out.append(tok)
    return out


def add_names(names: list[str]) -> dict:
    users = config.USERS_FILE
    existing = []
    if users.exists():
        existing = [l.strip().lstrip("@") for l in users.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
    if os.environ.get("USERS_LOCKED") == "1":   # siyahı kilidlidir: yalnız users_base.txt
        return {"new_names": [], "added": 0, "dup": 0, "bad": [], "total": len(existing)}
    seen = {n.lower() for n in existing}
    added, dup, bad = [], 0, []
    for name in names:
        key = name.lower()
        if key in seen:
            dup += 1
        elif not VALID.match(key):
            bad.append(name)
        else:
            seen.add(key)
            added.append(name)
    if added:
        with open(users, "a", encoding="utf-8") as fh:
            if users.exists() and users.stat().st_size and not users.read_bytes().endswith(b"\n"):
                fh.write("\n")
            fh.write("\n".join(added) + "\n")
    return {"new_names": added, "added": len(added), "dup": dup, "bad": bad, "total": len(existing) + len(added)}
