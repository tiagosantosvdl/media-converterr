#!/usr/bin/env python3

"""Remove MKV video-track names without invalidating media_convert.db."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator

import media_convert_5 as converter


def video_tracks_with_names(path: str) -> list[int]:
    """Return one-based video-track ordinals whose Matroska name is non-empty."""
    result = subprocess.run(
        ["mkvmerge", "-J", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode > 1:
        detail = result.stderr.strip() or "mkvmerge identification failed"
        raise RuntimeError(detail)

    try:
        tracks = json.loads(result.stdout).get("tracks", [])
    except (json.JSONDecodeError, AttributeError) as error:
        raise RuntimeError(f"Invalid mkvmerge JSON: {error}") from error

    named_ordinals = []
    video_ordinal = 0
    for track in tracks:
        if track.get("type") != "video":
            continue
        video_ordinal += 1
        name = track.get("properties", {}).get("track_name")
        if str(name or "").strip():
            named_ordinals.append(video_ordinal)
    return named_ordinals


def remove_video_track_names(path: str, ordinals: list[int]) -> str:
    """Delete selected video-track names in place and return warning text."""
    command = ["mkvpropedit", path]
    for ordinal in ordinals:
        command += ["--edit", f"track:v{ordinal}", "--delete", "name"]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode > 1:
        detail = result.stderr.strip() or "mkvpropedit failed"
        raise RuntimeError(detail)
    return result.stderr.strip() if result.returncode == 1 else ""


def iter_mkv_files(roots: list[str]) -> Iterator[str]:
    for base_path in roots:
        base_path = converter.normalize_path(base_path)
        if not os.path.isdir(base_path):
            print(f"WARNING: skipping unavailable directory: {base_path}", file=sys.stderr)
            continue

        for root, dirs, files in os.walk(base_path, topdown=True):
            dirs[:] = [name for name in dirs if name not in converter.exclude]
            for filename in files:
                if filename.lower().endswith(".mkv"):
                    yield converter.normalize_path(os.path.join(root, filename))


def reconcile_existing_row(
    conn: sqlite3.Connection,
    path: str,
) -> None:
    """Reconcile a pre-edit rename without marking unknown media as converted."""
    signature = converter.file_signature(path)
    row = converter.db_lookup(conn, path, *signature)
    if row is None:
        converter.db_reconcile_rename(conn, path, *signature)


def refresh_database_signature(
    conn: sqlite3.Connection,
    path: str,
) -> bool:
    """Refresh an existing DB row and preserve its current status and note."""
    size, mtime, device, inode = converter.file_signature(path)
    with conn:
        updated = conn.execute(
            """
            UPDATE files
            SET size=?, mtime=?, last_checked=?, device=?, inode=?
            WHERE path=?
            """,
            (size, mtime, time.time(), device, inode, path),
        ).rowcount
    return updated == 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove all MKV video-track names in place and refresh existing "
            "media_convert.db signatures so files are not re-encoded."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report files with video-track names without modifying them",
    )
    parser.add_argument(
        "--database",
        default=converter.DB_PATH,
        help=f"converter database path (default: {converter.DB_PATH})",
    )
    parser.add_argument(
        "--lock",
        default=converter.LOCK_PATH,
        help=f"converter lock path (default: {converter.LOCK_PATH})",
    )
    parser.add_argument(
        "roots",
        nargs="*",
        default=converter.watched_folders,
        help="library roots (default: converter watched folders)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    required_commands = ["mkvmerge"]
    if not args.dry_run:
        required_commands.append("mkvpropedit")
    missing = [name for name in required_commands if shutil.which(name) is None]
    if missing:
        print(
            f"ERROR: missing required command(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2

    os.makedirs(os.path.dirname(os.path.abspath(args.lock)), exist_ok=True)
    try:
        lock_handle = converter.acquire_process_lock(args.lock)
    except BlockingIOError:
        print(
            f"ERROR: media converter is running and holds {args.lock}",
            file=sys.stderr,
        )
        return 2

    conn = None
    scanned = 0
    changed = 0
    unchanged = 0
    failed = 0
    db_rows_refreshed = 0

    try:
        if not args.dry_run:
            conn = converter.db_connect(args.database)

        for path in iter_mkv_files(args.roots):
            scanned += 1
            try:
                ordinals = video_tracks_with_names(path)
                if not ordinals:
                    unchanged += 1
                    continue

                tracks = ", ".join(f"v{ordinal}" for ordinal in ordinals)
                if args.dry_run:
                    print(f"Would clear video title(s) {tracks}: {path}")
                    continue

                reconcile_existing_row(conn, path)
                warning = remove_video_track_names(path, ordinals)
                if warning:
                    print(f"WARNING: {path}: {warning}", file=sys.stderr)

                if refresh_database_signature(conn, path):
                    db_rows_refreshed += 1
                changed += 1
                print(f"Cleared video title(s) {tracks}: {path}")
            except Exception as error:
                failed += 1
                print(f"ERROR: {path}: {error}", file=sys.stderr)

        action = "would_change" if args.dry_run else "changed"
        print(
            f"Summary: scanned={scanned} {action}={changed if not args.dry_run else scanned - unchanged - failed} "
            f"unchanged={unchanged} db_rows_refreshed={db_rows_refreshed} "
            f"failed={failed}"
        )
        return 1 if failed else 0
    finally:
        if conn is not None:
            conn.close()
        lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
