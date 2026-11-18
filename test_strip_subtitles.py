import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace
from unittest.mock import patch

import media_convert_5 as mc5
import media_converterr as mc


@unittest.skipIf(not shutil.which("ffmpeg"), "needs ffmpeg")
class StripSubtitlesTests(unittest.TestCase):
    def test_moves_subtitles_out_and_keeps_streams(self):
        with tempfile.TemporaryDirectory() as d:
            lib, work, data = (os.path.join(d, x) for x in ("lib", "work", "data"))
            for x in (lib, work, data):
                os.makedirs(x)
            for lang, text in (("en", "Hello there."), ("nl", "Hallo daar."), ("fr", "Bonjour.")):
                with open(os.path.join(d, f"{lang}.srt"), "w") as f:
                    f.write(f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n")
            with open(os.path.join(d, "fr.srt"), "a") as f:  # ends after the picture: container duration > video
                f.write("\n2\n00:00:04,000 --> 00:00:05,500\nFin.\n")
            with open(os.path.join(d, "forced.srt"), "w") as f:
                f.write("1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n2\n00:00:02,200 --> 00:00:02,800\nMOSCOW\n")
            video = os.path.join(lib, "Show - S01E01.mkv")
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=24:duration=3",
                            "-f", "lavfi", "-i", "sine=duration=3", "-i", os.path.join(d, "en.srt"),
                            "-i", os.path.join(d, "nl.srt"), "-i", os.path.join(d, "fr.srt"),
                            "-i", os.path.join(d, "forced.srt"),
                            "-map", "0", "-map", "1", "-map", "2", "-map", "3", "-map", "4", "-map", "5",
                            "-c:v", "mpeg4", "-c:a", "aac", "-c:s", "srt", "-metadata:s:s:0", "language=eng",
                            "-metadata:s:s:1", "language=dut", "-metadata:s:s:2", "language=fre",
                            "-metadata:s:s:3", "language=eng", "-disposition:s:3", "forced", video], check=True)
            with open(os.path.join(lib, "Show - S01E01.nl.srt"), "w") as f:
                f.write("1\n00:00:01,000 --> 00:00:02,000\nDownloaded.\n")
            conn = mc.connect_database(os.path.join(data, "db.sqlite"))
            settings = SimpleNamespace(database_path=os.path.join(data, "db.sqlite"), work_dir=work,
                                       callback_delay_seconds=0, instances=(), unlinked_folders=())
            catalog = SimpleNamespace(owner_for_path=lambda p: None)
            with patch.object(mc5, "wanted_subtitle_languages", ["en", "nl"]), \
                    patch.object(mc, "choose_owner", return_value=None), \
                    patch.object(mc5, "run", wraps=mc5.run) as ran:
                deferred = []
                result = mc.strip_subtitles(conn, video, settings, catalog, logging.getLogger("t"),
                                            background=deferred.append)
            cmd = ran.call_args.args[0]
            staged = os.path.join(lib, ".Show - S01E01.mkv.partial")  # beside the original, not a video name
            self.assertEqual(cmd[cmd.index(staged) - 2:cmd.index(staged)], ["-f", "matroska"])
            self.assertEqual(len(deferred), 1)  # the OCR step is handed to the caller, after the swap
            with patch.object(mc, "jellyfin_media_updated") as told:
                deferred[0]()
            told.assert_called_once_with(settings, [video], unittest.mock.ANY)  # after the sidecars are in place
            self.assertEqual(result, "done")
            kinds = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                                    video], capture_output=True, text=True).stdout.split()
            self.assertEqual(kinds, ["video", "audio"])
            en = open(os.path.join(lib, "Show - S01E01.en.srt")).read()
            self.assertIn("Hello there.", en)
            self.assertIn("MOSCOW", en)  # the forced-only cue was merged in
            self.assertEqual(en.count("Hello there."), 1)
            self.assertIn("Hallo daar.", open(os.path.join(lib, "Show - S01E01.nl.srt")).read())
            self.assertFalse(os.path.exists(os.path.join(lib, "Show - S01E01.fr.srt")))
            backup = os.path.join(data, "replaced-subtitles", lib.lstrip("/"), "Show - S01E01.nl.srt")
            self.assertIn("Downloaded.", open(backup).read())
            self.assertEqual(sorted(os.listdir(lib)), ["Show - S01E01.en.srt", "Show - S01E01.mkv", "Show - S01E01.nl.srt"])
            self.assertEqual(os.listdir(work), [])
            self.assertEqual(mc.strip_subtitles(conn, video, settings, catalog, logging.getLogger("t")), "none")


class OcrFailureTests(unittest.TestCase):
    def test_failed_ocr_keeps_the_raw_stream(self):
        import ocr_subtitles
        with tempfile.TemporaryDirectory() as d:
            sup = os.path.join(d, "x.mkv.en.srt.sup")
            with open(sup, "wb") as f:
                f.write(b"PGraw")
            temps = {".en.srt": sup}
            settings = SimpleNamespace(database_path=os.path.join(d, "data", "db.sqlite"))
            with patch.object(ocr_subtitles, "sup_to_srt", side_effect=ValueError("bad")):
                mc.ocr_bitmap_subtitles("/lib/Show - S01E01.mkv", temps, settings, logging.getLogger("t"))
            self.assertEqual(temps, {})
            kept = os.path.join(d, "data", "ocr-rejected", "lib", "Show - S01E01.en.srt.sup")
            self.assertEqual(open(kept, "rb").read(), b"PGraw")
            self.assertFalse(os.path.exists(sup))


class OcrRetryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.lib = os.path.join(d, "lib", "Show")
        os.makedirs(self.lib)
        open(os.path.join(self.lib, "Show - S01E01.mkv"), "w").close()
        self.settings = SimpleNamespace(database_path=os.path.join(d, "data", "db.sqlite"), work_dir=os.path.join(d, "work"),
                                        convertible_extensions=("mkv",), subtitle_languages=())
        os.makedirs(self.settings.work_dir)
        self.rejected = os.path.join(d, "data", "ocr-rejected")
        self.raw = os.path.join(self.rejected, self.lib.lstrip("/"), "Show - S01E01.en.srt.mks")
        os.makedirs(os.path.dirname(self.raw))
        with open(self.raw, "wb") as f:
            f.write(b"raw")
        gone = os.path.join(self.rejected, self.lib.lstrip("/"), "Gone - S01E02.pt-BR.srt.sup")
        with open(gone, "wb") as f:
            f.write(b"raw")

    def tearDown(self):
        self.dir.cleanup()

    def run_retry(self, ocr):
        def fake(path, temps, settings, logger):
            ocr(temps, settings)
        with patch.object(mc, "ocr_bitmap_subtitles", side_effect=fake):
            return mc.retry_rejected_ocr(self.settings, logging.getLogger("t"))

    def test_installs_the_new_ocr_and_drops_the_raw_stream(self):
        def ok(temps, settings):
            (suffix, temp), = temps.items()
            with open(temp[:-4], "w") as f:
                f.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")
            os.remove(temp)
            temps[suffix] = temp[:-4]
        counts = self.run_retry(ok)
        self.assertEqual(counts, {"raw": 2, "installed": 1, "low confidence": 0, "failed": 0, "no video": 1})
        self.assertIn("Hello", open(os.path.join(self.lib, "Show - S01E01.en.srt")).read())
        self.assertFalse(os.path.exists(self.raw))
        self.assertEqual(os.listdir(self.settings.work_dir), [])

    def test_a_second_failure_keeps_the_raw_stream(self):
        def fail(temps, settings):
            (suffix, temp), = temps.items()
            os.replace(temp, self.raw)
            del temps[suffix]
        counts = self.run_retry(fail)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(open(self.raw, "rb").read(), b"raw")
        self.assertFalse(os.path.exists(os.path.join(self.lib, "Show - S01E01.en.srt")))

    def test_low_confidence_keeps_the_text_only(self):
        def low(temps, settings):
            (suffix, temp), = temps.items()
            with open(self.raw[:-4], "w") as f:
                f.write("garbled")
            os.remove(temp)
            del temps[suffix]
        counts = self.run_retry(low)
        self.assertEqual(counts["low confidence"], 1)
        self.assertFalse(os.path.exists(self.raw))
        self.assertTrue(os.path.exists(self.raw[:-4]))


class EmptyExtractionTests(unittest.TestCase):
    def test_empty_output_from_a_stream_with_content_stops_the_swap(self):
        with tempfile.TemporaryDirectory() as d:
            empty = os.path.join(d, "x.en.srt")
            open(empty, "w").close()
            sub = mc5.SubtitleExtraction(index=3, code="en")
            with patch.object(mc, "_packet_count", return_value=12):
                with self.assertRaises(RuntimeError):
                    mc.check_text_extractions("v.mkv", [sub], {sub.suffix: empty})
            with patch.object(mc, "_packet_count", return_value=0):  # a forced track without lines
                mc.check_text_extractions("v.mkv", [sub], {sub.suffix: empty})


class ScanSkipsHiddenTests(unittest.TestCase):
    def test_hidden_temp_files_are_not_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("Show - S01E01.mkv", ".Show - S01E02.mkv.strip.tmp.mkv", ".Show - S01E03.mkv.partial"):
                open(os.path.join(d, name), "w").close()
            settings = SimpleNamespace(scan_roots=(d,), exclude=(), convertible_extensions={"mkv"})
            paths, _ = mc.collect_scan_paths(settings, logging.getLogger("t"))
        self.assertEqual([os.path.basename(p) for p in paths], ["Show - S01E01.mkv"])


if __name__ == "__main__":
    unittest.main()
