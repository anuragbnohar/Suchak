"""Load India's scheduled commercial banks as regulated entities.

    python -m scripts.load_banks

Aliases are chosen for precision: abbreviations that are ambiguous in news
text (BoB, TMB) are deliberately omitted, because a bad alias costs money
and fills the queue with another institution's news. Names that contain
other banks' names -- "Bank of India", "Indian Bank" -- need no special
handling: app.matching resolves those by longest match across the whole
registry, and excludes the rival names from each search query.
"""
import json
import sys

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


def main(dry_run: bool = False) -> None:
    init_db()
    db = connect()
    try:
        added = skipped = 0
        for name, aliases in PUBLIC_SECTOR + PRIVATE_SECTOR:
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
    finally:
        db.close()


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
