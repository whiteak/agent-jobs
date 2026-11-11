"""Word statistics."""
import re
from collections import Counter

_WORD = re.compile(r"[A-Za-z0-9'-]+")


def words(text):
    out = []
    for m in _WORD.findall(text):
        w = m.strip("'-")
        if w:
            out.append(w)
    return out


def _norm(text, ignore_case):
    ws = words(text)
    return [w.lower() for w in ws] if ignore_case else ws


def word_counts(text, ignore_case=False):
    return Counter(_norm(text, ignore_case))


def _top(counter, n):
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def top_words(text, n=10, ignore_case=False):
    return _top(word_counts(text, ignore_case), n)


def bigrams(text, ignore_case=False):
    ws = _norm(text, ignore_case)
    return Counter(zip(ws, ws[1:]))


def top_bigrams(text, n=10, ignore_case=False):
    return _top(bigrams(text, ignore_case), n)
