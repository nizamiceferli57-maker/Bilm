"""Konfiqurasiya: .env faylından və ya mühit dəyişənlərindən oxunur."""
import itertools
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Admin(lər): yalnız bunlara cavab verilir. Boşdursa TELEGRAM_CHAT_ID istifadə olunur. Vergüllə bir neçə: 123,456
ADMIN_IDS = [
    int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", "") or TELEGRAM_CHAT_ID) if x.lstrip("-").isdigit()
]
# Başlanğıcda kanalları .env-dən də vermək olar: CHANNEL_IDS=@kanal1,-1001234567890 (əmrlə də əlavə olunur)
CHANNEL_IDS = [x for x in re.split(r"[,\s]+", os.environ.get("CHANNEL_IDS", "")) if x]
PANEL_REFRESH = _int("PANEL_REFRESH", 15)              # canlı panelin yenilənmə intervalı (saniyə)
AUTO_START = os.environ.get("AUTO_START", "1").strip().lower() not in ("0", "false", "no")  # 0: bot gözləyir, /baslat ilə başlayır
EXIT_WHEN_DONE = os.environ.get("EXIT_WHEN_DONE", "0").strip().lower() in ("1", "true", "yes")  # 1: bitəndə proses çıxsın
SETTINGS_FILE = ROOT / "results" / "settings.json"
RATIO_GREAT = _float("RATIO_GREAT", 2.0)               # bu ratio və yuxarı: 💎 ƏLA
RATIO_GOOD = _float("RATIO_GOOD", 1.0)                 # bu ratio və yuxarı: 🔥 YAXŞI
TELEGRAM_REPORT_EVERY = _int("TELEGRAM_REPORT_EVERY", 300)  # saniyə
CONSOLE_STATUS_EVERY = _int("CONSOLE_STATUS_EVERY", 10)     # konsol sayğacı (saniyə)

# Fayllar
USERS_FILE = ROOT / os.environ.get("USERS_FILE", "users.txt")
RESULTS_DIR = ROOT / "results"
# Default: hər başlanğıcda siyahı sıfırdan yoxlanır. Qaldığı yerdən davam üçün: python main.py --resume (və ya SCAN_RESUME=1)
RESUME = ("--resume" in sys.argv) or os.environ.get("SCAN_RESUME", "0").strip().lower() in ("1", "true", "yes")

# İşçi 1 (skaner): canlı yoxlaması
BLOCK_BACKOFF = _int("BLOCK_BACKOFF", 60)       # 403/429 olanda gözləmə (saniyə)

# İşçi 2 (sandıq): canlıya qoşulub dinləmə
CHEST_CONCURRENCY = _int("CHEST_CONCURRENCY", 3)
CHEST_LISTEN_SECONDS = _int("CHEST_LISTEN_SECONDS", 90)

QUICK_IDLE = _int("QUICK_IDLE", 20)                   # /yoxla ad: sandıq görünmürsə neçə saniyə sonra bitsin
# İstəyə bağlı: öz TikTok hesabınızın cookie-si (18+ yayımların otaq məlumatı üçün). Gizli saxlayın, heç kimə verməyin.
HOT_MAX = _int("HOT_MAX", 15)                         # sandıq çıxan yayımlardan neçəsində davamlı qalsın (0 = söndür)
HOT_HOURS = _float("HOT_HOURS", 3.0)                  # bir yayım ən çox neçə saat izlənsin
def _clean_cookie(raw: str, name: str) -> str:
    """'sessionid=abc; ...' və ya dırnaqlı yapışdırılmış dəyəri təmizləyir → yalnız qiymət."""
    raw = (raw or "").strip().strip('"').strip("'")
    m = re.search(rf"(?:^|[;\s]){re.escape(name)}=([^;\s]+)", raw)
    if m:
        return m.group(1)
    return raw.split(";")[0].strip()


SESSION_ID = _clean_cookie(os.environ.get("SESSION_ID", ""), "sessionid")
WS_PLATFORM = os.environ.get("WS_PLATFORM", "web").strip().lower()  # "mobile" = təcrübə (yalnız SESSION_ID ilə)
TT_TARGET_IDC = _clean_cookie(os.environ.get("TT_TARGET_IDC", ""), "tt-target-idc")

# TikTokLive sessiya ilə qoşulmanı YALNIZ bu iki şərtlə icazə verir:
#   1) tt-target-idc cookie-si verilməlidir (məs. alisg, useast2a, maliva, eu-ttp2)
#   2) WHITELIST_AUTHENTICATED_SESSION_ID_HOST mühit dəyişəni imza serverinin host-una bərabər olmalıdır
# Əks halda hər qoşulma "BLOCKED"/"Target IDC must be passed" xətası ilə düşür (v10-da səbəb bu idi).
SESSION_PROBLEM = ""
if SESSION_ID and not TT_TARGET_IDC:
    SESSION_PROBLEM = ("SESSION_ID var, amma TT_TARGET_IDC boşdur → sessiya İSTİFADƏ OLUNMUR (yoxsa bütün qoşulmalar düşərdi). "
                       "Brauzerdə tiktok.com → F12 → Application → Cookies → 'tt-target-idc' qiymətini .env-də TT_TARGET_IDC-ə yazın")
SESSION_OK = bool(SESSION_ID and TT_TARGET_IDC)
if SESSION_OK:
    # TikTokLive yoxlaması WebDefaults.tiktok_sign_url ilə müqayisə edir (defolt: api.eulerstream.com)
    os.environ["WHITELIST_AUTHENTICATED_SESSION_ID_HOST"] = "api.eulerstream.com"

MANUAL_WATCH_MIN = _int("MANUAL_WATCH_MIN", 10)       # /yoxla ad: canlıdırsa bu qədər dəqiqə yayımda qal (sonradan qoyulan sandığı tutmaq üçün; 0 = söndür)
CHEST_NOTIFY_MAX = _int("CHEST_NOTIFY_MAX", 5)        # bir yayımda Telegram-a maksimum neçə sandıq bildirişi

# Skaner gücləndirilməsi
SCAN_TIMEOUT = _float("SCAN_TIMEOUT", 12.0)            # bir sorğunun maksimum müddəti (saniyə)
SCAN_NAME_ATTEMPTS = _int("SCAN_NAME_ATTEMPTS", 3)     # bir ad üçün dərhal təkrar cəhd sayı
SCAN_RETRY_ROUNDS = _int("SCAN_RETRY_ROUNDS", 3)       # xəta/blok olan adlar üçün əlavə tam dövrlər
SCAN_ROUND_PAUSE = _int("SCAN_ROUND_PAUSE", 30)        # dövrlər arası gözləmə (saniyə)
BLOCK_BACKOFF_MAX = _int("BLOCK_BACKOFF_MAX", 600)     # ardıcıl bloklarda gözləmənin yuxarı həddi

# Euler Stream API (canlıları təsdiqləmək)
EULER_API_KEY = os.environ.get("EULER_API_KEY", "").strip()
EULER_BASE_URL = os.environ.get("EULER_BASE_URL", "https://api.eulerstream.com").rstrip("/")
EULER_TIMEOUT = _float("EULER_TIMEOUT", 15.0)
EULER_VERIFY_BUDGET = _int("EULER_VERIFY_BUDGET", 800)  # təsdiq üçün maksimum sorğu (günlük limitin qalanı sandıq işçilərinə)
EULER_CONCURRENCY = _int("EULER_CONCURRENCY", 3)
# TikTokLive (sandıq işçisi) imza sorğuları üçün eyni açarı istifadə etsin (limiti artırır)
if EULER_API_KEY:
    os.environ.setdefault("SIGN_API_KEY", EULER_API_KEY)
EULER_VERIFY_LIVE = os.environ.get("EULER_VERIFY_LIVE", "1").strip() not in ("0", "false", "no", "")

# Skaner sürəti: başda SCAN_START_WORKERS paralel, hər blokda 1 azalır (min 1)
SCAN_START_WORKERS = _int("SCAN_START_WORKERS", 1)   # loqda blok vermədən işləyən sürət: paralel 1
SCAN_RECOVER_AFTER = _int("SCAN_RECOVER_AFTER", 30)    # >0: bu qədər blok-suz sorğudan sonra paralel say 1 artsın (0 = söndürülü)
SCAN_SPACING = _float("SCAN_SPACING", 1.2)            # eyni kanalda sorğu başlanğıcları arası minimum fasilə (saniyə)
SCAN_WORKER_CAP = _int("SCAN_WORKER_CAP", 20)           # /surat ilə çıxıla biləcək maksimum paralel say (hər kanalda)
SCAN_MAX_SPACING = _float("SCAN_MAX_SPACING", 10.0)   # paralel 1 olanda da blok davam edərsə, fasilənin yuxarı həddi


def _load_proxies() -> list[str]:
    """PROXY_URL, PROXY_URLS (vergül/boşluqla) və proxies.txt (hər sətirdə bir proxy)."""
    items: list[str] = []
    single = os.environ.get("PROXY_URL", "").strip()
    if single:
        items.append(single)
    items += [p for p in re.split(r"[,\s]+", os.environ.get("PROXY_URLS", "")) if p]
    f = ROOT / os.environ.get("PROXIES_FILE", "proxies.txt")
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                items.append(line)
    dead: set[str] = set()
    df = ROOT / "results" / "dead_proxies.txt"      # əvvəlki işlərdə heç vaxt işləməyən pulsuz proxy-lər (avtomatik yazılır)
    if df.exists():
        dead = {l.strip() for l in df.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()}
    seen, out = set(), []
    for p in items:
        if p in dead and "@" not in p:               # ödənişli (login:parol@) proxy heç vaxt silinmir
            continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# Dəstəklənən sxemlər: http://, https://, socks5://, socks5h://
PROXIES = _load_proxies()
PROXY_URL = PROXIES[0] if PROXIES else ""  # geriyə uyğunluq
# PROXY_MODE=all (defolt): bütün proxy-lər BİRDƏN işləyir, canlılar və skan onlar arasında bölünür. Öz IP-yə toxunulmur.
# PROXY_MODE=fallback: ƏSAS = öz (birbaşa) IP. Proxy-lər (pulsuz da ola bilər) yalnız birbaşa IP bloklananda/dincələndə işə düşür,
#                               birbaşa IP bərpa olanda bot avtomatik ona qayıdır.
PROXY_MODE = os.environ.get("PROXY_MODE", "all").strip().lower()
PROXY_FALLBACK = bool(PROXIES) and PROXY_MODE != "all"
_chest_cycle = itertools.cycle(PROXIES) if PROXIES else None


def next_chest_proxy() -> str:
    """Sandıq işçiləri üçün proxy-ləri növbə ilə ver."""
    return next(_chest_cycle) if _chest_cycle else ""


# ---------------- ORDU (fleet): çoxlu işçi, yayım bitənə qədər qalıcı qoşulma ----------------
FLEET = os.environ.get("FLEET", "1").strip().lower() not in ("0", "false", "no")  # 1: yeni ordu rejimi
FLEET_PER_IP = _int("FLEET_PER_IP", 25)                  # bir IP (proxy) eyni anda neçə canlı saxlasın (pulsuz proxy)
FLEET_PER_IP_PREMIUM = _int("FLEET_PER_IP_PREMIUM", 150)  # login/parollu (premium) proxy: daha çox canlı saxlaya bilər
FLEET_PER_PROXY_CONNECT = _int("FLEET_PER_PROXY_CONNECT", 4)   # bir proxy üzərindən eyni anda neçə əl sıxma (qoşulma) gedə bilər
INCLUDE_DIRECT = os.environ.get("INCLUDE_DIRECT", "1").strip().lower() not in ("0", "false", "no")  # PROXY_MODE=all: telefonun öz IP-si də işçi olsun
FLEET_DIRECT_FIRST = os.environ.get("FLEET_DIRECT_FIRST", "1").strip().lower() not in ("0", "false", "no")   # 1: yeni canlı ƏVVƏL telefonun öz IP-sinə verilir (növbəsi qısadırsa), qalanı proxy-lərə
FLEET_DIRECT_CONNECT = _int("FLEET_DIRECT_CONNECT", 12)    # öz IP üzərindən eyni anda neçə əl sıxma
FLEET_DIRECT_SHARE = _int("FLEET_DIRECT_SHARE", 3000)       # proxy-lərlə birgə işləyəndə telefonun öz IP-sinin canlı payı
SCAN_MAX_PARALLEL = _int("SCAN_MAX_PARALLEL", 150)         # skan: eyni anda maksimum sorğu (telefon CPU-su üçün)
SCAN_PER_LANE = _int("SCAN_PER_LANE", 2)                 # skan: hər proxy-də eyni anda neçə yoxlama
EXECUTOR_THREADS = _int("EXECUTOR_THREADS", 500)         # skan/proxy sorğuları üçün thread sayı
VIEWERS_TIMEOUT = _float("VIEWERS_TIMEOUT", 4.0)         # sandıq mesajına canlıdakı say əlavə etmək üçün maksimum gözləmə (saniyə)
FLEET_CONNECT_PARALLEL = _int("FLEET_CONNECT_PARALLEL", 300)   # eyni anda neçə canlıya qoşulma cəhdi (əl sıxma); açılandan sonra yer boşalır
FLEET_TARGET_LIVES = _int("FLEET_TARGET_LIVES", 3000)   # minimum hədəf tutum (altında olsa xəbərdarlıq)
FLEET_MAX_LIVES = _int("FLEET_MAX_LIVES", 3000)          # ümumi yuxarı hədd
FLEET_START_DELAY = _float("FLEET_START_DELAY", 0.15)    # iki qoşulma arası fasilə (saniyə)
FLEET_PROXY_MAX_FAILS = _int("FLEET_PROXY_MAX_FAILS", 2) # ardıcıl bu qədər xətadan sonra proxy dincəlir
FLEET_DIRECT_START_DELAY = _float("FLEET_DIRECT_START_DELAY", 0.8)  # proxy YOXDURSA: iki qoşulma arası fasilə (saniyə), TikTok 403 verməsin
FLEET_DIRECT_MAX_FAILS = _int("FLEET_DIRECT_MAX_FAILS", 5)         # proxy YOXDURSA: ardıcıl bu qədər xətadan sonra qısa fasilə
FLEET_DIRECT_COOLDOWN = _int("FLEET_DIRECT_COOLDOWN", 45)           # proxy YOXDURSA: fasilə müddəti (saniyə)
PROXY_REFRESH_MIN = _int("PROXY_REFRESH_MIN", 30)   # bot işləyərkən pulsuz proxy-lər hər N dəqiqədən bir təzədən endirilib yoxlanılır və ordu-ya əlavə olunur (0 = söndür)
WATCH_EVERY = _int("WATCH_EVERY", 45)   # /izle siyahısındakı hesabların canlı olub-olmadığını yoxlama aralığı (saniyə)
FLEET_CONNECT_TIMEOUT = _float("FLEET_CONNECT_TIMEOUT", 20.0)  # canlıya qoşulma addımlarının (otaq yoxlaması, ttwid) timeout-u; yavaş proxy-lərə vaxt verir
FLEET_PROXY_COOLDOWN = _int("FLEET_PROXY_COOLDOWN", 600)
FLEET_BALANCE = os.environ.get("FLEET_BALANCE", "1").strip().lower() not in ("0", "false", "no")  # 1: canlılar proxy-lər arasında bərabər bölünür (ən az yüklü proxy seçilir)
TTWID_REUSE = _int("TTWID_REUSE", 60)                   # bir ttwid neçə qoşulma arasında bölüşülsün (hər IP üçün)
TTWID_ATTEMPTS = _int("TTWID_ATTEMPTS", 3)               # bir ttwid almaq üçün cəhd sayı (əvvəl 8 idi: IP-ni lazımsız yorurdu)
TTWID_RETRY_DELAY = _float("TTWID_RETRY_DELAY", 1.5)     # cəhdlər arası fasilə (saniyə)
TTWID_FAIL_COOLDOWN = _int("TTWID_FAIL_COOLDOWN", 20)    # ttwid alınmasa: bu qədər saniyə yeni sorğu atma (hər uğursuzluqda 2x, max 300)
FLEET_GIVEUP_AFTER = _int("FLEET_GIVEUP_AFTER", 6)       # ardıcıl bu qədər tez qopmadan sonra vaz keç
FLEET_ENDED_VOTES = _int("FLEET_ENDED_VOTES", 2)             # "yayım bitib" cavabı bu qədər dəfə təsdiqlənməyincə canlı atılmır
FLEET_DIRECT_QUEUE_MAX = _float("FLEET_DIRECT_QUEUE_MAX", 8.0)   # fallback: öz IP-nin qoşulma növbəsi bu qədər saniyədən uzundursa yeni canlı ehtiyat proxy-yə gedir
FLEET_REBALANCE_EVERY = _int("FLEET_REBALANCE_EVERY", 15)       # fallback: neçə saniyədən bir proxy-dəkiləri öz IP-yə qaytarmağa bax
FLEET_REBALANCE_BATCH = _int("FLEET_REBALANCE_BATCH", 6)        # hər dəfə ən çox neçə canlı geri köçürülsün
LUCKYBAG = os.environ.get("LUCKYBAG", "1").strip().lower() not in ("0", "false", "no")                  # Lucky Bag (hədiyyə çantası) hadisələrini tut
LUCKYBAG_COOLDOWN = float(os.environ.get("LUCKYBAG_COOLDOWN", "600"))      # eyni yayımçı üçün Lucky Bag xəbəri arası minimum saniyə
LUCKYBAG_AUTOWATCH = os.environ.get("LUCKYBAG_AUTOWATCH", "1").strip().lower() not in ("0", "false", "no")   # Lucky Bag göstərən yayımçı avtomatik izləmə siyahısına düşür (yayımı bitsə də, qayıdanda qoşulur)
LUCKYBAG_DISCOVER = os.environ.get("LUCKYBAG_DISCOVER", "1").strip().lower() not in ("0", "false", "no")   # naməlum nadir mesaj növlərini adminə bildir
LUCKYBAG_CHANNELS = os.environ.get("LUCKYBAG_CHANNELS", "0").strip().lower() in ("1", "true", "yes")   # kanallara da göndər (defolt: yalnız admin)
CONTINUOUS = os.environ.get("CONTINUOUS", "1").strip().lower() not in ("0", "false", "no")  # siyahı dövrü təkrar yoxlansın
RESCAN_PAUSE = _int("RESCAN_PAUSE", 20)                  # dövrlər arası fasilə (saniyə)
FLEET_DIRECT_CAP = _int("FLEET_DIRECT_CAP", 3000)         # proxy YOXDURSA: tək birbaşa IP-də max canlı
ADMIN_BOT = os.environ.get("ADMIN_BOT", "1").strip().lower() not in ("0", "false", "no")  # 0: bu nüsxədə Telegram idarə botu yoxdur (yalnız bildiriş)

# Qoşulma mühərriki: "piratetok" (imza/API yoxdur) | "tiktoklive" (Euler imzası ilə, v12 məntiqi: tam diaqnostika)
FLEET_ENGINE = os.environ.get("FLEET_ENGINE", "piratetok").strip().lower()
REPROBE_EVERY = _int("REPROBE_EVERY", 45)   # tiktoklive mühərriki: otaq məlumatı yoxlama aralığı (saniyə); Euler sorğusu qənaəti üçün 600 qoyun


# ---------------- v21: qrup qapısı ----------------
# 1 (defolt): bot başlayanda qrupa/kanala heç nə atmır, əvvəl admin-dən soruşur (Qrupa at / Hazırla)
GROUP_LIVE_DIGEST_MIN = _int("GROUP_LIVE_DIGEST_MIN", 15)   # qrupa/kanala say məlumatı (neçə canlı / offline) hər N dəqiqədən bir; eyni mesaj redaktə olunur (0 = söndür)
GROUP_GATE = os.environ.get("GROUP_GATE", "0").strip().lower() not in ("0", "false", "no")
