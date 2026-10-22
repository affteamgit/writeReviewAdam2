"""
test_linking.py - the internal-linking pass, run against a fixed review map so no
sitemap fetch is needed.
"""

import linking

MAP = {
    "stake": "https://gamblineers.com/stake-casino-review/",
    "reels": "https://gamblineers.com/reels-casino-review/",
    "7bit": "https://gamblineers.com/7bit-casino-review/",
    "mbit": "https://gamblineers.com/mbit-casino-review/",
    "bc-game": "https://gamblineers.com/bc-game-review/",
}


def test_brand_names_are_linked_with_their_casing_kept():
    text = "Stake pays faster, 7Bit has more tools, mBit pays in a minute and BC.Game takes 124 coins."
    linked, added = linking.link_casino_mentions(text, "Roobet", MAP)
    assert added == 4
    assert "[Stake](https://gamblineers.com/stake-casino-review/)" in linked
    assert "[7Bit](https://gamblineers.com/7bit-casino-review/)" in linked
    assert "[mBit](https://gamblineers.com/mbit-casino-review/)" in linked
    assert "[BC.Game](https://gamblineers.com/bc-game-review/)" in linked


def test_common_words_in_running_text_are_not_linked():
    """Real case (BetFury review, 2026-10-19): 'lets you stake crypto' was linked to
    Stake's review. Lowercase words are never brand mentions."""
    text = "The wallet also lets you stake crypto, and the reels spin fast."
    linked, added = linking.link_casino_mentions(text, "BetFury", MAP)
    assert added == 0
    assert linked == text


def test_reviewed_casino_is_never_self_linked():
    linked, added = linking.link_casino_mentions("Stake is the site under review.", "Stake", MAP)
    assert added == 0
