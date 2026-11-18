"""Fold forced subtitles into the full tracks of the same language.

A forced track carries only the lines a viewer needs when the audio is already in their language: foreign dialogue and
on-screen text. The full track usually has those lines too, but not always (signs and captions are the usual gap).
This module finds forced cues the full track lacks and inserts them, so the forced track can be dropped.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")
_MARKUP = re.compile(r"<[^>]+>|\{[^}]*\}")
_INVISIBLE = re.compile(r"[​-‏⁠﻿]")
SLACK = 1.0  # seconds of timing difference still treated as the same moment
SIMILAR = 0.6  # SequenceMatcher ratio above which two lines say the same thing


@dataclass
class Cue:
    start: float
    end: float
    lines: list[str]

    @property
    def words(self) -> str:
        text = _INVISIBLE.sub("", _MARKUP.sub("", " ".join(self.lines)))
        return re.sub(r"[^\w]+", " ", text.lower()).strip()


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[Cue]:
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")):
        lines = block.strip("\n").split("\n")
        for i, line in enumerate(lines):
            m = _TIME.search(line)
            if m:
                g = m.groups()
                cues.append(Cue(_seconds(*g[:4]), _seconds(*g[4:]), [l for l in lines[i + 1:] if l.strip()]))
                break
    return cues


def _stamp(t: float) -> str:
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def format_srt(cues: list[Cue]) -> str:
    return "".join(f"{n}\n{_stamp(c.start)} --> {_stamp(c.end)}\n" + "\n".join(c.lines) + "\n\n"
                   for n, c in enumerate(cues, 1))


def _covered(cue: Cue, full: list[Cue]) -> bool:
    words = cue.words
    for other in full:
        if other.end < cue.start - SLACK or other.start > cue.end + SLACK:
            continue
        theirs = other.words
        if words in theirs or difflib.SequenceMatcher(None, words, theirs).ratio() > SIMILAR:
            return True
    return False


def missing_cues(forced: list[Cue], full: list[Cue]) -> list[Cue]:
    """Forced cues with visible text that no full cue around the same time already says."""
    return [c for c in forced if c.words and not _covered(c, full)]


def merge(forced_text: str, full_text: str) -> tuple[str, int]:
    """Return the full track with the missing forced cues inserted in time order, and how many were added. The full
    text comes back unchanged when nothing is missing."""
    forced, full = parse_srt(forced_text), parse_srt(full_text)
    add = missing_cues(forced, full)
    if not add:
        return full_text, 0
    return format_srt(sorted(full + add, key=lambda c: (c.start, c.end))), len(add)
