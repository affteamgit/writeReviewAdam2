#!/usr/bin/env python3
"""
seed_drive.py - publish locally generated reviews into the Drive folder as real
Google Docs, so the rolling anti-repetition window isn't empty on the app's first run.

Why this exists rather than dragging .md files into Drive: history.py only looks at
files whose mimeType is application/vnd.google-apps.document and reads them with
drive.files().export(), which only works on native Google Workspace files. An uploaded
.md stays text/markdown, so it would be filtered out of the window entirely - and the
window failing is silent, it just means reviews start repeating each other again.

Going through gdocs.upload_review() also exercises the real upload path (markup
validation, nested bold+link spans, bullet runs, section headers, folder move) against
the live Docs API before a paid generation depends on it.

Docs are created oldest-first so createdTime ordering in Drive matches the order the
reviews were actually written - the window reads newest-first and would otherwise treat
the seeding order as recency.

Usage:
    .venv/bin/python seed_drive.py --dry-run          # show what would be created
    .venv/bin/python seed_drive.py                    # seed the 5 newest, one per casino
    .venv/bin/python seed_drive.py --all              # seed every .md in the folder
    .venv/bin/python seed_drive.py --no-link          # skip the internal-linking pass
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List

import config
import gdocs
import linking


def casino_from_filename(path: Path) -> str:
    """'BitStarz Review (agent 20260901-1616).md' -> 'BitStarz'."""
    return re.split(r"\s+Review", path.stem)[0].strip()


def pick_files(source: Path, take_all: bool, limit: int) -> List[Path]:
    """Newest-first, one per casino unless --all.

    One per casino by default because seeding three versions of the same BitStarz review
    would fill most of the window with near-duplicate text, which is worse context than
    five different casinos.
    """
    files = sorted(source.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    if take_all:
        return files
    seen = set()
    picked = []
    for f in files:
        casino = casino_from_filename(f).lower()
        if casino in seen:
            continue
        seen.add(casino)
        picked.append(f)
        if len(picked) >= limit:
            break
    return picked


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed the Drive review folder from local .md files.")
    ap.add_argument("--source", default="reviews_agent", help="Folder of .md reviews")
    ap.add_argument("--limit", type=int, default=5, help="How many to seed (default 5)")
    ap.add_argument("--all", action="store_true", help="Seed every .md, including older versions")
    ap.add_argument("--no-link", action="store_true", help="Skip the internal-linking pass")
    ap.add_argument("--dry-run", action="store_true", help="Show the plan, create nothing")
    args = ap.parse_args()

    folder_id = config.get("FOLDER_ID")
    if not folder_id:
        sys.exit("FOLDER_ID is not set. Export it, or add it to Streamlit secrets.")

    source = Path(args.source)
    if not source.is_dir():
        sys.exit(f"No such folder: {source}")

    files = pick_files(source, args.all, args.limit)
    if not files:
        sys.exit(f"No .md files found in {source}/")

    # Oldest first, so Drive's createdTime order matches writing order.
    files = list(reversed(files))

    print(f"Target Drive folder: {folder_id}")
    print(f"Seeding {len(files)} review(s), oldest first:\n")
    for f in files:
        print(f"  {casino_from_filename(f):14} {f.name}")

    if args.dry_run:
        print("\nDry run - validating markup only, creating nothing.")
        for f in files:
            try:
                plain, spans, flags = gdocs.parse_markdown(f.read_text(encoding="utf-8"))
                para, bullets = gdocs._structure_requests(plain, flags)
                print(f"  OK   {f.name[:44]:46} {len(spans):3} spans, "
                      f"{len(bullets)} bullet runs")
            except Exception as e:  # noqa: BLE001
                print(f"  FAIL {f.name[:44]:46} {e}")
        return

    docs = config.docs_service()
    drive = config.drive_service()

    created = 0
    for f in files:
        casino = casino_from_filename(f)
        text = f.read_text(encoding="utf-8")
        try:
            if not args.no_link:
                text, added = linking.link_casino_mentions(text, casino)
            else:
                added = 0
            title = gdocs.unique_title(drive, folder_id, f"{casino} Review")
            _, url = gdocs.upload_review(docs, drive, folder_id, title, text)
            print(f"\n  created: {title}\n    {url}\n    (+{added} internal links)")
            created += 1
        except Exception as e:  # noqa: BLE001
            print(f"\n  FAILED {casino}: {type(e).__name__}: {e}")

    print(f"\n{created}/{len(files)} created. The app's rolling window will now read these.")
    if created:
        print("Check one doc by eye before generating: bold, bullets, links, section headers.")


if __name__ == "__main__":
    main()
