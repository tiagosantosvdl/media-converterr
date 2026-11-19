"""Untagged subtitle streams (a lone Blu-ray .m2ts carries no language tags): named after the language of their text."""
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

import media_convert_5 as mc5
import media_converterr as mc


def srt(lines):
    return "\n".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n{line}\n" for i, line in enumerate(lines, 1))


EN = srt(["You don't know what that is.", "I'm just saying, the thing you have.", "And this is your car."] * 12)
EN_HI = srt(["[door slams]", "You don't know what that is.", "I'm just saying, the thing you have.",
             "And this is your car."] * 12)
ES = srt(["¿Qué es eso? Pero no tengo nada.", "Bueno, señor, una cosa con usted.", "Él está aquí, muy bien."] * 12)
PT_PT = srt(["Tu estás a brincar comigo, não é?", "Eu estava a ficar sóbrio, tenho a certeza.",
             "Não sei o que isso é, mas está aqui para uma coisa muito boa."] * 12)
PT_BR = srt(["Você não sabe o que é isso.", "Eu tô falando com ele, cara.", "Está aqui, muito bom para uma."] * 12)

PROBE = json.dumps({"streams": [
    {"index": 3, "codec_name": "hdmv_pgs_subtitle"},
    {"index": 4, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}},
    {"index": 5, "codec_name": "subrip", "tags": {"language": "und"}},
]}).encode()


class UntaggedTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mc5.apply_conversion_settings, None)
        langs = ["en", "eng", "nl", "dut", "pt", "por"]
        mc5.apply_conversion_settings({"audio_languages": langs, "subtitle_languages": langs})
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        self.log = logging.getLogger("t")

    def temp(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_untagged_streams_are_extracted_last(self):
        with patch("media_convert_5.subprocess.check_output", return_value=PROBE):
            subs = mc5.subtitle_extractions("x.m2ts")
        self.assertEqual([(s.index, s.suffix) for s in subs],
                         [(4, ".en.srt"), (5, ".und5.srt"), (3, ".und3.srt")])

    def test_named_from_text(self):
        temps = {".en.srt": self.temp("a.srt", EN), ".und5.srt": self.temp("b.srt", PT_BR),
                 ".und6.srt": self.temp("c.srt", ES), ".und7.srt": self.temp("d.srt", EN_HI),
                 ".und8.srt": self.temp("e.srt", EN), ".und9.srt": self.temp("f.srt", srt(["♪ ♪"]))}
        mc.identify_untagged("/x/Film.mkv", temps, self.log)
        self.assertEqual(set(temps), {".en.srt", ".pt-BR.srt", ".en.hi.srt"})
        self.assertTrue(temps[".pt-BR.srt"].endswith("b.srt"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "c.srt")))  # Spanish: not wanted, removed
        self.assertFalse(os.path.exists(os.path.join(self.dir, "e.srt")))  # a second English track: name taken

    def test_bitmap_stream_is_sampled_by_ocr(self):
        import ocr_subtitles
        sup = self.temp("g.und3.srt.sup", "PG")
        temps = {".und3.srt": sup}
        result = ocr_subtitles.OcrResult(EN, 36, 90.0)
        with patch.object(ocr_subtitles, "sup_to_srt", return_value=result) as ocr:
            mc.identify_untagged("/x/Film.mkv", temps, self.log)
        self.assertEqual(ocr.call_args.args[1:], ("und",))
        self.assertEqual(ocr.call_args.kwargs, {"limit": mc.UNTAGGED_SAMPLE_CUES})
        self.assertEqual(temps, {".en.srt": sup})  # still the .sup: OCR'd in full afterwards, as English

    def test_portuguese_bitmap_is_ocrd_in_full_for_the_variant(self):
        import ocr_subtitles
        from types import SimpleNamespace
        sup = self.temp("h.und6.srt.sup", "PG")
        temps = {".und6.srt": sup}
        settings = SimpleNamespace(database_path=os.path.join(self.dir, "data", "db.sqlite"))
        sample = ocr_subtitles.OcrResult(PT_BR[:400] + PT_PT, 30, 92.0)  # a sample could go either way
        whole = ocr_subtitles.OcrResult(PT_PT * 3, 108, 92.0)
        with patch.object(ocr_subtitles, "sup_to_srt", side_effect=[sample, whole]) as ocr:
            mc.identify_untagged("/x/Film.mkv", temps, self.log, settings)
        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(list(temps), [".pt.srt"])
        self.assertFalse(temps[".pt.srt"].endswith(".sup"))  # already OCR'd: the later OCR step skips it
        self.assertIn("estás a brincar", open(temps[".pt.srt"], encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
