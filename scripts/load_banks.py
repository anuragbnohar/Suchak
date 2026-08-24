"""Load India's scheduled commercial banks as regulated entities.

    python -m scripts.load_banks --yes                 # all 33
    python -m scripts.load_banks --only "SBI,HDFC,ICICI"

Aliases are chosen for precision: abbreviations that are ambiguous in news
text (BoB, TMB) are deliberately omitted, because a bad alias costs money
and fills the queue with another institution's news. Names that contain
other banks' names -- "Bank of India", "Indian Bank" -- need no special
handling: app.matching resolves those by longest match across the whole
registry, and excludes the rival names from each search query.
"""
import argparse
import json

# Runnable either as `python -m scripts.load_banks` or `python scripts/load_banks.py`.
# The second form puts scripts/ on the import path instead of the project
# root, so add the root here and let `app` import the same way in both.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, init_db, one, x
from app.matching import Registry, build_query

PUBLIC_SECTOR = [
    ("State Bank of India", ["State Bank of India", "SBI"]),
    ("Bank of Baroda", ["Bank of Baroda"]),
    ("Bank of India", ["Bank of India"]),
    ("Bank of Maharashtra", ["Bank of Maharashtra"]),
    ("Canara Bank", ["Canara Bank"]),
    ("Central Bank of India", ["Central Bank of India"]),
    ("Indian Bank", ["Indian Bank"]),
    ("Indian Overseas Bank", ["Indian Overseas Bank", "IOB"]),
    ("Punjab National Bank", ["Punjab National Bank", "PNB"]),
    ("Punjab & Sind Bank", ["Punjab & Sind Bank"]),
    ("UCO Bank", ["UCO Bank"]),
    ("Union Bank of India", ["Union Bank of India"]),
]

PRIVATE_SECTOR = [
    ("Axis Bank Ltd.", ["Axis Bank"]),
    ("Bandhan Bank Ltd.", ["Bandhan Bank"]),
    ("CSB Bank Ltd.", ["CSB Bank", "Catholic Syrian Bank"]),
    ("City Union Bank Ltd.", ["City Union Bank"]),
    ("DCB Bank Ltd.", ["DCB Bank"]),
    ("Dhanlaxmi Bank Ltd.", ["Dhanlaxmi Bank"]),
    ("Federal Bank Ltd.", ["Federal Bank"]),
    ("HDFC Bank Ltd.", ["HDFC Bank"]),
    ("ICICI Bank Ltd.", ["ICICI Bank"]),
    ("IDBI Bank Ltd.", ["IDBI Bank"]),
    ("IDFC FIRST Bank Ltd.", ["IDFC FIRST Bank", "IDFC First"]),
    ("IndusInd Bank Ltd.", ["IndusInd Bank"]),
    ("Jammu & Kashmir Bank Ltd.", ["Jammu & Kashmir Bank", "J&K Bank"]),
    ("Karnataka Bank Ltd.", ["Karnataka Bank"]),
    ("Karur Vysya Bank Ltd.", ["Karur Vysya Bank"]),
    ("Kotak Mahindra Bank Ltd.", ["Kotak Mahindra Bank"]),
    ("Nainital Bank Ltd.", ["Nainital Bank"]),
    ("RBL Bank Ltd.", ["RBL Bank"]),
    ("South Indian Bank Ltd.", ["South Indian Bank"]),
    ("Tamilnad Mercantile Bank Ltd.", ["Tamilnad Mercantile Bank"]),
    ("YES Bank Ltd.", ["YES Bank"]),
]

# Phrases that disqualify an item outright -- the manual lever for cases the
# automatic rules cannot see. Keep this list short and specific.
EXCLUDE_TERMS = {
    "Federal Bank Ltd.": ["Federal Reserve"],
}

KIND = "Scheduled Commercial Bank"


def _selected(only: list[str] | None):
    """Banks matching any of the given terms, against name or alias, so
    "SBI,HDFC,ICICI" is enough -- no need for exact legal names."""
    everything = PUBLIC_SECTOR + PRIVATE_SECTOR
    if not only:
        return everything
    terms = [t.strip().lower() for t in only if t.strip()]
    picked = []
    for name, aliases in everything:
        hay = " | ".join([name] + aliases).lower()
        if any(t in hay for t in terms):
            picked.append((name, aliases))
    return picked


def main(dry_run: bool = False, team: str | None = None,
         only: list[str] | None = None) -> None:
    init_db()
    db = connect()
    try:
        selection = _selected(only)
        if only:
            if not selection:
                print(f"No bank matched {only!r}. Nothing loaded.")
                return
            print("Loading only:")
            for name, _ in selection:
                print(f"  {name}")
            print()
        added = skipped = 0
        for name, aliases in selection:
            if one(db, "SELECT 1 FROM entities WHERE name = ?", (name,)):
                skipped += 1
                continue
            if dry_run:
                added += 1
                continue
            x(db, "INSERT INTO entities (name, kind, aliases, exclude_terms)"
                  " VALUES (?,?,?,?)",
              (name, KIND, json.dumps(aliases),
               json.dumps(EXCLUDE_TERMS.get(name, []))))
            added += 1
        print(f"{added} banks added, {skipped} already present")

        registry = Registry(db.execute("SELECT * FROM entities").fetchall())
        ambiguous = [
            (e["name"], registry.competitors_of(eid))
            for eid, e in registry.entities.items()
            if registry.competitors_of(eid)
        ]
        if ambiguous:
            print("\nNames that contain another entity's name "
                  "(rivals excluded from each query automatically):")
            for name, rivals in sorted(ambiguous):
                print(f"  {name:<28} excludes {', '.join(rivals)}")

        if team:
            target = one(db, "SELECT id, name FROM entities WHERE name = ?", (team,))
            if not target:
                print(f"\nNo entity named {team!r} -- team accounts unchanged.")
            else:
                x(db, "UPDATE users SET entity_id = ? WHERE role IN ('lead','member')"
                      " AND entity_id IS NULL", (target["id"],))
                print(f"\nTeam accounts (lead/member) without an entity now "
                      f"supervise {target['name']}.")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="comma-separated names or aliases to load")
    ap.add_argument("--team", help="entity the lead/member accounts supervise")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be added, write nothing")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = ap.parse_args()

    # This script adds banks to whatever roster already exists; it never
    # removes. On a deliberately small roster that is still destructive in
    # effect -- 30-odd entities appear alongside the curated ones and each
    # has to be deleted by hand. So it confirms first, like set_roster and
    # reset_data do, rather than running on a stray invocation.
    if not args.dry_run and not args.yes:
        init_db()
        db = connect()
        try:
            existing = one(db, "SELECT COUNT(*) AS n FROM entities")["n"]
        finally:
            db.close()
        picked = _selected(args.only.split(",") if args.only else None)
        print(f"About to add up to {len(picked)} banks to a roster that "
              f"currently holds {existing}.")
        print("Banks already present by name are skipped; nothing is removed.")
        try:
            answer = input("Type 'yes' to continue: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("Cancelled. Nothing loaded.")
            sys.exit(1)

    main(dry_run=args.dry_run, team=args.team,
         only=args.only.split(",") if args.only else None)
