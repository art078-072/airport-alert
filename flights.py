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


def _hm(iso):
    return iso[11:16] if iso else "—"


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


def find_flight(code, num, date):
    """Все записи табло по рейсу на дату (может быть 2: вылет из одного аэропорта и прилёт в другой)."""
    found, errors = [], []
    for fn in (_svo, _led):
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
             + (f", расч. {_hm(d['est'])}" if d.get("est") and d["est"] != d.get("sched") else "")
             + (f", факт {_hm(d['actual'])}" if d.get("actual") else "")]
    delay = None
    ref = d.get("actual") or d.get("est")
    if ref and d.get("sched"):
        try:
            delay = int((datetime.fromisoformat(ref[:19]) - datetime.fromisoformat(d["sched"][:19])).total_seconds() // 60)
        except Exception:
            pass
    st = d.get("status") or "—"
    if delay and delay >= 15:
        st += f" (задержка {delay} мин)"
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
                parts.append(f"{n}: {_hm(a)} → {_hm(b)}")
            else:
                parts.append(f"{n}: {a or '—'} → {b}")
    return "; ".join(parts)
