"""Encoder backends, the capability check and the choice of encoder for each conversion.

A backend says how a source is decoded, filtered and encoded on one kind of hardware. `detect()` proves, on every
GPU render node, which backends, codecs and bit depths work with a short test encode each: ffmpeg builds list
encoders whose hardware or driver is absent, so a listed encoder proves nothing. `plan()` then picks the backend,
device, codec and bit depth for a conversion.

Backends: Intel QSV (verified on Intel Arc), the CPU (SVT-AV1, x265; verified), VAAPI (verified on Intel Arc; the
same path drives AMD GPUs, unverified there), NVIDIA NVENC and AMD AMF (written from the ffmpeg documentation, not
tested on real hardware). Standard library only.
"""
from __future__ import annotations

import glob
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

DEFAULT_RENDER_DEVICE = "/dev/dri/renderD128"
TEST_SOURCE = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=1"]
TEST_TIMEOUT = 60
CHECK_VERSION = 2  # raise when the tests change, so stored checks are redone
GPU_ORDER = ("qsv", "nvenc", "vaapi", "amf")  # preferred first when the setting is automatic

# x265 takes the same preset names as QSV; SVT-AV1 and NVENC take their own scales.
SVT_PRESETS = {"veryfast": 10, "faster": 9, "fast": 8, "medium": 6, "slow": 5, "slower": 4, "veryslow": 3}
NVENC_PRESETS = {"veryfast": "p2", "faster": "p3", "fast": "p4", "medium": "p5", "slow": "p6", "slower": "p7",
                 "veryslow": "p7"}
AMF_QUALITY = {"veryfast": "speed", "faster": "speed", "fast": "balanced", "medium": "balanced", "slow": "quality",
               "slower": "quality", "veryslow": "quality"}


@dataclass(frozen=True)
class Plan:
    """How one conversion is encoded."""
    backend: str
    device: str
    codec: str  # "av1" or "hevc"
    ten_bit: bool = True


@dataclass(frozen=True)
class Pipeline:
    hardware_init: list[str]
    accel: list[str]
    vf: str
    encoder: list[str]
    extra: list[str]  # encoder-specific options after the encoder (QSV: -async_depth)


def _scale(width: int, height: int) -> tuple[str, str]:
    return f"w='ceil(min({width},iw)/8)*8'", f"h='ceil(min({height},ih)/8)*8'"


def _placebo_size(width: int, height: int) -> str:
    w, h = _scale(width, height)
    return f"{w}:{h}:force_original_aspect_ratio=decrease:"


def dovi_render(setparams: str, width: int = 3840, height: int = 2160) -> str:
    """Dolby Vision profile 5 rendered to HDR10 by libplacebo, scaled there too."""
    return (f"libplacebo={_placebo_size(width, height)}apply_dolbyvision=true:color_primaries=bt2020:"
            "color_trc=smpte2084:colorspace=bt2020nc:range=tv:format=p010,"
            f"{setparams}")


def tonemap_render(setparams: str, width: int = 3840, height: int = 2160) -> str:
    """HDR (HDR10, HDR10+, HLG, any Dolby Vision) tone mapped to SDR BT.709 by libplacebo (BT.2390, perceptual gamut
    mapping, Dolby Vision metadata applied first), scaled there too. setparams: the SDR output's signalling."""
    return (f"libplacebo={_placebo_size(width, height)}apply_dolbyvision=true:tonemapping=bt.2390:"
            "gamut_mode=perceptual:color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv:format=p010,"
            f"{setparams}")


def tonemap_vaapi(fmt: str) -> str:
    """HDR to SDR BT.709 on Intel/AMD video hardware (VAAPI video processing). Dolby Vision profile 5 needs
    libplacebo instead (its base layer isn't HDR10)."""
    return f"tonemap_vaapi=format={fmt}:p=bt709:t=bt709:m=bt709"


def _cpu_render(render: str, ten_bit: bool = True, pre: str = "") -> str:
    """A libplacebo render (which also scales): CPU-decoded frames (cropped/deinterlaced first by `pre`) uploaded to
    Vulkan, rendered, downloaded for the encoder. Mapping VAAPI frames into Vulkan instead crashed on Intel Arc
    (Battlemage) with ffmpeg 8.1."""
    return (f"{pre}format=p010le,hwupload,{render},hwdownload,format=p010le"
            + ("" if ten_bit else ",format=nv12"))


def fit(w: int, h: int, width: int, height: int) -> tuple[int, int]:
    """(w, h) scaled down to fit width x height, keeping the aspect ratio; even sizes."""
    f = min(1.0, width / w, height / h)
    return int(w * f) // 2 * 2, int(h * f) // 2 * 2


def _cpu_pre(deinterlace: bool, crop: Optional[tuple]) -> str:
    """CPU filters ahead of scaling: crop to the picture, deinterlace (bwdif, one frame per frame)."""
    out = f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}," if crop else ""
    return out + ("bwdif=mode=send_frame," if deinterlace else "")


def _value(quality: dict, codec: str, software: bool = False) -> str:
    return str(quality[f"software_{codec}" if software else codec])


class QsvBackend:
    """Intel Quick Sync: VAAPI decode and scaling, QSV encode; libplacebo renders (Dolby Vision profile 5, tone
    mapping) on Vulkan. For 10-bit output these are the exact commands from before backends existed (snapshot test)."""
    name, label, verified = "qsv", "Intel Quick Sync (QSV)", True
    encoder_names = {"av1": "av1_qsv", "hevc": "hevc_qsv", "h264": "h264_qsv"}
    per_device = True

    def pipeline(self, plan: Plan, *, render: Optional[str], setparams: str, color_range: str, width: int,
                 height: int, software_decode: bool, quality: dict, preset: str, hdr: bool,
                 gpu_tonemap: bool = False, deinterlace: bool = False, crop: Optional[tuple] = None,
                 lookahead: int = 100) -> Pipeline:
        w, h = _scale(width, height)
        fmt = "p010" if plan.ten_bit else "nv12"
        scale = f"scale_vaapi={w}:{h}:force_original_aspect_ratio=decrease:format={fmt}:out_range={color_range}"
        hwmap = "hwmap=derive_device=qsv,format=qsv"
        vf = f"{setparams},{scale},{hwmap}"
        accel = ["-hwaccel", "vaapi", "-hwaccel_device", "va", "-hwaccel_output_format", "vaapi"]
        init = ["-init_hw_device", f"vaapi=va:{plan.device}", "-init_hw_device", "qsv=qsv@va",
                "-filter_hw_device", "va"]
        deint = "deinterlace_vaapi=rate=frame," if deinterlace else ""
        if deinterlace:
            vf = f"{setparams},{deint}{scale},{hwmap}"
        if crop and not render and not software_decode:
            # crop and scale in one QSV video-processing step (VAAPI's scaler can't crop)
            ow, oh = fit(crop[0], crop[1], width, height)
            vpp = (f"vpp_qsv=cx={crop[2]}:cy={crop[3]}:cw={crop[0]}:ch={crop[1]}:w={ow}:h={oh}:format={fmt}"
                   + (":deinterlace=2" if deinterlace else ""))
            first = f"{tonemap_vaapi(fmt)}," if gpu_tonemap else ""
            vf = f"{first}{setparams},{hwmap},{vpp}"
        elif gpu_tonemap and not software_decode:
            # Intel's VPP tone mapping (HDR10/HLG and Dolby Vision's HDR10 base) on the GPU; setparams re-tags the
            # frames so ffmpeg needn't convert hardware frames to match the output colour options
            vf = f"{deint}{tonemap_vaapi(fmt)},{setparams},{scale},{hwmap}"
        elif render:
            # Vulkan opened on its own: derived from the VAAPI device, uploads were about 7x slower (Intel Arc)
            init = ["-init_hw_device", f"vaapi=va:{plan.device}", "-init_hw_device", "qsv=qsv@va",
                    "-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk"]
            accel = []
            # the QSV encoder takes the frames from system memory
            vf = _cpu_render(render, plan.ten_bit, _cpu_pre(deinterlace, crop))
        elif software_decode:
            accel = []
            vf = (f"{setparams},{_cpu_pre(deinterlace, crop)}format={fmt}{'le' if plan.ten_bit else ''},hwupload,"
                  f"{scale},{hwmap}")
        look = str(lookahead)
        if plan.codec == "av1":
            encoder = ["-c:v:0", "av1_qsv", "-preset", preset, "-extbrc", "1", "-look_ahead_depth", look,
                       "-global_quality", _value(quality, "av1")]
        elif plan.codec == "hevc":
            encoder = ["-c:v:0", "hevc_qsv", "-profile:v:0", "main10" if plan.ten_bit else "main", "-preset", preset,
                       "-extbrc", "1", "-look_ahead_depth", look, "-global_quality", _value(quality, "hevc")]
        else:
            encoder = ["-c:v:0", "h264_qsv", "-profile:v:0", "high", "-preset", preset, "-extbrc", "1",
                       "-look_ahead_depth", look, "-global_quality", _value(quality, "h264")]
        return Pipeline(init, accel, vf, encoder, ["-async_depth", "4"])

    def test_commands(self, device: str) -> dict[tuple[str, bool], list[str]]:
        init = ["-init_hw_device", f"vaapi=va:{device}", "-init_hw_device", "qsv=qsv@va", "-filter_hw_device", "va"]

        def vf(ten): return ["-vf", f"format={'p010le' if ten else 'nv12'},hwupload,scale_vaapi=format="
                                    f"{'p010' if ten else 'nv12'},hwmap=derive_device=qsv,format=qsv"]
        return {("av1", True): [*init, *TEST_SOURCE, *vf(True), "-c:v", "av1_qsv"],
                ("hevc", True): [*init, *TEST_SOURCE, *vf(True), "-c:v", "hevc_qsv", "-profile:v", "main10"],
                ("hevc", False): [*init, *TEST_SOURCE, *vf(False), "-c:v", "hevc_qsv", "-profile:v", "main"],
                ("h264", False): [*init, *TEST_SOURCE, *vf(False), "-c:v", "h264_qsv"]}


class VaapiBackend:
    """VAAPI decode, scaling and encode (Intel through iHD/i965, AMD through radeonsi). Rate control: ICQ with the
    GPU quality values where the driver offers it (Intel), else constant QP with the same numbers (AMD; provisional).
    Tone mapping uses the GPU's own filter (libplacebo frames can't reach a VAAPI encoder); Dolby Vision profile 5
    goes to the CPU encoder. Verified on Intel Arc; untested on AMD."""
    name, label, verified = "vaapi", "VAAPI (Intel, AMD)", True
    encoder_names = {"av1": "av1_vaapi", "hevc": "hevc_vaapi", "h264": "h264_vaapi"}
    per_device = True

    def __init__(self, rate_control: Optional[dict] = None):
        self.rate_control = rate_control or {}  # device -> "ICQ" or "CQP", from the capability check

    def pipeline(self, plan: Plan, *, render: Optional[str], setparams: str, color_range: str, width: int,
                 height: int, software_decode: bool, quality: dict, preset: str, hdr: bool,
                 gpu_tonemap: bool = False, deinterlace: bool = False, crop: Optional[tuple] = None,
                 lookahead: int = 100) -> Pipeline:
        w, h = _scale(width, height)
        fmt = "p010" if plan.ten_bit else "nv12"
        scale = f"scale_vaapi={w}:{h}:force_original_aspect_ratio=decrease:format={fmt}:out_range={color_range}"
        init = ["-init_hw_device", f"vaapi=va:{plan.device}", "-filter_hw_device", "va"]
        accel = ["-hwaccel", "vaapi", "-hwaccel_device", "va", "-hwaccel_output_format", "vaapi"]
        deint = "deinterlace_vaapi=rate=frame," if deinterlace else ""
        vf = f"{setparams},{deint}{scale}"
        if crop and not render:
            # VAAPI's scaler can't crop: decode on the CPU, crop (and deinterlace) there, then upload
            accel = []
            vf = f"{setparams},{_cpu_pre(deinterlace, crop)}format={fmt}{'le' if plan.ten_bit else ''},hwupload,{scale}"
            if gpu_tonemap:
                vf = (f"{_cpu_pre(deinterlace, crop)}format=p010le,hwupload,{tonemap_vaapi(fmt)},{setparams},"
                      f"{scale}")
        elif gpu_tonemap and not software_decode:
            vf = f"{deint}{tonemap_vaapi(fmt)},{setparams},{scale}"
        elif render:
            # ffmpeg 8.1 can't hand frames that went through Vulkan to a VAAPI encoder (hwupload from a Vulkan filter
            # device fails): media_convert_5.build_cmd tone maps with the GPU's own filter instead and sends Dolby
            # Vision profile 5 to the CPU encoder, so this branch is never used
            raise ValueError("VAAPI encoders can't take libplacebo-rendered frames")
        elif software_decode:
            accel = []
            vf = f"{setparams},{_cpu_pre(deinterlace, None)}format={fmt}{'le' if plan.ten_bit else ''},hwupload,{scale}"
        value = _value(quality, plan.codec)
        rc = (["-rc_mode", "ICQ", "-global_quality", value] if self.rate_control.get(plan.device, "ICQ") == "ICQ"
              else ["-rc_mode", "CQP", "-qp", value])
        encoder = ["-c:v:0", f"{plan.codec}_vaapi", *rc]
        if plan.codec == "hevc":
            encoder += ["-profile:v:0", "main10" if plan.ten_bit else "main"]
        elif plan.codec == "h264":
            encoder += ["-profile:v:0", "high"]
        return Pipeline(init, accel, vf, encoder, [])

    def test_commands(self, device: str) -> dict[tuple[str, bool], list[str]]:
        def src(ten): return ["-vaapi_device", device, *TEST_SOURCE, "-vf",
                              f"format={'p010le' if ten else 'nv12'},hwupload"]
        return {("av1", True): [*src(True), "-c:v", "av1_vaapi"],
                ("hevc", True): [*src(True), "-c:v", "hevc_vaapi", "-profile:v", "main10"],
                ("hevc", False): [*src(False), "-c:v", "hevc_vaapi", "-profile:v", "main"],
                ("h264", False): [*src(False), "-c:v", "h264_vaapi"]}


class NvencBackend:
    """NVIDIA: CUDA decode and scaling, NVENC encode with constant quality (-cq, the GPU quality values; provisional).
    Needs the container started with NVIDIA's runtime. Written from the ffmpeg documentation; NOT TESTED on real
    hardware (HDR10 static metadata in particular)."""
    name, label, verified = "nvenc", "NVIDIA NVENC (untested)", True
    encoder_names = {"av1": "av1_nvenc", "hevc": "hevc_nvenc", "h264": "h264_nvenc"}
    per_device = False

    def pipeline(self, plan: Plan, *, render: Optional[str], setparams: str, color_range: str, width: int,
                 height: int, software_decode: bool, quality: dict, preset: str, hdr: bool,
                 gpu_tonemap: bool = False, deinterlace: bool = False, crop: Optional[tuple] = None,
                 lookahead: int = 100) -> Pipeline:
        w, h = _scale(width, height)
        fmt = "p010le" if plan.ten_bit else "nv12"
        init: list[str] = []
        accel = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        deint = "bwdif_cuda=mode=send_frame," if deinterlace else ""
        vf = f"{setparams},{deint}scale_cuda={w}:{h}:force_original_aspect_ratio=decrease:format={fmt}"
        if render:
            init = ["-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk"]
            accel = []
            vf = _cpu_render(render, plan.ten_bit, _cpu_pre(deinterlace, crop))
        elif software_decode or crop:  # scale_cuda can't crop: the CPU does
            accel = []
            vf = (f"{setparams},{_cpu_pre(deinterlace, crop)}scale={w}:{h}:force_original_aspect_ratio=decrease:"
                  f"out_range={color_range},format={fmt}")
        encoder = ["-c:v:0", f"{plan.codec}_nvenc", "-preset", NVENC_PRESETS.get(preset, "p6"), "-tune", "hq",
                   "-rc", "vbr", "-cq", _value(quality, plan.codec), "-b:v", "0", "-rc-lookahead",
                   str(min(lookahead, 32))]
        if plan.codec == "hevc":
            encoder += ["-profile:v:0", "main10" if plan.ten_bit else "main"]
        elif plan.codec == "h264":
            encoder += ["-profile:v:0", "high"]
        return Pipeline(init, accel, vf, encoder, [])

    def test_commands(self, device: str) -> dict[tuple[str, bool], list[str]]:
        def src(ten): return [*TEST_SOURCE, "-pix_fmt", "p010le" if ten else "nv12"]
        return {("av1", True): [*src(True), "-c:v", "av1_nvenc"],
                ("hevc", True): [*src(True), "-c:v", "hevc_nvenc", "-profile:v", "main10"],
                ("hevc", False): [*src(False), "-c:v", "hevc_nvenc", "-profile:v", "main"],
                ("h264", False): [*src(False), "-c:v", "h264_nvenc"]}


class AmfBackend:
    """AMD AMF: CPU decode and scaling, AMF encode with constant QP (the GPU quality values; provisional). Needs
    AMD's proprietary AMF runtime in the container. Written from the ffmpeg documentation; NOT TESTED."""
    name, label, verified = "amf", "AMD AMF (untested)", True
    encoder_names = {"av1": "av1_amf", "hevc": "hevc_amf", "h264": "h264_amf"}
    per_device = False

    def pipeline(self, plan: Plan, *, render: Optional[str], setparams: str, color_range: str, width: int,
                 height: int, software_decode: bool, quality: dict, preset: str, hdr: bool,
                 gpu_tonemap: bool = False, deinterlace: bool = False, crop: Optional[tuple] = None,
                 lookahead: int = 100) -> Pipeline:
        w, h = _scale(width, height)
        fmt = "p010le" if plan.ten_bit else "nv12"
        scale = f"scale={w}:{h}:force_original_aspect_ratio=decrease:out_range={color_range}"
        init: list[str] = []
        vf = f"{setparams},{_cpu_pre(deinterlace, crop)}{scale},format={fmt}"
        if render:
            init = ["-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk"]
            vf = _cpu_render(render, plan.ten_bit, _cpu_pre(deinterlace, crop))
        value = _value(quality, plan.codec)
        encoder = ["-c:v:0", f"{plan.codec}_amf", "-quality", AMF_QUALITY.get(preset, "quality"), "-rc", "cqp",
                   "-qp_i", value, "-qp_p", value]
        if plan.codec == "av1":
            encoder += ["-qp_b", value]
        elif plan.codec == "hevc":
            encoder += ["-profile:v:0", "main10" if plan.ten_bit else "main"]
        return Pipeline(init, [], vf, encoder, [])

    def test_commands(self, device: str) -> dict[tuple[str, bool], list[str]]:
        def src(ten): return [*TEST_SOURCE, "-pix_fmt", "p010le" if ten else "nv12"]
        return {("av1", True): [*src(True), "-c:v", "av1_amf"],
                ("hevc", True): [*src(True), "-c:v", "hevc_amf"],
                ("hevc", False): [*src(False), "-c:v", "hevc_amf"],
                ("h264", False): [*src(False), "-c:v", "h264_amf"]}


class SoftwareBackend:
    """The CPU: SVT-AV1 and x265 (10-bit), x264 (8-bit). libplacebo renders need Vulkan (a CPU Vulkan driver such as
    lavapipe works, slowly). Static HDR10 metadata comes from the frames' side data; x265 repeats it in every
    keyframe (hdr10-opt, repeat-headers)."""
    name, label, verified = "software", "CPU (SVT-AV1, x265, x264)", True
    encoder_names = {"av1": "libsvtav1", "hevc": "libx265", "h264": "libx264"}
    per_device = False

    def pipeline(self, plan: Plan, *, render: Optional[str], setparams: str, color_range: str, width: int,
                 height: int, software_decode: bool, quality: dict, preset: str, hdr: bool,
                 gpu_tonemap: bool = False, deinterlace: bool = False, crop: Optional[tuple] = None,
                 lookahead: int = 100) -> Pipeline:
        w, h = _scale(width, height)
        scale = f"scale={w}:{h}:force_original_aspect_ratio=decrease:out_range={color_range}"
        pix = "yuv420p" if plan.codec == "h264" else "yuv420p10le"
        init: list[str] = []
        vf = f"{setparams},{_cpu_pre(deinterlace, crop)}{scale},format={pix}"
        if render:
            init = ["-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk"]
            vf = _cpu_render(render, pre=_cpu_pre(deinterlace, crop)) + f",format={pix}"
        if plan.codec == "av1":
            encoder = ["-c:v:0", "libsvtav1", "-preset", str(SVT_PRESETS.get(preset, 5)),
                       "-crf", _value(quality, "av1", True), "-svtav1-params", f"lookahead={min(lookahead, 120)}"]
        elif plan.codec == "hevc":
            params = ("hdr10-opt=1:repeat-headers=1" if hdr else "repeat-headers=1") + f":rc-lookahead={lookahead}"
            encoder = ["-c:v:0", "libx265", "-profile:v:0", "main10", "-preset", preset,
                       "-crf", _value(quality, "hevc", True), "-x265-params", params]
        else:
            encoder = ["-c:v:0", "libx264", "-profile:v:0", "high", "-preset", preset,
                       "-crf", _value(quality, "h264", True), "-rc-lookahead", str(lookahead)]
        return Pipeline(init, [], vf, encoder, [])

    def test_commands(self, device: str) -> dict[tuple[str, bool], list[str]]:
        return {("av1", True): [*TEST_SOURCE, "-pix_fmt", "yuv420p10le", "-c:v", "libsvtav1", "-preset", "12"],
                ("hevc", True): [*TEST_SOURCE, "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset",
                                 "ultrafast"],
                ("h264", False): [*TEST_SOURCE, "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast"]}


def render_devices() -> list[str]:
    return sorted(glob.glob("/dev/dri/renderD*"))


def all_devices() -> list[str]:
    return render_devices() + sorted(glob.glob("/dev/nvidia[0-9]*"))


def backend(name: str, capabilities: Optional[dict] = None):
    rate_control = {r["device"]: r.get("rate_control", "ICQ") for r in (capabilities or {}).get("results", [])
                    if r["backend"] == "vaapi" and r.get("ok")}
    return {"qsv": QsvBackend(), "vaapi": VaapiBackend(rate_control), "nvenc": NvencBackend(), "amf": AmfBackend(),
            "software": SoftwareBackend()}[name]


def conversion_backends() -> tuple[str, ...]:
    return ("qsv", "vaapi", "nvenc", "amf", "software")


def ffmpeg_version() -> str:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.splitlines()[0] if out else ""


def listed_encoders() -> set[str]:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {m.group(1) for m in re.finditer(r"^\s*V\S*\s+(\S+)", out, re.M)}


def _try(args: list[str]) -> tuple[bool, str]:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args, "-frames:v", "12", "-f", "null",
           "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TEST_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)[:300]
    if p.returncode == 0:
        return True, ""
    generic = ("Nothing was written", "Conversion failed", "Error while", "Terminating thread", "Task finished")
    lines = [re.sub(r"^\[[^\]]*\]\s*", "", line).strip() for line in p.stderr.splitlines() if line.strip()]
    useful = [line for line in lines if line and not any(g in line for g in generic)]
    return False, (useful[0] if useful else lines[-1] if lines else f"exit {p.returncode}")[:300]


def detect() -> dict:
    """Prove each backend, device, codec and bit depth with a test encode; also libplacebo on Vulkan (Dolby Vision
    profile 5). Stored and shown on the Hardware page."""
    listed = listed_encoders()
    devices = render_devices() or [DEFAULT_RENDER_DEVICE]
    results = []
    for name in ("qsv", "vaapi", "nvenc", "amf", "software"):
        b = backend(name)
        for device in (devices if b.per_device else [name]):
            for (codec, ten_bit), args in b.test_commands(device).items():
                encoder = b.encoder_names[codec]
                ok, error = (False, "not in this ffmpeg build") if encoder not in listed else _try(args)
                result = {"backend": name, "label": b.label, "device": device, "codec": codec, "ten_bit": ten_bit,
                          "encoder": encoder, "ok": ok, "error": error, "used_for_conversion": True}
                if name == "vaapi" and ok:
                    icq, _ = _try([*args, "-rc_mode", "ICQ", "-global_quality", "25"])
                    result["rate_control"] = "ICQ" if icq else "CQP"
                results.append(result)
    vulkan_ok, vulkan_error = _try(["-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk", *TEST_SOURCE, "-vf",
                                    "format=p010le,hwupload,libplacebo=format=p010,hwdownload,format=p010le"])
    return {"version": CHECK_VERSION, "checked_at": time.time(), "ffmpeg": ffmpeg_version(), "devices": all_devices(),
            "results": results,
            "libplacebo_vulkan": {"ok": vulkan_ok, "error": vulkan_error}}


def working(capabilities: Optional[dict], name: str, codec: str, ten_bit: bool) -> list[str]:
    """Devices on which this backend encoded this codec at this bit depth in the check."""
    per_device = name in ("qsv", "vaapi")
    return [r.get("device") or (DEFAULT_RENDER_DEVICE if per_device else name)  # checks stored by older versions
            for r in (capabilities or {}).get("results", [])
            if r["backend"] == name and r["codec"] == codec and r.get("ten_bit", True) == ten_bit and r["ok"]]


def plan(*, codec: str, hdr: bool, preference: str, capabilities: Optional[dict],
         fallback: str = "gpu") -> Plan:
    """The encoder for one conversion to `codec` ("av1", "hevc" or "h264"); HDR output needs 10-bit HEVC or AV1.

    10-bit where possible; SDR may use 8-bit HEVC or H.264. Automatic: GPUs first (GPU_ORDER, every device). When no
    GPU encodes the codec: with fallback "gpu" another codec on a GPU (HEVC 10-bit, then for SDR HEVC 8-bit and
    H.264), much faster than the CPU though larger; with "cpu", or when no GPU encodes anything usable, the codec on
    the CPU. A chosen backend is used even when its check failed (the conversion then fails visibly). Without a
    check: Intel QSV on the default device, as before backends existed."""
    if not capabilities:
        name = "qsv" if preference == "auto" else preference
        return Plan(name, DEFAULT_RENDER_DEVICE if name in ("qsv", "vaapi") else name, codec, codec != "h264")
    first = {"av1": [("av1", True)], "hevc": [("hevc", True)] + ([] if hdr else [("hevc", False)]),
             "h264": [("h264", False)]}[codec]
    names = GPU_ORDER if preference == "auto" else (preference,)
    for want, ten_bit in first:
        for name in names:
            devices = working(capabilities, name, want, ten_bit)
            if devices:
                return Plan(name, devices[0], want, ten_bit)
    if preference != "auto":
        device = (render_devices() or [DEFAULT_RENDER_DEVICE])[0] if preference in ("qsv", "vaapi") else preference
        return Plan(preference, device, codec, codec != "h264")
    if fallback == "gpu":
        others = [("hevc", True)] + ([] if hdr else [("hevc", False), ("h264", False)])
        for want, ten_bit in others:
            if want == codec:
                continue
            for name in GPU_ORDER:
                devices = working(capabilities, name, want, ten_bit)
                if devices:
                    return Plan(name, devices[0], want, ten_bit)
    return Plan("software", "software", codec, codec != "h264")


def stale(capabilities: Optional[dict]) -> bool:
    """True when the stored check is missing, from an older version of the check, or made with another ffmpeg or
    other devices."""
    if not capabilities or capabilities.get("version") != CHECK_VERSION:
        return True
    return capabilities.get("ffmpeg") != ffmpeg_version() or capabilities.get("devices") != all_devices()
