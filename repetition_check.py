"""
repetition_check.py - a separate pass after a review is written: find sentences that
reuse wording from earlier published reviews, and rewrite only those.

Why a separate pass instead of asking the writer to avoid repetition: measured on the
BCK pipeline (2026-09-29 / 2026-10-06), showing the writer its past reviews did not stop
verbatim reuse - phrases from one batch were copied into the next, and the next. Finding
reuse is a job code does exactly and cheaply (shared word runs against the last N
reviews); judging how to say the same thing differently is the model's job. So code
finds, the model rewrites only what code found, and code applies the rewrites one
sentence at a time - nothing else in the review can change.

Casino names are masked before comparing, so "Roobet blocks 75 countries and..." and
"Thrill blocks 75 countries and..." count as the same sentence - which is exactly the
template reuse a reader notices.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple

RUN = 6  # a shared run of this many words (after masking) flags a sentence

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'\[*(A-Z0-9$])")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _is_prose(line: str) -> bool:
    """Answer text only: not a header, section title, the doc title, or a data flag."""
    s = line.strip()
    if not s or s.startswith("#") or s.startswith("DATA FLAG:"):
        return False
    if re.fullmatch(r"\*\*[^*]+\*\*", s):  # **Section** title
        return False
    if s.endswith("?") and len(s.split()) <= 15:  # question header in a plain export
        return False
    return True


def sentences(text: str) -> List[str]:
    """Exact substrings of `text`, one per sentence, from prose lines only."""
    out = []
    for line in text.splitlines():
        if not _is_prose(line):
            continue
        body = line.strip()
        if body.startswith("- "):
            body = body[2:]
        out += [p.strip() for p in _SENTENCE_SPLIT.split(body) if p.strip()]
    return out


class _Masker:
    def __init__(self, casino_names: List[str]):
        names = sorted({n.strip().lower() for n in casino_names if n and n.strip()},
                       key=len, reverse=True)
        self._pattern = (re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(n) for n in names)
                                    + r")(?![a-z0-9])") if names else None)

    def words(self, sentence: str) -> List[str]:
        s = _LINK.sub(r"\1", sentence).replace("**", "")
        s = s.replace("’", "'").replace("—", " ").replace("–", " ").lower()
        if self._pattern:
            s = self._pattern.sub(" casinoname ", s)
        s = s.replace("-", " ")
        toks = re.findall(r"[a-z0-9$%][a-z0-9$%,.'/]*", s)
        return [t.rstrip(".,'") for t in toks if t.rstrip(".,'")]


def _runs(words: List[str]) -> set:
    return {" ".join(words[i:i + RUN]) for i in range(len(words) - RUN + 1)}


def _longest_shared(a: List[str], b: List[str]) -> int:
    best = 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def find_overlaps(review: str, prior: List[Tuple[str, str]],
                  casino_names: List[str]) -> List[Dict]:
    """Every sentence of `review` sharing a RUN-word stretch with any prior review.

    Returns [{"sentence", "title", "prior_sentence", "shared_words"}], one entry per
    flagged sentence, matched to the prior sentence it shares the most words with.
    Sentences repeated *within* the review are also flagged against each other.
    """
    m = _Masker(casino_names)
    index: Dict[str, List[Tuple[str, str, List[str]]]] = {}
    for title, text in prior:
        for s in sentences(text):
            w = m.words(s)
            for r in _runs(w):
                index.setdefault(r, []).append((title, s, w))

    flagged = []
    for s in sentences(review):
        w = m.words(s)
        candidates = {}
        for r in _runs(w):
            for title, ps, pw in index.get(r, []):
                candidates[(title, ps)] = pw
        if candidates:
            (title, ps), pw = max(candidates.items(),
                                  key=lambda kv: _longest_shared(w, kv[1]))
            flagged.append({"sentence": s, "title": title, "prior_sentence": ps,
                            "shared_words": _longest_shared(w, pw)})
        # Indexed after checking, so a sentence is only compared with EARLIER ones in
        # this review - of a repeated pair, only the second gets rewritten.
        for r in _runs(w):
            index.setdefault(r, []).append(("this review", s, w))
    return flagged


# ----------------------------------------------------------------------------
# REWRITE - a separate model call that only sees the flagged sentences
# ----------------------------------------------------------------------------

REWRITE_SYSTEM = (
    "You are the editor of a casino review site. A reviewer's new draft reuses wording "
    "from reviews the site has already published, and readers who read several reviews "
    "notice it. You rewrite individual sentences so they say the same thing in the "
    "reviewer's own fresh words."
)

REWRITE_TASK = """\
Below is a new review, followed by the sentences in it that reuse wording from earlier
reviews. Each one shows the earlier sentence it overlaps with and which review that was.

For each flagged sentence, write a replacement that:
- keeps every fact, number, name, bonus code and verdict exactly as they are
- reads naturally in its paragraph - check the sentences before and after it
- keeps the same first-person voice and any **bold** or [Name](placeholder) markup
- does not borrow wording from the earlier sentence shown, or from the other
  flagged sentences

A sentence can stay one sentence or become two, but don't add new information or
drop any. Return one rewrite per flagged sentence, by its number."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "rewrites": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rewrites"],
    "additionalProperties": False,
}


def _rewrite_prompt(review: str, flagged: List[Dict]) -> str:
    items = []
    for n, f in enumerate(flagged, 1):
        where = ("earlier in this same review" if f["title"] == "this review"
                 else f'in "{f["title"]}"')
        items.append(f'{n}. Your sentence: {f["sentence"]}\n'
                     f'   Overlaps with, {where}: {f["prior_sentence"]}')
    return (f"{REWRITE_TASK}\n\n--- NEW REVIEW ---\n{review}\n\n"
            f"--- FLAGGED SENTENCES ---\n" + "\n\n".join(items))


def rewrite(review: str, flagged: List[Dict], model: str, effort: str = "medium",
            progress=None, drain=None) -> Tuple[str, List[Tuple[str, str]], object]:
    """Apply one model-written replacement per flagged sentence.

    Returns (new_review, [(old, new) applied], usage). A replacement is applied only when
    its original sentence occurs exactly once in the review, so nothing else can move.
    """
    import anthropic

    import config

    client = anthropic.Anthropic(api_key=config.anthropic_api_key(),
                                 timeout=1800.0, max_retries=1)
    with client.messages.stream(
        model=model,
        max_tokens=32000,
        system=REWRITE_SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": effort,
                       "format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": _rewrite_prompt(review, flagged)}],
    ) as stream:
        message = drain(stream, "repetition", progress) if drain else stream.get_final_message()

    if message.stop_reason in ("refusal", "max_tokens"):
        return review, [], message.usage
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        rewrites = json.loads(text)["rewrites"]
    except (ValueError, KeyError, TypeError):
        return review, [], message.usage

    applied = []
    for r in rewrites:
        idx = r.get("id", 0) - 1
        new = (r.get("text") or "").strip()
        if not 0 <= idx < len(flagged) or not new:
            continue
        old = flagged[idx]["sentence"]
        if review.count(old) == 1 and new != old:
            review = review.replace(old, new, 1)
            applied.append((old, new))
    return review, applied, message.usage


def check_and_fix(review: str, prior: List[Tuple[str, str]], casino_names: List[str],
                  model: str, effort: str = "medium", progress=None,
                  drain=None) -> Dict[str, object]:
    """Find -> rewrite -> find again. The 'after' count is measured, not self-reported."""
    before = find_overlaps(review, prior, casino_names)
    usage = None
    applied: List[Tuple[str, str]] = []
    if before:
        review, applied, usage = rewrite(review, before, model, effort, progress, drain)
    after = find_overlaps(review, prior, casino_names) if before else []
    return {"review": review, "found": before, "applied": applied,
            "remaining": after, "usage": usage, "compared_against": len(prior)}
