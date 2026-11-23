"""Deterministic cleanup of answer text.

Removes typographic habits that read as machine-written: em dashes, en dashes and the ellipsis character.
The prompt already asks the model to avoid them; this code makes the result independent of whether it did.
Numbers are never changed, so number tracing is unaffected.

The characters are defined by their code points (EM_DASH and so on) so this source file stays plain ASCII.
"""

import re

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
ELLIPSIS = chr(0x2026)

# A number range written with an en dash ("1.03" en dash "1.75") becomes "1.03 to 1.75".
_RANGE = re.compile(r"(\d)\s*" + EN_DASH + r"\s*(?=[-+]?\d)")
# An em dash between spaced words ("word" space em dash space "word") becomes a comma.
_EM_SPACED = re.compile(r"\s+" + EM_DASH + r"\s+")
# An em dash directly between two words becomes a comma as well.
_EM_TIGHT = re.compile(r"(?<=\w)" + EM_DASH + r"(?=\w)")


def clean_text(s: str) -> str:
    """Replace dashes and ellipsis characters in one string with plain punctuation."""
    if not s:
        return s
    s = _RANGE.sub(r"\1 to ", s)
    s = _EM_SPACED.sub(", ", s)
    s = _EM_TIGHT.sub(", ", s)
    # Any remaining cases: em dash to comma, en dash to hyphen, ellipsis character to three dots.
    s = s.replace(EM_DASH, ", ").replace(EN_DASH, "-").replace(ELLIPSIS, "...")
    return re.sub(r",\s*,", ",", s)   # collapse a double comma created by the replacements


def clean_answer(answer: dict) -> dict:
    """Clean every human-readable text field of a FinalAnswer dict."""
    a = dict(answer)
    a["answer_markdown"] = clean_text(a.get("answer_markdown", ""))
    a["assumptions"] = [clean_text(x) for x in a.get("assumptions", [])]
    a["suggested_followups"] = [clean_text(x) for x in a.get("suggested_followups", [])]
    if isinstance(a.get("uncertainty"), dict):
        a["uncertainty"] = {**a["uncertainty"], "reasons": [clean_text(x) for x in a["uncertainty"].get("reasons", [])]}
    for h in a.get("hubs", []):
        h["key_drivers"] = [clean_text(x) for x in h.get("key_drivers", [])]
    return a
