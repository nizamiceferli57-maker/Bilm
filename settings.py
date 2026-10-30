"""Admin ayarları (results/settings.json): kanallar, filtrlər, sürət, panel. Yenidən başlayanda saxlanır."""
from __future__ import annotations

import json
import os
import time

import config

DEFAULTS = {
    "channels": [],            # [{id, title, username, enabled, mode: 'chest'|'all'}]
    "muted": False,            # admin üçün sandıq bildirişləri susdurulsun
    "ch_min_ratio": 0.0,       # kanala göndərmək üçün minimum ratio
    "ch_min_coins": 0,         # kanala göndərmək üçün minimum jeton
    "report_every": config.TELEGRAM_REPORT_EVERY,
    "listen_seconds": config.CHEST_LISTEN_SECONDS,
    "notify_max": config.CHEST_NOTIFY_MAX,
    "probe_room": True,        # qoşulanda otaq məlumatından əvvəldən olan sandığı da axtar
    "fixed_rate": None,        # sabit sürət (ad/saniyə) və ya None
    "speed": None,             # {"workers": n, "interval": x}
    "panel": None,             # {"chat_id": ..., "message_id": ...}
    "alert_block": True,       # çox blok olanda admin-ə xəbər
    "alert_done": True,        # skan bitəndə xəbər
    "ignore": [],              # skan edilməyəcək adlar
    "join_after_scan": False,  # True olsa: siyahı tam yoxlanıb bitəndən SONRA qoşulur (defolt: tapılan kimi qoşul)
    "join_migrated": False,    # v17: köhnə saxlanmış True dəyəri bir dəfə False-a çevrilir
    "rescan_minutes": 30,      # siyahı neçə dəqiqədən bir yenidən yoxlansın
    "watch": [],               # /izle ilə verilən hesablar: yayıma başlayan kimi ordu qoşulur, sandıq həmişə bildirilir
}


class Settings:
    def __init__(self) -> None:
        self.data: dict = json.loads(json.dumps(DEFAULTS))
        self._load()
        # .env-dən gələn kanallar (təkrarsız)
        known = {str(c["id"]).lower() for c in self.data["channels"]}
        for cid in config.CHANNEL_IDS:
            if cid.lower() not in known:
                self.data["channels"].append({"id": cid, "title": cid, "username": "", "enabled": True, "mode": "chest"})
                known.add(cid.lower())

    def _load(self) -> None:
        f = config.SETTINGS_FILE
        try:
            if f.exists():
                saved = json.loads(f.read_text(encoding="utf-8"))
                for k, v in saved.items():
                    if k in self.data:
                        self.data[k] = v
                if not self.data.get("join_migrated"):
                    self.data["join_after_scan"] = False
                    self.data["join_migrated"] = True
        except (ValueError, OSError):
            pass

    def save(self) -> None:
        f = config.SETTINGS_FILE
        try:
            f.parent.mkdir(exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, f)
        except OSError as exc:
            print(f"[settings] saxlanmadı: {exc}")

    def __getitem__(self, key):
        return self.data[key]

    def set(self, key, value) -> None:
        self.data[key] = value
        self.save()

    # ---- kanallar ----
    def find_channel(self, ref) -> dict | None:
        ref = str(ref).strip().lower().lstrip("@")
        for c in self.data["channels"]:
            if str(c["id"]).lower().lstrip("@") == ref or (c.get("username") or "").lower() == ref:
                return c
        return None

    def add_channel(self, chat: dict) -> dict:
        """chat: Telegram getChat nəticəsi."""
        cid = chat["id"]
        existing = self.find_channel(cid) or (self.find_channel(chat["username"]) if chat.get("username") else None)
        entry = {
            "id": cid,
            "title": chat.get("title") or str(cid),
            "username": chat.get("username") or "",
            "enabled": True,
            "mode": "chest",
        }
        if existing:
            existing.update({k: entry[k] for k in ("id", "title", "username")})
            self.save()
            return existing
        self.data["channels"].append(entry)
        self.save()
        return entry

    def remove_channel(self, ref) -> bool:
        c = self.find_channel(ref)
        if not c:
            return False
        self.data["channels"].remove(c)
        self.save()
        return True

    def ch_get(self, ch: dict, key: str):
        """Kanalın öz filtri; təyin olunmayıbsa ümumi filtr."""
        v = ch.get(key)
        if v is not None:
            return v
        return {"min_ratio": self.data["ch_min_ratio"], "min_coins": self.data["ch_min_coins"], "min_viewers": 0,
                "luckybag": config.LUCKYBAG_CHANNELS}.get(key)

    def active_channels(self, mode: str = "chest") -> list[dict]:
        """mode='chest': sandıq bildirişi alanlar (hamısı). mode='all': hesabat da alanlar."""
        out = []
        for c in self.data["channels"]:
            if not c.get("enabled", True):
                continue
            if mode == "all" and c.get("mode") != "all":
                continue
            out.append(c)
        return out

    # ---- ignore ----
    def ignored(self) -> set[str]:
        return {x.lower() for x in self.data["ignore"]}


settings = Settings()
_ = time  # noqa
