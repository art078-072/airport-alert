"""Статус конкретного рейса и положение самолёта.

Источники: онлайн-табло Шереметьево (svo.aero, вылет/прилёт, любые даты) и Пулково
(pulkovoairport.ru, вылет/прилёт, ближайшие ~сутки); положение — ADS-B через api.adsb.lol
(бесплатно, без ключа; над Россией покрытие неполное, поэтому координаты есть не всегда).
"""
import json, re, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}

# IATA-код авиакомпании -> ICAO (позывной в ADS-B)
ICAO = {"SU": "AFL", "FV": "SDM", "DP": "PBD", "U6": "SVR", "UT": "UTA", "S7": "SBI", "N4": "NWS", "5N": "AUL",
        "EO": "KAR", "R3": "SYL", "Y7": "TYA", "WZ": "RWZ", "A4": "AZO", "ZF": "AZV", "7R": "RLU", "YC": "LLM",
        "4G": "GZP", "I8": "IZA", "6R": "TNO", "HZ": "SHU", "JA": "JAV", "IO": "IRA", "TK": "THY", "EK": "UAE",
        "QR": "QTR", "EY": "ETD", "FZ": "FDB", "J2": "AHY", "HY": "UZB", "KC": "KZR", "DV": "VSV", "B2": "BRU",
        "PC": "PGT", "XC": "CAI", "MS": "MSR", "AI": "AIC", "CA": "CCA", "MU": "CES", "CZ": "CSN", "HU": "CHH",
        "3U": "CSC", "W5": "IRM", "IR": "IRA", "OM": "MGL", "TC": "TAJ", "SZ": "SMR", "H9": "HIM", "LE": "LNK"}
LED_STATUS = {"SKD": "По расписанию", "DLY": "Задерживается", "OFB": "Вылетел", "RFB": "Вылетел", "XLD": "Отменён",
              "BRD": "Идёт посадка", "GTO": "Выход открыт", "CKN": "Регистрация", "LND": "Приземлился",
              "ARR": "Прибыл", "ONB": "Прибыл", "EXP": "Ожидается", "DIV": "Ушёл на запасной", "RTN": "Вернулся"}


def http_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def parse_flight(text):
    """'SU 284', 'su284', 'SU0284 21.09', 'FV6923 21.09.2026' -> ('SU', '284', date|None) или None."""
    m = re.match(r"^\s*([A-Za-z0-9]{2})\s?-?\s?0*(\d{1,4})(?:\s+(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?)?\s*$", text)
    if not m:
        return None
    code, num, d, mo, y = m.groups()
    code = code.upper()
    if not re.search(r"[A-Z]", code):          # два числа — не рейс
        return None
    date = None
    if d:
        year = datetime.now(MSK).year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
        try:
            date = datetime(year, int(mo), int(d)).date()
        except ValueError:
            return None
    return code, num, date


def _hm(iso, ref=None):
    """ЧЧ:ММ; если дата отличается от ref (ISO) — с датой дд.мм."""
    if not iso:
        return "—"
    if ref and iso[:10] != ref[:10]:
        return f"{iso[8:10]}.{iso[5:7]} {iso[11:16]}"
    return iso[11:16]


def _delay_text(minutes):
    if minutes < 120:
        return f"{minutes} мин"
    h, m = divmod(minutes, 60)
    if h < 48:
        return f"{h} ч {m:02d} мин"
    d, h = divmod(h, 24)
    return f"{d} дн {h} ч"


def _svo(code, num, date):
    d1 = f"{date}T00:00:00+03:00"
    d2 = f"{date + timedelta(days=1)}T00:00:00+03:00"
    out = []
    for direction in ("departure", "arrival"):
        url = ("https://www.svo.aero/bitrix/timetable/?direction=" + direction + "&dateStart=" + urllib.parse.quote(d1)
               + "&dateEnd=" + urllib.parse.quote(d2) + "&perPage=1000&page=0&locale=ru")
        for f in http_json(url).get("items", []):
            if f.get("co", {}).get("code") != code or str(f.get("flt", "")).lstrip("0") != num:
                continue
            dep = f["ad"] == "D"
            other = (f.get("mar2") if dep else f.get("mar1")) or {}
            city = other.get("city") or other.get("airport") or "?"
            iata = other.get("iata", "")
            out.append({
                "airport": "Шереметьево", "iata_here": "SVO", "dir": "D" if dep else "A",
                "flight": f"{code}{num}", "airline": f.get("co", {}).get("name", code),
                "route": f"Москва (SVO) → {city} ({iata})" if dep else f"{city} ({iata}) → Москва (SVO)",
                "sched": f.get("t_st"), "est": f.get("t_et"), "actual": f.get("t_at"),
                "status": f.get("vip_status_rus") or "", "terminal": f.get("term") or "",
                "gate": f.get("gate_id") or "", "belt": f.get("bbel_id") or "",
                "aircraft": f.get("aircraft_type_name") or "",
            })
    return out


def _led(code, num, date):
    out = []
    for t, p in (("departure", "OD"), ("arrival", "OA")):
        rows = http_json(f"https://pulkovoairport.ru/api/?col=400&type={t}",
                         headers={"X-Requested-With": "XMLHttpRequest",
                                  "Referer": "https://pulkovoairport.ru/passengers/departure/"})
        for f in rows:
            fn = re.sub(r"\s+", "", f.get(f"{p}_FLIGHT_NUMBER", ""))
            if not fn.upper().startswith(code) or fn[len(code):].lstrip("0") != num:
                continue
            sched = f.get(f"{p}_STD") if p == "OD" else f.get(f"{p}_STA")
            if not sched or sched[:10] != str(date):
                continue
            est = f.get(f"{p}_ETD") if p == "OD" else f.get(f"{p}_ETA")
            actual = f.get("OD_OFFBLOCK") if p == "OD" else (f.get("OA_ONBLOCK") or f.get("OA_ATA"))
            city = f.get(f"{p}_RAP_DESTINATION_NAME_RU" if p == "OD" else f"{p}_RAP_ORIGIN_NAME_RU") or \
                   f.get(f"{p}_RAP_NEXT_NAME_RU") or f.get(f"{p}_RAP_PREV_NAME_RU") or "?"
            iata = f.get(f"{p}_RAP_CODE_DESTINATION" if p == "OD" else f"{p}_RAP_CODE_ORIGIN") or ""
            if iata and iata not in city:
                city = f"{city} ({iata})"
            st = f.get(f"{p}_RFS_CODE") or ""
            out.append({
                "airport": "Пулково", "iata_here": "LED", "dir": "D" if p == "OD" else "A",
                "flight": f"{code}{num}", "airline": f.get(f"{p}_RAL_NAME_RUS") or code,
                "route": f"Санкт-Петербург (LED) → {city}" if p == "OD" else f"{city} → Санкт-Петербург (LED)",
                "sched": sched, "est": est, "actual": actual,
                "status": LED_STATUS.get(st, st), "terminal": f.get(f"{p}_RTRM_CODE") or "",
                "gate": f.get("OD_GATES") or "", "belt": f.get("OA_BAGGAGEBELTS") or "",
                "aircraft": f.get(f"{p}_RACT_ICAO_CODE") or "",
            })
    return out


# ---------------------------------------------------------------------------------------
# Сочи (aer.aero): табло отдаётся готовым HTML — вылеты и прилёты за вчера/сегодня/завтра
# ---------------------------------------------------------------------------------------
AER_ROW = re.compile(r'<a href="/flights/online-schedule/(\d+)/">\s*<div class="main-widget__content__item-block[^"]*"'
                     r'((?:\s+data-[\w-]+="[^"]*")+)\s*(?:style="[^"]*")?>(.*?)</a>', re.S)


def _aer_page(day):
    url = f"https://aer.aero/flights/online-schedule/?day_departure={day}&day_arrival={day}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def _aer_dt(text, fallback_date):
    """'19.09 17:35' / '17:35' / '' -> ISO-строка МСК или None."""
    text = (text or "").strip()
    m = re.match(r"(?:(\d{2})\.(\d{2})\s+)?(\d{2}):(\d{2})$", text)
    if not m:
        return None
    d, mo, hh, mm = m.groups()
    if d:
        year = fallback_date.year
        # переход через Новый год
        if int(mo) == 12 and fallback_date.month == 1:
            year -= 1
        elif int(mo) == 1 and fallback_date.month == 12:
            year += 1
        base = datetime(year, int(mo), int(d))
    else:
        base = datetime(fallback_date.year, fallback_date.month, fallback_date.day)
    return base.replace(hour=int(hh), minute=int(mm)).strftime("%Y-%m-%dT%H:%M:00+03:00")


def parse_aer(page, page_date):
    """Строки табло Сочи -> список карточек (dir D/A)."""
    out = []
    dep_at, arr_at = page.find('class="departure-cont"'), page.find('class="arrival-cont"')
    for m in AER_ROW.finditer(page):
        attrs = dict(re.findall(r'data-([\w-]+)="([^"]*)"', m.group(2)))
        body = m.group(3)
        is_dep = arr_at < 0 or m.start() < arr_at
        sched_txt = re.search(r'main-widget__discount">([^<]*)<', body)
        act = re.search(r'main-widget__discount-sum">\s*(?:<span>([^<]*)</span>)?\s*([^<]*)<', body)
        sched = _aer_dt(sched_txt.group(1), page_date) if sched_txt else \
            _aer_dt(f"{attrs.get('time', '00')}:{attrs.get('time-minute', '00')}", page_date)
        est = None
        if act:
            est = _aer_dt(((act.group(1) or "").strip() + " " + act.group(2).strip()).strip(), page_date)
        flt = re.search(r'main-widget-td1__flight">.*?<span>\s*([A-Z0-9]{2})-?\s?(\d{1,4})\s*</span>', body, re.S)
        if not flt:
            continue
        city = re.search(r'main-widget-td1--country bold">\s*(?:<span>)?([^<]+?)(?:</span>)?\s*<div', body)
        comp = re.search(r'main-widget-td1__company show-1024-up">(?:\s*<img[^>]*>)?\s*([^<]*)<', body)
        gate = re.search(r'main-widget__baggage">([^<]*)<', body)
        status = attrs.get("status", "").strip()
        actual = est if re.search(r"вылетел|прибыл|приземл", status, re.I) else None
        city_s = (city.group(1).strip().title() if city else "?")
        gate_s = (gate.group(1).strip() if gate else "")
        out.append({
            "airport": "Сочи", "iata_here": "AER", "dir": "D" if is_dep else "A",
            "flight": f"{flt.group(1)}{flt.group(2).lstrip('0')}", "airline": comp.group(1).strip() if comp else flt.group(1),
            "route": f"Сочи (AER) → {city_s}" if is_dep else f"{city_s} → Сочи (AER)",
            "sched": sched, "est": est if est != sched else None, "actual": actual,
            "status": status, "terminal": "", "gate": "" if gate_s in ("н/д", "") else gate_s, "belt": "",
            "aircraft": "", "aer_id": m.group(1),
        })
    return out


def aer_board(day="today"):
    """Табло Сочи за день: 'yesterday' | 'today' | 'tomorrow'."""
    today = datetime.now(MSK).date()
    page_date = {"yesterday": today - timedelta(days=1), "today": today, "tomorrow": today + timedelta(days=1)}[day]
    return parse_aer(_aer_page(day), page_date)


def _aer(code, num, date):
    today = datetime.now(MSK).date()
    days = {today - timedelta(days=1): "yesterday", today: "today", today + timedelta(days=1): "tomorrow"}
    if date not in days and not (today - timedelta(days=3) <= date <= today + timedelta(days=1)):
        return []
    def match(c):
        return c["flight"] == f"{code}{num}" and str(date) in ((c["sched"] or "")[:10], (c["est"] or "")[:10], (c["actual"] or "")[:10])
    found = [c for c in aer_board("today") if match(c)]
    if not found and date in days and days[date] != "today":
        found = [c for c in aer_board(days[date]) if match(c)]
    return found


def find_flight(code, num, date):
    """Все записи табло по рейсу на дату (может быть 2: вылет из одного аэропорта и прилёт в другой)."""
    found, errors = [], []
    for fn in (_svo, _led, _aer):
        try:
            found += fn(code, num, date)
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
    # убираем дубли (SVO отдаёт один рейс в нескольких страницах не должен, но на всякий случай)
    seen, uniq = set(), []
    for f in found:
        k = (f["airport"], f["dir"], f["sched"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq, errors


def position(code, num):
    """Положение по ADS-B. Возвращает dict или None."""
    cs = ICAO.get(code, code) + num
    try:
        d = http_json(f"https://api.adsb.lol/v2/callsign/{cs}", timeout=20)
    except Exception:
        return None
    ac = (d.get("ac") or [None])[0]
    if not ac:
        return None
    alt = ac.get("alt_baro")
    return {
        "callsign": cs, "reg": ac.get("r"), "type": ac.get("t"),
        "lat": ac.get("lat"), "lon": ac.get("lon"),
        "alt_m": round(alt * 0.3048) if isinstance(alt, (int, float)) else None,
        "on_ground": alt == "ground",
        "speed_kmh": round(ac["gs"] * 1.852) if ac.get("gs") else None,
        "track": ac.get("track"), "seen_s": ac.get("seen"),
    }


def in_air(card):
    """Рейс, по табло, сейчас в воздухе?"""
    s = (card.get("status") or "").lower()
    if card["dir"] == "D":
        return bool(card.get("actual")) and "верну" not in s and "отмен" not in s
    return "полет" in s or "полёт" in s or "ожида" in s and bool(card.get("est")) and not card.get("actual")


def format_card(card, pos=None):
    d = card
    date = (d.get("sched") or "")[:10]
    date_h = f"{date[8:10]}.{date[5:7]}" if date else ""
    lines = [f"✈️ {d['flight']} — {d['airline']}" + (f", {d['aircraft']}" if d.get("aircraft") else ""),
             d["route"],
             f"📅 {date_h}, {'вылет' if d['dir'] == 'D' else 'прилёт'}: план {_hm(d.get('sched'))}"
             + (f", расч. {_hm(d['est'], d.get('sched'))}" if d.get("est") and d["est"] != d.get("sched") else "")
             + (f", факт {_hm(d['actual'], d.get('sched'))}" if d.get("actual") else "")]
    delay = None
    ref = d.get("actual") or d.get("est")
    if ref and d.get("sched"):
        try:
            delay = int((datetime.fromisoformat(ref[:19]) - datetime.fromisoformat(d["sched"][:19])).total_seconds() // 60)
        except Exception:
            pass
    st = d.get("status") or "—"
    if delay and delay >= 15:
        st += f" (задержка {_delay_text(delay)})"
    lines.append(f"Статус: {st}")
    extra = []
    if d.get("terminal"):
        extra.append(f"терминал {d['terminal']}")
    if d.get("gate"):
        extra.append(f"выход {d['gate']}")
    if d.get("belt"):
        extra.append(f"багаж: лента {d['belt']}")
    if extra:
        lines.append("🏢 " + ", ".join(extra) + f" ({d['airport']})")
    if pos:
        if pos.get("on_ground"):
            lines.append(f"🛰 Борт {pos.get('reg') or ''} на земле")
        else:
            p = []
            if pos.get("alt_m") is not None:
                p.append(f"высота {pos['alt_m']} м")
            if pos.get("speed_kmh"):
                p.append(f"скорость {pos['speed_kmh']} км/ч")
            if pos.get("reg"):
                p.append(pos["reg"])
            lines.append("🛰 В воздухе: " + (", ".join(p) if p else "сигнал есть") +
                         ("" if pos.get("lat") is not None else " — координат нет (ADS-B над РФ ловится не везде)"))
    return "\n".join(lines)


def snapshot(card):
    """Поля, изменения которых интересны подписчику."""
    return {k: card.get(k) for k in ("est", "actual", "status", "terminal", "gate", "belt")}


def describe_change(old, new, card):
    names = {"est": "расчётное время", "actual": "фактическое время", "status": "статус",
             "terminal": "терминал", "gate": "выход", "belt": "лента багажа"}
    parts = []
    for k, n in names.items():
        a, b = old.get(k), new.get(k)
        if a != b and b:
            if k in ("est", "actual"):
                parts.append(f"{n}: {_hm(a, card.get('sched'))} → {_hm(b, card.get('sched'))}")
            else:
                parts.append(f"{n}: {a or '—'} → {b}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------------------
# Разбор пересланного бронирования (письмо/маршрут-квитанция): номера рейсов + даты
# ---------------------------------------------------------------------------------------
MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9,
          "окт": 10, "ноя": 11, "дек": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
          "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
FLIGHT_RE = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9]|[0-9][A-Z])\s?-?\s?(\d{2,4})(?![0-9])")
DATE_RE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})|(\d{1,2})\s+([а-яА-Яa-zA-Z]{3,8})\.?\s*(\d{4})?")
# слова, после которых двухбуквенные сочетания — не авиакомпании (терминал, выход, ряд и т.п.)
NOISE = {"PNR", "ID", "NO", "ОК", "OK", "RU", "EN", "UP", "GB", "US", "KG", "PC", "PS"}


def _parse_date(m, year_hint):
    if m.group(1):
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y) + 2000 if len(y) == 2 else int(y)
    else:
        d, mon, y = int(m.group(4)), m.group(5).lower()[:3], m.group(6)
        if mon not in MONTHS:
            return None
        mo, y = MONTHS[mon], int(y) if y else year_hint
    try:
        return datetime(y, mo, d).date()
    except ValueError:
        return None


def parse_itinerary(text, today):
    """Находит пары (рейс, дата) в свободном тексте: каждому рейсу — ближайшая по тексту дата.
    Возвращает список (code, num, date), без дублей, только даты не раньше вчера."""
    up = text.upper()
    dates = [(m.start(), _parse_date(m, today.year)) for m in DATE_RE.finditer(text)]
    dates = [(p, d) for p, d in dates if d]
    out, seen = [], set()
    for m in FLIGHT_RE.finditer(up):
        code, num = m.group(1), m.group(2).lstrip("0")
        if code in NOISE or not num or code.isdigit():
            continue
        # только известные авиакомпании либо код с цифрой (U6, S7, 5N ...) — чтобы не ловить «ряд 12» и т.п.
        if code not in ICAO and not re.search(r"\d", code):
            continue
        if not dates:
            continue
        pos = m.start()
        # ближайшая дата после рейса в пределах 300 символов, иначе ближайшая до
        after = [(p - pos, d) for p, d in dates if p >= pos and p - pos < 300]
        before = [(pos - p, d) for p, d in dates if p < pos]
        cand = min(after)[1] if after else (min(before)[1] if before else None)
        if not cand or cand < today - timedelta(days=1):
            continue
        key = (code, num, cand)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out
