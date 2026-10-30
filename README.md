# TikTok canlı + sandıq / hədiyyə çantası botu (v29)

- `vendor/live-py` (PirateTok): siyahıdakı adların canlı olub-olmadığını yoxlayır
- `vendor/TikTokLive`: canlılara qoşulub sandıq (`EnvelopeEvent`) və Lucky/Goody Bag mesajlarını dinləyir
- `bot_admin.py`: Telegram idarəsi. Başlatmaq üçün `START_HERE.txt`.

## v25
- Eyni anda maksimum canlı **3000** (`FLEET_MAX_LIVES`, `FLEET_TARGET_LIVES`, `FLEET_DIRECT_SHARE`, `FLEET_DIRECT_CAP`); fayl limiti 32768-ə qaldırılır, azdırsa xəbərdarlıq.
- Yeni proxy siyahıları (`proxy_lists/`): təmizlənib, təkrarlar atılıb; kiçik siyahılar əvvəl yoxlanır; Downloads-dakı `http.txt/get.txt/data.txt/proxy.txt` avtomatik götürülür.
- Başlanğıcda Telegram-a «Ordu başladı: maksimum / real tutum» xülasəsi.
- Alış-veriş «LiveBag» açar sözləri hədiyyə çantası siqnalından ayrıldı (weak).
- Silindi: köhnə skriptlər (setup.sh, run.bat, try_ips.py, get_proxies.py, check_proxies.py, split_users.py), köhnə README-lər, vendor-dakı nümunə/CI faylları, təkrar data.txt.
