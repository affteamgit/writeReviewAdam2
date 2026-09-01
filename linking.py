"""
linking.py - wrap mentions of other Gamblineers-reviewed casinos in links to their reviews.

Ported from writeReviewAdam.py with one real bug fixed (see link_casino_mentions).
The generator itself never writes links: it names casinos in plain text and this pass
adds the only real URLs, taken live from the published sitemap, so a fabricated link is
structurally impossible.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import requests

import config

MIN_CASINO_SLUG_LENGTH = 3  # skip pathologically short slugs to avoid false positives


def get_casino_review_map(timeout: int = 10) -> Dict[str, str]:
    """Fetch the live post sitemap and map review slug -> full URL.

    Casino reviews publish at /<slug>-casino-review/ or /<slug>-review/; both patterns
    exist in production. Fetched fresh each run so a newly published review becomes
    linkable with no list to maintain. Fails open (returns {}) so a network blip never
    blocks a generation - callers must treat empty as "skip linking this run".
    """
    try:
        response = requests.get(config.get("SITEMAP_URL"), timeout=timeout)
        response.raise_for_status()
        urls = re.findall(r"<loc>(https://gamblineers\.com/[^<]+)</loc>", response.text)

        review_map: Dict[str, str] = {}
        for url in urls:
            path = url.rstrip("/").rsplit("/", 1)[-1]
            if path.endswith("-casino-review"):
                slug = path[: -len("-casino-review")]
            elif path.endswith("-review"):
                slug = path[: -len("-review")]
            else:
                continue
            if slug:
                review_map[slug] = url
        return review_map
    except Exception as e:  # noqa: BLE001 - fail open by design
        print(f"Sitemap fetch failed, skipping internal linking: {e}")
        return {}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _same_casino(slug_key: str, reviewed_key: str) -> bool:
    """Whether a sitemap slug refers to the casino currently under review.

    Plain equality was the bug. A brand written "Jack.com" normalises to "jackcom"
    while its own sitemap slug "jack-casino-review" normalises to "jack", so the two
    never matched and the linker treated "Jack" as a *different* casino inside
    Jack.com's own review - self-linking it, including in the document title. Compare
    with a trailing "com" tolerated on either side.
    """
    if slug_key == reviewed_key:
        return True
    for a, b in ((slug_key, reviewed_key), (reviewed_key, slug_key)):
        if a.endswith("com") and len(a) > 3 and a[:-3] == b:
            return True
    return False


def _link_patterns(text: str, pattern_url_pairs) -> str:
    """Wrap each safe match with a markdown link, preserving the matched text exactly.

    Skips any match overlapping an existing link (no nested links), recomputing the
    protected ranges after each pattern since earlier patterns insert new links.

    A match falling entirely inside a **bold** span's inner text is allowed - only the
    four "**" delimiter characters are protected - so it comes out nested as
    **[text](url)**. The writer bolds competitor names often enough that skipping those
    meant most comparisons never got linked at all.
    """
    linked_text = text
    for pattern, url in pattern_url_pairs:
        protected = [False] * len(linked_text)
        for span in re.finditer(r"\*\*.*?\*\*", linked_text):
            s, e = span.span()
            for i in range(s, s + 2):
                protected[i] = True
            for i in range(e - 2, e):
                protected[i] = True
        for span in re.finditer(r"\[[^\]]*\]\(https?://[^\)]+\)", linked_text):
            for i in range(*span.span()):
                protected[i] = True

        pieces: List[str] = []
        last_end = 0
        for match in pattern.finditer(linked_text):
            start, end = match.span()
            if any(protected[start:end]):
                continue
            pieces.append(linked_text[last_end:start])
            pieces.append(f"[{match.group(0)}]({url})")
            last_end = end
        pieces.append(linked_text[last_end:])
        linked_text = "".join(pieces)
    return linked_text


def link_casino_mentions(
    review_text: str,
    reviewed_casino_name: str,
    casino_review_map: Optional[Dict[str, str]] = None,
) -> Tuple[str, int]:
    """Link mentions of other reviewed casinos. Returns (text, links_added).

    Matching works from each casino's URL slug and tolerates how the writer punctuates
    the name, so "BC.Game", "BC Game" and "bc-game" all match, while the writer's own
    casing is preserved in the visible link text.

    Two fixes over the original:
      * The casino under review is excluded even when its brand name carries a ".com"
        suffix its slug does not (see _same_casino).
      * A slug pattern optionally absorbs a trailing ".com", so "Jack.com" links as one
        unit instead of producing "[Jack](url).com" - which was happening in every
        review that mentioned it, not just its own.
    """
    if casino_review_map is None:
        casino_review_map = get_casino_review_map()
    if not casino_review_map:
        return review_text, 0

    reviewed_key = _norm(reviewed_casino_name)

    # Longest slug first so a shorter slug can't claim part of a longer, more specific
    # match ("jackpoker" must be tried before "jack").
    candidates = sorted(casino_review_map.items(), key=lambda kv: -len(kv[0]))

    pairs = []
    for slug, url in candidates:
        key = _norm(slug)
        if len(key) < MIN_CASINO_SLUG_LENGTH or _same_casino(key, reviewed_key):
            continue
        chunks = [c for c in slug.split("-") if c]
        if not chunks:
            continue
        pattern = re.compile(
            r"\b"
            + r"[\s\.\-]*".join(re.escape(c) for c in chunks)
            + r"(?:\.com)?\b",
            re.IGNORECASE,
        )
        pairs.append((pattern, url))

    linked = _link_patterns(review_text, pairs)
    added = linked.count("](https://") - review_text.count("](https://")
    return linked, added
