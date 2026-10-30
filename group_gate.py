"""Qrup/kanal qapısı SİLİNİB: qrup/kanala bildirişlər həmişə açıqdır (artıq 'Qrupa at / Hazırla' sualı və qapı yoxdur).

Bu modul yalnız köhnə importlar işləsin deyə qalıb. Qrupa yalnız CANLI yayım bildirişləri gedir:
sandıqlı canlı, (kanal üçün açıqsa) Lucky Bag və yeni canlıların qısa siyahısı (group_feed.py).
"""


def is_open() -> bool:
    return True


def is_gated() -> bool:
    return False


def open_gate() -> None:
    return None


def close_gate(prepare: bool = True) -> None:
    return None           # qapını bağlamaq mümkün deyil


def drop() -> None:
    return None


def snapshot() -> dict:
    return {"open": True, "asked": True, "dropped": 0, "mode": "send"}


def status_line() -> str:
    return "📤 Qrup: <b>AÇIQ</b> — yalnız canlı yayım bildirişləri gedir"
