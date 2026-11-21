#!/usr/bin/env python3

"""
Conversion policy and standalone library scanner (SPDX-License-Identifier: MIT, see LICENSE).

`media_converterr.py` imports the policy from here (stream selection, HDR handling, ffmpeg command building).
Run on its own, it scans the watched folders and re-encodes every candidate not marked 'ok' in its SQLite database:
- Video: AV1 (Intel QSV via VAAPI interop). Copies selected audio, converts selected text subtitles, skips other
  subtitle formats.
- Preserves HDR10/HLG, converts Dolby Vision profile 5 to HDR10, and uses HDR10-compatible base layers from
  supported Dolby Vision profiles.
- Logs to the system log directory with rotation; falls back to the work directory.

DB schema (auto-created):
  files(path PRIMARY KEY, size INTEGER, mtime REAL, sha256 TEXT, last_checked REAL, status TEXT, note TEXT)
Status values: 'ok' (converted), 'error' (failed last try)

Requirements: Python 3, ffmpeg with VAAPI+QSV; libplacebo+Vulkan for Dolby Vision profile 5.
Standalone settings: MEDIA_CONVERT_WORK_DIR (default /data) and MEDIA_CONVERT_FOLDERS (colon-separated,
default /media/movies:/media/series).
"""

from __future__ import annotations
import os
import sys
import time
import logging
import logging.handlers as lh
import sqlite3
import subprocess
import signal
import shutil
import json
import re
import fcntl
import shlex
from dataclasses import dataclass
from typing import Optional, Tuple

import encoders

#######################################################################
#                            Configuration                             #
#######################################################################

# Output container and temp path
EXT = "mkv"  # container stays MKV
work_dir = os.environ.get("MEDIA_CONVERT_WORK_DIR", "/data")
DB_PATH = os.path.join(work_dir, "media_convert.db")
LOCK_PATH = os.path.join(work_dir, "media_convert.lock")

# Logging configuration
LOG_DIR = "/var/log/media-convert"  # fallback to work_dir if not writable
LOG_FILE = "media-convert.log"
LOG_LEVEL = logging.INFO
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
LOG_BACKUP_COUNT = 5

# Watched folders and convertible extensions
watched_folders = os.environ.get("MEDIA_CONVERT_FOLDERS", "/media/movies:/media/series").split(":")
exclude = []
convertible_extensions = ["rmvb", "mkv", "avi", "mov", "wmv", "m4v", "mp4", "m2ts", "mts", "ts", "mpg", "mpeg", "vob", "webm", "flv", "3gp"]

# Transcode policy
TARGET_WIDTH = 3840
TARGET_HEIGHT = 2160

# Encoder controls (internal options only)
PRESET = "slow"       # maps to av1_qsv -preset
GQ = 20               # maps to av1_qsv -global_quality
HEVC_GQ = 16          # maps to hevc_qsv -global_quality (HDR is encoded to HEVC); 16 matches av1_qsv at GQ 20

# Behavior flags
DELETE = True         # delete source after successful encode
JUST_CHECK = False    # if True, only log the ffmpeg commands

# Stream language filters. Empty lists keep every tagged language.
# Untagged/undefined streams are always kept. Regional tags such as en-US also match en.
# Set them with MEDIA_CONVERT_AUDIO_LANGUAGES / MEDIA_CONVERT_SUBTITLE_LANGUAGES (comma-separated, e.g. "en,eng,fr,fra")
# or in media_convert.local.toml next to this script:  audio_languages = ["en", "eng"]  subtitle_languages = [...]
LOCAL_SETTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media_convert.local.toml")


def _language_setting(env_name: str, key: str, local_path: str = LOCAL_SETTINGS) -> list[str]:
    value = os.environ.get(env_name)
    if value is not None:
        return [item.strip() for item in value.split(",") if item.strip()]
    try:
        import tomllib
        with open(local_path, "rb") as handle:
            items = tomllib.load(handle).get(key, [])
        return [str(item).strip() for item in items if str(item).strip()]
    except (OSError, ValueError):
        return []


wanted_audio_languages: list[str] = _language_setting("MEDIA_CONVERT_AUDIO_LANGUAGES", "audio_languages")
wanted_subtitle_languages: list[str] = _language_setting("MEDIA_CONVERT_SUBTITLE_LANGUAGES", "subtitle_languages")
_ENV_AUDIO_LANGUAGES = tuple(wanted_audio_languages)
_ENV_SUBTITLE_LANGUAGES = tuple(wanted_subtitle_languages)

# Output codecs: SDR output "av1", "hevc" or "h264"; HDR output "hevc" or "av1" (HDR10+ and Dolby Vision output need
# HEVC, which they then use whatever this says). When no GPU encodes the codec: "gpu" uses another codec on a GPU,
# "cpu" keeps the codec on the CPU.
SDR_CODEC = "av1"
HDR_CODEC = "hevc"
CODEC_FALLBACK = "gpu"
# Output per kind of source (see output_spec): what HDR10/HDR10+, HLG, Dolby Vision 7/8 and Dolby Vision 5 sources
# become. "hdr10plus": HDR10, with HDR10+ when the source has it; "dv81": Dolby Vision profile 8.1; "sdr": tone mapped.
OUTPUT_HDR10 = "hdr10plus"
OUTPUT_HLG = "hlg"
OUTPUT_DV = "dv81"  # default: every source keeps its HDR format (user decision)
OUTPUT_DV5 = "hdr10"
# Tone mapping to SDR: "quality" (libplacebo on Vulkan: keeps shadow detail; about 0.2x real time at 4K on Intel Arc)
# or "fast" (the GPU's video-processing tone mapping, Intel/AMD via VAAPI: about 4x real time, crushes dark scenes).
TONEMAP = "quality"
# Subtitles in the wanted languages: "extract" (sidecar files next to the video, Bazarr naming) or "embed" (kept in the
# container). Bitmap subtitles (Blu-ray PGS, DVD VobSub) when extracting: "ocr" (to SRT with Tesseract), "sidecar"
# (the stream as is: .sup, or .idx with .sub) or "drop".
SUBTITLE_MODE = "extract"
BITMAP_SUBTITLES = "ocr"
KEYFRAME_SECONDS = 2   # one keyframe at least this often (seeking jumps to keyframes)
LOOKAHEAD = 100        # frames the encoder looks ahead (QSV look_ahead_depth; others their own equivalent)
DEINTERLACE = True     # deinterlace sources flagged interlaced (DVD, older TV), one frame per frame
# European Portuguese subtitles ("pt"): "always", "fallback" (only for videos without a Brazilian one) or "never"
EUROPEAN_PT = "always"
CROP = False           # crop black bars (never for Dolby Vision output: its metadata describes the full frame)
OUTPUT_CHOICES = {"output_hdr10": ("hdr10plus", "hdr10", "sdr"), "output_hlg": ("hlg", "sdr"),
                  "output_dv": ("dv81", "hdr10plus", "hdr10", "sdr"), "output_dv5": ("hdr10", "sdr")}
# Encoder backend ("auto", "qsv", "software"; see encoders.py) and the stored capability check that "auto" uses.
BACKEND = "auto"
CAPABILITIES: Optional[dict] = None
# CPU encoder quality (CRF, lower is better and larger). Provisional until calibrated against the GPU values.
SOFTWARE_AV1_CRF = 25
SOFTWARE_HEVC_CRF = 18
H264_GQ = 21              # GPU H.264 quality (provisional)
SOFTWARE_H264_CRF = 19
# Keep efficient low-bitrate sources by remuxing instead of encoding (see remux_reason).
REMUX_ENABLED = True

QSV_PRESETS = ("veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
MAX_RESOLUTIONS = {2160: (3840, 2160), 1440: (2560, 1440), 1080: (1920, 1080), 720: (1280, 720)}
LANGUAGE_CODE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")


def conversion_defaults() -> dict:
    """Built-in conversion policy (languages from the environment or media_convert.local.toml)."""
    return {"backend": "auto", "sdr_codec": "av1", "hdr_codec": "hevc", "codec_fallback": "gpu",
            "output_hdr10": "hdr10plus", "output_hlg": "hlg", "output_dv": "dv81", "output_dv5": "hdr10",
            "tonemap": "quality", "subtitle_mode": "extract", "bitmap_subtitles": "ocr",
            "keyframe_seconds": 2, "lookahead": 100, "deinterlace": True, "crop": False,
            "european_portuguese": "always",
            "av1_quality": 20, "hevc_quality": 16, "h264_quality": 21, "software_av1_crf": 25,
            "software_hevc_crf": 18, "software_h264_crf": 19, "preset": "slow", "max_height": 2160,
            "remux_enabled": True, "remux_mbps_per_mpx": 1.8,
            "audio_languages": list(_ENV_AUDIO_LANGUAGES), "subtitle_languages": list(_ENV_SUBTITLE_LANGUAGES)}


def validate_conversion_settings(values: dict) -> dict:
    """Complete and check a conversion settings document (stored from the web interface); raises ValueError."""
    merged = {**conversion_defaults(), **{k: v for k, v in (values or {}).items() if v is not None}}
    if merged["backend"] not in ("auto", *encoders.conversion_backends()):
        raise ValueError(f"encoder must be auto or one of {', '.join(encoders.conversion_backends())}")
    for key, choices in (("sdr_codec", ("av1", "hevc", "h264")), ("hdr_codec", ("hevc", "av1")),
                         ("codec_fallback", ("gpu", "cpu")), ("tonemap", ("quality", "fast")),
                         ("subtitle_mode", ("extract", "embed")), ("bitmap_subtitles", ("ocr", "sidecar", "drop")),
                         ("european_portuguese", ("always", "fallback", "never")),
                         *OUTPUT_CHOICES.items()):
        if merged[key] not in choices:
            raise ValueError(f"{key.replace('_', ' ')} must be one of {', '.join(choices)}")
    for key in ("software_av1_crf", "software_hevc_crf", "software_h264_crf"):
        merged[key] = int(merged[key])
        if not 0 <= merged[key] <= 51:
            raise ValueError(f"{key.replace('_', ' ')} must be between 0 and 51")
    for key in ("av1_quality", "hevc_quality", "h264_quality"):
        merged[key] = int(merged[key])
        if not 1 <= merged[key] <= 51:
            raise ValueError(f"{key.replace('_', ' ')} must be between 1 and 51")
    if merged["preset"] not in QSV_PRESETS:
        raise ValueError(f"preset must be one of {', '.join(QSV_PRESETS)}")
    merged["max_height"] = int(merged["max_height"])
    if merged["max_height"] not in MAX_RESOLUTIONS:
        raise ValueError("maximum resolution must be 2160, 1440, 1080 or 720")
    merged["remux_enabled"] = bool(merged["remux_enabled"])
    merged["deinterlace"], merged["crop"] = bool(merged["deinterlace"]), bool(merged["crop"])
    merged["keyframe_seconds"], merged["lookahead"] = int(merged["keyframe_seconds"]), int(merged["lookahead"])
    if not 1 <= merged["keyframe_seconds"] <= 20:
        raise ValueError("keyframe interval must be between 1 and 20 seconds")
    if not 0 <= merged["lookahead"] <= 250:
        raise ValueError("look-ahead must be between 0 and 250 frames")
    merged.pop("remux_mbps", None)  # per-resolution limits of an earlier draft
    merged["remux_mbps_per_mpx"] = float(merged["remux_mbps_per_mpx"])
    if not 0 <= merged["remux_mbps_per_mpx"] <= 50:
        raise ValueError("the remux limit must be between 0 and 50 Mbit/s per megapixel")
    for key in ("audio_languages", "subtitle_languages"):
        items = [str(item).strip() for item in merged[key] if str(item).strip()]
        bad = [item for item in items if not LANGUAGE_CODE.match(item)]
        if bad:
            raise ValueError(f"not a language code: {', '.join(bad)}")
        merged[key] = items
    return merged


def apply_capabilities(capabilities: Optional[dict]) -> None:
    """The stored capability check: encode_plan picks backend, device, codec and bit depth from it."""
    global CAPABILITIES
    CAPABILITIES = capabilities


def apply_conversion_settings(values: Optional[dict]) -> dict:
    """Set the policy globals from a stored document (missing keys fall back to the defaults); returns it complete."""
    global SDR_CODEC, GQ, HEVC_GQ, PRESET, TARGET_WIDTH, TARGET_HEIGHT, REMUX_ENABLED, REMUX_MBPS_PER_MPX
    global BACKEND, SOFTWARE_AV1_CRF, SOFTWARE_HEVC_CRF, HDR_CODEC, CODEC_FALLBACK, H264_GQ, SOFTWARE_H264_CRF
    global OUTPUT_HDR10, OUTPUT_HLG, OUTPUT_DV, OUTPUT_DV5, TONEMAP, SUBTITLE_MODE, BITMAP_SUBTITLES
    global KEYFRAME_SECONDS, LOOKAHEAD, DEINTERLACE, CROP, EUROPEAN_PT
    global wanted_audio_languages, wanted_subtitle_languages
    merged = validate_conversion_settings(values or {})
    SDR_CODEC, GQ, HEVC_GQ, PRESET = merged["sdr_codec"], merged["av1_quality"], merged["hevc_quality"], merged["preset"]
    TARGET_WIDTH, TARGET_HEIGHT = MAX_RESOLUTIONS[merged["max_height"]]
    REMUX_ENABLED = merged["remux_enabled"]
    REMUX_MBPS_PER_MPX = merged["remux_mbps_per_mpx"]
    BACKEND, SOFTWARE_AV1_CRF, SOFTWARE_HEVC_CRF = (merged["backend"], merged["software_av1_crf"],
                                                    merged["software_hevc_crf"])
    HDR_CODEC, CODEC_FALLBACK = merged["hdr_codec"], merged["codec_fallback"]
    H264_GQ, SOFTWARE_H264_CRF = merged["h264_quality"], merged["software_h264_crf"]
    OUTPUT_HDR10, OUTPUT_HLG, OUTPUT_DV, OUTPUT_DV5 = (merged["output_hdr10"], merged["output_hlg"],
                                                       merged["output_dv"], merged["output_dv5"])
    TONEMAP = merged["tonemap"]
    SUBTITLE_MODE, BITMAP_SUBTITLES = merged["subtitle_mode"], merged["bitmap_subtitles"]
    KEYFRAME_SECONDS, LOOKAHEAD = merged["keyframe_seconds"], merged["lookahead"]
    DEINTERLACE, CROP = merged["deinterlace"], merged["crop"]
    EUROPEAN_PT = merged["european_portuguese"]
    wanted_audio_languages = list(merged["audio_languages"])
    wanted_subtitle_languages = list(merged["subtitle_languages"])
    return merged

# List of supported subtitles
TEXT_SUBTITLE_CODECS = {
    "subrip",
    "srt",
    "ass",
    "ssa",
    "mov_text",
    "webvtt",
    "text",
    "ttml",
}

#######################################################################
#                         Utilities / Logging                          #
#######################################################################

def setup_logger() -> logging.Logger:
    # Attempt system log dir first
    log_path = None
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, LOG_FILE)
        handler = lh.RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    except Exception:
        # Fallback to work_dir
        fallback_dir = os.path.join(work_dir, "logs")
        os.makedirs(fallback_dir, exist_ok=True)
        log_path = os.path.join(fallback_dir, LOG_FILE)
        handler = lh.RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)

    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    handler.setFormatter(fmt)

    logger = logging.getLogger(__name__)
    logger.setLevel(LOG_LEVEL)
    # Avoid duplicate handlers on re-run
    logger.handlers.clear()
    logger.addHandler(handler)

    # Also log to stderr for interactive runs at INFO+
    sh = logging.StreamHandler()
    sh.setLevel(LOG_LEVEL)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.debug(f"Logger initialized at {log_path}")
    return logger


def normalize_path(path: str) -> str:
    return path.replace('\\', '/')


def to_target_naming(filename: str) -> str:
    parts = filename.split('.')
    parts[-1] = EXT
    return '.'.join(parts)


def file_signature(path: str) -> Tuple[int, float, int, int]:
    st = os.stat(path)
    return st.st_size, st.st_mtime, st.st_dev, st.st_ino


def temporary_path(candidate_index: int) -> str:
    return os.path.join(work_dir, f"temp.{os.getpid()}.{candidate_index}.{EXT}")


def command_text(cmd: list[str]) -> str:
    return shlex.join(cmd)


def unique_sidecar_path(path: str, suffix: str) -> str:
    candidate = path + suffix
    while os.path.lexists(candidate):
        candidate += suffix
    return candidate


def ensure_output_path_available(input_path: str, output_path: str) -> None:
    if os.path.abspath(input_path) != os.path.abspath(output_path) and os.path.lexists(output_path):
        raise FileExistsError(f"Refusing to overwrite existing output: {output_path}")


def install_output(input_path: str, temp_path: str, output_path: str) -> str:
    ensure_output_path_available(input_path, output_path)
    backup_path = unique_sidecar_path(input_path, ".old")
    shutil.move(input_path, backup_path)

    try:
        shutil.move(temp_path, output_path)
    except Exception as move_error:
        failed_output_path = None
        if os.path.lexists(output_path):
            failed_output_path = unique_sidecar_path(output_path, ".failed")
            try:
                shutil.move(output_path, failed_output_path)
            except Exception as preserve_error:
                raise RuntimeError(
                    f"Output installation failed and the partial output could not be moved; "
                    f"original remains at {backup_path}: {preserve_error}"
                ) from move_error

        try:
            shutil.move(backup_path, input_path)
        except Exception as restore_error:
            partial_note = (
                f"; partial output retained at {failed_output_path}"
                if failed_output_path else ""
            )
            raise RuntimeError(
                f"Output installation failed and original restoration failed; "
                f"original remains at {backup_path}{partial_note}: {restore_error}"
            ) from move_error

        partial_note = (
            f"; partial output retained at {failed_output_path}"
            if failed_output_path else ""
        )
        raise RuntimeError(
            f"Output installation failed; original restored{partial_note}: {move_error}"
        ) from move_error

    return backup_path


_active_process: Optional[subprocess.Popen] = None


def run(cmd: list[str], cwd: str, logger: logging.Logger) -> tuple[int, str]:
    global _active_process

    try:
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as e:
        return 127, str(e)

    _active_process = process
    try:
        stdout, stderr = process.communicate()
    finally:
        _active_process = None

    for line in (stdout or "").splitlines():
        logger.debug(line)
    for line in (stderr or "").splitlines():
        if process.returncode:
            logger.error(line)
        else:
            logger.debug(line)

    return process.returncode, (stderr or "") + (stdout or "")


def acquire_process_lock(lock_path: str):
    lock_handle = open(lock_path, "a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        lock_handle.close()
        raise
    return lock_handle


def cleanup_temp(path: str, logger: logging.Logger) -> None:
    if not os.path.lexists(path):
        return
    try:
        os.remove(path)
        logger.debug(f"Temporary file removed: {path}")
    except Exception as e:
        logger.warning(f"Temporary file cleanup failed for {path}: {e}")


#######################################################################
#                               Database                                #
#######################################################################

def db_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            size INTEGER,
            mtime REAL,
            sha256 TEXT,
            last_checked REAL,
            status TEXT,
            note TEXT,
            device INTEGER,
            inode INTEGER
        )
        """
    )

    # Existing databases predate filesystem identity tracking. SQLite's
    # CREATE TABLE IF NOT EXISTS does not add new columns, so migrate them
    # independently and populate them lazily as files are encountered.
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()
    }
    if "device" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN device INTEGER")
    if "inode" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN inode INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS files_identity ON files(device, inode)"
    )
    conn.commit()
    return conn


def db_lookup(
    conn: sqlite3.Connection,
    path: str,
    size: int,
    mtime: float,
    device: int,
    inode: int,
) -> Optional[Tuple]:
    cur = conn.execute(
        """
        SELECT path, size, mtime, sha256, last_checked, status, note,
               device, inode
        FROM files
        WHERE path=?
        """,
        (path,),
    )
    row = cur.fetchone()
    if not row or row[1] != size or row[2] != mtime or row[5] != 'ok':
        return None

    stored_identity = (row[7], row[8])
    current_identity = (device, inode)
    if stored_identity != current_identity:
        # Device and inode values can change when a filesystem is remounted.
        # The unchanged path/size/mtime tuple remains authoritative for a file
        # already marked ok; refresh identity only for later rename matching.
        with conn:
            conn.execute(
                "UPDATE files SET device=?, inode=? WHERE path=?",
                (device, inode, path),
            )
        return row[:7] + current_identity

    return row


def db_reconcile_rename(
    conn: sqlite3.Connection,
    path: str,
    size: int,
    mtime: float,
    device: int,
    inode: int,
) -> Optional[str]:
    # A row already stored at this path represents a modification or
    # replacement, not a rename that needs path reconciliation.
    if conn.execute("SELECT 1 FROM files WHERE path=?", (path,)).fetchone():
        return None

    rows = conn.execute(
        "SELECT path FROM files WHERE device=? AND inode=? AND path<>?",
        (device, inode, path),
    ).fetchall()
    identity_candidates = [
        row[0]
        for row in rows
        if not os.path.lexists(row[0])
    ]
    if len(identity_candidates) > 1:
        return None

    candidates = identity_candidates
    if not candidates:
        # Some pooled and overlay filesystems derive inode values from paths,
        # so a rename can change the exposed identity. Accept one exact size and
        # mtime match whose previous path is gone. Ambiguous matches deliberately
        # fall through to encode.
        legacy_rows = conn.execute(
            """
            SELECT path
            FROM files
            WHERE size=?
              AND mtime=?
              AND status='ok'
              AND path<>?
            """,
            (size, mtime, path),
        ).fetchall()
        candidates = [
            row[0]
            for row in legacy_rows
            if not os.path.lexists(row[0])
        ]
    if len(candidates) != 1:
        return None

    old_path = candidates[0]
    with conn:
        updated = conn.execute(
            """
            UPDATE files
            SET path=?, last_checked=?, device=?, inode=?
            WHERE path=?
            """,
            (path, time.time(), device, inode, old_path),
        ).rowcount
        if updated != 1:
            raise RuntimeError(f"Could not reconcile renamed DB path: {old_path}")
    return old_path


def db_upsert(
    conn: sqlite3.Connection,
    path: str,
    size: int,
    mtime: float,
    status: str,
    note: str = None,
    device: Optional[int] = None,
    inode: Optional[int] = None,
) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT INTO files(
            path, size, mtime, sha256, last_checked, status, note, device, inode
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
            size=excluded.size,
            mtime=excluded.mtime,
            last_checked=excluded.last_checked,
            status=excluded.status,
            note=excluded.note,
            device=COALESCE(excluded.device, files.device),
            inode=COALESCE(excluded.inode, files.inode)
        """,
        (path, size, mtime, None, now, status, note, device, inode),
    )
    conn.commit()


def db_remove_orphans(
    conn: sqlite3.Connection,
    scanned_folders: list[str],
    paths_seen_at_scan: Optional[set[str]] = None,
) -> list[str]:
    roots = [os.path.abspath(path) for path in scanned_folders]
    initial_paths = {
        os.path.abspath(path) for path in (paths_seen_at_scan or set())
    }
    orphaned = []
    for path, stored_device in conn.execute(
        "SELECT path, device FROM files"
    ).fetchall():
        absolute_path = os.path.abspath(path)
        matching_root = None
        for root in roots:
            try:
                if os.path.commonpath((absolute_path, root)) == root:
                    matching_root = root
                    break
            except ValueError:
                continue

        if matching_root is None or os.path.exists(path):
            continue

        # A path that existed in the initial scan but disappeared during this
        # run may have been renamed. Retain its row for reconciliation during
        # the next scan; a genuinely deleted path is removed one run later.
        if absolute_path in initial_paths:
            continue

        # If the row has filesystem identity, require the closest existing
        # ancestor to still be on that device. This avoids deleting an entire
        # library's records when its mount is already absent at scan start.
        if stored_device is not None:
            ancestor = os.path.dirname(absolute_path)
            ancestor_device = None
            while True:
                try:
                    is_under_root = (
                        os.path.commonpath((ancestor, matching_root))
                        == matching_root
                    )
                    if not is_under_root:
                        break
                    ancestor_device = os.stat(ancestor).st_dev
                    break
                except (FileNotFoundError, NotADirectoryError):
                    parent = os.path.dirname(ancestor)
                    if parent == ancestor:
                        break
                    ancestor = parent
                except (OSError, ValueError):
                    break
            if ancestor_device != stored_device:
                continue

        orphaned.append(path)

    if orphaned:
        conn.executemany(
            "DELETE FROM files WHERE path=?",
            ((path,) for path in orphaned),
        )
        conn.commit()
    return orphaned


#######################################################################
#                         HDR / Dolby Vision probe                    #
#######################################################################

HDR_SDR = "sdr"
HDR10_PQ = "hdr10-pq"
HDR_HLG = "hlg"
DOVI_PROFILE_5 = "dovi-profile-5"


class UnsupportedVideoError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoInfo:
    color_transfer: str
    color_primaries: str
    color_space: str
    color_range: str
    codec_name: str = ""
    dovi_profile: Optional[int] = None
    dovi_compatibility_id: Optional[int] = None
    dovi_el_present: bool = False
    dovi_bl_present: bool = False
    hdr10_plus: bool = False
    has_mastering_display: bool = False
    has_content_light_level: bool = False
    width: int = 0
    height: int = 0
    field_order: str = ""  # ffprobe: progressive, tt, bb, tb, bt (interlaced) or unknown/empty


# H.273 values shared by FFmpeg and AV1. Unknown values are resolved separately;
# a recognized source value must never silently become BT.709.
COLOR_VALUES = {
    "color_primaries": {
        "bt709": 1, "bt470m": 4, "bt470bg": 5, "smpte170m": 6,
        "smpte240m": 7, "film": 8, "bt2020": 9, "smpte428": 10,
        "smpte431": 11, "smpte432": 12, "jedec-p22": 22,
    },
    "color_transfer": {
        "bt709": 1, "gamma22": 4, "gamma28": 5, "smpte170m": 6,
        "smpte240m": 7, "linear": 8, "log100": 9, "log316": 10,
        "iec61966-2-4": 11, "bt1361e": 12, "iec61966-2-1": 13,
        "bt2020-10": 14, "bt2020-12": 15, "smpte2084": 16,
        "smpte428": 17, "arib-std-b67": 18,
    },
    "color_space": {
        "gbr": 0, "bt709": 1, "fcc": 4, "bt470bg": 5, "smpte170m": 6,
        "smpte240m": 7, "ycgco": 8, "bt2020nc": 9, "bt2020c": 10,
        "smpte2085": 11, "chroma-derived-nc": 12, "chroma-derived-c": 13,
        "ictcp": 14, "ipt-c2": 15, "ycgco-re": 16, "ycgco-ro": 17,
    },
    "color_range": {"tv": 0, "pc": 1},
}


def normalize_color_value(field: str, value: object) -> str:
    value = str(value or "").strip().lower()
    if value in {"", "unknown", "unspecified", "undef", "reserved", "n/a"}:
        return ""
    aliases = {
        "color_primaries": {"ebu3213": "jedec-p22"},
        "color_transfer": {"bt470m": "gamma22", "bt470bg": "gamma28"},
        "color_space": {"rgb": "gbr", "ycocg": "ycgco"},
        "color_range": {
            "limited": "tv", "mpeg": "tv", "full": "pc", "jpeg": "pc",
        },
    }
    return aliases.get(field, {}).get(value, value)


def probe_color_properties(stream: dict, frames: list[dict], path: str) -> dict[str, str]:
    colors = {}
    for field in COLOR_VALUES:
        stream_value = normalize_color_value(field, stream.get(field))
        frame_values = {
            normalize_color_value(field, frame.get(field)) for frame in frames
        } - {""}
        if len(frame_values) > 1:
            raise UnsupportedVideoError(
                f"Changing {field} in sampled frames: {sorted(frame_values)} path={path}"
            )
        frame_value = next(iter(frame_values), "")
        if frame_value and stream_value and frame_value != stream_value:
            logging.getLogger(__name__).warning(
                "Color metadata disagreement: %s stream=%s frame=%s; "
                "using decoded frame value path=%s",
                field, stream_value, frame_value, path,
            )
        colors[field] = frame_value or stream_value
    return colors


def resolve_color_properties(info: VideoInfo, mode: Optional[str] = None) -> dict[str, str]:
    mode = mode or hdr_mode(info)
    defaults = {
        "color_primaries": "bt709", "color_transfer": "bt709",
        "color_space": "bt709", "color_range": "tv",
    }
    if mode in {HDR10_PQ, DOVI_PROFILE_5, HDR_HLG}:
        defaults.update(
            color_primaries="bt2020", color_space="bt2020nc",
            color_transfer="arib-std-b67" if mode == HDR_HLG else "smpte2084",
        )
    elif 0 < info.width < 1280 and info.height in {480, 486, 576}:
        # A source-resolution heuristic, used only for missing fields.
        standard = "bt470bg" if info.height == 576 else "smpte170m"
        defaults.update(
            color_primaries=standard, color_space=standard,
            color_transfer="smpte170m",
        )

    # Profile 5 is actually rendered into HDR10 by libplacebo. Its source
    # properties describe the Dolby Vision input, not the rendered output.
    if mode == DOVI_PROFILE_5:
        return defaults

    colors = {}
    for field, supported in COLOR_VALUES.items():
        value = normalize_color_value(field, getattr(info, field)) or defaults[field]
        if value not in supported:
            raise UnsupportedVideoError(f"Cannot preserve {field}={value!r} in AV1")
        colors[field] = value
    if colors["color_space"] == "gbr":
        raise UnsupportedVideoError("Cannot preserve an RGB matrix in the P010/YUV420 pipeline")
    return colors


def optional_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def probe_video_info(inp: str) -> VideoInfo:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-read_intervals", "%+#60",
        "-show_streams",
        "-show_frames",
        "-of", "json",
        inp,
    ]
    p = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {(p.stderr or '').strip()}")

    try:
        probe = json.loads(p.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid ffprobe JSON: {e}") from e

    streams = probe.get("streams", [])
    if not streams:
        raise RuntimeError("No video stream found")

    stream = streams[0]
    stream_side_data = stream.get("side_data_list", [])
    frame_side_data = [
        side_data
        for frame in probe.get("frames", [])
        for side_data in frame.get("side_data_list", [])
    ]
    all_side_data = stream_side_data + frame_side_data
    side_data_names = [
        str(side_data.get("side_data_type", "")).lower()
        for side_data in all_side_data
    ]
    dovi = next(
        (
            side_data for side_data in stream_side_data
            if "dovi configuration record" in side_data.get("side_data_type", "").lower()
        ),
        None,
    )

    return VideoInfo(
        **probe_color_properties(stream, probe.get("frames", []), inp),
        width=optional_int(stream.get("width")) or 0,
        height=optional_int(stream.get("height")) or 0,
        field_order=str(stream.get("field_order") or "").lower(),
        codec_name=str(stream.get("codec_name", "")).lower(),
        dovi_profile=optional_int(dovi.get("dv_profile")) if dovi else None,
        dovi_compatibility_id=(
            optional_int(dovi.get("dv_bl_signal_compatibility_id")) if dovi else None
        ),
        dovi_el_present=bool(optional_int(dovi.get("el_present_flag"))) if dovi else False,
        dovi_bl_present=bool(optional_int(dovi.get("bl_present_flag"))) if dovi else False,
        hdr10_plus=any(
            "hdr10+" in name or "smpte2094-40" in name
            for name in side_data_names
        ),
        has_mastering_display=any(
            "mastering display metadata" in name for name in side_data_names
        ),
        has_content_light_level=any(
            "content light level metadata" in name for name in side_data_names
        ),
    )


def hdr_mode(info: VideoInfo) -> str:
    if info.dovi_profile == 5:
        if not info.dovi_bl_present:
            raise UnsupportedVideoError("Dolby Vision profile 5 has no base layer")
        return DOVI_PROFILE_5

    if info.dovi_profile == 7:
        if not info.dovi_bl_present:
            raise UnsupportedVideoError("Dolby Vision profile 7 has no HDR10 base layer")
        return HDR10_PQ

    if info.dovi_profile == 8:
        if not info.dovi_bl_present:
            raise UnsupportedVideoError("Dolby Vision profile 8 has no base layer")
        if info.dovi_compatibility_id == 1:
            return HDR10_PQ
        raise UnsupportedVideoError(
            "Only HDR10-compatible Dolby Vision profile 8.1 is supported; "
            f"compatibility id is {info.dovi_compatibility_id!r}"
        )

    if info.dovi_profile == 10:
        if info.codec_name != "av1":
            raise UnsupportedVideoError(
                "Dolby Vision profile 10 requires an AV1 base layer; "
                f"codec is {info.codec_name or 'unknown'!r}"
            )
        if not info.dovi_bl_present:
            raise UnsupportedVideoError("Dolby Vision profile 10 has no base layer")
        if info.dovi_compatibility_id == 1:
            return HDR10_PQ
        raise UnsupportedVideoError(
            "Only HDR10-compatible Dolby Vision profile 10.1 is supported; "
            f"compatibility id is {info.dovi_compatibility_id!r}"
        )

    if info.dovi_profile is not None:
        raise UnsupportedVideoError(
            f"Dolby Vision profile {info.dovi_profile} is not supported safely"
        )

    if "smpte2084" in info.color_transfer:
        return HDR10_PQ
    if "arib-std-b67" in info.color_transfer:
        return HDR_HLG
    return HDR_SDR


def describe_video(info: VideoInfo, mode: str) -> str:
    details = [
        f"mode={mode}",
        f"codec={info.codec_name or 'unknown'}",
        f"transfer={info.color_transfer or 'unknown'}",
        f"primaries={info.color_primaries or 'unknown'}",
        f"matrix={info.color_space or 'unknown'}",
        f"range={info.color_range or 'unknown'}",
    ]
    details += [
        f"hdr10_plus={info.hdr10_plus}",
        f"mastering_display={info.has_mastering_display}",
        f"content_light_level={info.has_content_light_level}",
    ]
    if info.dovi_profile is not None:
        details += [
            f"dovi_profile={info.dovi_profile}",
            f"dovi_compatibility_id={info.dovi_compatibility_id}",
            f"dovi_el_present={info.dovi_el_present}",
        ]
    return " ".join(details)


def validate_output(
    path: str, mode: str, source_info: Optional[VideoInfo] = None, expected_codec: Optional[str] = None,
) -> VideoInfo:
    remux_check = expected_codec is not None  # a remux keeps the source's video as it is
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        raise RuntimeError(f"Encoded output is missing or empty: {path}")

    info = probe_video_info(path)
    expected_codec = expected_codec or output_codec(mode, source_info)
    if info.codec_name != expected_codec:
        raise RuntimeError(
            f"Encoded output video codec is {info.codec_name or 'unknown'}, expected {expected_codec.upper()}"
        )

    if source_info is not None:
        expected = output_colors(source_info, mode) if not remux_check else resolve_color_properties(source_info, mode)
        mismatches = [
            f"{field}={getattr(info, field)!r} expected={value!r}"
            for field, value in expected.items()
            if normalize_color_value(field, getattr(info, field)) != value
        ]
        if mismatches:
            raise RuntimeError(
                "Encoded output has incorrect color signaling: " + "; ".join(mismatches)
            )
        return info

    # Compatibility for callers that do not provide source properties.
    if mode in {HDR10_PQ, DOVI_PROFILE_5}:
        if (
            info.color_transfer != "smpte2084"
            or info.color_primaries != "bt2020"
            or info.color_space != "bt2020nc"
        ):
            raise RuntimeError(
                "Encoded HDR10 output has incorrect color signaling: "
                f"transfer={info.color_transfer!r}, "
                f"primaries={info.color_primaries!r}, matrix={info.color_space!r}"
            )
    elif mode == HDR_HLG:
        if (
            info.color_transfer != "arib-std-b67"
            or info.color_primaries != "bt2020"
            or info.color_space != "bt2020nc"
        ):
            raise RuntimeError(
                "Encoded HLG output has incorrect color signaling: "
                f"transfer={info.color_transfer!r}, "
                f"primaries={info.color_primaries!r}, matrix={info.color_space!r}"
            )
    return info

#######################################################################
#                Check and prepare subtitle conversion                #
#######################################################################

# Common ISO 639-1, terminology, and bibliographic aliases.
LANGUAGE_ALIAS_GROUPS = (
    {"ar", "ara"}, {"cs", "ces", "cze"}, {"da", "dan"},
    {"de", "deu", "ger"}, {"el", "ell", "gre"}, {"en", "eng"},
    {"es", "spa"}, {"fi", "fin"}, {"fr", "fra", "fre"},
    {"he", "heb"}, {"hi", "hin"}, {"hu", "hun"}, {"id", "ind"},
    {"it", "ita"}, {"ja", "jpn"}, {"ko", "kor"},
    {"nl", "nld", "dut"}, {"no", "nor"}, {"pl", "pol"},
    {"pt", "por", "pob"}, {"ro", "ron", "rum"}, {"ru", "rus"},
    {"sk", "slk", "slo"}, {"sv", "swe"}, {"th", "tha"},
    {"tr", "tur"}, {"uk", "ukr"}, {"vi", "vie"},
    {"zh", "zho", "chi"},
)
UNTAGGED_LANGUAGES = {"", "und", "unk"}


def language_keys(language: Optional[str]) -> set[str]:
    normalized = str(language or "").strip().lower().replace("_", "-")
    primary = normalized.split("-", 1)[0]
    keys = {normalized, primary}
    for aliases in LANGUAGE_ALIAS_GROUPS:
        if keys & aliases:
            keys.update(aliases)
            break
    return keys


def language_is_wanted(language: Optional[str], wanted: list[str]) -> bool:
    stream_keys = language_keys(language)
    if stream_keys & UNTAGGED_LANGUAGES:
        return True
    if not wanted:
        return True
    wanted_keys = set().union(*(language_keys(item) for item in wanted))
    return bool(stream_keys & wanted_keys)


def stream_mapping_args(path: str) -> list[str]:
    probe_cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries",
        "stream=index,codec_name,codec_type:stream_tags=language:"
        "stream_disposition=default,original",
        "-of", "json",
        path,
    ]
    probe = json.loads(subprocess.check_output(probe_cmd))
    logger = logging.getLogger(__name__)

    streams = probe.get("streams", [])
    audio_streams = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]
    original_audio_indices = {
        stream["index"]
        for stream in audio_streams
        if (stream.get("disposition") or {}).get("original") == 1
    }
    wanted_audio_present = any(
        language_is_wanted(
            (stream.get("tags") or {}).get("language"),
            wanted_audio_languages,
        )
        for stream in audio_streams
    )
    if not original_audio_indices and audio_streams and not wanted_audio_present:
        original_audio_indices.add(audio_streams[0]["index"])

    args: list[str] = []
    embedded = 0  # output subtitle stream number, for its codec option

    for stream in streams:
        stream_type = stream.get("codec_type")
        if stream_type not in {"audio", "subtitle"}:
            continue

        index = stream["index"]
        codec = stream.get("codec_name")
        language = (stream.get("tags") or {}).get("language")
        wanted = (
            wanted_audio_languages
            if stream_type == "audio"
            else wanted_subtitle_languages
        )

        language_wanted = language_is_wanted(language, wanted)
        is_original_audio = (
            stream_type == "audio" and index in original_audio_indices
        )
        if not language_wanted and not is_original_audio:
            logger.info(
                f"Dropping unwanted {stream_type} stream={index} "
                f"language={language} codec={codec or 'unknown'} path={path}"
            )
            continue

        if stream_type == "audio":
            if not language_wanted:
                logger.info(
                    f"Keeping original audio outside language list stream={index} "
                    f"language={language} codec={codec or 'unknown'} path={path}"
                )
            args += ["-map", f"0:{index}"]
            continue

        if SUBTITLE_MODE != "embed":
            continue  # extracted instead: subtitle_extractions() writes them next to the output
        if codec not in TEXT_SUBTITLE_CODECS and codec not in BITMAP_KINDS and codec not in ("ass", "ssa"):
            logger.warning(f"Not embedding unsupported subtitle stream={index} codec={codec or 'unknown'} path={path}")
            continue
        args += ["-map", f"0:{index}"]
        # Matroska can't hold MP4 text subtitles: those become SRT; everything else is copied
        args += [f"-c:s:{embedded}", "srt" if codec == "mov_text" else "copy"]
        embedded += 1

    return args


@dataclass
class SubtitleExtraction:
    index: int
    code: str       # Bazarr's language code for the file name: en, nl, pt, pt-BR, ...
    hi: bool = False
    forced: bool = False
    kind: str = "text"  # "text" (written as SRT), "pgs" (copied as .sup) or "vobsub" (copied as .mks); bitmaps are OCR'd

    @property
    def suffix(self) -> str:
        return f".{self.code}" + (".hi" if self.hi else "") + (".forced" if self.forced else "") + ".srt"


BITMAP_KINDS = {"hdmv_pgs_subtitle": "pgs", "dvd_subtitle": "vobsub"}  # OCR'd into SRT


def subtitle_code(language: Optional[str], title: str = "") -> Optional[str]:
    """Bazarr's file-name language code for a stream language tag (None when untagged or unknown)."""
    tag = str(language or "").strip().lower().replace("_", "-")
    if tag in UNTAGGED_LANGUAGES:
        return None
    if tag in ("pob", "pt-br", "por-br") or (language_keys(tag) & {"pt"} and re.search(r"brazil|brasil|\bbr\b", title, re.I)):
        return "pt-BR"
    keys = language_keys(tag)
    two = sorted(k for k in keys if len(k) == 2)
    return two[0] if two else None


def subtitle_extractions(path: str) -> list[SubtitleExtraction]:
    """Subtitle streams in the wanted languages, to be written as sidecar .srt files (Bazarr naming). Text streams
    come first; bitmap streams (Blu-ray PGS, DVD VobSub) follow and are OCR'd, so a text stream wins a file name over
    a bitmap one. Untagged streams (a lone Blu-ray `.m2ts` carries no language tags) come last as `und<index>`: their
    language is read from the text when the subtitles are placed. A second stream with the same file name is
    skipped."""
    if SUBTITLE_MODE == "embed":
        return []  # kept in the container (stream_mapping_args)
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
        "stream=index,codec_name:stream_tags=language,title:stream_disposition=forced,hearing_impaired",
        "-of", "json", path]))
    logger = logging.getLogger(__name__)
    out, names, later = [], set(), []
    streams = probe.get("streams", [])
    streams = ([s for s in streams if s.get("codec_name") in TEXT_SUBTITLE_CODECS]
               + [s for s in streams if s.get("codec_name") not in TEXT_SUBTITLE_CODECS])
    for stream in streams:
        index, codec = stream["index"], stream.get("codec_name")
        tags, disp = stream.get("tags") or {}, stream.get("disposition") or {}
        language, title = tags.get("language"), str(tags.get("title") or "")
        untagged = bool(language_keys(language) & UNTAGGED_LANGUAGES)
        if not untagged and not language_is_wanted(language, wanted_subtitle_languages):
            continue  # unwanted language (logged by stream_mapping_args)
        if codec not in TEXT_SUBTITLE_CODECS and codec not in BITMAP_KINDS:
            logger.warning(f"Skipping unsupported subtitle stream={index} language={language} "
                           f"codec={codec or 'unknown'} path={path}")
            continue
        if codec in BITMAP_KINDS and BITMAP_SUBTITLES == "drop":
            logger.info(f"Dropping bitmap subtitle stream={index} language={language} (setting) path={path}")
            continue
        code = f"und{index}" if untagged else subtitle_code(language, title)  # untagged: named from its text later
        if code is None:
            logger.warning(f"Not extracting subtitle with unknown language stream={index} language={language} path={path}")
            continue
        sub = SubtitleExtraction(index, code,
                                 hi=bool(disp.get("hearing_impaired")) or bool(re.search(r"\bsdh\b|\bcc\b|hearing", title, re.I)),
                                 forced=bool(disp.get("forced")) or "forced" in title.lower(),
                                 kind="text" if codec in TEXT_SUBTITLE_CODECS else BITMAP_KINDS[codec])
        if untagged:
            later.append(sub)
            continue
        if sub.suffix in names:
            logger.info(f"Skipping second subtitle stream={index} for {sub.suffix} path={path}")
            continue
        names.add(sub.suffix)
        out.append(sub)
    return out + later  # untagged last: a tagged stream wins a file name


def learning_extractions(path: str, taken: list[SubtitleExtraction], wants) -> list[SubtitleExtraction]:
    """Tagged text streams of languages the library extraction doesn't already cover, one per language, written in
    the same ffmpeg pass so their text teaches the language profiles (`language_profiles`). `wants(language)` says
    whether a language still needs tracks. Forced tracks are skipped (too little text)."""
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
        "stream=index,codec_name:stream_tags=language,title:stream_disposition=forced", "-of", "json", path]))
    seen = {s.code.split("-")[0] for s in taken if not s.code.startswith("und")}
    out = []
    for stream in probe.get("streams", []):
        tags, disp = stream.get("tags") or {}, stream.get("disposition") or {}
        language, title = tags.get("language"), str(tags.get("title") or "")
        if stream.get("codec_name") not in TEXT_SUBTITLE_CODECS or language_keys(language) & UNTAGGED_LANGUAGES:
            continue
        if disp.get("forced") or "forced" in title.lower():
            continue
        tag = str(language).strip().lower()
        code = subtitle_code(language, title) or (tag if re.fullmatch(r"[a-z]{3}", tag) else None)
        primary = code.split("-")[0] if code else None
        if not primary or primary in seen or not wants(primary):
            continue
        seen.add(primary)
        out.append(SubtitleExtraction(stream["index"], f"learn-{primary}"))
    return out


def subtitle_output_args(extractions: list[SubtitleExtraction], temp_base: str) -> tuple[list[str], dict[str, str]]:
    """Extra ffmpeg outputs (placed after the main output) writing each subtitle to a temporary file: SRT for text
    streams, the bitmap stream copied as .sup for PGS or as a subtitle-only .mks for VobSub (OCR'd afterwards).
    Returns the arguments and {suffix: temporary path}."""
    args, temps = [], {}
    for sub in extractions:
        if sub.kind == "pgs":
            temp = f"{temp_base}{sub.suffix}.sup"
            args += ["-map", f"0:{sub.index}", "-c:s", "copy", "-f", "sup", temp]
        elif sub.kind == "vobsub":  # ffmpeg can't write .idx/.sub; mkvextract makes them from this
            temp = f"{temp_base}{sub.suffix}.mks"
            args += ["-map", f"0:{sub.index}", "-c:s", "copy", "-f", "matroska", temp]
        else:
            temp = f"{temp_base}{sub.suffix}"
            args += ["-map", f"0:{sub.index}", "-c:s", "srt", "-f", "srt", temp]
        temps[sub.suffix] = temp
    return args, temps

#######################################################################
#                        FFmpeg command builder                        #
#######################################################################

def build_cmd(
    input_path: str,
    output_path: str,
    software_fallback: bool = False,
    video_info: Optional[VideoInfo] = None,
) -> list[str]:
    info = video_info or probe_video_info(input_path)
    mode = hdr_mode(info)
    spec = output_spec(info, mode)
    colors = output_colors(info, mode)
    codes = {field: COLOR_VALUES[field][value] for field, value in colors.items()}
    if mode != DOVI_PROFILE_5 and not spec.tonemap:
        assumed = [
            f"{field}={value}" for field, value in colors.items()
            if not normalize_color_value(field, getattr(info, field))
        ]
        if assumed:
            logging.getLogger(__name__).warning(
                "Assuming missing color properties: %s mode=%s source=%sx%s path=%s",
                " ".join(assumed), mode, info.width, info.height, input_path,
            )
    # Numeric values avoid differences between ffprobe and setparams names
    # (for example gamma22 vs bt470m). This changes frame properties only.
    setparams = (
        f"setparams=color_primaries={codes['color_primaries']}:"
        f"color_trc={codes['color_transfer']}:colorspace={codes['color_space']}:"
        f"range={colors['color_range']}"
    )
    plan = encode_plan(mode, info)
    render = (encoders.tonemap_render(setparams, TARGET_WIDTH, TARGET_HEIGHT) if spec.tonemap
              else encoders.dovi_render(setparams, TARGET_WIDTH, TARGET_HEIGHT) if mode == DOVI_PROFILE_5 else None)
    gpu_tonemap = spec.tonemap and TONEMAP == "fast" and mode != DOVI_PROFILE_5
    if render and plan.backend == "vaapi" and not gpu_tonemap:
        # a VAAPI encoder can't take libplacebo-rendered frames (ffmpeg 8.1): its own tone mapping, or the CPU
        if spec.tonemap and mode != DOVI_PROFILE_5:
            gpu_tonemap = True
        else:
            plan = encoders.Plan("software", "software", plan.codec, plan.codec != "h264")
    codec = plan.codec
    interlaced = DEINTERLACE and info.field_order in ("tt", "bb", "tb", "bt")
    crop = None
    if CROP and spec.target != "dv81":  # Dolby Vision metadata describes the full frame: never cropped
        crop = crop_rect(input_path, info)
    pipe = encoders.backend(plan.backend, CAPABILITIES).pipeline(
                            plan, render=render, setparams=setparams,
                            color_range=colors["color_range"], width=TARGET_WIDTH, height=TARGET_HEIGHT,
                            software_decode=software_fallback, preset=PRESET, hdr=spec.target != "sdr",
                            gpu_tonemap=gpu_tonemap, deinterlace=interlaced, crop=crop, lookahead=LOOKAHEAD,
                            quality={"av1": GQ, "hevc": HEVC_GQ, "h264": H264_GQ, "software_av1": SOFTWARE_AV1_CRF,
                                     "software_hevc": SOFTWARE_HEVC_CRF, "software_h264": SOFTWARE_H264_CRF})
    hardware_init, accel, vf, encoder = pipe.hardware_init, pipe.accel, pipe.vf, pipe.encoder

    color_args = [
        "-color_primaries", colors["color_primaries"],
        "-color_trc", colors["color_transfer"],
        "-colorspace", colors["color_space"],
        "-color_range", colors["color_range"],
    ]
    if codec == "av1":
        metadata_bsf = (
            "av1_metadata="
            f"color_primaries={codes['color_primaries']}:"
            f"transfer_characteristics={codes['color_transfer']}:"
            f"matrix_coefficients={codes['color_space']}:"
            f"color_range={codes['color_range']}"
        )
    else:  # HEVC (HDR10+ and Dolby Vision can be injected afterwards) and H.264 share the field names
        metadata_bsf = (
            f"{codec}_metadata="
            f"colour_primaries={codes['color_primaries']}:"
            f"transfer_characteristics={codes['color_transfer']}:"
            f"matrix_coefficients={codes['color_space']}:"
            f"video_full_range_flag={codes['color_range']}"
        )

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        *hardware_init,
        *accel,
        "-i", input_path,
        "-map", "0:v:0",
        *stream_mapping_args(input_path),
        "-map_chapters", "0",
        "-map_metadata", "0",
        "-metadata", f"{OUTPUT_TAG}={codec}-{spec.tag_mode}",
        "-metadata:s:v:0", "title=",
        "-c:a", "copy",
        "-vf:v:0", vf,
        *color_args,
        *encoder,
        *pipe.extra,
        "-g", str(KEYFRAME_SECONDS * 60),
        "-force_key_frames", f"expr:gte(t,n_forced*{KEYFRAME_SECONDS})",
        "-cluster_time_limit", "5000",
        "-cluster_size_limit", "5242880",
        "-bsf:v:0", metadata_bsf,
        output_path,
    ]


OUTPUT_TAG = "MEDIA_CONVERTERR"  # container tag on every output: files carrying it are never re-encoded on their own


def output_tag(path: str) -> Optional[str]:
    """The OUTPUT_TAG value of a file this converter produced, or None (also when the file can't be probed)."""
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", path],
                           capture_output=True, text=True, timeout=60)
        tags = json.loads(p.stdout or "{}").get("format", {}).get("tags", {})
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return next((v for k, v in tags.items() if k.upper() == OUTPUT_TAG), None)


# Sources already in an efficient codec at a low bitrate are remuxed, not re-encoded (user decision): an encode can't
# add quality to them, only lose some (a 2.4 Mbit/s 4K web release came out at 2.9 Mbit/s). Video and audio are copied,
# audio and subtitle tracks are filtered as usual, HDR10+ and other metadata stay as they are.
REMUX_CODECS = {"hevc", "av1"}
REMUX_MBPS_PER_MPX = 1.8  # keep a source whose video bitrate is at most this many Mbit/s per megapixel of picture
# (1.8: 15 Mbit/s for 3840x2160, 3.7 for 1920x1080, 1.7 for 1280x720)


def video_bitrate(path: str) -> Optional[float]:
    """Video bitrate in bit/s: the stream's own value or BPS tag, else the container's minus the audio streams."""
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=bit_rate:stream=codec_type,bit_rate:"
                        "stream_tags=BPS,BPS-eng", "-of", "json", path], capture_output=True, text=True)
    try:
        data = json.loads(p.stdout or "{}")
    except ValueError:
        return None

    def rate(stream):
        tags = stream.get("tags") or {}
        for value in (stream.get("bit_rate"), tags.get("BPS"), tags.get("BPS-eng")):
            if value and str(value).isdigit() and int(value) > 0:
                return float(value)
        return None
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is not None and rate(video):
        return rate(video)
    total = (data.get("format") or {}).get("bit_rate")
    if not total or not str(total).isdigit():
        return None
    audio = [rate(s) for s in streams if s.get("codec_type") == "audio"]
    if any(a is None for a in audio):
        return None
    return float(total) - sum(audio)


CROP_SAMPLES = (0.2, 0.35, 0.5, 0.65, 0.8)  # positions checked for black bars (credits and openings avoided)


def picture_area(path: str, info: VideoInfo, duration: Optional[float] = None) -> Optional[tuple[int, int]]:
    """Width and height of the picture without black bars: ffmpeg cropdetect on 2 s at each CROP_SAMPLES position,
    keeping the largest area seen (one dark scene must not shrink it). None when it can't be measured."""
    if duration is None:
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                               capture_output=True, text=True)
        try:
            duration = float(probe.stdout.strip())
        except ValueError:
            return None
    width = height = left = top = 0
    for position in CROP_SAMPLES:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{duration * position:.1f}", "-i", path,
                            "-map", "0:v:0", "-t", "2", "-vf", "cropdetect=limit=0.1:round=2:reset=0", "-f", "null",
                            "-"], capture_output=True, text=True)
        crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", p.stderr)
        if crops:
            w, h, x, y = (int(v) for v in crops[-1])
            if w * h > width * height:
                width, height, left, top = w, h, x, y
            else:
                width, height = max(width, w), max(height, h)
    if not width or not height:
        return None
    return min(width, info.width or width), min(height, info.height or height)


def crop_rect(path: str, info: VideoInfo) -> Optional[tuple[int, int, int, int]]:
    """(width, height, x, y) of the picture without black bars, centred on the frame, even sizes; None when there are
    no bars worth cropping (under 2 % on both axes) or they can't be measured."""
    area = picture_area(path, info)
    if not area or not info.width or not info.height:
        return None
    w, h = area[0] - area[0] % 2, area[1] - area[1] % 2
    if w >= info.width * 0.98 and h >= info.height * 0.98:
        return None
    return w, h, (info.width - w) // 2 // 2 * 2, (info.height - h) // 2 // 2 * 2


def remux_decision(info: VideoInfo, mode: str, path: str) -> Optional[str]:
    """remux_reason with the source's bitrate, measured against the picture without black bars when the full frame
    alone would allow a remux (bars cost almost no bits, so the real picture gets more per pixel)."""
    if output_spec(info, mode).tonemap:
        return None  # tone mapping to SDR needs an encode
    bitrate = video_bitrate(path)
    reason = remux_reason(info, mode, bitrate)
    if reason is None:
        return None
    area = picture_area(path, info)
    if area and area != (info.width, info.height):
        return remux_reason(info, mode, bitrate, area)
    return reason


def remux_reason(info: VideoInfo, mode: str, bitrate: Optional[float],
                 area: Optional[tuple[int, int]] = None) -> Optional[str]:
    """Why a source should be remuxed instead of encoded, or None to encode it. area: the picture without black
    bars (width, height); the full frame when not given."""
    if not REMUX_ENABLED or info.codec_name not in REMUX_CODECS or bitrate is None:
        return None
    if mode not in (HDR_SDR, HDR10_PQ, HDR_HLG) or info.dovi_profile is not None:
        return None  # Dolby Vision is always converted, never kept as it is
    width, height = area or (info.width or round(info.height * 16 / 9), info.height)
    megapixels = width * height / 1e6
    if megapixels <= 0:
        return None
    mbps = bitrate / 1e6
    if mbps / megapixels > REMUX_MBPS_PER_MPX:
        return None
    return (f"{info.codec_name} {width}x{height}{' picture' if area else ''} at {mbps:.1f} Mbit/s = "
            f"{mbps / megapixels:.2f} Mbit/s per "
            f"megapixel (limit {REMUX_MBPS_PER_MPX:g})")


def build_remux_cmd(input_path: str, output_path: str, info: VideoInfo, mode: str) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", input_path,
        "-map", "0:v:0",
        *stream_mapping_args(input_path),
        "-map_chapters", "0",
        "-map_metadata", "0",
        "-metadata", f"{OUTPUT_TAG}=remux-{info.codec_name}-{mode}",
        "-metadata:s:v:0", "title=",
        "-c:v", "copy",
        "-c:a", "copy",
        output_path,
    ]  # embedded subtitles carry their own -c:s:N from stream_mapping_args


SDR_COLORS = {"color_primaries": "bt709", "color_transfer": "bt709", "color_space": "bt709", "color_range": "tv"}


@dataclass(frozen=True)
class OutputSpec:
    """What a source becomes: target "sdr", "hdr10", "hdr10plus" (HDR10 with HDR10+ injected), "hlg" or "dv81"; the
    wanted codec; whether HDR is tone mapped to SDR; and the mode recorded in the output tag."""
    target: str
    codec: str
    tonemap: bool
    tag_mode: str


def source_kind(info: Optional[VideoInfo], mode: str) -> str:
    if mode == HDR_SDR:
        return "sdr"
    if mode == DOVI_PROFILE_5:
        return "dv5"
    if info is not None and info.dovi_profile is not None:
        return "dv"
    return "hlg" if mode == HDR_HLG else "hdr10"


def dv7_note() -> str:
    """What happens to a Dolby Vision profile 7 source's enhancement layer, by the Dolby Vision output setting."""
    kept = ("its base layer and dynamic metadata become Dolby Vision 8.1" if OUTPUT_DV == "dv81"
            else "encoding its HDR10 base layer")
    return f"Dolby Vision profile 7 enhancement layer will not be retained; {kept}"


def output_spec(info: Optional[VideoInfo], mode: str) -> OutputSpec:
    """The configured output for this source (settings OUTPUT_*, SDR_CODEC, HDR_CODEC). Without source details the
    source is taken as plain HDR10 (for display)."""
    kind = source_kind(info, mode)
    if kind == "sdr":
        return OutputSpec("sdr", SDR_CODEC, False, HDR_SDR)
    target = {"dv5": OUTPUT_DV5, "dv": OUTPUT_DV, "hlg": OUTPUT_HLG, "hdr10": OUTPUT_HDR10}[kind]
    if target == "sdr":
        return OutputSpec("sdr", SDR_CODEC, True, HDR_SDR)
    hdr10_plus = bool(info and info.hdr10_plus)
    if target == "hdr10plus" and not hdr10_plus:
        target = "hdr10"
    dynamic = target in ("hdr10plus", "dv81")
    return OutputSpec(target, "hevc" if dynamic else HDR_CODEC, False, "dovi-8.1" if target == "dv81" else mode)


def encode_plan(mode: str, info: Optional[VideoInfo] = None) -> "encoders.Plan":
    """Backend, device, codec and bit depth for this source (see output_spec and encoders.plan)."""
    spec = output_spec(info, mode)
    return encoders.plan(codec=spec.codec, hdr=spec.target != "sdr", preference=BACKEND, capabilities=CAPABILITIES,
                         fallback=CODEC_FALLBACK)


def output_codec(mode: str, info: Optional[VideoInfo] = None) -> str:
    """The codec this source is encoded to (the plan's: a GPU fallback can change it)."""
    return encode_plan(mode, info).codec


def output_colors(info: VideoInfo, mode: str) -> dict[str, str]:
    """The colour signalling the output must carry: BT.709 when tone mapped, else the source's (resolved)."""
    if output_spec(info, mode).tonemap:
        return dict(SDR_COLORS)
    return resolve_color_properties(info, mode)


def count_video_frames(path: str) -> int:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                        "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
                       capture_output=True, text=True, check=True)
    return int(p.stdout.strip().split(",")[0])


def packet_pts(path: str, count: int = 400) -> list[float]:
    """Presentation times of the first `count` video packets, in file (decode) order."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", f"%+#{count}",
                          "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout
    return [float(x.strip(",")) for x in out.split() if x.strip(",") not in ("", "N/A")]


def same_timestamps(a: list[float], b: list[float], tolerance: float = 0.0015) -> bool:
    return len(a) == len(b) and len(a) > 0 and all(abs(x - y) <= tolerance for x, y in zip(a, b))


def inject_hdr10plus(source_path: str, encoded_path: str, work_dir: str, logger: logging.Logger) -> bool:
    """Copy the source's HDR10+ dynamic metadata into the HEVC encode, in place. Returns True when injected; on any
    problem the encode stays plain HDR10 and a warning is logged (never fails the conversion)."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="hdr10plus.", dir=work_dir)
    try:
        meta, raw, inj = (os.path.join(tmp, n) for n in ("meta.json", "video.hevc", "injected.hevc"))
        out = os.path.join(tmp, "out.mkv")
        quiet = {"capture_output": True, "text": True, "check": True}
        source_frames, encoded_frames = count_video_frames(source_path), count_video_frames(encoded_path)
        if source_frames != encoded_frames:
            logger.warning("HDR10+ not injected: frame count differs (source %d, encode %d): %s",
                           source_frames, encoded_frames, source_path)
            return False
        subprocess.run(["hdr10plus_tool", "extract", source_path, "-o", meta], **quiet)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", encoded_path, "-map", "0:v:0", "-c", "copy",
                        "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", raw], **quiet)
        subprocess.run(["hdr10plus_tool", "inject", "-i", raw, "-j", meta, "-o", inj], **quiet)
        rate = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=r_frame_rate", "-of", "csv=p=0", encoded_path], **quiet).stdout.strip()
        rate = rate.split(",")[0].strip()  # csv output can carry a trailing separator ("24000/1001,")
        # raw HEVC has no timestamps: mkvmerge derives them from the stream (picture order), keeping B-frame order;
        # ffmpeg would number the frames in decode order (frames shown out of order, players stutter or crash).
        # The other tracks, chapters and tags come from the encode.
        merge = subprocess.run(["mkvmerge", "-q", "-o", out, "--default-duration", f"0:{rate}p", inj,
                                "--no-video", encoded_path], capture_output=True, text=True)
        if merge.returncode not in (0, 1):  # 1 = warnings only
            raise RuntimeError(f"mkvmerge exit {merge.returncode}: {merge.stdout[-300:]}")
        info = probe_video_info(out)
        if not same_timestamps(packet_pts(out), packet_pts(encoded_path)):
            logger.warning("HDR10+ injection changed the video timestamps; keeping plain HDR10: %s", source_path)
            return False
        if not info.hdr10_plus or count_video_frames(out) != source_frames:
            logger.warning("HDR10+ injection did not verify; keeping plain HDR10: %s", source_path)
            return False
        os.replace(out, encoded_path)
        return True
    except (subprocess.CalledProcessError, OSError, ValueError, RuntimeError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        logger.warning("HDR10+ not injected (%s): %s", detail.strip()[-300:], source_path)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def inject_dovi(source_path: str, encoded_path: str, work_dir: str, logger: logging.Logger) -> bool:
    """Make the HEVC encode Dolby Vision profile 8.1: the source's RPUs (converted to 8.1 by dovi_tool mode 2, which
    keeps profile 7's and 8's base-layer mapping) are injected into the encode and the result remuxed. In place;
    returns True when injected and verified. On any problem the encode stays HDR10 and a warning is logged. Needs the
    same frames and picture size as the source (the RPUs describe the source's frames and active area)."""
    import shutil
    import tempfile
    source_info, encoded_info = probe_video_info(source_path), probe_video_info(encoded_path)
    if (source_info.width, source_info.height) != (encoded_info.width, encoded_info.height):
        logger.warning("Dolby Vision not injected: the picture was scaled (%dx%d to %dx%d): %s", source_info.width,
                       source_info.height, encoded_info.width, encoded_info.height, source_path)
        return False
    tmp = tempfile.mkdtemp(prefix="dovi.", dir=work_dir)
    try:
        rpu, raw, inj = (os.path.join(tmp, n) for n in ("rpu.bin", "video.hevc", "injected.hevc"))
        out = os.path.join(tmp, "out.mkv")
        quiet = {"capture_output": True, "text": True, "check": True}
        source_frames = count_video_frames(source_path)
        if count_video_frames(encoded_path) != source_frames:
            logger.warning("Dolby Vision not injected: frame count differs: %s", source_path)
            return False
        # the source's HEVC stream piped into dovi_tool (no copy of the whole stream on disk)
        dump = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", source_path, "-map", "0:v:0", "-c", "copy",
                                 "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"], stdout=subprocess.PIPE)
        extract = subprocess.run(["dovi_tool", "-m", "2", "extract-rpu", "-", "-o", rpu], stdin=dump.stdout,
                                 capture_output=True, text=True)
        dump.stdout.close()
        dump.wait()
        if extract.returncode != 0 or dump.returncode != 0 or not os.path.getsize(rpu):
            raise RuntimeError(f"RPU extraction failed: {extract.stderr[-300:]}")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", encoded_path, "-map", "0:v:0", "-c", "copy",
                        "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", raw], **quiet)
        subprocess.run(["dovi_tool", "inject-rpu", "-i", raw, "--rpu-in", rpu, "-o", inj], **quiet)
        rate = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=r_frame_rate", "-of", "csv=p=0", encoded_path], **quiet).stdout.strip()
        rate = rate.split(",")[0].strip()
        merge = subprocess.run(["mkvmerge", "-q", "-o", out, "--default-duration", f"0:{rate}p", inj,
                                "--no-video", encoded_path], capture_output=True, text=True)
        if merge.returncode not in (0, 1):
            raise RuntimeError(f"mkvmerge exit {merge.returncode}: {merge.stdout[-300:]}")
        info = probe_video_info(out)
        if not same_timestamps(packet_pts(out), packet_pts(encoded_path)):
            logger.warning("Dolby Vision injection changed the video timestamps; keeping HDR10: %s", source_path)
            return False
        if info.dovi_profile != 8 or count_video_frames(out) != source_frames:
            logger.warning("Dolby Vision injection did not verify (profile %s); keeping HDR10: %s", info.dovi_profile,
                           source_path)
            return False
        os.replace(out, encoded_path)
        return True
    except (subprocess.CalledProcessError, OSError, ValueError, RuntimeError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        logger.warning("Dolby Vision not injected (%s): %s", str(detail).strip()[-300:], source_path)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


#######################################################################
#                               Main                                   #
#######################################################################

def signal_handler(signum, frame):
    process = _active_process
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    sys.exit(128 + signum)


def main() -> int:
    logger = setup_logger()
    logger.info(
        "==== Media Convert V5 start ==== script=%s",
        os.path.realpath(__file__),
    )
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    os.makedirs(work_dir, exist_ok=True)
    try:
        lock_handle = acquire_process_lock(LOCK_PATH)
    except BlockingIOError:
        logger.error(f"Another media-convert process holds {LOCK_PATH}")
        return 2

    conn = None
    try:
        conn = db_connect(DB_PATH)
        logger.debug(f"DB opened at {DB_PATH}")

        # Collect candidate files
        paths = []
        completed_scans = []
        for base_path in watched_folders:
            base_path = normalize_path(base_path)
            logger.info(f"Scanning {base_path}")
            t0 = time.time()
            try:
                root_stat = os.stat(base_path)
            except OSError as e:
                logger.warning(
                    f"Scan unavailable; orphan cleanup disabled for "
                    f"{base_path}: {e}"
                )
                continue
            if not os.path.isdir(base_path):
                logger.warning(
                    f"Scan path is not a directory; orphan cleanup disabled "
                    f"for {base_path}"
                )
                continue

            scan_errors = []

            def record_scan_error(error):
                scan_errors.append(error)
                logger.warning(f"Scan error for {base_path}: {error}")

            for root, dirs, files in os.walk(
                base_path,
                topdown=True,
                onerror=record_scan_error,
            ):
                dirs[:] = [d for d in dirs if d not in exclude and not d.startswith(".")]  # hidden: not library
                for fname in files:
                    full = normalize_path(os.path.join(root, fname))
                    ext = fname.rsplit('.', 1)[-1].lower()
                    if ext in convertible_extensions and not fname.startswith('.'):  # hidden = temp files
                        paths.append(full)
            if scan_errors:
                logger.warning(
                    f"Incomplete scan; orphan cleanup disabled for {base_path}"
                )
            else:
                completed_scans.append(
                    (base_path, (root_stat.st_dev, root_stat.st_ino))
                )
            logger.info(
                "Scan time %.0fs for %s",
                round(time.time() - t0, 0),
                base_path,
            )

        logger.info(f"Candidates found: {len(paths)}")
        if not paths:
            logger.info("No files to process.")

        processed = 0
        checked = 0
        skipped = 0
        failed = 0

        for candidate_index, in_path in enumerate(paths):
            temp_path = temporary_path(candidate_index)
            try:
                in_path = normalize_path(in_path)
                size, mtime, device, inode = file_signature(in_path)
                logger.debug(
                    f"File sig size={size} mtime={mtime} device={device} "
                    f"inode={inode} path={in_path}"
                )

                # DB short-circuit: only files not recorded as ok are encoded
                row = db_lookup(conn, in_path, size, mtime, device, inode)
                renamed_from = None
                if not row:
                    renamed_from = db_reconcile_rename(
                        conn, in_path, size, mtime, device, inode
                    )
                    if renamed_from:
                        logger.info(
                            f"DB path updated after rename: "
                            f"{renamed_from} -> {in_path}"
                        )
                        row = db_lookup(
                            conn, in_path, size, mtime, device, inode
                        )
                if row:
                    skipped += 1
                    if renamed_from:
                        logger.info(f"Skip (renamed DB ok): {in_path}")
                    else:
                        logger.debug(f"Skip (DB ok): {in_path}")
                    continue

                out_path = to_target_naming(in_path)
                ensure_output_path_available(in_path, out_path)
                if os.path.lexists(temp_path):
                    raise FileExistsError(f"Refusing to overwrite stale temp file: {temp_path}")

                video_info = probe_video_info(in_path)
                mode = hdr_mode(video_info)
                logger.info(f"Video format: {describe_video(video_info, mode)} path={in_path}")

                if (
                    mode == HDR10_PQ
                    and video_info.dovi_profile == 7
                    and video_info.dovi_el_present
                ):
                    logger.warning(f"{dv7_note()}: {in_path}")
                if video_info.hdr10_plus:
                    logger.info(
                        "HDR10+ input detected; output dynamic metadata will be verified "
                        f"after encoding: {in_path}"
                    )

                cmd = build_cmd(in_path, temp_path, False, video_info)
                if JUST_CHECK:
                    checked += 1
                    logger.info(f"Check command: {command_text(cmd)} (cwd={work_dir})")
                    continue

                logger.warning(f"Encoding start: {in_path}")
                logger.info(f"Encoding cmd: {command_text(cmd)} (cwd={work_dir})")
                return_code, command_log = run(cmd, work_dir, logger)
                logger.info(f"Encoding exit: {return_code}")

                if return_code != 0:
                    logger.warning(
                        "Encoding failed. Retrying with software decode/filter fallback."
                    )
                    cmd = build_cmd(in_path, temp_path, True, video_info)
                    logger.warning(f"Encoding start: {in_path}")
                    logger.info(f"Encoding cmd: {command_text(cmd)} (cwd={work_dir})")
                    return_code, command_log = run(cmd, work_dir, logger)
                    logger.info(f"Encoding exit: {return_code}")

                if return_code != 0:
                    detail = command_log.strip().splitlines()
                    last_error = detail[-1] if detail else "no ffmpeg error text"
                    raise RuntimeError(
                        f"ffmpeg exit {return_code}: {last_error}"
                    )

                if output_codec(mode, video_info) == "hevc" and video_info.hdr10_plus:
                    if inject_hdr10plus(in_path, temp_path, work_dir, logger):
                        logger.info(f"HDR10+ metadata injected: {in_path}")
                output_info = validate_output(temp_path, mode, video_info)
                logger.info(
                    f"Validated output: "
                    f"{describe_video(output_info, hdr_mode(output_info))}"
                )

                if video_info.hdr10_plus and not output_info.hdr10_plus:
                    logger.warning(
                        "HDR10+ dynamic metadata was not retained; "
                        f"output is standard HDR10: {in_path}"
                    )

                missing_static_metadata = []
                if (
                    video_info.has_mastering_display
                    and not output_info.has_mastering_display
                ):
                    missing_static_metadata.append("mastering-display")
                if (
                    video_info.has_content_light_level
                    and not output_info.has_content_light_level
                ):
                    missing_static_metadata.append("content-light-level")
                if missing_static_metadata:
                    logger.warning(
                        "Static HDR metadata was not retained: "
                        f"{', '.join(missing_static_metadata)} path={in_path}"
                    )

                backup_path = install_output(in_path, temp_path, out_path)
                new_size, new_mtime, new_device, new_inode = file_signature(
                    out_path
                )
                db_upsert(
                    conn,
                    out_path,
                    new_size,
                    new_mtime,
                    status='ok',
                    note=f'encoded-{output_codec(mode, video_info)}-{output_spec(video_info, mode).tag_mode}',
                    device=new_device,
                    inode=new_inode,
                )

                if DELETE:
                    try:
                        os.remove(backup_path)
                    except Exception as e:
                        logger.warning(
                            f"Could not delete original backup {backup_path}: {e}"
                        )
                else:
                    logger.info(f"Original retained at: {backup_path}")

                processed += 1
                logger.info(f"Encoding success: {in_path} -> {out_path}")

            except Exception as e:
                failed += 1
                logger.exception(f"Processing failed for {in_path}: {e}")
                cleanup_temp(temp_path, logger)
                try:
                    if os.path.isfile(in_path):
                        size, mtime, device, inode = file_signature(in_path)
                        db_upsert(
                            conn,
                            in_path,
                            size,
                            mtime,
                            status='error',
                            note=str(e),
                            device=device,
                            inode=inode,
                        )
                except Exception:
                    logger.debug("DB upsert skipped due to signature error")

        cleanup_folders = []
        for base_path, scanned_identity in completed_scans:
            try:
                current_stat = os.stat(base_path)
            except OSError as e:
                logger.warning(
                    f"Scan path unavailable after processing; orphan cleanup "
                    f"disabled for {base_path}: {e}"
                )
                continue
            current_identity = (current_stat.st_dev, current_stat.st_ino)
            if current_identity != scanned_identity:
                logger.warning(
                    f"Scan path identity changed; orphan cleanup disabled "
                    f"for {base_path}"
                )
                continue
            cleanup_folders.append(base_path)

        orphaned = db_remove_orphans(
            conn, cleanup_folders, set(paths)
        )
        for orphaned_path in orphaned:
            logger.debug(f"Removed orphaned DB entry: {orphaned_path}")
        if orphaned:
            logger.info(f"Removed orphaned DB entries: {len(orphaned)}")

        logger.info(
            f"Summary: processed={processed} checked={checked} "
            f"skipped={skipped} failed={failed} orphaned={len(orphaned)}"
        )
        logger.info("==== Media Convert V5 end ====")
        return 1 if failed else 0
    finally:
        if conn is not None:
            conn.close()
        lock_handle.close()

if __name__ == '__main__':
    sys.exit(main())
