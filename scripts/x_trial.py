"""Run a bounded X/Twitter complaint trial for one entity.

    python -m scripts.x_trial "HDFC Bank Ltd." --handle HDFCBank_Cares
    python -m scripts.x_trial "HDFC Bank Ltd." --max 100 --yes

X bills per post returned, so this prints the worst-case cost and waits for
confirmation before spending anything. One request, never paginated: the
bill cannot exceed --max posts.
"""
import argparse
import sys

from app import ingest
from app.db import connect, init_db, one, x


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("entity", help="entity name as stored (quote it)")
    ap.add_argument("--handle", help="the bank's grievance handle, without @")
    ap.add_argument("--max", type=int, default=100,
                    help="hard ceiling on posts fetched (default 100)")
    ap.add_argument("--strategy", choices=["complaints", "care_handle", "both"],
                    help="override SUCHAK_X_STRATEGY for this run")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = ap.parse_args()

    if not ingest.X_BEARER:
        print("SUCHAK_X_BEARER is not set - nothing would be fetched.", file=sys.stderr)
        return 1
    # the trial is an explicit, bounded, confirmed run, so it does not need
    # the standing SUCHAK_X_ENABLED switch that guards the automatic sweeps
    ingest.X_ENABLED = True

    init_db()
    db = connect()
    try:
        entity = one(db, "SELECT * FROM entities WHERE name = ?", (args.entity,))
        if not entity:
            print(f"No entity named {args.entity!r}. Load banks first with "
                  f"`python -m scripts.load_banks`.", file=sys.stderr)
            return 1

        if args.handle:
            x(db, "UPDATE entities SET x_handle = ? WHERE id = ?",
              (args.handle.lstrip("@"), entity["id"]))
            entity = one(db, "SELECT * FROM entities WHERE id = ?", (entity["id"],))

        if args.strategy:
            ingest.X_STRATEGY = args.strategy
        ingest.X_MAX_POSTS = max(10, min(args.max, 1000))

        registry = ingest.load_registry(db)
        query = ingest.x_query(registry, entity)
        worst_case = ingest.X_MAX_POSTS * ingest.X_PRICE_PER_POST

        print(f"Entity   : {entity['name']}")
        print(f"Strategy : {ingest.X_STRATEGY}"
              f"{'  (handle @' + entity['x_handle'] + ')' if entity['x_handle'] else ''}")
        print(f"Window   : last {ingest.X_RECENT_SEARCH_DAYS} days "
              f"(X recent search covers no more)")
        print(f"Query    : {query}")
        print(f"Ceiling  : {ingest.X_MAX_POSTS} posts -> at most "
              f"${worst_case:.2f} at ${ingest.X_PRICE_PER_POST}/post")

        if not args.yes:
            try:
                answer = input("\nProceed and spend up to that amount? [y/N] ")
            except EOFError:      # non-interactive shell: never spend by default
                answer = ""
            if answer.strip().lower() != "y":
                print("Cancelled. Nothing fetched, nothing billed.")
                return 0

        result = ingest.ingest_entity(db, entity, registry)
        billed = result.get("billed", 0)
        print(f"\nFetched {billed} post(s) -> actual cost about "
              f"${billed * ingest.X_PRICE_PER_POST:.2f}")
        print(f"  stored as new items : {result['added']}")
        print(f"  rejected (not this entity) : {result['rejected']}")
        if result.get("note"):
            print(f"  note: {result['note']}")

        n = ingest.classify_new_items(db)
        print(f"\nClassified {n} item(s). Open the queue to review them.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
