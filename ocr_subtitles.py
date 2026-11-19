"""OCR for Blu-ray bitmap subtitles (PGS / .sup) into SRT, with Tesseract.

A PGS stream is a series of display sets: a presentation composition (PCS) placing one or more run-length encoded
bitmap objects (ODS) coloured by a palette (PDS). A composition with objects starts a subtitle at its timestamp; the
next composition (often an empty one) ends it. Each object is rendered as white text on black from the palette's
luma and alpha, inverted to dark-on-light, padded, and read by Tesseract (`--psm 6`, one block of text). The mean word
confidence decides whether the result is trustworthy enough to install.
"""
from __future__ import annotations

import io
import re
import struct
import subprocess
from dataclasses import dataclass, field

TESSERACT_LANG = {"en": "eng", "nl": "nld", "pt": "por", "pt-BR": "por",
                  "und": "eng+nld+por"}  # language unknown: read with every wanted model to tell which it is
MIN_CONFIDENCE = 75.0   # mean Tesseract word confidence needed to install an OCR'd subtitle
PAD = 12                # pixels of white border around each object (Tesseract reads edge text poorly)


@dataclass
class Composition:
    start: float
    end: float | None = None
    objects: list[tuple[int, int, int]] = field(default_factory=list)  # (object id, x, y)
    palette: dict[int, tuple[int, int]] = field(default_factory=dict)  # entry -> (luma, alpha)
    bitmaps: dict[int, tuple[int, int, bytes]] = field(default_factory=dict)  # id -> (width, height, rle)


def _rle_decode(data: bytes, width: int, height: int) -> bytearray:
    """PGS run-length data to one palette index per pixel."""
    out, i, n = bytearray(width * height), 0, len(data)
    x = y = 0
    while i < n and y < height:
        b = data[i]
        i += 1
        if b:
            run, color = 1, b
        else:
            if i >= n:
                break
            c = data[i]
            i += 1
            if c == 0:  # end of line
                x, y = 0, y + 1
                continue
            flag, run = c & 0xC0, c & 0x3F
            if flag & 0x40:
                run = (run << 8) | data[i]
                i += 1
            if flag & 0x80:
                color = data[i]
                i += 1
            else:
                color = 0
        end = min(x + run, width)
        if y < height:
            out[y * width + x:y * width + end] = bytes([color]) * (end - x)
        x = end
    return out


def parse_sup(data: bytes) -> list[Composition]:
    """Display sets with their bitmaps, start and end times (seconds)."""
    out: list[Composition] = []
    palettes: dict[int, dict[int, tuple[int, int]]] = {}
    objects: dict[int, list] = {}  # id -> [width, height, rle bytearray]
    current: Composition | None = None
    pos = 0
    while pos + 13 <= len(data):
        if data[pos:pos + 2] != b"PG":
            pos += 1
            continue
        pts = struct.unpack(">I", data[pos + 2:pos + 6])[0] / 90000
        kind, size = data[pos + 10], struct.unpack(">H", data[pos + 11:pos + 13])[0]
        seg = data[pos + 13:pos + 13 + size]
        pos += 13 + size
        if kind == 0x16 and len(seg) >= 11:  # PCS
            palette_id, count = seg[9], seg[10]
            if current is not None and current.end is None:
                current.end = pts
            if count:
                current = Composition(start=pts)
                current.palette_id = palette_id
                p = 11
                for _ in range(count):
                    if p + 8 > len(seg):
                        break
                    oid, _wid, cropped, x, y = struct.unpack(">HBBHH", seg[p:p + 8])
                    current.objects.append((oid, x, y))
                    p += 8 + (8 if cropped & 0x40 else 0)
                out.append(current)
            else:
                current = None
        elif kind == 0x14 and len(seg) >= 2:  # PDS
            pal = palettes.setdefault(seg[0], {})
            for p in range(2, len(seg) - 4, 5):
                pal[seg[p]] = (seg[p + 1], seg[p + 4])  # luma, alpha
        elif kind == 0x15 and len(seg) >= 4:  # ODS
            oid, flags = struct.unpack(">H", seg[0:2])[0], seg[3]
            if flags & 0x80 and len(seg) >= 11:  # first fragment: length, size, data
                w, h = struct.unpack(">HH", seg[7:11])
                objects[oid] = [w, h, bytearray(seg[11:])]
            elif oid in objects:
                objects[oid][2] += seg[4:]
        elif kind == 0x80 and current is not None:  # END of display set: bind palette and bitmaps
            current.palette = dict(palettes.get(getattr(current, "palette_id", 0), {}))
            for oid, _, _ in current.objects:
                if oid in objects:
                    w, h, rle = objects[oid]
                    current.bitmaps[oid] = (w, h, bytes(rle))
    if out and out[-1].end is None:
        out[-1].end = out[-1].start + 4.0
    return [c for c in out if c.bitmaps and c.end and c.end > c.start]


def render(width: int, height: int, rle: bytes, palette: dict[int, tuple[int, int]]):
    """Dark text on a light background, padded (Pillow image, mode L)."""
    from PIL import Image, ImageOps
    idx = _rle_decode(rle, width, height)
    lut = bytes(min(255, luma * alpha // 255) for luma, alpha in (palette.get(i, (0, 0)) for i in range(256)))
    img = Image.frombytes("L", (width, height), bytes(idx).translate(lut))
    img = ImageOps.invert(img)
    return ImageOps.expand(img, border=PAD, fill=255)


def tesseract(image, language: str) -> tuple[str, list[float]]:
    """(text with line breaks, word confidences) for one rendered image."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    out = subprocess.run(["tesseract", "stdin", "stdout", "-l", language, "--psm", "6", "tsv"],
                         input=buf.getvalue(), capture_output=True, check=True).stdout.decode("utf-8", "replace")
    lines: dict[tuple, list[str]] = {}
    confs = []
    for row in out.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        lines.setdefault((int(cols[2]), int(cols[3]), int(cols[4])), []).append(cols[11])
        try:
            confs.append(float(cols[10]))
        except ValueError:
            pass
    return "\n".join(" ".join(words) for _, words in sorted(lines.items())), confs


_FIXES = [(re.compile(r"(?<![\w'])\|(?![\w'])"), "I"), (re.compile(r"\|"), "I"),
          (re.compile(r"\s+([,.!?;:])"), r"\1"), (re.compile(r"(?<=\w)''"), '"')]
_EN_FIXES = [(re.compile(r"\bl(?=['’](m|ll|d|ve)\b)"), "I"), (re.compile(r"(?<![\w'])l(?![\w'])"), "I")]


def clean(text: str, code: str) -> str:
    for pattern, repl in _FIXES + (_EN_FIXES if code == "en" else []):
        text = pattern.sub(repl, text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


@dataclass
class OcrResult:
    srt: str
    cues: int
    confidence: float  # mean word confidence (0-100)

    @property
    def trusted(self) -> bool:
        return self.cues > 0 and self.confidence >= MIN_CONFIDENCE


def sup_to_srt(data: bytes, code: str, limit: int | None = None) -> OcrResult:
    """OCR a PGS stream into SRT; `limit` stops after that many subtitles (a sample)."""
    language = TESSERACT_LANG.get(code, "eng")
    blocks, confs = [], []
    for comp in parse_sup(data):
        if limit is not None and len(blocks) >= limit:
            break
        texts = []
        for oid, _x, y in sorted(comp.objects, key=lambda o: o[2]):  # top to bottom
            if oid not in comp.bitmaps:
                continue
            w, h, rle = comp.bitmaps[oid]
            text, c = tesseract(render(w, h, rle, comp.palette), language)
            confs += c
            text = clean(text, code)
            if text:
                texts.append(text)
        if texts:
            blocks.append(f"{len(blocks) + 1}\n{_ts(comp.start)} --> {_ts(comp.end)}\n" + "\n".join(texts) + "\n")
    return OcrResult("\n".join(blocks), len(blocks), sum(confs) / len(confs) if confs else 0.0)


# ---- DVD subtitles (VobSub: .idx + .sub, as written by mkvextract) ----------------------------------------------

@dataclass
class Spu:
    start: float
    end: float | None
    width: int
    height: int
    pixels: bytes          # one palette slot (0-3) per pixel
    colors: tuple          # slot -> index into the 16-colour palette
    alpha: tuple           # slot -> 0-15


def parse_idx(text: str) -> tuple[list[tuple[int, int, int]], list[tuple[float, int]]]:
    """(16-colour palette as RGB, [(timestamp seconds, file position in .sub)])."""
    palette, entries = [], []
    for line in text.splitlines():
        if line.startswith("palette:"):
            palette = [tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) for c in
                       (x.strip() for x in line.split(":", 1)[1].split(",")) if len(c) == 6]
        m = re.match(r"timestamp:\s*(-?)(\d+):(\d+):(\d+):(\d+),\s*filepos:\s*([0-9a-fA-F]+)", line)
        if m:
            sign = -1 if m.group(1) else 1
            h, mi, s, ms = (int(m.group(i)) for i in range(2, 6))
            entries.append((sign * (h * 3600 + mi * 60 + s + ms / 1000), int(m.group(6), 16)))
    return palette, entries


def _spu_packet(sub: bytes, pos: int) -> bytes:
    """Reassemble one SPU from the MPEG program stream packets starting at `pos` (private stream 1)."""
    payload = bytearray()
    size = None
    while pos + 4 <= len(sub) and (size is None or len(payload) < size):
        if sub[pos:pos + 4] == b"\x00\x00\x01\xba":  # pack header (MPEG-2)
            stuffing = sub[pos + 13] & 0x07 if pos + 13 < len(sub) else 0
            pos += 14 + stuffing
            continue
        if sub[pos:pos + 3] != b"\x00\x00\x01":
            break
        stream_id = sub[pos + 3]
        length = struct.unpack(">H", sub[pos + 4:pos + 6])[0]
        body = sub[pos + 6:pos + 6 + length]
        pos += 6 + length
        if stream_id != 0xBD or len(body) < 3:
            continue
        header_len = body[2]
        data = body[3 + header_len:]
        if not data:
            continue
        payload += data[1:]  # first byte: substream id (0x20-0x3f)
        if size is None and len(payload) >= 2:
            size = struct.unpack(">H", payload[0:2])[0]
    return bytes(payload[:size] if size else payload)


def _decode_field(data: bytes, offset: int, width: int, lines: int) -> list[bytearray]:
    """Nibble-based RLE of one interlaced field."""
    rows, nib = [], offset * 2
    get = lambda n: (data[n // 2] >> 4) if n % 2 == 0 else (data[n // 2] & 0x0F)
    total = len(data) * 2
    for _ in range(lines):
        row, x = bytearray(width), 0
        while x < width and nib < total:
            v = get(nib); nib += 1
            if v < 0x4 and nib < total:
                v = (v << 4) | get(nib); nib += 1
                if v < 0x10 and nib < total:
                    v = (v << 4) | get(nib); nib += 1
                    if v < 0x40 and nib < total:
                        v = (v << 4) | get(nib); nib += 1
            run, color = v >> 2, v & 3
            if run == 0:
                run = width - x
            end = min(width, x + run)
            row[x:end] = bytes([color]) * (end - x)
            x = end
        if nib % 2:
            nib += 1  # each line starts on a byte boundary
        rows.append(row)
    return rows


def parse_spu(packet: bytes, start: float) -> Spu | None:
    if len(packet) < 4:
        return None
    ctrl = struct.unpack(">H", packet[2:4])[0]
    colors, alpha, coords, offsets, show, hide = (0, 1, 2, 3), (0, 15, 15, 15), None, None, 0.0, None
    pos, last = ctrl, -1
    while pos + 4 <= len(packet) and pos != last:
        last = pos
        raw_delay = struct.unpack(">H", packet[pos:pos + 2])[0]
        delay = raw_delay * 1024 / 90000
        nxt = struct.unpack(">H", packet[pos + 2:pos + 4])[0]
        p = pos + 4
        while p < len(packet):
            cmd = packet[p]; p += 1
            if cmd == 0x01:
                show = delay
            elif cmd == 0x02:
                hide = None if raw_delay == 0xFFFF else delay  # 0xFFFF: end unknown (some encoders)
            elif cmd == 0x03:
                b = packet[p:p + 2]; p += 2
                colors = (b[1] & 0xF, b[1] >> 4, b[0] & 0xF, b[0] >> 4)
            elif cmd == 0x04:
                b = packet[p:p + 2]; p += 2
                alpha = (b[1] & 0xF, b[1] >> 4, b[0] & 0xF, b[0] >> 4)
            elif cmd == 0x05:
                b = packet[p:p + 6]; p += 6
                coords = ((b[0] << 4) | (b[1] >> 4), ((b[1] & 0xF) << 8) | b[2],
                          (b[3] << 4) | (b[4] >> 4), ((b[4] & 0xF) << 8) | b[5])
            elif cmd == 0x06:
                offsets = struct.unpack(">HH", packet[p:p + 4]); p += 4
            elif cmd == 0xFF:
                break
            elif cmd == 0x00:
                continue
            else:
                break
        pos = nxt
    if not coords or not offsets:
        return None
    x1, x2, y1, y2 = coords
    width, height = x2 - x1 + 1, y2 - y1 + 1
    if width <= 0 or height <= 0:
        return None
    top = _decode_field(packet, offsets[0], width, (height + 1) // 2)
    bottom = _decode_field(packet, offsets[1], width, height // 2)
    rows = [r for pair in zip(top, bottom) for r in pair] + top[len(bottom):]
    return Spu(start + show, start + hide if hide is not None else None, width, height,
               b"".join(rows[:height]), colors, alpha)


def render_spu(spu: Spu, palette: list[tuple[int, int, int]]):
    """Dark text on light, padded. DVD subtitles draw text in one slot and an outline in another; the brightest
    opaque slot is taken as the text, everything else as background."""
    from PIL import Image, ImageOps
    luma = []
    for slot in range(4):
        r, g, b = palette[spu.colors[slot]] if spu.colors[slot] < len(palette) else (0, 0, 0)
        luma.append((299 * r + 587 * g + 114 * b) // 1000 if spu.alpha[slot] >= 8 else 0)
    text_slot = max(range(4), key=lambda s: luma[s])
    lut = bytes(0 if s == text_slot and luma[s] > 0 else 255 for s in range(4)) + bytes(252)
    img = Image.frombytes("L", (spu.width, spu.height), spu.pixels.translate(lut))
    return ImageOps.expand(img, border=PAD, fill=255)


def vobsub_to_srt(idx_text: str, sub: bytes, code: str, limit: int | None = None) -> OcrResult:
    language = TESSERACT_LANG.get(code, "eng")
    palette, entries = parse_idx(idx_text)
    spus = [parse_spu(_spu_packet(sub, pos), ts) for ts, pos in entries]
    spus = [s for s in spus if s is not None]
    for a, b in zip(spus, spus[1:] + [None]):
        if a.end is None:
            a.end = b.start if b is not None else a.start + 4.0
        elif b is not None and a.end > b.start:
            a.end = b.start  # never overlap the next subtitle
    blocks, confs = [], []
    for spu in spus:
        if limit is not None and len(blocks) >= limit:
            break
        if spu.end <= spu.start:
            continue
        text, c = tesseract(render_spu(spu, palette), language)
        confs += c
        text = clean(text, code)
        if text:
            blocks.append(f"{len(blocks) + 1}\n{_ts(spu.start)} --> {_ts(spu.end)}\n{text}\n")
    return OcrResult("\n".join(blocks), len(blocks), sum(confs) / len(confs) if confs else 0.0)
