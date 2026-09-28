"""
test_jacob_agent.py - the BCK/Jakob reflection must work on what the live app actually
feeds it: Drive docs exported as text/plain (history.py), which have no '## Q:'
markers. The fixtures are real exports from the Jacob2 Reviews folder (2026-09-28);
the 7Bit one has section titles, the BitStarz one doesn't. Before this test the parser
only understood markdown and returned 0 pairs for both, so the reflection block was
silently empty on every live generation.
"""

from pathlib import Path

import writeReviewJacobAgent as j

FIX = Path(__file__).parent / "tests" / "fixtures"


def _export(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_plain_text_export_parses_every_question():
    pairs = j.parse_qa_pairs(_export("bck_7bit_drive_export.txt"))
    headers = [h for h, _ in pairs]
    assert len(pairs) == 20  # matches the app's "Questions answered" for this run
    assert headers[0] == "Is 7Bit legit?"
    assert headers[-1] == "Is 7Bit good for highrollers?"
    # Section titles are neither questions nor part of an answer.
    assert not any(s in (a for _, a in pairs) for s in j.SECTIONS)
    assert all("\nPayments" not in a and not a.endswith("Games") for _, a in pairs)
    assert pairs[0][1].startswith("Yes. 7Bit has been paying players since 2014")


def test_plain_text_export_without_section_titles():
    pairs = j.parse_qa_pairs(_export("bck_bitstarz_drive_export.txt"))
    assert len(pairs) == 22
    assert pairs[0][0] == "Is BitStarz legit?"


def test_reflection_is_built_from_drive_exports():
    history = [("7Bit Review", _export("bck_7bit_drive_export.txt")),
               ("BitStarz Review", _export("bck_bitstarz_drive_export.txt"))]
    sig = j.collect_qa_signatures(history)
    assert "vpn" in sig["by_slot"] and len(sig["by_slot"]["vpn"]) == 2
    reflection = j.format_qa_reflection(sig)
    assert "BC.Game" in reflection  # the line that repeated across the 2026-09-28 batch


def test_markdown_form_still_parses():
    md = ("# X review\n\n**General**\n\n## Q: Is X legit?\nYes.\n\n"
          "**Payments**\n\n## Q: How fast are withdrawals?\nFast.\n")
    assert j.parse_qa_pairs(md) == [("Is X legit?", "Yes."),
                                    ("How fast are withdrawals?", "Fast.")]


def test_missing_sections_detected():
    assert j.missing_sections(_export("bck_7bit_drive_export.txt")) == []
    assert j.missing_sections("# X review\n\n**General**\n\n**FAQ**\n") == [
        "Payments", "Games", "Responsible Gambling"]
