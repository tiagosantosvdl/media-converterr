import os
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace
from unittest.mock import patch

import media_converterr as mc
from media_convert_5 import HDR10_PQ, VideoInfo, build_cmd


class OwnOutputTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conn = mc.connect_database(os.path.join(self.dir.name, "db.sqlite"))
        self.path = os.path.join(self.dir.name, "Show - S01E01.mkv")
        with open(self.path, "wb") as f:
            f.write(b"video")

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def record(self, note="encoded-hevc-hdr10-pq"):
        with self.conn:
            mc.upsert_file(self.conn, self.path, mc.SourceSignature.from_path(self.path), status="ok", note=note, owner=None)

    def touch(self):
        st = os.stat(self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    def test_touched_recorded_output_is_own_output(self):
        self.record()
        self.touch()
        with patch("media_convert_5.output_tag", return_value=None):
            self.assertTrue(mc.converter_output(self.conn, self.path, mc.SourceSignature.from_path(self.path)))

    def test_replaced_file_with_other_size_is_not_own_output(self):
        self.record()
        with open(self.path, "wb") as f:
            f.write(b"a new download")
        with patch("media_convert_5.output_tag", return_value=None):
            self.assertIsNone(mc.converter_output(self.conn, self.path, mc.SourceSignature.from_path(self.path)))

    def test_tag_marks_own_output_without_record(self):
        with patch("media_convert_5.output_tag", return_value="hevc-hdr10-pq"):
            self.assertEqual(mc.converter_output(self.conn, self.path, mc.SourceSignature.from_path(self.path)),
                             "tagged hevc-hdr10-pq")

    def test_force_flag_is_stored_and_kept_on_requeue(self):
        sig = mc.SourceSignature.from_path(self.path)
        job, created = mc.enqueue_job(self.conn, self.path, trigger="manual", signature=sig, force=True)
        self.assertTrue(created)
        again, created = mc.enqueue_job(self.conn, self.path, trigger="scan", signature=sig)
        self.assertEqual((again, created), (job, False))
        self.assertEqual(self.conn.execute("SELECT force FROM jobs WHERE id=?", (job,)).fetchone()[0], 1)

    def run_job(self, force):
        """Process one job for a low-bitrate source with the encoder stubbed; returns which path ran."""
        import logging
        import media_convert_5 as mc5
        sig = mc.SourceSignature.from_path(self.path)
        job, _ = mc.enqueue_job(self.conn, self.path, trigger="manual", signature=sig, force=force)
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
        ran = []

        def fake_encode(src, temp, *a):
            ran.append("encode")
            open(temp, "wb").write(b"encoded")

        settings = SimpleNamespace(work_dir=self.dir.name, callback_delay_seconds=0, database_path=os.path.join(self.dir.name, "db.sqlite"),
                                   retry_delays=(60,), max_attempts=3)
        info = VideoInfo(codec_name="hevc", color_transfer="smpte2084", color_primaries="bt2020", color_space="bt2020nc",
                         color_range="tv")
        with patch.object(mc, "encode_video", side_effect=fake_encode), \
                patch.object(mc5, "probe_video_info", return_value=info), \
                patch.object(mc5, "remux_decision", return_value="low bitrate"), \
                patch.object(mc5, "video_bitrate", return_value=2.4), \
                patch.object(mc5, "subtitle_extractions", return_value=[]), \
                patch.object(mc5, "build_remux_cmd", side_effect=lambda i, t, *a: ["remux", t]), \
                patch.object(mc5, "run", side_effect=lambda cmd, cwd, lg: (ran.append("remux"), open(cmd[-1], "wb").close(), (0, ""))[2]), \
                patch.object(mc5, "count_video_frames", return_value=1), \
                patch.object(mc5, "validate_output", return_value=info), \
                patch.object(mc5, "to_target_naming", side_effect=lambda p: p), \
                patch.object(mc, "choose_owner", return_value=None), \
                patch.object(mc, "jellyfin_media_updated") as told:
            mc.process_conversion_job(self.conn, row, settings, SimpleNamespace(), logging.getLogger("t"))
        told.assert_called_once_with(settings, [self.path], unittest.mock.ANY, deleted=[])
        return ran

    def test_forced_job_encodes_even_when_the_remux_rule_applies(self):
        self.assertEqual(self.run_job(force=True), ["encode"])

    def test_automatic_job_follows_the_remux_rule(self):
        self.assertEqual(self.run_job(force=False), ["remux"])

    def test_encoder_command_tags_output(self):
        info = VideoInfo(color_transfer="smpte2084", color_primaries="bt2020", color_space="bt2020nc", color_range="tv")
        with patch("media_convert_5.stream_mapping_args", return_value=[]):
            cmd = build_cmd("in.mkv", "out.mkv", video_info=info)
        self.assertIn(f"MEDIA_CONVERTERR=hevc-{HDR10_PQ}", cmd)


if __name__ == "__main__":
    unittest.main()


class TimestampCheckTests(unittest.TestCase):
    def test_decode_order_timestamps_are_rejected(self):
        from media_convert_5 import same_timestamps
        reordered = [0.0, 0.167, 0.083, 0.042, 0.125, 0.209]   # B-frames: display order differs from decode order
        sequential = [0.0, 0.042, 0.083, 0.125, 0.167, 0.209]  # what the broken MP4 step produced
        self.assertTrue(same_timestamps(reordered, [0.0, 0.1675, 0.083, 0.042, 0.125, 0.209]))
        self.assertFalse(same_timestamps(reordered, sequential))
        self.assertFalse(same_timestamps([], []))
