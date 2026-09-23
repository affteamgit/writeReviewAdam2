"""
test_app.py - behavioral test for the site selector (Gamblineers/Adam vs BCK/Jakob)
added to app.py this session, via Streamlit's AppTest framework.

Makes real live Sheets API calls (get_casino_names() is not mocked) - same choice
already established for this app's UI tests, since a mocked casino list can't catch a
real wiring bug between the site selector and which CasinoDB actually gets queried.
Needs a real GOOGLE_SERVICE_ACCOUNT_FILE reachable via config.py's normal resolution.
"""

import os

os.environ.setdefault("APP_PASSWORD", "test-only-password")
os.environ.setdefault("FOLDER_ID", "15ubKdKEC1eH_XagXNUc0ZM9wv8byKMHg")  # Adam2 Reviews
os.environ.setdefault("FOLDER_ID_BCK", "1O4U3BOZlSAB11chDPvRiAwfCEOIx8SfJ")  # Jacob2 Reviews

from streamlit.testing.v1 import AppTest


def _logged_in_app() -> AppTest:
    at = AppTest.from_file("app.py", default_timeout=60)
    at.session_state["authed"] = True
    at.run()
    return at


def test_site_selector_switches_casino_list_and_resets_selection():
    at = _logged_in_app()

    # Default site is Gamblineers - its casino list should be showing.
    site_select = at.selectbox(key="site_select")
    assert site_select.value == "Gamblineers (Adam)"
    gambl_casino = at.selectbox(key="casino_select")
    gambl_options = list(gambl_casino.options)
    assert "BitStarz" in gambl_options

    # Pick a casino and type a keyword for it.
    gambl_casino.select("BitStarz").run()
    at.text_input(key="keyword_input").input("BitStarz Casino Review").run()
    assert at.text_input(key="keyword_input").value == "BitStarz Casino Review"

    # Switch the site to BCK - the casino list must change to BCK's set, and the
    # previous Gamblineers casino selection/keyword must not silently survive into
    # the new site's context (that would let a Gamblineers-only casino name leak
    # into a BCK generation call).
    at.selectbox(key="site_select").select("BCK (Jakob)").run()

    bck_casino = at.selectbox(key="casino_select")
    bck_options = list(bck_casino.options)
    assert bck_options != gambl_options, (
        "casino list did not change when the site was switched"
    )
    assert bck_casino.value is None, (
        "the Gamblineers casino selection survived the site switch"
    )
    assert at.text_input(key="keyword_input").value == "", (
        "the Gamblineers keyword survived the site switch"
    )


if __name__ == "__main__":
    test_site_selector_switches_casino_list_and_resets_selection()
    print("PASS")
