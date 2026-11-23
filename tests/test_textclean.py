from app.agent.textclean import clean_answer, clean_text
from app.agent.validator import numbers_in_text


def test_dashes_and_ellipses_removed():
    assert clean_text("Heat adds nothing — not because heat is harmless.") == \
        "Heat adds nothing, not because heat is harmless."
    assert clean_text("90% range: 1.03–1.75 days") == "90% range: 1.03 to 1.75 days"
    assert clean_text("flooding—then storms") == "flooding, then storms"
    assert clean_text("Working on it…") == "Working on it..."
    assert clean_text("Dec 12 – Mar 14") == "Dec 12 - Mar 14"


def test_numbers_are_unchanged():
    s = "Dallas ranks 14 of 47 at 1.32 days/yr (90% range 1.03–1.75) — flood 0.88."
    assert [v for _, v, _, _ in numbers_in_text(clean_text(s))] == [v for _, v, _, _ in numbers_in_text(s)]


def test_clean_answer_fields():
    a = clean_answer({"answer_markdown": "a — b", "assumptions": ["x — y"], "suggested_followups": ["p — q"],
                      "uncertainty": {"level": "low", "reasons": ["r — s"]}, "hubs": [{"key_drivers": ["k — l"]}]})
    assert "—" not in str(a)
