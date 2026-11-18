import json
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import media_converterr as mc
import sidecar_names as sn


def srt(lines):
    return "".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n{line}\n\n" for i, line in enumerate(lines, 1))


EN = srt(["What do you think you're doing with that?", "I don't know, it's just the way you have it."] * 12)
EN_HI = srt(["[DOOR SLAMS]", "What do you think you're doing with that?", "I'm just saying, it's your call."] * 12)
NL = srt(["Ik weet het niet, maar dat is wat hij zei.", "Je moet niet van mij verwachten dat ik het doe."] * 12)
PT_BR = srt(["Você não sabe o que está fazendo com isso.", "A gente vai pegar o ônibus, cara."] * 12)
PT_PT = srt(["Tu estás a brincar comigo, não é?", "Eu vou apanhar o autocarro para a casa de banho que tem isso."] * 12)


class DetectTests(unittest.TestCase):
    def test_languages(self):
        self.assertEqual(sn.detect_language(EN), "en")
        self.assertEqual(sn.detect_language(NL), "nl")
        self.assertEqual(sn.detect_language(PT_BR), "pt-BR")
        self.assertEqual(sn.detect_language(PT_PT), "pt")
        self.assertIsNone(sn.detect_language(srt(["♪ ♪", "..."])))

    def test_valid_and_mangled_names(self):
        for ok in (".en", ".nl", ".pt-BR", ".pt", ".en.hi", ".en.forced", ".pt-BR.hi.forced"):
            self.assertTrue(sn.is_valid_suffix(ok), ok)
        for bad in ("", ".hi", ".1.hi", ".BR.pt", ".BR.pt-BR", ".en.sdh", ".BR", ".sdh", ".cc", ".forced"):
            self.assertFalse(sn.is_valid_suffix(bad), bad)
        self.assertEqual(sn.repaired_suffix(".1.hi", "nl"), ".nl.hi")
        self.assertEqual(sn.repaired_suffix(".sdh", "en"), ".en.hi")
        self.assertEqual(sn.repaired_suffix(".sdh.forced", "en"), ".en.hi.forced")
        self.assertEqual(sn.repaired_suffix("", "pt-BR"), ".pt-BR")

    def test_sidecars_of_a_longer_named_video_are_not_claimed(self):
        names = ["Show - S01E01.mkv", "Show - S01E01.srt", "Show - S01E01 Part 2.mkv", "Show - S01E01 Part 2.srt",
                 "Show - S01E01.Extended.mkv", "Show - S01E01.Extended.srt", "Show - S01E01.en.srt"]
        self.assertEqual(sn.mangled_sidecars("/x/Show - S01E01.mkv", names), [("Show - S01E01.srt", "")])


class SameLinesTests(unittest.TestCase):
    def test_timing_is_ignored(self):
        self.assertTrue(sn.same_lines(NL, NL.replace(",000 -->", ",080 -->")))
        self.assertFalse(sn.same_lines(NL, NL.replace("Ik weet", "Wij weten")))


class RepairTests(unittest.TestCase):
    """The names a real Sonarr rename produced from .en.srt, .en.hi.srt, .nl.srt, .pt-BR.srt, .BR.pt-BR.srt."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.lib = os.path.join(d, "lib")
        os.makedirs(self.lib)
        self.settings = SimpleNamespace(database_path=os.path.join(d, "data", "db.sqlite"), jellyfin=None,
                                        api_timeout_seconds=5)
        self.video = os.path.join(self.lib, "Show - S01E01 EAC3.mkv")
        open(self.video, "w").close()
        for suffix, text in ((".en", EN), (".hi", EN_HI), (".nl", NL), ("", PT_BR), (".BR.pt", PT_BR)):
            with open(os.path.join(self.lib, f"Show - S01E01 EAC3{suffix}.srt"), "w") as f:
                f.write(text)

    def tearDown(self):
        self.dir.cleanup()

    def test_sonarr_rename_result_is_put_back(self):
        counts = mc.repair_sidecars(self.video, self.settings, logging.getLogger("t"))
        self.assertEqual(counts, {"renamed": 2, "duplicate": 1, "unclear": 0, "conflict": 0})
        self.assertEqual(sorted(os.listdir(self.lib)), sorted(
            ["Show - S01E01 EAC3.mkv", "Show - S01E01 EAC3.en.srt", "Show - S01E01 EAC3.en.hi.srt",
             "Show - S01E01 EAC3.nl.srt", "Show - S01E01 EAC3.pt-BR.srt"]))
        self.assertEqual(open(os.path.join(self.lib, "Show - S01E01 EAC3.en.hi.srt")).read(), EN_HI)
        stash = os.path.join(self.dir.name, "data", "odd-sidecars", self.lib.lstrip("/"))
        (moved,) = os.listdir(stash)  # one of the two Portuguese copies; the other took the name
        self.assertEqual(open(os.path.join(stash, moved)).read(), PT_BR)

    def test_numbered_copy_with_other_timing_is_a_duplicate(self):
        with open(os.path.join(self.lib, "Show - S01E01 EAC3.1.nl.srt"), "w") as f:
            f.write(NL.replace(",000 -->", ",080 -->"))
        counts = mc.repair_sidecars(self.video, self.settings, logging.getLogger("t"))
        self.assertEqual(counts["duplicate"], 2)
        self.assertEqual(open(os.path.join(self.lib, "Show - S01E01 EAC3.nl.srt")).read(), NL)

    def test_a_folder_argument_covers_its_videos(self):
        totals = mc.repair_sidecars_pass(self.settings, logging.getLogger("t"), [self.dir.name], dry_run=True)
        self.assertEqual((totals["videos"], totals["renamed"]), (1, 2))

    def test_dry_run_changes_nothing(self):
        before = sorted(os.listdir(self.lib))
        counts = mc.repair_sidecars(self.video, self.settings, logging.getLogger("t"), dry_run=True)
        self.assertEqual(counts, {"renamed": 2, "duplicate": 1, "unclear": 0, "conflict": 0})
        self.assertEqual(sorted(os.listdir(self.lib)), before)

    def test_conflict_and_unclear_are_left_alone(self):
        with open(os.path.join(self.lib, "Show - S01E01 EAC3.pt-BR.srt"), "w") as f:
            f.write(srt(["Outro texto você não está vendo isso aqui com a gente."] * 25))
        with open(os.path.join(self.lib, "Show - S01E01 EAC3.1.hi.srt"), "w") as f:
            f.write(srt(["♪ ♪"] * 5))
        counts = mc.repair_sidecars(self.video, self.settings, logging.getLogger("t"))
        self.assertEqual(counts["conflict"], 2)  # bare and .BR.pt both want .pt-BR, which holds another text
        self.assertEqual(counts["unclear"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.lib, "Show - S01E01 EAC3.srt")))
        self.assertTrue(os.path.exists(os.path.join(self.lib, "Show - S01E01 EAC3.1.hi.srt")))

    def test_rename_hook_repairs_and_tells_jellyfin(self):
        self.settings.jellyfin = mc.JellyfinSettings("http://jf", "token", ((self.lib, "/series"),))
        sent = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(request, timeout):
            sent.append((request.full_url, json.loads(request.data), request.get_header("Authorization")))
            return Response()
        with patch.object(mc.urllib.request, "urlopen", side_effect=fake_urlopen):
            mc.repair_after_rename([(self.video, None), (os.path.join(self.lib, "missing.mkv"), None)], self.settings,
                                   logging.getLogger("t"))
        self.assertEqual(sent, [("http://jf/Library/Media/Updated",
                                 {"Updates": [{"Path": "/series/Show - S01E01 EAC3.mkv", "UpdateType": "Modified"}]},
                                 'MediaBrowser Token="token"')])
        self.assertTrue(os.path.exists(os.path.join(self.lib, "Show - S01E01 EAC3.en.hi.srt")))


class FollowRenameTests(unittest.TestCase):
    """What Sonarr did live: it moved only the sidecars on its record and left the repaired ones behind."""

    def test_left_behind_sidecars_follow_the_video(self):
        with tempfile.TemporaryDirectory() as d:
            settings = SimpleNamespace(database_path=os.path.join(d, "data", "db.sqlite"), jellyfin=None)
            old, new = os.path.join(d, "Ep DV h265.mkv"), os.path.join(d, "Ep AV1.mkv")
            open(new, "w").close()
            files = {"Ep DV h265.en.hi.srt": EN_HI, "Ep DV h265.pt-BR.srt": PT_BR, "Ep DV h265.nl.srt": NL,
                     "Ep AV1.en.srt": EN, "Ep AV1.nl.srt": NL.replace(",000 -->", ",080 -->"),
                     "Ep DV h265 Extended.mkv": "", "Ep DV h265 Extended.en.srt": EN}
            for name, text in files.items():
                with open(os.path.join(d, name), "w") as f:
                    f.write(text)
            mc.repair_after_rename([(new, old)], settings, logging.getLogger("t"))
            self.assertEqual(sorted(n for n in os.listdir(d) if n != "data"), sorted(
                ["Ep AV1.mkv", "Ep AV1.en.srt", "Ep AV1.en.hi.srt", "Ep AV1.nl.srt", "Ep AV1.pt-BR.srt",
                 "Ep DV h265 Extended.mkv", "Ep DV h265 Extended.en.srt"]))
            self.assertEqual(open(os.path.join(d, "Ep AV1.en.hi.srt")).read(), EN_HI)
            self.assertTrue(os.path.exists(os.path.join(d, "data", "odd-sidecars", d.lstrip("/"), "Ep DV h265.nl.srt")))

    def test_nothing_moves_while_the_previous_video_still_exists(self):
        with tempfile.TemporaryDirectory() as d:
            settings = SimpleNamespace(database_path=os.path.join(d, "db.sqlite"), jellyfin=None)
            for name in ("A.mkv", "A.en.srt", "B.mkv"):
                open(os.path.join(d, name), "w").close()
            self.assertEqual(mc.follow_rename(os.path.join(d, "B.mkv"), os.path.join(d, "A.mkv"), settings,
                                              logging.getLogger("t")), 0)
            self.assertTrue(os.path.exists(os.path.join(d, "A.en.srt")))


class JellyfinDeletedTests(unittest.TestCase):
    def test_moved_output_is_sent_as_modified_plus_deleted(self):
        settings = SimpleNamespace(jellyfin=mc.JellyfinSettings("http://jf", "t", (("/lib/movies", "/movies"),)),
                                   api_timeout_seconds=5)
        sent = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with patch.object(mc.urllib.request, "urlopen", side_effect=lambda r, timeout: (sent.append(json.loads(r.data)), Response())[1]):
            mc.jellyfin_media_updated(settings, ["/lib/movies/A/A.mkv"], logging.getLogger("t"), deleted=["/lib/movies/A/A.mp4"])
        self.assertEqual(sent, [{"Updates": [{"Path": "/movies/A/A.mkv", "UpdateType": "Modified"},
                                             {"Path": "/movies/A/A.mp4", "UpdateType": "Deleted"}]}])


class JellyfinConfigTests(unittest.TestCase):
    def test_key_from_environment(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[media_converterr]\nunlinked_folders = []\n[jellyfin]\nurl = "http://127.0.0.1:8096/"\napi_key_env = "TEST_JF_KEY"\n'
                    'paths = [{ local = "/media/series", remote = "/series" }]\n')
        try:
            with patch.dict(os.environ, {"TEST_JF_KEY": "abc"}):
                settings = mc.load_settings(f.name)
            self.assertEqual(settings.jellyfin, mc.JellyfinSettings("http://127.0.0.1:8096", "abc",
                                                                    (("/media/series", "/series"),)))
            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(mc.load_settings(f.name).jellyfin)  # no key: no notifications
        finally:
            os.remove(f.name)


if __name__ == "__main__":
    unittest.main()
