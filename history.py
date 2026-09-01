"""
history.py - the rolling window of recently published reviews, read from Google Drive.

Why Drive and not the filesystem: the anti-repetition mechanism feeds the last N
reviews back into the prompt, and a hosted Streamlit container has an ephemeral disk.
Local history would vanish on every redeploy and idle restart, and it would fail
silently - reviews would simply start repeating each other again with no error to
notice. Reading the Drive folder the app already publishes into fixes that and buys two
more things:

  * The window becomes shared across everyone using the app, so two writers can't
    unknowingly reproduce each other's habits.
  * It reflects "the last N reviews actually published" rather than "the last N this
    process happened to write".

Exports the same (title, text) shape the local loader used, so the generator does not
care which source it got.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def list_recent_docs(drive, folder_id: str, limit: int = 40) -> List[dict]:
    """Newest Google Docs in the folder, most recent first."""
    resp = drive.files().list(
        q=(f"'{folder_id}' in parents and trashed=false "
           "and mimeType='application/vnd.google-apps.document'"),
        orderBy="createdTime desc",
        fields="files(id,name,createdTime)",
        pageSize=limit,
    ).execute()
    return resp.get("files", [])


def _export_text(drive, file_id: str) -> str:
    """Plain-text export of a Doc.

    text/plain rather than markdown on purpose: Google's markdown exporter adds its own
    escaping (turning "2019." into "2019\\." and "#" into "\\#"), which would pollute
    the voice and phrase analysis with artefacts that were never in the document. The
    window is only used to detect repetition and set rhythm, so formatting is noise here.
    """
    data = drive.files().export(fileId=file_id, mimeType="text/plain").execute()
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def load_window(
    drive,
    folder_id: str,
    focus_casino: str,
    n: int = 5,
    exclude_same_casino: bool = True,
) -> List[Tuple[str, str]]:
    """The n most recent published reviews as (title, text).

    exclude_same_casino mirrors the local loader: the full-text context skips prior
    reviews of the casino being written (so the model neither copies nor over-avoids its
    own last take), while phrase/opener bans deliberately include them - a casino's own
    previous review is the most important place not to repeat yourself.

    Fails open: any Drive problem returns what it managed to collect, because a missing
    window degrades variety but must never block a generation.
    """
    slug = _norm(focus_casino)
    out: List[Tuple[str, str]] = []
    try:
        files = list_recent_docs(drive, folder_id)
    except Exception as e:  # noqa: BLE001
        print(f"Could not list the review folder, continuing without history: {e}")
        return []

    for f in files:
        if len(out) >= n:
            break
        if exclude_same_casino and slug and slug in _norm(f["name"]):
            continue
        try:
            text = _export_text(drive, f["id"])
        except Exception as e:  # noqa: BLE001
            print(f"Skipping {f['name']} (export failed: {e})")
            continue
        if text.strip():
            out.append((f["name"], text))
    return out


def load_both_windows(
    drive, folder_id: str, focus_casino: str, n: int = 5
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """(context_window, signature_window) in one pass over the folder.

    Fetching once and filtering twice avoids exporting the same documents twice, which
    on a five-review window is five extra Drive round-trips per generation.
    """
    slug = _norm(focus_casino)
    try:
        files = list_recent_docs(drive, folder_id)
    except Exception as e:  # noqa: BLE001
        print(f"Could not list the review folder, continuing without history: {e}")
        return [], []

    fetched: List[Tuple[str, str]] = []
    for f in files:
        # Stop once both windows can be filled: the signature window is the wider of
        # the two (it keeps same-casino docs), so n entries of it is always enough.
        if len(fetched) >= n:
            break
        try:
            text = _export_text(drive, f["id"])
        except Exception as e:  # noqa: BLE001
            print(f"Skipping {f['name']} (export failed: {e})")
            continue
        if text.strip():
            fetched.append((f["name"], text))

    signature = fetched[:n]
    context = [(t, x) for t, x in fetched if not (slug and slug in _norm(t))][:n]
    return context, signature
