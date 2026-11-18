import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from recover_deleted_entries import (
    RecoveredEntry,
    entry_from_values,
    find_recoveries,
    load_recovered_entries,
)


class RecoveryMatchingTests(unittest.TestCase):
    @patch("recover_deleted_entries.subprocess.run")
    def test_filters_sqlite_shell_commands_from_recovery_output(self, run_mock):
        run_mock.return_value = CompletedProcess(
            ["sqlite3"],
            0,
            stdout=(
                ".dbconfig defensive off\n"
                "BEGIN;\n"
                "CREATE TABLE files("
                "path,size,mtime,sha256,last_checked,status,note,device,inode"
                ");\n"
                "INSERT INTO files VALUES("
                "'/library/old.mkv',123,456.25,NULL,789,'ok',"
                "'encoded-av1',59,5007"
                ");\n"
                "COMMIT;\n"
            ),
            stderr="",
        )

        entries = load_recovered_entries("media.db")

        self.assertEqual(
            entries,
            [
                RecoveredEntry(
                    path="/library/old.mkv",
                    size=123,
                    mtime=456.25,
                    status="ok",
                    note="encoded-av1",
                    device=59,
                    inode=5007,
                )
            ],
        )

    def test_extracts_complete_ok_row_from_recovery_values(self):
        entry = entry_from_values(
            (
                "/library/old.mkv",
                123,
                456.25,
                None,
                789.0,
                "ok",
                "encoded-av1-hdr10-pq",
                59,
                5007,
            )
        )

        self.assertEqual(
            entry,
            RecoveredEntry(
                path="/library/old.mkv",
                size=123,
                mtime=456.25,
                status="ok",
                note="encoded-av1-hdr10-pq",
                device=59,
                inode=5007,
            ),
        )

    def test_matches_deleted_row_to_renamed_file_by_identity(self):
        entry = RecoveredEntry(
            path="/library/old.mkv",
            size=123,
            mtime=456.25,
            status="ok",
            note="encoded-av1",
            device=59,
            inode=5007,
        )
        signature = (123, 456.25, 59, 5007)

        self.assertEqual(
            find_recoveries(
                [entry],
                [("/library/new.mkv", signature)],
                set(),
            ),
            [(entry, "/library/new.mkv", signature)],
        )

    def test_rejects_ambiguous_size_and_mtime_fallback(self):
        entry = RecoveredEntry(
            path="/library/old.mkv",
            size=123,
            mtime=456.25,
            status="ok",
            note="encoded-av1",
            device=None,
            inode=None,
        )
        files = [
            ("/library/new-one.mkv", (123, 456.25, 59, 1)),
            ("/library/new-two.mkv", (123, 456.25, 59, 2)),
        ]

        self.assertEqual(find_recoveries([entry], files, set()), [])


if __name__ == "__main__":
    unittest.main()
