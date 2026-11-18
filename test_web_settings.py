"""Settings stored from the web interface, the web login, webhook payloads and webhook self-registration."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import media_converterr as mc
import settings_store
import web


class StoreTests(unittest.TestCase):
    def test_password_and_session(self):
        record = {"user": "admin", **settings_store.hash_password("correct horse", iterations=1000)}
        self.assertTrue(settings_store.verify_password(record, "admin", "correct horse"))
        self.assertFalse(settings_store.verify_password(record, "admin", "wrong"))
        self.assertFalse(settings_store.verify_password(record, "other", "correct horse"))
        self.assertFalse(settings_store.verify_password(None, "admin", "correct horse"))
        token = settings_store.make_session("k", "admin", now=1000, seconds=60)
        self.assertEqual(settings_store.check_session("k", token, now=1030), "admin")
        self.assertIsNone(settings_store.check_session("k", token, now=1061))  # expired
        self.assertIsNone(settings_store.check_session("other-key", token, now=1030))  # forged
        body, mac = token.rsplit(".", 1)
        self.assertIsNone(settings_store.check_session("k", body + "x." + mac, now=1030))
        self.assertIsNone(settings_store.check_session("k", None))

    def test_values_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "db.sqlite")
            self.assertEqual(settings_store.read(path, "x", "default"), "default")  # no database yet
            conn = mc.connect_database(path)
            self.assertEqual(settings_store.version(conn), 0.0)
            settings_store.put(conn, "x", {"a": 1})
            self.assertEqual(settings_store.read(path, "x"), {"a": 1})
            self.assertGreater(settings_store.version(conn), 0)
            first = settings_store.secret(conn, "hook_token")
            self.assertEqual(settings_store.secret(conn, "hook_token"), first)
            conn.close()


class WebhookPayloadTests(unittest.TestCase):
    def test_sonarr_download_and_rename(self):
        env = mc.webhook_environ("sonarr", {
            "eventType": "Download", "series": {"id": 7, "path": "/tv/Show"},
            "episodeFile": {"path": "/tv/Show/Season 1/Show - S01E01.mkv"}, "isUpgrade": False})
        self.assertEqual(env["sonarr_eventtype"], "Download")
        self.assertEqual(env["sonarr_series_id"], "7")
        self.assertEqual(env["sonarr_series_path"], "/tv/Show")
        self.assertEqual(env["sonarr_episodefile_path"], "/tv/Show/Season 1/Show - S01E01.mkv")
        env = mc.webhook_environ("sonarr", {
            "eventType": "Rename", "series": {"id": 7, "path": "/tv/Show"},
            "renamedEpisodeFiles": [{"path": "/tv/Show/a2.mkv", "previousPath": "/tv/Show/a1.mkv"},
                                    {"path": "/tv/Show/b2.mkv", "previousPath": "/tv/Show/b1.mkv"}]})
        self.assertEqual(env["sonarr_episodefile_paths"], "/tv/Show/a2.mkv|/tv/Show/b2.mkv")
        self.assertEqual(env["sonarr_episodefile_previouspaths"], "/tv/Show/a1.mkv|/tv/Show/b1.mkv")

    def test_radarr_delete_and_test(self):
        env = mc.webhook_environ("radarr", {"eventType": "MovieDelete", "movie": {"id": 3, "folderPath": "/movies/M"},
                                            "deletedFiles": True})
        self.assertEqual(env["radarr_movie_path"], "/movies/M")
        self.assertEqual(env["radarr_movie_deletedfiles"], "True")
        env = mc.webhook_environ("radarr", {"eventType": "MovieFileDelete", "movie": {"id": 3},
                                            "movieFile": {"path": "/movies/M/M.mkv"}})
        self.assertEqual(env["radarr_moviefile_path"], "/movies/M/M.mkv")
        self.assertEqual(mc.webhook_environ("radarr", {"eventType": "Test"}), {"radarr_eventtype": "Test"})


class StoredConnectionTests(unittest.TestCase):
    def test_stored_instances_replace_the_file_and_keep_its_keys(self):
        base = mc.default_settings()
        base = mc.dataclasses.replace(base, instances=(mc.ArrInstance("sonarr", "sonarr", "http://a", "filekey",
                                                                      "/tv", "/media/tv"),))
        stored = {"instances": [{"name": "sonarr", "type": "sonarr", "url": "http://b:8989", "api_key": "",
                                 "remote_root": "/tv", "local_root": "/media/series"},
                                {"name": "radarr", "type": "radarr", "url": "http://c", "api_key": "k2",
                                 "remote_root": "/movies", "local_root": "/media/movies"}],
                  "jellyfin": None, "public_url": "http://conv:8090/"}
        result = mc.apply_stored_connections(base, stored)
        self.assertEqual(result.instance("sonarr").url, "http://b:8989")
        self.assertEqual(result.instance("sonarr").api_key, "filekey")
        self.assertEqual(result.instance("radarr").api_key, "k2")
        self.assertIsNone(result.jellyfin)
        self.assertEqual(result.public_url, "http://conv:8090")
        self.assertIs(mc.apply_stored_connections(base, None), base)
        with self.assertRaises(mc.ConfigurationError):
            mc.apply_stored_connections(base, {"instances": [{"name": "x", "type": "lidarr", "remote_root": "/a",
                                                              "local_root": "/b"}]})


class RegisterTests(unittest.TestCase):
    def test_creates_then_updates_the_webhook(self):
        instance = mc.ArrInstance("sonarr", "sonarr", "http://s", "k", "/tv", "/media/tv")
        settings = mc.default_settings()
        schema = [{"implementation": "Webhook", "id": 0, "onGrab": False, "onDownload": False, "onUpgrade": False,
                   "onRename": False, "onEpisodeFileDelete": False, "onHealthIssue": False, "supportsOnRename": True,
                   "fields": [{"name": "url", "value": ""}, {"name": "method", "value": 2}]}]
        calls, existing = [], []

        def fake(inst, method, endpoint, _settings, payload=None):
            calls.append((method, endpoint, payload))
            if endpoint == "/api/v3/notification" and method == "GET":
                return existing
            if endpoint.endswith("/schema"):
                return schema
            return None
        with mock.patch.object(mc, "api_request", side_effect=fake):
            self.assertEqual(mc.register_webhook(instance, settings, "http://conv/hook/sonarr?token=t"), "created")
            sent = calls[-1][2]
            self.assertEqual(calls[-1][:2], ("POST", "/api/v3/notification"))
            self.assertEqual(sent["name"], mc.WEBHOOK_NAME)
            self.assertTrue(sent["onDownload"] and sent["onRename"] and sent["onEpisodeFileDelete"])
            self.assertFalse(sent["onGrab"] or sent["onHealthIssue"])
            self.assertEqual({f["name"]: f["value"] for f in sent["fields"]},
                             {"url": "http://conv/hook/sonarr?token=t", "method": 1})
            existing.append({**sent, "id": 12})
            self.assertEqual(mc.register_webhook(instance, settings, "http://conv/new"), "updated")
            self.assertEqual(calls[-1][:2], ("PUT", "/api/v3/notification/12"))


class ServerTests(unittest.TestCase):
    """The real HTTP handler on a temporary database: first-run setup, login, settings and a webhook call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        os.makedirs(os.path.join(d, "tv"))
        self.config = os.path.join(d, "config.toml")
        with open(self.config, "w") as f:
            f.write(f'[media_converterr]\ndatabase = "{d}/db.sqlite"\nlog_dir = "{d}/logs"\nwork_dir = "{d}"\n'
                    f'hook_spool_dir = "{d}/hooks"\n[[instances]]\nname = "sonarr"\ntype = "sonarr"\n'
                    f'url = "http://127.0.0.1:9"\napi_key = "filekey"\nremote_root = "/tv"\nlocal_root = "{d}/tv"\n')
        mc.connect_database(os.path.join(d, "db.sqlite")).close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(web.Dashboard(self.config)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cookie = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path, form=None, body=None):
        data = urllib.parse.urlencode(form, doseq=True).encode() if form is not None else body
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"Cookie": self.cookie} if self.cookie else {})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            resp = opener.open(req)
            status, headers, text = resp.status, resp.headers, resp.read().decode()
        except urllib.error.HTTPError as error:
            status, headers, text = error.code, error.headers, error.read().decode()
        if headers.get("Set-Cookie", "").startswith(web.SESSION_COOKIE):
            self.cookie = headers["Set-Cookie"].split(";")[0]
        return status, headers, text

    def test_flow(self):
        status, headers, _ = self.request("/")
        self.assertEqual((status, headers["Location"]), (303, "/setup"))
        status, _, text = self.request("/setup", {"user": "admin", "password": "short", "password2": "short"})
        self.assertEqual(status, 400)
        status, headers, _ = self.request("/setup", {"user": "admin", "password": "long enough pw",
                                                     "password2": "long enough pw"})
        self.assertEqual((status, headers["Location"]), (303, "/settings"))
        self.assertEqual(self.request("/settings")[0], 200)
        self.cookie = ""
        self.assertEqual(self.request("/settings")[1]["Location"], "/login")
        self.assertEqual(self.request("/login", {"user": "admin", "password": "nope"})[0], 401)
        self.assertEqual(self.request("/login", {"user": "admin", "password": "long enough pw"})[0], 303)
        # save: the API key field left empty keeps the file's key; the public URL is stored
        status, _, text = self.request("/settings", {
            "count": "2", "i0-name": "sonarr", "i0-type": "sonarr", "i0-url": "http://127.0.0.1:8",
            "i0-key": "", "i0-remote": "/tv", "i0-local": os.path.join(self.tmp.name, "tv"),
            "jf-url": "", "public_url": "http://conv:8090"})
        self.assertEqual(status, 200, text)
        self.assertIn("http://conv:8090/hook/sonarr?token=", text)
        settings = mc.load_settings(self.config)
        self.assertEqual(settings.instance("sonarr").url, "http://127.0.0.1:8")
        self.assertEqual(settings.instance("sonarr").api_key, "filekey")
        # webhook: wrong token refused, test event answered, download queued
        event = json.dumps({"eventType": "Download", "series": {"id": 1, "path": "/tv/S"},
                            "episodeFile": {"path": "/tv/S/e.mkv"}}).encode()
        self.assertEqual(self.request("/hook/sonarr?token=wrong", body=event)[0], 403)
        token = settings_store.read(settings.database_path, "hook_token")
        self.assertEqual(self.request(f"/hook/sonarr?token={token}", body=b'{"eventType":"Test"}')[2], "test ok")
        status, _, text = self.request(f"/hook/sonarr?token={token}", body=event)
        self.assertEqual(status, 200)
        self.assertTrue(text.startswith("queued"))
        self.assertEqual(self.request(f"/hook/nope?token={token}", body=event)[0], 404)

    def test_conversion_settings(self):
        import media_convert_5 as mc5
        self.addCleanup(mc5.apply_conversion_settings, None)
        self.request("/setup", {"skip": "1"})
        status, _, text = self.request("/settings/conversion", {"sdr_codec": "hevc", "av1_quality": "22",
                                                                "hevc_quality": "15", "preset": "slower",
                                                                "max_height": "1080", "remux_mbps_per_mpx": "2.5",
                                                                "audio_languages": "en, nl", "subtitle_languages": ""})
        self.assertEqual(status, 200, text)
        mc.load_settings(self.config)  # the worker's reload path applies them
        self.assertEqual((mc5.SDR_CODEC, mc5.GQ, mc5.HEVC_GQ, mc5.PRESET), ("hevc", 22, 15, "slower"))
        self.assertEqual((mc5.TARGET_WIDTH, mc5.TARGET_HEIGHT), (1920, 1080))
        self.assertFalse(mc5.REMUX_ENABLED)  # checkbox left unticked
        self.assertEqual(mc5.REMUX_MBPS_PER_MPX, 2.5)
        self.assertEqual((mc5.wanted_audio_languages, mc5.wanted_subtitle_languages), (["en", "nl"], []))
        self.assertEqual(mc5.output_codec(mc5.HDR_SDR), "hevc")
        self.assertEqual(mc5.output_codec(mc5.HDR10_PQ), "hevc")
        status, _, text = self.request("/settings/conversion", {
            "sdr_codec": "h264", "hdr_codec": "av1", "codec_fallback": "cpu", "output_hdr10": "sdr", "output_hlg": "sdr",
            "output_dv": "dv81", "output_dv5": "sdr", "tonemap": "fast", "h264_quality": "23"})
        self.assertEqual(status, 200, text)
        mc.load_settings(self.config)
        self.assertEqual((mc5.SDR_CODEC, mc5.HDR_CODEC, mc5.CODEC_FALLBACK, mc5.OUTPUT_HDR10, mc5.OUTPUT_DV,
                          mc5.TONEMAP, mc5.H264_GQ), ("h264", "av1", "cpu", "sdr", "dv81", "fast", 23))
        self.assertEqual(self.request("/settings/conversion", {"output_dv5": "dv81"})[0], 400)  # not offered
        self.assertEqual(self.request("/settings/conversion", {"subtitle_mode": "embed", "bitmap_subtitles": "sidecar"})[0], 200)
        mc.load_settings(self.config)
        self.assertEqual((mc5.SUBTITLE_MODE, mc5.BITMAP_SUBTITLES), ("embed", "sidecar"))
        status, _, text = self.request("/settings/conversion", {"sdr_codec": "av1", "av1_quality": "99"})
        self.assertEqual(status, 400)
        self.assertIn("between 1 and 51", text)
        self.assertEqual(self.request("/settings/conversion", {"audio_languages": "en, english!"})[0], 400)

    def test_every_page_renders(self):
        self.request("/setup", {"skip": "1"})
        for path in ("/", "/queue", "/history", "/history?limit=1000", "/events", "/files", "/files?q=x", "/errors",
                     "/settings", "/settings/connect", "/settings/conversion", "/settings/general", "/system/status",
                     "/stats", "/system/tasks", "/hardware", "/log"):
            with self.subTest(path=path):
                status, _, text = self.request(path)
                self.assertEqual(status, 200, text[-500:])
                self.assertIn("class=\"sidebar\"", text)

    def test_connect_dialogs(self):
        self.request("/setup", {"skip": "1"})
        tv = os.path.join(self.tmp.name, "tv")
        # add a Radarr instance, edit Sonarr (empty key keeps the file's), set the address, add then remove Jellyfin
        status, headers, text = self.request("/settings/connect/instance", {
            "original": "", "name": "radarr", "type": "radarr", "url": "http://127.0.0.1:7", "api_key": "rk",
            "remote_root": "/movies", "local_root": tv})
        self.assertEqual(status, 303, text)
        self.assertEqual(self.request("/settings/connect/instance", {
            "original": "sonarr", "name": "sonarr", "type": "sonarr", "url": "http://127.0.0.1:6", "api_key": "",
            "remote_root": "/tv", "local_root": tv})[0], 303)
        self.assertEqual(self.request("/settings/connect/address", {"public_url": "http://conv:8090/"})[0], 303)
        self.assertEqual(self.request("/settings/connect/jellyfin", {"url": "http://jf:8096", "api_key": "jk",
                                                                     "paths": f"{tv} = /data/tv"})[0], 303)
        s = mc.load_settings(self.config)
        self.assertEqual((s.instance("sonarr").url, s.instance("sonarr").api_key), ("http://127.0.0.1:6", "filekey"))
        self.assertEqual(s.instance("radarr").api_key, "rk")
        self.assertEqual(s.public_url, "http://conv:8090")
        self.assertEqual((s.jellyfin.url, s.jellyfin.api_key, s.jellyfin.paths), ("http://jf:8096", "jk",
                                                                                  ((tv, "/data/tv"),)))
        self.assertEqual(self.request("/settings/connect/jellyfin/delete", {})[0], 303)
        self.assertIsNone(mc.load_settings(self.config).jellyfin)
        self.assertEqual(self.request("/settings/connect/instance/delete", {"name": "radarr"})[0], 303)
        self.assertEqual(self.request("/settings/connect/instance/delete", {"name": "sonarr"})[0], 400)  # last one
        self.assertEqual(self.request("/settings/connect/instance", {
            "original": "", "name": "x", "type": "lidarr", "remote_root": "/a", "local_root": "/b"})[0], 400)
        # testing unsaved dialog values answers JSON
        status, _, text = self.request("/settings/connect/test-one?kind=arr", {
            "original": "sonarr", "name": "sonarr", "type": "sonarr", "url": "http://127.0.0.1:9", "api_key": ""})
        result = json.loads(text)
        self.assertFalse(result["ok"])
        self.assertIn("failed", result["message"])
        page = self.request("/settings/connect")[2]
        self.assertIn("http://conv:8090/hook/sonarr?token=", page)
        self.assertNotIn("filekey", page)  # keys are never shown

    def test_real_progress(self):
        import web as web_module
        work = self.tmp.name
        self.assertIsNone(web_module.read_progress(work))
        with open(os.path.join(work, mc.PROGRESS_META), "w") as f:
            json.dump({"path": "/m/a.mkv", "duration": 1000.0, "kind": "encode"}, f)
        with open(os.path.join(work, mc.PROGRESS_FILE), "w") as f:
            f.write("frame=10\nout_time_us=100000000\nspeed=2.00x\nprogress=continue\n"
                    "frame=20\nout_time_us=250000000\nspeed=1.50x\nprogress=continue\n")
        p = web_module.read_progress(work)
        self.assertEqual((p["path"], round(p["pct"]), p["speed"], round(p["left"])), ("/m/a.mkv", 25, 1.5, 500))
        settings = mc.load_settings(self.config)
        settings = mc.dataclasses.replace(settings, work_dir=work)
        mc.set_stage(settings, "/m/a.mkv", ["Converting", "Injecting Dolby Vision", "Checking output"],
                     "Injecting Dolby Vision")
        with open(os.path.join(work, mc.PROGRESS_FILE), "a") as f:
            f.write("out_time_us=990000000\nprogress=end\n")
        p = web_module.read_progress(work)
        self.assertEqual((p["stage"], p["step"], p["steps"], p["ended"], p["pct"]),
                         ("Injecting Dolby Vision", 2, 3, True, 100.0))
        self.assertEqual(json.load(open(os.path.join(work, mc.PROGRESS_META)))["duration"], 1000.0)  # merged
        info = mc5_info = __import__("media_convert_5").VideoInfo(
            color_transfer="smpte2084", color_primaries="bt2020", color_space="bt2020nc", color_range="tv",
            codec_name="hevc", dovi_profile=8, dovi_compatibility_id=1, dovi_bl_present=True, hdr10_plus=True)
        self.assertEqual(mc.job_steps(info, "hdr10-pq", None, [1]),
                         ["Converting", "Injecting HDR10+", "Injecting Dolby Vision", "Checking output", "Installing",
                          "Subtitles"])
        self.assertEqual(mc.job_steps(info, "hdr10-pq", "low bitrate", []), ["Remuxing", "Checking output",
                                                                             "Installing"])

    def test_cross_site_posts_are_refused(self):
        self.request("/setup", {"skip": "1"})
        def post(headers):
            req = urllib.request.Request(self.base + "/settings/test", data=b"", method="POST", headers=headers)
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as error:
                return error.code
        host = self.base.split("//")[1]
        self.assertEqual(post({"Sec-Fetch-Site": "same-origin", "Origin": "null"}), 200)  # what browsers send
        self.assertEqual(post({"Origin": self.base}), 200)
        self.assertEqual(post({"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.example"}), 403)
        self.assertEqual(post({"Origin": "http://evil.example"}), 403)
        self.assertEqual(post({"Origin": f"http://{host}"}), 200)

    def test_without_login(self):
        self.assertEqual(self.request("/setup", {"skip": "1"})[1]["Location"], "/settings")
        self.assertEqual(self.request("/settings")[0], 200)  # no login needed
        self.assertEqual(self.request("/login")[1]["Location"], "/")
        # turned on later from the Settings page, then required
        status, _, _ = self.request("/settings/login", {"action": "set", "user": "me", "password": "a long password",
                                                        "password2": "a long password"})
        self.assertEqual(status, 303)
        self.assertEqual(self.request("/settings")[0], 200)  # the cookie from turning it on
        self.cookie = ""
        self.assertEqual(self.request("/settings")[1]["Location"], "/login")
        self.assertEqual(self.request("/login", {"user": "me", "password": "a long password"})[0], 303)
        # and off again
        self.assertEqual(self.request("/settings/login", {"action": "off"})[0], 303)
        self.cookie = ""
        self.assertEqual(self.request("/settings")[0], 200)


if __name__ == "__main__":
    unittest.main()
