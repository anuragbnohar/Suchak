"""Make the supervised entity list exactly ROSTER -- add what is missing,
remove what is not on it.

    python -m scripts.set_roster --dry-run        # show the plan, change nothing
    python -m scripts.set_roster                  # plan, then ask before acting
    python -m scripts.set_roster --yes            # no prompt
    python -m scripts.set_roster --sync-aliases   # also align existing aliases

Editing ROSTER is how the supervised list changes. Entities already present
keep their aliases and exclude terms untouched unless --sync-aliases is
given, because those are tuned by hand on the Entities page and a script
should not quietly overwrite that work.

Removing an entity destroys its items and its review history. The plan
counts both before anything happens, and removal needs confirmation.
"""
import argparse
import json
import sys

from app.db import connect, init_db, one, q, remove_entity, x
from app.matching import Registry

# name, kind, aliases, exclude terms
#
# Aliases are chosen for precision, not recall: each one is a phrase that
# identifies this institution and nothing else. "Shriram" alone would pull
# in the group's insurance, housing-finance and properties arms; "Nagpur"
# alone would pull in a city. Exclude terms are subtracted from the search
# query, which is where sibling companies sharing a brand do the most damage.
ROSTER = [
    ("State Bank of India", "Scheduled Commercial Bank",
     ["State Bank of India", "SBI"],
     ["SBI Life", "SBI Cards", "SBI Mutual Fund", "SBI General Insurance",
      "SBI Securities"]),

    ("HDFC Bank Ltd.", "Scheduled Commercial Bank",
     ["HDFC Bank"],
     ["HDFC Life", "HDFC AMC", "HDFC Ergo", "HDFC Securities"]),

    ("ICICI Bank Ltd.", "Scheduled Commercial Bank",
     ["ICICI Bank"],
     ["ICICI Lombard", "ICICI Prudential", "ICICI Securities"]),

    # NBFC, not a bank -- listed, so exchange filings route to it as well as
    # news. "Shriram Transport Finance" is the pre-2022 name of this same
    # legal entity; drop it from the aliases if you would rather not see
    # coverage that still uses the old name.
    ("Shriram Finance Ltd.", "NBFC",
     ["Shriram Finance", "Shriram Transport Finance"],
     ["Shriram Life Insurance", "Shriram General Insurance",
      "Shriram Housing Finance", "Shriram Properties", "Shriram Pistons"]),

    # Urban cooperative bank. Expect little national press: for an entity
    # this size the signal arrives through RBI press releases -- penalties,
    # directions, supersession orders -- which the broadcast feed routes by
    # mention, not through Google News.
    ("Nagpur Nagrik Sahakari Bank Ltd.", "Urban Cooperative Bank",
     ["Nagpur Nagrik Sahakari Bank"],
     []),
]


def _plan(db):
    wanted = {name: (kind, aliases, excludes) for name, kind, aliases, excludes in ROSTER}
    present = {e["name"]: e for e in q(db, "SELECT * FROM entities ORDER BY name")}
    add = [n for n in wanted if n not in present]
    keep = [n for n in wanted if n in present]
    drop = [n for n in present if n not in wanted]
    return wanted, present, add, keep, drop


def _cost(db, entity_id):
    n = lambda sql: one(db, sql, (entity_id,))["n"]
    return (n("SELECT COUNT(*) n FROM items WHERE entity_id = ?"),
            n("SELECT COUNT(*) n FROM reviews r JOIN items i ON i.id = r.item_id"
              " WHERE i.entity_id = ?"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    ap.add_argument("--dry-run", action="store_true", help="show the plan only")
    ap.add_argument("--sync-aliases", action="store_true",
                    help="also overwrite existing entities' aliases and exclude terms")
    args = ap.parse_args()

    init_db()
    db = connect()
    try:
        wanted, present, add, keep, drop = _plan(db)

        print(f"Target roster: {len(ROSTER)} entities\n")
        for name in sorted(keep):
            kind, aliases, excludes = wanted[name]
            have = json.loads(present[name]["aliases"])
            note = ""
            if have != aliases:
                note = ("  [aliases differ: has " + ", ".join(have) +
                        ("  -- --sync-aliases would replace them]" if not args.sync_aliases
                         else "  -- will be replaced]"))
            print(f"  keep    {name}{note}")
        for name in sorted(add):
            kind, aliases, _ = wanted[name]
            print(f"  ADD     {name}  ({kind}; {', '.join(aliases)})")

        losses = 0
        for name in sorted(drop):
            items, reviews = _cost(db, present[name]["id"])
            losses += reviews
            print(f"  REMOVE  {name}  -- deletes {items} item(s) and {reviews} review(s)")

        if not add and not drop and not (args.sync_aliases and any(
                json.loads(present[n]["aliases"]) != wanted[n][1] for n in keep)):
            print("\nNothing to do -- the roster already matches.")
            return 0
        if args.dry_run:
            print("\nDry run -- nothing changed.")
            return 0
        if losses:
            print(f"\n{losses} recorded review(s) will be destroyed. "
                  "Nothing else in the app deletes a review.")
        if drop and not args.yes:
            try:
                if input("\nType DELETE to apply this plan: ").strip() != "DELETE":
                    print("Cancelled. Nothing changed.")
                    return 0
            except EOFError:
                print("Cancelled. Nothing changed.")
                return 0

        for name in add:
            kind, aliases, excludes = wanted[name]
            x(db, "INSERT INTO entities (name, kind, aliases, exclude_terms)"
                  " VALUES (?,?,?,?)",
              (name, kind, json.dumps(aliases), json.dumps(excludes)))
        if args.sync_aliases:
            for name in keep:
                kind, aliases, excludes = wanted[name]
                x(db, "UPDATE entities SET aliases = ?, exclude_terms = ? WHERE id = ?",
                  (json.dumps(aliases), json.dumps(excludes), present[name]["id"]))
        for name in drop:
            remove_entity(db, present[name]["id"])
        db.commit()
        print(f"\n{len(add)} added, {len(drop)} removed, {len(keep)} kept.")

        # Registry entries are normalized for matching and carry no kind, so
        # the roster listing reads kind from the rows themselves.
        rows = q(db, "SELECT * FROM entities ORDER BY name")
        registry = Registry(rows)
        print("\nRoster now:")
        for e in rows:
            rivals = registry.competitors_of(e["id"])
            extra = f"   (query excludes {', '.join(rivals)})" if rivals else ""
            print(f"  {e['name']:<34} {e['kind']}{extra}")
        orphans = one(db, "SELECT COUNT(*) n FROM users"
                          " WHERE entity_id IS NULL AND role IN ('lead','member')")["n"]
        if orphans:
            print(f"\n{orphans} team account(s) have no entity. Assign them with:"
                  "\n  python -m scripts.load_banks --team \"<entity name>\"")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
