"""Bazarr link: wait for its first subtitle searches before converting, record its matches, rescan afterwards."""
import logging
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import media_converterr as mc

LOG = logging.getLogger("t")


class BazarrTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.conn = sqlite3.connect(f"{d.name}/db.sqlite")
        self.conn.row_factory = sqlite3.Row
        mc.migrate_database(self.conn)
        self.addCleanup(self.conn.close)
        self.settings = SimpleNamespace(
            bazarr=mc.BazarrSettings("http://bazarr", "k", 30, 360), api_timeout_seconds=5,
            instance=lambda name: mc.ArrInstance("sonarr", "sonarr", "http://s", "k", "/tv", "/media/tv"))
        self.owner = mc.Ownership("sonarr", "series", 7)
        self.path = "/media/tv/Show/S01E01.mkv"

    def job(self, minutes_ago, force=0):
        now = time.time()
        self.n = getattr(self, "n", 0) + 1  # one active job per path
        self.conn.execute("INSERT INTO jobs(path, state, trigger, enqueued_at, available_at, force) "
                          "VALUES(?, 'running', 'hook', ?, ?, ?)", (f"{self.path}#{self.n}", now - minutes_ago * 60,
                                                                    now, force))
        return self.conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()

    def episodes(self, missing):
        return {"data": [{"path": "/tv/Show/S01E01.mkv", "sonarrEpisodeId": 11,
                          "missing_subtitles": [{"code2": m} for m in missing]}]}

    def gate(self, row, reply=None, error=None):
        def fake(bazarr, method, endpoint, settings, params=None, form=None):
            if error:
                raise RuntimeError(error)
            return reply
        with patch.object(mc, "bazarr_request", side_effect=fake):
            return mc.bazarr_gate(self.conn, row, self.settings, self.owner, self.path, LOG)

    def state(self, row):
        return self.conn.execute("SELECT state, available_at, last_error FROM jobs WHERE id=?", (row["id"],)).fetchone()

    def test_minimum_wait(self):
        row = self.job(5)
        self.assertFalse(self.gate(row))
        st = self.state(row)
        self.assertEqual(st["state"], "queued")
        self.assertAlmostEqual(st["available_at"], row["enqueued_at"] + 1800, delta=1)

    def test_waits_while_bazarr_lacks_subtitles(self):
        row = self.job(60)
        self.assertFalse(self.gate(row, self.episodes(["pb"])))
        self.assertIn("pb", self.state(row)["last_error"])

    def test_converts_when_nothing_missing_or_after_the_maximum(self):
        self.assertTrue(self.gate(self.job(60), self.episodes([])))
        self.assertTrue(self.gate(self.job(400), self.episodes(["pb"])))

    def test_forced_unowned_or_unreachable_convert(self):
        self.assertTrue(self.gate(self.job(1, force=1)))
        self.assertTrue(self.gate(self.job(60), error="down"))
        self.assertTrue(mc.bazarr_gate(self.conn, self.job(1), self.settings, None, self.path, LOG))

    def test_records_matches_and_rescans(self):
        history = {"data": [{"action": 1, "language": {"code2": "en"}, "provider": "opensubtitlescom",
                             "description": "English subtitles downloaded with a score of 99.72%."},
                            {"action": 1, "language": {"code2": "pb"}, "provider": "opensubtitlescom",
                             "description": "with a score of 93.89%"},
                            {"action": 5, "description": "synchronization ended with an offset of 0 seconds"}]}
        calls = []

        def fake(bazarr, method, endpoint, settings, params=None, form=None):
            calls.append((method, endpoint, params, form))
            return {"episodes": self.episodes([]), "episodes/history": history}.get(endpoint)
        with patch.object(mc, "bazarr_request", side_effect=fake):
            mc.record_bazarr_matches(self.conn, self.settings, self.owner, self.path, LOG)
            mc.bazarr_rescan(self.settings, self.owner, LOG)
        rows = [tuple(r) for r in self.conn.execute("SELECT language, score, kind FROM bazarr_matches ORDER BY language")]
        self.assertEqual(rows, [("en", 99.72, "hash"), ("pb", 93.89, "name")])
        self.assertIn(("GET", "episodes/history", {"episodeid": 11}, None), calls)
        self.assertEqual(calls[-1], ("PATCH", "series", None, {"seriesid": 7, "action": "scan-disk"}))

    def test_settings(self):
        self.assertIsNone(mc.parse_bazarr({"url": "http://b"}))
        self.assertEqual(mc.parse_bazarr({"url": "http://b/", "api_key": "x", "min_wait_minutes": "10"}),
                         mc.BazarrSettings("http://b", "x", 10.0, 360.0))
        with self.assertRaises(mc.ConfigurationError):
            mc.parse_bazarr({"url": "http://b", "api_key": "x", "min_wait_minutes": 90, "max_wait_minutes": 30})


if __name__ == "__main__":
    unittest.main()
