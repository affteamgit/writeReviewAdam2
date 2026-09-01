"""
app.py - Gamblineers review generator (Streamlit).

Wires the agent generator to the pieces that make it usable by a team:

  casino picker -> Opus 5 single-pass generation -> verified internal linking
                -> formatted Google Doc in the review folder -> link written back

Deliberate choices worth knowing:

  * The rolling anti-repetition window is read from the Drive folder, not local disk.
    A hosted container's filesystem is ephemeral, so local history would vanish on every
    redeploy and repetition would quietly return with no error to notice.
  * Nothing is ever deleted. The old app deleted any existing doc with the same title
    before writing a new one, which would destroy a draft a reviewer was commenting on.
    Titles are versioned instead.
  * The casino name comes from a picker over the Live list rather than a free-text cell,
    so two people generating at once can't collide the way the old TempOutput!B1 flow did.
  * A password gate, because this holds an Opus API key.

Run locally:  .venv/bin/streamlit run app.py
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

import streamlit as st

import config
import gdocs
import history as history_mod
import linking
import writeReviewAgent as agent

st.set_page_config(page_title="Gamblineers Review Generator", layout="centered")


# ---------------------------------------------------------------------------
# ACCESS GATE
# ---------------------------------------------------------------------------

def check_password() -> bool:
    """Simple shared-password gate.

    Not authentication in any real sense, but this app can spend real money per click,
    so it must not be openly reachable. If APP_PASSWORD is unset the app refuses to run
    rather than defaulting to open - failing closed is the only safe default here.
    """
    expected = config.get("APP_PASSWORD")
    if not expected:
        st.error(
            "APP_PASSWORD is not set. Add it to Streamlit secrets before using this app "
            "- it holds an API key that costs money per generation."
        )
        return False
    if st.session_state.get("authed"):
        return True

    st.title("Gamblineers Review Generator")
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        if st.form_submit_button("Enter"):
            if pw == expected:
                st.session_state.authed = True
                st.rerun()
            else:
                st.error("Wrong password.")
    return False


# ---------------------------------------------------------------------------
# CACHED RESOURCES
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_services():
    return config.docs_service(), config.drive_service()


@st.cache_data(ttl=600, show_spinner=False)
def get_casino_names() -> list:
    """Live casino names for the picker. Cached briefly so the sheet isn't refetched on
    every widget interaction, but short enough that a newly added casino shows up."""
    return agent.CasinoDB().live_casino_names()


def load_db() -> "agent.CasinoDB":
    """Fresh per generation, deliberately uncached: the review must reflect the sheet as
    it is now, not as it was when someone else's session warmed a cache."""
    return agent.CasinoDB()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    if not check_password():
        return

    st.title("Gamblineers Review Generator")
    st.caption(
        f"Claude Opus 5, single pass. Facts come from the Casino Data sheet; the last "
        f"reviews in the Drive folder are fed back in so each new one reads differently."
    )

    folder_id = config.get("FOLDER_ID")
    if not folder_id:
        st.error("FOLDER_ID is not set in secrets - that's the Drive folder reviews go into.")
        return

    try:
        names = get_casino_names()
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not read the casino sheet: {e}")
        return

    col1, col2 = st.columns([3, 2])
    with col1:
        casino = st.selectbox("Casino", names, index=None,
                              placeholder="Pick a casino from the Live list")
    with col2:
        keyword = st.text_input("SEO phrase (must appear verbatim)",
                                value="", placeholder="defaults to '<Casino> Casino Review'")

    with st.expander("Options", expanded=False):
        effort = st.select_slider("Model effort", ["low", "medium", "high", "xhigh", "max"],
                                  value="high",
                                  help="Higher effort thinks longer. 'high' is the tested default.")
        window = st.slider("Prior reviews fed back in (anti-repetition)", 0, 8, 5)
        do_revise = st.checkbox("Run the fact/voice self-check pass", value=False,
                                help="A second Opus pass that only fixes rule breaks. "
                                     "Adds cost and time; off by default.")
        do_link = st.checkbox("Add internal links to other casino reviews", value=True)
        upload = st.checkbox("Upload to Google Docs", value=True)

    if not casino:
        st.info("Pick a casino to begin.")
        return

    if not st.button(f"Generate {casino} review", type="primary"):
        return

    status = st.status(f"Generating {casino}...", expanded=True)
    progress_line = status.empty()

    def on_progress(label, phase, elapsed, thinking_chars, text_chars, final):
        progress_line.markdown(
            f"**{label}** · {phase} · {elapsed:.0f}s · "
            f"thinking {thinking_chars:,} chars · review {text_chars:,} chars"
        )

    try:
        status.write("Reading the casino database...")
        db = load_db()

        docs, drive = get_services()
        status.write("Loading the rolling window from Drive...")
        ctx, sig = history_mod.load_both_windows(drive, folder_id, casino, n=window) \
            if window else ([], [])
        if ctx:
            status.write(f"Window: {', '.join(t for t, _ in ctx)}")
        else:
            status.write("Window: empty (first review in this folder, or none readable).")

        status.write(f"Writing with Claude Opus 5 (effort={effort})...")
        result = agent.generate_review(
            db, casino,
            keyword=keyword.strip() or None,
            history=ctx, signature_history=sig,
            effort=effort, do_revise=do_revise,
            progress=on_progress,
        )
        review = result["review"]

        links_added = 0
        if do_link:
            status.write("Adding verified internal links...")
            review, links_added = linking.link_casino_mentions(review, result["casino"])

        doc_url = None
        if upload:
            status.write("Creating the Google Doc...")
            base = f"{result['casino']} Review"
            title = gdocs.unique_title(drive, folder_id, base)
            _, doc_url = gdocs.upload_review(docs, drive, folder_id, title, review)
            status.write(f"Uploaded as **{title}**")

        status.update(label=f"{result['casino']} done", state="complete", expanded=False)

    except LookupError as e:
        status.update(label="Casino not found", state="error")
        st.error(str(e))
        return
    except ValueError as e:
        # Formatting validation refused to publish - the review is fine, the markup isn't.
        status.update(label="Formatting check failed", state="error")
        st.error(f"The review was written but not uploaded: {e}")
        st.download_button("Download the draft anyway", review,
                           file_name=f"{casino} Review.md")
        return
    except Exception as e:  # noqa: BLE001
        status.update(label="Failed", state="error")
        st.error(f"{type(e).__name__}: {e}")
        return

    # ---- results ----
    if doc_url:
        st.success(f"[Open the Google Doc]({doc_url})")
    st.metric("Cost", f"${result['cost']:.2f}")

    c1, c2, c3 = st.columns(3)
    c1.metric("Characters", f"{len(review):,}")
    c2.metric("Words", f"{len(review.split()):,}")
    c3.metric("Links added", links_added)

    for flag in result["data_flags"]:
        st.warning(f"Source data problem reported by the model:\n\n{flag}")

    with st.expander("Read the review", expanded=True):
        # Escape $ so Streamlit doesn't read dollar amounts as LaTeX math.
        st.markdown(review.replace("$", "\\$"))

    st.download_button("Download markdown", review,
                       file_name=f"{result['casino']} Review.md")


if __name__ == "__main__":
    main()
