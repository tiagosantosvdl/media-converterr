"""Subtitle files from the releases are recorded by content, at install and (once) from the converter's log."""
import calendar
import logging
import os
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace

import media_converterr as mc


class ReleaseSidecarTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = d.name
        os.makedirs(os.path.join(d.name, "data"))
        self.folder = os.path.join(d.name, "tv", "Show", "Season 1")
        os.makedirs(self.folder)
        self.settings = SimpleNamespace(database_path=os.path.join(d.name, "data", "db.sqlite"))
        self.log = logging.getLogger("t")

    def write(self, name, text, mtime=None):
        path = os.path.join(self.folder, name)
        with open(path, "w") as f:
            f.write(text)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def listing(self):
        with open(os.path.join(self.root, "data", mc.RELEASE_LIST)) as f:
            return f.read().split()

    def test_installed_subtitles_are_recorded(self):
        video = os.path.join(self.folder, "Show - S01E01.mkv")
        open(video, "w").close()
        temp = os.path.join(self.root, "t.srt")
        with open(temp, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        mc.install_subtitles(video, {".en.srt": temp}, self.settings, self.log)
        self.assertEqual(self.listing(), [mc._sha1(video[:-4] + ".en.srt")])

    def test_backfill_matches_folder_suffix_and_time(self):
        when = calendar.timegm((2026, 11, 17, 16, 12, 56, 0, 0, 0))
        extracted = self.write("Show - S01E01 - Renamed Since.en.srt", "release", when - 1)
        downloaded = self.write("Show - S01E01 - Renamed Since.nl.srt", "bazarr", when - 3)  # suffix not logged
        older = self.write("Show - S01E02 - Other.en.srt", "older", when - 86400)            # outside the window
        log = os.path.join(self.root, "media-converterr.log")
        with open(log, "w") as f:
            f.write(f"2026-11-17 16:12:56,529 INFO Subtitles moved out: 1 of 2 streams extracted (.en.srt), file remuxed "
                    f"without subtitles: {os.path.join(self.folder, 'Show (2009) - S01E01 - Old Name.mkv')}\n"
                    "2026-11-17 16:13:00,000 INFO Something else\n")
        counts = mc.backfill_release_sidecars(self.settings, self.log, [log])
        self.assertEqual(counts, {"events": 1, "files": 1})
        self.assertEqual(self.listing(), [mc._sha1(extracted)])
        self.assertNotIn(mc._sha1(downloaded), self.listing())
        self.assertNotIn(mc._sha1(older), self.listing())


if __name__ == "__main__":
    unittest.main()
