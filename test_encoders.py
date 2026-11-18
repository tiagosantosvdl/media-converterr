"""Encoder backends: the Intel QSV commands are unchanged byte for byte, CPU commands, backend choice, detection."""
import json
import os
import unittest
from unittest.mock import patch

import encoders
import media_convert_5 as m
from media_convert_5 import VideoInfo

HERE = os.path.dirname(os.path.abspath(__file__))


def fixtures():
    sdr = dict(color_transfer="bt709", color_primaries="bt709", color_space="bt709", color_range="tv",
               codec_name="h264", width=1920, height=1080)
    hdr = dict(color_transfer="smpte2084", color_primaries="bt2020", color_space="bt2020nc", color_range="tv",
               codec_name="hevc", width=3840, height=2160, has_mastering_display=True)
    return {"sdr": VideoInfo(**sdr),
            "sdr_missing_colors": VideoInfo(color_transfer="", color_primaries="", color_space="", color_range="",
                                            width=720, height=576),
            "hdr10": VideoInfo(**hdr), "hdr10plus": VideoInfo(**hdr, hdr10_plus=True),
            "hlg": VideoInfo(**{**hdr, "color_transfer": "arib-std-b67"}),
            "dv5": VideoInfo(**{**hdr, "dovi_profile": 5, "dovi_bl_present": True, "color_primaries": "",
                                "color_space": "", "color_transfer": ""}),
            "dv8": VideoInfo(**hdr, dovi_profile=8, dovi_compatibility_id=1, dovi_bl_present=True)}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(m.apply_conversion_settings, None)
        self.addCleanup(m.apply_capabilities, None)
        patcher = patch.object(m, "stream_mapping_args", return_value=["-map", "0:1"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_qsv_commands_are_unchanged(self):
        """The commands from before backends existed (testdata/build_cmd_qsv.json), for every mode, both SDR codecs,
        with and without software decoding; "auto" without a capability check must also give them."""
        with open(os.path.join(HERE, "testdata", "build_cmd_qsv.json")) as f:
            golden = json.load(f)
        for backend in ("qsv", "auto"):
            for sdr_codec in ("av1", "hevc"):
                m.apply_conversion_settings({"sdr_codec": sdr_codec, "backend": backend,
                                             "audio_languages": [], "subtitle_languages": []})
                m.CAPABILITIES = None
                for name, info in fixtures().items():
                    for fallback in (False, True):
                        key = f"{sdr_codec}/{name}/{'fallback' if fallback else 'hw'}"
                        with self.subTest(backend=backend, case=key):
                            self.assertEqual(m.build_cmd("in.mkv", "out.mkv", fallback, info), golden[key])

    def test_software_commands(self):
        m.apply_conversion_settings({"backend": "software"})
        cmd = m.build_cmd("in.mkv", "out.mkv", False, fixtures()["sdr"])
        self.assertNotIn("-init_hw_device", cmd)
        self.assertEqual(cmd[cmd.index("-c:v:0") + 1], "libsvtav1")
        self.assertEqual(cmd[cmd.index("-crf") + 1], str(m.SOFTWARE_AV1_CRF))
        self.assertIn("format=yuv420p10le", cmd[cmd.index("-vf:v:0") + 1])
        self.assertNotIn("-async_depth", cmd)
        cmd = m.build_cmd("in.mkv", "out.mkv", False, fixtures()["hdr10"])
        self.assertEqual(cmd[cmd.index("-c:v:0") + 1], "libx265")
        self.assertIn("hdr10-opt=1", cmd[cmd.index("-x265-params") + 1])
        self.assertIn("MEDIA_CONVERTERR=hevc-hdr10-pq", cmd)
        cmd = m.build_cmd("in.mkv", "out.mkv", False, fixtures()["dv5"])
        self.assertIn("apply_dolbyvision=true", cmd[cmd.index("-vf:v:0") + 1])
        self.assertEqual(cmd[cmd.index("-filter_hw_device") + 1], "vk")

    @staticmethod
    def caps(*working):
        """A capability check where exactly these (backend, device, codec, ten_bit) pairs worked."""
        rows = []
        for name in ("qsv", "vaapi", "nvenc", "amf", "software"):
            for codec, ten in (("av1", True), ("hevc", True), ("hevc", False), ("h264", False)):
                for device in ("/dev/dri/renderD128", "/dev/dri/renderD129", name):
                    rows.append({"backend": name, "device": device, "codec": codec, "ten_bit": ten,
                                 "ok": (name, device, codec, ten) in working})
        return {"results": rows}

    def test_plan(self):
        P, D, D2 = encoders.Plan, "/dev/dri/renderD128", "/dev/dri/renderD129"
        arc = self.caps(("qsv", D, "av1", True), ("qsv", D, "hevc", True), ("software", "software", "av1", True),
                        ("software", "software", "hevc", True))
        self.assertEqual(encoders.plan(codec="hevc", hdr=True, preference="auto", capabilities=arc),
                         P("qsv", D, "hevc", True))
        self.assertEqual(encoders.plan(codec="av1", hdr=False, preference="auto", capabilities=arc),
                         P("qsv", D, "av1", True))
        # an older Intel GPU (no AV1) as the second device: SDR switches to HEVC on the GPU, not AV1 on the CPU
        older = self.caps(("qsv", D2, "hevc", True), ("software", "software", "av1", True),
                          ("software", "software", "hevc", True))
        self.assertEqual(encoders.plan(codec="av1", hdr=False, preference="auto", capabilities=older),
                         P("qsv", D2, "hevc", True))
        # a GPU with only 8-bit HEVC: SDR uses it, HDR goes to the CPU
        eight = self.caps(("vaapi", D, "hevc", False), ("software", "software", "hevc", True))
        self.assertEqual(encoders.plan(codec="hevc", hdr=False, preference="auto", capabilities=eight),
                         P("vaapi", D, "hevc", False))
        self.assertEqual(encoders.plan(codec="hevc", hdr=True, preference="auto", capabilities=eight),
                         P("software", "software", "hevc", True))
        nvidia = self.caps(("nvenc", "nvenc", "hevc", True), ("nvenc", "nvenc", "av1", True))
        self.assertEqual(encoders.plan(codec="hevc", hdr=True, preference="auto", capabilities=nvidia),
                         P("nvenc", "nvenc", "hevc", True))
        self.assertEqual(encoders.plan(codec="av1", hdr=False, preference="software", capabilities=arc).backend,
                         "software")
        self.assertEqual(encoders.plan(codec="av1", hdr=False, preference="auto", capabilities=None),
                         P("qsv", D, "av1", True))  # never checked: as before backends existed
        old_format = {"results": [{"backend": "qsv", "codec": "hevc", "ok": True}]}  # stored by the first version
        self.assertEqual(encoders.plan(codec="hevc", hdr=True, preference="auto", capabilities=old_format),
                         P("qsv", D, "hevc", True))

    def test_codec_fallbacks(self):
        P, D = encoders.Plan, "/dev/dri/renderD128"
        old = self.caps(("qsv", D, "h264", False), ("software", "software", "av1", True),
                        ("software", "software", "hevc", True), ("software", "software", "h264", False))
        self.assertEqual(encoders.plan(codec="av1", hdr=False, preference="auto", capabilities=old),
                         P("qsv", D, "h264", False))  # an H.264-only GPU, fallback "gpu"
        self.assertEqual(encoders.plan(codec="av1", hdr=False, preference="auto", capabilities=old, fallback="cpu"),
                         P("software", "software", "av1", True))
        self.assertEqual(encoders.plan(codec="hevc", hdr=True, preference="auto", capabilities=old),
                         P("software", "software", "hevc", True))  # HDR never goes to H.264
        arc = self.caps(("qsv", D, "av1", True), ("qsv", D, "hevc", True), ("qsv", D, "h264", False))
        self.assertEqual(encoders.plan(codec="av1", hdr=True, preference="auto", capabilities=arc),
                         P("qsv", D, "av1", True))  # HDR10 on AV1
        self.assertEqual(encoders.plan(codec="h264", hdr=False, preference="auto", capabilities=arc),
                         P("qsv", D, "h264", False))

    def test_output_choices(self):
        f = fixtures()
        hdr10plus, dv8 = f["hdr10plus"], f["dv8"]
        m.apply_conversion_settings({})
        self.assertEqual(m.output_spec(dv8, m.HDR10_PQ), m.OutputSpec("dv81", "hevc", False, "dovi-8.1"))  # default
        self.assertEqual(m.output_spec(hdr10plus, m.HDR10_PQ).target, "hdr10plus")
        m.apply_conversion_settings({"hdr_codec": "av1", "output_dv": "dv81"})
        self.assertEqual(m.output_spec(f["hdr10"], m.HDR10_PQ).codec, "av1")  # HDR10 on AV1
        self.assertEqual(m.output_spec(hdr10plus, m.HDR10_PQ).codec, "hevc")  # HDR10+ needs HEVC
        self.assertEqual(m.output_spec(dv8, m.HDR10_PQ), m.OutputSpec("dv81", "hevc", False, "dovi-8.1"))
        m.apply_conversion_settings({"output_hdr10": "hdr10", "hdr_codec": "av1"})
        self.assertEqual(m.output_spec(hdr10plus, m.HDR10_PQ), m.OutputSpec("hdr10", "av1", False, m.HDR10_PQ))
        m.CAPABILITIES = None
        cmd = m.build_cmd("in.mkv", "out.mkv", False, f["hdr10"])
        self.assertEqual(cmd[cmd.index("-c:v:0") + 1], "av1_qsv")
        self.assertIn("av1_metadata=color_primaries=9:transfer_characteristics=16", cmd[cmd.index("-bsf:v:0") + 1])
        # everything to SDR: tone mapped on Vulkan, BT.709 signalling, SDR codec, tagged sdr
        m.apply_conversion_settings({"output_hdr10": "sdr", "output_hlg": "sdr", "output_dv": "sdr",
                                     "output_dv5": "sdr", "sdr_codec": "h264"})
        for case in ("hdr10", "hlg", "dv5", "dv8"):
            with self.subTest(case=case):
                cmd = m.build_cmd("in.mkv", "out.mkv", False, f[case])
                vf = cmd[cmd.index("-vf:v:0") + 1]
                self.assertIn("tonemapping=bt.2390", vf)  # quality (default): libplacebo for every source
                self.assertNotIn("-hwaccel", cmd)  # CPU-decoded frames into Vulkan (VAAPI-to-Vulkan mapping crashed)
                self.assertIn("setparams=color_primaries=1:color_trc=1:colorspace=1", vf)
                self.assertEqual(cmd[cmd.index("-color_trc") + 1], "bt709")
                self.assertEqual(cmd[cmd.index("-c:v:0") + 1], "h264_qsv")
                self.assertIn("h264_metadata=colour_primaries=1", cmd[cmd.index("-bsf:v:0") + 1])
                self.assertIn("MEDIA_CONVERTERR=h264-sdr", cmd)
                self.assertIn("format=nv12", vf)
        m.apply_conversion_settings({"output_hdr10": "sdr", "output_dv5": "sdr", "tonemap": "fast"})
        vf = m.build_cmd("in.mkv", "out.mkv", False, f["hdr10"])
        vf = vf[vf.index("-vf:v:0") + 1]
        self.assertTrue(vf.startswith("tonemap_vaapi=format=p010"), vf)  # fast: the GPU's video processing
        dv5 = m.build_cmd("in.mkv", "out.mkv", False, f["dv5"])
        self.assertIn("tonemapping=bt.2390", dv5[dv5.index("-vf:v:0") + 1])  # profile 5 always needs libplacebo
        self.assertEqual(m.output_colors(f["hdr10"], m.HDR10_PQ)["color_transfer"], "bt709")
        with patch.object(m, "video_bitrate", return_value=1e6):  # tone mapping is never a remux
            self.assertIsNone(m.remux_decision(f["hdr10"], m.HDR10_PQ, "x.mkv"))

    def test_vaapi_renders(self):
        f = fixtures()
        m.apply_conversion_settings({"backend": "vaapi", "output_hdr10": "sdr"})
        m.CAPABILITIES = self.caps(("vaapi", "/dev/dri/renderD128", "av1", True),
                                   ("vaapi", "/dev/dri/renderD128", "hevc", True))
        cmd = m.build_cmd("in.mkv", "out.mkv", False, f["hdr10"])
        self.assertTrue(cmd[cmd.index("-vf:v:0") + 1].startswith("tonemap_vaapi"))  # quality impossible: GPU's own
        self.assertEqual(cmd[cmd.index("-c:v:0") + 1], "av1_vaapi")
        cmd = m.build_cmd("in.mkv", "out.mkv", False, f["dv5"])  # profile 5 to HDR10: the CPU encoder
        self.assertEqual(cmd[cmd.index("-c:v:0") + 1], "libx265")
        self.assertIn("apply_dolbyvision=true", cmd[cmd.index("-vf:v:0") + 1])

    def test_other_backend_commands(self):
        def cmd(backend, info, sdr="av1", fallback=False, ten=True):
            m.apply_conversion_settings({"backend": backend, "sdr_codec": sdr})
            m.CAPABILITIES = self.caps(*[(backend, d, c, t) for d in ("/dev/dri/renderD128", backend)
                                         for c in ("av1", "hevc") for t in (True, False) if ten or not t])
            return m.build_cmd("in.mkv", "out.mkv", fallback, info)
        f = fixtures()
        c = cmd("vaapi", f["hdr10"])
        self.assertEqual(c[c.index("-c:v:0") + 1], "hevc_vaapi")
        self.assertIn("main10", c)
        self.assertIn("scale_vaapi=", c[c.index("-vf:v:0") + 1])
        self.assertNotIn("hwmap=derive_device=qsv", c[c.index("-vf:v:0") + 1])
        c = cmd("nvenc", f["hdr10"])
        self.assertEqual(c[c.index("-hwaccel") + 1], "cuda")
        self.assertIn("scale_cuda=", c[c.index("-vf:v:0") + 1])
        self.assertEqual(c[c.index("-cq") + 1], str(m.HEVC_GQ))
        c = cmd("nvenc", f["dv5"])
        self.assertNotIn("-hwaccel", c)
        self.assertIn("libplacebo", c[c.index("-vf:v:0") + 1])
        c = cmd("amf", f["sdr"])
        self.assertEqual(c[c.index("-c:v:0") + 1], "av1_amf")
        self.assertIn("format=p010le", c[c.index("-vf:v:0") + 1])
        m.apply_conversion_settings({"backend": "qsv", "sdr_codec": "hevc"})
        m.CAPABILITIES = self.caps(("qsv", "/dev/dri/renderD128", "hevc", False))
        c = m.build_cmd("in.mkv", "out.mkv", False, f["sdr"])  # 8-bit-only Intel GPU
        self.assertEqual(c[c.index("-profile:v:0") + 1], "main")
        self.assertIn("format=nv12", c[c.index("-vf:v:0") + 1])

    def test_detection_reports_each_pair(self):
        def fake_run(cmd, **kw):
            class P:
                returncode = 0 if "libx265" in cmd or "libsvtav1" in cmd else 1
                stderr = "" if returncode == 0 else "[hevc_qsv @ 0x1] Device creation failed: -22.\nConversion failed!\n"
                stdout = " V..... libsvtav1  x\n V....D libx265  x\n V..... hevc_qsv  x\n V....D hevc_nvenc x\n"
            return P()
        with patch.object(encoders.subprocess, "run", side_effect=fake_run), \
                patch.object(encoders, "render_devices", return_value=["/dev/dri/renderD128"]):
            caps = encoders.detect()
        by = {(r["backend"], r["codec"], r["ten_bit"]): r for r in caps["results"]}
        self.assertTrue(by[("software", "hevc", True)]["ok"])
        self.assertFalse(by[("qsv", "hevc", True)]["ok"])
        self.assertEqual(by[("qsv", "hevc", True)]["error"], "Device creation failed: -22.")
        self.assertEqual(by[("qsv", "av1", True)]["error"], "not in this ffmpeg build")
        self.assertTrue(by[("qsv", "h264", False)]["used_for_conversion"])  # H.264 is an output option
        self.assertEqual(encoders.plan(codec="hevc", hdr=True, preference="auto", capabilities=caps).backend,
                         "software")

if __name__ == "__main__":
    unittest.main()


class GpuTests(unittest.TestCase):
    def test_families(self):
        import gpus
        cases = {("Intel", "Battlemage G21 [Arc B580]"): (True, True), ("Intel", "DG2 [Arc A380]"): (True, True),
                 ("Intel", "Alder Lake-S GT1 [UHD Graphics 730]"): (False, True),
                 ("Intel", "CometLake-S GT2 [UHD Graphics 630]"): (False, True),
                 ("Intel", "Skylake GT2 [HD Graphics 520]"): (False, False),
                 ("NVIDIA", "AD104 [GeForce RTX 4070]"): (True, True), ("NVIDIA", "GA102 [GeForce RTX 3080]"): (False, True),
                 ("NVIDIA", "GP108 [GeForce GT 1030]"): (False, False), ("NVIDIA", "GB202 [GeForce RTX 5090]"): (True, True),
                 ("AMD", "Navi 31 [Radeon RX 7900 XT/7900 XTX]"): (True, True),
                 ("AMD", "Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]"): (False, True),
                 ("AMD", "Navi 24 [Radeon RX 6400/6500 XT/6500M]"): (False, False),
                 ("AMD", "Phoenix1"): (True, True), ("AMD", "Ellesmere [Radeon RX 470/480/570/580]"): (False, False)}
        for (vendor, name), (av1, hevc10) in cases.items():
            fam = gpus.family_for(vendor, name)
            with self.subTest(name=name):
                self.assertIsNotNone(fam)
                self.assertEqual((fam.av1, fam.hevc10), (av1, hevc10))

    def test_find_and_results(self):
        import gpus, tempfile
        with tempfile.TemporaryDirectory() as d:
            dev = os.path.join(d, "sys", "0000:03:00.0")
            os.makedirs(os.path.join(dev, "drm", "renderD128"))
            for f, v in (("class", "0x030000"), ("vendor", "0x8086"), ("device", "0x56a5")):
                open(os.path.join(dev, f), "w").write(v + "\n")
            ids = os.path.join(d, "pci.ids")
            open(ids, "w").write("8086  Intel Corporation\n\t56a5  DG2 [Arc A380]\n10de  NVIDIA\n")
            with patch.object(gpus, "PCI_IDS", (ids,)):
                found = gpus.find(os.path.join(d, "sys"))
        self.assertEqual(len(found), 1)
        g = found[0]
        self.assertEqual((g.vendor, g.name, g.render_node), ("Intel", "DG2 [Arc A380]", "/dev/dri/renderD128"))
        caps = {"results": [{"backend": "qsv", "device": "/dev/dri/renderD128", "codec": "hevc", "ten_bit": True,
                             "ok": True}]}
        gpus.attach_results(found, caps)
        self.assertEqual(g.tested, {"HEVC 10-bit": ["qsv"]})
        self.assertEqual(gpus.missing(g), ["AV1 10-bit"])


class ValidateReportTests(unittest.TestCase):
    def test_markdown_row(self):
        import validate
        report = {"ffmpeg": "ffmpeg version 8.1 Copyright (c) x", "kernel": "6.1.0",
                  "gpus": [{"name": "DG2 [Arc A380]", "family": "Intel Xe-HPG"}],
                  "tests": [{"test": "qsv-sdr-av1", "backend": "qsv", "ok": True, "problems": []},
                            {"test": "qsv-hdr10-av1", "backend": "qsv", "ok": False, "problems": ["missing x"]},
                            {"test": "software-sdr-h264", "backend": "software", "ok": True, "problems": []}]}
        text = validate.markdown(report)
        self.assertIn("Passed: 2 of 3", text)
        self.assertIn("- qsv-hdr10-av1: missing x", text)
        self.assertIn("| DG2 [Arc A380] (Intel Xe-HPG) | qsv | sdr-av1 | ffmpeg version 8.1, kernel 6.1.0 |", text)
