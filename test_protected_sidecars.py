"""Subtitles of an output whose path differs from its source survive the *arr's extra-file cleanup."""
import logging
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

import media_converterr as mc


class ProtectedSidecarTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.folder = os.path.join(d.name, "movies", "Film (2000)")
        os.makedirs(self.folder)
        os.makedirs(os.path.join(d.name, "data"))
        self.settings = SimpleNamespace(database_path=os.path.join(d.name, "data", "db.sqlite"))
        self.conn = sqlite3.connect(self.settings.database_path)
        self.conn.row_factory = sqlite3.Row
        mc.migrate_database(self.conn)
        self.addCleanup(self.conn.close)
        self.source = os.path.join(self.folder, "Film (2000) BR-DISK.m2ts")
        self.output = os.path.join(self.folder, "Film (2000) BR-DISK.mkv")
        open(self.output, "w").close()
        self.subs = []
        for suffix in (".en.srt", ".pt-BR.srt", ".nl.sup"):
            path = self.output[:-4] + suffix
            with open(path, "w") as f:
                f.write(suffix)
            self.subs.append(path)
        open(os.path.join(self.folder, "Film (2000) BR-DISK.nfo"), "w").close()
        self.log = logging.getLogger("t")

    def test_deleted_subtitles_come_back_until_released(self):
        self.assertEqual(mc.protect_sidecars(self.conn, self.settings, self.source, self.output, self.log), 3)
        for path in self.subs:  # Radarr: movie file missing from disk -> its extra files deleted
            os.remove(path)
        self.assertEqual(mc.restore_protected_sidecars(self.conn, self.log), 3)
        self.assertEqual(open(self.subs[1]).read(), ".pt-BR.srt")
        mc.release_protected_sidecars(self.conn, self.source, now=0)  # the delete webhook arrived long ago
        os.remove(self.subs[0])
        mc.restore_protected_sidecars(self.conn, self.log)  # still restored once more, then dropped
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM protected_sidecars").fetchone()[0], 0)
        os.remove(self.subs[0])  # a later deletion (the user, Bazarr) stands
        self.assertEqual(mc.restore_protected_sidecars(self.conn, self.log), 0)
        self.assertFalse(os.path.exists(self.subs[0]))

    def test_not_restored_when_the_video_is_gone_or_moved_on_purpose(self):
        mc.protect_sidecars(self.conn, self.settings, self.source, self.output, self.log)
        mc.forget_protected(self.settings.database_path, self.subs[1])
        os.remove(self.subs[1])
        self.assertEqual(mc.restore_protected_sidecars(self.conn, self.log), 0)
        os.remove(self.output)  # movie deleted in Radarr
        os.remove(self.subs[0])
        self.assertEqual(mc.restore_protected_sidecars(self.conn, self.log), 0)


if __name__ == "__main__":
    unittest.main()
