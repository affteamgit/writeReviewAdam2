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
