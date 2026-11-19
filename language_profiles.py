"""Subtitle language detection learned from the library's own tagged subtitles, without any model.

Every tagged text subtitle the converter handles (wanted or not) is counted into a profile of its language: the
frequencies of 1–3 letter sequences inside words ("th", "ij", "ção"). An untagged track is named after the profile
nearest to its own ranked sequences (Cavnar and Trenkle's out-of-place distance).

Measured before use: about one track in five (fixed by its content hash) is held back as a test. A language is used
only when it has at least MIN_TRACKS learning tracks and MIN_HOLDOUT test tracks, its test tracks come out as it
(recall) and the test tracks named as it really are it (precision), both at least MIN_ACCURACY. A track is also
refused when it is further from the winning profile than that language's own test tracks were (with some slack), so a
language never learned (Afrikaans, say) is "unclear" rather than taken for its nearest learned neighbour (Dutch), and
when the runner-up is nearly as close. Languages that can't be told apart (Norwegian and Danish can be close) fail
the measurement and stay unused until enough tracks separate them.

Standard library and SQLite only (the converter's database).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from typing import Optional

PROFILE_SIZE = 300      # ranked sequences compared per profile
MIN_TRACKS = 5          # learning tracks before a language can be used
MIN_HOLDOUT = 2         # test tracks before a language can be used
MIN_ACCURACY = 0.95     # recall and precision on the test tracks
HOLDOUT_EVERY = 5       # one track in five is a test track
MAX_TRACKS = 200        # stop learning a language after this many tracks (profiles are stable long before)
MARGIN = 0.04           # the runner-up must be this much further (share of the winner's distance)
SLACK = 1.15            # how much further than its furthest correct test track a track may be
MIN_LETTERS = 400       # text needed to judge at all

_TIMING = re.compile(r"^\s*\d+\s*$|-->")
_MARKUP = re.compile(r"<[^>]+>|\{[^}]*\}|\[[^\]]*\]")
_WORD = re.compile(r"[^\W\d_]+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS language_grams (
    language TEXT NOT NULL,
    gram TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY(language, gram)
);
CREATE TABLE IF NOT EXISTS language_tracks (
    language TEXT NOT NULL,
    sha1 TEXT NOT NULL,
    holdout INTEGER NOT NULL,
    profile TEXT,
    source TEXT,
    added_at REAL,
    PRIMARY KEY(language, sha1)
);
"""


def text_of(srt: str) -> str:
    """The spoken text of an SRT: no numbers, timings, markup or sound descriptions."""
    return _MARKUP.sub(" ", "\n".join(l for l in srt.splitlines() if not _TIMING.search(l))).lower()


def grams(text: str) -> Counter:
    out: Counter = Counter()
    for word in _WORD.findall(text):
        padded = f" {word} "
        for n in (1, 2, 3):
            for i in range(len(padded) - n + 1):
                gram = padded[i:i + n]
                if gram.strip():
                    out[gram] += 1
    return out


def ranked(counts: Counter, size: int = PROFILE_SIZE) -> list[str]:
    return [g for g, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:size]]


def distance(doc: list[str], profile: dict[str, int]) -> int:
    """Out-of-place distance: how far each of the document's ranked sequences sits from its rank in the profile."""
    return sum(abs(i - profile[g]) if g in profile else PROFILE_SIZE for i, g in enumerate(doc))


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def learn(conn: sqlite3.Connection, language: str, srt: str, source: str = "", now: float = 0.0) -> str:
    """Count a tagged subtitle into its language's profile (or keep it as a test track). Returns what happened."""
    ensure(conn)
    text = text_of(srt)
    if sum(len(w) for w in _WORD.findall(text)) < MIN_LETTERS:
        return "too short"
    sha1 = hashlib.sha1(text.encode()).hexdigest()
    if conn.execute("SELECT 1 FROM language_tracks WHERE language=? AND sha1=?", (language, sha1)).fetchone():
        return "known"
    if conn.execute("SELECT COUNT(*) FROM language_tracks WHERE language=?", (language,)).fetchone()[0] >= MAX_TRACKS:
        return "enough"
    counts = grams(text)
    holdout = int(sha1[:8], 16) % HOLDOUT_EVERY == 0
    with conn:
        conn.execute("INSERT INTO language_tracks(language, sha1, holdout, profile, source, added_at) "
                     "VALUES(?,?,?,?,?,?)", (language, sha1, int(holdout),
                                            json.dumps(ranked(counts)) if holdout else None, source, now))
        if not holdout:
            conn.executemany("INSERT INTO language_grams(language, gram, count) VALUES(?,?,?) "
                             "ON CONFLICT(language, gram) DO UPDATE SET count=count+excluded.count",
                             [(language, g, c) for g, c in counts.items()])
    return "test" if holdout else "learned"


def wants_more(conn: sqlite3.Connection, language: str) -> bool:
    ensure(conn)
    return conn.execute("SELECT COUNT(*) FROM language_tracks WHERE language=?",
                        (language,)).fetchone()[0] < MAX_TRACKS


class Model:
    """The learned profiles and the measurement that decides which languages are used."""

    def __init__(self, conn: sqlite3.Connection):
        ensure(conn)
        self.profiles: dict[str, dict[str, int]] = {}
        self.tracks: Counter = Counter()
        tests: list[tuple[str, list[str]]] = []
        for language, holdout, profile in conn.execute("SELECT language, holdout, profile FROM language_tracks"):
            if holdout:
                tests.append((language, json.loads(profile)))
            else:
                self.tracks[language] += 1
        for language in self.tracks:
            rows = conn.execute("SELECT gram, count FROM language_grams WHERE language=? ORDER BY count DESC, gram "
                                "LIMIT ?", (language, PROFILE_SIZE)).fetchall()
            self.profiles[language] = {g: i for i, (g, _) in enumerate(rows)}
        self.tests = tests
        self.report = self._measure()

    def _nearest(self, doc: list[str], languages) -> list[tuple[int, str]]:
        return sorted((distance(doc, self.profiles[l]), l) for l in languages)

    def _measure(self) -> dict[str, dict]:
        candidates = {l for l, n in self.tracks.items() if n >= MIN_TRACKS}
        report: dict[str, dict] = {}
        while True:
            # the furthest a correctly named test track sat from its own profile, per language
            reach: dict[str, int] = {}
            stats = {l: {"tests": 0, "correct": 0, "named": 0} for l in self.profiles}
            for truth, doc in self.tests:
                if truth in stats:
                    stats[truth]["tests"] += 1
                if not candidates:
                    continue
                ranking = self._nearest(doc, candidates)
                best_d, best = ranking[0]
                if best == truth:
                    reach[best] = max(reach.get(best, 0), best_d)
            for truth, doc in self.tests:
                if not candidates:
                    break
                ranking = self._nearest(doc, candidates)
                named = self._decide(ranking, reach)
                if named is not None:
                    stats[named]["named"] += 1
                    if named == truth:
                        stats[named]["correct"] += 1
            failed = set()
            for l in candidates:
                s = stats[l]
                recall = s["correct"] / s["tests"] if s["tests"] else 0.0
                precision = s["correct"] / s["named"] if s["named"] else 0.0
                if s["tests"] < MIN_HOLDOUT or recall < MIN_ACCURACY or precision < MIN_ACCURACY:
                    failed.add(l)
            for l in self.profiles:
                s = stats[l]
                report[l] = {"tracks": self.tracks[l], "tests": s["tests"],
                             "recall": round(s["correct"] / s["tests"], 3) if s["tests"] else None,
                             "precision": round(s["correct"] / s["named"], 3) if s["named"] else None,
                             "used": l in candidates and l not in failed}
            if not failed:
                self.used = candidates
                self.reach = reach
                return report
            candidates -= failed

    @staticmethod
    def _decide(ranking: list[tuple[int, str]], reach: dict[str, int]) -> Optional[str]:
        best_d, best = ranking[0]
        if best not in reach or best_d > reach[best] * SLACK:
            return None  # further than this language's own test tracks: another language
        if len(ranking) > 1 and ranking[1][0] - best_d < MARGIN * best_d:
            return None  # the runner-up is nearly as close
        return best

    def detect(self, srt: str) -> Optional[str]:
        """The learned language of an SRT text, or None (too short, unknown or ambiguous)."""
        if not self.used:
            return None
        text = text_of(srt)
        if sum(len(w) for w in _WORD.findall(text)) < MIN_LETTERS:
            return None
        return self._decide(self._nearest(ranked(grams(text)), self.used), self.reach)


_cache: dict[tuple, Model] = {}


def model(database_path: str) -> Optional[Model]:
    """The current model of a converter database (cached until a track is added)."""
    try:
        conn = sqlite3.connect(database_path, timeout=30)
        ensure(conn)
        version = conn.execute("SELECT COUNT(*), COALESCE(MAX(added_at), 0) FROM language_tracks").fetchone()
        key = (database_path, *version)
        if key not in _cache:
            _cache.clear()
            _cache[key] = Model(conn)
        conn.close()
        return _cache[key]
    except sqlite3.Error:
        return None
