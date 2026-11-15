#!/usr/bin/env python3
"""
РПЛ Arena — сервер данных (прокси к Flashscore)
===============================================

Локально:  python rpl_flashscore_proxy.py            (http://localhost:8765/)
Хостинг:   PORT=8080 python rpl_flashscore_proxy.py   (слушает 0.0.0.0, отдаёт и сайт, и API)

Нужен только Python 3.9+ (без сторонних библиотек).
Положи файл рядом с rpl_arena_v6.html и открой http://localhost:8765/ — приложение подключится само.

Эндпоинты (все отдают JSON, CORS открыт):
  GET /health                       — состояние сервера и источника (всегда 200, пока сервер жив)
  GET /api/season[?full=1]          — календарь и результаты сезона (live-счёт тоже)
  GET /api/squads                   — составы 16 клубов + статистика игроков (РПЛ и все турниры)
  GET /api/match/<id>               — события, статистика, составы и рейтинги матча
  GET /api/player/<slug>/<id>       — фото, полное имя, возраст, стоимость, контракт игрока

Как это устроено надёжнее, чем «ходить во Flashscore на каждый запрос»:
  * сезон и составы обновляются в фоне (раз в минуту при live/близких матчах, иначе раз в 10 минут;
    составы — раз в 6 часов), пользователь получает готовый снимок из памяти мгновенно;
  * каждый запрос к Flashscore — с повторами (экспоненциальная пауза, Retry-After), общим лимитом
    параллельности и проверкой, что вернулась настоящая страница, а не заглушка/антибот;
  * если Flashscore недоступен, отдаётся последний удачный снимок с пометкой meta.stale=true;
  * частичный сбой (не ответили 3 страницы клуба из 32) не теряет остальные данные;
  * снимки сохраняются на диск (CACHE_DIR, по умолчанию ./.cache) и переживают перезапуск;
  * завершённые матчи кэшируются навсегда, неизвестные id не пробрасываются во Flashscore.

Это неофициальный доступ к публичным страницам Flashscore: при изменениях на их стороне
разбор может сломаться, а условия использования данных нужно проверить до публичного запуска.

Настройки окружения (все необязательны):
  PORT, HOST           — порт/адрес (если задан PORT, по умолчанию HOST=0.0.0.0)
  CACHE_DIR            — куда складывать снимки (по умолчанию ./.cache рядом со скриптом)
  FLASHSCORE_SITE      — база сайта (по умолчанию https://www.flashscorekz.com)
  FLASHSCORE_FEED      — база feed (по умолчанию https://46.flashscore.ninja/46/x/feed/)
  FLASHSCORE_FSIGN     — значение заголовка x-fsign
"""
import argparse
import gzip
import html
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "7.0"
SITE = os.environ.get("FLASHSCORE_SITE", "https://www.flashscorekz.com").rstrip("/")   # русскоязычная версия Flashscore
FEED = os.environ.get("FLASHSCORE_FEED", "https://46.flashscore.ninja/46/x/feed/")
if not FEED.endswith("/"):
    FEED += "/"
FSIGN = os.environ.get("FLASHSCORE_FSIGN", "SW9D1eZo")
LEAGUE = "/football/russia/premier-league/"
TEMPLATE_ID = "YacqHHdS"                        # «РОССИЯ: Премьер-лига»
MSK = timezone(timedelta(hours=3))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get("CACHE_DIR") or os.path.join(HERE, ".cache")
STATE_PATH = os.path.join(CACHE_DIR, "rpl-proxy-state.json")

# параметры надёжности
HTTP_TIMEOUT = 15            # секунд на одну попытку
HTTP_TRIES = 3               # попыток на один адрес
MAX_UPSTREAM = 4             # одновременных запросов к Flashscore
RETRY_CODES = {403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
SEASON_LIVE_EVERY = 60       # секунд между обновлениями, когда идёт матч или он скоро
SEASON_IDLE_EVERY = 600
SQUADS_EVERY = 6 * 3600
FULL_CRAWL_EVERY = 6 * 3600
MAX_STORED_DETAILS = 600

TEAMS = {
    "din": ("dynamo-moscow", "AFWA2jAQ"), "zen": ("zenit-st-petersburg", "vsXou9m7"),
    "spa": ("spartak-moscow", "IXVkvT2D"), "cska": ("cska-moscow", "l0r24CuE"),
    "kra": ("krasnodar", "dz6KeNj3"), "ore": ("orenburg", "Q19CngBE"),
    "bal": ("baltika", "2cJYOpxn"), "dmx": ("dynamo-makhachkala", "6T0lbAmI"),
    "ros": ("fk-rostov", "GbznhOLi"), "rub": ("rubin-kazan", "t482C620"),
    "kry": ("krylya-sovetov-samara", "SKAE94nJ"), "loko": ("lokomotiv-moscow", "Sjs63WfK"),
    "akh": ("akhmat-grozny", "bJGqkoet"), "akr": ("akron-togliatti", "6qPFRkGT"),
    "fak": ("fakel-voronezh", "KK7VWueJ"), "rod": ("rodina-moscow", "nHLtKDh0"),
}
SLUG2ID = {v[1]: k for k, v in TEAMS.items()}
POS = {"Вратари": "GK", "Защитники": "DF", "Полузащитники": "MF", "Нападающие": "FW", "Тренер": "COACH", "Тренеры": "COACH"}


def log(*a):
    sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), " ".join(str(x) for x in a)))


class UpstreamError(Exception):
    """Flashscore не ответил или вернул не то, что ожидалось."""


class NotFound(Exception):
    pass


# ------------------------------------------------------------------ http + cache
_cache = {}
_lock = threading.Lock()
_upstream = threading.BoundedSemaphore(MAX_UPSTREAM)
_up = {"ok_at": 0.0, "fail_at": 0.0, "error": None, "streak": 0}


def _retry_after(err):
    try:
        v = err.headers.get("Retry-After")
        return min(float(v), 20.0) if v else None
    except Exception:
        return None


def fetch(url, feed=False, ttl=60, must=None, tries=HTTP_TRIES):
    """GET с кэшем, повторами и проверкой содержимого.

    must — подстрока, которая обязана быть в ответе (отсекает антибот-заглушки и пустые страницы).
    Бросает UpstreamError, если после всех попыток получить ответ не удалось.
    """
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    headers = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9", "Accept-Encoding": "gzip"}
    if feed:
        headers.update({"x-fsign": FSIGN, "Referer": SITE + "/", "Origin": SITE, "Accept": "*/*"})
    last, wait = None, 0.0
    for attempt in range(tries):
        if attempt:
            time.sleep(wait if wait else min(8.0, 0.8 * 2 ** (attempt - 1)) + random.random() * 0.4)
        wait = 0.0
        try:
            with _upstream:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                    raw = r.read()
                    enc = r.headers.get("Content-Encoding")
            if enc == "gzip":
                raw = gzip.decompress(raw)
            text = raw.decode("utf-8", "replace")
            if must and must not in text:
                raise UpstreamError("неожиданный ответ (заглушка или антибот)")
            with _lock:
                _cache[url] = (time.time(), text)
                if len(_cache) > 700:      # не даём памяти расти бесконечно
                    for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:300]:
                        _cache.pop(k, None)
            _up.update(ok_at=time.time(), streak=0)
            return text
        except urllib.error.HTTPError as e:           # раньше URLError: HTTPError — его подкласс
            last = e
            if e.code not in RETRY_CODES:
                break
            wait = _retry_after(e) or 0.0
        except (urllib.error.URLError, OSError, EOFError, zlib.error, UpstreamError) as e:
            last = e
    _up.update(fail_at=time.time(), error=f"{type(last).__name__}: {last}", streak=_up["streak"] + 1)
    raise UpstreamError(f"{url} — {type(last).__name__}: {last}")


def page(path, ttl=60, must=None):
    return fetch(SITE + path, ttl=ttl, must=must)


def feed(name, ttl=30):
    return fetch(FEED + name, feed=True, ttl=ttl, tries=2)


# ------------------------------------------------------------------ feed helpers
def records(text, sep="~"):
    out = []
    for raw in text.split(sep):
        o = {}
        for kv in raw.split("¬"):
            if "÷" in kv:
                k, v = kv.split("÷", 1)
                k = k.lstrip("~")
                if k not in o:
                    o[k] = v
        out.append(o)
    return out


def initial_feeds(html_text):
    return re.findall(r'initialFeeds\["([^"]+)"\]\s*=\s*\{\s*data:\s*`([\s\S]*?)`', html_text)


def matches_from_feed(data, stage=None):
    """Разбор «~AA÷»-ленты матчей. Возвращает (stage_of_league, [матчи РПЛ])."""
    out, cur_tpl, cur_stage, league_stage = [], None, None, None
    for o in records(data):
        if "ZA" in o:
            cur_tpl, cur_stage = o.get("ZEE"), o.get("ZC")
            if cur_tpl == TEMPLATE_ID and league_stage is None:
                league_stage = cur_stage
            continue
        if "AA" not in o or cur_tpl != TEMPLATE_ID:
            continue
        if stage and cur_stage != stage:
            continue
        h, a = SLUG2ID.get(o.get("PX")), SLUG2ID.get(o.get("PY"))
        if not h or not a:
            continue
        ts = int(o.get("AD", "0") or 0)
        dt = datetime.fromtimestamp(ts, MSK)
        st = int(o.get("AB", "1") or 1)
        rnd = re.sub(r"\D", "", o.get("ER", "") or "")
        hg = o.get("AG")
        ag = o.get("AH")
        out.append({
            "id": o["AA"], "h": h, "a": a, "d": dt.strftime("%Y-%m-%d"), "t": dt.strftime("%H:%M"),
            "r": int(rnd) if rnd else None, "st": st, "live": st == 2,
            "hg": int(hg) if hg not in (None, "") and st in (2, 3) else None,
            "ag": int(ag) if ag not in (None, "") and st in (2, 3) else None,
            "stage": cur_stage,
        })
    return league_stage, out


# ------------------------------------------------------------------ season
_full = {"t": 0.0, "rows": {}}
_force_full = threading.Event()


def season():
    rows, stage, ok_pages = {}, None, 0
    for path in (LEAGUE, LEAGUE + "results/", LEAGUE + "fixtures/"):
        try:
            for _, data in initial_feeds(page(path, ttl=45, must="initialFeeds")):
                s, ms = matches_from_feed(data)
                stage = stage or s
                for m in ms:
                    rows[m["id"]] = m
            ok_pages += 1
        except Exception as e:  # noqa
            log("season page error", path, e)
    if ok_pages == 0:
        # ни одной страницы лиги — это сбой источника, а не «пустой сезон»; пусть сработает последний снимок
        raise UpstreamError("Flashscore не ответил (проверь интернет/VPN)")
    if stage:
        rows = {k: v for k, v in rows.items() if v["stage"] == stage}
    if _force_full.is_set() or time.time() - _full["t"] > FULL_CRAWL_EVERY:
        _force_full.clear()

        def team_pages(tid):
            p, s = TEAMS[tid]
            res, ok = [], True
            for kind in ("results", "fixtures"):
                try:
                    for _, data in initial_feeds(page(f"/team/{p}/{s}/{kind}/", ttl=1800, must="initialFeeds")):
                        res += matches_from_feed(data, stage)[1]
                except Exception as e:  # noqa
                    ok = False
                    log("team page error", tid, kind, e)
            return res, ok

        allrows, fails = dict(_full["rows"]), 0      # прежние матчи не теряем при частичном сбое
        with ThreadPoolExecutor(6) as ex:
            for lst, ok in ex.map(team_pages, TEAMS):
                fails += 0 if ok else 1
                for m in lst:
                    allrows[m["id"]] = m
        _full["rows"] = allrows
        # много сбоев — повторим обход через 5 минут, а не через 6 часов
        _full["t"] = time.time() if fails <= 4 else time.time() - FULL_CRAWL_EVERY + 300
    merged = dict(_full["rows"])
    for k, v in rows.items():           # свежие данные лиги поверх
        old = merged.get(k)
        if old and not v.get("r"):
            v["r"] = old.get("r")
        merged[k] = v
    if stage:
        merged = {k: v for k, v in merged.items() if v["stage"] == stage}
    if not merged:
        raise UpstreamError("Flashscore не вернул ни одного матча РПЛ")
    return {"stage": stage, "count": len(merged), "matches": sorted(merged.values(), key=lambda m: (m["d"], m["t"]))}


# ------------------------------------------------------------------ squads
class SquadParser(HTMLParser):
    CELLS = ("jersey", "age", "matchesPlayed", "minutesPlayed", "goal", "assist", "yellowCard", "redCard")

    def __init__(self):
        super().__init__()
        self.tables = {}        # table_id -> list[row]
        self.stack = []         # (tag, marker)
        self.table = None
        self.pos = None
        self.row = None
        self.capture = None
        self.buf = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "") or ""
        marker = None
        tid = a.get("id", "") or ""
        if tag == "div" and tid.endswith("-table"):
            self.table = tid
            self.tables.setdefault(tid, [])
            marker = "table"
        elif "lineupTable__title" in cls:
            marker, self.capture, self.buf = "title", "title", ""
        elif "lineupTable__row" in cls and self.table:
            self.row = {"pos": self.pos}
            marker = "row"
        elif self.row is not None:
            if "lineupTable__cell--name" in cls:
                self.row["href"] = a.get("href")
                marker, self.capture, self.buf = "cell", "name", ""
            elif "flag" in cls and a.get("title"):
                self.row["nat"] = a.get("title")
            else:
                for c in self.CELLS:
                    if "lineupTable__cell--" + c in cls.split():
                        marker, self.capture, self.buf = "cell", c, ""
                        break
        if tag not in ("img", "br", "input", "link", "meta"):
            self.stack.append((tag, marker))

    def handle_endtag(self, tag):
        while self.stack:
            t, marker = self.stack.pop()
            if marker == "title":
                self.pos = POS.get(self.buf.strip(), self.buf.strip())
                self.capture = None
            elif marker == "cell":
                if self.row is not None and self.capture:
                    self.row[self.capture] = self.buf.strip()
                self.capture = None
            elif marker == "row":
                if self.row and self.row.get("href"):
                    self.tables[self.table].append(self.row)
                self.row = None
            elif marker == "table":
                self.table = None
            if t == tag:
                break

    def handle_data(self, data):
        if self.capture:
            self.buf += data


def _int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


_squad_prev = {}      # tid -> (players, coach): последний удачный состав клуба


def squads():
    league_tid = None
    try:
        for _, data in initial_feeds(page(LEAGUE + "results/", ttl=600, must="initialFeeds")):
            m = re.search(r"ZEE÷" + TEMPLATE_ID + r"¬[^~]*?ZE÷([^¬]+)", data)
            if m:
                league_tid = m.group(1)
                break
    except Exception:
        pass

    def one(tid):
        p, s = TEAMS[tid]
        try:
            parser = SquadParser()
            parser.feed(page(f"/team/{p}/{s}/squad/", ttl=1800, must="lineupTable"))
            tabs = parser.tables
            overall = tabs.get("overall-all-table", [])
            if not overall:
                raise UpstreamError("пустой состав")
            lg_key = f"league-{league_tid}-table" if league_tid else next((k for k in tabs if k.startswith("league-")), None)
            league = {r["href"]: r for r in tabs.get(lg_key, [])} if lg_key else {}
            seen, players, coach = set(), [], None
            stat = lambda r: [_int(r.get(k)) for k in ("matchesPlayed", "minutesPlayed", "goal", "assist", "yellowCard", "redCard")]
            for r in overall:
                href = r["href"]
                if href in seen:
                    continue
                seen.add(href)
                pid = href.replace("/player/", "").strip("/")
                name = html.unescape(r.get("name", ""))
                if r.get("pos") == "COACH":
                    coach = {"t": tid, "id": pid, "name": name}
                    continue
                lg = league.get(href)
                players.append({"id": pid, "n": name, "t": tid, "p": r.get("pos"), "no": _int(r.get("jersey")),
                                "nat": r.get("nat"), "age": _int(r.get("age")),
                                "lg": stat(lg) if lg else None, "tot": stat(r)})
            _squad_prev[tid] = (players, coach)
            return players, coach, True
        except Exception as e:  # noqa
            log("squad error", tid, e)
            prev = _squad_prev.get(tid)          # клуб не ответил — оставляем прошлый состав
            return (prev[0], prev[1], False) if prev else ([], None, False)

    allp, coaches, fails = [], [], 0
    with ThreadPoolExecutor(6) as ex:
        for pl, c, ok in ex.map(one, TEAMS):
            fails += 0 if ok else 1
            allp += pl
            if c:
                coaches.append(c)
    if fails == len(TEAMS) or not allp:
        raise UpstreamError("Flashscore не вернул составы")
    return {"players": allp, "coaches": coaches, "count": len(allp)}


# ------------------------------------------------------------------ player
def player(pid):
    h = page(f"/player/{pid}/", ttl=6 * 3600, must="playerHeader")
    i = h.find("playerHeader__wrapper")
    j = h.find('id="player-profile"', i)
    seg = h[i:j if j > 0 else i + 8000]
    img = re.search(r'<img[^>]+src="([^"]+)"', seg)
    name = re.search(r"<h2[^>]*>([^<]+)</h2>", seg)
    info = {}
    for it in re.findall(r'class="playerInfoItem">(.*?)</div>', seg, re.S):
        spans = [html.unescape(re.sub(r"<[^>]+>", "", x)).strip() for x in re.findall(r"<span[^>]*>(.*?)</span>", it, re.S)]
        if spans:
            info[spans[0].rstrip(":")] = " ".join(spans[1:]).strip()
    out = {"id": pid, "n": html.unescape(name.group(1)).strip() if name else None}
    if img and "empty-face" not in img.group(1):
        out["img"] = img.group(1).replace("https://static.flashscore.com/res/image/data/", "")
    age = info.get("Возр") or info.get("Возраст") or ""
    m = re.match(r"(\d+)\s*\((\d\d\.\d\d\.\d{4})\)", age)
    if m:
        out["age"], out["dob"] = int(m.group(1)), m.group(2)
    if info.get("Рыночная цена"):
        out["val"] = info["Рыночная цена"]
    if info.get("Конец контракта"):
        out["ct"] = info["Конец контракта"]
    return out


# ------------------------------------------------------------------ match
def _pid(u):
    return (u or "").replace("/player/", "").strip("/")


def parse_incidents(t):
    ev, meta = [], {}
    for raw in t.split("~"):
        if raw.startswith("MIT÷"):
            for k, v in re.findall(r"MIT÷([^¬]*)¬MIV÷([^¬]*)", raw):
                meta[k] = v
            continue
        if not raw.startswith("III÷"):
            continue
        side = re.search(r"IA÷(\d)", raw)
        minute = re.search(r"IB÷([^¬]*)", raw)
        parts = []
        for p in raw.split("¬IE÷")[1:]:
            code = _int(p.split("¬")[0])
            g = lambda k: (re.search(r"(?:^|¬)" + k + r"÷([^¬]*)", p) or [None, None])[1]
            if code == 47:
                continue
            parts.append([code, g("IF"), _pid(g("IU"))])
        ev.append([int(side.group(1)) if side else 0, minute.group(1) if minute else "", parts])
    return ev, meta


def parse_stats(t):
    groups, st, se, sf = [], [], None, None
    for o in records(t):
        if "SE" in o:
            se = o["SE"]
        if "SF" in o:
            sf = o["SF"]
        if "SG" in o and se == "Матч":
            if sf not in groups:
                groups.append(sf)
            st.append([groups.index(sf), o["SG"], o.get("SH"), o.get("SI")])
    return groups, st


def parse_lineups(t):
    L = {"1": {"xi": [], "subs": [], "coach": None}, "2": {"xi": [], "subs": [], "coach": None}}
    sec = side = None
    LS = {"Капитан": "C", "Вратарь": "G"}
    for o in records(t):
        if "LB" in o:
            sec = o["LB"]
        if "LC" in o:
            side = o["LC"]
        if side and "LD" in o:
            L[side]["f"] = o["LD"]
        if "LRH" in o:
            L["1"]["r"] = o["LRH"]
        if "LRA" in o:
            L["2"]["r"] = o["LRA"]
        if "LP" not in o or not side:
            continue
        rt = o.get("LPR")
        P = [_pid(o.get("NU")), o.get("LI"), _int(o.get("LJ")), float(rt) if rt else None, _int(o.get("LL")), LS.get(o.get("LS"))]
        if sec and "Тренер" in sec:
            L[side]["coach"] = [P[0], P[1]]
        elif sec and "Стартов" in sec:
            L[side]["xi"].append(P)
        elif sec and "Замен" in sec:
            L[side]["subs"].append(P)
    return L


_details = {}         # id -> протокол завершённого матча (не меняется, хранится навсегда)


def match(mid):
    if not re.fullmatch(r"[A-Za-z0-9]{6,12}", mid):
        raise ValueError("bad id")
    if mid in _details:
        return _details[mid]
    known = {m["id"]: m for m in (SEASON.value or {}).get("matches", [])}
    if known and mid not in known:                 # не гоняем Flashscore по произвольным id
        raise NotFound("unknown match")
    with ThreadPoolExecutor(3) as ex:
        sui, st, li = ex.map(lambda f: _safe_feed(f + mid), ("df_sui_1_", "df_st_1_", "df_li_1_"))
    if sui is None and st is None and li is None:
        raise UpstreamError("Flashscore не ответил (проверь интернет/VPN)")
    ev, meta = parse_incidents(sui or "")
    groups, stats = parse_stats(st or "")
    out = {"ev": ev, "g": groups, "st": stats, "li": parse_lineups(li) if li else None,
           "ref": meta.get("REF"), "ven": meta.get("VEN"), "att": meta.get("ATT")}
    finished = known.get(mid, {}).get("st") == 3
    # навсегда кэшируем только полный ответ по сыгранному матчу: частичный (одна лента не ответила) не запоминаем
    if finished and None not in (sui, st, li) and (ev or stats):
        _details[mid] = out
        if len(_details) > MAX_STORED_DETAILS:
            for k in list(_details)[: len(_details) - MAX_STORED_DETAILS]:
                _details.pop(k, None)
        save_state()
    return out


def _safe_feed(name):
    try:
        return feed(name, ttl=30)
    except Exception as e:  # noqa
        log("feed error", name, e)
        return None


# ------------------------------------------------------------------ снимки + фон
class Snapshot:
    """Последний удачный результат тяжёлой операции + фоновое обновление.

    get() никогда не ходит во Flashscore, если снимок уже есть: отдаёт его сразу (при необходимости
    запуская обновление в фоне). Если обновление падает, снимок остаётся и помечается stale.
    """

    def __init__(self, name, builder, fresh):
        self.name, self.builder, self.fresh = name, builder, fresh
        self.value, self.at = None, 0.0
        self.error, self.fails, self.next_try = None, 0, 0.0
        self.lock = threading.Lock()

    def load(self, value, at):
        self.value, self.at = value, at

    def due(self, every):
        now = time.time()
        return now >= self.next_try and now - self.at >= every

    def refresh(self, wait=True, force=False):
        if not self.lock.acquire(blocking=wait):
            return False                        # обновление уже идёт
        try:
            if not force and self.value is not None and time.time() - self.at < self.fresh:
                return True                     # пока ждали замок, снимок уже обновили
            self.value, self.at, self.error, self.fails = self.builder(), time.time(), None, 0
            save_state()
            return True
        except Exception as e:  # noqa
            self.error = str(e)
            self.fails += 1
            self.next_try = time.time() + min(600, 20 * 2 ** min(self.fails, 5))
            log(f"{self.name}: обновление не удалось ({self.fails}):", e)
            return False
        finally:
            self.lock.release()

    def get(self):
        if self.value is None:
            if self.error and time.time() < self.next_try and not self.lock.locked():
                raise UpstreamError(self.error)      # недавно уже не получилось — не держим пользователя
            self.refresh(wait=True)           # параллельные «холодные» запросы ждут один общий сбор
            if self.value is None:
                raise UpstreamError(self.error or "данных пока нет")
        elif time.time() - self.at > self.fresh and time.time() >= self.next_try and not self.lock.locked():
            threading.Thread(target=self.refresh, kwargs={"wait": False}, daemon=True).start()
        return self.value, self.meta()

    def meta(self):
        age = int(time.time() - self.at) if self.value is not None else None
        stale = self.value is not None and (self.error is not None or time.time() - self.at > self.fresh * 3)
        return {"fetchedAt": datetime.fromtimestamp(self.at, timezone.utc).isoformat(timespec="seconds") if self.at else None,
                "ageSec": age, "stale": bool(stale), "error": self.error if stale else None}


SEASON = Snapshot("season", season, fresh=SEASON_LIVE_EVERY * 2)
SQUADS = Snapshot("squads", squads, fresh=SQUADS_EVERY + 3600)

_save_lock = threading.Lock()


def save_state():
    try:
        with _save_lock:
            os.makedirs(CACHE_DIR, exist_ok=True)
            data = {"v": 1,
                    "season": {"at": SEASON.at, "value": SEASON.value}, "squads": {"at": SQUADS.at, "value": SQUADS.value},
                    "full": {"t": _full["t"], "rows": _full["rows"]}, "details": _details}
            tmp = f"{STATE_PATH}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, STATE_PATH)
    except Exception as e:  # noqa
        log("не удалось сохранить снимок:", e)


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("season", {}).get("value"):
            SEASON.load(d["season"]["value"], float(d["season"]["at"]))
        if d.get("squads", {}).get("value"):
            SQUADS.load(d["squads"]["value"], float(d["squads"]["at"]))
        _full.update(t=float(d.get("full", {}).get("t", 0)), rows=d.get("full", {}).get("rows", {}))
        _details.update(d.get("details", {}))
        log(f"снимок с диска загружен: матчей {len((SEASON.value or {}).get('matches', []))}, "
            f"игроков {len((SQUADS.value or {}).get('players', []))}, протоколов {len(_details)}")
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa
        log("снимок на диске повреждён, начинаю с нуля:", e)


def _live_soon(matches):
    now = datetime.now(MSK)
    for m in matches:
        if m.get("live"):
            return True
        if m.get("st") == 1:
            try:
                dt = datetime.strptime(f"{m['d']} {m['t']}", "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
            except Exception:
                continue
            if abs((dt - now).total_seconds()) < 3 * 3600:
                return True
    return False


def refresher(stop):
    """Фоновое обновление: пользователь не ждёт Flashscore."""
    while not stop.is_set():
        try:
            matches = (SEASON.value or {}).get("matches", [])
            every = SEASON_LIVE_EVERY if _live_soon(matches) else SEASON_IDLE_EVERY
            if SEASON.value is None or SEASON.due(every):
                SEASON.refresh(wait=True, force=True)
            if SQUADS.value is None or SQUADS.due(SQUADS_EVERY):
                SQUADS.refresh(wait=True, force=True)
        except Exception as e:  # noqa
            log("фоновое обновление:", e)
        stop.wait(15)


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    server_version = "RPLArenaProxy/" + VERSION

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        gz = len(data) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            data = gzip.compress(data, 5)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Accept-Encoding")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def _snapshot(self, snap, **extra):
        value, meta = snap.get()
        return self._send(200, dict(value, meta=meta, **extra))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"
        try:
            if path == "/health":
                return self._send(200, {
                    "ok": True, "version": VERSION, "source": SITE,
                    "ready": SEASON.value is not None,
                    "season": SEASON.meta(), "squads": SQUADS.meta(),
                    "upstream": {"lastOkAgoSec": int(time.time() - _up["ok_at"]) if _up["ok_at"] else None,
                                 "failStreak": _up["streak"], "lastError": _up["error"]},
                    "endpoints": ["/api/season", "/api/squads", "/api/match/<id>", "/api/player/<slug>/<id>"]})
            if path == "/api/season":
                if q.get("full", ["0"])[0] == "1":
                    _force_full.set()
                    SEASON.refresh(wait=True, force=True)
                return self._snapshot(SEASON)
            if path == "/api/squads":
                return self._snapshot(SQUADS)
            m = re.fullmatch(r"/api/match/([A-Za-z0-9]+)", path)
            if m:
                return self._send(200, match(m.group(1)))
            m = re.fullmatch(r"/api/player/([\w-]+/[A-Za-z0-9]+)", path)
            if m:
                known = {p["id"] for p in (SQUADS.value or {}).get("players", [])}
                if known and m.group(1) not in known:
                    raise NotFound("unknown player")
                return self._send(200, player(m.group(1)))
            if path in ("/", "/index.html", "/rpl_arena_v6.html"):
                for name in ("rpl_arena_v6.html", "index.html"):
                    f = os.path.join(HERE, name)
                    if os.path.exists(f):
                        with open(f, "rb") as fh:
                            return self._send(200, fh.read(), "text/html; charset=utf-8")
                return self._send(200, b"<h1>RPL Arena</h1><p>Put rpl_arena_v6.html next to this script.</p>", "text/html; charset=utf-8")
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except NotFound as e:
            return self._send(404, {"error": str(e)})
        except Exception as e:  # noqa
            return self._send(502, {"error": str(e)})

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))


def make_server(host, port, background=True):
    load_state()
    stop = threading.Event()
    srv = ThreadingHTTPServer((host, port), Handler)
    if background:
        threading.Thread(target=refresher, args=(stop,), daemon=True, name="refresher").start()
    srv.stop_event = stop
    return srv


def main():
    ap = argparse.ArgumentParser(description="РПЛ Arena — сервер данных (прокси к Flashscore)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8765))
    ap.add_argument("--host", default=os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"))
    args = ap.parse_args()
    srv = make_server(args.host, args.port)
    print(f"РПЛ Arena запущен: http://localhost:{args.port}/   (Ctrl+C — остановить)")
    print("Проверка: http://localhost:%d/health" % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop_event.set()
        save_state()


if __name__ == "__main__":
    main()
