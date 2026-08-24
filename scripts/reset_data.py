"""Wipe all entities and all collected items from the database.

    python -m scripts.reset_data          # shows what will go, asks first
    python -m scripts.reset_data --yes    # no prompt

Deletes: every entity (demo or real), every item with its extra sources,
reviews recorded on those items, and the fetch log. With --items-only the
entities stay and only the collected items go.

Keeps: user accounts (their team-entity link is cleared), global factors,
and every setting (severity criteria, negative list). The database will
NOT re-seed demo data on the next start.

This is permanent. Copy suchak.db somewhere first if in doubt.
"""
import argparse

# Runnable either as `python -m scripts.reset_data` or `python scripts/reset_data.py`.
# The second form puts scripts/ on the import path instead of the project
# root, so add the root here and let `app` import the same way in both.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, init_db, one, set_setting


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    ap.add_argument("--items-only", action="store_true",
                    help="clear collected items but keep the entities")
    args = ap.parse_args()

    init_db()
    db = connect()
    try:
        n = lambda sql: db.execute(sql).fetchone()[0]
        counts = {
            "entities": n("SELECT COUNT(*) FROM entities"),
            "items": n("SELECT COUNT(*) FROM items"),
            "extra sources": n("SELECT COUNT(*) FROM item_sources"),
            "reviews on items": n("SELECT COUNT(*) FROM items WHERE reviewed_at IS NOT NULL"),
            "review history rows": n("SELECT COUNT(*) FROM reviews"),
            "entity-scoped factors": n("SELECT COUNT(*) FROM factors WHERE entity_id IS NOT NULL"),
            "fetch-log rows": n("SELECT COUNT(*) FROM fetch_log"),
        }
        if args.items_only:
            counts.pop("entities")
            counts.pop("entity-scoped factors")
        print("This will permanently delete:")
        for label, count in counts.items():
            print(f"  {label:<22} {count}")
        kept = ["user accounts", "global factors", "settings"]
        if args.items_only:
            kept.insert(0, f"all {n('SELECT COUNT(*) FROM entities')} entities")
        print("Kept: " + ", ".join(kept) + ".")

        if not args.yes:
            try:
                answer = input("\nType DELETE to proceed: ")
            except EOFError:
                answer = ""
            if answer.strip() != "DELETE":
                print("Cancelled. Nothing was deleted.")
                return 0

        with db:  # one transaction: all of it or none of it
            db.execute("DELETE FROM reviews")
            db.execute("DELETE FROM item_sources")
            db.execute("DELETE FROM items")
            db.execute("DELETE FROM fetch_log")
            if not args.items_only:
                db.execute("DELETE FROM factors WHERE entity_id IS NOT NULL")
                db.execute("UPDATE users SET entity_id = NULL")
                db.execute("DELETE FROM entities")

        admin = one(db, "SELECT id FROM users WHERE role='superadmin' LIMIT 1")
        set_setting(db, "seeded", "1", admin["id"] if admin else None)
        db.execute("VACUUM")

        if args.items_only:
            kept = n("SELECT COUNT(*) FROM entities")
            print(f"\nDone. All collected items cleared; {kept} entities kept.")
            print("Press Fetch on the Entities page to collect afresh.")
            return 0

        print("\nDone. The database is empty of entities and items and will "
              "not re-seed demo data.")
        print("Next steps:")
        print("  python -m scripts.load_banks              # add the 33 SCBs")
        print('  python -m scripts.load_banks --only "SBI,HDFC,ICICI" --team "HDFC Bank Ltd."')
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
