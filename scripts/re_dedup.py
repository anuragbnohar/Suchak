"""Re-cluster stored items with the current similarity rules.

    python -m scripts.re_dedup                  # shows the plan, asks first
    python -m scripts.re_dedup --yes            # no prompt
    python -m scripts.re_dedup --smart          # also ask the classifier's
                                                # model what reports one event
    python -m scripts.re_dedup --smart --days 30

Items ingested before the clustering improvements (publisher suffixes,
plural folding, calibrated threshold) sit as separate rows for one event.
This pass cleans the stored titles and merges near-duplicates: the extra
outlets become additional sources on one surviving item.

--smart goes further. Word overlap clubs the first day's headlines ("CEO
resigns" in six outlets) but not the story's later angles ("shares dip
after top boss exit", "what the succession means"). The smart pass shows
the classifier's model each entity's remaining items of the last --days
days (90 by default) and merges the groups it says report one occurrence.
It needs the API key on this computer and costs about one classification
per hundred items. Newly fetched items are folded this way as they are
classified; the smart pass is for what was stored before that.

Safety rules: social posts and exchange filings never merge (volume is
signal / titles are formulaic); a reviewed item is never deleted -- it
becomes the survivor of its cluster, and a cluster containing two or more
reviewed items is left untouched and reported.
"""
import argparse
import json
from datetime import datetime, timedelta, timezone

# Runnable either as `python -m scripts.re_dedup` or `python scripts/re_dedup.py`.
# The second form puts scripts/ on the import path instead of the project
# root, so add the root here and let `app` import the same way in both.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import classify
from app.db import connect, init_db, q, x
from app.ingest import DUP_MIN_SHARED, DUP_MIN_STRONG, DUP_THRESHOLD
from app.similarity import (alias_tokens, distinctive_overlap,
                            event_similarity, strip_publisher, strong_shared)

# Printed first, so a PowerShell paste shows which copy of the script ran.
BUILD = "2026-09-04.5"

# Items per model call in the smart pass. Read in date order, so the items
# of one week land in the same call and can be grouped together.
SMART_BATCH = 100


def _drain_pending_input() -> None:
    """Discard keys pressed before the question below was shown. The smart
    pass thinks for a while, and an Enter pressed during that wait would
    otherwise answer "Apply?" -- as No -- the instant it appears."""
    try:
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getwch()
        return
    except ImportError:
        pass
    try:
        import termios
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def merge_into(db, primary: dict, victim: dict) -> None:
    """The victim becomes one more source on the primary, sources already
    attached to the victim move across, and the victim's own row goes."""
    with db:
        db.execute(
            "INSERT INTO item_sources (item_id, url, source_name, title,"
            " published_at) VALUES (?,?,?,?,?)",
            (primary["id"], victim["url"], victim["source_name"],
             victim["title"], victim["published_at"]))
        db.execute("UPDATE item_sources SET item_id = ? WHERE item_id = ?",
                   (primary["id"], victim["id"]))
        db.execute("DELETE FROM items WHERE id = ?", (victim["id"],))


def merge_cluster(db, members: list[dict], apply: bool) -> tuple[int, bool]:
    """Merge one cluster under the reviewed-item rules. Returns (items
    merged, skipped): skipped when two or more members carry a human
    judgement, which are never collapsed into one."""
    reviewed = [m for m in members if m["reviewed_at"]]
    if len(reviewed) >= 2:
        return 0, True
    primary = reviewed[0] if reviewed else members[0]
    merged = 0
    for victim in members:
        if victim["id"] == primary["id"]:
            continue
        merged += 1
        if apply:
            merge_into(db, primary, victim)
    return merged, False


def cluster_entity(db, entity, apply: bool) -> dict:
    stop = alias_tokens(json.loads(entity["aliases"]))
    rows = [dict(r) for r in q(
        db,
        "SELECT id, title, url, source_name, published_at, reviewed_at"
        " FROM items WHERE entity_id = ? AND gated_out = 0"
        "   AND source_type NOT IN ('social','filing')"
        " ORDER BY published_at, id",
        (entity["id"],),
    )]

    cleaned = 0
    for r in rows:
        clean = strip_publisher(r["title"], r["source_name"])
        if clean != r["title"]:
            cleaned += 1
            if apply:
                x(db, "UPDATE items SET title = ? WHERE id = ?", (clean, r["id"]))
            r["title"] = clean

    # membership: an item joins the cluster whose variants it best matches
    clusters = []   # {"members": [row, ...], "titles": [str, ...]}
    for r in rows:
        best, best_score = None, 0.0
        for c in clusters:
            for t in c["titles"]:
                score = event_similarity(r["title"], t, stop)
                shared = distinctive_overlap(r["title"], t, stop)
                strong = strong_shared(r["title"], t, stop)
                # same rule as the live fetch: three shared words, or two
                # that actually pin down one event
                if score > best_score and (
                        shared >= DUP_MIN_SHARED
                        or (shared >= 2 and strong >= DUP_MIN_STRONG)):
                    best, best_score = c, score
        if best is not None and best_score >= DUP_THRESHOLD:
            best["members"].append(r)
            best["titles"].append(r["title"])
        else:
            clusters.append({"members": [r], "titles": [r["title"]]})

    merged = skipped = 0
    for c in clusters:
        if len(c["members"]) < 2:
            continue
        m, s = merge_cluster(db, c["members"], apply)
        merged += m
        skipped += int(s)
    return {"cleaned": cleaned, "merged": merged, "skipped": skipped}


def run(apply: bool) -> dict:
    totals = {"cleaned": 0, "merged": 0, "skipped": 0}
    db = connect()
    try:
        for entity in q(db, "SELECT * FROM entities ORDER BY id"):
            r = cluster_entity(db, entity, apply)
            for k in totals:
                totals[k] += r[k]
    finally:
        db.close()
    return totals


# --- the smart pass ---------------------------------------------------------

def smart_rows(db, entity, days: int) -> list[dict]:
    """The entity's remaining primaries of the last `days` days, oldest
    first -- the same kinds of item the fetch-time rules may merge."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    return [dict(r) for r in q(
        db,
        "SELECT id, title, summary, url, source_name, published_at, reviewed_at"
        " FROM items WHERE entity_id = ? AND gated_out = 0"
        "   AND source_type NOT IN ('social','filing')"
        "   AND substr(COALESCE(published_at, created_at), 1, 10) >= ?"
        " ORDER BY published_at, id",
        (entity["id"], since))]


def smart_groups(db, entity, days: int) -> tuple[list[dict], int]:
    """Ask the model which of the entity's remaining items report one
    occurrence. Returns (groups, items read); each group's members are
    ordered oldest first, so the first report is the natural survivor."""
    rows = smart_rows(db, entity, days)
    groups = []
    for start in range(0, len(rows), SMART_BATCH):
        batch = rows[start:start + SMART_BATCH]
        if len(batch) < 2:
            continue
        by_id = {r["id"]: r for r in batch}
        print(f"  {entity['name']}: asking about {len(batch)} item(s) ...",
              flush=True)
        for g in classify.group_same_events(entity, batch):
            members = sorted((by_id[i] for i in g["item_ids"]),
                             key=lambda r: (r["published_at"] or "", r["id"]))
            groups.append({"entity": entity["name"], "label": g["label"],
                           "members": members})
    return groups, len(rows)


def smart_plan(db, days: int) -> tuple[list[dict], int]:
    groups, read = [], 0
    for entity in q(db, "SELECT * FROM entities ORDER BY id"):
        g, n = smart_groups(db, entity, days)
        groups += g
        read += n
    return groups, read


def apply_smart(db, groups: list[dict], apply: bool) -> dict:
    merged = skipped = 0
    for g in groups:
        m, s = merge_cluster(db, g["members"], apply)
        merged += m
        skipped += int(s)
    return {"merged": merged, "skipped": skipped}


def _confirm(yes: bool) -> bool:
    if yes:
        return True
    _drain_pending_input()
    try:
        answer = input("\nApply? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() != "y":
        print("Cancelled. Nothing changed.")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Club stored news items that report one event.")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    ap.add_argument("--smart", action="store_true",
                    help="also ask the classifier's model which remaining "
                         "items report one occurrence (needs the API key)")
    ap.add_argument("--days", type=int, default=90,
                    help="how far back --smart looks (default 90)")
    args = ap.parse_args()
    init_db()
    print(f"re_dedup build {BUILD}")

    plan = run(apply=False)
    print("Plan (word-overlap rules):")
    print(f"  titles to clean of publisher suffixes : {plan['cleaned']}")
    print(f"  duplicate items to merge into sources : {plan['merged']}")
    if plan["skipped"]:
        print(f"  clusters left alone (2+ reviewed)     : {plan['skipped']}")
    if plan["cleaned"] or plan["merged"]:
        if not _confirm(args.yes):
            return 0
        done = run(apply=True)
        print(f"\nDone: {done['cleaned']} titles cleaned, "
              f"{done['merged']} duplicates merged into their primary item.")
    else:
        print("Nothing to do by word overlap.")
    if not args.smart:
        return 0

    print(f"\nSmart pass: asking the model which items of the last "
          f"{args.days} days report one occurrence ...")
    db = connect()
    try:
        try:
            groups, read = smart_plan(db, args.days)
        except Exception as exc:
            print(f"The model could not be reached "
                  f"({type(exc).__name__}: {exc}).")
            print("Nothing changed. Set ANTHROPIC_API_KEY on this computer "
                  "and try again.")
            return 1
        if not groups:
            print(f"Read {read} item(s); the model found no two reporting "
                  "one occurrence. Nothing to do.")
            return 0
        print(f"Read {read} item(s). Groups the model says report one "
              "occurrence (* = reviewed by a person; always survives):")
        for g in groups:
            reviewed = sum(1 for m in g["members"] if m["reviewed_at"])
            flag = "   [left alone: 2+ reviewed]" if reviewed >= 2 else ""
            print(f"\n  {g['entity']} -- {g['label']}: "
                  f"{len(g['members'])} items -> 1{flag}")
            for m in g["members"]:
                mark = "*" if m["reviewed_at"] else "-"
                print(f"    {mark} [{m['id']}] {m['title'][:90]}")
        plan2 = apply_smart(db, groups, apply=False)
        print(f"\n  items to merge into their story  : {plan2['merged']}")
        if plan2["skipped"]:
            print(f"  groups left alone (2+ reviewed)  : {plan2['skipped']}")
        if not plan2["merged"]:
            print("Nothing to do.")
            return 0
        if not _confirm(args.yes):
            return 0
        done2 = apply_smart(db, groups, apply=True)
        print(f"\nDone: {done2['merged']} item(s) merged into their story.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
