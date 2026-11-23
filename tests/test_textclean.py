"""Tests for the answer text cleanup (dashes and ellipses removed, numbers unchanged).

Typographic characters are written as escape codes so this file stays plain ASCII.
"""

from app.agent.textclean import ELLIPSIS, EM_DASH, EN_DASH, clean_answer, clean_text
from app.agent.validator import numbers_in_text


def test_dashes_and_ellipses_removed():
    assert clean_text(f"Heat adds nothing {EM_DASH} not because heat is harmless.") == \
        "Heat adds nothing, not because heat is harmless."
    assert clean_text(f"90% range: 1.03{EN_DASH}1.75 days") == "90% range: 1.03 to 1.75 days"
    assert clean_text(f"flooding{EM_DASH}then storms") == "flooding, then storms"
    assert clean_text(f"Working on it{ELLIPSIS}") == "Working on it..."
    assert clean_text(f"Dec 12 {EN_DASH} Mar 14") == "Dec 12 - Mar 14"


def test_numbers_are_unchanged():
    s = f"Dallas ranks 14 of 47 at 1.32 days/yr (90% range 1.03{EN_DASH}1.75) {EM_DASH} flood 0.88."
    assert [v for _, v, _, _ in numbers_in_text(clean_text(s))] == [v for _, v, _, _ in numbers_in_text(s)]


def test_clean_answer_fields():
    d = f" {EM_DASH} "
    a = clean_answer({"answer_markdown": f"a{d}b", "assumptions": [f"x{d}y"], "suggested_followups": [f"p{d}q"],
                      "uncertainty": {"level": "low", "reasons": [f"r{d}s"]}, "hubs": [{"key_drivers": [f"k{d}l"]}]})
    assert EM_DASH not in str(a)
