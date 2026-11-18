import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import forced_subs as fs
import media_converterr as mc

FULL = """1
00:00:01,000 --> 00:00:03,000
Where are you going?

2
00:00:10,000 --> 00:00:12,000
<i>Ich weiß es nicht.</i>
I don't know.

3
00:00:20,000 --> 00:00:22,000
Come back!
"""

FORCED = """1
00:00:10,100 --> 00:00:11,900
I don't know.

2
00:00:15,000 --> 00:00:17,000
CAROL STURKA - ALBUQUERQUE, NM

3
00:00:18,000 --> 00:00:19,000
​
"""


class MergeTests(unittest.TestCase):
    def test_only_missing_visible_cues_are_added_in_order(self):
        merged, added = fs.merge(FORCED, FULL)
        self.assertEqual(added, 1)  # the sign; the dialogue is already there; the blank cue is ignored
        cues = fs.parse_srt(merged)
        self.assertEqual([c.start for c in cues], [1.0, 10.0, 15.0, 20.0])
        self.assertEqual(cues[2].lines, ["CAROL STURKA - ALBUQUERQUE, NM"])
        self.assertTrue(merged.startswith("1\n00:00:01,000 --> 00:00:03,000\n"))
        self.assertIn("4\n00:00:20,000 --> 00:00:22,000\nCome back!", merged)

    def test_nothing_missing_returns_the_full_text_unchanged(self):
        self.assertEqual(fs.merge(FORCED.split("\n\n2")[0], FULL), (FULL, 0))


class FoldTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.lib, self.data = os.path.join(d, "lib"), os.path.join(d, "data")
        os.makedirs(self.lib)
        self.settings = SimpleNamespace(database_path=os.path.join(self.data, "db.sqlite"))
        self.video = os.path.join(self.lib, "Show - S01E01.mkv")

    def tearDown(self):
        self.dir.cleanup()

    def write(self, name, text):
        path = os.path.join(self.lib, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_forced_is_merged_into_library_tracks_backed_up_and_archived(self):
        self.write("Show - S01E01.en.srt", FULL)
        self.write("Show - S01E01.en.hi.srt", FULL)
        forced = self.write("Show - S01E01.en.forced.srt", FORCED)
        mc.fold_forced_subtitles(self.video, {".en.forced.srt": forced}, self.settings, logging.getLogger("t"))
        self.assertEqual(sorted(os.listdir(self.lib)), ["Show - S01E01.en.hi.srt", "Show - S01E01.en.srt"])
        for name in ("Show - S01E01.en.srt", "Show - S01E01.en.hi.srt"):
            self.assertIn("CAROL STURKA", open(os.path.join(self.lib, name)).read())
            backup = os.path.join(self.data, "replaced-subtitles", self.lib.lstrip("/"), name)
            self.assertEqual(open(backup).read(), FULL)
        archived = os.path.join(self.data, "forced-subtitles", self.lib.lstrip("/"), "Show - S01E01.en.forced.srt")
        self.assertEqual(open(archived).read(), FORCED)

    def test_pending_full_track_is_merged_without_backup(self):
        full = self.write("pending.en.srt", FULL)
        forced = self.write("pending.en.forced.srt", FORCED)
        temps = {".en.srt": full, ".en.forced.srt": forced}
        mc.fold_forced_subtitles(self.video, temps, self.settings, logging.getLogger("t"))
        self.assertEqual(list(temps), [".en.srt"])
        self.assertIn("CAROL STURKA", open(full).read())
        self.assertFalse(os.path.exists(os.path.join(self.data, "replaced-subtitles")))

    def test_forced_without_full_track_is_archived_only(self):
        forced = self.write("Show - S01E01.nl.forced.srt", FORCED)
        mc.fold_forced_subtitles(self.video, {".nl.forced.srt": forced}, self.settings, logging.getLogger("t"))
        self.assertEqual(os.listdir(self.lib), [])
        self.assertTrue(os.path.exists(os.path.join(self.data, "forced-subtitles", self.lib.lstrip("/"),
                                                    "Show - S01E01.nl.forced.srt")))

    def test_sidecar_cleanup_finds_forced_files(self):
        self.write("Show - S01E01.en.srt", FULL)
        self.write("Show - S01E01.pt-BR.forced.srt", FORCED)
        self.write("Show - S01E01.en.forced.srt", FORCED)
        settings = SimpleNamespace(database_path=self.settings.database_path, scan_roots=(self.lib,), exclude=())
        counts = mc.fold_forced_sidecars(settings, logging.getLogger("t"), dry_run=True)
        self.assertEqual(counts, {"forced": 2, "cues added": 1, "no full track": 1, "full track": 0, "review": 0})
        mc.fold_forced_sidecars(settings, logging.getLogger("t"))
        self.assertEqual(os.listdir(self.lib), ["Show - S01E01.en.srt"])


    def fold(self, temps, fresh=frozenset()):
        with patch.object(mc, "FULL_TRACK_CUES", 2):  # FORCED has two cues: a "full track" here
            return mc.fold_forced_subtitles(self.video, temps, self.settings, logging.getLogger("t"), fresh)

    def test_long_forced_track_without_release_regular_becomes_the_regular_track(self):
        forced = self.write("pending.en.forced.srt", FORCED)
        temps = {".en.forced.srt": forced}
        self.assertEqual(self.fold(temps), [])
        self.assertEqual(temps, {".en.srt": forced})

    def test_long_forced_track_beside_a_release_regular_track_is_merged(self):
        full = self.write("pending.en.srt", FULL)
        forced = self.write("pending.en.forced.srt", FORCED)
        temps = {".en.srt": full, ".en.forced.srt": forced}
        self.fold(temps)
        self.assertEqual(temps, {".en.srt": full})
        self.assertIn("CAROL STURKA", open(full).read())

    def test_long_forced_track_after_a_fresh_release_regular_is_merged(self):
        self.write("Show - S01E01.en.srt", FULL)
        forced = self.write("pending.en.forced.srt", FORCED)
        temps = {".en.forced.srt": forced}
        self.fold(temps, frozenset({os.path.join(self.lib, "Show - S01E01.en.srt")}))
        self.assertEqual(temps, {})
        self.assertIn("CAROL STURKA", open(os.path.join(self.lib, "Show - S01E01.en.srt")).read())

    def test_long_library_sidecar_without_regular_is_renamed(self):
        forced = self.write("Show - S01E01.en.forced.srt", FORCED)
        self.fold({".en.forced.srt": forced})
        self.assertEqual(os.listdir(self.lib), ["Show - S01E01.en.srt"])
        self.assertEqual(open(os.path.join(self.lib, "Show - S01E01.en.srt")).read(), FORCED)

    def test_long_library_sidecar_beside_a_regular_is_left_for_review(self):
        self.write("Show - S01E01.en.srt", FULL)
        forced = self.write("Show - S01E01.en.forced.srt", FORCED)
        self.assertEqual(self.fold({".en.forced.srt": forced}), [".en.forced.srt"])
        self.assertEqual(sorted(os.listdir(self.lib)), ["Show - S01E01.en.forced.srt", "Show - S01E01.en.srt"])
        self.assertEqual(open(os.path.join(self.lib, "Show - S01E01.en.srt")).read(), FULL)


class ShortTrackTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.lib = os.path.join(d, "lib")
        os.makedirs(self.lib)
        self.settings = SimpleNamespace(database_path=os.path.join(d, "data", "db.sqlite"))
        self.video = os.path.join(self.lib, "Show - S01E01.mkv")

    def tearDown(self):
        self.dir.cleanup()

    def write(self, name, cues, folder=None):
        path = os.path.join(folder or self.dir.name, name)
        with open(path, "w") as f:
            f.write(fs.format_srt([fs.Cue(i * 3.0, i * 3.0 + 2, [f"line {i}"]) for i in range(cues)]))
        return path

    def fold(self, temps):
        return mc.fold_forced_subtitles(self.video, temps, self.settings, logging.getLogger("t"))

    def test_a_signs_only_track_is_archived_instead_of_installed(self):
        temps = {".en.srt": self.write("p.en.srt", 3), ".nl.srt": self.write("p.nl.srt", 400)}
        self.fold(temps)
        self.assertEqual(list(temps), [".nl.srt"])
        archived = os.path.join(self.dir.name, "data", "forced-subtitles", self.lib.lstrip("/"),
                                "Show - S01E01.en.forced.srt")
        self.assertTrue(os.path.exists(archived))

    def test_a_downloaded_sidecar_counts_as_the_longest_track(self):
        self.write("Show - S01E01.pt-BR.srt", 400, self.lib)
        temps = {".pt.srt": self.write("p.pt.srt", 6)}
        self.fold(temps)
        self.assertEqual(temps, {})

    def test_short_track_and_flagged_forced_track_become_one(self):
        self.write("Show - S01E01.en.srt", 400, self.lib)
        temps = {".en.hi.srt": self.write("p.en.hi.srt", 2), ".en.hi.forced.srt": self.write("p.f.srt", 1),
                 ".nl.srt": self.write("p.nl.srt", 400)}
        self.fold(temps)
        self.assertEqual(list(temps), [".nl.srt"])

    def test_similar_lengths_and_library_files_are_left_alone(self):
        short = self.write("Show - S01E01.en.srt", 3, self.lib)
        temps = {".nl.srt": self.write("p.nl.srt", 200), ".pt.srt": self.write("p.pt.srt", 120)}
        self.fold(temps)
        self.assertEqual(sorted(temps), [".nl.srt", ".pt.srt"])
        self.assertTrue(os.path.exists(short))


class BrazilianTests(unittest.TestCase):
    BR = "".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\nVocê não está aqui, a gente tem que ir com ele.\n\n" for i in range(1, 30))
    PT = "".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\nTu estás a falar com ele, não é para isso.\n\n" for i in range(1, 30))

    def relabel(self, texts, extra=()):
        with tempfile.TemporaryDirectory() as d:
            temps = {}
            for suffix, text in texts.items():
                temps[suffix] = os.path.join(d, "p" + suffix)
                with open(temps[suffix], "w") as f:
                    f.write(text)
            temps.update({k: os.path.join(d, "x" + k) for k in extra})
            mc.relabel_brazilian_tracks("/lib/Show - S01E01.mkv", temps, logging.getLogger("t"))
            return sorted(temps)

    def test_brazilian_text_gets_the_brazilian_name(self):
        self.assertEqual(self.relabel({".pt.srt": self.BR, ".pt.hi.srt": self.BR}), [".pt-BR.hi.srt", ".pt-BR.srt"])

    def test_european_text_and_a_release_brazilian_track_keep_the_name(self):
        self.assertEqual(self.relabel({".pt.srt": self.PT}), [".pt.srt"])
        self.assertEqual(self.relabel({".pt.srt": self.BR}, [".pt-BR.srt"]), [".pt-BR.srt", ".pt.srt"])


if __name__ == "__main__":
    unittest.main()
