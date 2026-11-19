import json
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import media_convert_5 as mc5
import media_converterr as mc


class SubtitleCodeTests(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(mc5.subtitle_code("eng"), "en")
        self.assertEqual(mc5.subtitle_code("dut"), "nl")
        self.assertEqual(mc5.subtitle_code("por"), "pt")
        self.assertEqual(mc5.subtitle_code("pob"), "pt-BR")
        self.assertEqual(mc5.subtitle_code("por", "Portuguese (Brazil)"), "pt-BR")
        self.assertIsNone(mc5.subtitle_code("und"))


class ExtractionTests(unittest.TestCase):
    def probe(self, streams):
        return json.dumps({"streams": streams}).encode()

    def test_wanted_text_tracks_with_flags(self):
        streams = [
            {"index": 2, "codec_name": "subrip", "tags": {"language": "eng"}, "disposition": {}},
            {"index": 3, "codec_name": "subrip", "tags": {"language": "eng", "title": "SDH"}, "disposition": {}},
            {"index": 4, "codec_name": "ass", "tags": {"language": "eng", "title": "Forced"}, "disposition": {"forced": 1}},
            {"index": 5, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "dut"}, "disposition": {}},
            {"index": 6, "codec_name": "subrip", "tags": {"language": "fre"}, "disposition": {}},
            {"index": 7, "codec_name": "subrip", "tags": {"language": "eng"}, "disposition": {}},   # duplicate name
            {"index": 8, "codec_name": "subrip", "tags": {}, "disposition": {}},                    # untagged: last
            {"index": 9, "codec_name": "subrip", "tags": {"language": "pob"}, "disposition": {}},
            {"index": 10, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}, "disposition": {}},  # text wins
            {"index": 11, "codec_name": "dvd_subtitle", "tags": {"language": "por"}, "disposition": {}},
        ]
        with patch("media_convert_5.subprocess.check_output", return_value=self.probe(streams)), \
                patch.object(mc5, "wanted_subtitle_languages", ["en", "nl", "pt", "pt-BR"]):
            subs = mc5.subtitle_extractions("x.mkv")
        self.assertEqual([(s.index, s.suffix, s.kind) for s in subs],
                         [(2, ".en.srt", "text"), (3, ".en.hi.srt", "text"), (4, ".en.forced.srt", "text"),
                          (9, ".pt-BR.srt", "text"), (5, ".nl.srt", "pgs"), (11, ".pt.srt", "vobsub"),
                          (8, ".und8.srt", "text")])
        args, temps = mc5.subtitle_output_args(subs[-3:-1], "/work/temp.mkv")
        self.assertEqual(args, ["-map", "0:5", "-c:s", "copy", "-f", "sup", "/work/temp.mkv.nl.srt.sup",
                                "-map", "0:11", "-c:s", "copy", "-f", "matroska", "/work/temp.mkv.pt.srt.mks"])
        args, temps = mc5.subtitle_output_args(subs[:1], "/work/temp.mkv")
        self.assertEqual(args, ["-map", "0:2", "-c:s", "srt", "-f", "srt", "/work/temp.mkv.en.srt"])
        self.assertEqual(temps, {".en.srt": "/work/temp.mkv.en.srt"})


class InstallTests(unittest.TestCase):
    def test_installs_and_backs_up_existing_download(self):
        with tempfile.TemporaryDirectory() as d:
            lib, work, data = (os.path.join(d, x) for x in ("lib", "work", "data"))
            for x in (lib, work, data):
                os.makedirs(x)
            video = os.path.join(lib, "Show - S01E01.mkv")
            open(video, "w").close()
            with open(os.path.join(lib, "Show - S01E01.nl.srt"), "w") as f:
                f.write("downloaded")
            temps = {}
            for suffix, text in ((".nl.srt", "release nl"), (".en.srt", "release en"), (".pt.srt", "")):
                temps[suffix] = os.path.join(work, "t" + suffix)
                with open(temps[suffix], "w") as f:
                    f.write(text)
            settings = SimpleNamespace(database_path=os.path.join(data, "db.sqlite"))
            n = mc.install_subtitles(video, temps, settings, logging.getLogger("t"))
            self.assertEqual(n, 2)  # the empty one is not installed
            self.assertEqual(open(os.path.join(lib, "Show - S01E01.nl.srt")).read(), "release nl")
            backup = os.path.join(data, "replaced-subtitles", lib.lstrip("/"), "Show - S01E01.nl.srt")
            self.assertEqual(open(backup).read(), "downloaded")
            self.assertFalse(any(os.listdir(work)))  # temporaries removed


if __name__ == "__main__":
    unittest.main()
