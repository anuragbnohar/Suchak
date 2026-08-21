"""Pure-Python text similarity: no numpy/sklearn, fine at prototype scale.

Used for (a) near-duplicate detection at ingest time and (b) retrieving
similar reviewed items to power few-shot classification and action
suggestions.
"""
import math
import re
from collections import Counter

_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "for", "on", "and", "or", "is", "are",
    "was", "were", "with", "at", "by", "from", "as", "it", "its", "this",
    "that", "be", "has", "have", "had", "will", "would", "after", "over",
    "into", "amid", "says", "say", "said", "new", "not", "no", "but",
}


def tokenize(text: str) -> list[str]:
    return [
        t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 1 and t not in _STOPWORDS
    ]


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    num = sum(v * b[t] for t, v in a.items() if t in b)
    if num == 0:
        return 0.0
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0


def title_similarity(a: str, b: str) -> float:
    """Cosine similarity of token counts — used for duplicate clustering."""
    return _cosine(Counter(tokenize(a)), Counter(tokenize(b)))


def rank_similar(query: str, candidates: list[tuple], top_k: int = 3,
                 min_score: float = 0.12) -> list[tuple]:
    """Rank (key, text) candidates against a query with TF-IDF weighting
    computed over the candidate set. Returns [(key, score)] best-first."""
    if not candidates:
        return []
    docs = {key: tokenize(text) for key, text in candidates}
    n_docs = len(docs) + 1
    df = Counter(term for toks in docs.values() for term in set(toks))

    def vec(toks: list[str]) -> dict:
        if not toks:
            return {}
        tf = Counter(toks)
        return {
            t: (c / len(toks)) * (math.log((n_docs + 1) / (1 + df[t])) + 1.0)
            for t, c in tf.items()
        }

    qv = Counter(vec(tokenize(query)))
    scored = [(key, _cosine(qv, Counter(vec(toks)))) for key, toks in docs.items()]
    scored = [(k, s) for k, s in scored if s >= min_score]
    scored.sort(key=lambda p: p[1], reverse=True)
    return scored[:top_k]
