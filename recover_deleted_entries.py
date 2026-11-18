#!/usr/bin/env python3

"""Recover purged converter rows by matching SQLite recovery data to live files."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

import media_convert_5 as converter


@dataclass(frozen=True)
class RecoveredEntry:
    path: str
    size: int
    mtime: float
    status: str
    note: Optional[str]
    device: Optional[int]
    inode: Optional[int]


def looks_like_media_path(value: object) -> bool:
    if not isinstance(value, str) or not os.path.isabs(value):
        return False
    extension = value.rsplit(".", 1)[-1].lower()
    return extension in converter.convertible_extensions


def entry_from_values(values: tuple) -> Optional[RecoveredEntry]:
    if len(values) < 9 or not looks_like_media_path(values[0]):
        return None
    path, size, mtime, _sha256, _checked, status, note, device, inode = values[:9]
    if status != "ok" or not isinstance(size, int) or not isinstance(mtime, (int, float)):
        return None
    return RecoveredEntry(
        path=converter.normalize_path(path),
        size=size,
        mtime=float(mtime),
        status=status,
        note=note,
        device=device if isinstance(device, int) else None,
        inode=inode if isinstance(inode, int) else None,
    )


def load_recovered_entries(database_path: str) -> list[RecoveredEntry]:
    result = subprocess.run(
        ["sqlite3", database_path, ".recover"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "sqlite3 .recover failed"
        raise RuntimeError(detail)

    recovered = sqlite3.connect(":memory:")
    try:
        # The sqlite3 shell may prepend commands such as ".dbconfig" that
        # are not SQL and cannot be executed by Python's sqlite3 module.
        recovered_sql = "\n".join(
            line for line in result.stdout.splitlines()
            if not line.lstrip().startswith(".")
        )
        recovered.executescript(recovered_sql)
        entries = []

        tables = {
            row[0]
            for row in recovered.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "files" in tables:
            for row in recovered.execute(
                """
                SELECT path, size, mtime, sha256, last_checked, status, note,
                       device, inode
                FROM files
                """
            ):
                entry = entry_from_values(tuple(row))
                if entry is not None:
                    entries.append(entry)

        if "lost_and_found" in tables:
            for row in recovered.execute("SELECT * FROM lost_and_found"):
                values = tuple(row)
                for index, value in enumerate(values):
                    if looks_like_media_path(value):
                        entry = entry_from_values(values[index:])
                        if entry is not None:
                            entries.append(entry)
                        break
    finally:
        recovered.close()

    return list(dict.fromkeys(entries))


def scan_files(roots: list[str]) -> list[tuple[str, tuple[int, float, int, int]]]:
    files = []
    for base_path in roots:
        base_path = converter.normalize_path(base_path)
        if not os.path.isdir(base_path):
            print(f"WARNING: skipping unavailable directory: {base_path}", file=sys.stderr)
            continue
        for root, dirs, names in os.walk(base_path, topdown=True):
            dirs[:] = [name for name in dirs if name not in converter.exclude]
            for name in names:
                extension = name.rsplit(".", 1)[-1].lower()
                if extension not in converter.convertible_extensions:
                    continue
                path = converter.normalize_path(os.path.join(root, name))
                try:
                    files.append((path, converter.file_signature(path)))
                except OSError as error:
                    print(f"WARNING: cannot stat {path}: {error}", file=sys.stderr)
    return files


def find_recoveries(
    entries: list[RecoveredEntry],
    files: list[tuple[str, tuple[int, float, int, int]]],
    current_paths: set[str],
) -> list[tuple[RecoveredEntry, str, tuple[int, float, int, int]]]:
    by_identity: dict[tuple[int, int], list] = {}
    by_metadata: dict[tuple[int, float], list] = {}
    for path, signature in files:
        size, mtime, device, inode = signature
        by_identity.setdefault((device, inode), []).append((path, signature))
        by_metadata.setdefault((size, mtime), []).append((path, signature))

    proposed = []
    for entry in entries:
        if entry.path in current_paths or os.path.exists(entry.path):
            continue

        matches = []
        if entry.device is not None and entry.inode is not None:
            matches = by_identity.get((entry.device, entry.inode), [])
        if len(matches) != 1:
            matches = by_metadata.get((entry.size, entry.mtime), [])
        matches = [
            match for match in matches
            if match[0] not in current_paths and match[0] != entry.path
        ]
        if len(matches) == 1:
            path, signature = matches[0]
            proposed.append((entry, path, signature))

    counts: dict[str, int] = {}
    for _entry, path, _signature in proposed:
        counts[path] = counts.get(path, 0) + 1
    return [item for item in proposed if counts[item[1]] == 1]


def backup_database(conn: sqlite3.Connection, database_path: str) -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = f"{database_path}.backup.{timestamp}"
    backup = sqlite3.connect(backup_path)
    try:
        conn.backup(backup)
    finally:
        backup.close()
    return backup_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover verified converter rows deleted after media renames."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write verified recoveries; default behavior is a dry run",
    )
    parser.add_argument("--database", default=converter.DB_PATH)
    parser.add_argument("--lock", default=converter.LOCK_PATH)
    parser.add_argument(
        "roots",
        nargs="*",
        default=converter.watched_folders,
        help="library roots (default: converter watched folders)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if shutil.which("sqlite3") is None:
        print("ERROR: sqlite3 command not found", file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(os.path.abspath(args.lock)), exist_ok=True)
    try:
        lock_handle = converter.acquire_process_lock(args.lock)
    except BlockingIOError:
        print(f"ERROR: converter holds {args.lock}", file=sys.stderr)
        return 2

    conn = None
    try:
        entries = load_recovered_entries(args.database)
        files = scan_files(args.roots)
        conn = sqlite3.connect(args.database)
        current_paths = {
            row[0] for row in conn.execute("SELECT path FROM files")
        }
        recoveries = find_recoveries(entries, files, current_paths)

        if not recoveries:
            print("No unambiguous deleted entries can be recovered.")
            return 0

        for entry, path, signature in recoveries:
            print(f"Recover: {entry.path} -> {path}")

        if not args.apply:
            print(f"Dry run: recoverable={len(recoveries)}; rerun with --apply")
            return 0

        backup_path = backup_database(conn, args.database)
        print(f"Database backup: {backup_path}")
        for entry, path, signature in recoveries:
            size, mtime, device, inode = signature
            converter.db_upsert(
                conn,
                path,
                size,
                mtime,
                status="ok",
                note=entry.note or "recovered-ok",
                device=device,
                inode=inode,
            )
        print(f"Recovered entries: {len(recoveries)}")
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
        lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
