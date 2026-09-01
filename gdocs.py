"""
gdocs.py - turn the generated markdown into a formatted Google Doc.

Ported from writeReviewAdam.py, which had this part right: the two validation passes
below caught a real class of corruption (bold/link spans landing mid-word) and are kept
verbatim in spirit. Changes made during integration:

  * No destructive overwrite. The original looked for an existing doc with the same
    title and called drive.files().delete() on it before creating the new one, which
    would silently destroy a draft someone was mid-way through commenting on. Titles are
    now versioned instead (see unique_title).
  * One fewer API round-trip. Section headers were found by re-fetching the uploaded
    document and string-matching paragraph text; their ranges are computed from the
    plain text before upload instead.
  * Retries. Every Docs/Drive call goes through _execute() with backoff, so a transient
    429 or 502 doesn't throw away a review that cost real money to generate.
"""

from __future__ import annotations

import random
import re
import time
from typing import Dict, List, Optional, Tuple

SECTION_TITLES = ["Overview", "TLDR", "General", "Payments", "Games",
                  "Responsible Gambling", "Bonuses"]

_NESTED_LINK_PATTERN = re.compile(r"\[([^\]]+?)\]\((https?://[^\)]+)\)")

_MARKUP = (
    r"(?P<bold>\*\*(?P<bold_text>.*?)\*\*)"
    r"|(?P<link>\[(?P<link_text>[^\]]+?)\]\((?P<url>https?://[^\)]+)\))"
    r"|(?P<italic>\*(?P<italic_text>[^\*\n]+?)\*)"
)


def _execute(request, what: str, attempts: int = 4):
    """Run a Google API request with backoff on transient failures."""
    from googleapiclient.errors import HttpError  # noqa: PLC0415

    last = None
    for attempt in range(attempts):
        try:
            return request.execute()
        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            if status not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
            last = e
            time.sleep(min(2 ** attempt + random.uniform(0, 0.5), 10))
    raise last  # unreachable, keeps type checkers happy


# ---------------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------------

def _reconstruct_markdown(plain_text: str, formatting_requests: List[dict]) -> str:
    """Rebuild **bold**/[text](url)/*italic* from plain text plus computed style ranges.

    Replaying the ranges must reproduce the source markdown exactly. If it doesn't, the
    ranges are wrong and would land styling on the wrong characters once uploaded.
    """
    opens: Dict[int, List[str]] = {}
    closes: Dict[int, List[str]] = {}
    for req in formatting_requests:
        style = req.get("updateTextStyle")
        if not style:
            continue
        text_style = style["textStyle"]
        start, end = style["range"]["startIndex"], style["range"]["endIndex"]
        # Opens accumulate outer-to-inner; closes must be the reverse at a shared index
        # (the inner span closes before the outer one), hence insert(0, ...).
        if text_style.get("link"):
            opens.setdefault(start, []).append("[")
            closes.setdefault(end, []).insert(0, f"]({text_style['link']['url']})")
        elif text_style.get("bold"):
            opens.setdefault(start, []).append("**")
            closes.setdefault(end, []).insert(0, "**")
        elif text_style.get("italic"):
            opens.setdefault(start, []).append("*")
            closes.setdefault(end, []).insert(0, "*")

    pieces: List[str] = []
    for i, ch in enumerate(plain_text):
        idx = 1 + i
        pieces.extend(closes.get(idx, []))
        pieces.extend(opens.get(idx, []))
        pieces.append(ch)
    end_idx = 1 + len(plain_text)
    pieces.extend(closes.get(end_idx, []))
    pieces.extend(opens.get(end_idx, []))
    return "".join(pieces)


def _first_diff(a: str, b: str, radius: int = 40) -> str:
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return f"reconstructed={a[max(0, i-radius):i+radius]!r} vs source={b[max(0, i-radius):i+radius]!r}"
    if len(a) != len(b):
        return f"lengths differ ({len(a)} vs {len(b)}); one is a prefix of the other"
    return "(identical - unexpected)"


def _mid_word_spans(plain_text: str, formatting_requests: List[dict]) -> List[str]:
    """Flag any span starting or ending inside a word.

    The round-trip check only proves this module's own arithmetic is self-consistent; it
    would pass happily on markdown that was already malformed upstream. This catches the
    one thing that is never legitimate: styling that splits a word.
    """
    def is_word(ch: Optional[str]) -> bool:
        return ch is not None and ch.isalnum()

    problems = []
    for req in formatting_requests:
        style = req.get("updateTextStyle")
        if not style:
            continue
        start, end = style["range"]["startIndex"], style["range"]["endIndex"]
        before = plain_text[start - 2] if start - 2 >= 0 else None
        at_start = plain_text[start - 1] if 0 <= start - 1 < len(plain_text) else None
        at_last = plain_text[end - 2] if 0 <= end - 2 < len(plain_text) else None
        after = plain_text[end - 1] if 0 <= end - 1 < len(plain_text) else None
        if is_word(before) and is_word(at_start):
            problems.append(f"starts mid-word: ...{plain_text[max(0, start-20):start+20]!r}...")
        if is_word(at_last) and is_word(after):
            problems.append(f"ends mid-word: ...{plain_text[max(0, end-20):end+20]!r}...")
    return problems


def _extract_nested_links(bold_text: str, base_cursor: int) -> Tuple[str, List[dict]]:
    """Pull a [text](url) link out of a bold span's inner text.

    The linking pass nests links inside bold spans (**[Casino](url)**); the outer bold
    regex captures that raw markup as part of its inner text instead of parsing it.
    Strip it to visible text and return matching link requests positioned relative to
    where the bold span starts.
    """
    clean = ""
    requests: List[dict] = []
    last_end = 0
    cursor = base_cursor
    for m in _NESTED_LINK_PATTERN.finditer(bold_text):
        start, end = m.span()
        before = bold_text[last_end:start]
        clean += before
        cursor += len(before)
        link_text, url = m.group(1), m.group(2)
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": cursor, "endIndex": cursor + len(link_text)},
                "textStyle": {"link": {"url": url}},
                "fields": "link",
            }
        })
        clean += link_text
        cursor += len(link_text)
        last_end = end
    clean += bold_text[last_end:]
    return clean, requests


# ---------------------------------------------------------------------------
# PARSE + UPLOAD
# ---------------------------------------------------------------------------

def normalize_markdown(review_text: str) -> str:
    """Tidy the few markdown forms that would otherwise upload as literal characters.

    The generator is told to emit only bold, bullets and links, and it complies - but
    the document title legitimately arrives as "# Casino review", and a stray "##"
    heading would render as literal hashes rather than a heading. Handled here so the
    parser downstream only ever sees the markup it knows about.
    """
    lines = review_text.split("\n")
    for i, line in enumerate(lines):
        if line.strip():
            # First non-empty line is the document title; it gets TITLE paragraph
            # style applied later, so the hash marker itself is noise.
            lines[i] = re.sub(r"^#\s+", "", line)
            break
    text = "\n".join(lines)
    # Any deeper heading becomes bold, matching how section headers are already written.
    text = re.sub(r"^#{2,6}\s+(.+)$", r"**\1**", text, flags=re.MULTILINE)
    return text


def parse_markdown(review_text: str) -> Tuple[str, List[dict], List[bool]]:
    """Markdown -> (plain_text, style requests, per-line bullet flags).

    Raises ValueError if either validation pass fails, before anything is sent to the
    API - a loud failure here beats publishing a corrupted document.
    """
    review_text = normalize_markdown(review_text)
    original_lines = review_text.split("\n")
    bullet_flags = [line.startswith("- ") for line in original_lines]
    stripped = "\n".join(
        line[2:] if is_bullet else line
        for line, is_bullet in zip(original_lines, bullet_flags)
    )

    plain_text = ""
    requests: List[dict] = []
    cursor = 1  # Docs bodies are 1-indexed
    last_end = 0

    for match in re.finditer(_MARKUP, stripped):
        start, end = match.span()
        before = stripped[last_end:start]
        plain_text += before
        cursor_start = cursor + len(before)

        if match.group("bold") is not None:
            styled, nested = _extract_nested_links(match.group("bold_text"), cursor_start)
            plain_text += styled
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": cursor_start, "endIndex": cursor_start + len(styled)},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })
            requests.extend(nested)
        elif match.group("link") is not None:
            styled = match.group("link_text")
            plain_text += styled
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": cursor_start, "endIndex": cursor_start + len(styled)},
                    "textStyle": {"link": {"url": match.group("url")}},
                    "fields": "link",
                }
            })
        else:
            styled = match.group("italic_text")
            plain_text += styled
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": cursor_start, "endIndex": cursor_start + len(styled)},
                    "textStyle": {"italic": True},
                    "fields": "italic",
                }
            })

        cursor += len(before) + len(styled)
        last_end = end

    plain_text += stripped[last_end:]

    reconstructed = _reconstruct_markdown(plain_text, requests)
    if reconstructed != stripped:
        raise ValueError(
            "Formatting check failed before upload: replaying the computed bold/link/"
            f"italic ranges does not reproduce the source. First divergence: "
            f"{_first_diff(reconstructed, stripped)}"
        )

    problems = _mid_word_spans(plain_text, requests)
    if problems:
        raise ValueError(
            f"Formatting check failed before upload: {len(problems)} span(s) split a "
            "word. " + " | ".join(problems[:5])
        )

    # Degenerate markdown like "****" yields a zero-length range, which round-trips
    # fine (there is no text to misplace) but the Docs API rejects outright. Drop after
    # validation, so nothing visible is lost.
    requests = [
        r for r in requests
        if not ("updateTextStyle" in r
                and r["updateTextStyle"]["range"]["startIndex"]
                == r["updateTextStyle"]["range"]["endIndex"])
    ]
    return plain_text, requests, bullet_flags


def _line_ranges(plain_text: str) -> List[Tuple[str, int, int]]:
    """(line, start_index, end_index) for each line, in Docs 1-based coordinates."""
    out = []
    cursor = 1
    for line in plain_text.split("\n"):
        out.append((line, cursor, cursor + len(line)))
        cursor += len(line) + 1  # +1 for the newline
    return out


def _structure_requests(plain_text: str, bullet_flags: List[bool]) -> Tuple[List[dict], List[dict]]:
    """Title style + section-header style, and bullet-run requests.

    Section headers are located from the plain text rather than by re-fetching the
    uploaded doc and matching paragraph strings, which removes an API round-trip and a
    dependency on the upload having landed exactly as expected.
    """
    lines = _line_ranges(plain_text)
    style: List[dict] = []

    if lines:
        _, start, end = lines[0]
        style.append({
            "updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": {"namedStyleType": "TITLE"},
                "fields": "namedStyleType",
            }
        })

    for line, start, end in lines[1:]:
        if line.strip() in SECTION_TITLES and end > start:
            style.append({
                "updateTextStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "textStyle": {"bold": True, "fontSize": {"magnitude": 16, "unit": "PT"}},
                    "fields": "bold,fontSize",
                }
            })

    bullets: List[dict] = []
    run_start = run_end = None
    for i, (line, start, end) in enumerate(lines):
        if i < len(bullet_flags) and bullet_flags[i]:
            if run_start is None:
                run_start = start
            run_end = end
        elif run_start is not None:
            bullets.append({
                "createParagraphBullets": {
                    "range": {"startIndex": run_start, "endIndex": run_end},
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }
            })
            run_start = None
    if run_start is not None:
        bullets.append({
            "createParagraphBullets": {
                "range": {"startIndex": run_start, "endIndex": run_end},
                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
            }
        })
    return style, bullets


def unique_title(drive, folder_id: str, base_title: str) -> str:
    """A title not already used in the folder, suffixing " (v2)", " (v3)", ...

    Replaces the original find-and-delete behaviour. Regenerating a casino now leaves
    every earlier draft in place, which matters because reviewers comment directly in
    these documents - deleting one silently destroys their work.
    """
    safe = base_title.replace("'", " ")
    existing = set()
    try:
        resp = _execute(
            drive.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="files(name)", pageSize=1000,
            ),
            "list folder",
        )
        existing = {f["name"] for f in resp.get("files", [])}
    except Exception as e:  # noqa: BLE001 - a naming nicety must never block publishing
        print(f"Could not list folder to version the title ({e}); using base title.")
        return base_title

    if base_title not in existing:
        return base_title
    for n in range(2, 100):
        candidate = f"{base_title} (v{n})"
        if candidate not in existing:
            return candidate
    return f"{safe} ({int(time.time())})"


def upload_review(docs, drive, folder_id: str, title: str, review_text: str) -> Tuple[str, str]:
    """Create a formatted Google Doc in the folder. Returns (doc_id, doc_url).

    Parsing and validation happen first, so a formatting problem fails before an empty
    document has been created and left lying in the folder.
    """
    plain_text, style_requests, bullet_flags = parse_markdown(review_text)
    para_requests, bullet_requests = _structure_requests(plain_text, bullet_flags)

    doc_id = _execute(docs.documents().create(body={"title": title}), "create doc")["documentId"]

    _execute(
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": plain_text}}]},
        ),
        "insert text",
    )

    inline = para_requests + style_requests
    if inline:
        _execute(
            docs.documents().batchUpdate(documentId=doc_id, body={"requests": inline}),
            "apply inline formatting",
        )
    if bullet_requests:
        _execute(
            docs.documents().batchUpdate(documentId=doc_id, body={"requests": bullet_requests}),
            "apply bullets",
        )

    file = _execute(drive.files().get(fileId=doc_id, fields="parents"), "get parents")
    previous = ",".join(file.get("parents", []))
    _execute(
        drive.files().update(
            fileId=doc_id, addParents=folder_id, removeParents=previous, fields="id,parents"
        ),
        "move to folder",
    )
    return doc_id, f"https://docs.google.com/document/d/{doc_id}/edit"
