"""Re-run classification on items already in the database.

    python -m scripts.reclassify --entity "ICICI Bank Ltd."   # try one bank
    python -m scripts.reclassify                              # everything
    python -m scripts.reclassify --limit 50 --yes

Classification happens once, at fetch time. Items collected before an API
key was set carry keyword-rule verdicts; this re-runs them through the
configured models -- the cheap screen first, then the full verdict for
what survives -- so severity, risk areas, complaint topics and the
negative list all reflect the real classifier.

Costs money. The plan shows an estimate and waits for confirmation.
Human reviews are never touched: reviewed items are skipped by default,
and a reviewer's severity correction outranks any machine verdict anyway.
"""
import argparse
import sys
import time

from app.classify import GATE_MODEL, MODEL, classify_item
from app.db import connect, init_db, one, q, x

# $ per million tokens (input, output). Estimates only -- confirm current
# pricing before budgeting a large run.
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
VERDICT_TOKENS = (700, 400)     # measured on this app's prompt
GATE_TOKENS = (150, 30)
GATE_PASS_RATE = 0.7            # share reaching the full verdict


def _cost(model: str, tokens: tuple) -> float | None:
    price = PRICES.get(model)
    if not price:
        return None
    return tokens[0] / 1e6 * price[0] + tokens[1] / 1e6 * price[1]


def estimate(n: int) -> str:
    gate = _cost(GATE_MODEL, GATE_TOKENS) if GATE_MODEL else 0.0
    verdict = _cost(MODEL, VERDICT_TOKENS)
    if verdict is None or gate is None:
        return "unknown (unrecognised model; check pricing yourself)"
    low = n * (gate + GATE_PASS_RATE * verdict)
    high = n * (gate + verdict)
    return f"about ${low:,.2f}, at most ${high:,.2f} if nothing is screened out"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", help="limit to one entity, by exact name")
    ap.add_argument("--limit", type=int, help="stop after this many items")
    ap.add_argument("--include-reviewed", action="store_true",
                    help="also re-run items a person has reviewed")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = ap.parse_args()

    init_db()
    db = connect()
    try:
        where, params = ["1=1"], []
        if args.entity:
            ent = one(db, "SELECT id FROM entities WHERE name = ?", (args.entity,))
            if not ent:
                print(f"No entity named {args.entity!r}.", file=sys.stderr)
                return 1
            where.append("entity_id = ?")
            params.append(ent["id"])
        if not args.include_reviewed:
            where.append("reviewed_at IS NULL")

        sql = f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY id"
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        rows = q(db, sql, params)

        by_classifier = {}
        for r in rows:
            by_classifier[r["classifier"] or "unclassified"] = \
                by_classifier.get(r["classifier"] or "unclassified", 0) + 1

        print(f"Items to re-classify : {len(rows)}")
        for name, count in sorted(by_classifier.items()):
            print(f"  currently {name:<16} {count}")
        print(f"Screen model         : {GATE_MODEL or '(disabled)'}")
        print(f"Verdict model        : {MODEL}")
        print(f"Estimated cost       : {estimate(len(rows))}")
        print(f"Estimated runtime    : {len(rows) * 4 / 60:.0f}-{len(rows) * 7 / 60:.0f} minutes")
        if not args.include_reviewed:
            print("Reviewed items are skipped (--include-reviewed to include them).")
        if not rows:
            return 0

        if not args.yes:
            try:
                answer = input("\nProceed and spend that? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "y":
                print("Cancelled. Nothing re-classified, nothing spent.")
                return 0

        started, done, failed = time.monotonic(), 0, 0
        for row in rows:
            # clear any previous screening decision so the item is judged afresh
            x(db, "UPDATE items SET gated_out=0, gate_reason=NULL WHERE id=?", (row["id"],))
            try:
                classify_item(db, one(db, "SELECT * FROM items WHERE id=?", (row["id"],)))
            except Exception as exc:
                failed += 1
                print(f"  ! item {row['id']}: {type(exc).__name__}: {exc}")
            done += 1
            if done % 25 == 0 or done == len(rows):
                rate = done / max(time.monotonic() - started, 1e-6)
                left = (len(rows) - done) / rate / 60 if rate else 0
                print(f"  {done}/{len(rows)} done ({left:.0f} min left)")

        gated = one(db, "SELECT COUNT(*) n FROM items WHERE gated_out=1")["n"]
        print(f"\nRe-classified {done} item(s); {failed} failed.")
        print(f"{gated} item(s) are now screened out and sit under the "
              f"queue's Filtered out tab.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
