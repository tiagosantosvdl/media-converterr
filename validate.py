#!/usr/bin/env python3
"""Validate this machine's GPU (and CPU) with the converter's own commands.

Runs the capability check, makes short synthetic test sources (SDR, and HDR10 with mastering-display and light-level
metadata, encoded with x265, so no media files are needed), then converts them with every encoder that passed its
check, through `build_cmd` exactly as a real conversion would, for each output path: SDR in AV1, HEVC and H.264;
HDR10 kept in HEVC and in AV1; HDR tone mapped to SDR (quality and fast); Dolby Vision profile 5 rendering when
libplacebo works. Each output is checked: exit status, codec, colour signalling, static HDR metadata on the frames and
the frame count. Optional `--sample FILE` (a real HDR10+ or Dolby Vision file, 10 s are cut from it) also tests
HDR10+ and Dolby Vision 8.1 injection.

Writes a JSON report and prints a Markdown summary with a row for the README's "Validated hardware" table.
Usage (in the container, with the GPU passed in):  media_converterr.py validate [--sample FILE] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time

import encoders
import gpus
import media_convert_5 as mc5

MASTER = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"


def run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def make_sources(work: str) -> dict[str, str]:
    """3-second synthetic sources: 1080p SDR (H.264) and 2160p HDR10 (HEVC Main 10, PQ/BT.2020, static metadata)."""
    sdr, hdr = os.path.join(work, "sdr.mkv"), os.path.join(work, "hdr10.mkv")
    run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24:duration=3",
         "-f", "lavfi", "-i", "sine=duration=3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
         "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-c:a", "aac",
         "-metadata:s:a:0", "language=eng", sdr]).check_returncode()
    run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=24:duration=3",
         "-f", "lavfi", "-i", "sine=duration=3", "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt",
         "yuv420p10le", "-x265-params", f"hdr10=1:master-display={MASTER}:max-cll=1000,400:colorprim=bt2020:"
         "transfer=smpte2084:colormatrix=bt2020nc:repeat-headers=1", "-color_primaries", "bt2020", "-color_trc",
         "smpte2084", "-colorspace", "bt2020nc", "-c:a", "aac", "-metadata:s:a:0", "language=eng", hdr]
        ).check_returncode()
    return {"sdr": sdr, "hdr10": hdr}


def decoded_frames(path: str) -> int:
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
               "stream=nb_read_frames", "-of", "csv=p=0", path]).stdout.strip()
    return int(out) if out.isdigit() else -1


def cut_sample(sample: str, work: str) -> str:
    """10 s of the sample (video and first audio track), cut by mkvmerge at keyframes with its timestamps starting
    at zero, as the injections remux. A cut whose decoded frames don't match its packets (leading frames of an open
    GOP the decoder drops) is tried again elsewhere: the injections would rightly refuse such an encode."""
    out = os.path.join(work, "sample.mkv")
    duration = float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          sample]).stdout.strip() or 600)
    for share in (0.1, 0.15, 0.2, 0.3, 0.4, 0.5):  # early first: mkvmerge reads the file up to the cut
        start = int(duration * share)
        for name in os.listdir(work):
            if name.startswith("sample"):
                os.remove(os.path.join(work, name))
        span = f"{start // 3600:02d}:{start % 3600 // 60:02d}:{start % 60:02d}-{(start + 10) // 3600:02d}:" \
               f"{(start + 10) % 3600 // 60:02d}:{(start + 10) % 60:02d}"
        merge = run(["mkvmerge", "-q", "-o", out, "--split", f"parts:{span}", "-a", "1", "-S", "-B", "-M",
                     "--no-chapters", sample])
        if merge.returncode not in (0, 1) or not os.path.exists(out):
            continue
        if decoded_frames(out) == mc5.count_video_frames(out):
            return out
    return out


def side_data(path: str) -> set[str]:
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#3", "-show_frames",
               "-show_entries", "frame=side_data_list", "-of", "default=nw=1", path]).stdout
    return {line.split("=", 1)[1] for line in out.splitlines() if line.startswith("side_data_type=")}


def check(name: str, source: str, settings: dict, expect: dict, work: str, inject: str = "") -> dict:
    """Encode `source` with these conversion settings through build_cmd and check the output."""
    mc5.apply_conversion_settings({"audio_languages": [], "subtitle_languages": [], **settings})
    info = mc5.probe_video_info(source)
    mode = mc5.hdr_mode(info)
    plan = mc5.encode_plan(mode, info)
    out = os.path.join(work, f"{name}.mkv")
    result = {"test": name, "backend": plan.backend, "device": plan.device, "codec": plan.codec,
              "ten_bit": plan.ten_bit, "ok": False, "problems": []}
    started = time.time()
    try:
        proc = run(mc5.build_cmd(source, out, False, info))
        if proc.returncode != 0:  # the converter retries with software decoding; so does this
            proc = run(mc5.build_cmd(source, out, True, info))
            result["software_decode"] = True
    except subprocess.TimeoutExpired:
        result["problems"].append("timed out")
        return result
    result["seconds"] = round(time.time() - started, 1)
    if proc.returncode != 0:
        lines = [line for line in proc.stderr.splitlines() if line.strip()]
        result["problems"].append(f"ffmpeg exit {proc.returncode}: {(lines[-1] if lines else '')[:200]}")
        return result
    logger = logging.getLogger("validate")
    if inject == "hdr10plus":
        result["injected"] = mc5.inject_hdr10plus(source, out, work, logger)
    elif inject == "dovi":
        result["injected"] = mc5.inject_dovi(source, out, work, logger)
    got = mc5.probe_video_info(out)
    result["output"] = {"codec": got.codec_name, "primaries": got.color_primaries, "transfer": got.color_transfer,
                        "matrix": got.color_space}
    if got.codec_name != expect.get("codec", plan.codec):
        result["problems"].append(f"codec {got.codec_name}, expected {expect.get('codec', plan.codec)}")
    for field, value in (("color_transfer", expect.get("transfer")), ("color_primaries", expect.get("primaries"))):
        if value and mc5.normalize_color_value(field, getattr(got, field)) != value:
            result["problems"].append(f"{field} {getattr(got, field)!r}, expected {value}")
    frames = side_data(out)
    for wanted in expect.get("side_data", ()):
        if wanted not in frames:
            result["problems"].append(f"missing {wanted}")
    if inject and not result.get("injected"):
        result["problems"].append(f"{inject} injection did not verify")
    if mc5.count_video_frames(out) != mc5.count_video_frames(source):
        result["problems"].append("frame count differs from the source")
    result["ok"] = not result["problems"]
    return result


def run_all(work: str, sample: str = "") -> dict:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    caps = encoders.detect()
    mc5.apply_capabilities(caps)
    found = gpus.attach_results(gpus.find(), caps)
    sources = make_sources(work)
    hdr = {"transfer": "smpte2084", "primaries": "bt2020",
           "side_data": ("Mastering display metadata", "Content light level metadata")}
    sdr = {"transfer": "bt709", "primaries": "bt709"}
    tests = []
    working = [(r["backend"], r["codec"], r["ten_bit"]) for r in caps["results"] if r["ok"]]
    for backend in encoders.conversion_backends():
        mine = {(c, t) for b, c, t in working if b == backend}
        if not mine:
            continue
        base = {"backend": backend, "codec_fallback": "cpu", "preset": "veryfast"}
        for codec in ("av1", "hevc", "h264"):
            if (codec, codec != "h264") in mine or (codec == "hevc" and ("hevc", False) in mine):
                tests.append((f"{backend}-sdr-{codec}", "sdr", {**base, "sdr_codec": codec}, {**sdr, "codec": codec}))
        for codec in ("hevc", "av1"):
            if (codec, True) in mine:
                tests.append((f"{backend}-hdr10-{codec}", "hdr10", {**base, "hdr_codec": codec},
                              {**hdr, "codec": codec}))
        sdr_codec = next((c for c in ("av1", "hevc", "h264") if (c, c != "h264") in mine), None)
        if sdr_codec:
            tests.append((f"{backend}-tonemap-quality-{sdr_codec}", "hdr10",
                          {**base, "output_hdr10": "sdr", "sdr_codec": sdr_codec}, {**sdr, "codec": sdr_codec}))
            if backend in ("qsv", "vaapi"):
                tests.append((f"{backend}-tonemap-fast-{sdr_codec}", "hdr10",
                              {**base, "output_hdr10": "sdr", "sdr_codec": sdr_codec, "tonemap": "fast"},
                              {**sdr, "codec": sdr_codec}))
    results = []
    for name, source, settings, expect in tests:
        if expect["codec"] not in ("av1", "hevc", "h264"):
            continue
        result = check(name, sources[source], settings, expect, work)
        print(f"{'PASS' if result['ok'] else 'FAIL'} {name} ({result.get('seconds', '-')} s) "
              f"{'; '.join(result['problems'])}", flush=True)
        results.append(result)
    if sample:
        clip = cut_sample(sample, work)
        info = mc5.probe_video_info(clip)
        gpu_backend = next((b for b in ("qsv", "nvenc", "vaapi", "amf") if (b, "hevc", True) in working), "software")
        if info.hdr10_plus:
            results.append(check(f"{gpu_backend}-sample-hdr10plus", clip, {"backend": gpu_backend}, hdr, work,
                                 "hdr10plus"))
        if info.dovi_profile in (7, 8):
            results.append(check(f"{gpu_backend}-sample-dv81", clip, {"backend": gpu_backend, "output_dv": "dv81"},
                                 {**hdr, "side_data": ("Dolby Vision RPU Data",)}, work, "dovi"))
        for r in results[-2:]:
            if r["test"].startswith(f"{gpu_backend}-sample"):
                print(f"{'PASS' if r['ok'] else 'FAIL'} {r['test']} {'; '.join(r['problems'])}", flush=True)
    return {"version": 1, "checked_at": time.time(), "ffmpeg": caps.get("ffmpeg"), "kernel": platform.release(),
            "gpus": [{"name": g.name, "vendor": g.vendor, "pci_id": f"{g.vendor_id}:{g.device_id}",
                      "family": g.family.name if g.family else None, "render_node": g.render_node,
                      "proven": g.tested, "expected": g.family.encodes() if g.family else []} for g in found],
            "capabilities": caps["results"], "libplacebo_vulkan": caps.get("libplacebo_vulkan"),
            "tests": results}


def markdown(report: dict) -> str:
    """A summary and the README table row."""
    passed = {t["test"] for t in report["tests"] if t["ok"]}
    failed = [t for t in report["tests"] if not t["ok"]]

    def paths(backend: str) -> str:
        names = sorted(t[len(backend) + 1:] for t in passed if t.startswith(backend + "-"))
        return ", ".join(names) or "–"
    gpu = "; ".join(f"{g['name']} ({g['family'] or 'family unknown'})" for g in report["gpus"]) or "no GPU"
    backends = sorted({t["backend"] for t in report["tests"]})
    rows = [f"| {gpu} | {b} | {paths(b)} | {report['ffmpeg'].split(' Copyright')[0] if report['ffmpeg'] else ''}, "
            f"kernel {report['kernel']} |" for b in backends]
    lines = ["## Validation summary", "", f"GPU: {gpu}", f"Passed: {len(passed)} of {len(report['tests'])}", ""]
    if failed:
        lines += ["Failed:"] + [f"- {t['test']}: {'; '.join(t['problems'])}" for t in failed] + [""]
    lines += ["README row(s):", "", "| GPU | Backend | Passed | ffmpeg, kernel |", "|---|---|---|---|", *rows]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate this machine's encoders with the converter's commands")
    parser.add_argument("--sample", default="", help="a real HDR10+ or Dolby Vision file (10 s are cut from it)")
    parser.add_argument("--out", default="/data/validation", help="directory for the JSON report")
    args = parser.parse_args(argv)
    work = tempfile.mkdtemp(prefix="validate.")
    try:
        report = run_all(work, args.sample)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, time.strftime("validation-%Y%m%d-%H%M%S.json"))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1)
    print()
    print(markdown(report))
    print(f"\nReport: {path}")
    return 0 if all(t["ok"] for t in report["tests"]) else 1


if __name__ == "__main__":
    sys.exit(main())
