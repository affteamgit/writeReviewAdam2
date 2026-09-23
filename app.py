"""
app.py - Review generator (Streamlit), Gamblineers/Adam and BCK/Jakob.

Wires each site's single-agent generator to the pieces that make it usable by a team:

  casino picker -> single-pass generation -> (Gamblineers only: verified internal
                linking) -> formatted Google Doc in that site's review folder

Deliberate choices worth knowing:

  * A "Website" selector (added 2026-09-22) picks between the two sites' pipelines,
    casino sets, and Drive folders. The two pipelines differ in real ways beyond just
    which module to call - Gamblineers' generate_review() takes a separate signature
    window and a do_revise pass; BCK's takes fetch_evolution/fetch_player_feedback
    toggles instead and has no repetition-leak metric (it uses the reflection
    mechanism, not a computed ban-list check). Branched only where they actually
    differ, not duplicated wholesale.
  * The rolling anti-repetition window is read from each site's own Drive folder, not
    local disk. A hosted container's filesystem is ephemeral, so local history would
    vanish on every redeploy and repetition would quietly return with no error to
    notice.
  * Nothing is ever deleted. The old app deleted any existing doc with the same title
    before writing a new one, which would destroy a draft a reviewer was commenting on.
    Titles are versioned instead.
  * The casino name comes from a picker over the Live list rather than a free-text cell,
    so two people generating at once can't collide the way the old TempOutput!B1 flow did.
  * A password gate, because this holds paid API keys.

Run locally:  .venv/bin/streamlit run app.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import streamlit as st

import config
import gdocs
import history as history_mod
import linking
import writeReviewAgent as adam_agent
import writeReviewJacobAgent as jacob_agent

st.set_page_config(page_title="Review Generator", layout="centered")

SITES = {
    "Gamblineers (Adam)": "gamblineers",
    "BCK (Jakob)": "bck",
}


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
            "- it holds API keys that cost money per generation."
        )
        return False
    if st.session_state.get("authed"):
        return True

    st.title("Review Generator")
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
def get_casino_names(site_key: str) -> list:
    """Live casino names for the picker. Cached briefly so the sheet isn't refetched on
    every widget interaction, but short enough that a newly added casino shows up.

    site_key is part of the cache key on purpose - Gamblineers and BCK are different
    casino sets from the same shared spreadsheet, and caching them under one key would
    let one site's list leak into the other's picker after a site switch.
    """
    if site_key == "gamblineers":
        return adam_agent.CasinoDB().live_casino_names()
    return jacob_agent.CasinoDB().live_casino_names()


def load_db(site_key: str):
    """Fresh per generation, deliberately uncached: the review must reflect the sheet as
    it is now, not as it was when someone else's session warmed a cache."""
    if site_key == "gamblineers":
        return adam_agent.CasinoDB()
    return jacob_agent.CasinoDB()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    if not check_password():
        return

    st.title("Review Generator")

    site_label = st.selectbox("Website", list(SITES.keys()), key="site_select")
    site_key = SITES[site_label]

    # Switching sites must not let a casino selection or SEO phrase from the other
    # site's list silently carry over - the two sites have different casino sets, and
    # BitStarz-on-Gamblineers is not the same generation target as BitStarz-on-BCK.
    # Same session-state-reset-before-widget-creation pattern already used below for
    # the casino-change case.
    if st.session_state.get("_site_reset_for") != site_key:
        st.session_state["_site_reset_for"] = site_key
        st.session_state["casino_select"] = None
        st.session_state["_keyword_reset_for"] = None
        st.session_state["keyword_input"] = ""

    if site_key == "gamblineers":
        st.caption(
            "Claude Opus 5, single pass. Facts come from the Casino Data sheet; the "
            "last reviews in the Drive folder are fed back in so each new one reads "
            "differently."
        )
        folder_id = config.get("FOLDER_ID")
        folder_secret_name = "FOLDER_ID"
    else:
        st.caption(
            "Single pass. Facts come from the Casino Data sheet; the last reviews in "
            "the BCK Drive folder are fed back in so each new one reads differently."
        )
        folder_id = config.get("FOLDER_ID_BCK")
        folder_secret_name = "FOLDER_ID_BCK"

    if not folder_id:
        st.error(f"{folder_secret_name} is not set in secrets - that's the Drive "
                 f"folder reviews go into for this site.")
        return

    try:
        names = get_casino_names(site_key)
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not read the casino sheet: {e}")
        return

    col1, col2 = st.columns([3, 2])
    with col1:
        casino = st.selectbox("Casino", names, index=None,
                              placeholder="Pick a casino from the Live list",
                              key="casino_select")

    # The SEO phrase is required and must be THIS casino's - it gets written verbatim
    # into the review. A Streamlit widget otherwise keeps its typed value across
    # reruns, so picking a new casino without touching this field silently carries the
    # old casino's phrase into the new review. Clearing it here, before the widget is
    # created, forces a fresh phrase every time the casino changes.
    if st.session_state.get("_keyword_reset_for") != casino:
        st.session_state["_keyword_reset_for"] = casino
        st.session_state["keyword_input"] = ""

    with col2:
        keyword = st.text_input("SEO phrase (required, must appear verbatim) *",
                                placeholder="e.g. 'BitStarz Casino Review'",
                                key="keyword_input")

    with st.expander("Options", expanded=False):
        effort = st.select_slider("Model effort", ["low", "medium", "high", "xhigh", "max"],
                                  value="high",
                                  help="Higher effort thinks longer. 'high' is the tested default.")
        window = st.slider("Prior reviews fed back in (anti-repetition)", 0, 8, 5)
        if site_key == "gamblineers":
            do_revise = st.checkbox("Run the fact/voice self-check pass", value=False,
                                    help="A second Opus pass that only fixes rule breaks. "
                                         "Adds cost and time; off by default.")
            do_link = st.checkbox("Add internal links to other casino reviews", value=True)
        else:
            do_revise = False
            do_link = False
            fetch_evolution = st.checkbox(
                "Compare against the previously published review", value=True,
                help="Reads the casino's last BCK review from the WordPress DB to "
                     "state real deltas (game count grew, a feature is new). Skips "
                     "quietly if no prior review or DB access is found.")
            fetch_player_feedback = st.checkbox(
                "Scrape live player feedback (AskGamblers/Trustpilot)", value=False,
                help="Live Playwright scraping at generation time. Off by default - "
                     "adds real time, and needs a browser installed on the host.")
        upload = st.checkbox("Upload to Google Docs", value=True)

    if not casino:
        st.info("Pick a casino to begin.")
        return

    keyword = keyword.strip()
    if not keyword:
        st.warning("Enter the SEO phrase for this casino before generating - "
                  "it's required and gets written into the review verbatim.")
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
        db = load_db(site_key)

        docs, drive = get_services()
        status.write("Loading the rolling window from Drive...")
        if site_key == "gamblineers":
            ctx, sig = history_mod.load_both_windows(drive, folder_id, casino, n=window) \
                if window else ([], [])
        else:
            ctx = history_mod.load_window(drive, folder_id, casino, n=window) if window else []
            sig = None
        if ctx:
            status.write(f"Window: {', '.join(t for t, _ in ctx)}")
        else:
            status.write("Window: empty (first review in this folder, or none readable).")

        if site_key == "gamblineers":
            status.write("Writing with Claude Opus 5 (effort=%s)..." % effort)
            result = adam_agent.generate_review(
                db, casino,
                keyword=keyword,
                history=ctx, signature_history=sig,
                effort=effort, do_revise=do_revise,
                progress=on_progress,
            )
        else:
            status.write("Writing with Claude Fable 5.1 (effort=%s)..." % effort)
            result = jacob_agent.generate_review(
                db, casino,
                keyword=keyword,
                history=ctx,
                effort=effort,
                fetch_evolution=fetch_evolution,
                fetch_player_feedback=fetch_player_feedback,
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

    if site_key == "gamblineers":
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Characters", f"{len(review):,}")
        c2.metric("Words", f"{len(review.split()):,}")
        c3.metric("Links added", links_added)
        leaks = result.get("repetition_leaks", [])
        c4.metric("Repetition check", "0 leaked" if not leaks else f"{len(leaks)} leaked",
                 help=f"{result.get('banned_move_count', 0)} moves (openers, section "
                      f"closers, phrases) were banned from the last {len(result['history_titles'])} "
                      f"reviews in the window. This counts how many reappeared anyway - "
                      f"computed by checking the actual text, not self-reported by the model.")

        if leaks:
            with st.expander(f"⚠️ {len(leaks)} banned move(s) reappeared anyway", expanded=True):
                for l in leaks:
                    st.write(f"- {l}")
        elif result.get("banned_move_count"):
            st.caption(f"Repetition check: {result['banned_move_count']} moves banned from "
                      f"the window ({', '.join(result['history_titles'])}), none reappeared.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Characters", f"{len(review):,}")
        c2.metric("Words", f"{len(review.split()):,}")
        c3.metric("Questions answered", result.get("question_count", 0))
        c4.metric("Player feedback", result.get("player_feedback_count", 0))
        st.caption(f"Evolution (previous-review comparison): {result.get('evolution_status', 'skipped')}")

    for flag in result["data_flags"]:
        st.warning(f"Source data problem reported by the model:\n\n{flag}")

    with st.expander("Read the review", expanded=True):
        # Escape $ so Streamlit doesn't read dollar amounts as LaTeX math.
        st.markdown(review.replace("$", "\\$"))

    st.download_button("Download markdown", review,
                       file_name=f"{result['casino']} Review.md")


if __name__ == "__main__":
    main()
