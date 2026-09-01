#!/usr/bin/env python3
"""
voice_metrics.py - measure how close generated reviews sit to Adam's real voice.

Voice fidelity is otherwise a judgement call that drifts. This turns it into numbers
checked against targets derived from Adam's own published writing (examples.txt), so
a regression shows up as a failing row instead of a vague feeling that something is off.

Every target below is measured, not chosen. Tolerances are deliberately wide: the goal
is catching a register collapse (0.8 contractions per 1k words when the target is ~19),
not policing a writer into a numeric straitjacket.

Usage:
    .venv/bin/python voice_metrics.py                      # targets vs reviews_agent/
    .venv/bin/python voice_metrics.py reviews_agent reviews # compare two dirs
    .venv/bin/python voice_metrics.py --targets             # just print the targets
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List

# Targets come from Adam's real CASINO REVIEWS when available, falling back to his
# guide pages. The distinction matters more than it looks: the two registers measurably
# diverge (reviews 0.5 reader-questions per 1k words vs guide pages 7.3; reviews median
# 17 words per sentence with 4% under 8, guide pages 15 with 18% under 8). Measuring
# review output against guide-page targets produced a phantom "questions gap" and
# flagged correct sentence lengths as drift.
SAMPLES = "examples_reviews.txt"
SAMPLES_FALLBACK = "examples.txt"

# Bands are derived from the review corpus, widened to catch a register collapse rather
# than police a writer. Anything with no band is informational only.
BANDS = {
    "contractions": (10.0, 40.0),
    "I": (5.0, 16.0),
    "exclamations": (0.0, 10.0),
    "reader-opening paras %": (5.0, 25.0),
    "median sentence len": (13, 22),
    "sents >25 words %": (8, 28),
    "sents <8 words %": (0, 12),
    # Hard zero: house style bans dash punctuation outright. Adam's own older writing
    # uses it (~1.8/1k), so the ADAM column will read above this band by design - the
    # ban is a later house-style decision that overrides the historical samples.
    "em/en dashes": (0.0, 0.0),
}


def measure(text: str) -> Dict[str, float]:
    body = re.sub(r"[*#]", "", text)
    words = body.split()
    n = max(len(words), 1)
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if len(s.split()) > 2]
    lens = sorted(len(s.split()) for s in sents) or [0]
    paras = [p for p in body.split("\n") if len(p.split()) > 3]
    reader_open = sum(
        1 for p in paras
        if re.match(r"^(so,?\s+)?(you|your|craving|want|looking|ready|need|nothing)\b",
                    p.strip(), re.I)
    )

    def per1k(count: int) -> float:
        return round(count / n * 1000, 1)

    return {
        "words": len(words),
        "contractions": per1k(len(re.findall(r"\b\w+['’](t|s|re|ve|ll|d|m)\b", body))),
        "questions": per1k(body.count("?")),
        "exclamations": per1k(body.count("!")),
        "I": per1k(len(re.findall(r"\bI\b", body))),
        # Prose dashes only. A dash inside a numeric range ("15-30 minutes", "2-3
        # minutes") is ordinary typography, not the em-dash-as-punctuation habit the
        # house ban targets, so counting those produced false violations.
        "em/en dashes": per1k(len(re.findall(r"(?<!\d)[–—](?!\d)", body))),
        "median sentence len": lens[len(lens) // 2],
        "sents >25 words %": round(sum(1 for x in lens if x > 25) / len(lens) * 100),
        "sents <8 words %": round(sum(1 for x in lens if x < 8) / len(lens) * 100),
        "reader-opening paras %": round(reader_open / max(len(paras), 1) * 100),
    }


def read_dir(d: Path) -> str:
    files = sorted(d.glob("*.md"))
    if not files:
        sys.exit(f"No .md files in {d}/")
    return " ".join(f.read_text(encoding="utf-8", errors="replace") for f in files)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    show_targets_only = "--targets" in sys.argv

    sample_path = Path(SAMPLES)
    if not sample_path.exists():
        sample_path = Path(SAMPLES_FALLBACK)
        if not sample_path.exists():
            sys.exit(f"Neither {SAMPLES} nor {SAMPLES_FALLBACK} found - "
                     "one of them defines the targets.")
        print(f"NOTE: {SAMPLES} missing, falling back to {SAMPLES_FALLBACK} "
              "(guide-page register - questions and sentence-length targets will "
              "not reflect review register).\n", file=sys.stderr)
    target = measure(sample_path.read_text(encoding="utf-8", errors="replace"))

    if show_targets_only:
        print(f"Targets from {SAMPLES} ({target['words']:,} words):")
        for k, v in target.items():
            if k != "words":
                band = BANDS.get(k)
                extra = f"   pass band {band[0]}-{band[1]}" if band else ""
                print(f"  {k:24} {v}{extra}")
        return

    dirs = [Path(a) for a in args] or [Path("reviews_agent")]
    measured = {d.name: measure(read_dir(d)) for d in dirs}

    keys = [k for k in target if k != "words"]
    width = max(len(k) for k in keys) + 2
    head = f"{'metric':<{width}} {'ADAM':>8}"
    for name in measured:
        head += f" {name[:16]:>17}"
    print(head)
    print("-" * len(head))

    failures: List[str] = []
    for k in keys:
        row = f"{k:<{width}} {target[k]:>8}"
        for name, m in measured.items():
            band = BANDS.get(k)
            mark = ""
            if band:
                lo, hi = band
                if m[k] < lo:
                    mark, note = " LOW", f"{name}: {k} = {m[k]} (want >= {lo}, Adam {target[k]})"
                elif m[k] > hi:
                    mark, note = " HIGH", f"{name}: {k} = {m[k]} (want <= {hi}, Adam {target[k]})"
                if mark:
                    failures.append(note)
            row += f" {str(m[k]) + mark:>17}"
        print(row)

    print()
    for name, m in measured.items():
        print(f"{name}: {m['words']:,} words")

    if failures:
        print("\nOUT OF BAND:")
        for f in failures:
            print(f"  - {f}")
        print("\n(Bands are wide on purpose - these flag a register collapse, "
              "not a style preference. Judge the prose too.)")
    else:
        print("\nAll tracked metrics inside the Adam bands.")


if __name__ == "__main__":
    main()
