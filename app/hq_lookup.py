"""Find a regulated entity's headquarters district on the live web.

The add-entity screen must not guess a bank's home from its name; it
asks the internet. One Claude call with the server-side web search tool
does the searching (RBI publications, the entity's own site, financial
press) and answers with a district. The model proposes; code verifies:
an answer that is not a district in app/geography.py counts as not
found, and the human always sees the result before saving.
"""
import json
import logging
import re

from . import classify, geography

log = logging.getLogger("suchak.hq")

BUILD = "2026-09-02.1-web-lookup"

MAX_CONTINUES = 3          # web-search turns may pause; resume a few times
MAX_TOKENS = 1500

_SYSTEM = (
    "You identify where Indian regulated entities (banks, NBFCs, "
    "cooperative banks) have their head office / registered office. "
    "Search the web. Prefer, in order: rbi.org.in publications, the "
    "entity's own website, established financial press. The answer that "
    "matters is the DISTRICT of the headquarters (for metropolitan "
    "cities the city itself, e.g. Mumbai). Reply with ONLY a JSON "
    'object: {"found": true/false, "district": "...", "state": "...", '
    '"source_url": "..."} - found false when you are not reasonably '
    "sure. No other text."
)


class LookupUnavailable(RuntimeError):
    """The lookup could not run (network, key, refusal) -- distinct from
    ran-and-found-nothing, which is a normal answer."""


def _final_text(message) -> str:
    parts = [b.text for b in message.content if getattr(b, "type", "") == "text"]
    return "\n".join(parts).strip()


def lookup_headquarters(name: str, kind: str = "") -> dict:
    """One entity name -> {found, district, state, place_raw, source, note}.

    `district` is the canonical spelling from the geography tables, or
    None; `place_raw` keeps what the web said so the screen can show it
    even when verification fails.
    """
    client = classify._get_client()
    ask = (f"Entity: {name}" + (f" (kind: {kind})" if kind else "")
           + "\nWhere is its head office? Answer per the instructions.")
    messages = [{"role": "user", "content": ask}]
    try:
        resp = client.messages.create(
            model=classify.MODEL,
            max_tokens=MAX_TOKENS,
            system=_SYSTEM,
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
            messages=messages)
        for _ in range(MAX_CONTINUES):
            if resp.stop_reason != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": resp.content}]
            resp = client.messages.create(
                model=classify.MODEL,
                max_tokens=MAX_TOKENS,
                system=_SYSTEM,
                tools=[{"type": "web_search_20260209", "name": "web_search"}],
                messages=messages)
    except Exception as exc:
        raise LookupUnavailable(
            f"Could not search the web for the headquarters: "
            f"{type(exc).__name__}: {exc}") from exc

    text = _final_text(resp)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LookupUnavailable(
            f"The lookup answered without JSON: {text[:200]!r}")
    try:
        data = json.loads(m.group(0))
    except ValueError as exc:
        raise LookupUnavailable(
            f"The lookup's JSON did not parse: {text[:200]!r}") from exc

    raw = " ".join(str(data.get("district") or "").split())
    state = " ".join(str(data.get("state") or "").split()) or None
    source = str(data.get("source_url") or "") or None
    if not data.get("found") or not raw:
        return {"found": False, "district": None, "state": state,
                "place_raw": raw, "source": source,
                "note": "The web search could not establish the "
                        "headquarters. Pick the district from the list."}
    canon = geography.canonical_district(raw)
    if not canon:
        return {"found": False, "district": None, "state": state,
                "place_raw": raw, "source": source,
                "note": f'The web says "{raw}", which is not a district '
                        "this app knows. Pick the closest from the list."}
    return {"found": True, "district": canon, "state": state,
            "place_raw": raw, "source": source, "note": ""}
