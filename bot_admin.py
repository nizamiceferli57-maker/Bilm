"""Admin botu: YALNIZ ADMIN_IDS-dəki istifadəçilərə cavab verir, başqalarını sakitcə yox sayır.

- Əmrlər, inline düymələr, canlı yenilənən panel
- Kanal əlavə etmə: /kanal_elave @kanal, kanaldan post forward etmək, və ya botu kanala admin etmək (təsdiq soruşur)
- Skaneri idarə: pauza / davam / dayan / başlat / sürət
- Siyahı: .txt göndərmək və ya ad yazmaq ilə users.txt-yə əlavə
"""
import asyncio
import re
import time

import config
import group_gate
import telegram_notify as tg
import following
import manual_post
import userlist
import views
from log import LINES, log
from settings import settings
from stats import fmt_duration

ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member", "channel_post"]
NAME_RE = re.compile(r"^[A-Za-z0-9._]{1,24}$")
MANUAL_LISTEN = 90  # əl ilə verilən adın defolt dinləmə müddəti (saniyə)


def _split_secs(tokens: list[str]) -> tuple[list[str], int | None]:
    """Sondakı rəqəm (10–600) dinləmə müddəti sayılır: 'ad 120'."""
    if len(tokens) >= 2 and tokens[-1].isdigit() and 10 <= int(tokens[-1]) <= 600:
        return tokens[:-1], int(tokens[-1])
    return tokens, None

COMMANDS = [
    ("menu", "🏠 Ana menyu"),
    ("hazirla", "🛠 Hazırla (skan + qoşulma + kanal yoxlaması)"),
    ("menbe", "📥 Mənbə kanal: linklərdən TikTok adlarını çıxar"),
    ("qrupa_at", "📤 Say məlumatını qrupa göndər (neçə canlı / offline)"),
    ("izlenenler", "👥 Hesabın izlədikləri siyahısı (.txt)"),
    ("yenile_vaxt", "🔁 Siyahı neçə dəqiqədən bir yoxlansın"),
    ("kanal_filtr", "🎚 Kanalın öz filtri"),
    ("kanal_yoxla", "📢 Qrup/kanal icazələrini yoxla"),
    ("say", "🧮 Sayğac: neçə ad, neçəsi yoxlanıb, online/offline, çanta"),
    ("status", "📊 Tam status"),
    ("panel", "📡 Canlı panel"),
    ("top", "🏆 Ən yaxşı ratio"),
    ("son", "📦 Son sandıqlar"),
    ("canli", "🟢 Canlı yayımlar"),
    ("pauza", "⏸ Pauza"),
    ("davam", "▶️ Davam"),
    ("dayan", "🛑 Dayandır"),
    ("baslat", "🚀 Yeni skan başlat"),
    ("surat", "⚡ Skan sürəti"),
    ("sabit", "🔒 Sabit sürət (ad/saniyə)"),
    ("yoxla", "🔎 Bir hesabı yoxla"),
    ("hd", "🛍 Hədiyyə çantası mesajı (əl ilə)"),
    ("canta", "🎓 Çantanı öyrət: /canta ad (çantanı qoyanda bot mesajı tapıb əlavə edir)"),
    ("canta_bit", "⏹ Öyrənməni indi bitir"),
    ("canta_test", "🧪 Lucky Bag xəbərinin test nümunəsi (yalnız sənə)"),
    ("metodlar", "📄 Bütün gələn mesaj növləri faylı (nadir olanlar əvvəl)"),
    ("cantalar", "📋 Öyrənilən çanta mesajları + gələn naməlum növlər"),
    ("canta_elave", "➕ Çanta mesaj növü əlavə et"),
    ("canta_sil", "➖ Çanta mesaj növünü sil"),
    ("sd", "🎁 Sandıq mesajı (əl ilə)"),
    ("qosul", "🔌 Canlıya əl ilə qoşul"),
    ("izle", "👁 Hesabı izləməyə əlavə et"),
    ("izle_siyahi", "📋 İzlənən hesablar"),
    ("izle_sil", "🗑 İzləməni sil"),
    ("kanallar", "📢 Kanallar"),
    ("kanal_elave", "➕ Kanal əlavə et"),
    ("filtr", "🎚 Kanal filtri"),
    ("sessiz", "🔕 Admin bildirişi aç/bağla"),
    ("hesabat", "📨 Hesabat indi"),
    ("fayllar", "📁 Nəticə faylları"),
    ("log", "🧾 Son loglar"),
    ("komek", "❓ Bütün əmrlər"),
]


class AdminBot:
    def __init__(self, ctl) -> None:
        self.ctl = ctl
        self.offset = 0
        self.awaiting_users: set = set()
        self.awaiting_follow: set = set()
        self.manual: dict = {}  # /hd /sd söhbəti: cid -> {"kind", "step", "data"}
        self._denied: set = set()
        self._conflict_logged = False

    # ------------------------------------------------------------------ yoxlama
    @staticmethod
    def is_admin(user_id, chat: dict) -> bool:
        if user_id is None or user_id not in config.ADMIN_IDS:
            return False
        return chat.get("type") == "private" or chat.get("id") in config.ADMIN_IDS

    def _deny(self, user_id) -> None:
        if user_id not in self._denied:  # eyni şəxs üçün loqda bir dəfə (cavab VERİLMİR)
            self._denied.add(user_id)
            log(f"[BOT] 🚫 admin olmayan istifadəçi ({user_id}) yazdı, cavab verilmədi")

    # ------------------------------------------------------------------ başlanğıc
    async def setup(self) -> bool:
        ok, me = await tg.call("getMe")
        if not ok:
            log(f"[BOT] ❌ token işləmir: {me}")
            return False
        tg.BOT.update(me)
        await tg.call("deleteWebhook", {"drop_pending_updates": False})
        ok, ups = await tg.call("getUpdates", {"offset": -1, "timeout": 0})  # köhnə əmrlər icra olunmasın
        if ok and ups:
            self.offset = ups[-1]["update_id"] + 1
        cmds = [{"command": c, "description": d} for c, d in COMMANDS]
        await tg.call("deleteMyCommands")  # kənar adamlar menyu görməsin
        for aid in config.ADMIN_IDS:
            if aid > 0:
                await tg.call("setMyCommands", {"commands": cmds, "scope": {"type": "chat", "chat_id": aid}})
        log(f"[BOT] 🤖 admin botu hazırdır: @{me.get('username')} | adminlər: {len(config.ADMIN_IDS)} | kanallar: {len(settings['channels'])}")
        return True

    async def run(self) -> None:
        if not tg.has_admin():
            log("[BOT] token/admin yoxdur, Telegram əmrləri söndürülü")
            return
        while not await self.setup():  # şəbəkə yoxdursa gözləyib təkrar cəhd
            await asyncio.sleep(10)
        while True:
            ok, ups = await tg.call(
                "getUpdates", {"offset": self.offset, "timeout": 25, "allowed_updates": ALLOWED_UPDATES}, retries=1, timeout=45
            )
            if not ok:
                if "Conflict" in str(ups) and not self._conflict_logged:
                    self._conflict_logged = True
                    log("[BOT] ⚠ Eyni token ilə başqa bot nüsxəsi də işləyir (Conflict). Birini bağlayın.")
                await asyncio.sleep(3)
                continue
            for u in ups:
                self.offset = u["update_id"] + 1
                try:
                    await self.handle(u)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log(f"[BOT] ⚠ xəta: {type(exc).__name__}: {exc}")

    async def panel_loop(self) -> None:
        """Canlı panel: eyni mesaj hər PANEL_REFRESH saniyədən bir redaktə olunur."""
        while True:
            await asyncio.sleep(max(config.PANEL_REFRESH, 5))
            p = settings["panel"]
            if not p:
                continue
            ok, err = await tg.edit_message(
                p["chat_id"], p["message_id"], views.status_html(self.ctl, "📡 <b>CANLI PANEL</b>"), keyboard=views.status_kb(self.ctl)
            )
            if not ok and any(k in err.lower() for k in ("not found", "can't be edited", "message to edit", "chat not found")):
                settings.set("panel", None)
                log("[BOT] canlı panel mesajı silinib, panel söndürüldü (/panel ilə yenidən yaradın)")

    # ------------------------------------------------------------------ yönləndirmə
    async def handle(self, u: dict) -> None:
        if "message" in u:
            await self.on_message(u["message"])
        elif "callback_query" in u:
            await self.on_callback(u["callback_query"])
        elif "my_chat_member" in u:
            await self.on_my_chat_member(u["my_chat_member"])
        elif "channel_post" in u:
            await self.on_source_post(u["channel_post"])

    async def reply(self, chat_id, text: str, kb=None):
        return await tg.send_message(chat_id, text, keyboard=kb)

    def render(self, name: str):
        c = self.ctl
        if name == "status":
            return views.status_html(c), views.status_kb(c)
        if name == "top":
            return views.top_html(c.stats), views.back_kb()
        if name == "last":
            return views.last_html(c.stats), views.back_kb()
        if name == "live":
            return views.live_html(c.stats), views.back_kb()
        if name == "speed":
            return views.speed_html(c), views.speed_kb()
        if name == "channels":
            return views.channels_html(), views.channels_kb()
        if name.startswith("chan:"):
            i = int(name.split(":")[1])
            return views.chan_html(i), views.chan_kb(i)
        if name == "settings":
            return views.settings_html(c), views.settings_kb()
        return views.menu_html(c), views.menu_kb(c)

    async def show(self, chat_id, message_id, name: str) -> None:
        text, kb = self.render(name)
        ok, _ = await tg.edit_message(chat_id, message_id, text, keyboard=kb)
        if not ok:
            await self.reply(chat_id, text, kb)

    # ------------------------------------------------------------------ mesajlar
    async def on_message(self, m: dict) -> None:
        frm, chat = m.get("from") or {}, m.get("chat") or {}
        if not self.is_admin(frm.get("id"), chat):
            if chat.get("type") == "private":
                self._deny(frm.get("id"))
            return
        cid = chat["id"]

        fchat = self._forwarded_channel(m)
        if fchat:
            await self.add_channel(cid, fchat["id"])
            return
        if m.get("document"):
            await self.on_document(cid, m["document"])
            return

        text = (m.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            parts = text.split(None, 1)
            cmd = parts[0][1:].split("@")[0].lower()
            args = parts[1].strip() if len(parts) > 1 else ""
            await self.command(cid, cmd, args, frm)
        elif cid in self.manual:
            await self.manual_step(cid, text)
        elif cid in self.awaiting_follow:
            self.awaiting_follow.discard(cid)
            names = userlist.parse_names(text)
            if names:
                asyncio.create_task(self.cmd_following(cid, names[0]))
        elif cid in self.awaiting_users:
            self.awaiting_users.discard(cid)
            await self.add_users(cid, text)
        else:
            names, secs = _split_secs(userlist.parse_names(text))
            if 1 <= len(names) <= 5 and all(NAME_RE.match(n) for n in names) and (len(names) > 1 or "\n" not in text):
                for n in names:  # sadəcə ad yazılıb: dərhal öncəlikli yoxla
                    asyncio.create_task(self._manual_check(cid, n, secs))
            else:
                await self.reply(cid, "🏠 Menyu üçün /menu, əmrlər üçün /komek yazın 👇")

    async def manual_step(self, cid, text: str) -> None:
        """/hd /sd: sualları ardıcıl verir, sonda mesajı admin + kanallara göndərir. Yayıma qoşulmur, skana toxunmur."""
        st = self.manual[cid]
        key = manual_post.STEPS[st["step"]][0]
        val = manual_post.parse_answer(key, text)
        if val is None:
            await self.reply(cid, manual_post.error_text(key))
            return
        st["data"][key] = val
        st["step"] += 1
        if st["step"] < len(manual_post.STEPS):
            await self.reply(cid, manual_post.STEPS[st["step"]][1])
            return
        self.manual.pop(cid, None)
        n = manual_post.send(st["kind"], st["data"])
        await self.reply(cid, f"✅ Göndərildi: admin + <b>{n}</b> kanal/qrup.\nYenisi üçün: /{st['kind']}")

    @staticmethod
    def _forwarded_channel(m: dict):
        fo = m.get("forward_origin")
        if fo and fo.get("type") == "channel" and fo.get("chat"):
            return fo["chat"]
        ffc = m.get("forward_from_chat")
        if ffc and ffc.get("type") == "channel":
            return ffc
        return None

    # ------------------------------------------------------------------ əmrlər
    async def command(self, cid, cmd: str, args: str, frm: dict) -> None:
        c = self.ctl
        had_manual = None
        if cmd not in ("hd", "sd"):
            had_manual = self.manual.pop(cid, None)  # başqa əmr yazılsa yarımçıq /hd /sd söhbəti ləğv olur
        if cmd in ("hd", "sd"):
            self.manual[cid] = {"kind": cmd, "step": 0, "data": {}}
            await self.reply(cid, manual_post.TITLES[cmd] + "\n" + views.LINE + "\n" + manual_post.STEPS[0][1] + "\n<i>Ləğv etmək: /legv</i>")
        elif cmd in ("legv", "ləğv"):
            await self.reply(cid, "↩ Ləğv edildi." if had_manual else "ℹ️ Ləğv ediləcək yarımçıq mesaj yoxdur.")
        elif cmd in ("start", "menu"):
            name = esc_name(frm)
            head = f"👋 Salam, <b>{name}</b>!\n\n" if cmd == "start" else ""
            await self.reply(cid, head + views.menu_html(c), views.menu_kb(c))
        elif cmd in ("menbe", "mənbə", "source"):
            await self.cmd_source(cid, args)
        elif cmd in ("qrupa_at", "qrupa", "at", "qrupaat"):
            await self.open_group(cid)
        elif cmd in ("hazirla", "hazırla", "prepare"):
            await self.start_prepare(cid)
        elif cmd in ("izlenenler", "izlenen", "following"):
            names = userlist.parse_names(args)
            if names:
                asyncio.create_task(self.cmd_following(cid, names[0]))
            else:
                self.awaiting_follow.add(cid)
                await self.reply(cid, "👥 Hansı TikTok hesabın izlədiklərini çıxarım? Hesabın <b>adını</b> yazın (məs. <code>mssxanaqleey</code>).\nℹ️ Yalnız izlədikləri siyahısı <b>açıq</b> olan hesablarda işləyir.")
        elif cmd in ("yenile_vaxt", "yenilevaxt"):
            n = _int(args, 0, 0, 1440)
            if n <= 0:
                await self.reply(cid, f"🔁 İndi: hər <b>{settings['rescan_minutes']}</b> dəq. Dəyişmək üçün: <code>/yenile_vaxt 30</code>")
            else:
                settings.set("rescan_minutes", n)
                await self.reply(cid, f"✅ Siyahı artıq hər <b>{n}</b> dəqiqədən bir yoxlanacaq.")
        elif cmd in ("yoxla_indi", "yoxlaindi"):
            c.rescan_now.set()
            await self.reply(cid, "🔁 Siyahı dərhal yenidən yoxlanır (skan artıq gedirsə, bitəndən sonra yeni adlar götürüləcək).")
        elif cmd in ("kanal_filtr", "kanalfiltr"):
            await self.cmd_channel_filter(cid, args)
        elif cmd in ("kanal_yoxla", "kanalyoxla"):
            rep = await self.check_channels()
            await self.reply(cid, "📢 <b>KANAL / QRUP YOXLAMASI</b>\n" + views.LINE + "\n" + (
                "\n".join(f"{'✅' if ok else '❌'} <b>{views.esc(t)}</b> — {views.esc(n)}" for t, ok, n in rep) if rep else "📭 Kanal yoxdur: /kanal_elave @kanal"), views.channels_kb())
        elif cmd in ("komek", "help"):
            await self.reply(cid, views.help_html())
        elif cmd == "status":
            await self.reply(cid, views.status_html(c), views.status_kb(c))
        elif cmd == "panel":
            await self.create_panel(cid)
        elif cmd == "panel_off":
            await self.drop_panel()
            await self.reply(cid, "📴 Canlı panel söndürüldü.")
        elif cmd == "top":
            await self.reply(cid, views.top_html(c.stats), views.back_kb())
        elif cmd == "son":
            n = _int(args, 10, 1, 25)
            await self.reply(cid, views.last_html(c.stats, n), views.back_kb())
        elif cmd == "canli":
            await self.reply(cid, views.live_html(c.stats), views.back_kb())
        elif cmd == "pauza":
            await self.reply(cid, "⏸ <b>Fasiləyə alındı.</b> Davam üçün /davam" if c.pause() else "ℹ️ Hazırda fasiləyə almaq olmur (işləmir və ya artıq fasilədədir).")
        elif cmd == "davam":
            await self.reply(cid, "▶️ <b>Davam edir!</b>" if c.resume() else "ℹ️ Fasilədə olan skan yoxdur.")
        elif cmd == "dayan":
            if not c.active:
                await self.reply(cid, "ℹ️ Aktiv skan yoxdur.")
            else:
                await self.reply(
                    cid,
                    "🛑 <b>Skan dayandırılsın?</b>\nYoxlananlar saxlanır, sonra /baslat davam ilə qaldığı yerdən davam edə bilərsiniz.",
                    [[{"text": "🛑 Bəli, dayandır", "callback_data": "stopy"}, {"text": "↩ Xeyr", "callback_data": "stopn"}]],
                )
        elif cmd == "baslat":
            resume = args.lower().startswith(("d", "r"))  # davam / resume
            if c.request_start(resume):
                await self.reply(cid, f"🚀 <b>Yeni skan başlayır</b> ({'qaldığı yerdən davam' if resume else 'sıfırdan'}).")
            else:
                await self.reply(cid, "ℹ️ Skan artıq işləyir. Əvvəl /dayan edin.")
        elif cmd == "surat":
            await self.cmd_speed(cid, args)
        elif cmd == "sabit":
            await self.cmd_fixed(cid, args)
        elif cmd == "yoxla":
            await self.cmd_check(cid, args)
        elif cmd == "izle":
            await self.cmd_watch(cid, args)
        elif cmd == "izle_siyahi":
            await self.cmd_watchlist(cid)
        elif cmd == "izle_sil":
            await self.cmd_unwatch(cid, args)
        elif cmd == "qosul":
            await self.cmd_join(cid, args)
        elif cmd == "say":
            await self.reply(cid, views.counts_html(self.ctl))
        elif cmd == "canta":
            await self.cmd_learn(cid, args)
        elif cmd == "canta_bit":
            import luckybag
            n = luckybag.stop_learn()
            await self.reply(cid, "⏹ Öyrənmə bitirilir…" if n else "ℹ️ Hazırda öyrənmə yoxdur (və ya baza hələ yığılır).")
        elif cmd == "canta_test":
            await self.cmd_canta_test(cid)
        elif cmd == "metodlar":
            await self.cmd_methods(cid)
        elif cmd == "cantalar":
            await self.cmd_canta_list(cid)
        elif cmd in ("canta_elave", "canta_sil"):
            await self.cmd_canta_edit(cid, args, add=(cmd == "canta_elave"))
        elif cmd == "probe":
            await self.cmd_probe(cid, args)
        elif cmd == "kanallar":
            await self.reply(cid, views.channels_html(), views.channels_kb())
        elif cmd == "kanal_elave":
            if not args:
                await self.reply(cid, "➕ İstifadə: <code>/kanal_elave @kanal</code> və ya <code>/kanal_elave -1001234567890</code>\nVə ya kanaldan bir postu bura forward edin.")
            else:
                await self.add_channel(cid, args.split()[0])
        elif cmd == "kanal_sil":
            await self.cmd_channel_remove(cid, args)
        elif cmd == "filtr":
            await self.cmd_filter(cid, args)
        elif cmd == "sessiz":
            settings.set("muted", not settings["muted"])
            await self.reply(cid, "🔕 Admin sandıq bildirişi <b>söndürüldü</b> (kanallara getməyə davam edir)." if settings["muted"] else "🔔 Admin sandıq bildirişi <b>açıldı</b>.")
        elif cmd == "hesabat":
            await self.reply(cid, views.status_html(c, "⏱ <b>ARA HESABAT</b>"), views.status_kb(c))
        elif cmd == "hesabat_vaxt":
            await self.cmd_report_interval(cid, args)
        elif cmd == "dinleme":
            v = _int(args, 0, 5, 600)
            if not v:
                await self.reply(cid, f"👂 Dinləmə müddəti: <b>{settings['listen_seconds']}s</b>\nDəyişmək: <code>/dinleme 30</code> (5–600 san)")
            else:
                settings.set("listen_seconds", v)
                await self.reply(cid, f"👂 Dinləmə müddəti <b>{v}s</b> oldu (yeni qoşulmalardan etibarən).")
        elif cmd == "bildiris_max":
            v = _int(args, 0, 1, 50)
            if not v:
                await self.reply(cid, f"🔢 Yayım başına maks. bildiriş: <b>{settings['notify_max']}</b>\nDəyişmək: <code>/bildiris_max 3</code>")
            else:
                settings.set("notify_max", v)
                await self.reply(cid, f"🔢 Yayım başına maks. bildiriş <b>{v}</b> oldu.")
        elif cmd == "ayarlar":
            await self.reply(cid, views.settings_html(c), views.settings_kb())
        elif cmd == "istifadeci_elave":
            if args:
                await self.add_users(cid, args)
            else:
                self.awaiting_users.add(cid)
                await self.reply(cid, "📝 Adları göndərin (hər sətirdə bir ad, @ ilə də olar) və ya <b>.txt</b> faylı göndərin.")
        elif cmd == "istisna":
            await self.cmd_ignore(cid, args)
        elif cmd == "fayllar":
            await self.send_files(cid)
        elif cmd == "log":
            await self.send_log(cid, _int(args, 25, 5, 60))
        elif cmd == "ping":
            await self.reply(cid, f"🏓 <b>Pong!</b>\n⏱ İş vaxtı: <b>{fmt_duration(c.uptime())}</b>\n{views.PHASES.get(c.phase)}\n🕒 {time.strftime('%H:%M:%S')}")
        elif cmd == "kapat":
            await self.reply(
                cid,
                "⚠️ <b>Bot tam söndürülsün?</b>\nSonra yenidən kompüterdən/Termux-dan işə salmaq lazım olacaq.",
                [[{"text": "⏻ Bəli, söndür", "callback_data": "offy"}, {"text": "↩ Xeyr", "callback_data": "stopn"}]],
            )
        else:
            await self.reply(cid, "❓ Belə əmr yoxdur. /komek")

    # ------------------------------------------------------------------ düymələr
    async def on_callback(self, cq: dict) -> None:
        frm, msg = cq.get("from") or {}, cq.get("message") or {}
        chat = msg.get("chat") or {}
        if not self.is_admin(frm.get("id"), chat):
            return  # sakit: cavab yoxdur
        cid, mid = chat["id"], msg.get("message_id")
        kind, _, rest = (cq.get("data") or "").partition(":")
        c = self.ctl
        toast = ""

        if kind == "go":
            await self.show(cid, mid, rest)
        elif kind == "gate":
            toast = await self.open_group(cid)
        elif kind == "ctl":
            toast = await self.cb_control(cid, mid, rest)
        elif kind == "sp":
            toast = self.apply_preset(rest)
            await self.show(cid, mid, "speed")
        elif kind == "tg":
            if rest in ("muted", "alert_block", "alert_done", "join_after_scan"):
                settings.set(rest, not settings[rest])
                toast = "✅ Dəyişdirildi"
            await self.show(cid, mid, "settings")
        elif kind == "ch":
            toast = await self.cb_channel(rest)
            parts = rest.split(":")
            await self.show(cid, mid, f"chan:{parts[1]}" if parts[0] in ("s", "r", "c", "v", "l") and len(parts) > 1 else "channels")
        elif kind == "fw":
            toast = await self.cb_follow(cid, mid, rest)
        elif kind == "chadd":
            await self.add_channel(cid, int(rest))
            toast = "Yoxlanılır…"
        elif kind == "stopy":
            toast = "🛑 Dayandırılır" if c.request_stop() else "ℹ️ Aktiv skan yoxdur"
            await tg.edit_message(cid, mid, "🛑 <b>Dayandırılır…</b> Cari sorğular bitəndən sonra skan durur.")
        elif kind == "offy":
            await tg.edit_message(cid, mid, "⏻ <b>Bot söndürülür…</b> Görüşənədək 👋")
            c.request_stop()
            c.shutdown.set()
            toast = "⏻ Söndürülür"
        elif kind in ("stopn", "chno"):
            await tg.edit_message(cid, mid, "↩ Ləğv edildi.")
        await tg.call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": toast[:180]}, retries=1)

    async def cb_control(self, cid, mid, action: str) -> str:
        c = self.ctl
        if action == "pause":
            t = "⏸ Fasilədə" if c.pause() else "ℹ️ Mümkün deyil"
            await self.show(cid, mid, "status")
            return t
        if action == "resume":
            t = "▶️ Davam edir" if c.resume() else "ℹ️ Mümkün deyil"
            await self.show(cid, mid, "status")
            return t
        if action == "start":
            t = "🚀 Başlayır" if c.request_start(False) else "ℹ️ Artıq işləyir"
            return t
        if action == "follow":
            self.awaiting_follow.add(cid)
            await self.reply(cid, "👥 Hansı TikTok hesabın izlədiklərini çıxarım? Hesabın <b>adını</b> yazın.\nℹ️ Yalnız izlədikləri siyahısı <b>açıq</b> olan hesablarda işləyir.")
            return "👥 Ad yazın"
        if action == "rescan":
            self.ctl.rescan_now.set()
            return "🔁 Yenidən yoxlanır"
        if action == "prepare":
            await self.start_prepare(cid, mid)
            return "🛠 Hazırlanır…"
        if action == "report":
            await self.reply(cid, views.status_html(c, "⏱ <b>ARA HESABAT</b>"), views.status_kb(c))
            return "📨 Göndərildi"
        if action == "panel":
            await self.create_panel(cid)
            return "📡 Panel yaradıldı"
        if action == "files":
            await self.send_files(cid)
            return "📁"
        if action == "log":
            await self.send_log(cid, 25)
            return "🧾"
        return ""

    async def cb_channel(self, rest: str) -> str:
        parts = rest.split(":")
        op, idx = parts[0], (parts[1] if len(parts) > 1 else "")
        val = parts[2] if len(parts) > 2 else None
        chans = settings["channels"]
        try:
            ch = chans[int(idx)]
        except (ValueError, IndexError):
            return "❌ Kanal tapılmadı"
        if op == "s":
            return "⚙"
        if op in ("r", "c", "v") and val is not None:
            key = {"r": "min_ratio", "c": "min_coins", "v": "min_viewers"}[op]
            ch[key] = float(val) if op == "r" else int(float(val))
            settings.save()
            return f"✅ {key} = {val}"
        if op == "l":
            ch["luckybag"] = not settings.ch_get(ch, "luckybag")
            settings.save()
            return "🛍 Lucky Bag " + ("açıq" if ch["luckybag"] else "bağlı")
        if op == "t":
            ch["enabled"] = not ch.get("enabled", True)
            settings.save()
            return "🔔 Açıldı" if ch["enabled"] else "🔕 Söndürüldü"
        if op == "m":
            ch["mode"] = "all" if ch.get("mode") != "all" else "chest"
            settings.save()
            return "📊 Hesabatlar da gələcək" if ch["mode"] == "all" else "📦 Yalnız sandıq"
        if op == "x":
            ok, err = await tg.probe(ch["id"])           # səssiz yoxlama: göndərir və dərhal silir, qrupda heç nə qalmır
            return "✅ Yaza bilirəm" if ok else f"❌ {err}"
        if op == "d":
            chans.remove(ch)
            settings.save()
            return "🗑 Silindi"
        return ""

    # ------------------------------------------------------------------ kanallar
    async def add_channel(self, cid, ref) -> None:
        s = str(ref).strip()
        for pre in ("https://", "http://", "t.me/", "telegram.me/"):
            if s.lower().startswith(pre):
                s = s[len(pre):]
        s = s.strip("/")
        if s.lstrip("-").isdigit():
            target: int | str = int(s)
        else:
            target = "@" + s.lstrip("@")
        ok, chat = await tg.call("getChat", {"chat_id": target}, retries=1)
        if not ok:
            await self.reply(cid, f"❌ <b>Kanal tapılmadı:</b> <code>{views.esc(chat)}</code>\n👉 Botu əvvəl kanala <b>admin</b> edin, sonra yenidən cəhd edin.")
            return
        if chat.get("type") not in ("channel", "supergroup", "group"):
            await self.reply(cid, "❌ Bu kanal/qrup deyil.")
            return
        ok, err = await tg.probe(chat["id"])                 # səssiz yoxlama: qrupda heç nə qalmır
        if not ok:
            await self.reply(cid, f"❌ <b>Kanala yaza bilmirəm:</b> <code>{views.esc(err)}</code>\n👉 Botu kanalda admin edin və <b>Mesaj göndərmək</b> icazəsi verin.")
            return
        entry = settings.add_channel(chat)
        await self.reply(cid, f"✅ <b>Kanal əlavə olundu:</b> {views.esc(entry['title'])}\n🎁 Sandıq bildirişləri artıq bura da gedəcək.", views.channels_kb())

    async def cmd_channel_remove(self, cid, args: str) -> None:
        if not args:
            await self.reply(cid, "🗑 İstifadə: <code>/kanal_sil @kanal</code> və ya sıra nömrəsi (<code>/kanal_sil 2</code>)")
            return
        ref = args.split()[0]
        chans = settings["channels"]
        target = chans[int(ref) - 1] if ref.isdigit() and 0 < int(ref) <= len(chans) else settings.find_channel(ref)
        if target and settings.remove_channel(target["id"]):
            await self.reply(cid, f"🗑 Kanal silindi: <b>{views.esc(target['title'])}</b>")
        else:
            await self.reply(cid, "❌ Belə kanal siyahıda yoxdur. /kanallar")

    async def on_my_chat_member(self, mcm: dict) -> None:
        chat, new = mcm.get("chat") or {}, mcm.get("new_chat_member") or {}
        if chat.get("type") not in ("channel", "supergroup", "group"):
            return
        status = new.get("status")
        known = settings.find_channel(chat["id"])
        title = views.esc(chat.get("title") or chat["id"])
        is_group = chat.get("type") in ("group", "supergroup")
        can_send = status == "administrator" and (is_group or new.get("can_post_messages", True)) or (
            status == "member" and is_group) or (status == "restricted" and new.get("can_send_messages", False))
        if can_send and not known:
            how = "admin kimi" if status == "administrator" else "üzv kimi"
            warn = "" if status == "administrator" else "\n⚠️ Qrupda mesaj icazəsi bağlı ola bilər — botu <b>admin</b> etsəniz sandıqlar səhvsiz gedər."
            for aid in config.ADMIN_IDS:  # yalnız adminlərə sual verilir
                await tg.send_message(
                    aid,
                    f"📢 <b>Bot «{title}» {'qrupuna' if is_group else 'kanalına'} {how} əlavə olundu.</b>{warn}\nSandıq bildirişləri bura göndərilsin?",
                    keyboard=[[{"text": "✅ Bəli", "callback_data": f"chadd:{chat['id']}"}, {"text": "❌ Xeyr", "callback_data": "chno"}]],
                )
        elif can_send and known:
            # admin icazəsi verildi / bərpa olundu → kanal yenidən aktiv, test mesajı ilə təsdiq
            known["enabled"] = True
            known.pop("err", None)
            settings.save()
            ok, err = await tg.probe(chat["id"])
            for aid in config.ADMIN_IDS:
                await tg.send_message(aid, f"✅ «{title}»: bot icazəni aldı, bildirişlər başladı." if ok else f"⚠️ «{title}»: hələ yaza bilmirəm: <code>{views.esc(err)}</code>")
        elif status in ("member", "restricted") and known and not is_group:
            for aid in config.ADMIN_IDS:
                await tg.send_message(aid, f"⚠️ Bot «{title}» kanalında artıq admin deyil — bildiriş gedə bilməz. Admin icazəsini qaytarın.")
        elif status in ("left", "kicked") and known:
            known["enabled"] = False
            settings.save()
            for aid in config.ADMIN_IDS:
                await tg.send_message(aid, f"⚠️ Bot «{title}» kanalından çıxarıldı. Kanal <b>söndürüldü</b> (/kanallar).")

    # ------------------------------------------------------------------ 👥 izlənənlər / kanal filtri
    async def cmd_following(self, cid, name: str) -> None:
        name = name.lstrip("@")
        if not NAME_RE.match(name):
            await self.reply(cid, "❌ Ad düzgün deyil.")
            return
        await self.reply(cid, f"⏳ <b>@{views.esc(name)}</b> hesabının izlədikləri axtarılır… (bir neçə saniyə)")
        res = await asyncio.to_thread(following.fetch_following, name, list(config.PROXIES))
        st, names = res["status"], res.get("names", [])
        if st == "notfound":
            await self.reply(cid, f"❌ <b>@{views.esc(name)}</b> hesabı tapılmadı.")
        elif st == "private":
            await self.reply(cid, f"🔒 <b>@{views.esc(name)}</b> hesabı gizlidir — izlədikləri siyahısı açıq deyil.")
        elif st in ("hidden", "blocked", "error") and not names:
            why = {"hidden": "izlədikləri siyahısı gizlidir (və ya TikTok bu sorğunu imza ilə qoruyub)", "blocked": "TikTok sorğunu blokladı, bir az sonra yenidən yoxlayın"}.get(st, "oxuna bilmədi")
            await self.reply(cid, f"⚠️ <b>@{views.esc(name)}</b>: {why}.\nSiyahını əl ilə .txt fayl kimi mənə göndərə bilərsiniz.")
        else:
            f = config.RESULTS_DIR / f"following_{name.lower()}.txt"
            config.RESULTS_DIR.mkdir(exist_ok=True)
            f.write_text("\n".join(names) + "\n", encoding="utf-8")
            tot = res.get("total") or (len(names) + res.get("private_n", 0))
            pn, un = res.get("private_n", 0), res.get("unknown_n", 0)
            info = f"izlədiyi cəmi <b>{tot}</b> • oxunan <b>{len(names) + pn}</b> • 🔓 açıq <b>{len(names)}</b> • 🔒 gizli (çıxarıldı) <b>{pn}</b>"
            if un:
                info += f"\nℹ️ {un} hesabın açıq/gizli olduğu TikTok-dan gəlmədi, onlar siyahıda saxlanıldı."
            await tg.send_document(cid, f, caption=f"👥 @{name}: açıq hesablar {len(names)} (gizli {pn} çıxarıldı)")
            await self.reply(cid, f"👥 <b>@{views.esc(name)}</b>\n{info}\n\n➕ Açıq olanları izləmə siyahısına (users.txt) əlavə edim?",
                             [[{"text": "✅ Bəli, əlavə et", "callback_data": f"fw:add:{name.lower()}"}, {"text": "❌ Xeyr", "callback_data": "fw:no"}]])

    async def cb_follow(self, cid, mid, rest: str) -> str:
        op, _, name = rest.partition(":")
        if op != "add":
            await tg.edit_message(cid, mid, "↩ Siyahıya əlavə edilmədi.")
            return "↩"
        f = config.RESULTS_DIR / f"following_{name}.txt"
        try:
            names = f.read_text(encoding="utf-8").split()
            r = userlist.add_names(names)
        except OSError as exc:
            await tg.edit_message(cid, mid, f"❌ Fayl oxunmadı: <code>{views.esc(exc)}</code>")
            return "❌"
        await tg.edit_message(
            cid, mid,
            f"✅ <b>Siyahıya əlavə olundu</b>\n➕ Yeni: <b>{r['added']}</b> • ♻️ Təkrar: <b>{r['dup']}</b> • 🗑 Yararsız: <b>{len(r['bad'])}</b>\n📋 users.txt: <b>{views.num(r['total'])}</b> ad",
            keyboard=[[{"text": "🔁 İndi yoxla", "callback_data": "ctl:rescan"}]])
        return "✅ Əlavə olundu"

    async def cmd_channel_filter(self, cid, args: str) -> None:
        p = args.split()
        chans = settings["channels"]
        try:
            ch = chans[int(p[0]) - 1]
            vals = [float(x) for x in p[1:4]]
        except (ValueError, IndexError):
            await self.reply(cid, "🎚 İstifadə: <code>/kanal_filtr N ratio jeton izləyici</code>\nMəs: <code>/kanal_filtr 1 2 100 500</code> → 1-ci kanal: ratio≥2, jeton≥100, izləyici≥500 (0 = süzgəc yoxdur). Nömrələr: /kanallar")
            return
        for key, v in zip(("min_ratio", "min_coins", "min_viewers"), vals):
            ch[key] = v if key == "min_ratio" else int(v)
        settings.save()
        await self.reply(cid, "✅ Saxlandı.\n" + views.chan_html(chans.index(ch)), views.chan_kb(chans.index(ch)))

    # ------------------------------------------------------------------ 🛠 Hazırla
    async def check_channels(self) -> list:
        """Hər kanal/qrup üçün: bot orada yaza bilirmi? [(ad, ok, qeyd)]"""
        out = []
        me = tg.BOT.get("id")
        if not me:
            ok, info = await tg.call("getMe")
            if ok:
                tg.BOT.update(info)
                me = info.get("id")
        for c in settings["channels"]:
            title = c.get("title") or str(c["id"])
            if not c.get("enabled", True):
                out.append((title, False, "söndürülüb (/kanallar)"))
                continue
            ok, m = await tg.call("getChatMember", {"chat_id": c["id"], "user_id": me}, retries=1)
            if not ok:
                out.append((title, False, f"bot orada yoxdur / yazmaq olmur: {str(m)[:60]}"))
                continue
            st = m.get("status")
            if st == "administrator":
                if m.get("can_post_messages") is False:
                    out.append((title, False, "admin var, amma «Mesaj göndər» icazəsi bağlıdır"))
                else:
                    out.append((title, True, "admin ✔ mesaj göndərə bilir"))
            elif st == "member":
                ok2, ch = await tg.call("getChat", {"chat_id": c["id"]}, retries=1)
                perms = (ch or {}).get("permissions") if ok2 else None
                if (ch or {}).get("type") == "channel":
                    out.append((title, False, "kanalda yalnız admin yaza bilər → botu admin edin"))
                elif perms is not None and not perms.get("can_send_messages", True):
                    out.append((title, False, "üzvlərə yazmaq qadağandır → botu admin edin"))
                else:
                    out.append((title, True, "üzv kimi yaza bilir (admin etsəniz daha etibarlı)"))
            elif st == "restricted":
                out.append((title, bool(m.get("can_send_messages")), "məhdudlaşdırılıb" if not m.get("can_send_messages") else "məhdud, amma yaza bilir"))
            else:
                out.append((title, False, f"status: {st} → botu qrupa əlavə edin / admin edin"))
        return out

    # ------------------------------------------------------------------ mənbə kanal (başqa botun paylaşdığı linklər)
    @staticmethod
    def _post_names(post: dict) -> list[str]:
        """Postdakı mətn, link (entity) və düymə URL-lərindən TikTok adlarını çıxarır."""
        urls = [post.get("text") or "", post.get("caption") or ""]
        for key in ("entities", "caption_entities"):
            for e in post.get(key) or []:
                if e.get("url"):
                    urls.append(e["url"])
        for row in (post.get("reply_markup") or {}).get("inline_keyboard") or []:
            for b in row:
                if b.get("url"):
                    urls.append(b["url"])
        out, seen = [], set()
        for m in re.finditer(r"tiktok\.com/@([A-Za-z0-9._]+)", "\n".join(urls)):
            n = m.group(1).rstrip(".")
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
        return out

    async def on_source_post(self, post: dict) -> None:
        srcs = {str(x) for x in settings.data.get("sources", [])}
        if str((post.get("chat") or {}).get("id")) not in srcs:
            return
        names = self._post_names(post)
        if not names:
            return
        r = userlist.add_names(names)
        with open(config.RESULTS_DIR / "kanal_users.txt", "a", encoding="utf-8") as fh:
            fh.write("\n".join(names) + "\n")
        log(f"[MƏNBƏ] {len(names)} ad tapıldı, yeni: {r['added']}")
        if r["new_names"]:
            asyncio.create_task(self._quick_join_new(config.ADMIN_IDS[0], r["new_names"]))

    async def cmd_source(self, cid, args: str) -> None:
        srcs = settings.data.setdefault("sources", [])
        a = args.strip()
        if a.lower().startswith(("sil", "del")):
            settings.data["sources"] = []
            settings.save()
            await self.reply(cid, "🗑 Mənbə kanallar silindi.")
            return
        if not a:
            await self.reply(cid, "📥 <b>MƏNBƏ KANAL</b>\n" + views.LINE + "\n" + (f"Aktiv: <b>{len(srcs)}</b> kanal\n" if srcs else "Mənbə yoxdur.\n")
                             + "➕ <code>/menbe @kanal</code> (və ya <code>-100...</code>)\n🗑 <code>/menbe sil</code>\n"
                             "ℹ️ Botu həmin kanala <b>admin</b> edin: yeni postlardakı TikTok linklərindən adlar avtomatik götürülür, users.txt-ə əlavə olunur və canlılara qoşulur.\n"
                             "📜 Köhnə postların hamısı üçün: <code>python kanal_cek.py @kanal</code>")
            return
        ok, chat = await tg.call("getChat", {"chat_id": a if a.startswith("@") else int(a)}, retries=1)
        if not ok:
            await self.reply(cid, f"❌ Kanal tapılmadı: <code>{views.esc(str(chat)[:150])}</code>\nBot kanalda admin olmalıdır.")
            return
        if chat["id"] not in srcs:
            srcs.append(chat["id"])
            settings.save()
        await self.reply(cid, f"✅ Mənbə əlavə olundu: <b>{views.esc(chat.get('title') or a)}</b>\nBundan sonrakı postlardakı TikTok adları avtomatik götürüləcək.")

    async def open_group(self, cid) -> str:
        """Say məlumatını (neçə canlı / neçə offline) indi qrup/kanallara göndər (başqa heç nə, ad siyahısı yox)."""
        import group_feed

        c = self.ctl
        if not c.active and c.phase != "paused":
            c.request_start(False)
        chans = settings.active_channels("chest")
        if not chans:
            await self.reply(cid, "📭 Qrup/kanal yoxdur → <code>/kanal_elave @kanal</code>")
            return "📭 Kanal yoxdur"
        n = await group_feed.summary(c, force=True)
        if not n:
            await self.reply(cid, "ℹ️ Hələ yoxlanmış ad yoxdur — skan başlayanda məlumat qrupa düşəcək.")
            return "ℹ️ Hələ məlumat yoxdur"
        await self.reply(cid, f"📤 <b>Qrupa göndərildi:</b> say məlumatı • {n} qrup/kanal.\nQrupa yalnız sandıqlı canlı xəbəri və bu say məlumatı gedir (ad siyahısı yox).")
        return f"📤 {n} qrupa göndərildi"

    async def start_prepare(self, cid, mid=None) -> None:
        """🛠 Hazırla: skan və ordu arxa planda başlayır, kanal icazələri yoxlanır, irəliləyiş canlı yenilənən mesajda görünür."""
        c = self.ctl
        note = ""
        if c.phase == "paused":
            c.resume()
            note = "▶️ fasilədən çıxarıldı"
        elif not c.active and c.request_start(False):
            note = "🚀 skan başladıldı"
        old = getattr(self, "_prep_task", None)
        if old and not old.done():
            old.cancel()
        text = views.prepare_html(c, None) + (f"\n{note}" if note else "")
        if mid is None:
            mid = await tg.send_message(cid, text, keyboard=views.prepare_kb())
        else:
            ok, _ = await tg.edit_message(cid, mid, text, keyboard=views.prepare_kb())
            if not ok:
                mid = await tg.send_message(cid, text, keyboard=views.prepare_kb())
        if mid:
            self._prep_task = asyncio.create_task(self._prepare_loop(cid, mid))

    async def _prepare_loop(self, cid, mid) -> None:
        try:
            rep = await self.check_channels()
            bad = [t for t, ok, _ in rep if not ok]
            if bad:
                await tg.send_message(cid, "⚠️ <b>Diqqət:</b> bu kanal/qruplara hələ yaza bilmirəm: " + ", ".join(f"<b>{views.esc(b)}</b>" for b in bad)
                                      + "\n👉 Botu həmin yerdə <b>admin</b> edin (Mesaj göndərmə icazəsi ilə). Admin etdiyiniz an özüm təsdiq edəcəm.")
            for i in range(30):                      # ~2.5 dəqiqə canlı yenilənir
                await tg.edit_message(cid, mid, views.prepare_html(self.ctl, rep, final=False), keyboard=views.prepare_kb())
                await asyncio.sleep(5)
                if not self.ctl.active:
                    break
            await tg.edit_message(cid, mid, views.prepare_html(self.ctl, rep, final=True), keyboard=views.prepare_kb())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"[BOT] hazırlıq xətası: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ parametrlər
    def apply_preset(self, key: str) -> str:
        if key not in views.PRESETS:
            return ""
        label, w, i, _ = views.PRESETS[key]
        return self.apply_speed(w, i, label)

    def apply_speed(self, workers: int, interval: float, label: str = "") -> str:
        workers = max(1, min(workers, config.SCAN_WORKER_CAP))
        interval = max(0.2, min(interval, config.SCAN_MAX_SPACING))
        settings.set("speed", {"workers": workers, "interval": interval})
        if self.ctl.sched is not None:
            self.ctl.sched.set_speed(workers, interval)
        return f"{label or '⚡'} {workers} paralel • {interval:g}s"

    async def cmd_speed(self, cid, args: str) -> None:
        if not args:
            await self.reply(cid, views.speed_html(self.ctl), views.speed_kb())
            return
        parts = args.replace(",", ".").split()
        if parts[0].lower() in views.PRESETS:
            msg = self.apply_preset(parts[0].lower())
        else:
            try:
                w = int(float(parts[0]))
                i = float(parts[1]) if len(parts) > 1 else settings["speed"]["interval"] if settings["speed"] else config.SCAN_SPACING
            except (ValueError, IndexError, TypeError):
                await self.reply(cid, "❌ İstifadə: <code>/surat 2 1.0</code> (paralel, interval san) və ya <code>/surat safe|normal|fast|turbo</code>")
                return
            msg = self.apply_speed(w, i)
        await self.reply(cid, f"✅ <b>Sürət dəyişdi:</b> {msg}\n💾 Yadda saxlanıldı. Bloklasa bot özü avtomatik yavaşlayacaq.", views.speed_kb())

    async def cmd_fixed(self, cid, args: str) -> None:
        cur = settings["fixed_rate"]
        a = args.replace(",", ".").strip().lower()
        if not a:
            now = f"<b>{cur:g}</b> ad/saniyə (sabit)" if cur else "söndürülü (sürət blokdan asılı dəyişir)"
            await self.reply(cid, f"🔒 Sabit sürət: {now}\nQurmaq: <code>/sabit 1</code> (saniyədə 1 ad)\nSöndürmək: <code>/sabit off</code>")
            return
        if a in ("off", "0", "sondur", "söndür"):
            settings.set("fixed_rate", None)
            if self.ctl.sched is not None:
                self.ctl.sched.set_fixed(None)
                sp = settings["speed"] or {"workers": 1, "interval": config.SCAN_SPACING}
                self.ctl.sched.set_speed(int(sp["workers"]), float(sp["interval"]))
            await self.reply(cid, "🔓 Sabit sürət söndürüldü. Sürət yenidən /surat ilə idarə olunur.")
            return
        try:
            rate = max(0.1, min(float(a.split()[0]), 10.0))
        except ValueError:
            await self.reply(cid, "❌ İstifadə: <code>/sabit 1</code> (saniyədə 1 ad) və ya <code>/sabit off</code>")
            return
        settings.set("fixed_rate", rate)
        if self.ctl.sched is not None:
            self.ctl.sched.set_fixed(rate)
        await self.reply(cid, f"🔒 <b>Sabit sürət: {rate:g} ad/saniyə.</b> Blok olsa belə bu rəqəm dəyişmir (yalnız qısa soyuma var).\n💾 Yadda saxlanıldı.")

    async def cmd_filter(self, cid, args: str) -> None:
        if not args:
            await self.reply(
                cid,
                f"🎚 <b>Kanal filtri</b>\nratio ≥ <b>{settings['ch_min_ratio']:g}</b> • jeton ≥ <b>{settings['ch_min_coins']}</b>\n"
                "Dəyişmək: <code>/filtr 1.5 10</code> (ratio, jeton)\nSıfırlamaq: <code>/filtr 0 0</code>\n<i>Admin bütün sandıqları görür, filtr yalnız kanallara aiddir.</i>",
            )
            return
        parts = args.replace(",", ".").split()
        try:
            ratio = float(parts[0])
            coins = int(float(parts[1])) if len(parts) > 1 else settings["ch_min_coins"]
        except ValueError:
            await self.reply(cid, "❌ İstifadə: <code>/filtr 1.5 10</code>")
            return
        settings.set("ch_min_ratio", max(ratio, 0.0))
        settings.set("ch_min_coins", max(coins, 0))
        await self.reply(cid, f"✅ Kanal filtri: ratio ≥ <b>{settings['ch_min_ratio']:g}</b> • jeton ≥ <b>{settings['ch_min_coins']}</b>")

    async def cmd_report_interval(self, cid, args: str) -> None:
        try:
            mins = float(args.replace(",", "."))
        except ValueError:
            cur = settings["report_every"]
            await self.reply(cid, f"📨 Ara hesabat: <b>{'söndürülü' if cur <= 0 else fmt_duration(cur)}</b>\nDəyişmək: <code>/hesabat_vaxt 10</code> (dəqiqə, 0 = söndür)")
            return
        secs = 0 if mins <= 0 else max(int(mins * 60), 60)
        settings.set("report_every", secs)
        await self.reply(cid, "📨 Ara hesabat <b>söndürüldü</b>." if secs == 0 else f"📨 Ara hesabat hər <b>{fmt_duration(secs)}</b>.")

    async def cmd_ignore(self, cid, args: str) -> None:
        ign = settings["ignore"]
        if not args:
            shown = ", ".join("@" + views.esc(x) for x in ign[:40]) or "boşdur"
            await self.reply(cid, f"🙈 <b>İstisna adlar</b> ({len(ign)}): {shown}\n➕ <code>/istisna ad1 ad2</code>\n➖ <code>/istisna sil ad1</code>")
            return
        parts = userlist.parse_names(args)
        if parts and parts[0].lower() == "sil":
            gone = [p for p in parts[1:] if p.lower() in {x.lower() for x in ign}]
            settings.set("ignore", [x for x in ign if x.lower() not in {g.lower() for g in gone}])
            await self.reply(cid, f"➖ Çıxarıldı: <b>{len(gone)}</b>")
            return
        have = {x.lower() for x in ign}
        new = [p for p in parts if NAME_RE.match(p) and p.lower() not in have]
        settings.set("ignore", ign + new)
        await self.reply(cid, f"🙈 İstisnaya əlavə: <b>{len(new)}</b> (cəmi {len(settings['ignore'])}). Növbəti skanda atılacaq.")

    # ------------------------------------------------------------------ siyahı / fayl
    async def on_document(self, cid, doc: dict) -> None:
        name = (doc.get("file_name") or "").lower()
        if not (name.endswith(".txt") or (doc.get("mime_type") or "").startswith("text/")):
            await self.reply(cid, "ℹ️ Yalnız <b>.txt</b> fayl qəbul edirəm (hər sətirdə bir TikTok adı).")
            return
        data = await tg.download_file(doc["file_id"])
        if data is None:
            await self.reply(cid, "❌ Fayl yüklənmədi (5 MB-dan böyükdür və ya xəta).")
            return
        self.awaiting_users.discard(cid)
        await self.add_users(cid, data.decode("utf-8", errors="ignore"))

    async def add_users(self, cid, text: str) -> None:
        names = userlist.parse_names(text)
        if not names:
            await self.reply(cid, "ℹ️ Heç bir ad tapılmadı.")
            return
        try:
            r = userlist.add_names(names)
        except OSError as exc:
            await self.reply(cid, f"❌ users.txt-ə yazıla bilmir: <code>{views.esc(exc)}</code>")
            return
        bad = f"\n🗑 Yararsız: <b>{len(r['bad'])}</b> {views.esc(', '.join(r['bad'][:5]))}" if r["bad"] else ""
        await self.reply(
            cid,
            f"✅ <b>Siyahı yeniləndi</b>\n➕ Yeni: <b>{r['added']}</b>\n♻️ Təkrar: <b>{r['dup']}</b>{bad}\n📋 users.txt: <b>{views.num(r['total'])}</b> ad\n"
            "⚡ Yeni adlar dərhal yoxlanır, canlı olanlara ordu qoşulur. Nəticə aşağıda gələcək.",
        )
        asyncio.create_task(self._quick_join_new(cid, r["new_names"]))

    async def _quick_join_new(self, cid, names: list[str]) -> None:
        """Telegram-dan yüklənən yeni adları növbəti skanı gözləmədən yoxlayıb canlılara qoşulur."""
        import fleet
        import worker_scanner

        if fleet.snapshot() is None:
            await self.reply(cid, "ℹ️ Ordu hazırda işləmir; yeni adlar <b>növbəti skanda</b> yoxlanacaq (/baslat).")
            return
        names = [n for n in names if NAME_RE.match(n)][:3000]
        sem = asyncio.Semaphore(20)
        cnt = {"live": 0, "joined": 0, "err": 0}

        async def one(n: str) -> None:
            async with sem:
                try:
                    status, room, _ = await worker_scanner.priority_check(n, self.ctl)
                except Exception:
                    cnt["err"] += 1
                    return
                if status == "live" or (status not in ("offline", "notfound", "blocked", "error") and room):
                    cnt["live"] += 1
                    if fleet.join(n, room, 0) == "ok":
                        cnt["joined"] += 1
                elif status in ("blocked", "error"):
                    cnt["err"] += 1

        await asyncio.gather(*(one(n) for n in names))
        await self.reply(
            cid,
            f"🔎 <b>Yeni adların ani yoxlaması</b>\n📋 Yoxlanan: <b>{len(names)}</b>\n🟢 Canlı: <b>{cnt['live']}</b>\n🔌 Ordu qoşuldu: <b>{cnt['joined']}</b>"
            + (f"\n⚠️ Yoxlanmadı (blok/xəta): <b>{cnt['err']}</b> — növbəti skanda təkrar yoxlanacaq" if cnt["err"] else ""),
        )

    async def send_files(self, cid) -> None:
        sent = 0
        for name, label in (("live.txt", "🟢 Canlılar"), ("chests.txt", "📦 Sandıqlar"), ("failed.txt", "⚠ Yoxlanmayanlar"), ("notfound.txt", "❌ Tapılmayanlar"), ("luckybag.txt", "🛍 Lucky Bag"), ("methods_seen.txt", "🧬 Gələn mesaj növləri"), ("method_samples.txt", "🔬 Naməlum mesaj nümunələri")):
            f = config.RESULTS_DIR / name
            if f.exists() and f.stat().st_size > 0:
                n = sum(1 for _ in f.open(encoding="utf-8", errors="ignore"))
                ok, _ = await tg.send_document(cid, f, f"{label} — {n} sətir")
                sent += 1 if ok else 0
        if not sent:
            await self.reply(cid, "📭 Göndəriləcək nəticə faylı hələ yoxdur.")

    async def send_log(self, cid, n: int) -> None:
        lines = [l for l in list(LINES)[-n:]]
        body = views.esc("\n".join(lines))
        while len(body) > 3500 and lines:
            lines.pop(0)
            body = views.esc("\n".join(lines))
        await self.reply(cid, f"🧾 <b>Son {len(lines)} log</b>\n<pre>{body or 'boşdur'}</pre>")

    # ------------------------------------------------------------------ panel
    async def create_panel(self, cid) -> None:
        await self.drop_panel()
        mid = await tg.send_message(cid, views.status_html(self.ctl, "📡 <b>CANLI PANEL</b>"), keyboard=views.status_kb(self.ctl), silent=True)
        if mid:
            settings.set("panel", {"chat_id": cid, "message_id": mid})
            await tg.call("pinChatMessage", {"chat_id": cid, "message_id": mid, "disable_notification": True}, retries=1)
            await self.reply(cid, f"📡 Canlı panel yaradıldı və yuxarı sancaqlandı. Hər <b>{config.PANEL_REFRESH}s</b> yenilənir.\nSöndürmək: /panel_off")

    async def drop_panel(self) -> None:
        p = settings["panel"]
        if p:
            await tg.call("deleteMessage", {"chat_id": p["chat_id"], "message_id": p["message_id"]}, retries=1)
            settings.set("panel", None)

    # ------------------------------------------------------------------ /yoxla
    async def cmd_check(self, cid, args: str) -> None:
        names, secs = _split_secs(userlist.parse_names(args))
        names = [n for n in names if NAME_RE.match(n)][:5]
        if not names:
            await self.reply(cid, "🔎 İstifadə: <code>/yoxla ad</code>  (məsələn <code>/yoxla mssxanaqleey</code>)\nSadəcə adı yazsanız da olar.")
            return
        for name in names:
            asyncio.create_task(self._manual_check(cid, name, secs))

    # ------------------------------------------------------------------ /qosul
    async def cmd_join(self, cid, args: str) -> None:
        names = [n for n in userlist.parse_names(args) if NAME_RE.match(n)][:10]
        if not names:
            await self.reply(cid, "🔌 İstifadə: <code>/qosul istifadəçi_adı</code>  (məsələn <code>/qosul mssxanaqleey</code>)\nBir neçə ad: <code>/qosul ad1 ad2 ad3</code>. Canlıdırsa ordu dərhal qoşulur və qoşulanda xəbər verir.")
            return
        for name in names:
            asyncio.create_task(self._manual_join(cid, name))

    # ------------------------------------------------------------------ /canta (öyrətmə + kəşf)
    async def cmd_learn(self, cid, args: str) -> None:
        import fleet
        import luckybag
        import worker_scanner

        names = [n for n in userlist.parse_names(args) if NAME_RE.match(n)]
        if not names:
            await self.reply(cid, "🎓 <b>Hədiyyə çantasını öyrət</b>\n1) Çantanı qoyan yayımçının adını yaz: <code>/canta ad</code>\n"
                                  "2) Bot qoşulub 20 san baza yığır, sonra dinləyir\n3) Çanta qoyulanda bot serverdən gələn yeni mesaj növünü tapıb əlavə edir.\n"
                                  "💡 Özün qoymaq lazım deyil: bot bütün qoşulu yayımlardan nadir mesaj növlərini özü toplayır — bax: /cantalar")
            return
        name = names[0]
        u = views.esc(name)
        if fleet.snapshot() is None:
            await self.reply(cid, "⚠️ Ordu hazırda işləmir. Əvvəl /baslat yazın.")
            return
        status, room, _note = await worker_scanner.priority_check(name, self.ctl)
        if status != "live" and not room:
            await self.reply(cid, f"⚠️ <b>@{u}</b> hazırda canlı görünmür ({status}). Canlı yayımda olan hesab yaz.")
            return
        if not fleet.is_open(name):
            await self.reply(cid, f"🔌 <b>@{u}</b> yayımına qoşuluram…")
            fleet.join(name, room, cid)
            for _ in range(45):
                await asyncio.sleep(1)
                if fleet.is_open(name):
                    break
            else:
                await self.reply(cid, f"⚠️ <b>@{u}</b> yayımına 45 san-də qoşula bilmədim. Bir az sonra yenidən yaz.")
                return

        async def say(t: str) -> None:
            await self.reply(cid, t)

        ln = luckybag.start_learn(name, room, config.RESULTS_DIR, say)
        if ln is None:
            await self.reply(cid, "ℹ️ Bu yayım üçün öyrənmə artıq gedir.")
            return

        async def go() -> None:
            report = await ln.run()
            await self.reply(cid, report)
            if ln.out.exists():
                await tg.send_document(cid, ln.out, f"🎓 @{name} — bütün yeni mesajlar (lazım olsa Claude-a göndərin)")

        asyncio.create_task(go())

    async def cmd_canta_test(self, cid) -> None:
        import luckybag

        pl = bytes([0x0A, 0x09]) + b"TEST-BAG!" + bytes([0x10, 0x96, 0x01, 0x18, 0xC8, 0x01])
        for tier, method in (("sure", "WebcastLuckyBagMessage"), ("maybe", "WebcastFooMessage")):
            info = luckybag.extract(method, pl)
            info["tier"] = tier
            text = "🧪 <b>TEST (həqiqi çanta deyil)</b>\n" + luckybag.telegram_text("test_hesab", info, 1)
            await self.reply(cid, text)
        await self.reply(cid, f"✅ Xəbər yolu işləyir. Açar söz: <b>{sum(luckybag.KW_COUNT.values())}</b> • öyrənilmiş növ: <b>{len(luckybag.LEARNED)}</b>.")

    async def cmd_methods(self, cid) -> None:
        import luckybag

        path = config.RESULTS_DIR / "methods_seen.txt"
        n = luckybag.dump_methods(path)
        if not n:
            await self.reply(cid, "ℹ️ Hələ mesaj yığılmayıb (bot yayımlara qoşulmayıb).")
            return
        await tg.send_document(cid, path, f"📄 {n} mesaj növü (nadirlər əvvəl). Lucky Bag adı bilinmir — bu faylı Claude-a göndər.")

    async def cmd_canta_list(self, cid) -> None:
        import fleet
        import luckybag

        L = ["📋 <b>Lucky Bag mesaj növləri</b>", "━━━━━━━━━━━━━━━━"]
        if luckybag.LEARNED:
            L.append("✅ Öyrənilmiş (həmişə Lucky Bag sayılır):")
            L += [f"  • <code>{views.esc(m)}</code>" for m in luckybag.LEARNED.values()]
        else:
            L.append("✅ Öyrənilmiş: yoxdur.")
        fl = fleet._FLEET
        n_open = len(fl.stats.fleet_open) if fl else 0
        rows = luckybag.suspects(n_open)[:12]
        if rows:
            L += ["", "🔎 <b>Şübhəli nadir mesaj növləri</b> (az yayımda, amma təkrar gələnlər):"]
            for m, rooms, total, hinted in rows:
                who = [fl.room_user.get(str(r), "?") for r in list(rooms)[:3]] if fl else []
                inf = luckybag.sample_of(m) or {}
                L.append(f"{'🟡' if hinted else '▫️'} <code>{views.esc(m)}</code> ×{total} ({len(rooms)} yayım: {', '.join('@' + views.esc(x) for x in who)})"
                         + (f"\n    🏷 {views.esc(' | '.join(inf.get('strings', [])[:2]))[:100]}" if inf.get("strings") else ""))
            L.append("\nHansısa çantadırsa: <code>/canta_elave ad</code> — o mesajı göndərən bütün yayımçılar izləməyə düşəcək.")
        else:
            L.append("\n🔎 Hələ şübhəli nadir mesaj növü yoxdur (bot bütün qoşulu yayımlardan toplayır; bir neçə dəqiqə gözlə).")
        await self.reply(cid, "\n".join(L))

    async def cmd_canta_edit(self, cid, args: str, add: bool) -> None:
        import fleet
        import luckybag

        m = args.strip().split()[0] if args.strip() else ""
        if not m:
            await self.reply(cid, "İstifadə: <code>/canta_elave MesajAdı</code> • <code>/canta_sil MesajAdı</code> (adı /cantalar-dan kopyala)")
            return
        ok = luckybag.learn(m) if add else luckybag.forget(m)
        await self.reply(cid, (f"✅ <code>{views.esc(m)}</code> artıq Lucky Bag sayılır." if add else f"🗑 <code>{views.esc(m)}</code> silindi.") if ok
                         else ("ℹ️ Artıq siyahıdadır." if add else "ℹ️ Siyahıda belə ad yoxdur."))
        if add:
            names = fleet.adopt_hosts(m)
            if names:
                await self.reply(cid, f"👁 Bu mesajı artıq göndərmiş <b>{len(names)}</b> yayımçı izləməyə alındı:\n" + ", ".join(f"@{views.esc(n)}" for n in names[:15])
                                      + "\nOrdu onlardan qopmayacaq, yayımları bitib qayıdanda yenidən qoşulacaq. Siyahı: /izle_siyahi")

    # ------------------------------------------------------------------ /izle
    async def cmd_watch(self, cid, args: str) -> None:
        names = [n for n in userlist.parse_names(args) if NAME_RE.match(n)][:20]
        if not names:
            await self.cmd_watchlist(cid)
            return
        have = [x.lower() for x in settings["watch"]]
        added = []
        for n in names:
            if n.lower() not in have:
                have.append(n.lower())
                added.append(n.lower())
        settings.set("watch", have)
        txt = ("👁 <b>İzləməyə əlavə olundu:</b> " + ", ".join(f"@{views.esc(n)}" for n in added)) if added else "ℹ️ Bu hesablar artıq izlənir."
        await self.reply(cid, f"{txt}\nYayıma başlayan kimi ordu qoşulacaq və sandıq <b>həmişə</b> (filtrə baxmadan) bura gələcək. Siyahı: /izle_siyahi")
        import fleet
        if fleet.snapshot() is not None:
            for n in names:
                asyncio.create_task(self._manual_join(cid, n.lower(), watching=True))

    async def cmd_watchlist(self, cid) -> None:
        import fleet
        items = settings["watch"]
        if not items:
            await self.reply(cid, "👁 İzlənən hesab yoxdur.\nƏlavə etmək: <code>/izle ad</code> (bir neçə ad: <code>/izle a b c</code>)")
            return
        mark = {"open": "🔌 qoşulu", "joining": "⏳ qoşulur", "idle": "⚪ yayım gözlənilir"}
        L = [f"👁 <b>İZLƏNƏN HESABLAR</b> ({len(items)})", "━━━━━━━━━━━━━━━━━━━━"]
        for n in items:
            L.append(f'<a href="https://www.tiktok.com/@{views.esc(n)}/live">@{views.esc(n)}</a> — {mark[fleet.watch_state(n)]}')
        L.append("━━━━━━━━━━━━━━━━━━━━")
        L.append(f"Hər {config.WATCH_EVERY}s yoxlanılır • silmək: <code>/izle_sil ad</code>")
        await self.reply(cid, "\n".join(L))

    async def cmd_unwatch(self, cid, args: str) -> None:
        names = {n.lower() for n in userlist.parse_names(args)}
        if not names:
            await self.reply(cid, "🗑 İstifadə: <code>/izle_sil ad</code>")
            return
        keep = [x for x in settings["watch"] if x.lower() not in names]
        removed = len(settings["watch"]) - len(keep)
        settings.set("watch", keep)
        await self.reply(cid, f"🗑 {removed} hesab izləmədən silindi." if removed else "ℹ️ Bu ad izləmə siyahısında yoxdur.")

    async def _manual_join(self, cid, name: str, watching: bool = False) -> None:
        import fleet
        import worker_scanner

        u = views.esc(name)
        link = [[{"text": "🔴 Yayıma keç", "url": f"https://www.tiktok.com/@{name}/live"}]]
        if fleet.snapshot() is None:
            await self.reply(cid, "⚠️ Ordu hazırda işləmir. Əvvəl /baslat yazın.")
            return
        if fleet.is_open(name):
            await self.reply(cid, f"✅ <b>@{u}</b> yayımına artıq qoşuluyam.", link)
            return
        await self.reply(cid, f"🔎 <b>@{u}</b> yoxlanılır…")
        status, room, _note = await worker_scanner.priority_check(name, self.ctl)
        if status == "offline":
            await self.reply(cid, f"⚪ <b>@{u}</b> hazırda canlı deyil." + (" İzləyirəm, yayıma başlayanda qoşulacağam." if watching else ""))
        elif status == "notfound":
            await self.reply(cid, f"❌ <b>@{u}</b> hesabı tapılmadı." + (" İzləmə siyahısından silmək üçün: /izle_sil " + name if watching else ""))
        elif status in ("blocked", "error"):
            await self.reply(cid, f"⚠️ <b>@{u}</b> yoxlanmadı ({'TikTok blok etdi' if status == 'blocked' else 'şəbəkə xətası'})." + (" İzləmə davam edir, canlı olanda qoşulacağam." if watching else " Bir az sonra yenidən yazın."))
        else:
            r = fleet.join(name, room, cid)
            if r == "nofleet":
                await self.reply(cid, "⚠️ Ordu hazırda işləmir. Əvvəl /baslat yazın.")
            elif r == "already":
                await self.reply(cid, f"⏳ <b>@{u}</b> artıq ordunun siyahısındadır, qoşulmağa çalışır. Qoşulanda bildirəcəm.", link)
            else:
                await self.reply(cid, f"🟢 <b>@{u}</b> CANLIDIR. Ordu qoşulur… qoşulanda xəbər verəcəm.", link)

    # ------------------------------------------------------------------ /probe
    async def cmd_probe(self, cid, args: str) -> None:
        """Sandığı ARTIQ olan canlı yayımda: bütün ilkin mesajları və room/info-nu faylа yazıb Telegram-a göndərir."""
        import probe_chest

        mobile = "mobile" in args.lower() or "mobil" in args.lower()
        api = " api" in f" {args.lower()}"
        names = [n for n in userlist.parse_names(args) if NAME_RE.match(n)]
        if not names:
            await self.reply(cid, "🧪 İstifadə: <code>/probe ad</code> (mobil platforma: <code>/probe ad mobile</code>) — sandığı artıq qoyulmuş canlı yayımda yazın (~40 san). Gələn faylı mənə göndərin.")
            return
        name = names[0]
        await self.reply(cid, f"🧪 <b>@{views.esc(name)}</b> yayımına qoşulub ilkin məlumatı yığıram (~45 san{', API yoxlaması ilə ~2 dəq' if api else ''})…")
        try:
            await probe_chest.main(name, 40, mobile=mobile, api=api)
        except Exception as exc:
            await self.reply(cid, f"⚠️ probe xətası: {views.esc(type(exc).__name__)}: {views.esc(str(exc)[:300])}")
            return
        f = config.RESULTS_DIR / f"probe_{name}{'_mobile' if mobile else ''}{'_api' if api else ''}.json"
        if not f.exists():
            await self.reply(cid, "⚠️ probe faylı yaranmadı.")
            return
        ok, err = await tg.send_document(cid, f, f"🧪 probe @{name} — bu faylı Claude-a göndərin")
        if not ok:
            await self.reply(cid, f"⚠️ fayl göndərilmədi: {views.esc(str(err)[:200])}. Fayl: results/probe_{views.esc(name)}.json")

    async def _manual_check(self, cid, name: str, secs: int | None = None) -> None:
        """Öncəlikli: növbəni və skanı gözləmir, canlıdırsa dərhal yayıma qoşulub sandıq dinləyir."""
        import worker_chest
        import worker_scanner

        u = views.esc(name)
        link = [[{"text": "🔴 Yayıma keç", "url": f"https://www.tiktok.com/@{name}/live"}]]
        await self.reply(cid, f"🔎 <b>@{u}</b> öncəlikli yoxlanılır…")
        status, room, note = await worker_scanner.priority_check(name, self.ctl)
        if status == "offline":
            await self.reply(cid, f"⚪ <b>@{u}</b> hazırda canlı deyil.")
        elif status == "notfound":
            await self.reply(cid, f"❌ <b>@{u}</b> hesabı tapılmadı.")
        elif status in ("blocked", "error"):
            await self.reply(cid, f"⚠️ <b>@{u}</b> yoxlanmadı ({'TikTok blok etdi' if status == 'blocked' else 'xəta'}). Bir az sonra yenidən cəhd edin.")
        else:
            tag = f" • Euler: {note}" if note else ""
            quick = secs is None  # müddət yazılmayıbsa: sürətli rejim (sandıq yoxdursa tez bitir)
            secs = secs or MANUAL_LISTEN
            hint = "⚡ Qoşulub yoxlayıram…" if quick else f"⚡ Qoşulub <b>{secs}s</b> dinləyirəm…"
            await self.reply(cid, f"🟢 <b>@{u}</b> CANLIDIR{tag}\n{hint} (tam dinləmək üçün: <code>ad 90</code>)", link)
            out: dict = {}
            found = await worker_chest.listen_once(name, room, self.ctl.stats, listen_seconds=secs, out=out, quick=quick)
            types = sorted((out.get("types") or {}).items(), key=lambda kv: -kv[1])
            seen = ", ".join(f"{views.esc(k)} {v}" for k, v in types[:12]) or "heç nə gəlmədi"
            head = f"📦 <b>@{u}</b>: <b>{found}</b> sandıq hadisəsi görüldü." if found else f"📭 <b>@{u}</b>: bu müddətdə sandıq görünmədi."
            probe = f"\n🔍 {views.esc(out['probe'])}" if out.get("probe") else ""
            meths = sorted((out.get("methods") or {}).items(), key=lambda kv: -kv[1])
            mtxt = ", ".join(f"{views.esc(k.replace('Webcast', ''))} {v}" for k, v in meths[:25])
            unk = ", ".join(f"{views.esc(k)} {v}" for k, v in sorted((out.get("unknown") or {}).items(), key=lambda kv: -kv[1])[:12])
            extra = (f"\n📨 Server mesajları: <i>{mtxt}</i>" if mtxt else "") + (f"\n❓ Tanınmayan: <i>{unk}</i>" if unk else "")
            watch = ""
            if config.MANUAL_WATCH_MIN > 0 and worker_chest.watch_manual(name, room, self.ctl.stats):
                watch = f"\n👁 Yayımda <b>{config.MANUAL_WATCH_MIN} dəq</b> qalıram: sonradan qoyulan sandıq da tutulacaq və bura gələcək."
            warn = f"\n⚠️ {views.esc(config.SESSION_PROBLEM)}" if config.SESSION_PROBLEM else ""
            await self.reply(cid, f"{head}\n📋 Gələn hadisələr: <i>{seen}</i>{probe}{extra}{watch}{warn}", link)


def esc_name(frm: dict) -> str:
    return views.esc(frm.get("first_name") or frm.get("username") or "admin")


def _int(text: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(text.split()[0]))))
    except (ValueError, IndexError):
        return default
