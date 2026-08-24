"""Test the browser collector on its own: no storing, no classifying, no cost.

    python -m scripts.probe_x --login-only          # sign in once, save the session
    python -m scripts.probe_x --entity Nagpur       # probe one entity's handle
    python -m scripts.probe_x --handle HDFCBank_Cares --max 10
    python -m scripts.probe_x --entity HDFC --show-browser

A full fetch would also classify what it collects, which costs money and
hides whether the collector itself worked. This runs the collector alone and
prints what came back, so a failure is attributable: either it could not run
(and says why), or it ran and the search was genuinely empty.
"""
import argparse
import json
import os
import sys

from app.db import connect, init_db, one, q
from app import x_scrape


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", help="name or alias substring; uses that entity's handle")
    ap.add_argument("--handle", help="probe a handle directly, without an entity")
    ap.add_argument("--query", help="run this exact search string, bypassing to:handle")
    ap.add_argument("--max", type=int, default=10, help="posts to collect (default 10)")
    ap.add_argument("--login-only", action="store_true",
                    help="sign in and save the session, collect nothing")
    ap.add_argument("--show-browser", action="store_true",
                    help="watch it run; needed the first time, to clear any challenge")
    args = ap.parse_args()

    if args.show_browser:
        x_scrape.HEADLESS = False
    # The probe is explicit by nature: it runs whether or not the collector is
    # switched on for fetches, because that is the point of testing it.
    x_scrape.ENABLED = True

    print(f"build        : x_scrape {x_scrape.BUILD}")
    print(f"session file : {x_scrape.STATE_PATH}"
          f"  ({'present' if os.path.exists(x_scrape.STATE_PATH) else 'none yet'})")
    print(f"credentials  : user {'set' if x_scrape.USER else 'MISSING'},"
          f" password {'set' if x_scrape.PASS else 'MISSING'},"
          f" verify {'set' if x_scrape.VERIFY else 'unset'}")
    print(f"browser      : {'visible' if not x_scrape.HEADLESS else 'headless'}\n")

    # --login-only signs in and stops; it needs no entity and no handle, so
    # it has to be handled before either is demanded.
    if args.login_only:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("Playwright is not installed:\n"
                  "  pip install playwright\n  python -m playwright install chromium")
            return 1
        if not (x_scrape.USER and x_scrape.PASS):
            print("Set SUCHAK_X_USER and SUCHAK_X_PASS before signing in.")
            return 1
        with sync_playwright() as pw:
            browser = x_scrape._launch(pw)
            try:
                context, reused = x_scrape._new_context(browser)
                if reused:
                    print("A saved session already exists and looks usable.\n"
                          f"Delete {x_scrape.STATE_PATH} to force a fresh login.")
                else:
                    x_scrape._login(context)
                    print("Signed in. Session saved.")
            except x_scrape.ScrapeUnavailable as exc:
                print(f"COULD NOT SIGN IN: {exc}")
                return 2
            finally:
                browser.close()
        return 0

    handle = args.handle
    if args.query:
        handle = handle or "(explicit query)"
    if not handle:
        if not args.entity:
            print("Give --entity or --handle. Use --login-only to just sign in.")
            return 1
        init_db()
        db = connect()
        try:
            term = args.entity.strip().lower()
            rows = q(db, "SELECT * FROM entities ORDER BY name")
            match = [e for e in rows
                     if term in e["name"].lower()
                     or any(term in a.lower() for a in json.loads(e["aliases"]))]
            if len(match) != 1:
                print(f"{'No' if not match else 'More than one'} entity matched "
                      f"{args.entity!r}. Loaded: " + ", ".join(e["name"] for e in rows))
                return 1
            handle = (match[0]["x_handle"] or "").strip().lstrip("@")
            if not handle:
                print(f"{match[0]['name']} has no X handle. Set one on the Entities page.")
                return 1
            print(f"entity       : {match[0]['name']}")
        finally:
            db.close()

    query = args.query or f"to:{handle} -filter:retweets"
    print(f"query        : {query}\nmax posts    : {args.max}\n\ncollecting...\n")
    try:
        posts = x_scrape.scrape_query(query, args.max)
    except x_scrape.ScrapeUnavailable as exc:
        print(f"COULD NOT RUN: {exc}\n")
        print("This is the collector failing, not an empty search. Nothing was\n"
              "collected and nothing should be read into the silence.")
        return 2
    except Exception as exc:
        print(f"UNEXPECTED {type(exc).__name__}: {exc}\n")
        print("If this mentions a selector or a timeout, X has probably changed its\n"
              "markup: the selectors live in _EXTRACT in app/x_scrape.py.")
        return 3

    if not posts:
        print("Ran and found nothing. What the page actually held:\n")
        d = x_scrape.LAST_DIAGNOSIS
        if not d:
            print("  (no diagnosis captured)")
        for k in ("url", "signed_in", "articles_any", "articles_tweet",
                  "timeline_cells", "tweet_text_nodes", "login_prompt",
                  "screenshot", "note"):
            if k in d:
                print(f"  {k:16} {d[k]}")
        if d.get("body_start"):
            print(f"\n  page text: {d['body_start'][:240]}")
        print("\nReading it: signed_in False means the session is not really logged\n"
              "in. Articles present but articles_tweet zero means X changed its\n"
              "markup. Everything zero on a busy handle usually means the search\n"
              "itself returned nothing -- try --query \"to:HDFCBank_Cares\" to drop\n"
              "the retweet filter, or a plain keyword to prove search works at all.")
        return 0

    print(f"{len(posts)} post(s):\n")
    for p in posts:
        print(f"  {p['source_name']}  {p['published_at'] or 'undated'}")
        print(f"  {p['snippet'][:160]}")
        print(f"  {p['url']}\n")
    dated = sum(1 for p in posts if p["published_at"])
    print(f"{dated}/{len(posts)} carry a timestamp. Nothing was stored or classified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
