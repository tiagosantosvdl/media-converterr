"""Settings edited from the web interface, kept in the converter's database, plus the web login.

The TOML file stays the first-boot and fallback source. Values saved from the web interface live in the
`app_settings` table (one JSON document per key) and take precedence over the file. Standard library only.

Keys:
  connections   {"instances": [...], "jellyfin": {...} | null, "public_url": str}
  hook_token    secret in the webhook URL that Sonarr/Radarr call
  auth          {"user", "salt", "hash", "iterations"} (PBKDF2-SHA256)
  session_key   HMAC key for login cookies
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from typing import Any, Optional

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""
PBKDF2_ITERATIONS = 600_000
SESSION_SECONDS = 30 * 24 * 3600


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:  # table not created yet (database from an older version, read-only)
        return default
    return json.loads(row[0]) if row else default


def put(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with conn:
        conn.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), time.time()),
        )


def version(conn: sqlite3.Connection) -> float:
    """Changes whenever a setting is saved; the worker reloads its settings when it does."""
    try:
        row = conn.execute("SELECT MAX(updated_at) FROM app_settings").fetchone()
    except sqlite3.OperationalError:
        return 0.0
    return float(row[0] or 0.0)


def read(database_path: str, key: str, default: Any = None) -> Any:
    """One stored value, read-only; the default when the database or the table doesn't exist yet."""
    if not os.path.isfile(database_path):
        return default
    try:
        conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return default
    try:
        return get(conn, key, default)
    finally:
        conn.close()


def secret(conn: sqlite3.Connection, key: str) -> str:
    """A random secret stored under key, created on first use."""
    value = get(conn, key)
    if not value:
        value = secrets.token_urlsafe(32)
        put(conn, key, value)
    return str(value)


# ---- web login -----------------------------------------------------------------------------------------------------
def hash_password(password: str, *, salt: Optional[bytes] = None, iterations: int = PBKDF2_ITERATIONS) -> dict:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"salt": salt.hex(), "hash": digest.hex(), "iterations": iterations}


def verify_password(record: Optional[dict], user: str, password: str) -> bool:
    if not record or not hmac.compare_digest(str(record.get("user", "")), user):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(record["salt"]),
                                 int(record["iterations"]))
    return hmac.compare_digest(digest.hex(), record["hash"])


def make_session(key: str, user: str, *, now: Optional[float] = None, seconds: int = SESSION_SECONDS) -> str:
    expires = int((now or time.time()) + seconds)
    body = base64.urlsafe_b64encode(f"{user}\n{expires}".encode("utf-8")).decode("ascii")
    mac = hmac.new(key.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def check_session(key: str, token: Optional[str], *, now: Optional[float] = None) -> Optional[str]:
    """The user a session cookie belongs to, or None when it is missing, forged or expired."""
    if not token or "." not in token:
        return None
    body, mac = token.rsplit(".", 1)
    expected = hmac.new(key.encode("utf-8"), body.encode("ascii", "replace"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        user, expires = base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8").split("\n")
    except (ValueError, UnicodeDecodeError):
        return None
    return user if int(expires) > (now or time.time()) else None
