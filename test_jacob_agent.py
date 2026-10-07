"""
test_jacob_agent.py - checks on the finished BCK/Jakob review and the prompt around it.

The fixture is a real plain-text export from the Jacob2 Reviews folder (2026-09-28), the
same form the live app's rolling window reads.
"""

from pathlib import Path

import writeReviewJacobAgent as j

FIX = Path(__file__).parent / "tests" / "fixtures"


def test_question_count_from_markdown():
    md = ("# X review\n\n**General**\n\n## Q: Is X legit?\nYes.\n\n"
          "**Payments**\n\n## Q: How fast are withdrawals?\nFast.\n")
    assert [h for h, _ in j.parse_qa_pairs(md)] == ["Is X legit?", "How fast are withdrawals?"]


def test_missing_sections_detected():
    export = (FIX / "bck_7bit_drive_export.txt").read_text(encoding="utf-8")
    assert j.missing_sections(export) == []
    assert j.missing_sections("# X review\n\n**General**\n\n**FAQ**\n") == [
        "Payments", "Games", "Responsible Gambling"]


def test_prompt_has_full_prior_reviews_but_no_per_question_block():
    """The per-question reflection block was removed after it raised cross-review
    copying 34 -> 207 shared phrases (2026-09-29); the full prior reviews stay."""
    db = j.CasinoDB()
    row = db.find("7Bit")
    prior = (FIX / "bck_7bit_drive_export.txt").read_text(encoding="utf-8")
    _, user_text = j.assemble(db, row, "7Bit Casino Review",
                              [("BitStarz Review", prior)], "7Bit", "", "")
    assert "BEGIN PRIOR REVIEW: BitStarz Review" in user_text
    assert "A LOOK AT YOUR OWN LAST FEW REVIEWS" not in user_text


# ----------------------------------------------------------------------------
# repetition_check - the code half (finding reuse). The rewrite half calls the API.
# ----------------------------------------------------------------------------

import repetition_check as rc  # noqa: E402

NAMES = ["Roobet", "Thrill", "7Bit", "BC.Game"]


def test_reuse_found_across_casinos_with_names_masked():
    prior = [("7Bit Review", "7Bit review\n\nSignup is email-only, but the terms set no "
                             "threshold that triggers a document request.")]
    review = ("# Roobet review\n\n## Q: Can I stay anonymous at Roobet?\n"
              "Signup is email-only, but the terms set no threshold that triggers a "
              "document request. Support answers fast.\n")
    flagged = rc.find_overlaps(review, prior, NAMES)
    assert [f["sentence"] for f in flagged] == [
        "Signup is email-only, but the terms set no threshold that triggers a document request."]
    assert flagged[0]["title"] == "7Bit Review"


def test_headers_titles_and_flags_are_never_flagged():
    prior = [("Thrill Review", "Thrill review\n\nCan I use a VPN to play at Thrill?\n\nGeneral")]
    review = ("# Roobet review\n\n**General**\n\n## Q: Can I use a VPN to play at Roobet?\n"
              "No.\n\nDATA FLAG: the database lists 5,000 games while the old review said 7,000.\n")
    assert rc.find_overlaps(review, prior, NAMES) == []


def test_only_the_later_of_a_repeated_pair_in_one_review_is_flagged():
    review = ("# Roobet review\n\n## Q: A?\nKYC with no floor is the real risk at this site today.\n\n"
              "## Q: B?\nAgain, KYC with no floor is the real risk at this site today.\n")
    flagged = rc.find_overlaps(review, [], NAMES)
    assert len(flagged) == 1 and flagged[0]["sentence"].startswith("Again")
    assert flagged[0]["title"] == "this review"


def test_unique_sentences_pass():
    prior = [("Thrill Review", "Thrill review\n\nThe library sits at 3,000 titles and leans on slots.")]
    review = "# Roobet review\n\n## Q: Games?\nRoobet carries about 8,000 games across 70 studios.\n"
    assert rc.find_overlaps(review, prior, NAMES) == []


def test_every_earlier_version_of_a_sentence_is_listed_for_the_rewrite():
    prior = [("BC.Game Review", "BC.Game review\n\nThe live section rests on Evolution and "
                                "Pragmatic Play, the two names I look for."),
             ("Bspin Review", "Bspin review\n\nThe live section is built on Evolution and "
                              "Pragmatic Play, exactly the two I look for.")]
    review = ("# Roobet review\n\n## Q: Games?\nThe live section is built on Evolution and "
              "Pragmatic Play, the two names I look for.\n")
    flagged = rc.find_overlaps(review, prior, NAMES)
    assert len(flagged) == 1
    assert {mt["title"] for mt in flagged[0]["matches"]} == {"BC.Game Review", "Bspin Review"}
    prompt = rc._rewrite_prompt(review, flagged)
    assert "(BC.Game Review)" in prompt and "(Bspin Review)" in prompt


def test_writer_prompt_has_no_seeds_the_mentor_flagged():
    prompt = j.VOICE + j.TASK + "".join(s["guidance"] for s in j.QUESTION_SLOTS)
    for seed in ("email-only", "has no floor", "real signal for", "name the actual providers"):
        assert seed not in prompt, seed
