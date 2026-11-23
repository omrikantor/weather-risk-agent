"""Deterministic cleanup of answer text: removes typographic habits that read as machine-written
(em dashes, en dashes, ellipsis characters). Numbers are never changed, so number tracing is unaffected."""

import re

_RANGE = re.compile(r"(\d)\s*–\s*(?=[-+]?\d)")          # 1.03–1.75 -> 1.03 to 1.75
_EM_SPACED = re.compile(r"\s+—\s+")                       # "word — word" -> "word, word"
_EM_TIGHT = re.compile(r"(?<=\w)—(?=\w)")                 # "word—word" -> "word, word"


def clean_text(s: str) -> str:
    if not s:
        return s
    s = _RANGE.sub(r"\1 to ", s)
    s = _EM_SPACED.sub(", ", s)
    s = _EM_TIGHT.sub(", ", s)
    s = s.replace("—", ", ").replace("–", "-").replace("…", "...")
    return re.sub(r",\s*,", ",", s)


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
