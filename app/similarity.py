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
    "what", "when", "how", "why", "check", "here", "your", "you",
}

# Written-out numbers match their digits: "beyond four" == "beyond 4".
_NUM_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}


_MONTHS = {
    "january": "jan", "february": "feb", "march": "mar", "april": "apr",
    "june": "jun", "july": "jul", "august": "aug", "september": "sep",
    "sept": "sep", "october": "oct", "november": "nov", "december": "dec",
}


# Words for one event that the press writes half a dozen ways. Folding
# them to a single token is what lets "CEO resigns", "MD steps down" and
# "chief executive quits" be recognised as one story -- without this, a
# resignation covered by five outlets filled the queue five times.
# Deliberately narrow: only true synonyms of an EVENT, never words that
# distinguish events (Q1/Q2, crore/lakh, RBI/SEBI stay as they are).
_EVENT_SYNONYMS = {
    # departures
    "resign": "resign", "resignation": "resign", "resigns": "resign",
    "quit": "resign", "quits": "resign", "quitting": "resign",
    "steps": "resign", "stepping": "resign", "stepped": "resign",
    "exits": "resign", "exit": "resign", "departs": "resign",
    "departure": "resign", "leaves": "resign", "leaving": "resign",
    # the office being vacated or filled
    "ceo": "ceo", "md": "ceo", "chief": "ceo", "executive": "ceo",
    "chairman": "chairman", "chairperson": "chairman",
    "director": "director", "chairman-cum-managing": "chairman",
    "appointed": "appoint", "appointment": "appoint", "appoints": "appoint",
    "names": "appoint", "named": "appoint", "elevates": "appoint",
    "successor": "appoint",
    # enforcement
    "penalty": "penalty", "penalised": "penalty", "penalized": "penalty",
    "penalises": "penalty", "fine": "penalty", "fines": "penalty",
    "fined": "penalty", "imposes": "penalty", "imposed": "penalty",
    # supervisory action
    "curbs": "curb", "curb": "curb", "restrictions": "curb",
    "restriction": "curb", "restricts": "curb", "bars": "curb",
    "barred": "curb", "moratorium": "curb",
    # failures
    "outage": "outage", "outages": "outage", "downtime": "outage",
    "glitch": "outage", "glitches": "outage", "disruption": "outage",
    "fraud": "fraud", "scam": "fraud", "embezzlement": "fraud",
    "defrauded": "fraud", "siphoned": "fraud",
}


def _stem(token: str) -> str:
    """Light plural folding so "withdrawals" matches "withdrawal" and
    "charges" matches "charge" -- headline variants of one event routinely
    differ only in number. Months fold to their abbreviations for the same
    reason ("from Oct" == "from 1 October")."""
    token = _NUM_WORDS.get(token, token)
    token = _MONTHS.get(token, token)
    if token in _EVENT_SYNONYMS:
        return _EVENT_SYNONYMS[token]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        token = token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    return [
        _stem(t) for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 1 and t not in _STOPWORDS
    ]


def strip_publisher(title: str, source_name: str | None = None) -> str:
    """Remove the " - Publisher" suffix Google News appends to headlines.

    The suffix differs per outlet, so it poisons similarity between two
    outlets' headlines for the same event. Stripped only when the tail
    matches the item's source name or looks like a domain -- a real
    headline containing " - " is left alone.
    """
    head, sep, tail = (title or "").rpartition(" - ")
    if not sep or not head.strip() or len(tail) > 60:
        return title
    tail_n = re.sub(r"[^a-z0-9]", "", tail.lower())
    src_n = re.sub(r"[^a-z0-9]", "", (source_name or "").lower())
    if src_n and (src_n in tail_n or tail_n in src_n):
        return head.strip()
    if re.search(r"\.(com|in|net|org)$", tail.strip().lower()):
        return head.strip()
    return title


# Tokens so common in Indian banking headlines that sharing them says
# nothing about whether two items describe the same event.
_DOMAIN_GENERIC = {
    "bank", "rs", "inr", "crore", "lakh", "india", "indian", "ltd",
    "limited", "share", "stock", "customer", "account",
}


# Words that recur across unrelated stories about the same bank. Two
# headlines sharing only these describe one topic, not one event: "Q1
# profit rises" and "Q2 profit rises" share profit+rise and are different
# stories. A shared word outside this set -- a person's name, a place, a
# product -- is real evidence of one event.
_WEAK_SHARED = {
    "profit", "loss", "result", "quarter", "year", "growth", "rise",
    "fall", "gain", "drop", "jump", "surge", "slip", "report", "market",
    "price", "target", "buy", "sell", "rating", "analyst", "brokerage",
    "cent", "percent", "pc", "high", "low", "record", "plan", "launch",
    "new", "say", "sees", "expect", "fy", "q1", "q2", "q3", "q4",
    "board", "meeting", "approve", "raise", "fund", "branch", "loan",
    "deposit", "rate", "interest", "credit", "card", "app", "service",
    "ceo", "chairman", "director",
}


def strong_shared(a: str, b: str, exclude: set | None = None) -> int:
    """Shared words that actually pin down ONE event -- names, places,
    products -- rather than the vocabulary every story about this bank
    uses."""
    drop = _DOMAIN_GENERIC | _WEAK_SHARED | (exclude or set())
    return len((set(tokenize(a)) - drop) & (set(tokenize(b)) - drop))


def event_similarity(a: str, b: str, exclude: set | None = None) -> float:
    """Similarity between two headlines describing (maybe) one event.

    The entity's own aliases and domain-generic words are excluded: every
    SBI headline contains "SBI", so it inflates all pairs equally while
    distinctive words (withdrawal, penalty, outage) should decide."""
    drop = _DOMAIN_GENERIC | (exclude or set())
    ta = Counter(t for t in tokenize(a) if t not in drop)
    tb = Counter(t for t in tokenize(b) if t not in drop)
    return _cosine(ta, tb)


def distinctive_overlap(a: str, b: str, exclude: set | None = None) -> int:
    """How many distinctive words two headlines share -- the guard that
    stops a high cosine built on one or two words from merging different
    events."""
    drop = _DOMAIN_GENERIC | (exclude or set())
    return len((set(tokenize(a)) - drop) & (set(tokenize(b)) - drop))


def alias_tokens(aliases: list[str]) -> set:
    return {t for a in aliases for t in tokenize(a)}


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
