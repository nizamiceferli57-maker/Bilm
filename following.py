"""Bir TikTok hesabının izlədiyi (following) adamların siyahısını çıxarır. YALNIZ siyahısı AÇIQ olan hesablarda işləyir.

Necə işləyir: profil səhifəsindən secUid alınır, sonra TikTok-un veb "following" sorğusu səhifə-səhifə oxunur.
Qeyd: TikTok bu sorğunu vaxtaşırı imza (X-Bogus) ilə qoruyur. Belə olanda boş cavab gəlir; bot bunu "gizli/bloklu" kimi bildirir,
bu halda siyahını əl ilə .txt kimi göndərə bilərsiniz (/istifadeci_elave).
"""
import json
import random
import re
import time

from curl_cffi import requests as cffi

_DATA = re.compile(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)
_SIGI = re.compile(r'<script id="SIGI_STATE"[^>]*>(.*?)</script>', re.S)


def _profile(sess, username: str, proxy: str):
    r = sess.get(f"https://www.tiktok.com/@{username}", timeout=15,
                 proxies={"http": proxy, "https": proxy} if proxy else None)
    if r.status_code in (403, 429):
        return "blocked", None
    m = _DATA.search(r.text)
    if m:
        scope = json.loads(m.group(1)).get("__DEFAULT_SCOPE__", {})
        det = scope.get("webapp.user-detail") or {}
        if det.get("statusCode") in (10221, 10202, 10201) or not det.get("userInfo"):
            return "notfound", None
        return "ok", det["userInfo"]
    m = _SIGI.search(r.text)
    if m:
        users = (json.loads(m.group(1)).get("UserModule") or {}).get("users") or {}
        stats = (json.loads(m.group(1)).get("UserModule") or {}).get("stats") or {}
        for k, u in users.items():
            if k.lower() == username.lower():
                return "ok", {"user": u, "stats": stats.get(k, {})}
        return "notfound", None
    return "blocked", None


def _fetch_with(proxy: str, username: str, max_names: int) -> dict:
    sess = cffi.Session(impersonate="chrome")
    st, info = _profile(sess, username, proxy)
    if st != "ok":
        return {"status": st, "names": []}
    user, stats = info["user"], info.get("stats", {})
    total = int(stats.get("followingCount") or 0)
    if user.get("privateAccount"):
        return {"status": "private", "names": [], "total": total}
    if total == 0:
        return {"status": "ok", "names": [], "total": 0}
    sec = user.get("secUid", "")
    if not sec:
        return {"status": "error", "names": [], "total": total}
    names, seen, cursor, empty = [], set(), 0, 0
    priv_n, unknown_n = 0, 0          # gizli (private) hesablar atılır, açıq olanlar saxlanır
    device = "".join(random.choice("0123456789") for _ in range(19))
    while len(names) < max_names:
        params = {
            "WebIdLastTime": str(int(time.time())), "aid": "1988", "app_language": "en", "app_name": "tiktok_web",
            "browser_language": "en-US", "browser_name": "Mozilla", "browser_online": "true", "browser_platform": "Linux x86_64",
            "channel": "tiktok_web", "cookie_enabled": "true", "count": "30", "data_collection_enabled": "true",
            "device_id": device, "device_platform": "web_pc", "from_page": "user", "maxCursor": "0",
            "minCursor": str(cursor), "scene": "21", "secUid": sec, "user_is_login": "false", "webcast_language": "en",
        }
        r = sess.get("https://www.tiktok.com/api/user/list/", params=params, timeout=15,
                     headers={"Referer": f"https://www.tiktok.com/@{username}"},
                     proxies={"http": proxy, "https": proxy} if proxy else None)
        if r.status_code in (403, 429):
            return {"status": "blocked", "names": names, "total": total}
        try:
            d = r.json()
        except ValueError:
            return {"status": "blocked", "names": names, "total": total}
        page = []
        for it in (d.get("userList") or []):
            u = it.get("user") or {}
            n = u.get("uniqueId")
            if not n:
                continue
            page.append(n)
            if n.lower() in seen:
                continue
            seen.add(n.lower())
            if u.get("privateAccount") or u.get("secret"):
                priv_n += 1                       # gizli hesab: siyahıya salınmır
                continue
            if "privateAccount" not in u and "secret" not in u:
                unknown_n += 1                    # TikTok bu sahəni göndərməyib: saxlanır
            names.append(n)
        if not page:
            empty += 1
        if not d.get("hasMore") or empty >= 2:
            break
        cursor = int(d.get("minCursor") or 0)
        time.sleep(0.4)
    if not names and not priv_n:
        return {"status": "hidden", "names": [], "total": total}      # saylar var, siyahı boş: gizlidir və ya imza tələb olunub
    return {"status": "ok", "names": names, "total": total, "private_n": priv_n, "unknown_n": unknown_n}


def fetch_following(username: str, proxies: list[str], max_names: int = 5000) -> dict:
    """{'status': ok|private|hidden|notfound|blocked|error, 'names': [...], 'total': int}. Öz IP əvvəl, sonra proxy-lər."""
    username = username.strip().lstrip("@")
    tries = [""] + random.sample(proxies, min(5, len(proxies)))
    best = {"status": "error", "names": []}
    for px in tries:
        try:
            res = _fetch_with(px, username, max_names)
        except Exception:
            continue
        if res["status"] in ("ok", "private", "notfound"):
            return res
        if len(res.get("names", [])) > len(best.get("names", [])) or best["status"] == "error":
            best = res
    return best
