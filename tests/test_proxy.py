"""Тесты надёжности сервера данных на локальной имитации Flashscore.

Запуск:  python -m unittest discover -s tests -p "test_proxy.py" -v
Сеть не нужна: «Flashscore» поднимается на 127.0.0.1 и отдаёт реальные образцы лент
из rpl_feed_samples.json, а сбои (503, заглушка вместо страницы, полный блэкаут) внедряются по команде.
"""
import gzip
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import rpl_flashscore_proxy as px  # noqa: E402

SAMPLES = json.load(open(os.path.join(ROOT, "rpl_feed_samples.json"), encoding="utf-8"))
FINISHED = "QJOZBzCm"      # сыгранный матч из образца (st=3)
LEAGUE_STUB = "UNKNOWNID1"


def feeds_page(name, raw):
    return f'<html><script>{raw}\n}};</script></html>'


def squad_html(slug):
    def row(jersey, name, href, age):
        return ('<div class="lineupTable__row"><div class="lineupTable__cell lineupTable__cell--jersey">%s</div>'
                '<div class="lineupTable__cell lineupTable__cell--player"><div class="lineupTable__cell--flag flag fl_1" title="Россия"></div>'
                '<div class="lineupTable__cell--nameAndAbsence"><a class="lineupTable__cell--name" href="%s">%s</a></div></div>'
                '<div class="lineupTable__cell lineupTable__cell--age">%s</div>'
                '<div class="lineupTable__cell lineupTable__cell--matchesPlayed">5</div></div>') % (jersey, href, name, age)
    return ('<html><div id="overall-all-table"><div class="lineupTable__title">Вратари</div>'
            + row(1, "Вратарь " + slug, f"/player/gk-{slug}/AAAA{len(slug):04d}/", 30)
            + '<div class="lineupTable__title">Нападающие</div>' + row(9, "Форвард " + slug, f"/player/fw-{slug}/BBBB{len(slug):04d}/", 25)
            + '<div class="lineupTable__title">Тренеры</div>' + row("", "Тренер " + slug, f"/player/coach-{slug}/CCCC{len(slug):04d}/", 50)
            + '</div></html>')


class Fake:
    """Имитация Flashscore с управляемыми сбоями."""

    def __init__(self):
        self.hits = Counter()
        self.fail_n = {}          # префикс пути -> сколько первых запросов отдать 503
        self.fail_always = set()  # префиксы, которые всегда 500
        self.blackout = False
        self.junk = False         # 200, но вместо страницы заглушка
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                p = self.path
                fake.hits[p] += 1
                if fake.blackout:
                    return self._r(503, "down")
                for pre, n in list(fake.fail_n.items()):
                    if p.startswith(pre) and n > 0:
                        fake.fail_n[pre] = n - 1
                        return self._r(503, "temporary")
                if any(p.startswith(pre) for pre in fake.fail_always):
                    return self._r(500, "broken")
                if fake.junk:
                    return self._r(200, "<html>Just a moment... verifying you are human</html>")
                if p.startswith("/feed/"):
                    if self.headers.get("x-fsign") != px.FSIGN:
                        return self._r(403, "no sign")
                    name = p[len("/feed/"):]
                    for k in ("df_sui_1_", "df_st_1_", "df_li_1_"):
                        if name.startswith(k):
                            return self._r(200, SAMPLES[k])
                    return self._r(404, "nf")
                if p.startswith("/football/russia/premier-league/fixtures/") or p.endswith("/fixtures/"):
                    return self._r(200, feeds_page("fixtures", SAMPLES["teamFeeds"][3]))
                if p.startswith("/football/russia/premier-league/results/") or p.endswith("/results/"):
                    return self._r(200, feeds_page("results", SAMPLES["teamFeeds"][2]))
                if p == "/football/russia/premier-league/":
                    return self._r(200, feeds_page("summary", SAMPLES["teamFeeds"][0]))
                if p.endswith("/squad/"):
                    return self._r(200, squad_html(p.split("/")[2]))
                return self._r(404, "nf")

            def _r(self, code, body):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def count(self, prefix):
        return sum(n for p, n in self.hits.items() if p.startswith(prefix))

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def reset_proxy():
    px._cache.clear()
    px._full.update(t=0.0, rows={})
    px._details.clear()
    px._squad_prev.clear()
    px._force_full.clear()
    px._up.update(ok_at=0.0, fail_at=0.0, error=None, streak=0)
    for s in (px.SEASON, px.SQUADS):
        s.value, s.at, s.error, s.fails, s.next_try = None, 0.0, None, 0, 0.0


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(px, "SITE", f"http://127.0.0.1:{self.fake.port}"),
            mock.patch.object(px, "FEED", f"http://127.0.0.1:{self.fake.port}/feed/"),
            mock.patch.object(px, "STATE_PATH", os.path.join(self.tmp.name, "state.json")),
            mock.patch.object(px, "CACHE_DIR", self.tmp.name),
            mock.patch.object(px.time, "sleep", lambda s: None),   # не ждём паузы между повторами
            mock.patch.object(px, "log", lambda *a: None),
        ]
        for p in self.patches:
            p.start()
        reset_proxy()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.fake.close()
        self.tmp.cleanup()
        reset_proxy()

    # ---- повторы ----
    def test_retries_recover_from_transient_503(self):
        self.fake.fail_n["/football/russia/premier-league/"] = 2
        text = px.page(px.LEAGUE, must="initialFeeds")
        self.assertIn("initialFeeds", text)
        self.assertEqual(self.fake.count("/football/russia/premier-league/"), 3)

    def test_gives_up_after_all_tries(self):
        self.fake.blackout = True
        with self.assertRaises(px.UpstreamError):
            px.page(px.LEAGUE, must="initialFeeds")
        self.assertEqual(self.fake.count("/football"), px.HTTP_TRIES)
        self.assertGreaterEqual(px._up["streak"], 1)

    def test_404_is_not_retried(self):
        with self.assertRaises(px.UpstreamError):
            px.page("/nope/")
        self.assertEqual(self.fake.count("/nope/"), 1)

    def test_challenge_page_is_a_failure_not_empty_data(self):
        self.fake.junk = True
        with self.assertRaises(px.UpstreamError):
            px.season()

    # ---- сезон ----
    def test_season_builds(self):
        s = px.season()
        self.assertGreater(s["count"], 20)
        self.assertIn(FINISHED, {m["id"] for m in s["matches"]})

    def test_season_keeps_matches_when_team_pages_fail(self):
        first = px.season()
        px._cache.clear()
        px._full["t"] = 0.0                                  # пора заново обходить страницы клубов
        self.fake.fail_always.add("/team/")
        second = px.season()
        self.assertEqual(second["count"], first["count"])
        self.assertLess(px._full["t"], __import__("time").time() - px.FULL_CRAWL_EVERY + 400)   # повтор скоро

    def test_stale_snapshot_served_during_blackout(self):
        self.assertTrue(px.SEASON.refresh())
        count = px.SEASON.value["count"]
        self.fake.blackout = True
        px._cache.clear()
        self.assertFalse(px.SEASON.refresh(force=True))
        value, meta = px.SEASON.get()
        self.assertEqual(value["count"], count)
        self.assertTrue(meta["stale"])
        self.assertTrue(meta["error"])

    def test_snapshot_recovers_after_blackout(self):
        px.SEASON.refresh()
        self.fake.blackout = True
        px._cache.clear()
        px.SEASON.refresh(force=True)
        self.fake.blackout = False
        px.SEASON.next_try = 0.0
        self.assertTrue(px.SEASON.refresh(force=True))
        self.assertFalse(px.SEASON.meta()["stale"])

    # ---- составы ----
    def test_squads_survive_one_club_failing(self):
        full = px.squads()
        self.assertEqual(len(full["coaches"]), 16)
        px._cache.clear()
        self.fake.fail_always.add("/team/zenit-st-petersburg/")
        partial = px.squads()
        self.assertEqual(partial["count"], full["count"])    # состав «Зенита» взят из прошлого удачного

    def test_squads_first_run_with_one_club_down_still_returns_others(self):
        self.fake.fail_always.add("/team/zenit-st-petersburg/")
        r = px.squads()
        self.assertEqual(r["count"], 15 * 2)

    def test_squads_all_down_raises(self):
        self.fake.blackout = True
        with self.assertRaises(px.UpstreamError):
            px.squads()

    # ---- матч ----
    def test_finished_match_cached_forever(self):
        px.SEASON.refresh()
        d = px.match(FINISHED)
        self.assertTrue(d["ev"] and d["st"] and d["li"])
        hits = self.fake.count("/feed/")
        self.assertEqual(hits, 3)
        px._cache.clear()
        self.assertEqual(px.match(FINISHED), d)
        self.assertEqual(self.fake.count("/feed/"), hits)    # второй раз во Flashscore не ходили

    def test_partial_match_not_cached_forever(self):
        px.SEASON.refresh()
        self.fake.fail_always.add("/feed/df_li_1_")
        d = px.match(FINISHED)
        self.assertTrue(d["ev"])
        self.assertNotIn(FINISHED, px._details)

    def test_unknown_and_bad_match_ids_do_not_reach_upstream(self):
        px.SEASON.refresh()
        before = self.fake.count("/feed/")
        with self.assertRaises(px.NotFound):
            px.match("ZZZZZZZZ")
        with self.assertRaises(ValueError):
            px.match("../x")
        self.assertEqual(self.fake.count("/feed/"), before)

    # ---- диск ----
    def test_state_survives_restart(self):
        px.SEASON.refresh()
        px.SQUADS.refresh()
        count = px.SEASON.value["count"]
        px.match(FINISHED)
        px.save_state()
        reset_proxy()
        self.assertIsNone(px.SEASON.value)
        px.load_state()
        self.assertEqual(px.SEASON.value["count"], count)
        self.assertIsNotNone(px.SQUADS.value)
        self.assertIn(FINISHED, px._details)

    def test_corrupt_state_file_is_ignored(self):
        with open(px.STATE_PATH, "w") as f:
            f.write("{not json")
        px.load_state()                                       # не падает
        self.assertIsNone(px.SEASON.value)


class HttpTest(unittest.TestCase):
    """Сквозная проверка настоящего HTTP-сервера."""

    def setUp(self):
        self.fake = Fake()
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(px, "SITE", f"http://127.0.0.1:{self.fake.port}"),
            mock.patch.object(px, "FEED", f"http://127.0.0.1:{self.fake.port}/feed/"),
            mock.patch.object(px, "STATE_PATH", os.path.join(self.tmp.name, "state.json")),
            mock.patch.object(px, "CACHE_DIR", self.tmp.name),
            mock.patch.object(px.time, "sleep", lambda s: None),
            mock.patch.object(px, "log", lambda *a: None),
        ]
        for p in self.patches:
            p.start()
        reset_proxy()
        self.srv = px.make_server("127.0.0.1", 0, background=False)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for p in self.patches:
            p.stop()
        self.fake.close()
        self.tmp.cleanup()
        reset_proxy()

    def get(self, path, gz=False):
        req = urllib.request.Request(self.base + path, headers={"Accept-Encoding": "gzip"} if gz else {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return r.status, raw, r.headers
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read(), e.headers
            finally:
                e.close()

    def test_health_ok_even_before_any_data(self):
        status, body, _ = self.get("/health")
        j = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(j["ok"])
        self.assertFalse(j["ready"])

    def test_cold_start_season_then_served_from_snapshot(self):
        status, body, _ = self.get("/api/season")
        j = json.loads(body)
        self.assertEqual(status, 200)
        self.assertGreater(j["count"], 20)
        self.assertFalse(j["meta"]["stale"])
        league_hits = self.fake.count("/football/russia/premier-league/")
        for _ in range(5):
            self.assertEqual(self.get("/api/season")[0], 200)
        self.assertEqual(self.fake.count("/football/russia/premier-league/"), league_hits)  # пользователи не бьют по Flashscore

    def test_blackout_with_snapshot_still_answers_200_stale(self):
        self.get("/api/season")
        self.fake.blackout = True
        px._cache.clear()
        px.SEASON.refresh(force=True)
        status, body, _ = self.get("/api/season")
        j = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(j["meta"]["stale"])
        self.assertEqual(self.get("/health")[0], 200)

    def test_cold_start_blackout_returns_502_fast_then_recovers(self):
        self.fake.blackout = True
        self.assertEqual(self.get("/api/season")[0], 502)
        hits = self.fake.count("/football")
        self.assertEqual(self.get("/api/season")[0], 502)
        self.assertEqual(self.fake.count("/football"), hits)  # повторный запрос не стучится в упавший источник
        self.fake.blackout = False
        px.SEASON.next_try = 0.0
        px._cache.clear()
        self.assertEqual(self.get("/api/season")[0], 200)

    def test_match_endpoints(self):
        self.get("/api/season")
        status, body, _ = self.get("/api/match/" + FINISHED)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ev"])
        self.assertEqual(self.get("/api/match/ZZZZZZZZ")[0], 404)
        self.assertEqual(self.get("/api/match/ab")[0], 400)    # id неправильного формата

    def test_squads_and_gzip(self):
        status, raw, headers = self.get("/api/squads", gz=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(json.loads(raw)["count"], 32)

    def test_serves_the_app_page(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower()[:2000])


if __name__ == "__main__":
    unittest.main()
