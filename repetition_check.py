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
                  casino_names: List[str], max_matches: int = 3) -> List[Dict]:
    """Every sentence of `review` sharing a RUN-word stretch with any prior review.

    Returns [{"sentence", "title", "prior_sentence", "shared_words", "matches"}], one
    entry per flagged sentence. "matches" is up to `max_matches` distinct earlier
    sentences, longest shared run first; title/prior_sentence/shared_words are the top
    one. Sentences repeated *within* the review are flagged against each other too.
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
            ranked = sorted(((_longest_shared(w, pw), title, ps)
                             for (title, ps), pw in candidates.items()), reverse=True)
            matches = [{"title": t, "sentence": ps, "shared_words": n}
                       for n, t, ps in ranked[:max_matches]]
            flagged.append({"sentence": s, "title": matches[0]["title"],
                            "prior_sentence": matches[0]["sentence"],
                            "shared_words": matches[0]["shared_words"],
                            "matches": matches})
        # Indexed after checking, so a sentence is only compared with EARLIER ones in
        # this review - of a repeated pair, only the second gets rewritten.
        for r in _runs(w):
            index.setdefault(r, []).append(("this review", s, w))
    return flagged


# ----------------------------------------------------------------------------
# REWRITE - a separate model call that only sees the flagged sentences
# ----------------------------------------------------------------------------

REWRITE_ROLE = (
    "You are the reviewer below, editing your own new draft. Some of its sentences "
    "reuse wording from reviews the site has already published, and readers who read "
    "several reviews notice it. You rewrite those sentences the way you'd write them "
    "if you were saying it for the first time - in your own voice, not an editor's."
)

REWRITE_TASK = """\
Below is your new review, followed by the sentences in it that reuse wording from
earlier reviews. Each one lists the earlier sentences it overlaps with, so you can see
every way it's already been said - including earlier rewrites of the same point.

For each flagged sentence, write a replacement that:
- says exactly the same thing: every fact, number, name, bonus code and verdict
  unchanged, nothing added, nothing dropped
- claims no more experience than the original does - if the original doesn't say
  you tested or saw something yourself, the rewrite doesn't either
- is no longer than the original, and shorter is fine - your voice is direct and
  plain, so a rewrite that reaches for a fancier word or a longer clause to sound
  different is worse than one that finds a simpler angle
- reads naturally with the sentences around it
- keeps any **bold** or [Name](placeholder) markup
- shares no wording with the earlier sentences listed for it

Return one rewrite per flagged sentence, by its number."""

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

# Enforced in code, not left to the prompt: the first live batch (2026-10-06) grew
# ~20% longer through rewrites that padded sentences to make them "different".
MAX_GROWTH = 1.15


def _rewrite_prompt(review: str, flagged: List[Dict]) -> str:
    items = []
    for n, f in enumerate(flagged, 1):
        lines = [f"{n}. Your sentence: {f['sentence']}", "   Already said as:"]
        for mt in f.get("matches") or [{"title": f["title"], "sentence": f["prior_sentence"]}]:
            where = "earlier in this review" if mt["title"] == "this review" else mt["title"]
            lines.append(f"   - ({where}) {mt['sentence']}")
        items.append("\n".join(lines))
    return (f"{REWRITE_TASK}\n\n--- YOUR NEW REVIEW ---\n{review}\n\n"
            f"--- FLAGGED SENTENCES ---\n" + "\n\n".join(items))


def rewrite(review: str, flagged: List[Dict], model: str, voice: str = "",
            effort: str = "medium", progress=None,
            drain=None) -> Tuple[str, List[Tuple[str, str]], object]:
    """Apply one model-written replacement per flagged sentence.

    Returns (new_review, [(old, new) applied], usage). A replacement is applied only when
    its original sentence occurs exactly once in the review (so nothing else can move)
    and it isn't more than MAX_GROWTH times the original's length.
    """
    import anthropic

    import config

    system = REWRITE_ROLE + ("\n\n" + voice if voice else "")
    client = anthropic.Anthropic(api_key=config.anthropic_api_key(),
                                 timeout=1800.0, max_retries=1)
    with client.messages.stream(
        model=model,
        max_tokens=32000,
        system=system,
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
        if new == old or review.count(old) != 1 or len(new) > MAX_GROWTH * len(old) + 10:
            continue
        review = review.replace(old, new, 1)
        applied.append((old, new))
    return review, applied, message.usage


def _add_usage(a, b):
    import types
    if a is None:
        return b
    if b is None:
        return a
    fields = ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens")
    return types.SimpleNamespace(**{f: (getattr(a, f, 0) or 0) + (getattr(b, f, 0) or 0)
                                    for f in fields})


def check_and_fix(review: str, prior: List[Tuple[str, str]], casino_names: List[str],
                  model: str, voice: str = "", effort: str = "medium", rounds: int = 2,
                  progress=None, drain=None) -> Dict[str, object]:
    """Find -> rewrite -> find again, up to `rounds` times (later rounds only touch
    what's still overlapping). The 'remaining' count is measured, not self-reported."""
    found = find_overlaps(review, prior, casino_names)
    flagged, usage = found, None
    applied: List[Tuple[str, str]] = []
    for _ in range(rounds):
        if not flagged:
            break
        review, done, u = rewrite(review, flagged, model, voice, effort, progress, drain)
        usage = _add_usage(usage, u)
        for old, new in done:
            # A second-round rewrite of a first-round rewrite: show original -> final.
            for i, (o, n) in enumerate(applied):
                if n == old:
                    applied[i] = (o, new)
                    break
            else:
                applied.append((old, new))
        flagged = find_overlaps(review, prior, casino_names) if done else flagged
        if not done:
            break
    return {"review": review, "found": found, "applied": applied,
            "remaining": flagged, "usage": usage, "compared_against": len(prior)}
