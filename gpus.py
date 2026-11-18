"""The GPUs in this machine and what their hardware encoders should support.

Each display adapter is read from sysfs (vendor and device id, its render node), named from the PCI id database
(`pci.ids`) and matched to a family below. The family table is a register of documented hardware-encode support for
recent GPUs; the capability check in encoders.py stays the proof. The table explains a failed check on capable
hardware (driver, runtime, device not passed into the container) and shows what a GPU can do before it is checked.
Standard library only.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from typing import Optional

PCI_IDS = ("/usr/share/misc/pci.ids", "/usr/share/hwdata/pci.ids", "/usr/share/pci.ids")
VENDORS = {"0x8086": "Intel", "0x10de": "NVIDIA", "0x1002": "AMD"}


@dataclass(frozen=True)
class Family:
    vendor: str
    pattern: str  # matched against the PCI name ("DG2 [Arc A380]", "AD104 [GeForce RTX 4070]", "Navi 31 [...]")
    name: str
    av1: bool = False
    hevc10: bool = False
    hevc8: bool = False
    h264: bool = False
    notes: str = ""

    def encodes(self) -> list[str]:
        return [label for flag, label in ((self.av1, "AV1 10-bit"), (self.hevc10, "HEVC 10-bit"),
                                          (self.hevc8 and not self.hevc10, "HEVC 8-bit"), (self.h264, "H.264"))
                if flag]


# Most specific first. Sources: Intel media driver feature tables, NVIDIA's Video Encode and Decode support matrix,
# AMD VCN/VCE documentation (as known in 2026).
FAMILIES = (
    # Intel (QSV and VAAPI)
    Family("Intel", r"Battlemage|\[Arc (Pro )?B\d|Lunar Lake|Panther Lake|Xe2", "Intel Xe2 (Arc B-series, Lunar Lake)",
           av1=True, hevc10=True, hevc8=True, h264=True),
    Family("Intel", r"DG2|Alchemist|\[Arc (Pro )?A\d|Meteor Lake|Arrow Lake|Intel Arc Graphics",
           "Intel Xe-HPG/LPG (Arc A-series, Meteor/Arrow Lake)", av1=True, hevc10=True, hevc8=True, h264=True),
    Family("Intel", r"Tiger ?Lake|Rocket ?Lake|Alder ?Lake|Raptor ?Lake|Twin ?Lake|DG1|Iris Xe",
           "Intel Xe-LP (11th–14th gen Core)", hevc10=True, hevc8=True, h264=True,
           notes="AV1 decoding only; no AV1 encoder."),
    Family("Intel", r"Ice ?Lake|Jasper ?Lake|Elkhart ?Lake|Iris Plus Graphics G\d", "Intel Gen11 (Ice/Jasper Lake)",
           hevc10=True, hevc8=True, h264=True),
    Family("Intel", r"Kaby ?Lake|Coffee ?Lake|Comet ?Lake|Whiskey ?Lake|Amber ?Lake|Gemini ?Lake|Apollo ?Lake|"
                    r"UHD Graphics 6\d\d|HD Graphics 6[0-4]\d", "Intel Gen9.5 (7th–10th gen Core)",
           hevc10=True, hevc8=True, h264=True),
    Family("Intel", r"Skylake|HD Graphics 5[0-3]\d|Iris (Pro )?Graphics 5[0-9]\d", "Intel Gen9 (6th gen Core)",
           hevc8=True, h264=True, notes="HEVC 8-bit only: HDR goes to the CPU."),
    Family("Intel", r"Broadwell|Haswell|Braswell|Cherry ?Trail|Bay ?Trail|Ivy ?Bridge|Sandy ?Bridge|HD Graphics [45]\d00",
           "Intel Gen7/8 (4th–5th gen Core and older)", h264=True,
           notes="H.264 only: nothing this converter encodes to; the CPU is used."),
    # NVIDIA (NVENC); GP108 (GT 1030) and some low-end parts have no encoder
    Family("NVIDIA", r"\bGP108\b|\bGK208\b|\bGM108\b", "NVIDIA low-end (no NVENC)",
           notes="This chip has no hardware encoder."),
    Family("NVIDIA", r"\bGB\d{3}", "NVIDIA Blackwell (RTX 50)", av1=True, hevc10=True, hevc8=True, h264=True),
    Family("NVIDIA", r"\bAD\d{3}", "NVIDIA Ada Lovelace (RTX 40)", av1=True, hevc10=True, hevc8=True, h264=True),
    Family("NVIDIA", r"\bGA\d{3}", "NVIDIA Ampere (RTX 30)", hevc10=True, hevc8=True, h264=True,
           notes="AV1 decoding only; no AV1 encoder."),
    Family("NVIDIA", r"\bTU\d{3}|\bGV\d{3}", "NVIDIA Turing/Volta (RTX 20, GTX 16)", hevc10=True, hevc8=True,
           h264=True),
    Family("NVIDIA", r"\bGP\d{3}", "NVIDIA Pascal (GTX 10)", hevc10=True, hevc8=True, h264=True),
    Family("NVIDIA", r"\bGM20\d", "NVIDIA Maxwell 2 (GTX 900)", hevc8=True, h264=True),
    Family("NVIDIA", r"\bGM10\d|\bGK\d{3}", "NVIDIA Kepler/Maxwell 1", h264=True,
           notes="H.264 only: nothing this converter encodes to; the CPU is used."),
    # AMD (VAAPI through Mesa radeonsi; AMF with AMD's proprietary runtime)
    Family("AMD", r"Navi 24", "AMD Navi 24 (RX 6500/6400)", notes="This GPU has no hardware encoder."),
    Family("AMD", r"Navi 4\d", "AMD VCN 5 (RX 9000)", av1=True, hevc10=True, hevc8=True, h264=True),
    Family("AMD", r"Navi 3\d|Phoenix|Hawk Point|Strix|Krackan", "AMD VCN 4 (RX 7000, Ryzen 7040 and newer APUs)",
           av1=True, hevc10=True, hevc8=True, h264=True),
    Family("AMD", r"Navi 2\d|Rembrandt|Raphael|Granite Ridge|Van Gogh|Mendocino|Yellow Carp",
           "AMD VCN 3 (RX 6000, Ryzen 6000/7000 desktop graphics)", hevc10=True, hevc8=True, h264=True,
           notes="AV1 decoding only; no AV1 encoder."),
    Family("AMD", r"Navi 1\d|Renoir|Cezanne|Lucienne|Barcelo", "AMD VCN 2 (RX 5000, Ryzen 4000/5000 APUs)",
           hevc10=True, hevc8=True, h264=True),
    Family("AMD", r"Raven|Picasso|Dali|Pollock", "AMD VCN 1 (Ryzen 2000/3000 APUs)", hevc8=True, h264=True),
    Family("AMD", r"Vega|Polaris|Ellesmere|Baffin|Lexa", "AMD VCE 3.4/4 (RX 400/500, Vega)", hevc8=True, h264=True,
           notes="HEVC 8-bit only: HDR goes to the CPU."),
    Family("AMD", r"Fiji|Tonga|Hawaii|Bonaire|Tahiti|Pitcairn", "AMD VCE 1–3 (older GCN)", h264=True,
           notes="H.264 only: nothing this converter encodes to; the CPU is used."),
)

HINTS = {
    "Intel": "Pass the GPU's /dev/dri/renderD* node into the container (and the host's render group). Newer GPUs "
             "need a recent kernel and Intel media driver.",
    "NVIDIA": "Start the container with NVIDIA's runtime (Docker: --gpus all, with the NVIDIA Container Toolkit) so "
              "the driver's CUDA and NVENC libraries are available.",
    "AMD": "Pass the GPU's /dev/dri/renderD* node into the container; VAAPI uses the bundled Mesa driver. AMF also "
           "needs AMD's proprietary AMF runtime, which the image does not include.",
}


@dataclass
class Gpu:
    slot: str
    vendor: str
    vendor_id: str
    device_id: str
    name: str
    render_node: str = ""
    family: Optional[Family] = None
    tested: dict = field(default_factory=dict)  # "AV1 10-bit" -> [backends that passed]


def _read(path: str) -> str:
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def pci_name(vendor_id: str, device_id: str, databases: Optional[tuple[str, ...]] = None) -> str:
    vendor, device = vendor_id.lower().removeprefix("0x"), device_id.lower().removeprefix("0x")
    for path in databases or PCI_IDS:
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            in_vendor = False
            for line in handle:
                if line.startswith("#") or not line.strip():
                    continue
                if not line.startswith("\t"):
                    in_vendor = line[:4].lower() == vendor
                elif in_vendor and not line.startswith("\t\t") and line[1:5].lower() == device:
                    return line[5:].strip()
        break
    return ""


def family_for(vendor: str, name: str) -> Optional[Family]:
    for family in FAMILIES:
        if family.vendor == vendor and re.search(family.pattern, name, re.I):
            return family
    return None


def find(sysfs: str = "/sys/bus/pci/devices") -> list[Gpu]:
    found = []
    for path in sorted(glob.glob(os.path.join(sysfs, "*"))):
        if not _read(os.path.join(path, "class")).startswith("0x03"):
            continue
        vendor_id, device_id = _read(os.path.join(path, "vendor")), _read(os.path.join(path, "device"))
        vendor = VENDORS.get(vendor_id.lower(), vendor_id)
        name = pci_name(vendor_id, device_id) or f"device {device_id}"
        nodes = sorted(os.path.basename(p) for p in glob.glob(os.path.join(path, "drm", "renderD*")))
        found.append(Gpu(os.path.basename(path), vendor, vendor_id, device_id, name,
                         f"/dev/dri/{nodes[0]}" if nodes else "", family_for(vendor, name)))
    return found


def attach_results(gpu_list: list[Gpu], capabilities: Optional[dict]) -> list[Gpu]:
    """Which formats the capability check proved on each GPU: Intel/AMD by render node, NVIDIA by the NVENC test."""
    for gpu in gpu_list:
        gpu.tested = {}
        for r in (capabilities or {}).get("results", []):
            if not r.get("ok") or r["backend"] == "software":
                continue
            mine = (r.get("device") == gpu.render_node and gpu.render_node) or \
                   (gpu.vendor == "NVIDIA" and r["backend"] == "nvenc") or \
                   (gpu.vendor == "AMD" and r["backend"] == "amf")
            if mine:
                label = {"av1": "AV1 10-bit", "h264": "H.264"}.get(r["codec"]) or \
                    ("HEVC 10-bit" if r.get("ten_bit") else "HEVC 8-bit")
                gpu.tested.setdefault(label, []).append(r["backend"])
    return gpu_list


def missing(gpu: Gpu) -> list[str]:
    """Formats the family should encode but no backend proved on this GPU."""
    if not gpu.family:
        return []
    return [label for label in gpu.family.encodes() if label not in gpu.tested and label != "H.264"]
