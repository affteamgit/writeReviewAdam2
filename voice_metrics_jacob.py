#!/usr/bin/env python3
"""
voice_metrics.py - measure how close generated Jakob/BCK reviews sit to real BCK voice.

Same tool as Adam2's voice_metrics.py, same reasoning: voice fidelity is otherwise a
judgement call that drifts, so this turns it into numbers checked against targets
derived from real BCK-published writing (examples_jakob.txt), so a regression shows up
as a failing row instead of a vague feeling that something is off.

Every target below is measured, not chosen. Tolerances are deliberately wide: the goal
is catching a register collapse, not policing a writer into a numeric straitjacket.

Usage:
    .venv/bin/python voice_metrics.py                        # targets vs reviews_jakob/
    .venv/bin/python voice_metrics.py reviews_jakob other_dir # compare two dirs
    .venv/bin/python voice_metrics.py --targets               # just print the targets
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List

# Targets come from 5 real individual BCK review pages (bitstarz, 7bit-casino, bcgame,
# roobet, stake-com), fetched 2026-09-16 - review-register only, reader-comment threads
# stripped out (they were getting scraped in as if they were Jakob's own writing, which
# would have corrupted every metric below). One of the 5 (stake-com) is a clear outlier -
# exclamation-heavy, longer sentences, far more dash punctuation than the other 4, which
# share near-identical template phrasing and a calmer register. It stays in the combined
# corpus for size, but the bands below are widened partly to accommodate it rather than
# treating it as the target to hit.
#
# SAMPLES_FALLBACK is guide/listicle-register content (knowledge-base, game-providers,
# bitcoin-betting pages, plus one pasted "why play at X" article) - NOT review register.
# On Adam's project these two registers measurably diverged (reviews ran ~0.5
# reader-questions per 1k words vs guide pages' 7.3). Falling back to it here means the
# targets below stop reflecting what Jakob's reviews actually read like - treat any run
# using the fallback as informational only, not a real pass/fail signal.
SAMPLES = "examples_jakob.txt"
SAMPLES_FALLBACK = "examples_jakob_guide.txt"

# Bands are derived from the 5-review corpus (see per-review spread in the comment above
# each band), widened to catch a register collapse rather than police a writer. Anything
# with no band is informational only.
BANDS = {
    "contractions": (15.0, 45.0),        # per-review range 18.8-41.2
    "I": (5.0, 20.0),                    # per-review range 7.3-15.7
    "exclamations": (0.0, 6.0),          # per-review range 0.0-3.5 (stake-com the high end)
    "reader-opening paras %": (0.0, 12.0),
    "median sentence len": (9, 20),      # per-review range 10-17
    "sents >25 words %": (0, 20),        # per-review range 0-17 (stake-com the high end)
    "sents <8 words %": (5, 32),         # per-review range 9-28
    # Hard zero: house style (BaseGuidelinesClaude.txt, ported into writeReviewJacobAgent
    # .py's VOICE constant) bans em/en dash punctuation outright. The stake-com sample
    # reads well above this band (2.4/1k) - that's a pre-ban piece, not a target.
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
        # Prose dashes only. A dash inside a numeric range ("24-48 hours") is ordinary
        # typography, not the em-dash-as-punctuation habit the house ban targets.
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

    dirs = [Path(a) for a in args] or [Path("reviews_jakob")]
    measured = {d.name: measure(read_dir(d)) for d in dirs}

    keys = [k for k in target if k != "words"]
    width = max(len(k) for k in keys) + 2
    head = f"{'metric':<{width}} {'JAKOB':>8}"
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
                    mark, note = " LOW", f"{name}: {k} = {m[k]} (want >= {lo}, Jakob {target[k]})"
                elif m[k] > hi:
                    mark, note = " HIGH", f"{name}: {k} = {m[k]} (want <= {hi}, Jakob {target[k]})"
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
        print("\nAll tracked metrics inside the Jakob bands.")


if __name__ == "__main__":
    main()
