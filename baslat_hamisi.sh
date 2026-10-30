#!/data/data/com.termux/files/usr/bin/bash
# TƏK ƏMR:  bash baslat_hamisi.sh
# Addımlar: venv qurur → token soruşur (ilk dəfə) → Downloads-dakı proxy fayllarını proxies.txt-ə əlavə edir (yoxlamasız) → botu başladır.
#   bash baslat_hamisi.sh --yoxla   : proxy-ləri əvvəl TikTok-a qarşı yoxlayır (yavaşdır, lazım deyil)
# Bot çökərsə 20 san sonra özü yenidən başlayır. Dayandırmaq üçün: Ctrl+C
BOT="$(cd "$(dirname "$0")" && pwd)"
cd "$BOT" || exit 1
termux-wake-lock 2>/dev/null          # telefon ekranı söndükdə də işləsin

if [ ! -d venv ]; then
  echo "=== ilk quraşdırma: venv + paketlər (bir neçə dəqiqə çəkə bilər) ==="
  python3 -m venv venv || { echo "python tapılmadı: pkg install python -y"; exit 1; }
  . venv/bin/activate
  pip install -r requirements.txt || { echo "pip xətası, yuxarıdakı mesaja bax"; exit 1; }
else
  . venv/bin/activate
fi

[ -f .env ] || cp .env.example .env
cp -n .env .env.bak 2>/dev/null
set_env() { sed -i "/^$1=/d" .env; echo "$1=$2" >> .env; }
if ! grep -q '^TELEGRAM_BOT_TOKEN=..*' .env; then
  read -r -p "Telegram bot token: " T; set_env TELEGRAM_BOT_TOKEN "$T"
fi
if ! grep -q '^TELEGRAM_CHAT_ID=..*' .env; then
  read -r -p "Telegram chat id (sənin ID-n): " C; set_env TELEGRAM_CHAT_ID "$C"
fi

# ---- v14 TURBO ayarları: bütün proxy-lər birdən işləyir (+ telefonun öz IP-si) ----
set_env PROXY_MODE all
set_env INCLUDE_DIRECT 1
set_env FLEET_PER_IP 25
set_env FLEET_PER_IP_PREMIUM 150
set_env PROXY_REFRESH_MIN 30
set_env FLEET_DIRECT_SHARE 3000
set_env FLEET_DIRECT_CAP 3000
set_env FLEET_DIRECT_FIRST 1
set_env FLEET_DIRECT_CONNECT 12
set_env FLEET_DIRECT_QUEUE_MAX 4
set_env FLEET_DIRECT_MAX_FAILS 8
set_env FLEET_DIRECT_COOLDOWN 30
set_env FLEET_PER_PROXY_CONNECT 4
set_env FLEET_CONNECT_PARALLEL 300
set_env FLEET_MAX_LIVES 3000
set_env FLEET_START_DELAY 0.35
set_env FLEET_DIRECT_START_DELAY 0.4
set_env FLEET_PROXY_MAX_FAILS 3
set_env FLEET_PROXY_COOLDOWN 240
set_env TTWID_REUSE 150
set_env TTWID_FAIL_COOLDOWN 30
set_env SCAN_PER_LANE 2
set_env SCAN_WORKER_CAP 2
set_env SCAN_SPACING 1.2
set_env SCAN_MAX_PARALLEL 150
set_env SCAN_RECOVER_AFTER 30
set_env EXECUTOR_THREADS 500
set_env VIEWERS_TIMEOUT 4
set_env CONTINUOUS 1
set_env GROUP_GATE 0
set_env GROUP_LIVE_DIGEST_MIN 15
set_env FLEET_TARGET_LIVES 3000
set_env USERS_LOCKED 1

# ---- proxy-ləri əlavə et (Downloads + bot qovluğu; təkrarsız) ----
# ---- ad siyahısı: users.txt telefonda qalır (zip üstünə yazmır); yoxdursa baza siyahı, sonra kanal adları birləşdirilir ----
mkdir -p results
# users.txt = DƏQİQ users_base.txt (yalnız sənin siyahın). Baza dəyişəndə üstünə yazılır.
BASE_SUM="$(md5sum users_base.txt | cut -d' ' -f1)"
if [ ! -f users.txt ] || [ "$(cat results/.users_base_md5 2>/dev/null)" != "v29-$BASE_SUM" ]; then
  [ -f users.txt ] && cp users.txt "users.txt.$(date +%m%d_%H%M).bak"
  grep -v '^#' users_base.txt | sed '/^$/d' > users.txt
  [ -f results/kanal_users.txt ] && mv results/kanal_users.txt "results/kanal_users.$(date +%m%d_%H%M).bak"   # köhnə kanal adları geri qarışmasın
  echo "v29-$BASE_SUM" > results/.users_base_md5
  echo "=== ad siyahısı DƏQİQ yeni siyahıdır ==="
fi
echo "=== users.txt: $(wc -l < users.txt) ad ==="
touch proxies.txt
PROXY_CHECK_MAX_SEC=${PROXY_CHECK_MAX_SEC:-600} python proxy_auto.py        # premium-u sil → təzə pulsuz siyahıları endir → TikTok-a qarşı yoxla → yalnız İŞLƏYƏNLƏR proxies.txt-ə düşür
if [ "$1" = "--yoxla" ]; then
  DATA="$(ls -t ~/storage/downloads/data.txt "$BOT/proxy_lists/data.txt" 2>/dev/null | head -1)"
  [ -f "$DATA" ] && python proxy_check_all.py "$DATA" 600
fi
echo "=== proxy sayı: $(wc -l < proxies.txt) | bot başlayır ($(date +%H:%M)) ==="

while true; do
  bash run.sh
  echo "$(date +%H:%M:%S) bot dayandı, 20 san sonra yenidən başlayır (dayandırmaq üçün Ctrl+C)"
  sleep 20
done
