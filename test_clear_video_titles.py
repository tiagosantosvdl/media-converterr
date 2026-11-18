import json
import os
import tempfile
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from clear_video_titles import (
    refresh_database_signature,
    remove_video_track_names,
    video_tracks_with_names,
)
from media_convert_5 import db_connect, db_upsert, file_signature


class VideoTitleMaintenanceTests(unittest.TestCase):
    @patch("clear_video_titles.subprocess.run")
    def test_finds_only_named_video_track_ordinals(self, run_mock):
        payload = {
            "tracks": [
                {"type": "video", "properties": {"track_name": "Old HEVC title"}},
                {"type": "audio", "properties": {"track_name": "English"}},
                {"type": "video", "properties": {}},
                {"type": "video", "properties": {"track_name": "Commentary video"}},
            ]
        }
        run_mock.return_value = CompletedProcess(
            ["mkvmerge"], 0, stdout=json.dumps(payload), stderr=""
        )

        self.assertEqual(video_tracks_with_names("movie.mkv"), [1, 3])
        run_mock.assert_called_once_with(
            ["mkvmerge", "-J", "movie.mkv"],
            capture_output=True,
            text=True,
            check=False,
        )

    @patch("clear_video_titles.subprocess.run")
    def test_removes_each_named_video_track_in_one_edit(self, run_mock):
        run_mock.return_value = CompletedProcess(
            ["mkvpropedit"], 0, stdout="", stderr=""
        )

        warning = remove_video_track_names("movie.mkv", [1, 3])

        self.assertEqual(warning, "")
        run_mock.assert_called_once_with(
            [
                "mkvpropedit",
                "movie.mkv",
                "--edit",
                "track:v1",
                "--delete",
                "name",
                "--edit",
                "track:v3",
                "--delete",
                "name",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_refreshes_existing_database_signature_without_changing_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "movie.mkv")
            with open(path, "wb") as handle:
                handle.write(b"before")

            conn = db_connect(os.path.join(directory, "media.db"))
            try:
                old_signature = file_signature(path)
                db_upsert(
                    conn,
                    path,
                    old_signature[0],
                    old_signature[1],
                    status="ok",
                    note="encoded-av1-sdr",
                    device=old_signature[2],
                    inode=old_signature[3],
                )

                with open(path, "ab") as handle:
                    handle.write(b"-metadata-edit")

                self.assertTrue(refresh_database_signature(conn, path))
                row = conn.execute(
                    """
                    SELECT size, mtime, status, note, device, inode
                    FROM files
                    WHERE path=?
                    """,
                    (path,),
                ).fetchone()
            finally:
                conn.close()

            signature = file_signature(path)
            self.assertEqual(row[:2], signature[:2])
            self.assertEqual(row[2:4], ("ok", "encoded-av1-sdr"))
            self.assertEqual(row[4:], signature[2:])


if __name__ == "__main__":
    unittest.main()
