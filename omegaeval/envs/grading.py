"""Deterministic answer matching. Anything these rules cannot decide goes to a judge."""
from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s./%-]", re.UNICODE)
_ABSTAIN = ("don't know", "do not know", "not mentioned", "no information", "can't tell", "cannot tell",
            "not sure", "never mentioned", "didn't mention", "did not mention")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", str(text)).casefold().strip()
    t = re.sub(r"^(the answer is|answer:|final answer:)\s*", "", t)
    t = _PUNCT.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    for article in ("the ", "a ", "an "):
        if t.startswith(article):
            t = t[len(article):]
    return t


def _number(text: str) -> float | None:
    t = unicodedata.normalize("NFKC", str(text)).casefold().strip()
    t = re.sub(r"^(the answer is|answer:|final answer:)\s*", "", t)
    t = re.sub(r"(?<=\d)[,\s](?=\d{3}\b)", "", t).strip(" .$")
    m = re.fullmatch(r"-?\d+(?:\.\d+)?", t)
    if m:
        return float(t)
    m = re.fullmatch(r"(-?\d+)\s*/\s*(\d+)", t)
    if m and int(m.group(2)):
        return int(m.group(1)) / int(m.group(2))
    return None


def exact_match(reply: str, gold: str) -> bool:
    a, b = normalize(reply), normalize(gold)
    if a == b:
        return True
    na, nb = _number(reply), _number(gold)
    return na is not None and nb is not None and abs(na - nb) <= 1e-9 * max(1.0, abs(nb))


def contains_match(reply: str, gold: str) -> bool:
    a, b = normalize(reply), normalize(gold)
    return bool(b) and (a == b or re.search(r"(?:^|\s)" + re.escape(b) + r"(?:\s|$)", a) is not None)


def letter_choice(reply: str, choices: list[str], strict: bool = False) -> str | None:
    """Which option a reply picks, or None when it does not pick exactly one.

    In order: the reply is just a letter ("B", "(b)", "B."); it says "answer is B" or
    "option B"; it contains exactly one of the choice texts; it marks a letter ("B." "B)").
    A bare capital at the start of a sentence ("A prime is 53") is never read as an option.
    strict keeps only the first two rules, for when the option texts are unknown.
    """
    n = len(choices)
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:max(1, min(n, 26))]
    r = reply.strip()
    m = re.fullmatch(r"\(?([" + letters + letters.lower() + r"])\)?[.):]?", r)
    if m:
        return m.group(1).upper()
    m = re.search(r"(?:answer|option|choice)\s*(?:is|:)?\s*\(?([" + letters + letters.lower() + r"])\)?(?![A-Za-z])", r, re.I)
    if m:
        return m.group(1).upper()
    if strict:
        return None
    named = [i for i, c in enumerate(choices) if c and contains_match(r, c)]
    if len(named) == 1:
        return letters[named[0]]
    m = re.search(r"(?<![A-Za-z])\(?([" + letters + r"])[.)](?=\s|$)", r)
    if m:
        return m.group(1)
    return None


def abstains(reply: str) -> bool:
    r = reply.casefold()
    return any(p in r for p in _ABSTAIN)
