"""Telegram görünüşləri: emojili status paneli, top, son sandıqlar, ayarlar, kanallar, sürət, kömək."""
import html
import time

import config
import group_gate
import fleet
from settings import settings
from stats import fmt_duration

LINE = "━━━━━━━━━━━━━━━━━━━━"

PRESETS = {
    "safe": ("🐢 Təhlükəsiz", 1, 2.0, "blok riski ən aşağı"),
    "normal": ("🚶 Normal", 1, 1.2, "loqda blok vermədən işləyən sürət"),
    "fast": ("🚀 Sürətli", 2, 1.2, "2 paralel, bloklasa avtomatik azalır"),
    "turbo": ("⚡ Turbo", 3, 0.8, "yalnız proxy ilə məsləhətdir"),
    "max": ("🔥 Maks (20)", 20, 1.2, "hər proxy-də 20 paralel; blokda yarıya enir, özü geri qalxır"),
}

DEFAULT_SPEED = {"workers": 1, "interval": 1.2}  # loqda blok vermədən işləyən sürət (Normal)

PHASES = {
    "idle": "💤 GÖZLƏYİR",
    "running": "🟢 İŞLƏYİR",
    "paused": "⏸ FASİLƏDƏ",
    "stopping": "🛑 DAYANDIRILIR",
    "done": "✅ BİTDİ",
}


def esc(x) -> str:
    return html.escape(str(x))


def num(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def bar(pct: float, cells: int = 10) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * cells))
    return "🟩" * filled + "⬜" * (cells - filled)


def onoff(v: bool) -> str:
    return "🔔 açıq" if v else "🔕 söndürülü"


def current_speed(ctl) -> tuple[int, float]:
    if ctl.sched is not None:
        n = max(len(ctl.sched.lanes), 1)
        return max(1, round(ctl.sched.parallel_limit / n)), ctl.sched.avg_interval  # hər proxy-yə düşən paralel
    sp = settings["speed"] or DEFAULT_SPEED
    return int(sp["workers"]), float(sp["interval"])


# ------------------------------------------------------------------ STATUS
def fleet_html(ctl) -> list[str]:
    """Proxy → canlı bölməsi: neçə proxy işləyir, hər biri neçə canlıya qoşulub."""
    f = fleet.snapshot()
    L: list[str] = []
    if f is None:
        return L
    L += ["", "🪖 <b>ORDU (proxy → canlı)</b>"]
    if config.PROXIES:
        L.append(f"🌐 Proxy: ✅ <b>{f['up']}</b> işləyir • 🧊 <b>{f['down']}</b> dincəlir • cəmi {f['workers']}")
    else:
        L.append("🌐 Proxy YOXDUR — tək birbaşa IP (bloklana bilər)")
    if f.get("cooling"):
        L.append(f"🧊 Bloklanıb: <b>{f['cooling']}</b> — ən tez <b>{int(f['back_in'])} san</b>-dən sonra özü qayıdır")
    trying = max(f["tasks"] - f["open"], 0)
    L.append(f"✅ Həqiqətən qoşulu: <b>{f['open']}</b> canlı  •  ⏳ qoşulmağa çalışır: <b>{trying}</b>  •  tutum {f['capacity']}")
    L.append(f"🏅 Qoşulma uğuru olan proxy: <b>{f['proven']}</b>  •  istifadədə <b>{f['used']}</b>")
    top = [d for d in f["top"] if d["lives"] > 0]
    if top:
        L.append("📊 Ən yüklü proxy-lər:")
        for d in top[:5]:
            mark = "🟢" if d["state"] == "up" else "🧊"
            L.append(f"  {mark} <code>{esc(d['name'])}</code> — <b>{d['lives']}</b>/{d['cap']} canlı")
    if ctl.sched is not None and ctl.sched.lanes and ctl.sched.lanes[0].proxy:
        h = ctl.sched.health()
        L.append(f"🔎 Skan kanalları: ✅ <b>{h['up']}</b>/{h['total']} işləyir • sübut olunmuş <b>{h['proven']}</b> • 🧊 <b>{h['down']}</b> dincəlir")
    return L


def _list_lines(ctl) -> list:
    """Admin üçün bir baxışda: siyahıda neçə ad, neçəsi yoxlanıb, online/offline, neçəsinə qoşuluruq."""
    s = ctl.stats
    pct = (s.scanned / s.total * 100) if s.total else 0.0
    live_pct = (s.live / s.scanned * 100) if s.scanned else 0.0
    open_n = len(s.fleet_open)
    conn_pct = (open_n / s.live * 100) if s.live else 0.0
    return [
        "👥 <b>SİYAHI (bir baxışda)</b>",
        f"📚 Siyahıda: <b>{num(s.total)}</b> ad  •  ✅ Yoxlanıb: <b>{num(s.scanned)}</b> ({pct:.1f}%)  •  ⏳ Qalıb: <b>{num(s.unchecked)}</b>",
        f"🟢 Online: <b>{num(s.live)}</b> ({live_pct:.1f}%)  •  ⚪ Offline: <b>{num(s.offline)}</b>  •  ❌ Tapılmadı: <b>{num(s.notfound)}</b>",
        f"🔌 Qoşulu: <b>{num(open_n)}</b> / online <b>{num(s.live)}</b> ({conn_pct:.0f}%)  •  👂 Dinlənib: <b>{num(s.listened)}</b>",
    ]


def _bag_lines(ctl) -> list:
    import luckybag

    s = ctl.stats
    try:
        n_susp = len(luckybag.suspects(len(s.fleet_open)))
    except Exception:
        n_susp = 0
    return [
        "",
        "🛍 <b>HƏDİYYƏ ÇANTASI</b>",
        f"🛍 Dəqiq: <b>{s.lb_sure}</b>  •  🟡 Ehtimal: <b>{s.lb_maybe}</b>  •  👤 Çanta göstərən yayımçı: <b>{len(s.lb_hosts)}</b>",
        f"🎓 Öyrənilmiş növ: <b>{len(luckybag.LEARNED)}</b>  •  🔑 Açar söz: <b>{num(sum(luckybag.KW_COUNT.values()))}</b>  •  👁 İzlənən: <b>{len(settings['watch'])}</b>",
        f"🔎 Şübhəli nadir mesaj növü: <b>{n_susp}</b>" + ("  → /cantalar" if n_susp else ""),
    ]


def counts_html(ctl) -> str:
    return "\n".join(["🧮 <b>SAYĞAC</b>", LINE, *_list_lines(ctl), *_bag_lines(ctl), LINE, f"🕒 {time.strftime('%H:%M:%S')}"])


def status_html(ctl, title: str = "📡 <b>TikTok Sandıq Botu</b>") -> str:
    s = ctl.stats
    pct = (s.scanned / s.total * 100) if s.total else 0.0
    workers, interval = current_speed(ctl)
    rate = s.rate_per_min()
    h_emoji, h_text = s.health()
    n_ch = len(settings.active_channels())
    L = [
        f"{title}  ·  <b>{PHASES.get(ctl.phase, ctl.phase)}</b>",
        LINE,
        "🔎 <b>SKAN</b>",
        f"{bar(pct)} <b>{pct:.1f}%</b>",
        f"📋 Yoxlanıb: <b>{num(s.scanned)}</b> / {num(s.total)}",
        f"⏳ Qalıb: <b>{num(s.unchecked)}</b>  •  🕐 ETA: <b>{s.eta()}</b>",
        (f"🔒 Sabit sürət: <b>{settings['fixed_rate']:g}</b> ad/saniyə  •  real: <b>{rate / 60:.2f}</b> ad/san" if settings["fixed_rate"]
         else f"⚡ Sürət: <b>{rate:.0f}</b> ad/dəq (<b>{rate / 60:.2f}</b> ad/san)  •  👥 paralel <b>{workers}</b>/proxy  •  ⏱ interval <b>{interval:.1f}s</b>"),
        "",
        "🎯 <b>NƏTİCƏ</b>",
        f"🟢 Canlı <b>{num(s.live)}</b>    ⚪ Offline <b>{num(s.offline)}</b>",
        f"❌ Tapılmadı <b>{num(s.notfound)}</b>    ⚠ Xəta <b>{num(s.errors)}</b>    🗑 Yararsız <b>{num(s.invalid)}</b>",
        f"✔ Euler: ✅ <b>{s.euler_verified}</b> təsdiq · ❎ <b>{s.euler_rejected}</b> təkzib · ❔ <b>{s.euler_unverified}</b> təsdiqsiz",
        "",
        *_list_lines(ctl),
        "",
        "📦 <b>SANDIQ</b>",
        f"🔌 Dinlənir <b>{s.chest_active}</b>    👂 Dinlənib <b>{num(s.listened)}</b>/{num(max(s.live, s.listened))}",
        f"🎁 Sandıqlı yayım <b>{len(s.chest_users)}</b>    📦 Cəmi sandıq <b>{s.chest_total}</b>",
    ]
    if s.listening_now:
        names = ", ".join(f"@{esc(u)}" for u in list(s.listening_now)[:5])
        L.append(f"🎧 İndi: {names}")
    L += fleet_html(ctl)
    L += _bag_lines(ctl)
    if s.record:
        r, u, c, p = s.record
        L.append(f"🏆 Rekord: <b>@{esc(u)}</b> — {c}🪙/{p}👥 = <b>{r:.2f}</b>")
    L += [
        "",
        "🛡 <b>SAĞLAMLIQ</b>",
        f"{h_emoji} {h_text}",
        f"🚫 Blok cəmi <b>{s.blocked_hits}</b>  •  son 10 dəq: <b>{s.recent_blocks(600)}</b>",
        "",
        "📢 <b>BİLDİRİŞ</b>",
        f"👤 Admin: {onoff(not settings['muted'])}   📢 Kanal: <b>{n_ch}</b>",
        f"🎚 Kanal filtri: ratio ≥ <b>{settings['ch_min_ratio']:g}</b> • jeton ≥ <b>{settings['ch_min_coins']}</b>",
        LINE,
        f"⏱ İş vaxtı: <b>{fmt_duration(ctl.uptime())}</b>  •  🕒 {time.strftime('%H:%M:%S')}",
    ]
    return "\n".join(L)


def channel_summary(ctl, title: str = "📊 <b>İZLƏMƏ HESABATI</b>") -> str:
    """Kanal üçün QISA hesabat: neçə istifadəçi yoxlanıb, neçə canlıda qoşuluyuq. Ətraflı məlumat yalnız admin-ə gedir."""
    s = ctl.stats
    f = fleet.snapshot()
    watching_now = f["open"] if f else s.chest_active
    pct = (s.scanned / s.total * 100) if s.total else 0.0
    return "\n".join([
        title,
        LINE,
        f"👥 İzlənən istifadəçi: <b>{num(s.scanned)}</b> / {num(s.total)}  ({pct:.0f}%)",
        f"🔴 Canlı tapılan: <b>{num(s.live)}</b>",
        f"🔌 İndi canlıda izlənir: <b>{num(watching_now)}</b>",
        f"📦 Sandıqlı yayım: <b>{len(s.chest_users)}</b>",
        LINE,
        f"🕒 {time.strftime('%H:%M')}",
    ])


def prepare_html(ctl, ch_report: list | None = None, final: bool = False) -> str:
    """🛠 Hazırla paneli: proxy-lər, kanal/qrup icazələri, skan və qoşulma irəliləyişi (arxa planda canlı yenilənir)."""
    s = ctl.stats
    f = fleet.snapshot()
    L = ["🛠 <b>HAZIRLIQ</b>" + ("  ✅" if final else "  ⏳"), LINE]
    n_prem = f["premium"] if f else sum(1 for p in config.PROXIES if "@" in p)
    n_px = len(config.PROXIES)
    L.append(f"🌐 Proxy: <b>{n_px}</b> (premium <b>{n_prem}</b> • pulsuz <b>{n_px - n_prem}</b>)" + (" + öz IP" if (f and f.get('direct') and n_px) else ""))
    if f:
        L.append(f"🪖 Aktiv: ✅ <b>{f['up']}</b> • 🧊 <b>{f['down']}</b> dincəlir • tutum <b>{num(f['capacity'])}</b> canlı")
    if f and f.get("cooling"):
        L.append(f"🧊 Bloklanıb/dincəlir: <b>{f['cooling']}</b> proxy — gözləyir, <b>{int(f['back_in'])} san</b>-dən sonra özü işə düşür")
    if ctl.next_rescan:
        L.append(f"🔁 Siyahının növbəti yoxlaması: <b>{max(int((ctl.next_rescan - time.time()) // 60), 0)} dəq</b> sonra")
    L.append(group_gate.status_line())
    L.append("")
    L.append("📢 <b>Kanal / qrup</b>")
    if ch_report is None:
        L.append("⏳ yoxlanır…")
    elif not ch_report:
        L.append("📭 Kanal yoxdur → <code>/kanal_elave @kanal</code>")
    else:
        for title, ok, note in ch_report:
            L.append(f"{'✅' if ok else '❌'} <b>{esc(title)}</b> — {esc(note)}")
    pct = (s.scanned / s.total * 100) if s.total else 0.0
    L += ["", "🔎 <b>Skan</b>", f"{bar(pct)} <b>{pct:.0f}%</b>  ({num(s.scanned)}/{num(s.total)})",
          f"🟢 Canlı tapılıb: <b>{num(s.live)}</b>"]
    if f:
        conn = f["open"]
        cp = (conn / s.live * 100) if s.live else 0.0
        L += ["", "🔌 <b>Canlılara qoşulma (arxa planda)</b>",
              f"{bar(cp)} <b>{conn}</b> / {num(s.live)}",
              f"⏳ qoşulmağa çalışır: <b>{max(f['tasks'] - conn, 0)}</b> • növbədə gözləyən: <b>{num(f['waiting'])}</b>"]
    else:
        L.append("⏳ ordu hələ qurulur…")
    L += [LINE, f"🎁 Sandıq tapılan yayım: <b>{len(s.chest_users)}</b> • 🕒 {time.strftime('%H:%M:%S')}"]
    if final:
        L.append("✅ Hazırdır — canlılar və sandıq çıxan kimi kanala/qrupa düşəcək.")
    return "\n".join(L)


def prepare_kb() -> list:
    return [[{"text": "🔄 Yenilə", "callback_data": "ctl:prepare"}, {"text": "📊 Status", "callback_data": "go:status"}],
            [{"text": "🏠 Menyu", "callback_data": "go:menu"}]]


def status_kb(ctl) -> list:
    if ctl.phase == "paused":
        main_btn = {"text": "▶️ Davam", "callback_data": "ctl:resume"}
    elif ctl.phase == "running":
        main_btn = {"text": "⏸ Pauza", "callback_data": "ctl:pause"}
    elif ctl.phase == "stopping":
        main_btn = {"text": "🛑 Dayanır…", "callback_data": "noop"}
    else:
        main_btn = {"text": "🚀 Başlat", "callback_data": "ctl:start"}
    return [
        [main_btn, {"text": "🛠 Hazırla", "callback_data": "ctl:prepare"}, {"text": "🔄 Yenilə", "callback_data": "go:status"}],
        [
            {"text": "🏆 Top", "callback_data": "go:top"},
            {"text": "📦 Son", "callback_data": "go:last"},
            {"text": "🟢 Canlılar", "callback_data": "go:live"},
        ],
        [
            {"text": "🚀 Sürət", "callback_data": "go:speed"},
            {"text": "📢 Kanallar", "callback_data": "go:channels"},
            {"text": "⚙ Ayarlar", "callback_data": "go:settings"},
        ],
    ]


def back_kb(extra: list | None = None) -> list:
    rows = list(extra or [])
    rows.append([{"text": "⬅ Status", "callback_data": "go:status"}, {"text": "🏠 Menyu", "callback_data": "go:menu"}])
    return rows


# ------------------------------------------------------------------ TOP / SON / CANLI
def top_html(stats) -> str:
    if not stats.chest_best:
        return "🏆 <b>TOP SANDIQLAR</b>\n" + LINE + "\n📭 Hələ ratio məlumatı olan sandıq yoxdur."
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    L = ["🏆 <b>TOP SANDIQLAR (ratio)</b>", LINE]
    for i, (r, u, c, p) in enumerate(stats.chest_best[:10]):
        L.append(f'{medals[i]} <a href="https://www.tiktok.com/@{esc(u)}/live">@{esc(u)}</a> — {c}🪙/{p}👥 = <b>{r:.2f}</b>')
    return "\n".join(L)


def last_html(stats, n: int = 10) -> str:
    if not stats.chest_log:
        return "📦 <b>SON SANDIQLAR</b>\n" + LINE + "\n📭 Hələ sandıq görünməyib."
    L = ["📦 <b>SON SANDIQLAR</b>", LINE]
    for e in list(stats.chest_log)[:n]:
        t = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        val = f"{e['coins']}🪙/{e['can_open']}👥 = <b>{e['ratio']:.2f}</b>" if e["ratio"] is not None else "məlumat yoxdur"
        L.append(f"🕒 {t}  <a href=\"https://www.tiktok.com/@{esc(e['user'])}/live\">@{esc(e['user'])}</a>\n     {val}")
    return "\n".join(L)


def live_html(stats, n: int = 25) -> str:
    items = list(stats.live_list.items())
    head = f"🟢 <b>CANLI YAYIMLAR</b>  ({num(len(items))})"
    if not items:
        return head + "\n" + LINE + "\n📭 Hələ canlı tapılmayıb."
    L = [head, LINE]
    for u, info in items[-n:][::-1]:
        mark = "🔌" if u in stats.listening_now else ("📦" if u in stats.chest_users else "🟢")
        L.append(f'{mark} <a href="https://www.tiktok.com/@{esc(u)}/live">@{esc(u)}</a>')
    L.append(LINE)
    L.append(f"🔌 dinlənir • 📦 sandıq görünüb • 🟢 canlı  (son {min(n, len(items))})")
    return "\n".join(L)


# ------------------------------------------------------------------ SÜRƏT
def speed_html(ctl) -> str:
    workers, interval = current_speed(ctl)
    L = [
        "🚀 <b>SKAN SÜRƏTİ</b>",
        LINE,
        f"👥 Paralel: <b>{workers}</b>   ⏱ Interval: <b>{interval:.1f}s</b>   ⚡ Real: <b>{ctl.stats.rate_per_min():.0f}</b> ad/dəq",
        "",
    ]
    for key, (label, w, i, note) in PRESETS.items():
        L.append(f"{label}: {w} paralel • {i}s — <i>{note}</i>")
    L += ["", "✍ Əl ilə: <code>/surat 2 1.0</code>  (paralel, interval)", "ℹ️ TikTok blok edəndə bot özü paralel sayı azaldır."]
    return "\n".join(L)


def speed_kb() -> list:
    row1 = [{"text": PRESETS[k][0], "callback_data": f"sp:{k}"} for k in ("safe", "normal")]
    row2 = [{"text": PRESETS[k][0], "callback_data": f"sp:{k}"} for k in ("fast", "turbo")]
    row3 = [{"text": PRESETS["max"][0], "callback_data": "sp:max"}]
    return back_kb([row1, row2, row3])


# ------------------------------------------------------------------ KANALLAR
def channels_html() -> str:
    chans = settings["channels"]
    L = ["📢 <b>KANALLAR</b>", LINE]
    if not chans:
        L += [
            "📭 Kanal yoxdur.",
            "",
            "➕ <b>Necə əlavə edilir?</b>",
            "1️⃣ Botu kanala <b>admin</b> et (mesaj göndərmə icazəsi ilə)",
            "2️⃣ Bura <code>/kanal_elave @kanal</code> yaz",
            "    və ya kanaldan bir postu bura <b>forward</b> et",
        ]
        return "\n".join(L)
    for i, c in enumerate(chans, 1):
        un = f" (@{esc(c['username'])})" if c.get("username") else ""
        state = "🔔" if c.get("enabled", True) else "🔕"
        mode = "📦 yalnız sandıq" if c.get("mode") != "all" else "📦+📊 sandıq və hesabat"
        L.append(f"{i}. {state} <b>{esc(c['title'])}</b>{un}\n     {mode} • 📨 {c.get('sent', 0)}\n"
                 f"     🎚 ratio ≥ <b>{settings.ch_get(c, 'min_ratio'):g}</b> • 🪙 ≥ <b>{settings.ch_get(c, 'min_coins')}</b> • 👁 ≥ <b>{settings.ch_get(c, 'min_viewers')}</b>")
    L += ["", "⚙ Hər kanalın öz filtri var: kanalın yanındakı «⚙ Ayar» düyməsi.", f"🎚 Ümumi filtr (kanalda təyin olunmayıbsa): ratio ≥ <b>{settings['ch_min_ratio']:g}</b> • jeton ≥ <b>{settings['ch_min_coins']}</b>  (<code>/filtr 1.5 10</code>)"]
    return "\n".join(L)


def channels_kb() -> list:
    rows = []
    for i, c in enumerate(settings["channels"]):
        rows.append([{"text": f"{i + 1}. {c['title'][:28]}", "callback_data": "noop"}])
        rows.append(
            [
                {"text": "🔕 Söndür" if c.get("enabled", True) else "🔔 Aç", "callback_data": f"ch:t:{i}"},
                {"text": "📊 Hesabat da" if c.get("mode") != "all" else "📦 Yalnız sandıq", "callback_data": f"ch:m:{i}"},
                {"text": "⚙ Ayar", "callback_data": f"ch:s:{i}"},
                {"text": "🧪 Test", "callback_data": f"ch:x:{i}"},
                {"text": "🗑 Sil", "callback_data": f"ch:d:{i}"},
            ]
        )
    return back_kb(rows)


def chan_html(i: int) -> str:
    try:
        c = settings["channels"][i]
    except IndexError:
        return "❌ Kanal tapılmadı"
    return "\n".join([
        f"⚙ <b>{esc(c['title'])}</b> — kanal ayarları", LINE,
        f"{'🔔 açıq' if c.get('enabled', True) else '🔕 söndürülü'} • {'📦+📊 sandıq və hesabat' if c.get('mode') == 'all' else '📦 yalnız sandıq'}",
        f"🎚 Minimum ratio: <b>{settings.ch_get(c, 'min_ratio'):g}</b>",
        f"🪙 Minimum jeton: <b>{settings.ch_get(c, 'min_coins')}</b>",
        f"👁 Minimum izləyici: <b>{settings.ch_get(c, 'min_viewers')}</b>  <i>(say bilinmirsə süzgəc keçirir)</i>",
        f"🛍 Lucky Bag: {'🔔 göndərilir' if settings.ch_get(c, 'luckybag') else '🔕 göndərilmir'}",
        f"📨 Bu kanala göndərilən: <b>{c.get('sent', 0)}</b>",
        "", "Düymələrlə seçin 👇 (0 = süzgəc yoxdur)",
    ])


def chan_kb(i: int) -> list:
    def row(label, key, vals):
        return [{"text": f"{label} {v:g}", "callback_data": f"ch:{key}:{i}:{v}"} for v in vals]
    return back_kb([
        row("🎚", "r", (0, 1, 2, 3)),
        row("🪙", "c", (0, 50, 200, 500)),
        row("👁", "v", (0, 100, 500, 1000)),
        [{"text": "🛍 Lucky Bag aç/bağla", "callback_data": f"ch:l:{i}"}, {"text": "⬅ Kanallar", "callback_data": "go:channels"}],
    ])


# ------------------------------------------------------------------ AYARLAR
def settings_html(ctl) -> str:
    workers, interval = current_speed(ctl)
    rep = settings["report_every"]
    rep_txt = f"hər <b>{rep // 60 if rep % 60 == 0 else rep}</b> {'dəq' if rep % 60 == 0 else 'san'}" if rep > 0 else "<b>söndürülü</b>"
    return "\n".join(
        [
            "⚙ <b>AYARLAR</b>",
            LINE,
            f"👤 Admin sandıq bildirişi: {onoff(not settings['muted'])}   <i>/sessiz</i>",
            f"📢 Kanallar: <b>{len(settings['channels'])}</b> (aktiv {len(settings.active_channels())})   <i>/kanallar</i>",
            f"🎚 Kanal filtri: ratio ≥ <b>{settings['ch_min_ratio']:g}</b> • jeton ≥ <b>{settings['ch_min_coins']}</b>   <i>/filtr</i>",
            f"📨 Ara hesabat: {rep_txt}   <i>/hesabat_vaxt dəq</i>",
            f"👂 Dinləmə müddəti: <b>{settings['listen_seconds']}s</b>   <i>/dinleme san</i>",
            f"🔢 Yayım başına maks. bildiriş: <b>{settings['notify_max']}</b>   <i>/bildiris_max n</i>",
            f"🚀 Sürət: <b>{workers}</b> paralel • <b>{interval:.1f}s</b>   <i>/surat</i>",
            f"🚨 Blok xəbərdarlığı: {onoff(settings['alert_block'])}",
            f"🏁 Bitmə xəbəri: {onoff(settings['alert_done'])}",
            f"⏩ Skan bitəndən sonra canlılara qoşul: {'✅ açıq' if settings['join_after_scan'] else '❌ söndürülü (tapılan kimi qoşulur)'}",
            f"🔁 Siyahı təkrar yoxlanışı: hər <b>{settings['rescan_minutes']}</b> dəq   <i>/yenile_vaxt dəq</i>",
            f"🙈 İstisna adlar: <b>{len(settings['ignore'])}</b>   <i>/istisna</i>",
            f"🛡 Adminlər: <b>{len(config.ADMIN_IDS)}</b>",
        ]
    )


def settings_kb() -> list:
    return back_kb(
        [
            [
                {"text": "🔕 Admin bildirişi" if not settings["muted"] else "🔔 Admin bildirişi", "callback_data": "tg:muted"},
                {"text": "🚨 Blok xəbəri", "callback_data": "tg:alert_block"},
            ],
            [{"text": "🏁 Bitmə xəbəri", "callback_data": "tg:alert_done"}, {"text": "📨 Hesabat indi", "callback_data": "ctl:report"}],
            [{"text": "⏩ Skandan sonra qoşul", "callback_data": "tg:join_after_scan"}, {"text": "🔁 İndi yenidən yoxla", "callback_data": "ctl:rescan"}],
        ]
    )


# ------------------------------------------------------------------ MENYU / KÖMƏK
def menu_html(ctl) -> str:
    return (
        "🏠 <b>ANA MENYU</b>\n" + LINE + f"\n{PHASES.get(ctl.phase, ctl.phase)}\n"
        "Düymələrdən istifadə edin və ya /komek yazın 👇"
    )


def menu_kb(ctl) -> list:
    return [
        [{"text": "🛠 Hazırla", "callback_data": "ctl:prepare"}],
        [{"text": "📊 Status", "callback_data": "go:status"}, {"text": "📡 Canlı panel", "callback_data": "ctl:panel"}],
        [{"text": "🏆 Top", "callback_data": "go:top"}, {"text": "📦 Son sandıqlar", "callback_data": "go:last"}],
        [{"text": "🟢 Canlılar", "callback_data": "go:live"}, {"text": "📢 Kanallar", "callback_data": "go:channels"}],
        [{"text": "🚀 Sürət", "callback_data": "go:speed"}, {"text": "⚙ Ayarlar", "callback_data": "go:settings"}],
        [{"text": "👥 İzlənənlər siyahısı", "callback_data": "ctl:follow"}, {"text": "🔁 İndi yoxla", "callback_data": "ctl:rescan"}],
        [{"text": "📁 Fayllar", "callback_data": "ctl:files"}, {"text": "🧾 Log", "callback_data": "ctl:log"}],
    ]


def help_html() -> str:
    return "\n".join(
        [
            "🤖 <b>ƏMRLƏR</b>  <i>(yalnız admin üçün)</i>",
            LINE,
            "📊 <b>Baxış</b>",
            "/status — tam status • /panel — canlı yenilənən panel",
            "/top — ən yaxşı ratio • /son [n] — son sandıqlar",
            "/canli — tapılan canlılar • /ping — bot sağdır?",
            "",
            "🎮 <b>İdarə</b>",
            "/izlenenler <code>ad</code> — həmin hesabın izlədiyi adamların siyahısını .txt verir (yalnız siyahısı açıq olanlar), sonra siyahıya əlavə edim? soruşur",
            "/yenile_vaxt <code>dəq</code> — siyahı neçə dəqiqədən bir təkrar yoxlansın (30) • /yoxla_indi",
            "/kanal_filtr <code>N ratio jeton izləyici</code> — N-ci kanalın öz filtri (məs. <code>/kanal_filtr 1 2 100 500</code>)",
            "/hazirla — 🛠 hər şeyi hazırla: skan + canlılara arxa planda qoşul + kanal icazələrini yoxla\n/kanal_yoxla — qrup/kanalda admin icazəsi var?",
            "/pauza • /davam • /dayan • /baslat [davam]",
            "/surat — sürət presetləri (və ya <code>/surat 2 1.0</code>)",
            "/sabit <code>1</code> — sabit sürət (saniyədə 1 ad, dəyişmir), <code>/sabit off</code> söndürür",
            "/yoxla <code>ad</code> (və ya sadəcə adı yazın) — öncəlikli yoxla, canlıdırsa dərhal qoşul və dinlə",
            "/qosul <code>ad</code> — canlıdırsa ordu həmin yayıma dərhal qoşulsun (qoşulanda xəbər verir; bir neçə ad: <code>/qosul a b c</code>)",
            "/izle <code>ad</code> — hesabı izləməyə əlavə et: yayıma başlayan kimi qoşulur, sandıq həmişə gəlir • /izle_siyahi • <code>/izle_sil ad</code>",
            "/hd — 🛍 hədiyyə çantası mesajını əl ilə hazırla (link, canlıda say, jeton, qazanan sayı soruşur) • /sd — 🎁 eyni qayda ilə sandıq • /legv — ləğv",
            "/probe <code>ad</code> — sandığı artıq olan yayımda diaqnostika faylı göndərir",
            "/istisna <code>ad…</code> — skan olunmasın • <code>/istisna sil ad</code>",
            "",
            "📢 <b>Kanal və bildiriş</b>",
            "/kanallar • /kanal_elave <code>@kanal</code> • /kanal_sil <code>@kanal</code>",
            "/filtr <code>ratio jeton</code> — kanal filtri",
            "/sessiz — admin sandıq bildirişini aç/bağla",
            "/hesabat — indi • /hesabat_vaxt <code>dəq</code> • /dinleme <code>san</code> • /bildiris_max <code>n</code>",
            "",
            "🗂 <b>Fayl və siyahı</b>",
            "/istifadeci_elave — ad yazın və ya .txt göndərin",
            "/fayllar — nəticə faylları • /log [n] — son loglar",
            "/kapat — botu tam söndür",
            "",
            "💡 Kanalı <b>forward</b> etməklə də əlavə edə bilərsiniz.",
        ]
    )
