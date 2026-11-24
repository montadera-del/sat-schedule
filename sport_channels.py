#!/usr/bin/env python3
"""
جدول المباريات + كل القنوات الناقلة + القمر والتردد والتشفير.

المصادر:
  1. LiveSoccerTV  : قائمة مباريات اليوم، ولكل مباراة القنوات الناقلة في كل دولة.
  2. KingOfSat     : لكل قناة: القمر، التردد، الاستقطاب، معدل الترميز، التشفير.

المخرجات:
  out/matches.json   ← يقرأه التطبيق
  out/unmatched.txt  ← قنوات لم يُعثر لها على تردد (أضف لها alias في config.json)
  cache/channels_db.json ← ذاكرة الترددات (تُحدّث كل channel_cache_days يوم)

التشغيل:
  python sport_channels.py                 # اليوم
  python sport_channels.py --date 2026-11-25
  python sport_channels.py --all           # بدون فلترة البطولات
  python sport_channels.py --limit 3 --debug   # تجربة سريعة + حفظ صفحات HTML في debug/
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
LST = "https://www.livesoccertv.com"
KOS = "https://en.kingofsat.net"

CFG: dict = {}
DEBUG = False
_session = None


# ───────────────────────────── شبكة ─────────────────────────────
import threading
_tls = threading.local()


def _get_session():
    """جلسة لكل خيط: curl_cffi (يقلّد كروم) وإلا requests."""
    if getattr(_tls, "s", None) is not None:
        return _tls.s
    _tls.s = _new_session()
    return _tls.s


def _new_session():
    _session = None
    try:
        from curl_cffi import requests as creq  # type: ignore
        return creq.Session(impersonate="chrome")
    except ImportError:
        import requests
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
    return _session


_rate_lock = threading.Lock()
_last_by_host: dict[str, float] = {}


def _host_delay(url: str) -> tuple[str, float]:
    from urllib.parse import urlparse
    host = urlparse(url).netloc
    for k, v in CFG.get("host_delays", {}).items():
        if k in host:
            return host, float(v)
    return host, float(CFG.get("request_delay_seconds", 0.4))


def fetch(url: str, debug_name: str | None = None) -> str | None:
    """طلب مع فاصل زمني خاص بكل موقع (آمن مع عدة خيوط)، وانتظار أطول عند 429."""
    host, delay = _host_delay(url)
    for attempt in range(4):
        while True:
            with _rate_lock:
                wait = delay - (time.time() - _last_by_host.get(host, 0))
                if wait <= 0:
                    _last_by_host[host] = time.time()
                    break
            time.sleep(wait)
        try:
            r = _get_session().get(url, timeout=30)
            if r.status_code == 200:
                if DEBUG and debug_name:
                    d = ROOT / "debug"
                    d.mkdir(exist_ok=True)
                    (d / f"{debug_name}.html").write_text(r.text, encoding="utf-8")
                return r.text
            if r.status_code == 429:
                pause = 30 * (attempt + 1)
                log(f"  … الموقع طلب التمهّل، انتظار {pause} ثانية")
                with _rate_lock:
                    _last_by_host[host] = time.time() + pause
                continue
            log(f"  ! HTTP {r.status_code} ← {url}")
            if r.status_code in (403, 404):
                return None
        except Exception as e:  # noqa: BLE001
            log(f"  ! {type(e).__name__}: {e} ← {url}")
        time.sleep(3 * (attempt + 1))
    return None


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


# ─────────────────────────── أدوات نصوص ───────────────────────────
def norm(s: str) -> str:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)          # MagentaSport → Magenta Sport
    s = re.sub(r"(\d)([A-Za-z])", r"\1 \2", s)          # 5Sport → 5 Sport
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("+", " plus ").replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


QUALITY = {"hd", "uhd", "fhd", "sd", "4k", "hevc", "tv", "channel"}


def tokens(s: str) -> set[str]:
    return {t for t in norm(s).split() if t not in QUALITY}


def slug_safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s)[:60]


# ───────────────────── 1) مباريات اليوم من LST ─────────────────────
def competition_info(name: str) -> tuple[int, str] | None:
    """ترتيب البطولة (حسب القوة، من config.json) واسمها العربي، أو None إن لم تكن ضمن القائمة."""
    if re.search(r"women|femen|frauen|u-?\d\d|youth|reserve|primavera", name, re.I):
        return None
    for i, (pat, ar) in enumerate(CFG["competitions"]):
        if re.search(pat, name, re.I):
            return i, ar
    return None


def parse_schedule(html: str) -> list[dict]:
    """صفوف tr.matchrow بالترتيب، وعنوان البطولة من span.r_compname الذي يسبقها."""
    soup = BeautifulSoup(html, "html.parser")
    current_comp = ""
    out, seen = [], set()
    for el in soup.select("tr.sortable_comp, tr.matchrow"):
        if "sortable_comp" in (el.get("class") or []):
            link = el.find("a", class_="r_complink") or el
            current_comp = link.get_text(" ", strip=True)
            continue
        a = el.find("a", href=re.compile(r"/match/"))
        if not a:
            continue
        url = urljoin(LST, a["href"].split("#")[0])
        if url in seen:
            continue
        seen.add(url)
        title = (a.get("title") or a.get_text(" ", strip=True)).strip()
        if " vs " not in title:
            continue
        ts = el.find(class_="ts", attrs={"dv": True})
        ko_utc = None
        if ts and ts["dv"].isdigit():
            ko_utc = datetime.fromtimestamp(int(ts["dv"]) / 1000, timezone.utc).isoformat()
        home, away = [x.strip() for x in title.split(" vs ", 1)]
        out.append({
            "competition": current_comp, "home": home, "away": away, "url": url,
            "lst_id": el.get("id"), "ko_utc": ko_utc, "has_tv": el.get("data-has-tv") != "0",
        })
    return out


# ───────────────── 2) القنوات الناقلة لكل مباراة ─────────────────
def _ld_objects(soup):
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "")
        except Exception:  # noqa: BLE001
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            o = stack.pop(0)
            if isinstance(o, dict):
                if "@graph" in o:
                    stack.extend(o["@graph"])
                yield o


def parse_match_page(html: str) -> tuple[str | None, dict[str, list[str]], dict]:
    """من JSON-LD: (startDate, {القناة: [الدول]}, معلومات إضافية)."""
    soup = BeautifulSoup(html, "html.parser")
    start, extra = None, {}
    channels: dict[str, list[str]] = {}
    for o in _ld_objects(soup):
        t = o.get("@type")
        if t == "SportsEvent":
            start = o.get("startDate") or start
            extra["venue"] = (o.get("location") or {}).get("name", "")
            extra["status"] = (o.get("eventStatus") or "").rsplit("/", 1)[-1]
        elif t == "BroadcastEvent":
            svc = o.get("publishedOn") or {}
            name = (svc.get("name") or "").strip()
            if not name:
                continue
            area = svc.get("areaServed") or {}
            areas = area if isinstance(area, list) else [area]
            lst = channels.setdefault(name, [])
            for ar in areas:
                c = ar.get("name") if isinstance(ar, dict) else str(ar)
                if c and c not in lst:
                    lst.append(c)
    # الجدول الكامل (JSON-LD أحياناً يحوي جزءاً فقط من القنوات)
    table = soup.find("table", class_="ichannels")
    if table:
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            country = tds[0].get_text(" ", strip=True)
            for a in tds[1].find_all("a", href=re.compile(r"/channels/")):
                name = a.get_text(" ", strip=True)
                if not name:
                    continue
                lst = channels.setdefault(name, [])
                if country and country not in lst:
                    lst.append(country)
    return start, channels, extra


# ──────────────── 3) القمر والتردد من KingOfSat ────────────────
CAS = ["Conax", "Viaccess", "Irdeto", "Nagravision", "Nagra", "Videoguard", "VideoGuard", "Mediaguard",
       "Cryptoworks", "BISS", "PowerVu", "Verimatrix", "Tandberg", "Seca", "DRE-Crypt", "Bulcrypt", "Clear"]


def parse_kos(html: str) -> list[dict]:
    """جداول KingOfSat: tr.frequency-row (التردد) يليه table.kos-channel-table (القنوات)."""
    soup = BeautifulSoup(html, "html.parser")
    results, tp = [], None
    for el in soup.select("tr.frequency-row, table.kos-channel-table tr[data-channel-id]"):
        tds = el.find_all("td", recursive=False)
        if "frequency-row" in (el.get("class") or []):
            pos = re.search(r"(\d{1,3}(?:\.\d)?)\s*°\s*([EW])", tds[0].get_text())
            fp = re.search(r"(\d{4,5})(?:\.\d+)?\s*([HVLR])", tds[2].get_text(" ", strip=True))
            if not (pos and fp):
                tp = None
                continue
            sat_a = tds[1].find("a")
            sr = tds[7].get_text(strip=True) if len(tds) > 7 else ""
            fec = tds[8].get_text(strip=True) if len(tds) > 8 else ""
            tp = {
                "position": f"{float(pos.group(1)):.1f}{pos.group(2)}",
                "satellite": (sat_a.get_text(strip=True) if sat_a else tds[1].get_text(strip=True)),
                "frequency": int(fp.group(1)),
                "polarization": fp.group(2),
                "symbol_rate": int(sr) if sr.isdigit() else None,
                "fec": fec,
                "standard": tds[5].get_text(strip=True) if len(tds) > 5 else "",
            }
            continue
        if tp is None:
            continue
        ch = el.find("td", class_="ch")
        if not ch:
            continue
        a = ch.find("a")
        name = ch.get_text(" ", strip=True)
        cid = (a.get("title") or "").replace("Id:", "").strip() if a else ""
        enc_td = el.find("span", class_="encryption-label") or (tds[5] if len(tds) > 5 else None)
        enc = " / ".join(enc_td.stripped_strings) if enc_td else ""
        def cell(cls):
            c = el.find("td", class_=cls)
            return c.get_text(" ", strip=True) if c else ""
        results.append({
            **tp, "name": name, "kos_id": cid, "country": cell("pays"), "package": cell("bq"),
            "encryption": enc,
            "free": enc == "" or enc.lower() in ("clear", "fta", "free", "free to air"),
        })
    return results


_html_cache: dict[str, str | None] = {}


def fetch_cached(url: str, debug_name: str | None = None) -> str | None:
    if url not in _html_cache:
        _html_cache[url] = fetch(url, debug_name)
    return _html_cache[url]


def alias_for(name: str) -> tuple[str, list[str]]:
    """يرجع (نص البحث في KingOfSat, [الأسماء المقبولة]). يدعم alias نصي أو {query, match} أو regex."""
    a = CFG["aliases"].get(name)
    if isinstance(a, dict):
        return a["query"], a.get("match") or [a["query"]]
    if isinstance(a, str):
        return a, [a]
    for pat, rep in CFG.get("regex_aliases", []):
        if re.fullmatch(pat, name, flags=re.I):
            new = re.sub(pat, rep, name, flags=re.I)
            return new, [new]
    return name, [name]


def is_online_only(name: str) -> bool:
    low = name.lower()
    return any(re.search(p, low) for p in CFG["online_only_patterns"])


def same_channel(lst_name: str, kos: dict, use_id: bool = False) -> bool:
    a = tokens(lst_name)
    return bool(a) and a == tokens(kos["kos_id"] if use_id else kos["name"])


def needs_lookup(name: str, db: dict) -> bool:
    c = db.get(name)
    if not c:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(c["checked"])
    return age > timedelta(days=CFG.get("channel_cache_days", 7))


def resolve_channel(name: str, db: dict, countries: list[str] | None = None) -> dict:
    """يرجع {"status": "sat"/"online"/"unmatched", "sat": [...]}، مع ذاكرة محلية."""
    cached = db.get(name)
    max_age = timedelta(days=CFG.get("channel_cache_days", 7))
    if cached and datetime.fromisoformat(cached["checked"]) > datetime.now(timezone.utc) - max_age:
        return cached
    entry = {"status": "online", "sat": [], "checked": datetime.now(timezone.utc).isoformat()}
    if not is_online_only(name):
        query, targets = alias_for(name)
        queries = [query]
        split = re.sub(r"(\d)([A-Za-z])", r"\1 \2", re.sub(r"([a-z])([A-Z])", r"\1 \2", query))
        if split != query:
            queries.append(split)
        words = re.sub(r"\b(HD|SD|UHD|4K)\b", "", split, flags=re.I).split()
        country_words = {norm(c) for c in (countries or [])} | {
            "croatia", "serbia", "romania", "greece", "poland", "italy", "italia", "france", "germany",
            "spain", "portugal", "turkey", "israel", "hungary", "bulgaria", "slovenia", "uk", "hr", "srb"}
        if len(words) > 2 and targets == [query] and norm(words[-1]) in country_words:
            queries.append(" ".join(words[:-1]))      # بدون اسم الدولة في آخر الاسم
        found = []
        for q in dict.fromkeys(queries):
            html = fetch_cached(f"{KOS}/find.php?question={quote_plus(q)}", f"kos_{slug_safe(q)}")
            if not html:
                continue
            rows = parse_kos(html)
            tg = targets + [q]
            found = [r for r in rows if any(same_channel(t, r) for t in tg)] or \
                    [r for r in rows if r["kos_id"] and any(same_channel(t, r, use_id=True) for t in tg)]
            if found:
                break
        uniq = {}
        for r in found:
            uniq[(r["position"], r["frequency"], r["polarization"])] = r
        entry["sat"] = list(uniq.values())
        entry["status"] = "sat" if entry["sat"] else "unmatched"
    db[name] = entry
    return entry


def sat_sort_key(s: dict):
    pr = CFG["satellite_priority"]
    return (0 if s["free"] else 1, pr.index(s["position"]) if s["position"] in pr else len(pr), s["frequency"])


# ───────────────────────────── الوقت ─────────────────────────────
def to_local(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(CFG["timezone"]))


ARAB_COUNTRIES = {
    "Algeria", "Bahrain", "Comoros", "Djibouti", "Egypt", "Iraq", "Jordan", "Kuwait", "Lebanon", "Libya",
    "Mauritania", "Morocco", "Oman", "Palestine", "Qatar", "Saudi Arabia", "Somalia", "Sudan", "Syria",
    "Tunisia", "United Arab Emirates", "Yemen"}
NON_ARABIC = re.compile(r"english|french|\ben\b|\bfr\b", re.I)
AR_DAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
AR_MONTHS = ["كانون الثاني", "شباط", "آذار", "نيسان", "أيار", "حزيران", "تموز", "آب", "أيلول",
             "تشرين الأول", "تشرين الثاني", "كانون الأول"]


def ar_date(dt: datetime) -> str:
    return f"{AR_DAYS[dt.weekday()]} {dt.day} {AR_MONTHS[dt.month - 1]}"


def ar_12h(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'ص' if dt.hour < 12 else 'م'}"


# ─────────────────────── 365Scores: أسماء عربية ───────────────────────
S365 = ("https://webws.365scores.com/web/games/?langId={lang}&timezoneName=UTC&userCountryId=-1"
        "&appTypeId=5&sports=1&startDate={d}&endDate={d}")


def fetch_365(day: date) -> list[dict]:
    """مباريات اليوم من 365Scores بالإنجليزي والعربي (نفس الـ id) لترجمة الفرق وإضافة رقم المباراة للنتيجة المباشرة."""
    games: dict[int, dict] = {}
    for d in (day - timedelta(days=1), day):          # التوقيت UTC، ويوم بغداد يبدأ 21:00 UTC من اليوم السابق
        ds = d.strftime("%d/%m/%Y")
        for lang in (1, 27):
            txt = fetch(S365.format(lang=lang, d=ds))
            if not txt:
                continue
            try:
                data = json.loads(txt)
            except ValueError:
                continue
            comps = {c["id"]: c.get("name", "") for c in data.get("competitions", [])}
            for g in data.get("games", []):
                e = games.setdefault(g["id"], {"id": g["id"], "start": g.get("startTime")})
                suffix = "en" if lang == 1 else "ar"
                e[f"home_{suffix}"] = (g.get("homeCompetitor") or {}).get("name", "")
                e[f"away_{suffix}"] = (g.get("awayCompetitor") or {}).get("name", "")
                e[f"comp_{suffix}"] = comps.get(g.get("competitionId"), "")
                for side in ("home", "away"):
                    c = g.get(f"{side}Competitor") or {}
                    if c.get("id"):
                        e[f"{side}_logo"] = (
                            "https://imagecache.365scores.com/image/upload/f_png,w_64,h_64,c_limit,q_auto:eco,"
                            f"dpr_1,d_Competitors:default1.png/v{c.get('imageVersion', 1)}/Competitors/{c['id']}")
    return [g for g in games.values() if g.get("home_en") and g.get("start")]


def _sim(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def match_365(m: dict, ko: datetime, games: list[dict]) -> dict | None:
    best, best_score = None, 0.0
    for g in games:
        try:
            gs = datetime.fromisoformat(g["start"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if abs((gs - ko).total_seconds()) > 20 * 60:
            continue
        sc = (_sim(m["home"], g["home_en"]) + _sim(m["away"], g["away_en"])) / 2
        sc = max(sc, (_sim(m["home"], g["away_en"]) + _sim(m["away"], g["home_en"])) / 2)
        if sc > best_score:
            best, best_score = g, sc
    return best if best_score >= 0.55 else None


# ───────────────────────────── التشغيل ─────────────────────────────
MATCH_CACHE: dict = {}


def match_channels(m: dict):
    """قنوات المباراة مع ذاكرة (match_cache_hours) حتى لا نطلب نفس الصفحة كل مرة."""
    key = m["url"]
    c = MATCH_CACHE.get(key)
    max_age = timedelta(hours=CFG.get("match_cache_hours", 6))
    if c and datetime.now(timezone.utc) - datetime.fromisoformat(c["checked"]) < max_age:
        return c["start"], c["channels"], c["extra"]
    html = fetch(m["url"], f"match_{slug_safe(m['home'])}")
    if not html:
        return (c["start"], c["channels"], c["extra"]) if c else None
    start_iso, ch_map, extra = parse_match_page(html)
    MATCH_CACHE[key] = {"start": start_iso, "channels": ch_map, "extra": extra,
                        "checked": datetime.now(timezone.utc).isoformat()}
    return start_iso, ch_map, extra


def build_match(m: dict, db: dict, unmatched: dict, sat_count: dict, sat_names: dict) -> dict | None:
    got = match_channels(m)
    if not got:
        return None
    start_iso, ch_map, extra = got
    ko = to_local(start_iso) or m["ko"]
    db_path = ROOT / "cache" / "channels_db.json"

    # البحث عن ترددات القنوات الجديدة بالتوازي (القنوات المحفوظة لا تُطلب مرة أخرى)
    todo = [(n, c) for n, c in ch_map.items()
            if not (any(x in ARAB_COUNTRIES for x in c) and NON_ARABIC.search(n)) and needs_lookup(n, db)]
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=CFG.get("workers", 6)) as ex:
            list(ex.map(lambda nc: resolve_channel(nc[0], db, nc[1]), todo))
        db_path.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")

    arab, others, arab_online = [], [], []
    for name, countries in ch_map.items():
        in_arab = any(c in ARAB_COUNTRIES for c in countries)
        if in_arab and NON_ARABIC.search(name):          # beIN English / French … تُلغى
            continue
        info = resolve_channel(name, db, countries)
        if info["status"] != "sat":
            if in_arab and name not in arab_online:
                arab_online.append(name)             # صاحبة الحقوق على الإنترنت: بخانة "يعرض على" فقط
            if info["status"] == "unmatched":
                unmatched[name] = countries
            continue
        sats = sorted(info["sat"], key=sat_sort_key)
        for s_ in sats:
            s_["satellite_ar"] = CFG["satellites_ar"].get(s_["position"], s_["satellite"])
            sat_count[s_["position"]] = sat_count.get(s_["position"], 0) + 1
            sat_names.setdefault(s_["position"], s_["satellite_ar"])
        ident = alias_for(name)[1][0].lower()           # نفس القناة باسمين → قناة واحدة
        dup = next((c for c in arab + others if c["_id"] == ident), None)
        if dup:
            dup["countries"] += [c for c in countries if c not in dup["countries"]]
            if dup in others and in_arab:
                others.remove(dup)
                dup["arab_rights"] = True
                arab.append(dup)
            continue
        m_bein = re.fullmatch(r"beIN Sports (?:HD |Arabia )?((?:Xtra |Max )?\d*)(?: HD)?", name, re.I)
        shown = f"beIN SPORTS {m_bein.group(1).upper()}".strip() if m_bein and m_bein.group(1) else name
        ch = {"_id": ident, "name": shown, "countries": countries, "arab_rights": in_arab,
              "free": any(x["free"] for x in sats), "sat": sats}
        (arab if in_arab else others).append(ch)

    for c in arab + others:
        c.pop("_id", None)
    key = lambda c: (not c["free"], sat_sort_key(c["sat"][0]))
    arab.sort(key=key)
    others.sort(key=key)
    broadcast_on = [c["name"] for c in arab] or [
        n for n in arab_online if not re.search(r"connect|\btod\b", n, re.I)] or arab_online
    return {
        "id": m["lst_id"] or m["url"].rstrip("/").rsplit("/", 1)[-1],
        "kickoff": ko.isoformat(),
        "time_ar": ar_12h(ko),
        "venue": extra.get("venue", ""),
        "status": extra.get("status", ""),
        "broadcast_on": broadcast_on,
        "channels": arab + others,
        "source_url": m["url"],
        "_ko": ko,
    }


def day_label(d: date, today: date) -> str:
    return {-1: "أمس", 0: "اليوم", 1: "غداً"}.get((d - today).days, ar_date(datetime.combine(d, datetime.min.time())))


def main():
    global CFG, DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="أول يوم YYYY-MM-DD بتوقيت بغداد (الافتراضي اليوم)")
    ap.add_argument("--days", type=int, help="عدد الأيام (الافتراضي من config: اليوم + days_ahead)")
    ap.add_argument("--all", action="store_true", help="كل البطولات حتى غير الموجودة في config")
    ap.add_argument("--limit", type=int, default=0, help="أقصى عدد مباريات لكل يوم (للتجربة)")
    ap.add_argument("--debug", action="store_true", help="حفظ صفحات HTML في مجلد debug/")
    args = ap.parse_args()

    CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    DEBUG = args.debug
    tz = ZoneInfo(CFG["timezone"])
    today = datetime.now(tz).date()
    first = date.fromisoformat(args.date) if args.date else today
    n_days = args.days or (1 + CFG.get("days_ahead", 2))
    days = [first + timedelta(days=i) for i in range(n_days)]

    db_path = ROOT / "cache" / "channels_db.json"
    db_path.parent.mkdir(exist_ok=True)
    db = json.loads(db_path.read_text(encoding="utf-8")) if db_path.exists() else {}
    mc_path = ROOT / "cache" / "match_cache.json"
    if mc_path.exists():
        MATCH_CACHE.update(json.loads(mc_path.read_text(encoding="utf-8")))

    # جداول LST (بتوقيت مختلف) ليوم قبل أول يوم إلى يوم بعد آخر يوم
    lst, seen = [], set()
    d = days[0] - timedelta(days=1)
    while d <= days[-1] + timedelta(days=1):
        html = fetch(f"{LST}/schedules/{d.isoformat()}/", f"schedule_{d}")
        sc_path = ROOT / "cache" / f"schedule_{d}.json"
        if html:
            rows = parse_schedule(html)
            sc_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        elif sc_path.exists():                       # الموقع رفض الطلب: آخر نسخة محفوظة لهذا اليوم
            log(f"  … جدول {d} من النسخة المحفوظة")
            rows = json.loads(sc_path.read_text(encoding="utf-8"))
        else:
            rows = []
        for m in rows:
            if m["url"] not in seen:
                seen.add(m["url"])
                lst.append(m)
        d += timedelta(days=1)

    unmatched: dict = {}
    sat_count: dict[str, int] = {}
    sat_names: dict[str, str] = {}
    comp_seen: dict[str, dict] = {}
    out_days = []
    for day in days:
        todays = []
        for m in lst:
            ko = to_local(m["ko_utc"])
            if ko is None or ko.date() != day or not m["has_tv"]:
                continue
            ci = competition_info(m["competition"])
            if ci is None and not args.all:
                continue
            m = {**m, "ko": ko, "rank": ci[0] if ci else 999, "comp_ar": ci[1] if ci else m["competition"]}
            todays.append(m)
        todays.sort(key=lambda m: (m["rank"], m["ko"]))
        log(f"• {day}: {len(todays)} مباراة")
        games365 = fetch_365(day) if todays else []

        groups: dict[str, dict] = {}
        for i, m in enumerate(todays):
            if args.limit and i >= args.limit:
                break
            log(f"  [{i + 1}/{len(todays)}] {m['ko']:%H:%M} {m['home']} × {m['away']}  ({m['comp_ar']})")
            r = build_match(m, db, unmatched, sat_count, sat_names)
            if not r:
                continue
            g = match_365(m, r["_ko"], games365)
            r.update({
                "home": (g or {}).get("home_ar") or m["home"],
                "away": (g or {}).get("away_ar") or m["away"],
                "home_en": m["home"], "away_en": m["away"],
                "home_logo": (g or {}).get("home_logo"), "away_logo": (g or {}).get("away_logo"),
                "s365_id": (g or {}).get("id"),          # للتطبيق: النتيجة والدقيقة المباشرة من 365Scores
            })
            if g and (g["home_en"] and _sim(m["home"], g["away_en"]) > _sim(m["home"], g["home_en"])):
                r["home"], r["away"] = g["away_ar"], g["home_ar"]
                r["home_logo"], r["away_logo"] = g.get("away_logo"), g.get("home_logo")
            r.pop("_ko")
            mc_path.write_text(json.dumps(MATCH_CACHE, ensure_ascii=False), encoding="utf-8")
            grp = groups.setdefault(m["competition"], {
                "competition": m["comp_ar"], "competition_en": m["competition"],
                "rank": m["rank"], "matches": []})
            grp["matches"].append(r)
            comp_seen.setdefault(m["competition"], {"name_ar": m["comp_ar"], "name_en": m["competition"],
                                                    "rank": m["rank"]})
        out_days.append({
            "date": day.isoformat(),
            "label": day_label(day, today),
            "date_ar": ar_date(datetime.combine(day, datetime.min.time())),
            "competitions": sorted(groups.values(), key=lambda g: g["rank"]),
        })

    od = ROOT / "out"
    od.mkdir(exist_ok=True)
    payload = {
        "generated": datetime.now(tz).isoformat(timespec="seconds"),
        "timezone": CFG["timezone"],
        # للتطبيق: قوائم الاختيار (الأقمار والبطولات) عند أول دخول وفي الإعدادات
        "satellites": [{"position": p, "name_ar": sat_names[p], "channels": n}
                       for p, n in sorted(sat_count.items(), key=lambda kv: sat_sort_key(
                           {"free": True, "position": kv[0], "frequency": 0}))],
        # كل البطولات المعروفة (حتى بدون مباريات اليوم) حتى يختار المستخدم منها
        "competitions": [{"name_ar": ar, "name_en": (next((c["name_en"] for c in comp_seen.values() if c["name_ar"] == ar), "")),
                          "rank": i} for i, ar in enumerate(dict.fromkeys(a for _, a in CFG["competitions"]))],
        "days": out_days,
    }
    (od / "matches.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (od / "unmatched.txt").write_text(
        "\n".join(f"{k}\t{', '.join(v)}" for k, v in sorted(unmatched.items())), encoding="utf-8")
    n_m = sum(len(g["matches"]) for d_ in out_days for g in d_["competitions"])
    log(f"✓ {len(out_days)} أيام، {n_m} مباراة، {len(unmatched)} قناة بلا تردد")


if __name__ == "__main__":
    main()
