#!/usr/bin/env python3
"""Монитор аэропортов Шереметьево, Внуково, Пулково и Сочи.

Два типа событий:
  1. Закрытие / открытие — «временные ограничения на приём и выпуск воздушных судов»
     (обычно план «Ковёр» из-за атак беспилотников). Источник — официальные Telegram-каналы
     аэропортов (@svo_online, @vnukovoairport_VKO, @pulkovo_led), Росавиации (@favt_ru, @korenyako)
     и Аэрофлота (@aeroflot), читаются через публичные веб-превью t.me/s/<канал>.
  2. Массовые задержки — более DELAY_LIMIT вылетов задержаны на DELAY_MIN минут и больше.
  Сообщение отправляется один раз при изменении состояния (повторы отключены, REMIND_MIN = 0).
     Источник — онлайн-табло svo.aero и pulkovoairport.ru (JSON). Табло Внуково защищено
     от автоматического чтения, поэтому задержки по Внуково не отслеживаются.

Состояние хранится рядом со скриптом; при изменении рассылает сообщение подписчикам
Telegram-бота (и SMS через sms.ru, если настроен). Печатает JSON-отчёт.
"""
import html, json, os, re, sys, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flights as F   # статус конкретного рейса и положение самолёта

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "airport_state.json")
SUBS_FILE = os.path.join(HERE, "subscribers.json")   # {"offset": N, "chats": {"<id>": {"since": ...}}}
TG_CONFIG = os.path.join(HERE, "tg_config.json")     # {"bot_token": "...", "chat_id": "..."}
SMS_CONFIG = os.path.join(HERE, "sms_config.json")   # {"api_id": "...", "to": "79XXXXXXXXX"}
POSITION_ENABLED = False   # положение самолёта по ADS-B (пока выключено по просьбе пользователя)
TRACK_FILE = os.path.join(HERE, "tracked.json")      # {"<chat>": {"SU284|2026-09-21": {"last": {...}, "added": ...}}}

MSK = timezone(timedelta(hours=3))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}

# --- Закрытия -------------------------------------------------------------------------
CHANNELS = ["svo_online", "vnukovoairport_VKO", "pulkovo_led", "aeroaer", "favt_ru", "korenyako", "aeroflot"]
# Собственные каналы аэропортов: их сообщение об ограничениях относится к своему аэропорту
OWN_CHANNEL = {"svo_online": "Шереметьево", "vnukovoairport_VKO": "Внуково", "pulkovo_led": "Пулково", "aeroaer": "Сочи"}
AIRPORTS = ["Шереметьево", "Внуково", "Пулково", "Сочи"]
# Как аэропорт может упоминаться в тексте (в любом падеже)
MENTION = {
    "Шереметьево": r"Шереметьев|московск\w+ авиа\w* узл|московских аэропорт|аэропортах Москвы",
    "Внуково": r"Внуков|московск\w+ авиа\w* узл|московских аэропорт|аэропортах Москвы",
    "Пулково": r"Пулков",
    "Сочи": r"Сочи",
}
CLOSE_RE = re.compile(
    r"введен\w*\s+(временн\w+\s+)?ограничен|ограничен\w+\s+(на\s+)?(при[её]м|вылет|использован)"
    r"|прекрати\w+\s+при[её]м|закрыт\w*\s+(на\s+)?при[её]м|не\s+принимает|приостановл\w+\s+(при[её]м|полёт|полет)"
    r"|продолжа\w+\s+(действ\w+\s+)?(временн\w+\s+)?ограничен|действи\w+\s+(временн\w+\s+)?ограничен|ещ[её]\s+закрыт"
    r"|сигнал\w*\s+[«\"]?ков[её]р", re.I)
OPEN_RE = re.compile(
    r"снят\w*\s+(введ\w+\s+ранее\s+)?(временн\w+\s+)?ограничен|ограничен\w+\s+(\S+\s+){0,4}снят"
    r"|работает\s+(без\s+ограничений|в\s+штатном\s+режиме|штатно)|возобнов\w+\s+(при[её]м|работ|полёт|полет|выполнен)", re.I)
# Обороты про будущее снятие («после снятия ограничений») не считаем открытием
FUTURE_RE = re.compile(r"(после|до|в\s+случае|при)\s+снят\w+\s+(временн\w+\s+)?ограничен\w*", re.I)

# --- Задержки -------------------------------------------------------------------------
DELAY_MIN = 60        # задержка от ... минут
DELAY_LIMIT = 5       # тревога, если задержанных рейсов БОЛЬШЕ этого числа
DELAY_CLEAR = 3       # отбой, когда задержанных стало не больше этого числа (гистерезис от дребезга)
WINDOW_BACK_H, WINDOW_FWD_H = 2, 6   # учитываем вылеты по расписанию от -2 ч до +6 ч от текущего момента
CLOSURE_TTL_H = 8     # закрытие «живёт» не дольше N часов: без свежего подтверждения аэропорт снова считается
                      # открытым. Ограничения длятся часы, а пост о снятии может не попасть в ленту канала —
                      # без этого срока аэропорт навсегда застревал в статусе «закрыт» и уведомления не приходили.
REMIND_MIN = 0        # повторные напоминания каждые N минут, пока ситуация сохраняется; 0 = выключено
                      # (по желанию пользователя: одно сообщение при закрытии, одно при открытии)


def now_utc():
    return datetime.now(timezone.utc)


def msk(iso):
    try:
        return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m %H:%M МСК")
    except Exception:
        return ""


def http_get(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ======================================================================================
# Telegram-каналы: закрытия / открытия
# ======================================================================================
def parse_channel(page, channel):
    out = []
    for blk in re.findall(r'<div class="tgme_widget_message_wrap.*?(?=<div class="tgme_widget_message_wrap|$)', page, re.S):
        t = re.search(r'<time datetime="([^"]+)"', blk)
        m = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', blk, re.S)
        link = re.search(r'data-post="([^"]+)"', blk)
        if not (t and m):
            continue
        txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip()
        out.append({"time": t.group(1), "text": txt, "channel": channel,
                    "url": f"https://t.me/{link.group(1)}" if link else ""})
    return out


def classify(text):
    """'closed' / 'open' / None по тексту сообщения."""
    t = FUTURE_RE.sub(" ", text)
    if OPEN_RE.search(t):
        return "open"
    if CLOSE_RE.search(t):
        return "closed"
    return None


def closure_status(posts):
    status = {a: {"state": "open", "since": None, "source": None, "text": None, "stale": False} for a in AIRPORTS}
    horizon = (now_utc() - timedelta(hours=CLOSURE_TTL_H)).isoformat()
    for p in sorted(posts, key=lambda p: p["time"]):
        verdict = classify(p["text"])
        if not verdict:
            continue
        own = OWN_CHANNEL.get(p["channel"])
        targets = [a for a in AIRPORTS if re.search(MENTION[a], p["text"])]
        # В собственном канале аэропорта сообщение о своём аэропорте относится только к нему
        # (чтобы «ограничения в Пулково; рейсы в Сочи задерживаются» не закрывало Сочи)
        if own and own in targets:
            targets = [own]
        for a in targets:
            status[a] = {"state": verdict, "since": p["time"], "source": p["url"], "text": p["text"][:300],
                         "stale": False}
    # Устаревшее закрытие снимаем сами: пост о снятии мог не попасть в ленту канала
    for a, s in status.items():
        if s["state"] == "closed" and (s["since"] or "") < horizon:
            s.update(state="open", stale=True)
    return status


# ======================================================================================
# Онлайн-табло: задержки вылетов
# ======================================================================================
def delayed_svo():
    n = now_utc().astimezone(MSK)
    d1 = (n - timedelta(hours=WINDOW_BACK_H)).strftime("%Y-%m-%dT%H:%M:00+03:00")
    d2 = (n + timedelta(hours=WINDOW_FWD_H)).strftime("%Y-%m-%dT%H:%M:00+03:00")
    url = ("https://www.svo.aero/bitrix/timetable/?direction=departure&dateStart=" + urllib.parse.quote(d1)
           + "&dateEnd=" + urllib.parse.quote(d2) + "&perPage=1000&page=0&locale=ru")
    items = json.loads(http_get(url)).get("items", [])
    delayed, total = [], 0
    for f in items:
        std, est = f.get("t_st"), f.get("t_et") or f.get("t_at")
        if not std:
            continue
        total += 1
        if est and (datetime.fromisoformat(est) - datetime.fromisoformat(std)) >= timedelta(minutes=DELAY_MIN):
            delayed.append(f"{f.get('co', {}).get('code', '')}{f.get('flt', '')} {std[11:16]}→{est[11:16]}")
    return {"delayed": len(delayed), "total": total, "flights": delayed[:15]}


def delayed_led():
    n = now_utc().astimezone(MSK)
    lo, hi = n - timedelta(hours=WINDOW_BACK_H), n + timedelta(hours=WINDOW_FWD_H)
    rows = json.loads(http_get("https://pulkovoairport.ru/api/?col=300&type=departure",
                               headers={"X-Requested-With": "XMLHttpRequest",
                                        "Referer": "https://pulkovoairport.ru/passengers/departure/"}))
    delayed, total = [], 0
    for f in rows:
        std, etd = f.get("OD_STD"), f.get("OD_ETD")
        if not std:
            continue
        s = datetime.fromisoformat(std[:19]).replace(tzinfo=MSK)
        if not (lo <= s <= hi):
            continue
        total += 1
        if etd and (datetime.fromisoformat(etd[:19]).replace(tzinfo=MSK) - s) >= timedelta(minutes=DELAY_MIN):
            delayed.append(f"{f.get('OD_FLIGHT_NUMBER', '').replace('  ', ' ')} {std[11:16]}→{etd[11:16]}")
    return {"delayed": len(delayed), "total": total, "flights": delayed[:15]}


def delayed_aer():
    """Сочи: вылеты, у которых план или перенос попадает в окно, с задержкой >= DELAY_MIN."""
    n = now_utc().astimezone(MSK)
    lo, hi = n - timedelta(hours=WINDOW_BACK_H), n + timedelta(hours=WINDOW_FWD_H)
    delayed, total = [], 0
    for c in F.aer_board("today"):
        if c["dir"] != "D" or not c.get("sched"):
            continue
        sched = datetime.fromisoformat(c["sched"])
        ref = c.get("actual") or c.get("est")
        est = datetime.fromisoformat(ref) if ref else None
        if not (lo <= sched <= hi or (est and lo <= est <= hi)):
            continue
        if re.search(r"отмен", c.get("status", ""), re.I):
            continue
        total += 1
        if est and est - sched >= timedelta(minutes=DELAY_MIN):
            delayed.append(f"{c['flight']} {F._hm(c['sched'])}→{F._hm(ref, c['sched'])}")
    return {"delayed": len(delayed), "total": total, "flights": delayed[:15], "fetch_s": F._AER_CACHE.get("today_time")}


DELAY_SOURCES = {"Шереметьево": delayed_svo, "Пулково": delayed_led}
if F.AER_AVAILABLE:
    DELAY_SOURCES["Сочи"] = delayed_aer


# ======================================================================================
# Рассылка: Telegram-бот (подписчики) и SMS
# ======================================================================================
def tg_api(token, method, **params):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "description": e.read().decode("utf-8", "replace")[:300]}


def tg_config():
    cfg = {}
    if os.path.exists(TG_CONFIG):
        try:
            cfg = json.load(open(TG_CONFIG, encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg.setdefault("bot_token", os.environ.get("TG_BOT_TOKEN"))
    cfg.setdefault("chat_id", os.environ.get("TG_CHAT_ID"))
    return cfg if cfg.get("bot_token") else None


def load_subs():
    subs = {"offset": 0, "chats": {}}
    if os.path.exists(SUBS_FILE):
        try:
            subs.update(json.load(open(SUBS_FILE, encoding="utf-8")))
        except Exception:
            pass
    # Начальные подписчики из переменной окружения (чтобы не терять их при переезде на другой сервер)
    for cid in os.environ.get("TG_SEED_CHATS", "").replace(";", ",").split(","):
        cid = cid.strip()
        if cid and cid not in subs["chats"]:
            subs["chats"][cid] = {"since": "seed"}
    return subs


def save_subs(subs):
    new = json.dumps(subs, ensure_ascii=False, indent=2)
    old = open(SUBS_FILE, encoding="utf-8").read() if os.path.exists(SUBS_FILE) else None
    if new != old:
        open(SUBS_FILE, "w", encoding="utf-8").write(new)


HELP = ("✈️ Отслеживание рейса:\n"
        "• пришлите номер рейса — «SU284» или «SU284 21.09» — получите статус с табло "
        "(время, задержка, терминал, выход, лента багажа);\n"
        "• /track SU284 21.09 — следить за рейсом: сообщу об изменении времени, статуса, выхода;\n"
        "• /my — мои рейсы, /untrack SU284 — перестать следить;\n"
        "• пришлите билет PDF или перешлите письмо с бронированием — поставлю все рейсы на слежение до прибытия.\n"
        "Табло: Шереметьево, Пулково и Сочи (Внуково не даёт данных). Ответ приходит в течение ~10 минут.")
WELCOME = ("✅ Вы подписаны на оповещения по аэропортам Шереметьево, Внуково, Пулково и Сочи:\n"
           "• закрытие / открытие (временные ограничения, обычно из-за атак беспилотников);\n"
           f"• массовые задержки — больше {DELAY_LIMIT} вылетов задержаны на {DELAY_MIN} мин и дольше.\n"
           "Сообщение приходит один раз при закрытии и один раз при открытии.\n"
           "Отписаться: /stop\n\n" + HELP)
BYE = ("Вы отписаны от оповещений. Больше не пришлю ничего: ни о закрытии и открытии аэропортов, "
       "ни о массовых задержках, ни об изменениях по вашим рейсам.\nПодписаться снова: /start")
STOPPED_FLIGHT = ("Больше по этому рейсу сообщений не будет: ни об изменении времени вылета и прилёта, "
                  "ни о статусе, терминале, выходе и ленте багажа.")


def load_tracked():
    if os.path.exists(TRACK_FILE):
        try:
            return json.load(open(TRACK_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_tracked(tr):
    new = json.dumps(tr, ensure_ascii=False, indent=2)
    old = open(TRACK_FILE, encoding="utf-8").read() if os.path.exists(TRACK_FILE) else None
    if new != old:
        open(TRACK_FILE, "w", encoding="utf-8").write(new)


def flight_reply(token, cid, code, num, date):
    """Карточки рейса (+ точка на карте, если есть координаты). Возвращает найденные карточки."""
    cards, errs = F.find_flight(code, num, date)
    if not cards:
        tg_api(token, "sendMessage", chat_id=cid,
               text=f"Рейс {code}{num} на {date.strftime('%d.%m')} не найден на табло Шереметьево и Пулково.\n"
                    "Проверьте номер и дату (например «SU284 21.09»). Пулково и Сочи показывают только ближайшие сутки."
                    + (f"\n⚠️ {'; '.join(errs)}" if errs else ""))
        return []
    cards.sort(key=lambda c: c["dir"] != "D")
    pos = F.position(code, num) if POSITION_ENABLED and any(F.in_air(c) for c in cards) else None
    text = "\n\n".join(F.format_card(c, pos if i == 0 else None) for i, c in enumerate(cards))
    tg_api(token, "sendMessage", chat_id=cid, text=text)
    if pos and pos.get("lat") is not None:
        tg_api(token, "sendLocation", chat_id=cid, latitude=pos["lat"], longitude=pos["lon"])
    return cards


def flight_done(cards):
    """Рейс выполнен: есть фактический прилёт или статус «прибыл» / «отменён»."""
    for c in cards:
        st = (c.get("status") or "").lower()
        if (c["dir"] == "A" and c.get("actual")) or "прибыл" in st or "отмен" in st:
            return True
    return False


def track_itinerary(token, cid, text, tracked, source="текст"):
    """Находит в тексте рейсы с датами, показывает их статус и ставит на слежение до прибытия."""
    today = now_utc().astimezone(MSK).date()
    found = F.parse_itinerary(text, today)
    if not found:
        return False
    added, done = [], []
    for code, num, date in found[:8]:
        cards, _ = F.find_flight(code, num, date)
        if cards:
            cards.sort(key=lambda c: c["dir"] != "D")
            tg_api(token, "sendMessage", chat_id=cid, text="\n\n".join(F.format_card(c) for c in cards))
            if flight_done(cards):
                done.append(f"{code}{num} {date.strftime('%d.%m')}")
                continue
        else:
            tg_api(token, "sendMessage", chat_id=cid,
                   text=f"✈️ {code}{num} {date.strftime('%d.%m.%Y')} — пока нет на табло, сообщу, когда появится "
                        "(Шереметьево публикует рейсы заранее, Пулково — за сутки).")
        key = f"{code}{num}|{date}"
        tracked.setdefault(cid, {})[key] = {"last": {c["airport"] + c["dir"]: F.snapshot(c) for c in cards},
                                            "added": now_utc().isoformat(), "source": source}
        added.append(f"{code}{num} {date.strftime('%d.%m')}")
    msg = ""
    if added:
        msg += "👀 Слежу до прибытия: " + ", ".join(added) + ".\nСообщу об изменениях времени, статуса, выхода и багажа. /my — список, /untrack — отменить."
    if done:
        msg += ("\n\n" if msg else "") + "✔️ Уже выполнены, следить не нужно: " + ", ".join(done) + ".\n" + STOPPED_FLIGHT
    tg_api(token, "sendMessage", chat_id=cid, text=msg)
    return True


def pdf_text(token, file_id):
    """Скачивает документ из Telegram и извлекает текст (PDF)."""
    info = tg_api(token, "getFile", file_id=file_id)
    if not info.get("ok"):
        raise RuntimeError("не удалось получить файл из Telegram")
    url = f"https://api.telegram.org/file/bot{token}/{info['result']['file_path']}"
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def handle_command(token, cid, text, subs, tracked):
    """Команды бота. Возвращает True, если что-то сделано."""
    t = text.strip()
    low = t.lower()
    if low.startswith("/help"):
        tg_api(token, "sendMessage", chat_id=cid, text=HELP)
        return True
    if low.startswith("/my"):
        mine = tracked.get(cid, {})
        tg_api(token, "sendMessage", chat_id=cid,
               text=("Слежу за рейсами:\n" + "\n".join(f"• {k.split('|')[0]} {k.split('|')[1][8:10]}.{k.split('|')[1][5:7]}" for k in mine))
               if mine else "Вы пока не следите ни за одним рейсом. Пример: /track SU284 21.09")
        return True
    if low.startswith("/untrack"):
        arg = t[8:].strip()
        mine = tracked.get(cid, {})
        if not arg:
            had = bool(tracked.pop(cid, None))
            tg_api(token, "sendMessage", chat_id=cid,
                   text=("Перестал следить за всеми рейсами.\n" + STOPPED_FLIGHT.replace("этому рейсу", "этим рейсам")) if had
                        else "Вы и так не следите ни за одним рейсом.")
            return True
        pf = F.parse_flight(arg)
        keys = [k for k in mine if pf and k.startswith(f"{pf[0]}{pf[1]}|")]
        for k in keys:
            mine.pop(k, None)
        tg_api(token, "sendMessage", chat_id=cid,
               text=(f"Перестал следить за {pf[0]}{pf[1]}.\n" + STOPPED_FLIGHT) if keys
                    else "Такого рейса в списке нет. /my — список.")
        return True
    if low.startswith("/track"):
        pf = F.parse_flight(t[6:].strip())
        if not pf:
            tg_api(token, "sendMessage", chat_id=cid, text="Формат: /track SU284 21.09 (дата — необязательно, по умолчанию сегодня)")
            return True
        code, num, date = pf
        date = date or now_utc().astimezone(MSK).date()
        cards = flight_reply(token, cid, code, num, date)
        if cards and flight_done(cards):
            tg_api(token, "sendMessage", chat_id=cid,
                   text=f"✔️ {code}{num} {date.strftime('%d.%m')} уже выполнен — следить не нужно.\n" + STOPPED_FLIGHT)
            return True
        if cards:
            key = f"{code}{num}|{date}"
            tracked.setdefault(cid, {})[key] = {"last": {c["airport"] + c["dir"]: F.snapshot(c) for c in cards},
                                                "added": now_utc().isoformat()}
            tg_api(token, "sendMessage", chat_id=cid, text=f"👀 Слежу за {code}{num} {date.strftime('%d.%m')}: сообщу об изменениях времени, статуса, выхода и багажа. /untrack {code}{num} — отменить.")
        return True
    pf = F.parse_flight(t)
    if pf:
        code, num, date = pf
        flight_reply(token, cid, code, num, date or now_utc().astimezone(MSK).date())
        return True
    # Пересланное бронирование / маршрут-квитанция / текст билета
    if track_itinerary(token, cid, t, tracked):
        return True
    return False


def update_subscribers(token, subs):
    """/start (или любое сообщение) — подписка, /stop — отписка, остальное — команды по рейсам."""
    resp = tg_api(token, "getUpdates", offset=subs["offset"], timeout=0)
    if not resp.get("ok"):
        return
    tracked = load_tracked()
    for u in resp.get("result", []):
        subs["offset"] = max(subs["offset"], u["update_id"] + 1)
        m = u.get("message") or {}
        chat = m.get("chat") or {}
        if chat.get("type") != "private":
            continue
        cid = str(chat["id"])
        text = (m.get("text") or m.get("caption") or "").strip()
        low = text.lower()
        doc = m.get("document") or {}
        if doc and (doc.get("mime_type") == "application/pdf" or str(doc.get("file_name", "")).lower().endswith(".pdf")):
            if cid not in subs["chats"]:
                subs["chats"][cid] = {"since": now_utc().isoformat()}
            try:
                if doc.get("file_size", 0) > 20 * 1024 * 1024:
                    raise RuntimeError("файл больше 20 МБ")
                txt = pdf_text(token, doc["file_id"])
                if not txt.strip():
                    raise RuntimeError("в PDF нет текстового слоя (скан?) — пришлите текст бронирования")
                if not track_itinerary(token, cid, txt, tracked, source=doc.get("file_name", "pdf")):
                    tg_api(token, "sendMessage", chat_id=cid, text="В PDF не нашёл рейсов с датами. Пришлите номер рейса и дату текстом, например «SU284 21.09».")
            except Exception as e:
                tg_api(token, "sendMessage", chat_id=cid, text=f"Не удалось разобрать PDF: {e}")
            continue
        if doc or m.get("photo"):
            tg_api(token, "sendMessage", chat_id=cid, text="Принимаю билеты в PDF или текстом (перешлите письмо с бронированием). Фото пока не распознаю.")
            continue
        if low.startswith("/stop"):
            subs["chats"].pop(cid, None)
            tracked.pop(cid, None)
            tg_api(token, "sendMessage", chat_id=cid, text=BYE)
            continue
        if cid not in subs["chats"]:
            subs["chats"][cid] = {"since": now_utc().isoformat()}
            tg_api(token, "sendMessage", chat_id=cid, text=WELCOME)
            if low.startswith("/start"):
                continue
        elif low.startswith("/start"):
            tg_api(token, "sendMessage", chat_id=cid, text=WELCOME)
            continue
        try:
            if not handle_command(token, cid, text, subs, tracked):
                tg_api(token, "sendMessage", chat_id=cid, text="Не понял. " + HELP)
        except Exception as e:
            tg_api(token, "sendMessage", chat_id=cid, text=f"Не удалось получить данные: {e}")
    save_tracked(tracked)
    save_subs(subs)


def check_tracked(token):
    """Проверяет отслеживаемые рейсы и сообщает об изменениях. Возвращает список отправленных строк."""
    tracked = load_tracked()
    sent = []
    now = now_utc()
    for cid, items in list(tracked.items()):
        for key, item in list(items.items()):
            code_num, date_s = key.split("|")
            code, num = code_num[:2], code_num[2:]
            date = datetime.fromisoformat(date_s).date()
            # срок: сутки после даты рейса или час после фактического прилёта
            if now.astimezone(MSK).date() > date + timedelta(days=1):
                items.pop(key)
                continue
            try:
                cards, _ = F.find_flight(code, num, date)
            except Exception:
                continue
            if not cards:
                continue
            changes = []
            appeared = []
            for c in cards:
                k = c["airport"] + c["dir"]
                old = item["last"].get(k, {})
                new = F.snapshot(c)
                if not old:
                    appeared.append(c)
                elif new != old:
                    diff = F.describe_change(old, new, c)
                    if diff:
                        changes.append(f"{'Вылет' if c['dir'] == 'D' else 'Прилёт'} ({c['airport']}): {diff}")
                item["last"][k] = new
            if appeared:
                appeared.sort(key=lambda c: c["dir"] != "D")
                tg_api(token, "sendMessage", chat_id=cid, text="📋 Рейс появился на табло:\n\n" + "\n\n".join(F.format_card(c) for c in appeared))
                sent.append(f"{cid}: {code}{num} появился на табло")
            if changes:
                pos = F.position(code, num) if POSITION_ENABLED and any(F.in_air(c) for c in cards) else None
                text = f"🔔 {code}{num} {date.strftime('%d.%m')}\n" + "\n".join(changes)
                if pos and not pos.get("on_ground") and pos.get("alt_m") is not None:
                    text += f"\n🛰 Сейчас: высота {pos['alt_m']} м" + (f", скорость {pos['speed_kmh']} км/ч" if pos.get("speed_kmh") else "")
                tg_api(token, "sendMessage", chat_id=cid, text=text)
                if pos and pos.get("lat") is not None:
                    tg_api(token, "sendLocation", chat_id=cid, latitude=pos["lat"], longitude=pos["lon"])
                sent.append(f"{cid}: {code}{num} — {'; '.join(changes)}")
            if flight_done(cards):
                # рейс завершён — сообщаем и снимаем с отслеживания
                fin = [c for c in cards if c["dir"] == "A" and c.get("actual")] or cards
                tg_api(token, "sendMessage", chat_id=cid,
                       text=f"✅ {code}{num} {date.strftime('%d.%m')} выполнен: {fin[0].get('status') or 'прибыл'}"
                            + (f" в {fin[0]['actual'][11:16]}" if fin[0].get("actual") and fin[0]["dir"] == "A" else "")
                            + ". Снимаю с наблюдения.\n" + STOPPED_FLIGHT)
                sent.append(f"{cid}: {code}{num} выполнен")
                items.pop(key, None)
        if not items:
            tracked.pop(cid, None)
    save_tracked(tracked)
    return sent


def send_telegram(text):
    cfg = tg_config()
    if not cfg:
        return False, "нет tg_config.json / TG_BOT_TOKEN"
    subs = load_subs()
    chats = set(subs["chats"]) | ({str(cfg["chat_id"])} if cfg.get("chat_id") else set())
    results = {}
    for cid in sorted(chats):
        resp = tg_api(cfg["bot_token"], "sendMessage", chat_id=cid, text=text)
        results[cid] = "ok" if resp.get("ok") else resp.get("description", "error")
        if not resp.get("ok") and "blocked" in str(resp.get("description", "")):
            subs["chats"].pop(cid, None)   # пользователь заблокировал бота
    save_subs(subs)
    ok = any(v == "ok" for v in results.values())
    return ok, f"доставлено {sum(v == 'ok' for v in results.values())}/{len(results)}"


def send_sms(text):
    if not os.path.exists(SMS_CONFIG):
        return False, "нет sms_config.json"
    cfg = json.load(open(SMS_CONFIG, encoding="utf-8"))
    if not cfg.get("api_id") or not cfg.get("to") or "XXX" in str(cfg.get("to")):
        return False, "sms_config.json не заполнен"
    q = urllib.parse.urlencode({"api_id": cfg["api_id"], "to": cfg["to"], "msg": text.split("\n")[0][:300], "json": 1})
    req = urllib.request.Request("https://sms.ru/sms/send", data=q.encode(), headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8", "replace"))
    ok = resp.get("status") == "OK" and all(v.get("status") == "OK" for v in resp.get("sms", {}).values())
    return ok, json.dumps(resp, ensure_ascii=False)[:200]


# ======================================================================================
def main():
    errors, posts = [], []
    cfg = tg_config()
    tracked_sent = []
    if cfg:
        try:
            update_subscribers(cfg["bot_token"], load_subs())
        except Exception as e:
            errors.append(f"telegram subscribers: {e}")
        try:
            tracked_sent = check_tracked(cfg["bot_token"])
        except Exception as e:
            errors.append(f"tracked flights: {e}")

    for ch in CHANNELS:
        try:
            posts += parse_channel(http_get(f"https://t.me/s/{ch}"), ch)
        except Exception as e:
            errors.append(f"{ch}: {e}")
    status = closure_status(posts)

    delays = {}
    for a, fn in DELAY_SOURCES.items():
        try:
            delays[a] = fn()
        except Exception as e:
            errors.append(f"табло {a}: {e}")

    prev = {}
    if os.path.exists(STATE_FILE):
        try:
            prev = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            prev = {}
    prev_status, prev_delay = prev.get("status", {}), prev.get("delay_alert", {})
    reminded = dict(prev.get("reminded", {}))   # ключ "closed:<аэропорт>" / "delay:<аэропорт>" -> время последнего сообщения
    now_iso = now_utc().isoformat()

    def due(key):
        """Пора ли напомнить: прошло REMIND_MIN минут с последнего сообщения по этому ключу."""
        if not REMIND_MIN:
            return False
        last = reminded.get(key)
        return not last or (now_utc() - datetime.fromisoformat(last)) >= timedelta(minutes=REMIND_MIN)

    messages = []
    # 1) закрытие / открытие
    for a, cur in status.items():
        old = prev_status.get(a, {}).get("state", "open")
        if cur["state"] != old:
            if cur["state"] == "closed":
                line = f"🔴 ЗАКРЫТ: {a} — введены ограничения на приём и выпуск ({msk(cur['since'])})"
            elif cur.get("stale"):
                reminded.pop(f"closed:{a}", None)
                continue   # закрытие протухло — тихо возвращаем «открыт», без сообщения
            else:
                line = f"🟢 ОТКРЫТ: {a} — ограничения сняты ({msk(cur['since'])})"
            if cur.get("source"):
                line += f"\n{cur['source']}"
            messages.append(line)
            reminded[f"closed:{a}"] = now_iso
        elif cur["state"] == "closed" and due(f"closed:{a}"):
            hours = (now_utc() - datetime.fromisoformat(cur["since"])).total_seconds() / 3600 if cur.get("since") else 0
            messages.append(f"🔴 НАПОМИНАНИЕ: {a} по-прежнему закрыт — ограничения действуют {hours:.1f} ч "
                            f"(с {msk(cur['since'])})")
            reminded[f"closed:{a}"] = now_iso
        if cur["state"] == "open":
            reminded.pop(f"closed:{a}", None)

    # 2) массовые задержки (с гистерезисом)
    delay_alert = dict(prev_delay)
    stamp = now_utc().astimezone(MSK).strftime("%H:%M МСК")
    for a, d in delays.items():
        was = prev_delay.get(a, False)
        if not was and d["delayed"] > DELAY_LIMIT:
            delay_alert[a] = True
            messages.append(f"🟠 ЗАДЕРЖКИ: {a} — {d['delayed']} вылетов задержаны на {DELAY_MIN} мин и дольше "
                            f"(из {d['total']} ближайших, {stamp})\n" + ", ".join(d["flights"][:8]))
            reminded[f"delay:{a}"] = now_iso
        elif was and d["delayed"] <= DELAY_CLEAR:
            delay_alert[a] = False
            reminded.pop(f"delay:{a}", None)
            messages.append(f"🟢 ЗАДЕРЖКИ СНЯТЫ: {a} — задержанных на {DELAY_MIN}+ мин: {d['delayed']} ({stamp}).\n"
                            f"Про задержки в этом аэропорту больше не пишу — сообщу снова, только если задержанных "
                            f"станет больше {DELAY_LIMIT}.")
        elif was and due(f"delay:{a}"):
            messages.append(f"🟠 НАПОМИНАНИЕ: {a} — задержки продолжаются: {d['delayed']} вылетов на {DELAY_MIN}+ мин "
                            f"(из {d['total']} ближайших, {stamp})\n" + ", ".join(d["flights"][:8]))
            reminded[f"delay:{a}"] = now_iso

    new_state = {"status": status, "delay_alert": delay_alert, "reminded": reminded}
    if new_state != {"status": prev_status, "delay_alert": prev_delay, "reminded": prev.get("reminded", {})}:
        json.dump({"updated_at": now_utc().isoformat(), **new_state},
                  open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    tg = sms = None
    if messages:
        text = "\n\n".join(messages)[:3500]
        try:
            ok, info = send_telegram(text)
        except Exception as e:
            ok, info = False, f"ошибка отправки: {e}"
        tg = {"text": text, "sent": ok, "info": info}
        if os.path.exists(SMS_CONFIG):
            try:
                ok, info = send_sms(text)
            except Exception as e:
                ok, info = False, f"ошибка отправки: {e}"
            sms = {"sent": ok, "info": info}

    print(json.dumps({
        "checked_at": now_utc().isoformat(), "posts_scanned": len(posts), "errors": errors,
        "status": {a: s["state"] for a, s in status.items()},
        "delays": {a: {"delayed": d["delayed"], "total": d["total"]} for a, d in delays.items()},
        "delay_alert": delay_alert, "changes": messages, "telegram": tg, "sms": sms,
        "subscribers": len(load_subs()["chats"]),
        "tracked": sum(len(v) for v in load_tracked().values()), "tracked_sent": tracked_sent,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
