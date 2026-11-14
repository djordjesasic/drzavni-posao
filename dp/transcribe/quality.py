"""Detect Whisper failure modes (repetition loops, swallowed speech) and clean segments.

In the pilot, ~9% of audio was lost to loops: after intro music Whisper repeats one
invented line ("Top 10 najboljih ...", "Hvala vam na pozornosti") over real dialogue.
Re-transcribing the bad part in short pieces fixes it; see steps.do_transcribe.
"""

import re
from collections import Counter
from typing import Any

# Same text this many times in a row = loop. Real dialogue repeats 3x ("Ustanite."
# x3, "Jovo." x3 in the pilot); real loops ran 4-36x or are caught by the checks below.
LOOP_RUN = 4
LOOP_COUNT = 3  # same longer line this many times anywhere in a piece (A B A B loops)
LOOP_MIN_LEN = 20  # ... counted only for lines this long; "Dobro." may repeat for real
MIN_CHARS_PER_S = 3.0  # normal dialogue is ~10-15; below this a long piece swallowed speech
MIN_CHECK_S = 60.0  # pieces shorter than this are not judged by chars/s (may be just music)
# One line stretched over this long a stretch of audio hides swallowed speech
# (real Whisper segments are a few seconds; seen: "Hvala vam." over 30 s).
STRETCHED_S = 20.0
STRETCHED_CHARS_PER_S = 2.0

# Lines Whisper invents from its training data (YouTube/TED subtitles), not from the audio.
_HALLUCINATIONS = re.compile(
    r"(?i)^top \d+ najboljih"
    r"|hvala (vam )?na (pozornosti|gledanju)"
    r"|reviewer:"
    r"|amara\.org"
    r"|^(prevod|titlov[ia]|subtitles?)\b"
)
_LETTER = re.compile(r"[^\W\d_]")


def max_run(texts: list[str]) -> int:
    """Longest run of identical consecutive texts."""
    best = run = 0
    prev: str | None = None
    for t in texts:
        run = run + 1 if t == prev else 1
        best = max(best, run)
        prev = t
    return best


def is_stretched(seg: dict[str, Any]) -> bool:
    dur = seg["end"] - seg["start"]
    return dur >= STRETCHED_S and len(seg["text"]) / dur < STRETCHED_CHARS_PER_S


def is_suspect(segments: list[dict[str, Any]], span_s: float) -> bool:
    """True if a transcribed piece looks like a loop or swallowed speech."""
    texts = [s["text"] for s in segments]
    if max_run(texts) >= LOOP_RUN:
        return True
    long_lines = Counter(t for t in texts if len(t) >= LOOP_MIN_LEN)
    if long_lines and max(long_lines.values()) >= LOOP_COUNT:
        return True
    if any(is_stretched(s) for s in segments):
        return True
    chars = sum(len(t) for t in texts if _LETTER.search(t))
    return span_s >= MIN_CHECK_S and chars / span_s < MIN_CHARS_PER_S


def is_noise(text: str) -> bool:
    return not _LETTER.search(text) or bool(_HALLUCINATIONS.search(text))


def clean(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop music/punctuation-only, known invented and stretched lines; collapse
    consecutive duplicates into one segment spanning all of them."""
    out: list[dict[str, Any]] = []
    for s in segments:
        if is_noise(s["text"]) or is_stretched(s):
            continue
        if out and out[-1]["text"] == s["text"]:
            out[-1] = {**out[-1], "end": s["end"]}
            continue
        out.append(dict(s))
    return out
