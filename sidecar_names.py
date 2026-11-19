"""Recognise subtitle sidecar names mangled by a Sonarr/Radarr rename, and work out the right name from the text.

Sonarr and Radarr move a video's sidecars when they rename it, but their subtitle parser only knows the tags
forced/foreign/default/cc/psdh/sdh and two- or three-letter language codes, and they write back only the two-letter
code. So `.en.hi.srt` comes out as `.hi.srt` (read as Hindi), `.pt-BR.srt` as `.srt` (`BR` is not a language they
know), and a second file that maps to the same name gets a number (`.1.hi.srt`). Jellyfin and Bazarr read the
original names correctly (`hi` next to a language is hearing-impaired; `pt-BR` is Brazilian Portuguese), so the
names are put back: the language from the text, the hearing-impaired and forced flags from the tags that survived.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

VALID = re.compile(r"^\.(?P<lang>[a-z]{2,3}(?:-[A-Z]{2})?)(?:\.hi)?(?:\.forced)?$")
HEARING_IMPAIRED = {"hi", "sdh", "cc"}
FORCED = {"forced", "foreign"}
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".rmvb", ".ts", ".m2ts", ".mts", ".mpg", ".mpeg",
                    ".vob", ".webm", ".flv", ".3gp")

_WORDS = {
    "en": {"the", "you", "and", "what", "that", "it's", "don't", "i'm", "this", "with", "have", "your", "just"},
    "nl": {"het", "een", "niet", "wat", "dat", "ik", "je", "van", "maar", "zijn", "hij", "wel", "jij", "ook", "nog"},
    "pt": {"não", "você", "que", "isso", "está", "eu", "para", "com", "uma", "mas", "ele", "muito", "aqui", "tem"},
    # languages that aren't kept, so their tracks are recognised (and dropped) rather than mistaken for the above
    "es": {"qué", "eso", "pero", "con", "una", "del", "usted", "él", "muy", "aquí", "tengo", "bueno", "señor", "y"},
    "fr": {"vous", "pas", "est", "c'est", "les", "une", "et", "mais", "oui", "qu'il", "moi", "suis", "ça", "bien"},
    "de": {"ich", "nicht", "das", "ist", "und", "sie", "wir", "ein", "mir", "mich", "auch", "habe", "bin", "du"},
    "it": {"non", "che", "sono", "per", "questo", "cosa", "bene", "grazie", "perché", "sei", "lei", "ho", "io", "hai"},
}
# European vs Brazilian Portuguese markers: subtitlerr's relabelling set, extended and checked against a library's
# 221 European and 400 Brazilian files (per file: "estar a" + infinitive 20.5 vs 0.2, gerund 0.0 vs 11.4). With "você"
# at a quarter, random 300-cue stretches came out right in 219/219 European and 382/383 Brazilian cases.
_PT_PT = [r"\best(ou|ás|á|amos|ão|ava|avas|ávamos|avam|ive|eve|iveram|ar) a [a-zçáéí]+(ar|er|ir)\b",
          r"\btelemóve(l|is)\b", r"\bautocarros?\b", r"\becrã\b", r"\bequipas?\b", r"\braparigas?\b",
          r"\bcasa de banho\b", r"\bfacto\b", r"\btu (estás|tens|és|vais|queres|podes|sabes|fazes|foste)\b",
          r"\bvós\b", r"\bpequeno-almoço\b", r"\bcomboios?\b", r"\b(tenho|tens|tem|temos) a certeza\b",
          r"\bmiúd[oa]s?\b", r"\bfixe\b"]
_PT_BR = [r"\bvocê(s)?\b", r"\b(est|tô|tá)[a-z]* [a-z]+(ando|endo|indo)\b", r"\bcelular(es)?\b", r"\bônibus\b",
          r"\bgarotas?\b", r"\bbanheiros?\b", r"\bcafé da manhã\b", r"\btrens?\b", r"\ba gente\b",
          r"\bpra\b", r"\boi\b"]  # "você" first: it counts a quarter (European subtitles use it too, 3 per file)
_TIMING = re.compile(r"^\s*\d+\s*$|-->")
_MARKUP = re.compile(r"<[^>]+>|\{[^}]*\}")
MIN_WORDS = 20  # marker words the winning language needs, and twice as many as the runner-up


def detect_language(text: str) -> Optional[str]:
    """The sidecar code for the language of an SRT text: en, nl, pt or pt-BR (es, fr, de, it are recognised too, so
    they aren't mistaken for one of those); None when unclear."""
    body = _MARKUP.sub(" ", "\n".join(l for l in text.splitlines() if not _TIMING.search(l))).lower()
    words = re.findall(r"[\wçãõáéíóúâêôà']+", body)
    counts = {lang: sum(1 for w in words if w in markers) for lang, markers in _WORDS.items()}
    ranked = sorted(counts, key=counts.get, reverse=True)
    best, second = ranked[0], ranked[1]
    if counts[best] < MIN_WORDS or counts[best] < 2 * counts[second]:
        return None
    return best if best != "pt" else portuguese_variant(text)


def portuguese_variant(text: str) -> str:
    """pt (European) or pt-BR for a Portuguese SRT text, from the variants' typical phrasing."""
    body = _MARKUP.sub(" ", "\n".join(l for l in text.splitlines() if not _TIMING.search(l))).lower()
    european = sum(len(re.findall(p, body)) for p in _PT_PT)
    brazilian = len(re.findall(_PT_BR[0], body)) / 4 + sum(len(re.findall(p, body)) for p in _PT_BR[1:])
    return "pt" if european > brazilian else "pt-BR"


def same_lines(a: str, b: str) -> bool:
    """Two SRT texts with the same lines, whatever their timing (a synced and an unsynced copy)."""
    strip = lambda t: [l.strip() for l in t.replace("\r", "").split("\n") if l.strip() and not _TIMING.search(l)]
    return strip(a) == strip(b)


def is_valid_suffix(suffix: str) -> bool:
    """`.en.srt`-style names minus `.srt`: a language code (not a flag word such as `hi` or `sdh`, which a rename
    leaves in place of the language), optionally `.hi` and `.forced`."""
    match = VALID.match(suffix)
    return bool(match) and match["lang"].lower() not in HEARING_IMPAIRED | FORCED | {"default", "psdh"}


def mangled_sidecars(video_path: str, names: Iterable[str]) -> list[tuple[str, str]]:
    """(file name, suffix) of the .srt sidecars of `video_path` among `names` (a directory listing) whose suffix is
    not a valid sidecar suffix. A sidecar that belongs to another video with a longer name is left out."""
    names = list(names)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    others = [os.path.splitext(n)[0] for n in names
              if n.lower().endswith(VIDEO_EXTENSIONS) and os.path.splitext(n)[0] != stem]
    found = []
    for name in sorted(names):
        if not name.startswith(stem + ".") or not name.lower().endswith(".srt"):
            continue
        if any(len(o) > len(stem) and name.startswith(o + ".") for o in others):
            continue
        suffix = name[len(stem):-4]
        if not is_valid_suffix(suffix):
            found.append((name, suffix))
    return found


def tagged_language(suffix: str) -> Optional[str]:
    """A language the mangled name still states, to restore instead of guessing from the text. Sonarr and Radarr
    turn `.pt-BR.srt` into `.BR.pt.srt` (they know only two-letter codes): a name with both `pt` and `br` was
    Brazilian Portuguese. Relabelling a subtitle whose text is another variant is not the repair's job."""
    tokens = {t.lower() for t in suffix.split(".") if t}
    return "pt-BR" if {"pt", "br"} <= tokens or "pt-br" in tokens else None


def repaired_suffix(suffix: str, language: str) -> str:
    """The proper suffix (without `.srt`) for a mangled one, given the language of its text."""
    tokens = {t.lower() for t in suffix.split(".") if t}
    return ("." + language + (".hi" if tokens & HEARING_IMPAIRED else "") + (".forced" if tokens & FORCED else ""))
