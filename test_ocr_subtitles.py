import shutil
import struct
import unittest

import ocr_subtitles as ocr

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Pillow is in the image; the host may not have it
    Image = None


def rle_encode(pixels: bytes, width: int, height: int) -> bytes:
    out = bytearray()
    for y in range(height):
        row, x = pixels[y * width:(y + 1) * width], 0
        while x < width:
            c, run = row[x], 1
            while x + run < width and row[x + run] == c and run < 16383:
                run += 1
            if c == 0:
                out += bytes([0, run]) if run < 64 else bytes([0, 0x40 | (run >> 8), run & 0xFF])
            elif run < 3:
                out += bytes([c]) * run
            elif run < 64:
                out += bytes([0, 0x80 | run, c])
            else:
                out += bytes([0, 0xC0 | (run >> 8), run & 0xFF, c])
            x += run
        out += b"\x00\x00"
    return bytes(out)


def segment(kind: int, pts: float, data: bytes) -> bytes:
    return b"PG" + struct.pack(">IIBH", int(pts * 90000), 0, kind, len(data)) + data


def display_set(pts: float, end: float, pixels: bytes, width: int, height: int) -> bytes:
    """One subtitle (palette 0: transparent, 1: white text, 2: black outline) shown from pts to end."""
    pcs = struct.pack(">HHBHBBBB", 1920, 1080, 0x10, 0, 0x80, 0, 0, 1) + struct.pack(">HBBHH", 0, 0, 0, 100, 900)
    wds = bytes([1]) + struct.pack(">BHHHH", 0, 100, 900, width, height)
    pds = bytes([0, 0]) + bytes([0, 16, 128, 128, 0, 1, 235, 128, 128, 255, 2, 16, 128, 128, 255])
    rle = rle_encode(pixels, width, height)
    ods = struct.pack(">HBB", 0, 0, 0xC0) + (len(rle) + 4).to_bytes(3, "big") + struct.pack(">HH", width, height) + rle
    clear = struct.pack(">HHBHBBBB", 1920, 1080, 0x10, 1, 0x00, 0, 0, 0)
    return (segment(0x16, pts, pcs) + segment(0x17, pts, wds) + segment(0x14, pts, pds) + segment(0x15, pts, ods)
            + segment(0x80, pts, b"") + segment(0x16, end, clear) + segment(0x80, end, b""))


def text_pixels(text: str) -> tuple[bytes, int, int]:
    font = ImageFont.load_default(size=44)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    left, top, right, bottom = probe.multiline_textbbox((0, 0), text, font=font, stroke_width=3)
    w, h = right - left + 8, bottom - top + 8
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).multiline_text((4 - left, 4 - top), text, font=font, fill=(255, 255, 255, 255),
                                       stroke_width=3, stroke_fill=(0, 0, 0, 255))
    out = bytes(0 if a < 128 else (1 if r > 128 else 2) for r, g, b, a in img.getdata())
    return out, w, h


class RleTests(unittest.TestCase):
    def test_round_trip(self):
        width, height = 70, 3
        pixels = bytes([0] * 5 + [1] * 2 + [2] * 40 + [0] * 23) + bytes([1] * 70) + bytes([0] * 70)
        self.assertEqual(bytes(ocr._rle_decode(rle_encode(pixels, width, height), width, height)), pixels)


@unittest.skipIf(Image is None, "Pillow not installed")
class ParseTests(unittest.TestCase):
    def test_parse_display_sets(self):
        p1, w1, h1 = text_pixels("Hello there.")
        data = display_set(1.0, 3.5, p1, w1, h1) + display_set(5.0, 7.25, p1, w1, h1)
        comps = ocr.parse_sup(data)
        self.assertEqual([(c.start, c.end) for c in comps], [(1.0, 3.5), (5.0, 7.25)])
        w, h, rle = comps[0].bitmaps[0]
        self.assertEqual((w, h), (w1, h1))
        self.assertEqual(bytes(ocr._rle_decode(rle, w, h)), p1)


@unittest.skipIf(Image is None or not shutil.which("tesseract"), "Tesseract not installed")
class OcrTests(unittest.TestCase):
    def test_sup_to_srt(self):
        lines = ["Where are you going?", "I'm not sure,\nbut I'll be back.", "Waar ga je heen?"]
        data = b"".join(display_set(2.0 + 4 * i, 4.0 + 4 * i, *text_pixels(t)) for i, t in enumerate(lines[:2]))
        result = ocr.sup_to_srt(data, "en")
        self.assertEqual(result.cues, 2)
        self.assertTrue(result.trusted, result)
        self.assertIn("00:00:02,000 --> 00:00:04,000\nWhere are you going?", result.srt)
        self.assertIn("I'm not sure,\nbut I'll be back.", result.srt)
        nl = ocr.sup_to_srt(display_set(1.0, 2.0, *text_pixels(lines[2])), "nl")
        self.assertIn("Waar ga je heen?", nl.srt)


@unittest.skipIf(Image is None or not all(shutil.which(t) for t in ("tesseract", "ffmpeg", "mkvmerge", "mkvextract")),
                 "needs Tesseract, ffmpeg and MKVToolNix")
class VobSubTests(unittest.TestCase):
    def test_dvd_subtitles_from_ffmpeg(self):
        import os, subprocess, tempfile
        with tempfile.TemporaryDirectory() as d:
            sup = os.path.join(d, "en.sup")
            with open(sup, "wb") as f:
                f.write(display_set(1.0, 3.0, *text_pixels("Good morning, Captain."))
                        + display_set(4.0, 6.5, *text_pixels("We have a problem,\nand it is getting worse.")))
            run = lambda *a: subprocess.run(list(a), cwd=d, capture_output=True, check=True)
            run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=720x480:rate=25:duration=8", "-c:v", "mpeg4", "v.mkv")
            run("mkvmerge", "-q", "-o", "pgs.mkv", "v.mkv", "en.sup")
            run("ffmpeg", "-v", "error", "-y", "-i", "pgs.mkv", "-map", "0:s:0", "-c:s", "dvd_subtitle", "-f", "matroska", "dvd.mks")
            run("mkvextract", "-q", "dvd.mks", "tracks", "0:dvd")
            with open(os.path.join(d, "dvd.idx")) as f, open(os.path.join(d, "dvd.sub"), "rb") as g:
                result = ocr.vobsub_to_srt(f.read(), g.read(), "en")
        self.assertTrue(result.trusted, result)
        self.assertIn("00:00:01,000 --> 00:00:04,000\nGood morning, Captain.", result.srt)  # unknown end: next start
        self.assertIn("We have a problem,\nand it is getting worse.", result.srt)

    def test_converter_ocr_step_reads_mkvextract_output(self):
        """The converter's own step, with a temp named as in production (mkvextract replaces the extension)."""
        import logging, os, subprocess, tempfile, types
        import media_converterr
        with tempfile.TemporaryDirectory() as d:
            sup = os.path.join(d, "en.sup")
            with open(sup, "wb") as f:
                f.write(display_set(1.0, 3.0, *text_pixels("Good morning, Captain.")))
            run = lambda *a: subprocess.run(list(a), cwd=d, capture_output=True, check=True)
            run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=720x480:rate=25:duration=5", "-c:v", "mpeg4", "v.mkv")
            run("mkvmerge", "-q", "-o", "pgs.mkv", "v.mkv", "en.sup")
            temp = os.path.join(d, "strip.1.abc.mkv.en.srt.mks")
            run("ffmpeg", "-v", "error", "-y", "-i", "pgs.mkv", "-map", "0:s:0", "-c:s", "dvd_subtitle", "-f", "matroska", temp)
            temps = {".en.srt": temp}
            settings = types.SimpleNamespace(database_path=os.path.join(d, "db", "x.sqlite3"))
            media_converterr.ocr_bitmap_subtitles("/lib/Show/Ep.mkv", temps, settings, logging.getLogger("t"))
            self.assertEqual(temps, {".en.srt": temp[:-4]})
            with open(temp[:-4]) as f:
                self.assertIn("Good morning, Captain.", f.read())
            self.assertEqual(sorted(os.listdir(d)), ["en.sup", "pgs.mkv", "strip.1.abc.mkv.en.srt", "v.mkv"])


if __name__ == "__main__":
    unittest.main()
