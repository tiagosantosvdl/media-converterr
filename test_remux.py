import unittest
from unittest.mock import patch

from media_convert_5 import HDR10_PQ, HDR_SDR, DOVI_PROFILE_5, VideoInfo, build_remux_cmd, remux_decision, remux_reason


def info(**kw):
    base = dict(color_transfer="smpte2084", color_primaries="bt2020", color_space="bt2020nc", color_range="tv",
                codec_name="hevc", width=3840, height=2160, hdr10_plus=True)
    base.update(kw)
    return VideoInfo(**base)


class RemuxDecisionTests(unittest.TestCase):
    def test_low_bitrate_4k_hevc_is_remuxed(self):
        self.assertIn("2.4 Mbit/s", remux_reason(info(), HDR10_PQ, 2.4e6))
        self.assertIn("per megapixel", remux_reason(info(), HDR10_PQ, 2.4e6))

    def test_high_bitrate_or_inefficient_codec_is_encoded(self):
        self.assertIsNone(remux_reason(info(), HDR10_PQ, 40e6))
        self.assertIsNone(remux_reason(info(codec_name="h264"), HDR10_PQ, 2e6))
        self.assertIsNone(remux_reason(info(), HDR10_PQ, None))  # unknown bitrate: encode as before

    def test_limit_is_per_megapixel(self):
        hd = info(width=1920, height=1080, color_transfer="bt709", color_primaries="bt709", color_space="bt709")
        self.assertIsNotNone(remux_reason(hd, HDR_SDR, 3.5e6))  # 1.69 Mbit/s per megapixel
        self.assertIsNone(remux_reason(hd, HDR_SDR, 4.5e6))  # 2.17
        self.assertIsNotNone(remux_reason(info(), HDR10_PQ, 14.5e6))  # 4K: 1.75
        self.assertIsNone(remux_reason(info(), HDR10_PQ, 16e6))
        scope = info(width=3840, height=1600)  # cropped release: fewer pixels, lower limit
        self.assertIsNone(remux_reason(scope, HDR10_PQ, 12e6))
        self.assertIsNotNone(remux_reason(scope, HDR10_PQ, 10e6))

    def test_black_bars_count_against_the_picture(self):
        # 12 Mbit/s in a 3840x2160 frame passes (1.45), but the picture is 3840x1600 (1.95): encode
        self.assertIsNotNone(remux_reason(info(), HDR10_PQ, 12e6))
        self.assertIsNone(remux_reason(info(), HDR10_PQ, 12e6, (3840, 1600)))
        with patch("media_convert_5.video_bitrate", return_value=12e6), \
             patch("media_convert_5.picture_area", return_value=(3840, 1600)) as area:
            self.assertIsNone(remux_decision(info(), HDR10_PQ, "x.mkv"))
        with patch("media_convert_5.video_bitrate", return_value=40e6), \
             patch("media_convert_5.picture_area") as area:
            self.assertIsNone(remux_decision(info(), HDR10_PQ, "x.mkv"))
            area.assert_not_called()  # no bar detection when the full frame already says encode
        with patch("media_convert_5.video_bitrate", return_value=2.4e6), \
             patch("media_convert_5.picture_area", return_value=None):
            self.assertIsNotNone(remux_decision(info(), HDR10_PQ, "x.mkv"))  # unmeasurable: full frame decides

    def test_dolby_vision_is_always_encoded(self):
        self.assertIsNone(remux_reason(info(dovi_profile=7), HDR10_PQ, 3e6))
        self.assertIsNone(remux_reason(info(dovi_profile=5), DOVI_PROFILE_5, 3e6))
        self.assertIsNone(remux_reason(info(dovi_profile=8), HDR10_PQ, 3e6))  # no Dolby Vision kept

    def test_remux_command_copies_video_and_tags(self):
        with patch("media_convert_5.stream_mapping_args", return_value=["-map", "0:1"]):
            cmd = build_remux_cmd("in.mkv", "out.mkv", info(), HDR10_PQ)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertIn("MEDIA_CONVERTERR=remux-hevc-hdr10-pq", cmd)
        self.assertNotIn("hevc_qsv", cmd)


if __name__ == "__main__":
    unittest.main()
