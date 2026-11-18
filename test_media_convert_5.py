import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from media_convert_5 import (
    HDR10_PQ,
    UnsupportedVideoError,
    VideoInfo,
    build_cmd,
    db_connect,
    db_lookup,
    db_remove_orphans,
    db_reconcile_rename,
    db_upsert,
    file_signature,
    hdr_mode,
    stream_mapping_args,
)


def profile_10_info(**overrides):
    values = {
        "color_transfer": "smpte2084",
        "color_primaries": "bt2020",
        "color_space": "bt2020nc",
        "color_range": "tv",
        "codec_name": "av1",
        "dovi_profile": 10,
        "dovi_compatibility_id": 1,
        "dovi_bl_present": True,
    }
    values.update(overrides)
    return VideoInfo(**values)


class LanguageSettingTests(unittest.TestCase):
    def test_env_wins_then_local_file_then_keep_all(self):
        import media_convert_5
        with tempfile.TemporaryDirectory() as d:
            local = os.path.join(d, "local.toml")
            with open(local, "w") as f:
                f.write('audio_languages = ["fr", "fra"]\n')
            with patch.dict(os.environ, {"X_LANGS": "de, deu"}):
                self.assertEqual(media_convert_5._language_setting("X_LANGS", "audio_languages", local), ["de", "deu"])
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("X_LANGS", None)
                self.assertEqual(media_convert_5._language_setting("X_LANGS", "audio_languages", local), ["fr", "fra"])
                self.assertEqual(media_convert_5._language_setting("X_LANGS", "audio_languages", local + ".missing"), [])


class HdrModeProfile10Tests(unittest.TestCase):
    def test_hdr10_compatible_av1_base_layer_is_supported(self):
        self.assertEqual(hdr_mode(profile_10_info()), HDR10_PQ)

    def test_missing_base_layer_is_rejected(self):
        with self.assertRaisesRegex(UnsupportedVideoError, "no base layer"):
            hdr_mode(profile_10_info(dovi_bl_present=False))

    def test_non_hdr10_compatibility_id_is_rejected(self):
        with self.assertRaisesRegex(UnsupportedVideoError, "profile 10.1"):
            hdr_mode(profile_10_info(dovi_compatibility_id=4))

    def test_non_av1_codec_is_rejected(self):
        with self.assertRaisesRegex(
            UnsupportedVideoError, "requires an AV1 base layer"
        ):
            hdr_mode(profile_10_info(codec_name="hevc"))


class StreamMappingTests(unittest.TestCase):
    def mapping_args(self, streams):
        probe = json.dumps({"streams": streams}).encode()
        with patch("media_convert_5.subprocess.check_output", return_value=probe):
            return stream_mapping_args("movie.mkv")

    def test_unwanted_default_audio_is_dropped_when_wanted_audio_exists(self):
        streams = [
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "ac3",
                "tags": {"language": "ita"},
                "disposition": {"default": 1, "original": 0},
            },
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "truehd",
                "tags": {"language": "eng"},
                "disposition": {"default": 0, "original": 0},
            },
        ]

        self.assertEqual(self.mapping_args(streams), ["-map", "0:2"])

    def test_first_audio_is_kept_when_no_wanted_audio_exists(self):
        streams = [
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "ac3",
                "tags": {"language": "ita"},
                "disposition": {"default": 0, "original": 0},
            },
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "aac",
                "tags": {"language": "fra"},
                "disposition": {"default": 1, "original": 0},
            },
        ]

        self.assertEqual(self.mapping_args(streams), ["-map", "0:1"])

    def test_explicit_original_audio_is_kept_with_wanted_audio(self):
        streams = [
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "ac3",
                "tags": {"language": "ita"},
                "disposition": {"default": 1, "original": 1},
            },
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "truehd",
                "tags": {"language": "eng"},
                "disposition": {"default": 0, "original": 0},
            },
        ]

        self.assertEqual(
            self.mapping_args(streams),
            ["-map", "0:1", "-map", "0:2"],
        )


class DatabaseRenameTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.conn = db_connect(os.path.join(self.temp_dir.name, "media.db"))

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def create_file(self, name, contents=b"video"):
        path = os.path.join(self.temp_dir.name, name)
        with open(path, "wb") as handle:
            handle.write(contents)
        return path

    def remember(self, path, status="ok", include_identity=True):
        size, mtime, device, inode = file_signature(path)
        db_upsert(
            self.conn,
            path,
            size,
            mtime,
            status=status,
            device=device if include_identity else None,
            inode=inode if include_identity else None,
        )
        return size, mtime, device, inode

    def test_connect_migrates_legacy_database_without_losing_rows(self):
        self.conn.close()
        db_path = os.path.join(self.temp_dir.name, "legacy.db")
        legacy = sqlite3.connect(db_path)
        legacy.execute(
            """
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                size INTEGER,
                mtime REAL,
                sha256 TEXT,
                last_checked REAL,
                status TEXT,
                note TEXT
            )
            """
        )
        legacy.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?,?)",
            ("movie.mkv", 5, 123.0, None, 456.0, "ok", "encoded"),
        )
        legacy.commit()
        legacy.close()

        migrated = db_connect(db_path)
        try:
            columns = {
                row[1]
                for row in migrated.execute("PRAGMA table_info(files)").fetchall()
            }
            row = migrated.execute(
                "SELECT path, size, mtime, status FROM files"
            ).fetchone()
        finally:
            migrated.close()

        self.assertTrue({"device", "inode"}.issubset(columns))
        self.assertEqual(row, ("movie.mkv", 5, 123.0, "ok"))

    def test_legacy_exact_match_backfills_file_identity(self):
        path = self.create_file("movie.mkv")
        size, mtime, device, inode = self.remember(
            path, include_identity=False
        )

        row = db_lookup(self.conn, path, size, mtime, device, inode)
        stored_identity = self.conn.execute(
            "SELECT device, inode FROM files WHERE path=?", (path,)
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(stored_identity, (device, inode))

    def test_remount_identity_change_refreshes_identity_and_skips(self):
        path = self.create_file("movie.mkv")
        size, mtime, device, inode = file_signature(path)
        db_upsert(
            self.conn,
            path,
            size,
            mtime,
            status="ok",
            device=device + 1,
            inode=inode + 1,
        )

        row = db_lookup(self.conn, path, size, mtime, device, inode)
        stored_identity = self.conn.execute(
            "SELECT device, inode FROM files WHERE path=?", (path,)
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row[7:], (device, inode))
        self.assertEqual(stored_identity, (device, inode))

    def test_first_legacy_rename_uses_unique_size_and_mtime_match(self):
        old_path = self.create_file("old.mkv")
        signature = self.remember(old_path, include_identity=False)
        new_path = os.path.join(self.temp_dir.name, "new.mkv")
        os.rename(old_path, new_path)

        renamed_from = db_reconcile_rename(
            self.conn, new_path, *signature
        )
        stored = self.conn.execute(
            "SELECT path, device, inode FROM files"
        ).fetchone()

        self.assertEqual(renamed_from, old_path)
        self.assertEqual(stored, (new_path, signature[2], signature[3]))
        self.assertIsNotNone(db_lookup(self.conn, new_path, *signature))

    def test_ambiguous_legacy_metadata_match_is_not_a_rename(self):
        new_path = self.create_file("new.mkv")
        size, mtime, device, inode = file_signature(new_path)
        old_paths = [
            os.path.join(self.temp_dir.name, "old-one.mkv"),
            os.path.join(self.temp_dir.name, "old-two.mkv"),
        ]
        for old_path in old_paths:
            db_upsert(
                self.conn,
                old_path,
                size,
                mtime,
                status="ok",
            )

        renamed_from = db_reconcile_rename(
            self.conn, new_path, size, mtime, device, inode
        )

        self.assertIsNone(renamed_from)
        stored_paths = {
            row[0] for row in self.conn.execute("SELECT path FROM files")
        }
        self.assertEqual(stored_paths, set(old_paths))

    def test_touched_legacy_rename_does_not_use_metadata_fallback(self):
        old_path = self.create_file("old.mkv")
        original_signature = self.remember(
            old_path, include_identity=False
        )
        new_path = os.path.join(self.temp_dir.name, "new.mkv")
        os.rename(old_path, new_path)
        current_stat = os.stat(new_path)
        os.utime(
            new_path,
            ns=(current_stat.st_atime_ns, current_stat.st_mtime_ns + 1_000_000_000),
        )
        current_signature = file_signature(new_path)

        self.assertNotEqual(current_signature[1], original_signature[1])
        self.assertIsNone(
            db_reconcile_rename(self.conn, new_path, *current_signature)
        )

    def test_same_folder_rename_moves_db_path_and_skips(self):
        old_path = self.create_file("old.mkv")
        original_signature = self.remember(old_path)
        new_path = os.path.join(self.temp_dir.name, "new.mkv")

        os.rename(old_path, new_path)
        current_signature = file_signature(new_path)

        self.assertEqual(current_signature, original_signature)
        self.assertEqual(
            db_reconcile_rename(self.conn, new_path, *current_signature),
            old_path,
        )
        self.assertIsNotNone(
            db_lookup(self.conn, new_path, *current_signature)
        )
        paths = self.conn.execute("SELECT path FROM files").fetchall()
        self.assertEqual(paths, [(new_path,)])

    def test_parent_folder_rename_moves_db_path_and_skips(self):
        old_dir = os.path.join(self.temp_dir.name, "Old Movie")
        new_dir = os.path.join(self.temp_dir.name, "Movie (1979)")
        os.makedirs(old_dir)
        old_path = os.path.join(old_dir, "old.mkv")
        with open(old_path, "wb") as handle:
            handle.write(b"video")
        original_signature = self.remember(old_path)

        os.rename(old_dir, new_dir)
        new_path = os.path.join(new_dir, "old.mkv")
        current_signature = file_signature(new_path)

        self.assertEqual(current_signature, original_signature)
        self.assertEqual(
            db_reconcile_rename(self.conn, new_path, *current_signature),
            old_path,
        )
        self.assertIsNotNone(
            db_lookup(self.conn, new_path, *current_signature)
        )
        paths = self.conn.execute("SELECT path FROM files").fetchall()
        self.assertEqual(paths, [(new_path,)])

    def test_rename_followed_by_touch_moves_db_path_but_does_not_skip(self):
        old_path = self.create_file("old.mkv")
        original_signature = self.remember(old_path)
        new_path = os.path.join(self.temp_dir.name, "new.mkv")
        os.rename(old_path, new_path)
        current_stat = os.stat(new_path)
        os.utime(
            new_path,
            ns=(current_stat.st_atime_ns, current_stat.st_mtime_ns + 1_000_000_000),
        )
        current_signature = file_signature(new_path)

        self.assertEqual(current_signature[2:], original_signature[2:])
        self.assertNotEqual(current_signature[1], original_signature[1])
        self.assertEqual(
            db_reconcile_rename(self.conn, new_path, *current_signature),
            old_path,
        )
        self.assertIsNone(
            db_lookup(self.conn, new_path, *current_signature)
        )
        paths = self.conn.execute("SELECT path FROM files").fetchall()
        self.assertEqual(paths, [(new_path,)])

    def test_touch_at_existing_path_does_not_skip(self):
        path = self.create_file("movie.mkv")
        original_signature = self.remember(path)
        current_stat = os.stat(path)
        os.utime(
            path,
            ns=(current_stat.st_atime_ns, current_stat.st_mtime_ns + 1_000_000_000),
        )
        current_signature = file_signature(path)

        self.assertNotEqual(current_signature[1], original_signature[1])
        self.assertIsNone(db_lookup(self.conn, path, *current_signature))
        self.assertIsNone(
            db_reconcile_rename(self.conn, path, *current_signature)
        )

    def test_unique_size_and_mtime_match_handles_changed_inode(self):
        old_path = self.create_file("old.mkv")
        original_signature = self.remember(old_path)
        new_path = self.create_file("new.mkv")
        new_stat = os.stat(new_path)
        os.utime(
            new_path,
            ns=(new_stat.st_atime_ns, os.stat(old_path).st_mtime_ns),
        )
        new_signature = file_signature(new_path)
        os.remove(old_path)

        self.assertEqual(new_signature[:2], original_signature[:2])
        self.assertNotEqual(new_signature[3], original_signature[3])
        self.assertEqual(
            db_reconcile_rename(self.conn, new_path, *new_signature),
            old_path,
        )
        self.assertIsNotNone(
            db_lookup(self.conn, new_path, *new_signature)
        )

    def test_hard_link_is_not_a_rename_while_old_path_exists(self):
        old_path = self.create_file("old.mkv")
        self.remember(old_path)
        new_path = os.path.join(self.temp_dir.name, "new.mkv")
        os.link(old_path, new_path)
        signature = file_signature(new_path)

        self.assertIsNone(
            db_reconcile_rename(self.conn, new_path, *signature)
        )

    def test_error_record_never_skips(self):
        path = self.create_file("movie.mkv")
        signature = self.remember(path, status="error")

        self.assertIsNone(db_lookup(self.conn, path, *signature))

    def test_orphan_cleanup_only_removes_missing_paths_in_scanned_folders(self):
        library = os.path.join(self.temp_dir.name, "library")
        outside = os.path.join(self.temp_dir.name, "outside")
        os.makedirs(library)
        os.makedirs(outside)
        existing_path = os.path.join(library, "existing.mkv")
        with open(existing_path, "wb") as handle:
            handle.write(b"video")
        self.remember(existing_path)

        orphaned_path = os.path.join(library, "missing.mkv")
        outside_path = os.path.join(outside, "missing.mkv")
        library_device = os.stat(library).st_dev
        for path, inode in ((orphaned_path, 101), (outside_path, 102)):
            db_upsert(
                self.conn,
                path,
                5,
                123.0,
                status="ok",
                device=library_device,
                inode=inode,
            )

        removed = db_remove_orphans(self.conn, [library])
        remaining = {
            row[0] for row in self.conn.execute("SELECT path FROM files")
        }

        self.assertEqual(removed, [orphaned_path])
        self.assertEqual(remaining, {existing_path, outside_path})

    def test_orphan_cleanup_defers_path_that_vanished_after_initial_scan(self):
        library = os.path.join(self.temp_dir.name, "library")
        os.makedirs(library)
        path = os.path.join(library, "renamed-during-run.mkv")
        with open(path, "wb") as handle:
            handle.write(b"video")
        self.remember(path)

        paths_seen_at_scan = {path}
        os.remove(path)

        self.assertEqual(
            db_remove_orphans(self.conn, [library], paths_seen_at_scan),
            [],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT path FROM files WHERE path=?", (path,)
            ).fetchone(),
            (path,),
        )

        self.assertEqual(db_remove_orphans(self.conn, [library]), [path])

    def test_orphan_cleanup_requires_a_verified_scan_folder(self):
        library = os.path.join(self.temp_dir.name, "library")
        os.makedirs(library)
        orphaned_path = os.path.join(library, "missing.mkv")
        db_upsert(
            self.conn,
            orphaned_path,
            5,
            123.0,
            status="ok",
            device=1,
            inode=101,
        )

        removed = db_remove_orphans(self.conn, [])
        stored = self.conn.execute(
            "SELECT path FROM files WHERE path=?", (orphaned_path,)
        ).fetchone()

        self.assertEqual(removed, [])
        self.assertEqual(stored, (orphaned_path,))

    def test_orphan_cleanup_keeps_rows_when_filesystem_device_is_absent(self):
        library = os.path.join(self.temp_dir.name, "library")
        os.makedirs(library)
        orphaned_path = os.path.join(library, "missing.mkv")
        different_device = os.stat(library).st_dev + 1
        db_upsert(
            self.conn,
            orphaned_path,
            5,
            123.0,
            status="ok",
            device=different_device,
            inode=101,
        )

        removed = db_remove_orphans(self.conn, [library])
        stored = self.conn.execute(
            "SELECT path FROM files WHERE path=?", (orphaned_path,)
        ).fetchone()

        self.assertEqual(removed, [])
        self.assertEqual(stored, (orphaned_path,))


class EncoderCommandTests(unittest.TestCase):
    def build_sdr_cmd(self):
        info = VideoInfo(
            color_transfer="bt709",
            color_primaries="bt709",
            color_space="bt709",
            color_range="tv",
        )
        with patch("media_convert_5.stream_mapping_args", return_value=[]):
            return build_cmd("input.mkv", "output.mkv", video_info=info)

    def test_uses_icq_with_external_bitrate_control(self):
        cmd = self.build_sdr_cmd()

        self.assertEqual(cmd[cmd.index("-global_quality") + 1], "20")
        self.assertEqual(cmd[cmd.index("-extbrc") + 1], "1")
        self.assertEqual(cmd[cmd.index("-look_ahead_depth") + 1], "100")
        self.assertNotIn("-q:v:0", cmd)

    def test_clears_stale_video_track_title(self):
        cmd = self.build_sdr_cmd()

        self.assertEqual(
            cmd[cmd.index("-metadata:s:v:0") + 1],
            "title=",
        )

    def test_uses_original_forced_keyframes_and_cluster_limits(self):
        cmd = self.build_sdr_cmd()

        self.assertEqual(cmd[cmd.index("-g") + 1], "120")
        self.assertEqual(
            cmd[cmd.index("-force_key_frames") + 1],
            "expr:gte(t,n_forced*2)",
        )
        self.assertEqual(cmd[cmd.index("-cluster_time_limit") + 1], "5000")
        self.assertEqual(cmd[cmd.index("-cluster_size_limit") + 1], "5242880")
        self.assertNotIn("-adaptive_i", cmd)


if __name__ == "__main__":
    unittest.main()
