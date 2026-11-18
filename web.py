#!/usr/bin/env python3
"""Web interface for media-converterr, laid out like Sonarr/Radarr (see web_ui.py): Activity (queue, history,
Sonarr/Radarr events), Library (file search, re-encode), Failed, Settings (Connect, Conversion, General) and System
(Status, Tasks, Hardware, Logs); plus the webhook endpoint Sonarr/Radarr call.

Standard library only. Pages read the database read-only; the write actions use the converter's own functions: retry
a failed or cancelled job (`retry_job`), request a re-encode of a file (`enqueue_job(force=True)`, the only way a file
the converter already produced is encoded again), save settings (`settings_store`) and queue webhook events
(`capture_hook_event`).
Login (optional): the first visit (`/setup`) creates one account or continues without a login; Settings › General
turns it on or off later. WEB_AUTH=off forces it off (for an authenticating proxy in front). The webhook endpoint
always uses a secret token in its URL instead.
Usage: web.py --config /config/media_converterr.toml [--port 8090]
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import encoders
import gpus
import media_converterr as mc
import settings_store
import web_ui as ui
from web_ui import esc, form_group, icon, label, table, tool

SESSION_COOKIE = "mc_session"
MAX_FORM = 65536
MAX_HOOK = 1024 * 1024


def when(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "–"


def dur(seconds) -> str:
    if seconds is None:
        return "–"
    seconds = int(seconds)
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m" if seconds >= 3600 else f"{seconds // 60}m{seconds % 60:02d}s"


def gb(size) -> str:
    """Sizes as Sonarr shows them (binary units)."""
    if not size:
        return "–"
    for unit, factor in (("TiB", 2 ** 40), ("GiB", 2 ** 30), ("MiB", 2 ** 20)):
        if size >= factor:
            return f"{size / factor:.1f} {unit}"
    return f"{size / 1024:.1f} KiB"


def name(path) -> str:
    return esc(os.path.basename(path or ""))


def first(form: dict, key: str) -> str:
    return form.get(key, [""])[0].strip()


def read_progress(work_dir: str) -> dict | None:
    """The running job's state from the converter's note and ffmpeg's -progress file: {path, stage, step, steps, pct,
    speed, left, ended} (pct/speed/left None outside the conversion step), or None when there is no note."""
    try:
        with open(os.path.join(work_dir, mc.PROGRESS_META), encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return None
    out = {"path": meta.get("path"), "kind": meta.get("kind"), "stage": meta.get("stage"), "step": meta.get("step"),
           "steps": len(meta.get("steps") or []), "pct": None, "speed": None, "left": None, "ended": False}
    try:
        with open(os.path.join(work_dir, mc.PROGRESS_FILE), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4096))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return out
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    out["ended"] = values.get("progress") == "end"
    try:
        position = int(values.get("out_time_us") or values.get("out_time_ms") or 0) / 1e6
    except ValueError:
        position = 0.0
    duration = float(meta.get("duration") or 0)
    if duration and position > 0:
        out["pct"] = 100.0 if out["ended"] else min(99.9, 100 * position / duration)
        try:
            out["speed"] = float(values.get("speed", "").rstrip("x"))
        except ValueError:
            pass
        if out["speed"] and not out["ended"]:
            out["left"] = (duration - position) / out["speed"]
    return out


STATE_LABELS = {"running": "info", "queued": "", "retry_wait": "warn", "failed": "bad", "cancelled": "",
                "done": "ok"}


class Dashboard:
    def __init__(self, config_path: str, auth: bool = True):
        self.config_path = config_path
        self.auth_allowed = auth  # False: WEB_AUTH=off
        self.reload()

    def reload(self) -> None:
        self.settings = mc.load_settings(self.config_path)
        self.log_path = os.path.join(self.settings.log_dir, "media-converterr.log")
        roots = [i.local_root for i in self.settings.instances] + list(self.settings.unlinked_folders)
        # rows imported from older databases can point outside today's library: count only files under these roots
        self.scope = "(" + " OR ".join("path LIKE ?" for _ in roots) + ")" if roots else "1"
        self.scope_args = tuple(r.rstrip("/") + "/%" for r in roots)

    def db(self, write: bool = False) -> sqlite3.Connection:
        uri = f"file:{self.settings.database_path}" + ("" if write else "?mode=ro")
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def store(self) -> sqlite3.Connection:
        conn = self.db(write=True)
        settings_store.ensure(conn)
        return conn

    def put(self, key: str, value) -> None:
        conn = self.store()
        try:
            settings_store.put(conn, key, value)
        finally:
            conn.close()

    def page(self, title: str, body: str, path: str, toolbar: str = "", refresh: int = 0, search: str = "") -> str:
        return ui.layout(title, body, path=path, toolbar=toolbar, refresh=refresh, logout=self.auth_enabled(),
                         search=search)

    # ---- Activity -------------------------------------------------------------------------------------------------
    def queue(self) -> str:
        with self.db() as c:
            running = c.execute("SELECT * FROM jobs WHERE state='running' ORDER BY started_at").fetchall()
            rows = c.execute("SELECT * FROM jobs WHERE state IN ('queued','retry_wait','failed','cancelled') "
                             "ORDER BY CASE state WHEN 'queued' THEN 1 WHEN 'retry_wait' THEN 2 ELSE 3 END, "
                             "available_at LIMIT 500").fetchall()
            avg = c.execute("SELECT AVG(finished_at-started_at) FROM (SELECT * FROM jobs WHERE state='done' AND "
                            "started_at IS NOT NULL ORDER BY finished_at DESC LIMIT 20)").fetchone()[0]
        progress = read_progress(self.settings.work_dir)
        icons = {"running": ("info", "Converting"), "queued": ("clock", "Queued"), "retry_wait": ("warning", "Waiting "
                 "to retry"), "failed": ("error", "Failed"), "cancelled": ("clock", "Cancelled")}
        out = []
        waiting = 0
        for j in list(running) + list(rows):
            kind, title = icons.get(j["state"], ("clock", j["state"]))
            if j["state"] == "running" and progress and progress["path"] == j["path"]:
                step = (f"({progress['step']}/{progress['steps']}) {progress['stage']}"
                        if progress["steps"] and progress["stage"] else "")
                converting = progress["stage"] in (None, "Converting", "Remuxing")
                if converting and progress["pct"] is not None and not progress["ended"]:
                    speed = f" · {progress['speed']:.2f}×" if progress["speed"] else ""
                    bar = ui.progress(progress["pct"], f"{progress['pct']:.0f}%{speed}")
                    left = dur(progress["left"]) if progress["left"] is not None else "–"
                else:
                    bar, left = ui.progress(100 if not converting or progress["ended"] else 0,
                                            "finishing" if converting else "working"), "–"
                kind, title = "info", step or "Converting"
            elif j["state"] == "running":  # no progress file (an older converter, or not started yet): a guess
                elapsed = time.time() - (j["started_at"] or time.time())
                pct = min(99, 100 * elapsed / avg) if avg else None
                bar = ui.progress(pct, f"~{pct:.0f}% (guess)" if pct is not None else "")
                left = "–"
            else:
                waiting += j["state"] in ("queued", "retry_wait")
                bar, left = ui.progress(0 if j["state"] in ("queued", "retry_wait") else None, ""), "–"
            action = (f"<form method='post' action='/retry/{j['id']}'><button class='btn icon-only' title='Retry'>"
                      f"{icon('refresh', 16)}</button></form>" if j["state"] in ("failed", "cancelled") else "")
            error = f"<div class='bad' style='font-size:12px'>{esc((j['last_error'] or '')[:200])}</div>" \
                if j["last_error"] and j["state"] != "running" else ""
            step_line = (f"<div class='muted' style='font-size:12px'>{esc(title)}</div>"
                         if j["state"] == "running" and progress and progress["path"] == j["path"] else "")
            out.append([ui.status_icon(kind, title), f"{name(j['path'])}{step_line}{error}",
                        f"<span class='muted'>{esc(j['trigger'])}</span>", f"<span class='muted'>"
                        f"{when(j['started_at'] if j['state'] == 'running' else j['enqueued_at'])}</span>",
                        left, bar, action])
        body = ["<h1>Queue</h1>"]
        if waiting and avg:
            body.append(f"<p class='muted'>{waiting} waiting · about {dur(waiting * avg)} at the recent average of "
                        f"{dur(avg)} per file.</p>")
        body.append(table(["", "File", "Trigger", "Added", "Time Left", "Progress", ""], out,
                          "Queue is empty. Sonarr and Radarr send new files here as they import them."))
        return self.page("Queue", "".join(body), "/queue", tool("Refresh", "refresh", href="/queue"), refresh=30)

    def history(self, query: dict) -> str:
        limit = min(int(query.get("limit", ["100"])[0] or 100), 1000)
        with self.db() as c:
            rows = c.execute("SELECT * FROM jobs WHERE state='done' ORDER BY finished_at DESC LIMIT ?",
                             (limit,)).fetchall()
        bar = tool("Refresh", "refresh", href="/history") + tool(
            "Show 1000" if limit < 1000 else "Show 100", "advanced", href=f"/history?limit={1000 if limit < 1000 else 100}")
        return self.page("History", f"<h1>History</h1><p class='muted'>The last {limit} conversions.</p>"
                         + self._jobs_table(rows, done=True), "/history", bar)

    def events(self) -> str:
        with self.db() as c:
            rows = c.execute("SELECT * FROM hook_events ORDER BY id DESC LIMIT 200").fetchall()
        kinds = {"done": ("ok", "Processed"), "failed": ("error", "Failed"), "queued": ("clock", "Waiting"),
                 "running": ("info", "Processing"), "retry_wait": ("warning", "Waiting to retry")}
        body = table(["", "Source", "Event", "Received", "Error"],
                     ([ui.status_icon(*kinds.get(r["state"], ("clock", r["state"]))), esc(r["arr_instance"]),
                       esc(r["event_type"]), f"<span class='muted'>{when(r['created_at'])}</span>",
                       f"<span class='bad'>{esc(r['last_error'])}</span>"] for r in rows), "No events received yet.")
        return self.page("Events", "<h1>Events from Sonarr and Radarr</h1>" + body, "/events",
                         tool("Refresh", "refresh", href="/events"), refresh=30)

    # ---- Library and Failed ---------------------------------------------------------------------------------------
    def files(self, query: dict) -> str:
        q = query.get("q", [""])[0].strip()
        body = ["<h1>Files</h1><form method='get' action='/files' style='display:flex;gap:8px;max-width:640px;"
                f"margin-bottom:12px'><input name='q' value='{esc(q)}' placeholder='Part of a path or name'>"
                f"<button class='btn primary'>{icon('search', 16)} Search</button></form>"
                "<p class='muted'>Files this converter produced are never encoded again on their own. Re-encode "
                "converts one again from its current file (each encode loses a little quality; metadata an older "
                "version dropped can't come back: replace the file with the original for that).</p>"]
        if q:
            like = "%" + q.replace("%", "").replace("_", "") + "%"
            with self.db() as c:
                rows = c.execute(f"SELECT path, size, status, note, last_checked FROM files WHERE {self.scope} AND "
                                 "path LIKE ? ORDER BY path LIMIT 200", (*self.scope_args, like)).fetchall()
            body.append(table(["File", "Status", "Note", "Size", "Checked", ""],
                              ([f"{name(r['path'])}<div class='muted'>{esc(os.path.dirname(r['path']))}</div>",
                                label(r["status"], {"ok": "ok", "error": "bad"}.get(r["status"], "")),
                                f"<span class='muted'>{esc(r['note'])}</span>", gb(r["size"]),
                                f"<span class='muted'>{when(r['last_checked'])}</span>", self._reencode_button(r["path"])]
                               for r in rows), "No matching files."))
        return self.page("Files", "".join(body), "/files", search=q)

    def errors(self) -> str:
        with self.db() as c:
            grouped = c.execute(f"SELECT note, COUNT(*) n FROM files WHERE status='error' AND {self.scope} "
                                "GROUP BY note ORDER BY n DESC", self.scope_args).fetchall()
            sample = c.execute(f"SELECT path, note, last_checked FROM files WHERE status='error' AND {self.scope} "
                               "ORDER BY last_checked DESC LIMIT 200", self.scope_args).fetchall()
            failed = c.execute("SELECT * FROM jobs WHERE state='failed' ORDER BY finished_at DESC LIMIT 100").fetchall()
        body = ["<h1>Failed</h1><p class='muted'>Files under the configured library folders only.</p>",
                "<h2>Failed queue jobs</h2>", self._jobs_table(failed, actions=True),
                "<h2>Files with errors, by message</h2>",
                table(["Message", "Files"], ([f"<span class='bad'>{esc(r['note'])}</span>", str(r["n"])]
                                             for r in grouped)),
                "<h2>Most recent file errors</h2>",
                table(["File", "Message", "Checked"], ([name(r["path"]), f"<span class='bad'>{esc(r['note'])}</span>",
                                                        f"<span class='muted'>{when(r['last_checked'])}</span>"]
                                                       for r in sample))]
        return self.page("Failed", "".join(body), "/errors", tool("Refresh", "refresh", href="/errors"))

    @staticmethod
    def _reencode_button(path) -> str:
        return (f"<form method='post' action='/reencode' data-confirm='Re-encode this file? It is converted again "
                f"from its current version.'><input type='hidden' name='path' value='{esc(path)}'>"
                f"<button class='btn small'>Re-encode</button></form>")

    def in_scope(self, path: str) -> bool:
        roots = [i.local_root for i in self.settings.instances] + list(self.settings.unlinked_folders)
        return any(mc.path_is_within(path, r) for r in roots)

    def reencode(self, path: str) -> int | None:
        path = mc.normalize_path(path)
        if not self.in_scope(path) or not os.path.isfile(path):
            return None
        conn = self.db(write=True)
        try:
            job_id, _ = mc.enqueue_job(conn, path, trigger="manual", event_type="Dashboard re-encode",
                                       owner=mc.stored_owner_for_path(conn, path),
                                       signature=mc.SourceSignature.from_path(path), force=True)
            return job_id
        finally:
            conn.close()

    def retry(self, job_id: int) -> bool:
        conn = self.db(write=True)
        try:
            return mc.retry_job(conn, job_id)
        finally:
            conn.close()

    def _jobs_table(self, rows, done: bool = False, actions: bool = False) -> str:
        headers = ["#", "File", "State", "Trigger"] + (["Finished", "Took", "Size before → after", ""] if done else
                                                       ["Queued", "Attempts", "Error"]) + ([""] if actions else [])
        out = []
        for j in rows:
            cells = [f"<span class='muted'>{j['id']}</span>", name(j["path"]),
                     label(j["state"], STATE_LABELS.get(j["state"], "")), f"<span class='muted'>{esc(j['trigger'])}</span>"]
            if done:
                try:
                    after = os.path.getsize(j["output_path"] or j["path"])
                except OSError:
                    after = None
                took = (j["finished_at"] - j["started_at"]) if j["started_at"] and j["finished_at"] else None
                cells += [f"<span class='muted'>{when(j['finished_at'])}</span>", dur(took),
                          f"{gb(j['source_size'])} → {gb(after)}", self._reencode_button(j["output_path"] or j["path"])]
            else:
                cells += [f"<span class='muted'>{when(j['enqueued_at'])}</span>", str(j["attempts"]),
                          f"<span class='bad'>{esc((j['last_error'] or '')[:200])}</span>"]
            if actions:
                cells.append(f"<form method='post' action='/retry/{j['id']}'><button class='btn small'>Retry</button>"
                             f"</form>" if j["state"] in ("failed", "cancelled") else "")
            out.append(cells)
        return table(headers, out)

    # ---- System -----------------------------------------------------------------------------------------------------
    def health(self, caps: dict | None) -> list[tuple[str, str]]:
        """Problems worth a look, Sonarr-style: (kind, message with an optional link)."""
        issues = []
        if not caps:
            issues.append(("warning", "The encoders have not been checked yet. <a href='/hardware'>Hardware</a>"))
        else:
            found = gpus.attach_results(gpus.find(), caps)
            for g in found:
                missing = gpus.missing(g)
                if missing:
                    issues.append(("warning", f"{esc(g.name)} should encode {esc(', '.join(missing))} but its test "
                                              "failed. <a href='/hardware'>Hardware</a>"))
            mc5 = mc.get_legacy_converter()
            if mc5.encode_plan(mc5.HDR10_PQ).backend == "software":
                issues.append(("warning", "HDR is encoded on the CPU, which is slow at 4K: no GPU passed the 10-bit "
                                          "HEVC test. <a href='/hardware'>Hardware</a>"))
        if not self.settings.public_url:
            issues.append(("info", "No webhook address set: Sonarr and Radarr can't announce new files; only the "
                                   "scheduled scan finds them. <a href='/settings/connect'>Connect</a>"))
        for inst in self.settings.instances:
            if not os.path.isdir(inst.local_root):
                issues.append(("error", f"{esc(inst.name)}'s library folder {esc(inst.local_root)} is not visible to "
                                        "this service. <a href='/settings/connect'>Connect</a>"))
        if self.auth_allowed and not self.auth_enabled():
            issues.append(("info", "The login is off: anyone who can reach this page can use it. "
                                   "<a href='/settings/general'>General</a>"))
        try:
            with self.db() as c:
                failed = c.execute("SELECT COUNT(*) FROM jobs WHERE state='failed'").fetchone()[0]
            if failed:
                issues.append(("error", f"{failed} conversion(s) failed. <a href='/errors'>Failed</a>"))
        except sqlite3.Error:
            pass
        return issues

    def status(self) -> str:
        import shutil
        with self.db() as c:
            files = dict(c.execute(f"SELECT status, COUNT(*) FROM files WHERE {self.scope} GROUP BY status",
                                   self.scope_args).fetchall())
            notes = c.execute(f"SELECT note, COUNT(*) n FROM files WHERE status='ok' AND {self.scope} "
                              "GROUP BY note ORDER BY n DESC", self.scope_args).fetchall()
            done = c.execute("SELECT * FROM jobs WHERE state='done' AND finished_at IS NOT NULL").fetchall()
        caps = settings_store.read(self.settings.database_path, "capabilities")
        mc5 = mc.get_legacy_converter()
        mc5.apply_capabilities(caps)
        body = ["<h1>Status</h1><h2>Health</h2>"]
        issues = self.health(caps)
        body.append(table(["", "Message"], ([ui.status_icon(k), m] for k, m in issues),
                          "No issues with your configuration."))
        disks = []
        seen = set()
        for label_, path in [(i.local_root, i.local_root) for i in self.settings.instances] + [
                (self.settings.work_dir, self.settings.work_dir)]:
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            key = (usage.total, usage.free)
            if key in seen:
                continue
            seen.add(key)
            used = 100 * (usage.total - usage.free) / usage.total if usage.total else 0
            disks.append([f"<code>{esc(label_)}</code>", gb(usage.free), gb(usage.total),
                          ui.progress(used, f"{used:.0f}%", "bad" if used > 95 else "warn" if used > 85 else "")])
        body.append("<h2>Disk Space</h2>" + table(["Location", "Free Space", "Total Space", ""], disks))
        by_codec, by_mode = Counter(), Counter()
        for r in notes:
            parts = (r["note"] or "").split("-", 2)  # encoded-<codec>-<mode>
            by_codec[(parts[1] if len(parts) > 1 else "?").upper()] += r["n"]
            by_mode[parts[2] if len(parts) > 2 else "unrecorded (older versions)"] += r["n"]
        saved_in = saved_out = 0
        for j in done:
            try:
                if j["source_size"]:
                    saved_out += os.path.getsize(j["output_path"] or j["path"])
                    saved_in += j["source_size"]
            except OSError:
                pass
        durations = sorted(j["finished_at"] - j["started_at"] for j in done if j["started_at"])
        sdr, hdr = mc5.encode_plan(mc5.HDR_SDR), mc5.encode_plan(mc5.HDR10_PQ)
        about = [("Encoding SDR", self._describe(sdr)), ("Encoding HDR", self._describe(hdr)),
                 ("ffmpeg", esc((caps or {}).get("ffmpeg", "not checked yet"))),
                 ("Python", esc(sys.version.split()[0])),
                 ("Configuration", f"<code>{esc(self.config_path)}</code>"),
                 ("Database", f"<code>{esc(self.settings.database_path)}</code>"),
                 ("Log", f"<code>{esc(self.log_path)}</code>")]
        body.append("<h2>About</h2>" + table(["", ""], ([f"<b>{k}</b>", v] for k, v in about)))
        days = Counter(datetime.fromtimestamp(j["finished_at"]).strftime("%m-%d") for j in done
                       if j["finished_at"] > time.time() - 14 * 86400)
        keys = [datetime.fromtimestamp(time.time() - i * 86400).strftime("%m-%d") for i in range(13, -1, -1)]
        peak = max([days.get(k, 0) for k in keys] + [1])
        cards = [("Converted files", files.get("ok", 0)), ("Files with errors", files.get("error", 0)),
                 ("Conversions", len(done)), ("Median time", dur(durations[len(durations) // 2]) if durations else "–"),
                 ("Space saved", f"{100 * (1 - saved_out / saved_in):.0f}%" if saved_in else "–")]
        body.append("<h2>Statistics</h2><div class='stats'>")
        body += [f"<div class='card stat'><div class='n'>{v}</div><div class='k'>{k}</div></div>" for k, v in cards]
        body.append("</div><p class='muted'>Converted per day, last 14 days</p><div class='card bars'>")
        body += [f"<div class='bar' title='{k}: {days.get(k, 0)}'><div class='f' style='height:"
                 f"{100 * days.get(k, 0) / peak:.0f}%'></div><span>{days.get(k, 0) or ''}</span><small>{k[3:]}</small>"
                 "</div>" for k in keys]
        body.append("</div><div style='display:flex;gap:30px;flex-wrap:wrap;margin-top:20px'>")
        for title, counter in (("Library by codec", by_codec), ("Library by HDR mode", by_mode)):
            body.append(f"<div style='flex:1 1 300px'><p class='muted'>{title}</p>"
                        + table(["", "Files"], ([esc(k), str(v)] for k, v in counter.most_common())) + "</div>")
        body.append("</div>")
        return self.page("Status", "".join(body), "/system/status", tool("Refresh", "refresh", href="/system/status"))

    def _describe(self, plan) -> str:
        bits = "10-bit" if plan.ten_bit else "8-bit"
        where = "" if plan.device == plan.backend else f" on {plan.device}"
        return f"{esc(encoders.backend(plan.backend).label)}{esc(where)}, {plan.codec.upper()} {bits}"

    def tasks(self, message: str = "") -> str:
        caps = settings_store.read(self.settings.database_path, "capabilities") or {}
        last_scan = None
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 2_000_000))
                for line in f.read().decode("utf-8", "replace").splitlines():
                    if "Scan summary:" in line:
                        last_scan = line[:16]
        except OSError:
            pass
        hours = int(os.environ.get("SCAN_INTERVAL_HOURS", "168") or 168)
        next_scan = "–"
        if last_scan:
            try:
                next_scan = when(datetime.strptime(last_scan, "%Y-%m-%d %H:%M").timestamp() + hours * 3600)
            except ValueError:
                pass
        def run(action):
            return (f"<form method='post' action='{action}'><button class='btn icon-only' title='Run now'>"
                    f"{icon('refresh', 16)}</button></form>")
        rows = [["Library Scan", f"{hours // 24} days" if hours % 24 == 0 else f"{hours} hours", esc(last_scan or "–"),
                 next_scan, run("/system/tasks/scan")],
                ["Hardware Check", "when ffmpeg or the devices change", when(caps.get("checked_at")), "–",
                 run("/hardware/check")],
                ["Register Webhooks", "manual", "–", "–", run("/settings/register")]]
        body = (ui.alert(esc(message), "ok") if message else "") + "<h1>Tasks</h1><h2>Scheduled</h2>" + table(
            ["Name", "Interval", "Last Execution", "Next Execution", ""], rows)
        return self.page("Tasks", body, "/system/tasks")

    def run_scan(self) -> str:
        script = os.path.join(os.path.dirname(os.path.abspath(mc.__file__)), "media_converterr.py")
        subprocess.Popen([sys.executable, script, "--config", self.config_path, "scan"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return "Library scan started; its result appears in the log."

    def hardware(self, message: str = "") -> str:
        caps = settings_store.read(self.settings.database_path, "capabilities")
        bar = tool("Check again", "refresh", action="/hardware/check")
        body = [ui.alert(esc(message), "ok") if message else "", "<h1>Hardware</h1>"]
        found = gpus.attach_results(gpus.find(), caps)
        body.append("<h2>GPUs</h2>")
        if not found:
            body.append("<p class='muted'>No GPU is visible to this service: conversions run on the CPU. Pass a GPU "
                        "into the container to use it (Intel/AMD: its /dev/dri/renderD* node; NVIDIA: the NVIDIA "
                        "runtime).</p>")
        cards = []
        for g in found:
            fam = g.family
            expected = " ".join(label(x) for x in fam.encodes()) if fam else "<span class='muted'>unknown family</span>"
            proven = " ".join(label(f"{k} ({', '.join(v)})", "ok") for k, v in sorted(g.tested.items())) or \
                "<span class='muted'>none</span>"
            notes = f"<div class='muted'>{esc(fam.notes)}</div>" if fam and fam.notes else ""
            missing = gpus.missing(g)
            warn = (ui.alert(f"Should encode {esc(', '.join(missing))} but no test passed. "
                             f"{esc(gpus.HINTS.get(g.vendor, ''))}", "warn") if missing and caps else "")
            cards.append(f"<div class='card' style='flex:1 1 420px'><div class='title'>{icon('chip')} {esc(g.name)}</div>"
                         f"<div class='muted'>{esc(g.vendor)} · {esc(fam.name if fam else 'family not in the table')}"
                         f"{' · ' + esc(g.render_node) if g.render_node else ''} · PCI {esc(g.vendor_id)}:"
                         f"{esc(g.device_id)}</div><p>Hardware encoders: {expected}</p>{notes}"
                         f"<p>Proven here: {proven}</p>{warn}</div>")
        body.append("<div class='cards'>" + "".join(cards) + "</div>")
        if not caps:
            body.append("<p>Encoders not checked yet.</p>")
        else:
            mc5 = mc.get_legacy_converter()
            mc5.apply_capabilities(caps)  # the worker may have stored a newer check since this process loaded
            sdr_plan, hdr_plan = mc5.encode_plan(mc5.HDR_SDR), mc5.encode_plan(mc5.HDR10_PQ)
            switched = (f" — no GPU here encodes {mc5.SDR_CODEC.upper()}, so SDR uses HEVC on the GPU instead of "
                        "the slower CPU" if sdr_plan.codec != mc5.SDR_CODEC else "")
            body.append(f"<h2>Encoding with</h2>{table(['', ''], [['<b>SDR</b>', self._describe(sdr_plan) + esc(switched)], ['<b>HDR</b>', self._describe(hdr_plan)]])}")
            rows = []
            for r in caps.get("results", []):
                codec = r["codec"].upper() + (" 10-bit" if r.get("ten_bit") else " 8-bit")
                device = "" if r.get("device") == r["backend"] else r.get("device", "")
                rc = f" · {r['rate_control']}" if r.get("rate_control") else ""
                info = "" if r.get("used_for_conversion") else " <span class='muted'>(for information)</span>"
                rows.append([esc(r["label"]), esc(device), esc(codec) + info, f"<code>{esc(r['encoder'])}</code>"
                             f"<span class='muted'>{esc(rc)}</span>", label("yes", "ok") if r["ok"] else label("no", "bad"),
                             f"<span class='muted'>{esc(r['error'])}</span>"])
            lp = caps.get("libplacebo_vulkan", {})
            body.append(f"<h2>Encoder tests</h2><p class='muted'>{esc(caps.get('ffmpeg'))} · devices: "
                        f"{esc(', '.join(caps.get('devices', [])) or 'none')} · checked {when(caps.get('checked_at'))}"
                        "</p><p class='muted'>Each encoder is proven with a short test encode: ffmpeg lists encoders "
                        "whose hardware or driver is missing.</p>"
                        + table(["Encoder", "Device", "Format", "ffmpeg encoder", "Works", "Error"], rows)
                        + f"<p>Dolby Vision profile 5 rendering (libplacebo on Vulkan): "
                        f"{label('works', 'ok') if lp.get('ok') else label('unavailable', 'bad') + ' ' + esc(lp.get('error'))}</p>")
        return self.page("Hardware", "".join(body), "/hardware", bar)

    def check_hardware(self) -> str:
        caps = encoders.detect()
        self.put("capabilities", caps)
        self.reload()
        return f"Checked: {sum(r['ok'] for r in caps['results'])} of {len(caps['results'])} encoder tests passed."

    def log(self) -> str:
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 60000))
                lines = f.read().decode("utf-8", "replace").splitlines()[-300:]
        except OSError as error:
            lines = [f"log not readable: {error}"]
        rows = []
        for line in reversed([line for line in lines if not line.startswith("libva info")]):
            level = next((lv for lv in ("ERROR", "WARNING", "INFO") if f" {lv} " in line[:40]), "")
            kind = {"ERROR": "error", "WARNING": "warning", "INFO": "info"}.get(level, "info")
            message = line[24 + len(level):] if level else line
            rows.append([ui.status_icon(kind, level.title()), f"<span class='muted' style='white-space:nowrap'>"
                         f"{esc(line[:19])}</span>", f"<pre>{esc(message)}</pre>"])
        return self.page("Logs", "<h1>Logs</h1>" + table(["", "Time", "Message"], rows), "/log",
                         tool("Refresh", "refresh", href="/log"), refresh=15)

    # ---- login --------------------------------------------------------------------------------------------------------
    def auth_record(self) -> dict | None:
        conn = self.store()
        try:
            return settings_store.get(conn, "auth")
        finally:
            conn.close()

    def session_key(self) -> str:
        conn = self.store()
        try:
            return settings_store.secret(conn, "session_key")
        finally:
            conn.close()

    def auth_enabled(self) -> bool:
        record = self.auth_record() if self.auth_allowed else None
        return bool(record and not record.get("disabled"))

    def needs_setup(self) -> bool:
        return self.auth_allowed and self.auth_record() is None

    def set_login_disabled(self) -> None:
        self.put("auth", {"disabled": True})

    def login_page(self, message: str = "", setup: bool = False) -> str:
        title = "Create the login" if setup else "Log in"
        intro = ("<p class='muted'>Choose a user name and password for this web interface (at least 10 characters), "
                 "or continue without a login if only trusted people can reach it. Either can be changed later in "
                 "Settings › General.</p>" if setup else "")
        confirm = (form_group("Repeat password", "<input type='password' name='password2' required "
                                                  "autocomplete='new-password'>") if setup else "")
        skip = ("<form method='post' action='/setup' style='margin-top:12px'><input type='hidden' name='skip' "
                "value='1'><button class='btn' style='width:100%'>Continue without a login</button></form>"
                if setup else "")
        error = ui.alert(esc(message), "bad") if message else ""
        inner = (f"{error}{intro}<form method='post' action='{'/setup' if setup else '/login'}'>"
                 + form_group("Username", "<input name='user' required autocomplete='username'>")
                 + form_group("Password", "<input type='password' name='password' required "
                                          f"autocomplete='{'new-password' if setup else 'current-password'}'>")
                 + f"{confirm}<button class='btn primary' style='width:100%'>{title}</button></form>{skip}")
        return ui.bare(title, ui.panel("media-converterr", inner))

    def create_login(self, form: dict) -> str | None:
        """Error message, or None when the account was created."""
        user = form.get("user", [""])[0].strip()
        password = form.get("password", [""])[0]
        if not user or len(password) < 10:
            return "Enter a user name and a password of at least 10 characters."
        if password != form.get("password2", [""])[0]:
            return "The passwords differ."
        conn = self.store()
        try:
            current = settings_store.get(conn, "auth")
            if current and not current.get("disabled") and not form.get("replace"):
                return "An account already exists."
            settings_store.put(conn, "auth", {"user": user, **settings_store.hash_password(password)})
        finally:
            conn.close()
        return None

    # ---- Settings -----------------------------------------------------------------------------------------------------
    def settings_index(self) -> str:
        items = [("/settings/connect", "Connect", "Sonarr, Radarr and Jellyfin: addresses, API keys, library folders "
                                                  "and the webhooks that tell this service about new files"),
                 ("/settings/conversion", "Conversion", "Encoder, output codecs, quality, maximum resolution, which "
                                                        "sources are kept as they are, and languages"),
                 ("/settings/general", "General", "Login for this web interface"),
                 ("/hardware", "Hardware", "The GPUs found and which encoders work on them")]
        links = "".join(f"<a class='item' href='{href}'><div class='t'>{esc(title)}</div><div class='d'>{esc(text)}"
                        f"</div></a>" for href, title, text in items)
        return self.page("Settings", f"<h1>Settings</h1><div class='settings-list'>{links}</div>", "/settings",
                         ui.advanced_toggle())

    def hook_token(self) -> str:
        conn = self.store()
        try:
            return settings_store.secret(conn, "hook_token")
        finally:
            conn.close()

    def stored_connections(self) -> dict:
        return settings_store.read(self.settings.database_path, "connections", {}) or {}

    def current_connections(self) -> dict:
        """The effective connections as a stored document (saved keys kept; empty means: the file's key)."""
        stored = self.stored_connections()
        keys = {item.get("name"): item.get("api_key", "") for item in stored.get("instances", [])}
        jf = self.settings.jellyfin
        return {"instances": [{"name": i.name, "type": i.kind, "url": i.url, "api_key": keys.get(i.name, ""),
                               "remote_root": i.remote_root, "local_root": i.local_root} for i in self.settings.instances],
                "jellyfin": ({"url": jf.url, "api_key": (stored.get("jellyfin") or {}).get("api_key", ""),
                              "paths": [{"local": a, "remote": b} for a, b in jf.paths]} if jf else None),
                "public_url": self.settings.public_url}

    def store_connections(self, connections: dict) -> str | None:
        try:
            mc.apply_stored_connections(mc.load_settings(self.config_path, use_store=False), connections)
        except mc.ConfigurationError as error:
            return str(error)
        self.put("connections", connections)
        self.reload()
        return None

    def connect_page(self, results: list[tuple[str, bool, str]] | None = None, error: str = "") -> str:
        s = self.settings
        token = self.hook_token()
        body = []
        if error:
            body.append(ui.alert(esc(error), "bad"))
        for what, ok, text in results or []:
            body.append(ui.alert(f"<b>{esc(what)}</b>: {esc(text)}", "ok" if ok else "bad"))
        body.append("<h1>Connect</h1><h2>Sonarr and Radarr</h2><div class='cards'>")
        dialogs = []
        for n, inst in enumerate(s.instances):
            events = "".join(label(e, "ok" if s.public_url else "outline") for e in ("On Import", "On Upgrade",
                                                                                         "On Rename", "On Delete"))
            body.append(f"<button class='card click' type='button' data-open='inst{n}'><div class='title'>"
                        f"{esc(inst.name)}</div><div>{label(inst.kind.title(), 'info')}</div><div>{events}</div>"
                        f"</button>")
            dialogs.append(self._instance_dialog(f"inst{n}", inst))
        body.append(f"<button class='card click add' type='button' data-open='inst-new'>{icon('plus', 45)}</button>"
                    "</div>")
        dialogs.append(self._instance_dialog("inst-new", None))
        jf = s.jellyfin
        body.append("<h2>Jellyfin</h2><div class='cards'>")
        if jf:
            body.append(f"<button class='card click' type='button' data-open='jf'><div class='title'>Jellyfin</div>"
                        f"<div>{label('Emby / Jellyfin', 'info')}</div><div>{label('On Convert', 'ok')}"
                        f"{label('On Subtitles', 'ok')}{label('On Rename', 'ok')}</div></button>")
        else:
            body.append(f"<button class='card click add' type='button' data-open='jf'>{icon('plus', 45)}</button>")
        body.append("</div>")
        dialogs.append(self._jellyfin_dialog())
        rows = [[esc(i.name), f"<code>{esc(mc.webhook_url(s, i, token))}</code>"] for i in s.instances] \
            if s.public_url else []
        body.append("<h2>Webhooks</h2><form method='post' action='/settings/connect/address'>"
                    + form_group("Address of this service",
                                 ui.text_input("public_url", s.public_url, placeholder="http://media-converterr:8090"),
                                 "How Sonarr and Radarr reach this web interface (for example a container name on a "
                                 "shared Docker network). Register Webhooks then creates or updates a Webhook "
                                 f"connection named <code>{mc.WEBHOOK_NAME}</code> in each of them (import, upgrade, "
                                 "rename and delete events). Remove an older Custom Script connection to this "
                                 "service, or every event arrives twice.")
                    + form_group("", f"<button class='btn primary'>{icon('save', 16)} Save address</button>")
                    + "</form>" + (table(["Instance", "Webhook URL"], rows) if rows else ""))
        bar = (tool("Test All", "check", action="/settings/test") + tool("Register Webhooks", "link",
                                                                        action="/settings/register"))
        return self.page("Connect", "".join(body) + "".join(dialogs), "/settings/connect", bar)

    def _instance_dialog(self, dialog_id: str, inst) -> str:
        v = (lambda attr: getattr(inst, attr) if inst else "")
        fields = (f"<input type='hidden' name='original' value='{esc(v('name'))}'>"
                  + form_group("Name", ui.text_input("name", v("name"), placeholder="sonarr", attrs="required"))
                  + form_group("Type", ui.select("type", [("sonarr", "Sonarr"), ("radarr", "Radarr")],
                                                 v("kind") or "sonarr"))
                  + form_group("URL", ui.text_input("url", v("url"), placeholder="http://sonarr:8989"))
                  + form_group("API Key", ui.text_input("api_key", kind="password",
                                                        placeholder="saved" if inst and inst.api_key else "",
                                                        attrs="autocomplete='off'"),
                               "Settings › General in Sonarr/Radarr. Leave empty to keep the saved key.")
                  + form_group("Remote root", ui.text_input("remote_root", v("remote_root"), placeholder="/tv"),
                               "The library folder as Sonarr/Radarr see it.")
                  + form_group("Local root", ui.text_input("local_root", v("local_root"), placeholder="/media/series"),
                               "The same folder as this service sees it."))
        delete_fields = f"<input type='hidden' name='name' value='{esc(v('name'))}'>"
        return ui.dialog(dialog_id, f"Edit {inst.name}" if inst else "Add Sonarr or Radarr", fields,
                         action="/settings/connect/instance", test_url="/settings/connect/test-one?kind=arr",
                         delete_action="/settings/connect/instance/delete" if inst else "", delete_fields=delete_fields)

    def _jellyfin_dialog(self) -> str:
        jf = self.settings.jellyfin
        paths = "\n".join(f"{a} = {b}" for a, b in (jf.paths if jf else ()))
        fields = (form_group("URL", ui.text_input("url", jf.url if jf else "", placeholder="http://jellyfin:8096"))
                  + form_group("API Key", ui.text_input("api_key", kind="password", placeholder="saved" if jf else "",
                                                        attrs="autocomplete='off'"),
                               "Dashboard › API Keys in Jellyfin. Leave empty to keep the saved key.")
                  + form_group("Paths", f"<textarea name='paths' rows='3'>{esc(paths)}</textarea>",
                               "One <code>folder here = same folder as Jellyfin sees it</code> per line."))
        return ui.dialog("jf", "Jellyfin", fields, action="/settings/connect/jellyfin",
                         test_url="/settings/connect/test-one?kind=jellyfin",
                         delete_action="/settings/connect/jellyfin/delete" if jf else "")

    def save_instance(self, form: dict) -> str | None:
        conns = self.current_connections()
        original, name_ = first(form, "original"), first(form, "name")
        old = next((i for i in conns["instances"] if i["name"] == original), None) if original else None
        entry = {"name": name_, "type": first(form, "type"), "url": first(form, "url").rstrip("/"),
                 "api_key": first(form, "api_key") or (old or {}).get("api_key", ""),
                 "remote_root": first(form, "remote_root"), "local_root": first(form, "local_root")}
        if old and not first(form, "api_key") and not entry["api_key"] and name_ != original:
            # renamed and its key came from the file under the old name: keep it under the new name
            try:
                entry["api_key"] = self.settings.instance(original).api_key
            except mc.ConfigurationError:
                pass
        rest = [i for i in conns["instances"] if i["name"] != original] if old else conns["instances"]
        conns["instances"] = rest + [entry]
        return self.store_connections(conns)

    def delete_instance(self, form: dict) -> str | None:
        conns = self.current_connections()
        conns["instances"] = [i for i in conns["instances"] if i["name"] != first(form, "name")]
        if not conns["instances"]:
            return "At least one Sonarr or Radarr instance is needed."
        return self.store_connections(conns)

    def save_jellyfin(self, form: dict) -> str | None:
        conns = self.current_connections()
        pairs = []
        for line in form.get("paths", [""])[0].splitlines():
            if line.strip():
                if "=" not in line:
                    return f"A Jellyfin path line needs 'local = remote': {line}"
                local, remote = (part.strip() for part in line.split("=", 1))
                pairs.append({"local": local, "remote": remote})
        if not first(form, "url"):
            return "Enter Jellyfin's URL."
        conns["jellyfin"] = {"url": first(form, "url").rstrip("/"), "paths": pairs,
                             "api_key": first(form, "api_key") or (conns["jellyfin"] or {}).get("api_key", "")}
        return self.store_connections(conns)

    def delete_jellyfin(self) -> str | None:
        conns = self.current_connections()
        conns["jellyfin"] = None
        return self.store_connections(conns)

    def save_address(self, form: dict) -> str | None:
        address = first(form, "public_url").rstrip("/")
        if address and not address.startswith(("http://", "https://")):
            return "The address must start with http:// or https://"
        conns = self.current_connections()
        conns["public_url"] = address
        return self.store_connections(conns)

    def save_settings(self, form: dict) -> str | None:
        """The connections as one form (fields i<n>-name/type/url/key/remote/local, jf-*, public_url): the form of
        earlier versions, kept for scripts."""
        previous = self.stored_connections()
        saved_keys = {item.get("name"): item.get("api_key", "") for item in previous.get("instances", [])}
        instances = []
        for i in range(min(int(first(form, "count") or 0), 50)):
            name_ = first(form, f"i{i}-name")
            if not name_ or form.get(f"i{i}-delete"):
                continue
            instances.append({"name": name_, "type": first(form, f"i{i}-type"), "url": first(form, f"i{i}-url"),
                              "api_key": first(form, f"i{i}-key") or saved_keys.get(name_, ""),
                              "remote_root": first(form, f"i{i}-remote"), "local_root": first(form, f"i{i}-local")})
        jellyfin = None
        if first(form, "jf-url"):
            pairs = []
            for line in form.get("jf-paths", [""])[0].splitlines():
                if line.strip():
                    if "=" not in line:
                        return f"Jellyfin path line needs 'local = remote': {line}"
                    local, remote = (part.strip() for part in line.split("=", 1))
                    pairs.append({"local": local, "remote": remote})
            jellyfin = {"url": first(form, "jf-url"), "paths": pairs,
                        "api_key": first(form, "jf-key") or (previous.get("jellyfin") or {}).get("api_key", "")}
        address = first(form, "public_url").rstrip("/")
        if address and not address.startswith(("http://", "https://")):
            return "The webhook address must start with http:// or https://"
        return self.store_connections({"instances": instances, "jellyfin": jellyfin, "public_url": address})

    def test_one(self, kind: str, form: dict) -> tuple[bool, str]:
        """Test the values in an edit dialog before saving (an empty key: the saved or file key)."""
        try:
            if kind == "jellyfin":
                saved = self.settings.jellyfin
                key = first(form, "api_key") or (saved.api_key if saved else "")
                return True, mc.jellyfin_status(mc.JellyfinSettings(first(form, "url").rstrip("/"), key, ()),
                                                self.settings)
            original = first(form, "original")
            key = first(form, "api_key")
            if not key and original:
                try:
                    key = self.settings.instance(original).api_key
                except mc.ConfigurationError:
                    key = ""
            inst = mc.ArrInstance(first(form, "name") or "test", first(form, "type"), first(form, "url").rstrip("/"),
                                  key, first(form, "remote_root") or "/", first(form, "local_root") or "/")
            return True, mc.arr_status(inst, self.settings)
        except (RuntimeError, mc.ConfigurationError, ValueError) as error:
            return False, str(error)

    def test_connections(self) -> list[tuple[str, bool, str]]:
        results = []
        for inst in self.settings.instances:
            try:
                results.append((inst.name, True, mc.arr_status(inst, self.settings)))
            except (RuntimeError, mc.ConfigurationError) as error:
                results.append((inst.name, False, str(error)))
        if self.settings.jellyfin:
            try:
                results.append(("Jellyfin", True, mc.jellyfin_status(self.settings.jellyfin, self.settings)))
            except RuntimeError as error:
                results.append(("Jellyfin", False, str(error)))
        return results or [("Connections", False, "none configured")]

    def register_webhooks(self) -> list[tuple[str, bool, str]]:
        results, token = [], self.hook_token()
        for inst in self.settings.instances:
            try:
                done = mc.register_webhook(inst, self.settings, mc.webhook_url(self.settings, inst, token))
                others = mc.other_hook_connections(inst, self.settings)
                text = f"webhook {done}" + (f"; also connected through {', '.join(others)}: remove that one, or "
                                            "every event is queued twice" if others else "")
                results.append((inst.name, not others, text))
            except (RuntimeError, mc.ConfigurationError) as error:
                results.append((inst.name, False, str(error)))
        return results or [("Webhooks", False, "no Sonarr or Radarr configured")]

    def receive_hook(self, instance_name: str, token: str, body: bytes) -> tuple[int, str]:
        """Queue one Sonarr/Radarr webhook call; (HTTP status, message)."""
        if not hmac.compare_digest(token, self.hook_token()):
            return 403, "bad token"
        try:
            instance = self.settings.instance(instance_name)
        except mc.ConfigurationError:
            return 404, "unknown instance"
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return 400, "invalid JSON"
        if not isinstance(payload, dict):
            return 400, "invalid payload"
        event_id = mc.capture_hook_event(self.settings.database_path, instance.name,
                                         mc.webhook_environ(instance.kind, payload))
        return 200, "test ok" if event_id is None else f"queued {event_id}"

    def conversion_page(self, error: str = "", saved: bool = False) -> str:
        mc5 = mc.get_legacy_converter()
        c = mc5.validate_conversion_settings(settings_store.read(self.settings.database_path, "conversion") or {})
        g = form_group
        body = [ui.alert(esc(error), "bad") if error else "", ui.alert("Saved.", "ok") if saved else "",
                "<h1>Conversion</h1><p class='muted'>Applies to files converted from now on; files already converted "
                "are not redone.</p>"
                "<form method='post' action='/settings/conversion' id='conversion'><h2>Encoding</h2>",
                g("Encoder", ui.select("backend", [("auto", "Automatic")] + [
                    (n, encoders.backend(n).label) for n in encoders.conversion_backends()], c["backend"]),
                  "Automatic: a GPU that passed its test (Intel QSV, NVIDIA, VAAPI, AMD AMF, in that order), else the "
                  "CPU. See <a href='/hardware'>Hardware</a> for what was found. NVIDIA and AMD AMF are untested."),
                g("SDR codec", ui.select("sdr_codec", [("av1", "AV1"), ("hevc", "HEVC"), ("h264", "H.264")],
                                         c["sdr_codec"]),
                  "For SDR sources and HDR tone mapped to SDR. AV1 is the smallest; H.264 plays everywhere but is "
                  "the largest."),
                g("HDR codec", ui.select("hdr_codec", [("hevc", "HEVC"), ("av1", "AV1")], c["hdr_codec"]),
                  "For HDR10 and HLG output (10-bit). HDR10+ and Dolby Vision output always use HEVC: no tool can "
                  "carry them in AV1 yet."),
                g("When the GPU lacks the codec", ui.select("codec_fallback", [
                    ("gpu", "Use another codec on the GPU"), ("cpu", "Keep the codec, encode on the CPU")],
                    c["codec_fallback"]),
                  "Another codec: HEVC, then H.264 for SDR (fast, larger files on older GPUs). The CPU keeps the "
                  "codec but is much slower at 4K."),
                g("AV1 quality", ui.text_input("av1_quality", c["av1_quality"], kind="number",
                                               attrs="min='1' max='51'"),
                  "GPU <code>global_quality</code>: lower is better and larger. Default 20."),
                g("HEVC quality", ui.text_input("hevc_quality", c["hevc_quality"], kind="number",
                                                attrs="min='1' max='51'"),
                  "Default 16, measured on Intel to match AV1 at 20 (the numbers mean different things on each "
                  "encoder)."),
                g("Maximum resolution", ui.select("max_height", [(h, f"{w}×{h}") for h, (w, _) in sorted(
                    mc5.MAX_RESOLUTIONS.items(), reverse=True)], c["max_height"]), "Larger sources are scaled down."),
                g("Encoder preset", ui.select("preset", [(p, p) for p in mc5.QSV_PRESETS], c["preset"]),
                  "Slower presets compress better. Default slow.", advanced=True),
                g("H.264 quality", ui.text_input("h264_quality", c["h264_quality"], kind="number",
                                                 attrs="min='1' max='51'"),
                  "GPU H.264 quality. Provisional default 21.", advanced=True),
                g("CPU AV1 quality", ui.text_input("software_av1_crf", c["software_av1_crf"], kind="number",
                                                   attrs="min='0' max='51'"),
                  "SVT-AV1 CRF when encoding on the CPU. Provisional default 25, not yet calibrated.", advanced=True),
                g("CPU HEVC quality", ui.text_input("software_hevc_crf", c["software_hevc_crf"], kind="number",
                                                    attrs="min='0' max='51'"),
                  "x265 CRF. Provisional default 18.", advanced=True),
                g("CPU H.264 quality", ui.text_input("software_h264_crf", c["software_h264_crf"], kind="number",
                                                     attrs="min='0' max='51'"),
                  "x264 CRF. Provisional default 19.", advanced=True),
                g("Keyframe interval", ui.text_input("keyframe_seconds", c["keyframe_seconds"], kind="number",
                                                     attrs="min='1' max='20'"),
                  "Seconds between keyframes; seeking jumps to them. 2 suits streaming; 5–10 saves a few percent "
                  "for files played from your own disk."),
                g("Look-ahead", ui.text_input("lookahead", c["lookahead"], kind="number", attrs="min='0' max='250'"),
                  "Frames the encoder looks ahead to spread bits (QSV look_ahead_depth; NVENC uses up to 32, "
                  "SVT-AV1 up to 120). More can help quality slightly, at some speed and memory.", advanced=True),
                g("Deinterlace", ui.checkbox("deinterlace", c["deinterlace"], "Deinterlace interlaced sources"),
                  "Sources flagged interlaced (DVDs, older TV) are deinterlaced on the GPU where possible, one frame "
                  "per frame; progressive sources are untouched."),
                g("Crop black bars", ui.checkbox("crop", c["crop"], "Remove black bars"),
                  "Measured at five points of the film. Saves bits on letterboxed sources; never applied to Dolby "
                  "Vision output (its metadata describes the full frame). On VAAPI and NVIDIA, cropping decodes on "
                  "the CPU."),
                "<h2>Dynamic range</h2>",
                g("HDR10 and HDR10+ sources", ui.select("output_hdr10", [
                    ("hdr10plus", "HDR10, keeping HDR10+"), ("hdr10", "HDR10 (drop HDR10+)"), ("sdr", "SDR")],
                    c["output_hdr10"]), "HDR10+ needs HEVC."),
                g("HLG sources", ui.select("output_hlg", [("hlg", "HLG"), ("sdr", "SDR")], c["output_hlg"])),
                g("Dolby Vision sources (profiles 7, 8)", ui.select("output_dv", [
                    ("dv81", "Dolby Vision profile 8.1 (keeping HDR10+ too)"),
                    ("hdr10plus", "HDR10 from the base layer, keeping HDR10+"), ("hdr10", "HDR10 from the base layer"),
                    ("sdr", "SDR")], c["output_dv"]),
                  "Dolby Vision 8.1 keeps the source's Dolby Vision metadata (dovi_tool), needs HEVC and an unscaled "
                  "picture; players without Dolby Vision show its HDR10 base. Profile 7's enhancement layer is "
                  "dropped either way."),
                g("Dolby Vision profile 5 sources", ui.select("output_dv5", [("hdr10", "HDR10 (rendered)"),
                                                                             ("sdr", "SDR")], c["output_dv5"]),
                  "Profile 5 has no HDR10 base: it is rendered with libplacebo."),
                g("Tone mapping to SDR", ui.select("tonemap", [("quality", "Quality (libplacebo)"),
                                                               ("fast", "Fast (GPU video processing)")], c["tonemap"]),
                  "Quality keeps shadow detail (about 0.2× real time at 4K on Intel Arc); Fast is many times quicker "
                  "but can crush dark scenes (Intel/AMD only; profile 5 always uses Quality).", advanced=True),
                "<h2>Keeping sources</h2>",
                g("Keep efficient sources", ui.checkbox("remux_enabled", c["remux_enabled"], "Remux instead of encoding"),
                  "HEVC or AV1 sources at or below the limit are kept as they are (tracks still filtered): "
                  "re-encoding them can only lose quality. Dolby Vision is never kept."),
                g("Remux limit", ui.text_input("remux_mbps_per_mpx", f"{c['remux_mbps_per_mpx']:g}", kind="number",
                                               attrs="step='0.1' min='0' max='50'"),
                  "Mbit/s per megapixel, measured on the picture without black bars. 1.8 ≈ 15 Mbit/s for a full 4K "
                  "frame, 3.7 for 1080p.", advanced=True),
                "<h2>Audio</h2>",
                g("Audio languages", ui.text_input("audio_languages", ", ".join(c["audio_languages"])),
                  "Comma-separated codes (en, eng, nl, pt-BR…); empty keeps every language. The original-language "
                  "track is always kept."),
                "<h2>Subtitles</h2>",
                g("Subtitle languages", ui.text_input("subtitle_languages", ", ".join(c["subtitle_languages"])),
                  "Comma-separated codes; empty keeps every language. Other languages are dropped."),
                g("Subtitles", ui.select("subtitle_mode", [("extract", "Extract next to the video"),
                                                           ("embed", "Keep in the container")], c["subtitle_mode"]),
                  "Extracted subtitles are files beside the video with Bazarr's naming (<code>name.en.srt</code>, "
                  "<code>name.pt-BR.hi.srt</code>); a release subtitle replaces a downloaded one (kept as a backup) "
                  "and forced tracks are merged into the full track of their language. Kept in the container: "
                  "copied as they are (MP4 text becomes SRT)."),
                g("Bitmap subtitles", ui.select("bitmap_subtitles", [
                    ("ocr", "Convert to SRT (OCR)"), ("sidecar", "Extract as they are (.sup, .idx/.sub)"),
                    ("drop", "Drop")], c["bitmap_subtitles"]),
                  "Blu-ray (PGS) and DVD (VobSub) subtitles are pictures. OCR (Tesseract) makes searchable, "
                  "restylable text; results below its confidence threshold are kept aside, not installed. Only "
                  "applies when extracting."),
                "</form>"]
        bar = tool("Save Changes", "save", form="conversion") + "<span class='sep'></span>" + ui.advanced_toggle()
        return self.page("Conversion", "".join(body), "/settings/conversion", bar)

    def save_conversion(self, form: dict) -> str | None:
        keys = ("hdr_codec", "codec_fallback", "output_hdr10", "output_hlg", "output_dv", "output_dv5", "tonemap",
                "h264_quality", "software_h264_crf", "subtitle_mode", "bitmap_subtitles")
        values = {**{k: first(form, k) or None for k in keys},
                  "backend": first(form, "backend") or None, "software_av1_crf": first(form, "software_av1_crf") or None,
                  "software_hevc_crf": first(form, "software_hevc_crf") or None, "sdr_codec": first(form, "sdr_codec"),
                  "av1_quality": first(form, "av1_quality") or None, "hevc_quality": first(form, "hevc_quality") or None,
                  "preset": first(form, "preset") or None, "max_height": first(form, "max_height") or None,
                  "remux_enabled": bool(form.get("remux_enabled")),
                  "deinterlace": bool(form.get("deinterlace")), "crop": bool(form.get("crop")),
                  "keyframe_seconds": first(form, "keyframe_seconds") or None,
                  "lookahead": first(form, "lookahead") or None,
                  "remux_mbps_per_mpx": first(form, "remux_mbps_per_mpx") or None,
                  "audio_languages": first(form, "audio_languages").replace(";", ",").split(","),
                  "subtitle_languages": first(form, "subtitle_languages").replace(";", ",").split(",")}
        if not values["sdr_codec"]:
            values["sdr_codec"] = None
        try:
            values = mc.get_legacy_converter().validate_conversion_settings(values)
        except (ValueError, TypeError) as error:
            return str(error)
        self.put("conversion", values)
        self.reload()
        return None

    def general_page(self, error: str = "", message: str = "") -> str:
        body = [ui.alert(esc(error), "bad") if error else "", ui.alert(esc(message), "ok") if message else "",
                "<h1>General</h1><h2>Security</h2>"]
        login = ("<form method='post' action='/settings/login' id='login'>"
                 + form_group("User", "<input name='user' required autocomplete='username'>")
                 + form_group("Password", "<input type='password' name='password' required autocomplete='new-password'>",
                              "At least 10 characters.")
                 + form_group("Repeat password", "<input type='password' name='password2' required "
                                                 "autocomplete='new-password'>"))
        if not self.auth_allowed:
            body.append(form_group("Login", label("Off", "warn"), "Turned off by WEB_AUTH=off (an authenticating "
                                                                  "proxy in front is expected)."))
        elif self.auth_enabled():
            body.append(form_group("Login", label("On", "ok"), "Set a new user name and password below, or turn the "
                                                               "login off."))
            body.append(login + form_group("", "<button class='btn primary' name='action' value='set'>"
                                               "Change login</button>") + "</form>")
            body.append("<form method='post' action='/settings/login'>" + form_group(
                "", "<button class='btn danger' name='action' value='off'>Turn the login off</button>",
                "Anyone who can reach this page can then use it, settings included.") + "</form>")
        else:
            body.append(form_group("Login", label("Off", "warn"), "Anyone who can reach this page can use it, settings "
                                                                  "included."))
            body.append(login + form_group("", "<button class='btn primary' name='action' value='set'>Turn the login "
                                               "on</button>") + "</form>")
        return self.page("General", "".join(body), "/settings/general")


def make_handler(dash: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: str, headers: dict | None = None, content_type: str = "text/html"):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str, headers: dict | None = None):
            self._send(303, "", {"Location": location, **(headers or {})})

        def _form(self) -> dict:
            length = min(int(self.headers.get("Content-Length") or 0), MAX_FORM)
            return parse_qs(self.rfile.read(length).decode("utf-8", "replace"))

        def _user(self) -> str | None:
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            token = cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else None
            return settings_store.check_session(dash.session_key(), token)

        def _session_headers(self, user: str) -> dict:
            token = settings_store.make_session(dash.session_key(), user)
            return {"Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
                                  f"Max-Age={settings_store.SESSION_SECONDS}"}

        def _same_origin(self) -> bool:
            """Refuse form posts from other sites. Browsers mark them with Sec-Fetch-Site and Origin; a request
            without either (a script, an old browser) is allowed, the login still applies."""
            site = self.headers.get("Sec-Fetch-Site")
            if site:
                return site in ("same-origin", "none")
            origin = self.headers.get("Origin")
            return not origin or urlparse(origin).netloc == self.headers.get("Host")

        def _gate(self, path: str) -> bool:
            """True when the request may go on; otherwise the login or setup redirect has been sent."""
            if dash.needs_setup():
                if path == "/setup":
                    return True
                self._redirect("/setup")
                return False
            if not dash.auth_enabled():
                if path in ("/login", "/setup"):
                    self._redirect("/")
                    return False
                return True
            if path == "/login" or self._user():
                return True
            self._redirect("/login")
            return False

        def do_GET(self):
            url = urlparse(self.path)
            if url.path.startswith("/hook/"):
                self._send(405, "use POST", content_type="text/plain")
                return
            if not self._gate(url.path):
                return
            query = parse_qs(url.query)
            routes = {"/": dash.queue, "/queue": dash.queue, "/events": dash.events, "/errors": dash.errors,
                      "/log": dash.log, "/settings": dash.settings_index, "/settings/connect": dash.connect_page,
                      "/settings/conversion": dash.conversion_page, "/settings/general": dash.general_page,
                      "/system/status": dash.status, "/stats": dash.status, "/system/tasks": dash.tasks,
                      "/hardware": dash.hardware}
            try:
                if url.path == "/setup":
                    self._send(200, dash.login_page(setup=True))
                elif url.path == "/login":
                    self._send(200, dash.login_page())
                elif url.path == "/history":
                    self._send(200, dash.history(query))
                elif url.path == "/files":
                    self._send(200, dash.files(query))
                elif url.path in routes:
                    self._send(200, routes[url.path]())
                else:
                    self._send(404, dash.page("Not found", "<h1>Not found</h1>", url.path))
            except sqlite3.Error as error:
                self._send(503, dash.page("Busy", f"<h1>Database busy</h1><p>{esc(error)}</p>", url.path))

        def do_POST(self):
            url = urlparse(self.path)
            parts = url.path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "hook":
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_HOOK:
                    self._send(413, "too large", content_type="text/plain")
                    return
                token = parse_qs(url.query).get("token", [""])[0]
                status, text = dash.receive_hook(parts[1], token, self.rfile.read(length))
                self._send(status, text, content_type="text/plain")
                return
            if not self._same_origin():
                self._send(403, ui.bare("Refused", ui.alert("Cross-site request refused.", "bad")))
                return
            if not self._gate(url.path):
                return
            path = url.path
            if path == "/setup":
                form = self._form()
                if form.get("skip"):
                    dash.set_login_disabled()
                    self._redirect("/settings")
                    return
                error = dash.create_login(form)
                if error:
                    self._send(400, dash.login_page(error, setup=True))
                else:
                    self._redirect("/settings", self._session_headers(form["user"][0].strip()))
            elif path == "/login":
                form = self._form()
                user = form.get("user", [""])[0].strip()
                if settings_store.verify_password(dash.auth_record(), user, form.get("password", [""])[0]):
                    self._redirect("/", self._session_headers(user))
                else:
                    time.sleep(1)
                    self._send(401, dash.login_page("Wrong user name or password."))
            elif path == "/logout":
                self._redirect("/login", {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0"})
            elif path == "/settings":  # whole connections form (earlier versions, scripts)
                error = dash.save_settings(self._form())
                self._send(400 if error else 200, dash.connect_page(
                    results=None if error else [("Settings", True, "saved")], error=error or ""))
            elif path in ("/settings/connect/instance", "/settings/connect/instance/delete",
                          "/settings/connect/jellyfin", "/settings/connect/jellyfin/delete",
                          "/settings/connect/address"):
                form = self._form()
                action = {"/settings/connect/instance": dash.save_instance,
                          "/settings/connect/instance/delete": dash.delete_instance,
                          "/settings/connect/jellyfin": dash.save_jellyfin,
                          "/settings/connect/jellyfin/delete": lambda _form: dash.delete_jellyfin(),
                          "/settings/connect/address": dash.save_address}[path]
                error = action(form)
                if error:
                    self._send(400, dash.connect_page(error=error))
                else:
                    self._redirect("/settings/connect")
            elif path == "/settings/connect/test-one":
                ok, message = dash.test_one(parse_qs(url.query).get("kind", ["arr"])[0], self._form())
                self._send(200, json.dumps({"ok": ok, "message": message}), content_type="application/json")
            elif path == "/settings/test":
                self._send(200, dash.connect_page(results=dash.test_connections()))
            elif path == "/settings/register":
                self._send(200, dash.connect_page(results=dash.register_webhooks()))
            elif path == "/settings/conversion":
                error = dash.save_conversion(self._form())
                self._send(400 if error else 200, dash.conversion_page(error=error or "", saved=not error))
            elif path == "/settings/login":
                form = self._form()
                if form.get("action", [""])[0] == "off":
                    dash.set_login_disabled()
                    self._redirect("/settings/general", {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0"})
                    return
                error = dash.create_login({**form, "replace": ["1"]})
                if error:
                    self._send(400, dash.general_page(error=error))
                else:
                    self._redirect("/settings/general", self._session_headers(form["user"][0].strip()))
            elif path == "/hardware/check":
                self._send(200, dash.hardware(dash.check_hardware()))
            elif path == "/system/tasks/scan":
                self._send(200, dash.tasks(dash.run_scan()))
            elif len(parts) == 2 and parts[0] == "retry" and parts[1].isdigit():
                dash.retry(int(parts[1]))
                self._redirect("/queue")
            elif parts == ["reencode"]:
                job = dash.reencode(self._form().get("path", [""])[0])
                if job is None:
                    self._send(400, dash.page("Not allowed", "<h1>Not allowed</h1><p>Only existing files inside the "
                                                             "configured library folders can be re-encoded.</p>", "/files"))
                else:
                    self._redirect("/queue")
            else:
                self._send(404, dash.page("Not found", "<h1>Not found</h1>", path))

        def log_message(self, *args):  # keep the container log for the converter
            pass
    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="media-converterr web interface")
    parser.add_argument("--config", default=mc.DEFAULT_CONFIG_PATH)
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8090")))
    parser.add_argument("--bind", default=os.environ.get("WEB_BIND", "0.0.0.0"))
    args = parser.parse_args()
    auth = os.environ.get("WEB_AUTH", "on").lower() not in {"off", "0", "false", "no"}
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(Dashboard(args.config, auth=auth)))
    print(f"media-converterr web on {args.bind}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
