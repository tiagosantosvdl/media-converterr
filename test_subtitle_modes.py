"""Subtitle settings: extract (OCR, bitmap sidecars or drop for bitmaps) or keep them in the container."""
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

import media_convert_5 as mc5
import media_converterr as mc

PROBE = json.dumps({"streams": [
    {"index": 0, "codec_type": "video", "codec_name": "hevc"},
    {"index": 1, "codec_type": "audio", "codec_name": "eac3", "tags": {"language": "eng"}},
    {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
    {"index": 3, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "dut"}},
    {"index": 4, "codec_type": "subtitle", "codec_name": "mov_text", "tags": {"language": "por"}},
    {"index": 5, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "fre"}},
]}).encode()


class SubtitleModeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mc5.apply_conversion_settings, None)
        langs = ["en", "eng", "nl", "dut", "pt", "por"]
        self.base = {"audio_languages": langs, "subtitle_languages": langs}

    def test_extract_is_the_default(self):
        mc5.apply_conversion_settings(self.base)
        with patch("media_convert_5.subprocess.check_output", return_value=PROBE):
            args = mc5.stream_mapping_args("x.mkv")
            subs = mc5.subtitle_extractions("x.mkv")
        self.assertEqual(args, ["-map", "0:1"])  # no subtitle in the container
        self.assertEqual([(s.index, s.kind, s.suffix) for s in subs],
                         [(2, "text", ".en.srt"), (4, "text", ".pt.srt"), (3, "pgs", ".nl.srt")])

    def test_bitmaps_dropped(self):
        mc5.apply_conversion_settings({**self.base, "bitmap_subtitles": "drop"})
        with patch("media_convert_5.subprocess.check_output", return_value=PROBE):
            subs = mc5.subtitle_extractions("x.mkv")
        self.assertEqual([s.index for s in subs], [2, 4])

    def test_embed_keeps_wanted_streams_in_the_container(self):
        mc5.apply_conversion_settings({**self.base, "subtitle_mode": "embed"})
        with patch("media_convert_5.subprocess.check_output", return_value=PROBE):
            args = mc5.stream_mapping_args("x.mkv")
            subs = mc5.subtitle_extractions("x.mkv")
        self.assertEqual(subs, [])
        self.assertEqual(args, ["-map", "0:1", "-map", "0:2", "-c:s:0", "copy", "-map", "0:3", "-c:s:1", "copy",
                                "-map", "0:4", "-c:s:2", "srt"])  # MP4 text becomes SRT; French not wanted

    def test_embed_skips_the_subtitle_pass(self):
        mc5.apply_conversion_settings({"subtitle_mode": "embed"})
        counts = mc.extract_subtitles_pass(None, None, logging.getLogger("t"), ["a.mkv"])
        self.assertEqual(counts["done"], 0)

    def test_bitmap_sidecars(self):
        with tempfile.TemporaryDirectory() as d:
            sup = os.path.join(d, "t.nl.srt.sup")
            open(sup, "wb").write(b"PG")
            mks = os.path.join(d, "t.pt.srt.mks")
            open(mks, "wb").write(b"x")
            temps = {".nl.srt": sup, ".pt.hi.srt": mks}

            def fake_extract(cmd, **kw):
                target = cmd[-1].split(":", 1)[1]
                open(target, "w").write("idx")
                open(target[:-4] + ".sub", "wb").write(b"sub")
            with patch("media_converterr.subprocess.run", side_effect=fake_extract):
                mc.keep_bitmap_sidecars(temps, logging.getLogger("t"))
            self.assertEqual(set(temps), {".nl.sup", ".pt.hi.idx", ".pt.hi.sub"})
            self.assertEqual(temps[".nl.sup"], sup)
            self.assertFalse(os.path.exists(mks))


if __name__ == "__main__":
    unittest.main()
