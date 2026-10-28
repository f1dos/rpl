#!/usr/bin/env python3
"""
РПЛ Arena — локальный прокси к Flashscore
=========================================

Запуск:   python rpl_flashscore_proxy.py            (порт 8765)
          python rpl_flashscore_proxy.py --port 9000

Нужен только Python 3.9+ (без сторонних библиотек).
Положи этот файл рядом с rpl_arena_v6.html и открой http://localhost:8765/ —
приложение подключится автоматически. Можно открыть и сам HTML-файл двойным кликом:
он найдёт прокси по адресу http://localhost:8765.

Эндпоинты (все отдают JSON, CORS открыт):
  GET /health                       — проверка
  GET /api/season[?full=1]          — календарь и результаты сезона (live-счёт тоже)
  GET /api/squads                   — составы 16 клубов + статистика игроков (РПЛ и все турниры)
  GET /api/match/<id>               — события, статистика, составы и рейтинги матча
  GET /api/player/<slug>/<id>       — фото, полное имя, возраст, стоимость, контракт игрока

Это неофициальный доступ к публичным страницам Flashscore: при изменениях на их
стороне разбор может сломаться. Не делай слишком частых запросов — в прокси есть кэш.
"""
import argparse
import gzip
import html
import json
import os
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "6.0"
SITE = "https://www.flashscorekz.com"          # русскоязычная версия Flashscore
FEED = "https://46.flashscore.ninja/46/x/feed/"
FSIGN = "SW9D1eZo"
LEAGUE = "/football/russia/premier-league/"
TEMPLATE_ID = "YacqHHdS"                        # «РОССИЯ: Премьер-лига»
MSK = timezone(timedelta(hours=3))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"

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

# ------------------------------------------------------------------ http + cache
_cache = {}
_lock = threading.Lock()


def fetch(url, feed=False, ttl=60):
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    headers = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9", "Accept-Encoding": "gzip"}
    if feed:
        headers.update({"x-fsign": FSIGN, "Referer": SITE + "/", "Origin": SITE, "Accept": "*/*"})
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    text = raw.decode("utf-8", "replace")
    with _lock:
        _cache[url] = (now, text)
    return text


def page(path, ttl=60):
    return fetch(SITE + path, ttl=ttl)


def feed(name, ttl=30):
    return fetch(FEED + name, feed=True, ttl=ttl)


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
_full = {"t": 0, "rows": {}}


def season(full=False):
    rows, stage = {}, None
    for path in (LEAGUE, LEAGUE + "results/", LEAGUE + "fixtures/"):
        try:
            for _, data in initial_feeds(page(path, ttl=45)):
                s, ms = matches_from_feed(data)
                stage = stage or s
                for m in ms:
                    rows[m["id"]] = m
        except Exception as e:  # noqa
            print("season page error", path, e, file=sys.stderr)
    if stage:
        rows = {k: v for k, v in rows.items() if v["stage"] == stage}
    if full or time.time() - _full["t"] > 6 * 3600:
        def team_pages(tid):
            p, s = TEAMS[tid]
            res = []
            for kind in ("results", "fixtures"):
                try:
                    for _, data in initial_feeds(page(f"/team/{p}/{s}/{kind}/", ttl=1800)):
                        res += matches_from_feed(data, stage)[1]
                except Exception as e:  # noqa
                    print("team page error", tid, kind, e, file=sys.stderr)
            return res
        with ThreadPoolExecutor(6) as ex:
            allrows = {}
            for lst in ex.map(team_pages, TEAMS):
                for m in lst:
                    allrows[m["id"]] = m
        _full.update(t=time.time(), rows=allrows)
    merged = dict(_full["rows"])
    for k, v in rows.items():           # свежие данные лиги поверх
        old = merged.get(k)
        if old and not v.get("r"):
            v["r"] = old.get("r")
        merged[k] = v
    if not merged:
        raise RuntimeError("Flashscore не ответил (проверь интернет/VPN)")
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


def squads():
    league_tid = None
    try:
        for _, data in initial_feeds(page(LEAGUE + "results/", ttl=600)):
            m = re.search(r"ZEE÷" + TEMPLATE_ID + r"¬[^~]*?ZE÷([^¬]+)", data)
            if m:
                league_tid = m.group(1)
                break
    except Exception:
        pass

    def one(tid):
        p, s = TEAMS[tid]
        parser = SquadParser()
        parser.feed(page(f"/team/{p}/{s}/squad/", ttl=1800))
        tabs = parser.tables
        overall = tabs.get("overall-all-table", [])
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
        return players, coach

    allp, coaches = [], []
    with ThreadPoolExecutor(6) as ex:
        for pl, c in ex.map(one, TEAMS):
            allp += pl
            if c:
                coaches.append(c)
    return {"players": allp, "coaches": coaches, "count": len(allp)}


# ------------------------------------------------------------------ player
def player(pid):
    h = page(f"/player/{pid}/", ttl=6 * 3600)
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


def match(mid):
    if not re.fullmatch(r"[A-Za-z0-9]{6,12}", mid):
        raise ValueError("bad id")
    with ThreadPoolExecutor(3) as ex:
        sui, st, li = ex.map(lambda f: _safe_feed(f + mid), ("df_sui_1_", "df_st_1_", "df_li_1_"))
    if not (sui or st or li):
        raise RuntimeError("Flashscore не ответил (проверь интернет/VPN)")
    ev, meta = parse_incidents(sui or "")
    groups, stats = parse_stats(st or "")
    out = {"ev": ev, "g": groups, "st": stats, "li": parse_lineups(li) if li else None,
           "ref": meta.get("REF"), "ven": meta.get("VEN"), "att": meta.get("ATT")}
    return out


def _safe_feed(name):
    try:
        return feed(name, ttl=30)
    except Exception as e:  # noqa
        print("feed error", name, e, file=sys.stderr)
        return None


# ------------------------------------------------------------------ server
HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(BaseHTTPRequestHandler):
    server_version = "RPLArenaProxy/" + VERSION

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
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

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"
        try:
            if path == "/health":
                return self._send(200, {"ok": True, "version": VERSION, "source": SITE,
                                        "endpoints": ["/api/season", "/api/squads", "/api/match/<id>", "/api/player/<slug>/<id>"]})
            if path == "/api/season":
                return self._send(200, season(full=q.get("full", ["0"])[0] == "1"))
            if path == "/api/squads":
                return self._send(200, squads())
            m = re.fullmatch(r"/api/match/([A-Za-z0-9]+)", path)
            if m:
                return self._send(200, match(m.group(1)))
            m = re.fullmatch(r"/api/player/([\w-]+/[A-Za-z0-9]+)", path)
            if m:
                return self._send(200, player(m.group(1)))
            if path in ("/", "/index.html"):
                for name in ("rpl_arena_v6.html", "index.html"):
                    f = os.path.join(HERE, name)
                    if os.path.exists(f):
                        with open(f, "rb") as fh:
                            return self._send(200, fh.read(), "text/html; charset=utf-8")
                return self._send(200, b"<h1>RPL Arena proxy</h1><p>Put rpl_arena_v6.html next to this script.</p>", "text/html; charset=utf-8")
            return self._send(404, {"error": "not found"})
        except Exception as e:  # noqa
            return self._send(502, {"error": str(e)})

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))


def main():
    ap = argparse.ArgumentParser(description="РПЛ Arena — прокси к Flashscore")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"РПЛ Arena прокси запущен: http://localhost:{args.port}/   (Ctrl+C — остановить)")
    print("Проверка: http://localhost:%d/health" % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
