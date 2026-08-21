"""Re-cluster stored items with the current similarity rules.

    python -m scripts.re_dedup            # shows the plan, asks first
    python -m scripts.re_dedup --yes      # no prompt

Items ingested before the clustering improvements (publisher suffixes,
plural folding, calibrated threshold) sit as separate rows for one event.
This pass cleans the stored titles and merges near-duplicates: the extra
outlets become additional sources on one surviving item.

Safety rules: social posts and exchange filings never merge (volume is
signal / titles are formulaic); a reviewed item is never deleted -- it
becomes the survivor of its cluster, and a cluster containing two or more
reviewed items is left untouched and reported.
"""
import argparse
import json
import sys

from app.db import connect, init_db, q, x
from app.ingest import DUP_MIN_SHARED, DUP_THRESHOLD
from app.similarity import (alias_tokens, distinctive_overlap,
                            event_similarity, strip_publisher)


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
                if score > best_score and \
                        distinctive_overlap(r["title"], t, stop) >= DUP_MIN_SHARED:
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
        reviewed = [m for m in c["members"] if m["reviewed_at"]]
        if len(reviewed) >= 2:
            skipped += 1        # two human judgments; never collapse them
            continue
        primary = reviewed[0] if reviewed else c["members"][0]
        for victim in c["members"]:
            if victim["id"] == primary["id"]:
                continue
            merged += 1
            if not apply:
                continue
            with db:
                db.execute(
                    "INSERT INTO item_sources (item_id, url, source_name, title,"
                    " published_at) VALUES (?,?,?,?,?)",
                    (primary["id"], victim["url"], victim["source_name"],
                     victim["title"], victim["published_at"]))
                db.execute("UPDATE item_sources SET item_id = ? WHERE item_id = ?",
                           (primary["id"], victim["id"]))
                db.execute("DELETE FROM items WHERE id = ?", (victim["id"],))
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = ap.parse_args()
    init_db()

    plan = run(apply=False)
    print("Plan:")
    print(f"  titles to clean of publisher suffixes : {plan['cleaned']}")
    print(f"  duplicate items to merge into sources : {plan['merged']}")
    if plan["skipped"]:
        print(f"  clusters left alone (2+ reviewed)     : {plan['skipped']}")
    if not (plan["cleaned"] or plan["merged"]):
        print("Nothing to do.")
        return 0

    if not args.yes:
        try:
            answer = input("\nApply? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "y":
            print("Cancelled. Nothing changed.")
            return 0

    done = run(apply=True)
    print(f"\nDone: {done['cleaned']} titles cleaned, "
          f"{done['merged']} duplicates merged into their primary item.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
