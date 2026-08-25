"""Delete an entity's social-media items so the next fetch starts clean.

    python -m scripts.purge_social --entity HDFC
    python -m scripts.purge_social --all --yes
    python -m scripts.purge_social --entity HDFC --include-reviewed

Exists because the first social fetch ran before dates were handled
honestly: undated old complaints were stored wearing their fetch time, so
they cannot be told apart from genuinely recent ones after the fact. The
clean recovery is to remove the stored social items and fetch again with
the collectors that now refuse undated posts.

Items a human has reviewed are kept unless --include-reviewed is given:
a recorded review is the one thing in this app nothing deletes casually.
News, videos and filings are never touched.
"""
import argparse

# Runnable either as `python -m scripts.purge_social` or
# `python scripts/purge_social.py`. The second form puts scripts/ on the
# import path instead of the project root, so add the root here.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, init_db, one, q, x


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--entity", help="name or alias substring")
    ap.add_argument("--all", action="store_true", help="every entity")
    ap.add_argument("--include-reviewed", action="store_true",
                    help="also delete social items a human has reviewed,"
                         " together with their review history")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = ap.parse_args()

    if bool(args.entity) == bool(args.all):
        ap.error("give --entity <name> or --all (exactly one)")

    init_db()
    db = connect()
    try:
        rows = q(db, "SELECT * FROM entities ORDER BY name")
        if args.all:
            targets = list(rows)
        else:
            needle = args.entity.lower()
            targets = [r for r in rows
                       if needle in (r["name"] + " " + (r["aliases"] or "")).lower()]
            if not targets:
                print(f"No entity matched {args.entity!r}. On the roster:")
                for r in rows:
                    print(f"  {r['name']}")
                return 1

        plan = []
        for ent in targets:
            total = one(db, "SELECT COUNT(*) AS n FROM items"
                            " WHERE entity_id=? AND source_type='social'",
                        (ent["id"],))["n"]
            reviewed = one(db, "SELECT COUNT(*) AS n FROM items"
                               " WHERE entity_id=? AND source_type='social'"
                               " AND (status='reviewed' OR EXISTS"
                               "  (SELECT 1 FROM reviews v WHERE v.item_id=items.id))",
                           (ent["id"],))["n"]
            doomed = total if args.include_reviewed else total - reviewed
            plan.append((ent, total, reviewed, doomed))

        print("Social items (news, videos and filings are never touched):")
        for ent, total, reviewed, doomed in plan:
            keep = "" if args.include_reviewed else f", keeping {reviewed} reviewed"
            print(f"  {ent['name']:<36} {total:>4} stored -> delete {doomed}{keep}")
        if args.include_reviewed:
            lost = sum(r for _, _, r, _ in plan)
            if lost:
                print(f"\n--include-reviewed: {lost} reviewed item(s) and their"
                      " review history will be destroyed too.")
        if not any(d for _, _, _, d in plan):
            print("\nNothing to delete.")
            return 0

        if not args.yes:
            try:
                answer = input("\nType DELETE to proceed: ")
            except EOFError:
                answer = ""
            if answer.strip() != "DELETE":
                print("Cancelled. Nothing was deleted.")
                return 0

        for ent, _, _, doomed in plan:
            if not doomed:
                continue
            guard = ("" if args.include_reviewed else
                     " AND status != 'reviewed' AND NOT EXISTS"
                     " (SELECT 1 FROM reviews v WHERE v.item_id=items.id)")
            ids = [r["id"] for r in q(
                db, "SELECT id FROM items WHERE entity_id=?"
                    f" AND source_type='social'{guard}", (ent["id"],))]
            marks = ",".join("?" * len(ids))
            with db:
                if args.include_reviewed:
                    db.execute(f"DELETE FROM reviews WHERE item_id IN ({marks})", ids)
                db.execute(f"DELETE FROM item_sources WHERE item_id IN ({marks})", ids)
                db.execute(f"DELETE FROM items WHERE id IN ({marks})", ids)
            print(f"  {ent['name']}: {len(ids)} deleted")

        print("\nDone. Fetch again from the Entities page -- the collectors"
              " now keep only dated posts from the last year.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
