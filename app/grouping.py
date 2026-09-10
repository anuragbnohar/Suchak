"""Collapsing repeat complaints, so one persistent customer is not a trend.

Phase 1 recorded *who* complained. This is what that record is for. A
customer who writes six times about the same failed refund filled six
rows of the Social media tab, and six rows read as six complaints --
a spike where there is one aggrieved person. Here the six become one
entry with the other five folded under it.

Nothing is discarded. Every post keeps its row, its classification, its
severity and its place in every count on the page; only the *list*
changes. A reviewer can unfold a group, or switch grouping off, and see
exactly what was there before.

Two rules keep this from hiding what a supervisor needs to see.

**Only a named person is ever folded.** The merge key is the author, so
an item whose source names nobody stands alone -- the same rule Phase 1
counts by, and for the same reason: two unknowns are not one person
merely because both are unknown.

**The thread is not a merge key.** It is tempting, and it is wrong. On X
the conversation id belongs to the *root* of the conversation, so a bank
care handle's own post that fifty customers reply to gives all fifty
replies one thread key. Folding by it would collapse fifty complainants
into a single entry and hide the very volume the screen exists to show.
The thread is used only to describe a group that is already one person's
-- to say "a thread of four posts" rather than "four posts".
"""


def _get(row, field, default=None):
    """Rows arrive either as sqlite3.Row or as prep_item's dict, and a
    Row raises where a dict would simply be missing the column."""
    try:
        value = row[field]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def merge_key(row):
    """What decides that two posts are the same person complaining.

    The entity is part of it: one customer unhappy with two different
    banks is two complaints to two supervisory teams, and folding them
    together would post one team's grievance under the other's name.
    """
    author = _get(row, "author_key")
    if not author:
        return None
    return (author, _get(row, "entity_id"))


def _describe(group):
    # The list as a whole is ordered by severity, which is right for a list
    # of separate complaints and meaningless inside one person's own run of
    # posts: there the sequence IS the story -- charged, no reply, branch
    # sent me to the helpline, escalating -- and it reads only in order.
    # The lead is left where it is: it holds the card's severity, and the
    # card must carry the most serious thing the person said.
    group["others"].sort(key=lambda r: (_get(r, "published_at") is None,
                                        _get(r, "published_at") or "",
                                        _get(r, "id") or 0))
    members = [group["lead"], *group["others"]]
    group["size"] = len(members)
    group["repeats"] = len(group["others"])

    threads = {_get(m, "thread_key") for m in members}
    # One thread, and every member in it: "a thread of four" is a claim
    # about all of them, so a single member without a thread key breaks it.
    group["one_thread"] = (group["size"] > 1 and len(threads) == 1
                           and None not in threads)

    dates = sorted(d for d in (_get(m, "published_at") for m in members) if d)
    group["first_at"] = dates[0] if dates else None
    group["last_at"] = dates[-1] if dates else None

    # Topics the folded posts raise that the lead card does not show. A
    # group card states the lead's own topics -- putting the others' on it
    # would attribute them to a post that never carried them -- so what
    # would otherwise disappear from view is named here instead.
    lead_topics = set(_get(group["lead"], "complaint_topics", []) or [])
    extra = []
    for other in group["others"]:
        for topic in _get(other, "complaint_topics", []) or []:
            if topic not in lead_topics and topic not in extra:
                extra.append(topic)
    group["extra_topics"] = extra
    return group


def group_complaints(rows, fold: bool = True):
    """Fold each person's repeat posts into the first of them.

    `rows` must already be in the order the screen wants. The first post
    of a group becomes its lead and the group keeps that post's place, so
    grouping never reorders the list and never buries a severe post inside
    a milder card: under the usual sort the most severe post of a group is
    the one seen first, and the one seen first is the lead.

    With `fold` off every post is its own group, which is how the screen
    shows the raw list without the template needing a second shape to
    walk -- and without the counts beside it changing meaning.
    """
    groups, index = [], {}
    for row in rows:
        key = merge_key(row) if fold else None
        if key is not None and key in index:
            index[key]["others"].append(row)
            continue
        group = {"lead": row, "others": []}
        if key is not None:
            index[key] = group
        groups.append(group)
    return [_describe(g) for g in groups]


def repeat_total(groups) -> int:
    """How many of the posts listed are a repeat from someone already on
    the screen -- the size of the duplication, in one number."""
    return sum(g["repeats"] for g in groups)


def quoted_within(rows) -> dict:
    """Which listed posts quote another post that is also listed.

    A cross-reference, never a merge: a quote tweet is usually somebody
    *else* piling on, and that is a second complainant, not a repeat.
    """
    by_uid = {}
    for row in rows:
        uid = _get(row, "source_uid")
        if uid:
            by_uid.setdefault(uid, row)
    out = {}
    for row in rows:
        quoted = _get(row, "quoted_key")
        target = by_uid.get(quoted) if quoted else None
        if target is not None and _get(target, "id") != _get(row, "id"):
            out[_get(row, "id")] = target
    return out
