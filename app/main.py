"""Drishti (formerly Suchak) — supervisory intelligence prototype.

FastAPI app and routes. The SUCHAK_* environment variables and the
suchak.db database file keep their historical names on purpose, so an
existing installation keeps its data and keys across the rename."""
import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
from urllib.parse import quote
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import (forums, geography, hq_lookup, insights as insights_mod,
               reddit_source, taxonomy, tuning, x_scrape)
from .matching import derive_aliases, place_mentions
from .auth import (get_user, hash_password, is_guest, require_login,
                   require_role, verify_password)
from .classify import (classify_item,
                       DEFAULT_EXCLUSION_RULES, DEFAULT_GRIEVANCE_SEVERITY,
                       DEFAULT_RISK_DEFS, DEFAULT_SEVERITY_DEFS,
                       EXCLUSION_RULES_KEY, GRIEVANCE_SEVERITY_KEY,
                       RISK_DEFS_KEY, SEVERITY_DEFS_KEY,
                       recheck_factors, rescore_grievances, similar_reviewed,
                      suggest_action)
from .db import (connect, get_setting, init_db, one, q, remove_entity,
                 set_setting, x)
from .ingest import (CHANNELS, LOOKBACK_CHOICES, X_LOOKBACK_CHOICES, LOOKBACK_DAYS, NEWS_EDITIONS, SOCIAL_LOOKBACK_DAYS,
                     X_BEARER, X_ENABLED, X_MAX_POSTS, X_PRICE_PER_POST,
                     run_cycle)
from .seed import seed_if_empty
from .trust import (DEFAULT_TRUSTED_SOURCES, TRUSTED_SOURCES_KEY,
                    recompute_source_tiers)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("suchak")

# Fetching is MANUAL by default: items arrive only when someone presses
# Fetch. A background sweep would fetch every loaded entity on a timer and
# bill for it unattended, which is the wrong default for a paid pipeline.
# Set SUCHAK_FETCH_MINUTES to a positive number to enable the sweep.
FETCH_MINUTES = int(os.environ.get("SUCHAK_FETCH_MINUTES", "0"))
BASE_DIR = Path(__file__).resolve().parent

_bg_tasks: set = set()


# Completion notices for background fetches. In-memory on purpose: one
# process serves the app, and the browser only needs to hear about jobs
# this process started; a restart simply forgets them and the page's
# poller drops unknown ids silently.
FETCH_JOBS: dict[str, dict] = {}
FETCH_JOBS_MAX = 100


def _fetch_job(job_id: str, entity_id: int | None, days: int | None,
               channel: str) -> None:
    job = FETCH_JOBS.get(job_id)
    if job is None:
        return
    try:
        result = run_cycle(entity_id, days, channel)
        if result.get("skipped"):
            job.update(state="failed",
                       note="another fetch was already running — try again "
                            "in a minute")
            return
        bits = [f"{result.get('added', 0)} new item(s)"]
        if result.get("rejected"):
            bits.append(f"{result['rejected']} rejected (see the Rejected tab)")
        if result.get("classified"):
            bits.append(f"{result['classified']} classified")
        if result.get("folded"):
            bits.append(f"{result['folded']} folded into stories already "
                        "on the queue")
        job.update(state="done", note=", ".join(bits))
    except Exception as exc:
        log.exception("Fetch job %s failed", job_id)
        job.update(state="failed", note=f"{type(exc).__name__}: {exc}")


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _periodic_fetch() -> None:
    while True:
        await asyncio.sleep(FETCH_MINUTES * 60)
        try:
            await asyncio.to_thread(run_cycle)
        except Exception:
            log.exception("periodic fetch cycle failed")


# The demo roster, as published in the README of a public repository. A
# copy on the open internet where any of these still opens the door is
# not a copy with weak passwords -- it is a copy with no passwords.
DEMO_LOGINS = {"admin": "admin123", "priya": "priya123", "rahul": "rahul123"}
# What each demo account is for, printed beside it on the sign-in screen.
DEMO_ROLES = {
    "admin": "Super admin — all entities",
    "priya": "Team lead — HDFC Bank",
    "rahul": "Team member — HDFC Bank",
}


def _demo_passwords_that_still_work(db) -> list[str]:
    open_doors = []
    for username, password in DEMO_LOGINS.items():
        row = one(db, "SELECT password_hash FROM users WHERE username = ?"
                      " AND COALESCE(disabled, 0) = 0", (username,))
        if row and verify_password(password, row["password_hash"]):
            open_doors.append(username)
    return open_doors


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = connect()
    try:
        # The demo roster exists so a fresh install has something to show.
        # On a public address it would be three documented passwords, so a
        # public copy starts empty and its first account is made by hand.
        if not PUBLIC_MODE and seed_if_empty(db):
            log.info("Seeded demo entities, users, alerts and items")
        if PUBLIC_MODE:
            still = _demo_passwords_that_still_work(db)
            if still:
                raise RuntimeError(
                    "This copy is public, but "
                    + ", ".join(sorted(still))
                    + " can still be signed in to with the password printed "
                      "in the README. Start it without SUCHAK_PUBLIC, sign "
                      "in, change those passwords under Account, and remove "
                      "the accounts you do not need under Settings. Then "
                      "start it public again.")
        changed = recompute_source_tiers(db)
        if changed:
            log.info("Source trust tiers set on %d item(s)", changed)
    finally:
        db.close()
    if FETCH_MINUTES > 0:
        _spawn(_periodic_fetch())
        log.info("Background fetch every %s minutes", FETCH_MINUTES)
    else:
        log.info("Manual fetching only - use the Fetch buttons on the "
                 "Entities page (set SUCHAK_FETCH_MINUTES to enable a sweep)")
    yield


# A copy reachable from the internet rather than only from one laptop.
# Not a cosmetic flag: it refuses the conveniences that are harmless at a
# desk and dangerous on a public address -- a throwaway session secret, a
# demo roster, cookies that would travel unencrypted.
PUBLIC_MODE = os.environ.get("SUCHAK_PUBLIC", "").strip().lower() in (
    "1", "true", "yes")
_SECRET = os.environ.get("SUCHAK_SECRET", "").strip()
if PUBLIC_MODE and len(_SECRET) < 32:
    raise RuntimeError(
        "SUCHAK_PUBLIC is on, so SUCHAK_SECRET must be set to at least 32 "
        "characters of your own. Without it the app invents a new secret "
        "each time it starts, which signs everybody out on every restart "
        "and cannot be shared between processes. Generate one with:\n"
        "    python -c \"import secrets; print(secrets.token_hex(32))\"")

app = FastAPI(title="Drishti", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=_SECRET or secrets.token_hex(32),
    same_site="lax",
    # on a real address the sign-in cookie must never travel unencrypted
    https_only=PUBLIC_MODE,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Shown in every page's footer. Stale local files have now cost four
# debugging rounds -- the fix on GitHub, the report from an old copy on
# disk -- so the running build identifies itself where a screenshot
# always includes it. Bump on every user-visible change.
APP_BUILD = "2026-09-10.65"

# Templates load once, at startup, like the Python code. With live
# reloading, extracting an update ZIP over a RUNNING app served new
# screens against old code and crashed; now both change together, on
# restart, the way the update instructions say.
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.auto_reload = False
templates.env.globals["app_build"] = APP_BUILD

# Datalist suggestions wherever an office is typed; the jurisdictions
# themselves live in app/geography.py. The field stays free text -- a
# custom office simply defines its region as its own name.
RBI_OFFICES = sorted(geography.OFFICE_STATES)
templates.env.globals["rbi_offices"] = RBI_OFFICES
UNASSIGNED = "Unassigned"


def _timeago(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return iso
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 3600:
        return f"{max(1, secs // 60)}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 14 * 86400:
        return f"{secs // 86400}d ago"
    return dt.strftime("%d %b %Y")


templates.env.filters["timeago"] = _timeago


# --- the date window shared by every screen ---------------------------------

# Supervision reads the whole record by default -- a fraud case four months
# old is exactly what belongs on screen -- but every screen can be narrowed
# to a period. The wording is the calendar's, not the database's: an officer
# asks for "yesterday", never for a timestamp range.
DATE_WINDOWS = (
    ("", "All dates"),
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("7", "Last 7 days"),
    ("30", "Last 30 days"),
    ("90", "Last 90 days"),
    ("365", "Last 365 days"),
)
DATE_WINDOW_LABELS = dict(DATE_WINDOWS)
templates.env.globals["date_windows"] = DATE_WINDOWS


def date_window(request: Request) -> dict:
    """The period chosen in a screen's date dropdown.

    Returns the key (to re-select it in the dropdown and carry it on every
    link out of the page) and the bounds as plain YYYY-MM-DD strings. Dates
    are stored ISO-first -- '2026-09-07T09:15:00+00:00' from a feed,
    '2026-09-07 09:15:00' from SQLite's own clock -- so a date string
    compares correctly against either, character by character.

    'Last 7 days' counts back seven days from today, the same reckoning the
    fetch windows use, so a 7-day fetch and a 7-day filter agree.
    """
    key = request.query_params.get("since", "")
    if key not in DATE_WINDOW_LABELS:
        key = ""
    today = datetime.now(timezone.utc).date()
    start = end = None                     # end is exclusive
    if key == "today":
        start = today
    elif key == "yesterday":
        start, end = today - timedelta(days=1), today
    elif key:
        start = today - timedelta(days=int(key))
    return {"key": key, "label": DATE_WINDOW_LABELS[key],
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else ""}


def date_sql(win: dict, alias: str = "") -> tuple[str, list]:
    """A WHERE fragment restricting an items row to the chosen window.

    An item with no publication date falls back to when it was collected,
    so a forum post whose site gave no timestamp is not silently dropped
    from every dated view. Returns ('', []) when no window is chosen.
    """
    if not win["key"]:
        return "", []
    p = f"{alias}." if alias else ""
    col = f"COALESCE(NULLIF({p}published_at, ''), {p}created_at)"
    frag, args = f"{col} >= ?", [win["start"]]
    if win["end"]:
        frag += f" AND {col} < ?"
        args.append(win["end"])
    return f"({frag})", args


@app.exception_handler(StarletteHTTPException)
async def http_error_page(request: Request, exc: StarletteHTTPException):
    """Refusals as a page somebody can read and leave, not raw JSON.

    require_login signals "go and sign in" as a 303 carrying a Location,
    so anything with a redirect status is passed straight through -- it is
    a direction, not an error.
    """
    if exc.status_code in (301, 302, 303, 307, 308):
        where = (exc.headers or {}).get("Location", "/login")
        return RedirectResponse(where, status_code=exc.status_code)
    if "text/html" not in (request.headers.get("accept") or ""):
        return JSONResponse({"detail": exc.detail},
                            status_code=exc.status_code,
                            headers=dict(exc.headers or {}))
    db = connect()
    try:
        user = get_user(db, request)
        return templates.TemplateResponse(
            request, "error.html",
            {"user": user, "guest": is_guest(user),
             "status": exc.status_code, "detail": exc.detail,
             "heading": {400: "That could not be done",
                         403: "Not permitted",
                         404: "Not found",
                         405: "Not permitted"}.get(exc.status_code,
                                                   "Something went wrong")},
            status_code=exc.status_code)
    finally:
        db.close()


def render(request: Request, name: str, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx["taxonomy"] = taxonomy
    ctx["request"] = request
    # Every page gets the chosen window and the query-string tail that
    # carries it, so a drill-down out of a windowed screen stays windowed.
    win = ctx.get("win") or date_window(request)
    ctx["win"] = win
    ctx["since_qs"] = f"&since={win['key']}" if win["key"] else ""
    ctx["guest"] = is_guest(ctx.get("user"))
    if ctx.get("user") is not None and "todo_count" not in ctx:
        ctx["todo_count"] = _open_action_count(ctx["user"])
    return templates.TemplateResponse(request, name, ctx)


def _present_kinds(entities) -> list[str]:
    """Distinct entity types actually stored, in the roster's order, with
    any legacy values after, alphabetically."""
    order = {k: i for i, k in enumerate(taxonomy.ENTITY_KINDS)}
    return sorted({e["kind"] for e in entities if e["kind"]},
                  key=lambda k: (order.get(k, len(order)), k))


def complainant_count(rows) -> tuple[int, int]:
    """How many distinct people are behind these items, and how many of
    those items name a person at all.

    Posts are not complaints: one customer writing five times about the
    same refund is one aggrieved customer, and counting the posts makes a
    trend out of one person's persistence. Where a source names nobody --
    a news article, a forum post with no byline -- the item counts as its
    own complainant: two unknowns are never folded together merely because
    both are unknown. So this figure can overstate the number of people
    and never understate it, which is the safe direction for a supervisor,
    and the second number says how much of it is actually attributed.
    """
    known = {r["author_key"] for r in rows if r.get("author_key")}
    named = sum(1 for r in rows if r.get("author_key"))
    return len(known) + (len(rows) - named), named


def prep_item(row) -> dict:
    d = dict(row)
    for field in ("risk_areas", "factor_matches", "complaint_topics",
                  "relationships", "review_risk_areas"):
        if field in d:
            try:
                d[field] = json.loads(d[field] or "[]")
            except (TypeError, ValueError):
                d[field] = []
    d["actionability_label"] = taxonomy.ACTIONABILITY_LABELS.get(
        d.get("actionability") or "", d.get("actionability") or "")
    d["source_type_label"] = taxonomy.SOURCE_TYPE_LABELS.get(d.get("source_type") or "news")
    # A reviewer's correction of the complaint categories wins everywhere
    # topics are read -- tiles, filters, Insights -- while the
    # classifier's own list stays visible on the item page. NULL means no
    # ruling; a stored empty list means "ruled not a grievance".
    d["complaint_topics_model"] = d.get("complaint_topics") or []
    raw_rct = d.get("review_complaint_topics")
    try:
        d["review_complaint_topics"] = (json.loads(raw_rct)
                                        if raw_rct is not None else None)
    except (TypeError, ValueError):
        d["review_complaint_topics"] = None
    if d["review_complaint_topics"] is not None:
        d["complaint_topics"] = d["review_complaint_topics"]
    # a reviewer's correction wins over the classifier's verdict, for the
    # risk areas exactly as for the severity
    d["severity_shown"] = d.get("review_severity") or d.get("severity") or "low"
    d["risk_areas_shown"] = d.get("review_risk_areas") or d.get("risk_areas") or []
    # an item still awaiting classification has no verdict to show; callers
    # that group by category must exclude it rather than file it under "low"
    d["classified"] = d.get("status") != "new"
    return d


def entity_offices(e) -> list[str]:
    """An entity's RBI office(s). Stored comma-separated because a bank
    in a two-office state (Kerala, Andhra Pradesh, UP...) may be mapped
    to both."""
    return [o.strip() for o in (e["rbi_office"] or "").split(",") if o.strip()]


def visible_entities(db, user) -> list:
    if user["role"] == "superadmin":
        return q(db, "SELECT * FROM entities ORDER BY name")
    if user["rbi_office"]:
        # A Regional Director's beat is an office, not one entity: every
        # entity headquartered in that region is theirs to monitor, across
        # SSM teams. This one branch scopes every page for them. Membership
        # is checked in Python because an entity may carry two offices.
        return [e for e in q(db, "SELECT * FROM entities ORDER BY name")
                if user["rbi_office"] in entity_offices(e)]
    if user["entity_id"] is None:
        # "Every entity" on the add-account form stores no entity at all.
        # Read as "not scoped to one", which is what the form promises.
        # Read the other way -- scoped to none -- it made the account
        # useless: WHERE id = NULL matches nothing, so every page refused
        # it with "No entities configured". No seeded account had a null
        # entity, which is why nobody met this until an account was made
        # by hand.
        return q(db, "SELECT * FROM entities ORDER BY name")
    return q(db, "SELECT * FROM entities WHERE id = ?", (user["entity_id"],))


def resolve_entity(db, user, requested: str | None,
                   office: str | None = None):
    """The entity a page is scoped to, or None for "every entity I can see".

    Only the super admin has a cross-entity scope, and it exists so the
    severity and risk views can hand a total somewhere to open: a count of
    high-severity items across the portfolio has no single entity behind it.
    `office` narrows the visible set to one RBI office's entities -- the
    RD View's Queue and Social media sub-pages.
    """
    entities = visible_entities(db, user)
    if office:
        if user["role"] != "superadmin" and (user["rbi_office"] or "") != office:
            raise HTTPException(403, "That office is not visible to your role")
        entities = [e for e in entities if office in entity_offices(e)]
        if not entities:
            raise HTTPException(404, f"No entities under the {office} office")
    if not entities:
        raise HTTPException(404, "No entities configured")
    if requested == "all":
        if user["role"] != "superadmin":
            raise HTTPException(403, "Cross-entity view is for the super admin")
        return None, entities
    if requested:
        for e in entities:
            if str(e["id"]) == str(requested):
                return e, entities
        raise HTTPException(403, "Entity not visible to your role")
    return entities[0], entities


# --- auth -------------------------------------------------------------------

# Sign-in throttling. The prototype's login counted nothing and waited for
# nothing, which was honest enough while the only way to reach it was to be
# sitting at the laptop. On a public address a wrong guess has to cost
# something or there is no limit on how many can be tried.
#
# The counter is per name AND per address, deliberately. Per name alone
# would let anyone on the internet lock a colleague out of their own
# account by failing eight times on purpose; scoped this way, an attacker
# only ever throttles themselves.
LOGIN_WINDOW_MINUTES = 15
LOGIN_MAX_FAILURES = 8
# Every wrong password waits before answering. A person notices a second;
# a program working through a word list finds it ruinous.
LOGIN_FAILURE_PAUSE = 1.0


def _demo_panel() -> list[dict]:
    """The demo logins worth printing on the sign-in screen: the ones that
    still genuinely open the door, and no others.

    Printing a credential somebody has since changed is worse than printing
    nothing. It is untrue, and it still hands a stranger the account names
    that exist -- which was harmless on a laptop nobody could reach and is
    not harmless on a public address.

    On a public copy the answer is always none, and it is returned without
    looking: startup has already refused to run while any demo password
    worked, and verifying three password hashes is real work on a page
    anyone may load as often as they like.

    Opens its own connection, and gives up quietly if the database cannot
    answer. A decorative panel must never be the reason a sign-in screen
    fails to render -- a database in trouble is the moment somebody most
    needs to get in and look.
    """
    if PUBLIC_MODE:
        return []
    try:
        db = connect()
        try:
            names = _demo_passwords_that_still_work(db)
        finally:
            db.close()
    except sqlite3.Error:
        return []
    return [{"username": name,
             "password": DEMO_LOGINS[name],
             "role": DEMO_ROLES.get(name, "")}
            for name in names]


def client_ip(request: Request) -> str:
    """The visitor's address, so far as it can be known.

    Behind the tunnel every request arrives from localhost, so the socket
    address says nothing about who called. Cloudflare stamps the real one
    on at its edge and cloudflared passes it through. On a laptop no such
    header exists and the socket address is the honest answer.
    """
    stamped = (request.headers.get("cf-connecting-ip") or "").strip()
    if stamped:
        return stamped[:64]
    return (request.client.host if request.client else "") or "unknown"


def _login_failures_since(db, username: str, ip: str) -> int:
    since = (datetime.now(timezone.utc)
             - timedelta(minutes=LOGIN_WINDOW_MINUTES)).isoformat()
    row = one(db, "SELECT COUNT(*) AS n FROM login_failures"
                  " WHERE username = ? AND ip = ? AND at >= ?",
              (username, ip, since))
    return row["n"] if row else 0


def _record_login_failure(db, username: str, ip: str) -> None:
    now = datetime.now(timezone.utc)
    x(db, "INSERT INTO login_failures (username, ip, at) VALUES (?,?,?)",
      (username, ip, now.isoformat()))
    # Pruned on write rather than on a timer: the table only grows when
    # somebody is failing to sign in, so that is the moment to tidy it.
    x(db, "DELETE FROM login_failures WHERE at < ?",
      ((now - timedelta(days=30)).isoformat(),))


def _clear_login_failures(db, username: str, ip: str) -> None:
    x(db, "DELETE FROM login_failures WHERE username = ? AND ip = ?",
      (username, ip))


def recent_login_failures(db, hours: int = 24) -> dict:
    """What the door has been hearing lately, for the Settings page."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    row = one(db, "SELECT COUNT(*) AS tries,"
                  " COUNT(DISTINCT ip) AS places,"
                  " COUNT(DISTINCT username) AS names"
                  " FROM login_failures WHERE at >= ?", (since,))
    return {"tries": row["tries"] if row else 0,
            "places": row["places"] if row else 0,
            "names": row["names"] if row else 0,
            "hours": hours}


@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html", error=None, demo=_demo_panel())


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip().lower()
    password = form.get("password") or ""
    ip = client_ip(request)
    fault = None
    demo = _demo_panel()
    db = connect()
    try:
        if _login_failures_since(db, username, ip) >= LOGIN_MAX_FAILURES:
            # Refused before the password is even looked at. The message
            # says the same thing whether or not the name exists, so it
            # cannot be used to find out which accounts are real.
            return render(request, "login.html", demo=demo,
                          error="Too many failed attempts from here. Wait "
                                f"{LOGIN_WINDOW_MINUTES} minutes and try again.")
        user = one(db, "SELECT * FROM users WHERE username = ?"
                       " AND COALESCE(disabled, 0) = 0", (username,))
        if not user or not verify_password(password, user["password_hash"]):
            _record_login_failure(db, username, ip)
            fault = "Invalid username or password."
        else:
            _clear_login_failures(db, username, ip)
            request.session["uid"] = user["id"]
    finally:
        db.close()
    if fault:
        # Waited after the connection is closed, not while holding it, and
        # with asyncio.sleep rather than time.sleep -- the latter would
        # stop the whole app serving for a second, which is a denial of
        # service anyone could trigger by guessing wrongly.
        await asyncio.sleep(LOGIN_FAILURE_PAUSE)
        return render(request, "login.html", demo=demo, error=fault)
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def home(request: Request):
    db = connect()
    try:
        user = require_login(db, request)
    finally:
        db.close()
    return RedirectResponse(
        "/overview" if user["role"] == "superadmin" else "/queue", status_code=303)


# --- review queue -----------------------------------------------------------

@app.get("/queue")
def queue(request: Request):
    db = connect()
    try:
        user = require_login(db, request)
        entity, entities = resolve_entity(db, user, request.query_params.get("entity"),
                                          office=request.query_params.get("office") or None)
        status = request.query_params.get("status", "open")
        risk = request.query_params.get("risk", "")
        sev = request.query_params.get("sev", "")
        if sev not in taxonomy.SEVERITIES:
            sev = ""
        factor = (request.query_params.get("factor") or "")[:80]
        org = (request.query_params.get("org") or "")[:120]
        src = request.query_params.get("src", "")
        if src != "trusted":
            src = ""
        complaints = request.query_params.get("complaints", "") == "1"
        topic = request.query_params.get("topic", "")
        if topic not in taxonomy.COMPLAINT_TOPICS:
            topic = ""
        on_day = request.query_params.get("on", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", on_day or ""):
            on_day = ""
        # The default is still the whole record -- a fraud case four months
        # old is exactly what belongs on screen -- but the Date dropdown
        # narrows it when the question is "what came in this week".
        # A single day is also selectable by clicking the dashboard's
        # activity chart, which is a drill-down, not a filter.
        win = date_window(request)

        if entity is None:
            ids = [e["id"] for e in entities]
            where = [f"i.entity_id IN ({','.join('?' * len(ids))})"]
            params = list(ids)
        else:
            where, params = ["i.entity_id = ?"], [entity["id"]]
        # Social complaints are a workstream of their own: they are reviewed
        # from the Social media tab, and volume is their signal. Fifty forum
        # complaints would otherwise bury the day's news in every tab here.
        where.append("i.source_type != 'social'")
        if status == "open":
            where.append("i.status IN ('new','classified') AND i.gated_out = 0")
        elif status == "filtered":
            where.append("i.gated_out = 1"
                         " AND COALESCE(i.attribution,'') != 'rejected'")
        elif status == "rejected":
            where.append("COALESCE(i.attribution,'') = 'rejected'")
        elif status in ("reviewed", "dismissed"):
            where.append("i.status = ?")
            params.append(status)
        elif status == "all":
            # "everything the team works with": screened-out noise has its
            # own tab and is excluded, so dashboard counts match this view
            where.append("i.gated_out = 0")
        if risk:
            # The reviewer's risk areas outrank the classifier's here just
            # as they do on screen -- but only when the review actually set
            # some: '[]' means "no correction", so it falls through to the
            # machine's list, matching prep_item's display rule exactly.
            where.append("COALESCE(NULLIF(i.review_risk_areas, '[]'),"
                         " i.risk_areas) LIKE ?")
            params.append(f'%"{risk}"%')
        if sev:
            where.append("COALESCE(i.review_severity, i.severity) = ?")
            params.append(sev)
        if on_day:
            where.append("i.published_at LIKE ?")
            params.append(on_day + "%")
        win_sql, win_args = date_sql(win, "i")
        if win_sql:
            where.append(win_sql)
            params.extend(win_args)
        if factor:
            where.append("i.factor_matches LIKE ?")
            params.append(f'%"{factor}"%')
        if org:
            where.append("i.relationships LIKE ?")
            params.append(f'%"name": "{org}"%')
        if src == "trusted":
            # A story counts as trusted when its face is -- or when a
            # trusted outlet's report is among its attached sources: the
            # trusted article may simply have arrived second.
            where.append("(i.source_tier IN ('official','trusted')"
                         " OR EXISTS (SELECT 1 FROM item_sources ts"
                         "    WHERE ts.item_id = i.id"
                         "      AND ts.source_tier IN ('official','trusted')))")
        if complaints or topic:
            where.append("COALESCE(i.review_complaint_topics, i.complaint_topics,"
                         " '[]') != '[]'")
        if topic:
            where.append("COALESCE(i.review_complaint_topics, i.complaint_topics,"
                         " '[]') LIKE ?")
            params.append(f'%"{topic}"%')

        rows = q(
            db,
            "SELECT i.*, u.display_name AS reviewer_name,"
            " (SELECT COUNT(*) FROM item_sources s WHERE s.item_id = i.id) AS extra_sources,"
            " (SELECT COUNT(*) FROM reviews r WHERE r.item_id = i.id) AS review_count,"
            " e.name AS entity_name"
            " FROM items i JOIN entities e ON e.id = i.entity_id"
            " LEFT JOIN users u ON u.id = i.reviewed_by"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY CASE COALESCE(i.review_severity, i.severity)"
            "   WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
            " CASE i.actionability WHEN 'action_recommended' THEN 0"
            "   WHEN 'review_recommended' THEN 1 ELSE 2 END,"
            " CASE i.source_tier WHEN 'official' THEN 0"
            "   WHEN 'trusted' THEN 1 ELSE 2 END,"
            " i.relevance DESC, i.published_at DESC LIMIT 200",
            params,
        )
        if entity is None:
            scope_sql, scope_args = (
                f"entity_id IN ({','.join('?' * len(entities))})",
                [e["id"] for e in entities])
        else:
            scope_sql, scope_args = "entity_id = ?", [entity["id"]]
        scope_sql += " AND source_type != 'social'"
        # The tab counts obey the window too, so the number on a tab is the
        # number of rows it opens.
        cnt_sql, cnt_args = date_sql(win)
        if cnt_sql:
            scope_sql += f" AND {cnt_sql}"
            scope_args = list(scope_args) + cnt_args
        counts = {r["s"]: r["n"] for r in q(
            db, "SELECT CASE WHEN COALESCE(attribution,'') = 'rejected'"
                "             THEN 'rejected'"
                "        WHEN gated_out = 1 THEN 'filtered'"
                "        WHEN status IN ('new','classified') THEN 'open'"
                "        ELSE status END s,"
                f" COUNT(*) n FROM items WHERE {scope_sql} GROUP BY s", tuple(scope_args))}
        prepped = [prep_item(r) for r in rows]

        # the Complaints tile leads here: group by each item's primary topic
        grouped = None
        if complaints and not topic:
            buckets: dict = {}
            for it in prepped:
                first = (it["complaint_topics"] or ["Other grievance"])[0]
                buckets.setdefault(first, []).append(it)
            # key must not be "items": Jinja's g.items would resolve to
            # dict.items() instead of the list
            grouped = [{"topic": t, "entries": lst} for t, lst in
                       sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))]

        extras = {k: v for k, v in (
            ("risk", risk), ("sev", sev),
            ("on", on_day), ("factor", factor), ("org", org), ("src", src),
            ("complaints", "1" if complaints else ""), ("topic", topic),
            ("since", win["key"])) if v}
        filter_qs = "".join(f"&{k}={quote(str(v))}" for k, v in extras.items())
        # The period is a lens over every screen, not one of this screen's
        # filter chips: the Date dropdown already names it, and "clear
        # filters" keeps it rather than jumping back to the whole record.
        chips = {k: v for k, v in extras.items() if k != "since"}
        return render(request, "queue.html", user=user, entity=entity, chips=chips,
                      entity_qs="all" if entity is None else entity["id"],
                      office=request.query_params.get("office") or None,
                      entities=entities, items=prepped, grouped=grouped,
                      status=status, risk=risk, counts=counts, win=win,
                      extras=extras, filter_qs=filter_qs)
    finally:
        db.close()


def _flag(v) -> str:
    return "—" if v is None else ("yes" if v else "no")


def _review_changes(prev: dict, cur: dict) -> list[str]:
    """What this review altered relative to the one before it. The audit
    question is normally 'what did this reviewer change?', not 'what did
    they restate?'."""
    out = []
    if bool(prev["relevant"]) != bool(cur["relevant"]):
        out.append(f"relevant {_flag(prev['relevant'])} → {_flag(cur['relevant'])}")
    if (prev["severity"] or "") != (cur["severity"] or ""):
        out.append(f"severity {prev['severity'] or 'none'} → {cur['severity'] or 'none'}")
    if set(prev["risk_areas"]) != set(cur["risk_areas"]):
        out.append("risk areas " + (", ".join(prev["risk_areas"]) or "none")
                   + " → " + (", ".join(cur["risk_areas"]) or "none"))
    if bool(prev["actionable"]) != bool(cur["actionable"]):
        out.append(f"actionable {_flag(prev['actionable'])} → {_flag(cur['actionable'])}")
    if (prev["action"] or "") != (cur["action"] or ""):
        out.append(f"action {prev['action'] or 'none'} → {cur['action'] or 'none'}")
    return out


def _review_history(db, item_id: int) -> list[dict]:
    """Every review recorded on an item, oldest first, each annotated with
    what it changed. Ordered by id as well as time so two reviews saved in
    the same second still read in the order they were made."""
    rows = q(db, "SELECT r.*, u.display_name AS reviewer_name, u.role AS reviewer_role"
                 " FROM reviews r LEFT JOIN users u ON u.id = r.user_id"
                 " WHERE r.item_id = ? ORDER BY r.created_at, r.id", (item_id,))
    out, prev = [], None
    for row in rows:
        d = dict(row)
        try:
            d["risk_areas"] = json.loads(d["risk_areas"] or "[]")
        except (TypeError, ValueError):
            d["risk_areas"] = []
        try:
            d["complaint_topics"] = json.loads(d["complaint_topics"] or "[]")
        except (TypeError, ValueError, KeyError):
            d["complaint_topics"] = []
        d["changes"] = _review_changes(prev, d) if prev else []
        d["first"] = prev is None
        out.append(d)
        prev = d
    return out


@app.get("/item/{item_id}")
def item_detail(request: Request, item_id: int):
    db = connect()
    try:
        user = require_login(db, request)
        row = one(db, "SELECT i.*, u.display_name AS reviewer_name FROM items i"
                      " LEFT JOIN users u ON u.id = i.reviewed_by WHERE i.id = ?", (item_id,))
        if not row:
            raise HTTPException(404, "Item not found")
        if user["role"] != "superadmin" and row["entity_id"] != user["entity_id"]:
            raise HTTPException(403, "Item belongs to another entity's team")
        entity = one(db, "SELECT * FROM entities WHERE id = ?", (row["entity_id"],))
        sources = q(db, "SELECT * FROM item_sources WHERE item_id = ? ORDER BY id", (item_id,))

        similar = [
            (r, score) for r, score in
            similar_reviewed(db, row["entity_id"], f"{row['title']} {row['snippet'] or ''}", top_k=4)
            if r["id"] != item_id
        ][:3]
        suggestion = suggest_action(similar)
        similar_prepped = [(prep_item(r), round(score, 2)) for r, score in similar]

        return render(request, "item.html", user=user, entity=entity,
                      item=prep_action(row, _today()), sources=sources,
                      owners=_assignable_users(db, user, row["entity_id"]),
                      history=_review_history(db, item_id),
                      similar=similar_prepped, suggestion=suggestion,
                      set_aside_reasons=taxonomy.SOCIAL_SET_ASIDE,
                      set_aside_labels=taxonomy.SET_ASIDE_LABELS)
    finally:
        db.close()


@app.post("/item/{item_id}/attribute")
async def item_attribute(request: Request, item_id: int):
    """A human overturns the alias-match rejection: this item IS the
    entity's. Same scope as reviewing -- your team's items only -- since
    it is the same kind of judgement. The item re-enters classification,
    and the gate is told a human has already settled whose item it is."""
    db = connect()
    try:
        user = require_login(db, request)
        row = one(db, "SELECT * FROM items WHERE id = ?", (item_id,))
        if not row:
            raise HTTPException(404, "Item not found")
        if user["role"] != "superadmin" and row["entity_id"] != user["entity_id"]:
            raise HTTPException(403, "Item belongs to another entity's team")
        if row["attribution"] != "rejected":
            raise HTTPException(400, "Only a rejected item can be attributed")
        x(db, "UPDATE items SET attribution='human', status='new', gated_out=0,"
              " gate_reason=NULL, classifier=NULL, classified_at=NULL"
              " WHERE id=?", (item_id,))
    finally:
        db.close()

    def _classify_one():
        cdb = connect()
        try:
            item = one(cdb, "SELECT * FROM items WHERE id = ?", (item_id,))
            if item:
                classify_item(cdb, item)
        finally:
            cdb.close()
    _spawn(asyncio.to_thread(_classify_one))
    return RedirectResponse(
        f"/item/{item_id}?msg=" + quote(
            "Attributed to the entity — classification is running; "
            "refresh in a few seconds."),
        status_code=303)


@app.post("/item/{item_id}/set-aside")
async def item_set_aside(request: Request, item_id: int):
    """A reviewer rules that this social post is no use for
    pattern-finding: generic, venting, a duplicate. It stays on the
    Social media tab under its own tab, keeps its severity and topics,
    and Insights stops counting it. An empty reason undoes the ruling."""
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        row = one(db, "SELECT * FROM items WHERE id = ?", (item_id,))
        if not row:
            raise HTTPException(404, "Item not found")
        if user["role"] != "superadmin" and row["entity_id"] != user["entity_id"]:
            raise HTTPException(403, "Item belongs to another entity's team")
        if row["source_type"] != "social":
            raise HTTPException(400, "Only social posts can be set aside")
        reason = (form.get("reason") or "").strip()
        if reason and reason not in taxonomy.SET_ASIDE_LABELS:
            raise HTTPException(400, "Unknown reason")
        x(db, "UPDATE items SET set_aside = ?, set_aside_by = ? WHERE id = ?",
          (reason or None, user["username"] if reason else None, item_id))
        msg = (f"Set aside as {taxonomy.SET_ASIDE_LABELS[reason].split(' — ')[0].lower()}"
               " — Insights will not count it." if reason
               else "Restored — Insights counts this post again.")
    finally:
        db.close()
    back = form.get("back") or f"/item/{item_id}"
    join = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{join}msg={quote(msg)}", status_code=303)


@app.post("/item/{item_id}/review")
async def item_review(request: Request, item_id: int):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        row = one(db, "SELECT * FROM items WHERE id = ?", (item_id,))
        if not row:
            raise HTTPException(404, "Item not found")
        if user["role"] != "superadmin" and row["entity_id"] != user["entity_id"]:
            raise HTTPException(403, "Item belongs to another entity's team")

        relevant = 1 if form.get("relevant") == "yes" else 0
        actionable = 1 if form.get("actionable") == "yes" else 0
        severity = form.get("severity")
        if severity not in taxonomy.SEVERITIES:
            severity = None
        risk_areas = [a for a in form.getlist("risk_areas") if a in taxonomy.RISK_AREAS]
        topics = [t for t in form.getlist("complaint_topics")
                  if t in taxonomy.COMPLAINT_TOPICS]
        action = form.get("action") or None
        if action not in taxonomy.ACTIONS:
            action = None
        status = "reviewed" if relevant else "dismissed"
        notes = (form.get("notes") or "").strip() or None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        x(db, "UPDATE items SET status=?, reviewed_by=?, reviewed_at=?, review_relevant=?,"
              " review_severity=?, review_risk_areas=?, review_complaint_topics=?,"
              " review_actionable=?, review_action=?,"
              " review_notes=? WHERE id=?",
          (status, user["id"], now, relevant, severity, json.dumps(risk_areas),
           json.dumps(topics), actionable, action, notes, item_id))

        # The items row holds the CURRENT verdict, which the queue, dashboards
        # and learning loop read. This table holds every verdict ever recorded,
        # so a later reviewer can never erase who said what before them.
        x(db, "INSERT INTO reviews (item_id, user_id, created_at, relevant, severity,"
              " risk_areas, complaint_topics, actionable, action, notes)"
              " VALUES (?,?,?,?,?,?,?,?,?,?)",
          (item_id, user["id"], now, relevant, severity, json.dumps(risk_areas),
           json.dumps(topics), actionable, action, notes))

        # The review decides whether follow-up is owed; the To-do page tracks
        # whether it has happened. COALESCE keeps an existing action's state,
        # so re-reviewing an item never silently reopens work already closed.
        if actionable:
            raw = (form.get("action_owner") or "").strip()
            eligible = {str(u["id"]) for u in _assignable_users(db, user, row["entity_id"])}
            owner = int(raw) if raw in eligible else (row["action_owner"] or user["id"])
            x(db, "UPDATE items SET action_status=COALESCE(action_status,'open'),"
                  " action_owner=?, action_due=? WHERE id=?",
              (owner, _valid_date(form.get("action_due")), item_id))
        else:
            x(db, "UPDATE items SET action_status=NULL, action_owner=NULL,"
                  " action_due=NULL, action_closed_at=NULL, action_closed_by=NULL,"
                  " action_close_note=NULL WHERE id=?", (item_id,))
    finally:
        db.close()
    msg = "Review+saved+—+follow-up+added+to+To-do" if actionable else "Review+saved"
    return RedirectResponse(f"/queue?msg={msg}", status_code=303)


@app.post("/item/{item_id}/reviewed")
async def item_mark_reviewed(request: Request, item_id: int):
    """The Reviewed tick on the queue and the Social media tab: mark an
    item reviewed — or send it back for another look — without opening it.

    Deliberately status-only. It records who ticked it and when, but
    fabricates no verdict: the classifier's severity and categories stand
    until a reviewer actually corrects them on the item page, which stays
    open to corrections whatever the box says. Unticking never erases a
    recorded review — the corrections and the history all stand — it only
    puts the item back on the To review tab.
    """
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        row = one(db, "SELECT * FROM items WHERE id = ?", (item_id,))
        if not row:
            raise HTTPException(404, "Item not found")
        # exactly who may open the item page may tick the box
        if user["role"] != "superadmin" and row["entity_id"] != user["entity_id"]:
            raise HTTPException(403, "Item belongs to another entity's team")
        if form.get("reviewed"):
            if row["status"] in ("new", "classified"):
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                x(db, "UPDATE items SET status='reviewed', reviewed_by=?,"
                      " reviewed_at=? WHERE id=?", (user["id"], now, item_id))
        elif row["status"] == "reviewed":
            # back to where it stood before anyone ruled on it; an item the
            # classifier never reached goes back to awaiting classification
            x(db, "UPDATE items SET status=? WHERE id=?",
              ("classified" if row["classified_at"] else "new", item_id))
        # dismissed / filtered / rejected rows carry no box, and a stray
        # POST against one must not quietly resurrect or bury it
    finally:
        db.close()
    back = form.get("back") or "/queue"
    if not back.startswith("/") or back.startswith("//"):
        back = "/queue"
    return RedirectResponse(back, status_code=303)


# --- follow-up actions (the To-do page) -------------------------------------
# Division of labour: a review answers "does this need follow-up?", and the
# To-do page tracks whether that follow-up has happened. Keeping the two
# apart means re-reading an item never disturbs work already closed on it.

ACTION_ROW_SQL = """
SELECT i.*, e.name AS entity_name,
       ow.display_name AS owner_name,
       rv.display_name AS reviewer_name,
       cl.display_name AS closed_by_name
FROM items i
JOIN entities e ON e.id = i.entity_id
LEFT JOIN users ow ON ow.id = i.action_owner
LEFT JOIN users rv ON rv.id = i.reviewed_by
LEFT JOIN users cl ON cl.id = i.action_closed_by
WHERE i.action_status IS NOT NULL
"""


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _valid_date(raw: str | None) -> str | None:
    """Accept an ISO date from a <input type=date>, reject anything else."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _assignable_users(db, actor, entity_id):
    """Who this actor may allocate a follow-up on this entity to.

    The hierarchy allocates downward or sideways, never upward: the
    superadmin may pick anyone; a team lead picks themselves or their
    members; a member picks themselves or a fellow member -- handing a
    task up to the lead (or to a superadmin) is the lead's call to make
    about their own plate, not the member's.
    """
    if actor["role"] == "superadmin":
        return q(db, "SELECT id, display_name, role FROM users"
                     " ORDER BY (entity_id IS NOT ?), (role = 'superadmin'),"
                     " display_name", (entity_id,))
    if actor["entity_id"] != entity_id:
        return []
    if actor["role"] == "lead":
        return q(db, "SELECT id, display_name, role FROM users"
                     " WHERE entity_id = ? AND (role = 'member' OR id = ?)"
                     " ORDER BY (id != ?), display_name",
                 (entity_id, actor["id"], actor["id"]))
    return q(db, "SELECT id, display_name, role FROM users"
                 " WHERE entity_id = ? AND role = 'member'"
                 " ORDER BY (id != ?), display_name", (entity_id, actor["id"]))


def _can_close(user, row) -> bool:
    if user["role"] == "superadmin":
        return True
    if row["entity_id"] != user["entity_id"]:
        return False
    return user["role"] == "lead" or row["action_owner"] == user["id"]


def _can_assign(db, user, row) -> bool:
    """Whether this user may (re)allocate this follow-up.

    Members allocate sideways only: they may move a task that sits with a
    fellow member (or with nobody), but not one on the lead's or a
    superadmin's plate -- taking work off a senior's desk is as much an
    upward act as handing work to them.
    """
    if user["role"] == "superadmin":
        return True
    if row["entity_id"] != user["entity_id"]:
        return False
    if user["role"] == "lead":
        return True
    if user["role"] != "member":
        return False
    if not row["action_owner"]:
        return True
    owner = one(db, "SELECT role FROM users WHERE id = ?", (row["action_owner"],))
    return bool(owner) and owner["role"] == "member"


def prep_action(row, today: str) -> dict:
    d = prep_item(row)
    d["overdue"] = bool(d.get("action_due")) and d["action_status"] == "open" \
        and d["action_due"] < today
    d["due_today"] = d.get("action_due") == today and d["action_status"] == "open"
    d["due_label"] = ""
    if d.get("action_due"):
        try:
            d["due_label"] = datetime.strptime(d["action_due"], "%Y-%m-%d").strftime("%d %b")
        except ValueError:
            d["due_label"] = d["action_due"]
    return d


def _action_sort_key(d):
    """Open before done, overdue before the rest, then by severity, then by
    the nearest due date, then oldest review first."""
    return (
        0 if d["action_status"] == "open" else 1,
        0 if d["overdue"] else 1,
        taxonomy.SEVERITY_RANK.get(d["severity_shown"], 3),
        d.get("action_due") or "9999-12-31",
        d.get("reviewed_at") or "",
    )


def _open_action_count(user) -> int:
    """Open follow-ups inside the signed-in user's scope, for the nav badge.
    Opens its own short-lived connection so every page carries the badge
    without threading a handle through every route."""
    db = connect()
    try:
        if user["role"] == "superadmin":
            row = one(db, "SELECT COUNT(*) AS n FROM items WHERE action_status = 'open'")
        else:
            row = one(db, "SELECT COUNT(*) AS n FROM items"
                          " WHERE action_status = 'open' AND entity_id = ?",
                      (user["entity_id"],))
        return row["n"] if row else 0
    finally:
        db.close()


def _action_or_404(db, item_id: int):
    row = one(db, "SELECT * FROM items WHERE id = ?", (item_id,))
    if not row:
        raise HTTPException(404, "Item not found")
    if row["action_status"] is None:
        raise HTTPException(404, "This item has no follow-up recorded")
    return row


def _todo_back(form, msg: str) -> str:
    """Return to the same filtered view the action was taken from."""
    back = (form.get("back") or "").lstrip("?&")
    return f"/todo?{back}{'&' if back else ''}msg={quote(msg)}"


@app.get("/todo")
def todo_page(request: Request):
    db = connect()
    try:
        user = require_login(db, request)
        entities = visible_entities(db, user)
        params = request.query_params

        status = params.get("status") or "open"
        if status not in ("open", "done", "all"):
            status = "open"
        mine = params.get("owner") == "me"
        overdue_only = params.get("overdue") == "1"
        sev = params.get("sev")
        risk = params.get("risk")
        ent = params.get("entity")

        sql, args = ACTION_ROW_SQL, []
        if user["role"] != "superadmin":
            sql += " AND i.entity_id = ?"
            args.append(user["entity_id"])
        elif ent and ent.isdigit():
            sql += " AND i.entity_id = ?"
            args.append(int(ent))

        # Scope once, then count and filter in Python: severity_shown and the
        # reviewer's risk-area override are computed in prep_item, so SQL
        # cannot express them -- and deriving the tab counts from the same
        # list the rows come from keeps every count equal to its drill-down.
        today = _today()
        scoped = [prep_action(r, today) for r in q(db, sql, tuple(args))]
        counts = {
            "open": sum(1 for r in scoped if r["action_status"] == "open"),
            "done": sum(1 for r in scoped if r["action_status"] == "done"),
            "all": len(scoped),
            "overdue": sum(1 for r in scoped if r["overdue"]),
            "mine": sum(1 for r in scoped
                        if r["action_status"] == "open" and r["action_owner"] == user["id"]),
        }

        rows = scoped
        if status != "all":
            rows = [r for r in rows if r["action_status"] == status]
        if mine:
            rows = [r for r in rows if r["action_owner"] == user["id"]]
        if overdue_only:
            rows = [r for r in rows if r["overdue"]]
        if sev in taxonomy.SEVERITIES:
            rows = [r for r in rows if r["severity_shown"] == sev]
        if risk:
            rows = [r for r in rows if risk in r["risk_areas_shown"]]
        rows.sort(key=_action_sort_key)
        # Resolve per-row permissions here rather than in the template, where
        # the role rules would be spread across several nested conditionals.
        for r in rows:
            r["can_close"] = _can_close(user, r)
            r["can_assign"] = _can_assign(db, user, r)

        owners = {e["id"]: _assignable_users(db, user, e["id"]) for e in entities}
        extras = {k: v for k, v in (
            ("owner", "me" if mine else ""), ("overdue", "1" if overdue_only else ""),
            ("sev", sev or ""), ("risk", risk or ""), ("entity", ent or "")) if v}
        filter_qs = "".join(f"&{k}={quote(str(v))}" for k, v in extras.items())
        back = f"status={status}" + filter_qs

        def toggle(key: str, value: str) -> str:
            """The same view with one filter flipped on or off."""
            d = dict(extras)
            d.pop(key, None) if d.get(key) == value else d.update({key: value})
            return f"/todo?status={status}" + "".join(
                f"&{k}={quote(str(v))}" for k, v in d.items() if v)

        return render(request, "todo.html", user=user, rows=rows, counts=counts,
                      status=status, entities=entities, owners=owners,
                      extras=extras, filter_qs=filter_qs, back=back,
                      mine=mine, overdue_only=overdue_only,
                      url_overdue=toggle("overdue", "1"), url_mine=toggle("owner", "me"),
                      today=today, todo_count=counts["open"])
    finally:
        db.close()


@app.post("/todo/{item_id}/done")
async def todo_done(request: Request, item_id: int):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        row = _action_or_404(db, item_id)
        if not _can_close(user, row):
            raise HTTPException(403, "Only the owner or a team lead can close this")
        x(db, "UPDATE items SET action_status='done', action_closed_at=?,"
              " action_closed_by=?, action_close_note=? WHERE id=?",
          (datetime.now(timezone.utc).isoformat(timespec="seconds"), user["id"],
           (form.get("close_note") or "").strip() or None, item_id))
    finally:
        db.close()
    return RedirectResponse(_todo_back(form, "Action closed"), status_code=303)


@app.post("/todo/{item_id}/reopen")
async def todo_reopen(request: Request, item_id: int):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        row = _action_or_404(db, item_id)
        if not _can_close(user, row):
            raise HTTPException(403, "Only the owner or a team lead can reopen this")
        x(db, "UPDATE items SET action_status='open', action_closed_at=NULL,"
              " action_closed_by=NULL, action_close_note=NULL WHERE id=?", (item_id,))
    finally:
        db.close()
    return RedirectResponse(_todo_back(form, "Action reopened"), status_code=303)


@app.post("/todo/{item_id}/assign")
async def todo_assign(request: Request, item_id: int):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        row = _action_or_404(db, item_id)
        if not _can_assign(db, user, row):
            raise HTTPException(403, "You cannot reallocate this follow-up")
        raw = (form.get("action_owner") or "").strip()
        eligible = {str(u["id"]) for u in _assignable_users(db, user, row["entity_id"])}
        if raw and raw not in eligible:
            # An out-of-rank target is a refusal, not a silent keep: a
            # member posting the lead's id must hear no, not "updated".
            raise HTTPException(403, "You cannot allocate a task to that user")
        owner = int(raw) if raw else row["action_owner"]
        x(db, "UPDATE items SET action_owner=?, action_due=? WHERE id=?",
          (owner, _valid_date(form.get("action_due")), item_id))
    finally:
        db.close()
    return RedirectResponse(_todo_back(form, "Action updated"), status_code=303)


@app.get("/insights")
def insights_page(request: Request):
    """Patterns across the social-media grievances: which product or
    process keeps drawing complaints, with evidence and a recommendation.
    Anyone on the team may read them; generating costs a model call, so
    that stays with leads and the superadmin, like Fetch."""
    db = connect()
    try:
        user = require_login(db, request)
        entity, entities = resolve_entity(db, user, request.query_params.get("entity"))
        if entity is None:
            ids = [e["id"] for e in entities]
            scope = f"entity_id IN ({','.join('?' * len(ids))})"
            args = list(ids)
        else:
            scope, args = "entity_id = ?", [entity["id"]]
        rows = q(db, "SELECT i.*, e.name AS entity_name FROM insights i"
                     " JOIN entities e ON e.id = i.entity_id"
                     f" WHERE i.{scope}"
                     " ORDER BY CASE i.severity WHEN 'high' THEN 0"
                     "   WHEN 'medium' THEN 1 ELSE 2 END, i.id",
                 tuple(args))
        cards = []
        for r in rows:
            d = dict(r)
            try:
                d["item_ids"] = json.loads(d["item_ids"] or "[]")
            except (TypeError, ValueError):
                d["item_ids"] = []
            cards.append(d)
        n_grievances = 0
        if entity is not None:
            n_grievances = len(insights_mod.grievances_for(db, entity["id"]))
        return render(request, "insights.html", user=user, entity=entity,
                      entities=entities, cards=cards,
                      entity_qs="all" if entity is None else entity["id"],
                      n_grievances=n_grievances,
                      min_evidence=insights_mod.MIN_EVIDENCE,
                      can_generate=(user["role"] in ("lead", "superadmin")
                                    and entity is not None))
    finally:
        db.close()


@app.post("/insights/generate")
async def insights_generate(request: Request):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "lead", "superadmin")
        entity_id = form.get("entity_id")
        row = one(db, "SELECT * FROM entities WHERE id = ?", (entity_id,))
        if not row:
            raise HTTPException(404, "No such entity")
        if user["role"] != "superadmin" and user["entity_id"] != row["id"]:
            raise HTTPException(403, "Not your team's entity")
        try:
            result = insights_mod.generate(db, row, user["id"])
        except Exception as exc:
            log.warning("Insight generation failed for %s: %s: %s",
                        row["name"], type(exc).__name__, exc)
            # A RuntimeError here is one of our own plain-language messages
            # ("the model ran out of room while..."); anything else keeps
            # its class name so an unexpected failure stays diagnosable.
            what = (str(exc) if isinstance(exc, RuntimeError)
                    else f"{type(exc).__name__}: {exc}")
            msg = f"Could not generate insights: {what[:160]}"
            return RedirectResponse(
                f"/insights?entity={row['id']}&msg={quote(msg)}", status_code=303)
    finally:
        db.close()
    if result["grievances"] == 0:
        msg = "No social-media grievances to analyse yet — run Fetch social first."
    elif result["insights"] == 0:
        msg = (f"Analysed {result['grievances']} grievances — no pattern with "
               f"at least {insights_mod.MIN_EVIDENCE} supporting complaints. "
               "That is a finding too.")
    else:
        msg = (f"{result['insights']} pattern(s) found across "
               f"{result['grievances']} grievances.")
    return RedirectResponse(f"/insights?entity={row['id']}&msg={quote(msg)}",
                            status_code=303)


@app.get("/social")
def social_page(request: Request):
    """Customer grievances posted to the entities' handles on X.

    Deliberately narrower than the queue: a social post only appears here if
    the classifier found a grievance in it. Posts addressed to a bank's care
    handle that turn out to be praise, questions or noise are counted but not
    listed, because the supervisory question this screen answers is "what are
    customers complaining about", not "what was said".
    """
    db = connect()
    try:
        user = require_login(db, request)
        entity, entities = resolve_entity(db, user, request.query_params.get("entity"),
                                          office=request.query_params.get("office") or None)
        topic = request.query_params.get("topic", "")
        if topic not in taxonomy.COMPLAINT_TOPICS:
            topic = ""
        # a landing filter from the Dashboard's factor panel and the
        # Alerts screen's match counts, cleared by its own note below
        factor_f = (request.query_params.get("factor") or "")[:80]

        if entity is None:
            ids = [e["id"] for e in entities]
            scope = f"entity_id IN ({','.join('?' * len(ids))})"
            args = list(ids)
        else:
            scope, args = "entity_id = ?", [entity["id"]]
        # The window is applied once, here, so every count on the page --
        # collected, pending, per topic, per source -- speaks about the
        # same period as the list below them.
        win = date_window(request)
        win_sql, win_args = date_sql(win, "i")
        win_and = f" AND {win_sql}" if win_sql else ""
        rows = [prep_item(r) for r in q(
            db, "SELECT i.*, e.name AS entity_name FROM items i"
                " JOIN entities e ON e.id = i.entity_id"
                f" WHERE i.source_type = 'social' AND i.gated_out = 0 AND i.{scope}"
                f"{win_and}",
            tuple(args) + tuple(win_args))]
        # Posts the classifier dropped by what reviewers taught it. Shown
        # on their own tab: a learned gate that no one can inspect is a
        # silent loss, which is the one thing this pipeline never allows.
        learned = [prep_item(r) for r in q(
            db, "SELECT i.*, e.name AS entity_name FROM items i"
                " JOIN entities e ON e.id = i.entity_id"
                " WHERE i.source_type = 'social' AND i.gated_out = 1"
                " AND i.gate_reason LIKE 'matches posts this team set aside%'"
                f" AND i.{scope}{win_and}", tuple(args) + tuple(win_args))]
        for r in learned:
            r["platform"] = taxonomy.social_platform(r["url"])

        # Posts still awaiting classification carry no verdict yet; counting
        # them as "no complaint found" would misstate a fetch in progress.
        pending = sum(1 for r in rows if r["status"] == "new")
        grievances = [r for r in rows if r["complaint_topics"]]
        for r in grievances:
            r["platform"] = taxonomy.social_platform(r["url"])
        src = request.query_params.get("src", "")
        if src not in taxonomy.SOCIAL_PLATFORMS and src != "Other":
            src = ""
        view = request.query_params.get("view", "")
        view_aside = view == "aside"
        view_learned = view == "learned"
        set_aside_rows = [r for r in grievances if r["set_aside"]]
        grievances = [r for r in grievances if not r["set_aside"]]
        by_topic = Counter(t for r in grievances
                           if not src or r["platform"] == src
                           for t in r["complaint_topics"])
        # The topic row's "All" chip counts POSTS, like the source row's.
        # Summing the per-topic counts instead double-counted every post
        # that carries several topics (most real complaints do), so the
        # two "All" chips disagreed on the same page.
        topic_all = sum(1 for r in grievances
                        if not src or r["platform"] == src)
        by_source = Counter(r["platform"] for r in grievances
                            if not topic or topic in r["complaint_topics"])
        pool = (learned if view_learned else
                set_aside_rows if view_aside else grievances)
        shown = [r for r in pool
                 if (not topic or topic in r["complaint_topics"])
                 and (not src or r["platform"] == src)
                 and (not factor_f or factor_f in r["factor_matches"])]
        shown.sort(key=lambda r: (taxonomy.SEVERITY_RANK.get(r["severity_shown"], 3),
                                  r["published_at"] or ""), reverse=False)
        shown.reverse()
        shown.sort(key=lambda r: taxonomy.SEVERITY_RANK.get(r["severity_shown"], 3))

        handles = [e for e in entities if e["x_handle"]]
        return render(request, "social.html", user=user, entity=entity,
                      entity_qs="all" if entity is None else entity["id"],
                      office=request.query_params.get("office") or None,
                      entities=entities, rows=shown, topic=topic, src=src,
                      factor=factor_f, win=win,
                      view_aside=view_aside, view_learned=view_learned,
                      learned_count=len(learned),
                      set_aside_count=len(set_aside_rows),
                      set_aside_reasons=taxonomy.SOCIAL_SET_ASIDE,
                      by_topic=[(t, by_topic.get(t, 0)) for t in taxonomy.COMPLAINT_TOPICS],
                      topic_all=topic_all,
                      by_source=[(p, by_source.get(p, 0)) for p in
                                 taxonomy.SOCIAL_PLATFORMS + ["Other"]],
                      total_grievances=len(grievances),
                      complainants=complainant_count(grievances)[0],
                      attributed=complainant_count(grievances)[1],
                      pending=pending,
                      not_grievances=len(rows) - len(grievances) - pending,
                      collected=len(rows), handles=handles,
                      x_enabled=X_ENABLED or x_scrape.ENABLED,
                      any_source=(reddit_source.ENABLED or forums.ENABLED
                                  or X_ENABLED or x_scrape.ENABLED),
                      x_cap=X_MAX_POSTS, social_days=SOCIAL_LOOKBACK_DAYS)
    finally:
        db.close()


# --- dashboards -------------------------------------------------------------

# The activity chart is a picture of recent tempo, not a filter on what
# the page counts: every tile and list below covers the whole record.
TREND_DAYS = 30


def _entity_stats(db, entity_id: int, win: dict | None = None) -> dict:
    win = win or {"key": "", "start": "", "end": ""}
    win_sql, win_args = date_sql(win)
    win_and = f" AND {win_sql}" if win_sql else ""
    rows = [prep_item(r) for r in q(
        db, "SELECT * FROM items WHERE entity_id = ?"
            f" AND gated_out = 0 AND source_type != 'social'{win_and}",
        (entity_id, *win_args))]

    by_risk, by_sev, by_factor, by_day = Counter(), Counter(), Counter(), Counter()
    by_topic, complaints_total = Counter(), 0
    linkages = Counter()
    for it in rows:
        for a in it["risk_areas_shown"]:
            by_risk[a] += 1
        by_sev[it["severity_shown"]] += 1
        if it["complaint_topics"]:
            complaints_total += 1
            for t in it["complaint_topics"]:
                by_topic[t] += 1
        for f in it["factor_matches"]:
            by_factor[f] += 1
        for rel in it["relationships"]:
            linkages[(rel.get("type", "other"), rel.get("name", "?"))] += 1
        day = (it["published_at"] or "")[:10]
        if day:
            by_day[day] += 1

    # Factors watch both workstreams, so their panel counts news and social
    # side by side — the one figure here that looks at social, which
    # otherwise has its own tab and Insights. Social counts follow that
    # tab's default view (a grievance, not set aside), so the number
    # matches the screen it links to.
    soc_factor = Counter()
    for r in q(db, "SELECT factor_matches FROM items WHERE entity_id = ?"
                   " AND gated_out = 0 AND source_type = 'social'"
                   " AND factor_matches != '[]' AND set_aside IS NULL"
                   " AND COALESCE(review_complaint_topics, complaint_topics,"
                   "              '[]') != '[]'"
                   f"{win_and}", (entity_id, *win_args)):
        try:
            for f in json.loads(r["factor_matches"] or "[]"):
                soc_factor[f] += 1
        except (TypeError, ValueError):
            pass

    today = datetime.now(timezone.utc).date()
    trend = []
    for offset in range(TREND_DAYS - 1, -1, -1):
        d = today - timedelta(days=offset)
        trend.append({"date": d.strftime("%d %b"), "iso": d.isoformat(),
                      "count": by_day.get(d.isoformat(), 0)})
    max_trend = max((t["count"] for t in trend), default=0)

    open_count = one(db, "SELECT COUNT(*) n FROM items WHERE entity_id=? AND"
                         " status IN ('new','classified') AND gated_out = 0"
                         f" AND source_type != 'social'{win_and}",
                     (entity_id, *win_args))["n"]
    # Deliberately NOT windowed: this is the whole record, so "older" below
    # can say how much a chosen period is leaving out.
    total_all = one(db, "SELECT COUNT(*) n FROM items WHERE entity_id = ?"
                        " AND gated_out = 0 AND source_type != 'social'",
                    (entity_id,))["n"]
    # Follow-ups are deliberately NOT windowed: an action opened five weeks
    # ago is still owed today, and hiding it behind the window would be the
    # one number on this page that understates the team's workload.
    actions = one(db, "SELECT"
                      " SUM(action_status='open') AS open,"
                      " SUM(action_status='open' AND action_due IS NOT NULL"
                      "     AND action_due < date('now')) AS overdue"
                      " FROM items WHERE entity_id = ?", (entity_id,))
    high_recent = [prep_item(r) for r in q(
        db, "SELECT * FROM items WHERE entity_id=?"
            " AND COALESCE(review_severity, severity)='high'"
            f" AND gated_out = 0 AND source_type != 'social'{win_and}"
            " ORDER BY published_at DESC LIMIT 6", (entity_id, *win_args))]

    return {
        "total": len(rows),
        "by_risk": [(a, by_risk.get(a, 0)) for a in taxonomy.RISK_AREAS],
        "max_risk": max(by_risk.values(), default=0),
        "by_sev": {s: by_sev.get(s, 0) for s in taxonomy.SEVERITIES},
        "by_factor": [(n, by_factor.get(n, 0), soc_factor.get(n, 0))
                      for n, _ in (by_factor + soc_factor).most_common(8)],
        "complaints_total": complaints_total,
        "by_topic": by_topic.most_common(12),
        "trend": trend, "max_trend": max_trend,
        "open_count": open_count,
        "total_all": total_all,
        "older": total_all - len(rows),
        "actions_open": actions["open"] or 0,
        "actions_overdue": actions["overdue"] or 0,
        "high_recent": high_recent,
        "linkages": [
            {"type": t, "name": n, "count": c}
            for (t, n), c in linkages.most_common(10)
        ],
        "trend_days": TREND_DAYS,
    }


# ══ Complaints ════════════════════════════════════════════════════════
# Everywhere else in this application a grievance is met one at a time.
# This screen asks the other question -- what shape does the whole set
# have -- so it opens on no individual complaint at all: which entities,
# which kinds of grievance, and where in the country, with news coverage
# and social media counted together for once.

# What the classifier writes into items.geography when a story is not
# about one place. Matched case-folded, as words rather than as a code,
# because words are what the model returns.
# Matched as whole words over the whole geography field, never as a
# substring: "India" sits inside "Bhiwandi, India" and "national" inside
# "International", and a local complaint filed as a national one is a
# district that never has to answer for it.
PAN_INDIA_WORDS = {"india", "pan", "panindia", "national", "nationwide",
                   "countrywide", "nationally", "everywhere", "multiple",
                   "states", "country", "wide"}
PAN_INDIA_FILLER = {"across", "all", "in", "the", "and", "of", "a", "an",
                    "throughout", "over"}
PLACE_PAN = "Across India"
# A district two states share, with nothing in the story to say which.
# Its own answer, not the same answer as "nothing was named": the
# district is a fact, and the districts panel prints it by name.
PLACE_AMBIG = "District named, state unclear"
PLACE_NONE = "Not ascertainable"
UNPLACED = (PLACE_PAN, PLACE_AMBIG, PLACE_NONE)


def _complaint_rows(db, entities, win):
    """Every grievance in scope, from news and social media alike.

    This is the complaint test the rest of the application uses, with the
    exclusions each screen applies on its own brought together in one
    place: an item the pipeline gated out, one still awaiting
    classification, one whose attribution a reviewer rejected, one a
    reviewer dismissed as not this entity's, and a social post set aside
    as no use for pattern-finding are all left out. The dismissal matters
    most of the four: the review form arrives with the classifier's topics
    already ticked, so a reviewer who dismisses an item without unticking
    them stores those topics as their own ruling -- and "this is not our
    bank's story" would otherwise reach this screen as the strongest
    possible evidence that it is.
    A reviewer's correction of the topics decides -- and a corrected list
    with nothing in it means "ruled not a grievance", so it must not fall
    through to the classifier's verdict, which is why there is no NULLIF
    here as there is for risk areas.
    """
    if not entities:
        return []
    ids = [e["id"] for e in entities]
    holes = ",".join("?" * len(ids))
    win_sql, win_args = date_sql(win, "i")
    win_and = f" AND {win_sql}" if win_sql else ""
    rows = [prep_item(r) for r in q(
        db, "SELECT i.*, e.name AS entity_name, e.kind AS entity_kind,"
            " e.aliases AS entity_aliases,"
            " (SELECT group_concat(COALESCE(s.title, ''), ' ') FROM item_sources s"
            "  WHERE s.item_id = i.id) AS source_titles"
            " FROM items i JOIN entities e ON e.id = i.entity_id"
            f" WHERE i.entity_id IN ({holes})"
            " AND i.gated_out = 0 AND i.status NOT IN ('new', 'dismissed')"
            " AND COALESCE(i.attribution, '') != 'rejected'"
            " AND COALESCE(i.review_complaint_topics, i.complaint_topics, '[]') != '[]'"
            " AND (i.source_type != 'social' OR i.set_aside IS NULL)"
            f"{win_and}", (*ids, *win_args))]
    # The SQL test above compares stored text against '[]'; the parsed list
    # is the truth, and a reviewer who cleared every topic drops out here.
    return [r for r in rows if r["complaint_topics"]]


def _alias_words(it) -> list:
    """An entity's other names, to be struck out of a place scan along with
    its main one: a bank that merged keeps the old name in the copy, and
    Vijaya Bank should no more place a complaint than Bank of Maharashtra."""
    try:
        return [a for a in json.loads(it.get("entity_aliases") or "[]")
                if isinstance(a, str)]
    except (TypeError, ValueError):
        return []


def _reads_as_pan_india(geo: str) -> bool:
    """Is the classifier's geography saying "everywhere" and nothing else?

    A whole-string test, not a substring one. "India" appears inside
    "Bhiwandi, India" and "national" inside "International transactions",
    and either would otherwise file a local complaint as a national one and
    never look at the story again.
    """
    words = [w for w in re.split(r"[^a-z]+", (geo or "").lower()) if w]
    if not words:
        return False
    rest = [w for w in words if w not in PAN_INDIA_FILLER]
    return bool(rest) and all(w in PAN_INDIA_WORDS for w in rest)


def _complaint_place(it) -> dict:
    """Where a complaint happened, as far as its own words can say.

    The classifier is asked for the geography outright, so its answer is
    read first and a national story is left national: a district named
    somewhere in the body of a pan-India story is an example, not the
    place the complaint is about. Only when the classifier is silent, or
    says something no place index knows, is the story's own text scanned.
    The entity's name and its aliases are excluded from that scan
    throughout, so "Bank of Maharashtra" never places a complaint in
    Maharashtra.
    """
    name = " ".join([it.get("entity_name") or ""] + _alias_words(it))
    geo = (it.get("geography") or "").strip()
    hit = geography.locate(geo, exclude=name) if geo else None
    if not (hit and (hit["district"] or hit["state"])):
        if _reads_as_pan_india(geo):
            return {"district": None, "state": None, "bucket": PLACE_PAN,
                    "term": geo}
        text = " ".join(filter(None, (it.get("title"), it.get("snippet"),
                                      it.get("summary"),
                                      it.get("source_titles"))))
        hit = geography.locate(text, exclude=name)
    if hit["state"]:
        return {"district": hit["district"], "state": hit["state"],
                "bucket": hit["state"], "term": hit["term"]}
    if hit["district"]:
        # A district two states share (Aurangabad, Bilaspur, Hamirpur) with
        # nothing in the story to say which. The district is a fact and is
        # reported; the state is not, and is not guessed.
        return {"district": hit["district"], "state": None,
                "bucket": PLACE_AMBIG, "term": hit["district"]}
    return {"district": None, "state": None, "bucket": PLACE_NONE, "term": None}


def _place_choices(pool) -> list:
    """The place buckets present in a set of complaints, busiest first,
    with the ones that are answers rather than places kept last."""
    counts = Counter(r["place"]["bucket"] for r in pool)
    return [b for b, _ in sorted(
        counts.items(), key=lambda kv: (kv[0] in UNPLACED, -kv[1], kv[0]))]


def _previous_window(win: dict):
    """The period of equal length immediately before the chosen one, so a
    figure can be said to have risen or fallen rather than just to be."""
    if not win["key"]:
        return None
    start = datetime.fromisoformat(win["start"]).date()
    # An open-ended window runs to the end of today, so "last 7 days" is
    # eight dates wide, not seven. Deriving the comparison period from the
    # key instead of from the window measured seven against eight, and a
    # perfectly flat complaint rate read as a rise in every period on the
    # screen.
    end = (datetime.fromisoformat(win["end"]).date() if win["end"]
           else datetime.now(timezone.utc).date() + timedelta(days=1))
    span = max(1, (end - start).days)
    return {"key": "previous", "label": "previous period",
            "start": (start - timedelta(days=span)).isoformat(),
            "end": start.isoformat()}


def _heat_level(n: int, mx: int) -> int:
    """A cell's shade, 0 (empty) to 5, relative to the busiest cell on the
    screen. Shade only qualifies the figure printed in the cell; it never
    replaces it."""
    if not n or not mx:
        return 0
    return 1 + min(4, int(n / mx * 5))


def _period_before(win: dict) -> str:
    """How to name the period a figure is being compared against, in words
    a sentence can carry: "vs the 7 days before", "vs yesterday"."""
    if not win["key"]:
        return ""
    if win["key"] == "today":
        return "vs yesterday"
    if win["key"] == "yesterday":
        return "vs the day before"
    return f"vs the {win['key']} days before"


def _heat_scale(pools, key_fns, topics) -> int:
    """The busiest cell any of these matrices will print, so all of them
    can be shaded against the same number."""
    mx = 0
    for pool, key_fn in zip(pools, key_fns):
        cells = Counter((key_fn(it), t) for it in pool
                        if key_fn(it) is not None
                        for t in it["complaint_topics"] if t in topics)
        mx = max([mx] + list(cells.values()))
    return mx


def _heat_matrix(sel, key_fn, label_fn, topics, link, param, limit=0,
                 on_key=None, on_topic="", scale=0):
    """Complaints crossed against complaint type, for whatever a row is.

    A row's total counts complaints; a cell counts complaints carrying
    that topic. Most grievances carry more than one, so a row of cells
    legitimately adds up to more than the row total -- the page says so
    rather than quietly reconciling them.

    `scale` is the busiest cell on the whole screen, passed in so both
    matrices shade against the same figure. Left to their own maxima, a 1
    could be the palest cell in one table and the darkest in the other,
    which is a lie told in colour.
    """
    cells, totals = Counter(), Counter()
    for it in sel:
        key = key_fn(it)
        if key is None:
            continue
        totals[key] += 1
        for t in it["complaint_topics"]:
            if t in topics:
                cells[(key, t)] += 1
    keys = [k for k, _ in totals.most_common()]
    dropped = 0
    if limit and len(keys) > limit:
        dropped, keys = len(keys) - limit, keys[:limit]
    mx = scale or (max(cells.values()) if cells else 0)
    rows = [{
        "label": label_fn(k), "total": totals[k], "href": link(**{param: k}),
        "on": on_key is not None and k == on_key,
        "cells": [{"n": cells.get((k, t), 0), "topic": t,
                   "level": _heat_level(cells.get((k, t), 0), mx),
                   "on": on_key is not None and k == on_key and t == on_topic,
                   "href": link(**{param: k, "topic": t})}
                  for t in topics],
    } for k in keys]
    # Counted over everything in the pool, not just the rows that have a
    # key: a state matrix skips the complaints it could not place, and a
    # footer that skipped them too would print one number and open a page
    # showing a bigger one.
    col_totals = [{"topic": t, "href": link(topic=t),
                   "n": sum(1 for it in sel if t in it["complaint_topics"])}
                  for t in topics]
    return {"rows": rows, "cols": topics, "col_totals": col_totals,
            "total": sum(totals.values()), "dropped": dropped}


@app.get("/complaints")
def complaints(request: Request):
    db = connect()
    try:
        user = require_login(db, request)
        office = request.query_params.get("office") or None
        entities = visible_entities(db, user)
        if office:
            if user["role"] != "superadmin" and (user["rbi_office"] or "") != office:
                raise HTTPException(403, "That office is not visible to your role")
            entities = [e for e in entities if office in entity_offices(e)]
        if not entities:
            raise HTTPException(404, "No entities configured")
        # This screen is an aggregate over everything the signed-in officer
        # may see, so it scopes itself through visible_entities rather than
        # through resolve_entity: the cross-entity view there is the super
        # admin's alone, and a team lead has every right to the shape of
        # their own entities' grievances.
        names = {e["id"]: e["name"] for e in entities}

        topics = taxonomy.COMPLAINT_TOPICS
        ent_f = request.query_params.get("entity", "")
        if ent_f not in {str(e["id"]) for e in entities}:
            ent_f = ""
        topic_f = request.query_params.get("topic", "")
        if topic_f not in topics:
            topic_f = ""
        # Types of entity actually on the roster, not the whole vocabulary:
        # a supervisor with only banks should not be offered NBFC.
        kinds = _present_kinds(entities)
        kind_f = request.query_params.get("kind", "")
        if kind_f not in kinds:
            kind_f = ""
        loc_f = request.query_params.get("loc", "")
        district_f = request.query_params.get("district", "")
        src_f = request.query_params.get("src", "")
        if src_f not in ("social", "other"):
            src_f = ""
        sev_f = request.query_params.get("sev", "")
        if sev_f not in taxonomy.SEVERITIES:
            sev_f = ""
        win = date_window(request)

        def link(**over):
            """This same screen with one more thing decided. Every figure
            here is a link, and a click narrows the page rather than
            replacing it, so a supervisor can walk from a shape to the
            complaints behind it without losing the way back."""
            parts = {"entity": ent_f, "kind": kind_f, "topic": topic_f,
                     "loc": loc_f, "district": district_f, "src": src_f,
                     "sev": sev_f, "since": win["key"], "office": office or ""}
            parts.update({k: ("" if v is None else str(v))
                          for k, v in over.items()})
            tail = "&".join(f"{k}={quote(str(v))}" for k, v in parts.items() if v)
            return "/complaints" + (f"?{tail}" if tail else "")

        rows = _complaint_rows(db, entities, win)
        for it in rows:
            it["place"] = _complaint_place(it)

        def narrow(pool, skip=()):
            """The complaints left once the filters in force are applied.

            `skip` leaves named filters out. Every control that offers a
            choice counts as if its own choice were not made, so a figure
            beside a control always matches the page that control opens:
            a High severity tile reading 0 while Medium is selected, that
            opens a page with two items on it, is a number nobody can
            trust again. The tiles that describe the selection itself --
            the total, the entities, the categories, the list -- use the
            fully narrowed set.
            """
            out = pool
            if kind_f and "kind" not in skip:
                out = [r for r in out if r["entity_kind"] == kind_f]
            if ent_f and "entity" not in skip:
                out = [r for r in out if str(r["entity_id"]) == ent_f]
            if topic_f and "topic" not in skip:
                out = [r for r in out if topic_f in r["complaint_topics"]]
            if "src" not in skip:
                if src_f == "social":
                    out = [r for r in out if r["source_type"] == "social"]
                elif src_f == "other":
                    out = [r for r in out if r["source_type"] != "social"]
            if sev_f and "sev" not in skip:
                out = [r for r in out if r["severity_shown"] == sev_f]
            if loc_f and "loc" not in skip:
                out = [r for r in out if r["place"]["bucket"] == loc_f]
            if district_f and "district" not in skip:
                out = [r for r in out if (r["place"]["district"] or "") == district_f]
            return out

        sel = narrow(rows)
        # Each control counts as if its own choice were not made, so every
        # figure equals the page it opens; the selection's own figures come
        # from `sel`.
        sev_pool = narrow(rows, skip=("sev",))
        src_pool = narrow(rows, skip=("src",))
        topic_pool = narrow(rows, skip=("topic",))
        place_pool = narrow(rows, skip=("loc", "district"))
        kind_pool = narrow(rows, skip=("kind",))
        matrix_pool = narrow(rows, skip=("entity", "topic"))
        kind_matrix_pool = narrow(rows, skip=("kind", "topic"))
        state_pool = narrow(rows, skip=("loc", "district", "topic"))

        by_sev = Counter(r["severity_shown"] for r in sev_pool)
        by_bucket = Counter(r["place"]["bucket"] for r in place_pool)
        by_district = Counter(r["place"]["district"] for r in place_pool
                              if r["place"]["district"])
        social_sel = [r for r in sel if r["source_type"] == "social"]
        social_complainants, social_attributed = complainant_count(social_sel)
        social_n = sum(1 for r in src_pool if r["source_type"] == "social")
        news_n = len(src_pool) - social_n
        # A complaint whose district is known but whose state is not was
        # still placed: the districts panel prints it by name, and a tile
        # that called it unplaced would contradict the panel beside it.
        located = sum(1 for r in sel
                      if r["place"]["state"] or r["place"]["district"])
        topics_seen = {t for r in sel for t in r["complaint_topics"]}

        by_entity = lambda r: r["entity_id"]                    # noqa: E731
        by_ekind = lambda r: (r["entity_kind"] or None)         # noqa: E731
        by_state = lambda r: (r["place"]["state"] or None)      # noqa: E731
        scale = _heat_scale([matrix_pool, kind_matrix_pool, state_pool],
                            [by_entity, by_ekind, by_state], topics)
        entity_heat = _heat_matrix(
            matrix_pool, by_entity, lambda k: names.get(k, "\u2014"),
            topics, link, "entity", on_key=int(ent_f) if ent_f else None,
            on_topic=topic_f, scale=scale)
        kind_heat = _heat_matrix(
            kind_matrix_pool, by_ekind, lambda k: k, topics, link, "kind",
            on_key=kind_f or None, on_topic=topic_f, scale=scale)
        state_heat = _heat_matrix(
            state_pool, by_state, lambda k: k, topics, link, "loc", limit=12,
            on_key=loc_f or None, on_topic=topic_f, scale=scale)

        # A location table that hid what it could not place would
        # understate every other figure beside it, so the honest buckets
        # are rows of it like any state.
        max_bucket = max(by_bucket.values()) if by_bucket else 0
        places = [{"bucket": b, "n": by_bucket[b],
                   "href": link(loc=b, district=None),
                   "pct": round(100 * by_bucket[b] / max_bucket) if max_bucket else 0,
                   "known": b not in UNPLACED, "on": b == loc_f}
                  for b in _place_choices(place_pool)]

        prev_win, prev_total = _previous_window(win), None
        if prev_win:
            prev_rows = _complaint_rows(db, entities, prev_win)
            if loc_f or district_f:
                # only the location filters need a place, and resolving one
                # means scanning the story
                for it in prev_rows:
                    it["place"] = _complaint_place(it)
            prev_total = len(narrow(prev_rows)) if (loc_f or district_f) else len(
                narrow(prev_rows, skip=("loc", "district")))

        chips = []
        if kind_f:
            chips.append(("Entity type", kind_f, link(kind=None)))
        if ent_f:
            chips.append(("Entity", names.get(int(ent_f), ent_f), link(entity=None)))
        if topic_f:
            chips.append(("Type", topic_f, link(topic=None)))
        if loc_f:
            chips.append(("Location", loc_f, link(loc=None)))
        if district_f:
            chips.append(("District", district_f, link(district=None)))
        if src_f:
            chips.append(("Source", "Social media" if src_f == "social"
                          else "News & official", link(src=None)))
        if sev_f:
            chips.append(("Severity", sev_f, link(sev=None)))

        # Newest first within a severity, and an undated forum post sorts
        # by when it was collected rather than falling to the bottom.
        shown = sorted(sel, key=lambda r: r["published_at"] or r["created_at"] or "",
                       reverse=True)
        shown.sort(key=lambda r: taxonomy.SEVERITY_RANK.get(r["severity_shown"], 3))
        listed = shown[:60]

        by_kind = [{"kind": k, "href": link(kind=k, entity=None),
                    "on": k == kind_f,
                    "n": sum(1 for r in kind_pool if r["entity_kind"] == k)}
                   for k in kinds]
        by_kind.sort(key=lambda c: -c["n"])

        by_topic = [{"topic": t, "href": link(topic=t),
                     "on": t == topic_f,
                     "n": sum(1 for r in topic_pool if t in r["complaint_topics"])}
                    for t in topics]
        by_topic.sort(key=lambda c: -c["n"])

        return render(request, "complaints.html", user=user, win=win,
                      entities=entities, office=office, link=link,
                      topics=topics, chips=chips,
                      entity_f=ent_f, topic_f=topic_f, loc_f=loc_f,
                      kinds=kinds, kind_f=kind_f, by_kind=by_kind,
                      kind_entities=[e for e in entities
                                     if not kind_f or e["kind"] == kind_f
                                     or str(e["id"]) == ent_f],
                      district_f=district_f, src_f=src_f, sev_f=sev_f,
                      total=len(sel), scope_total=len(rows),
                      # Only social posts can name a complainant: a news
                      # article has a publication, not an aggrieved
                      # customer. Counting news here forced the figure to
                      # equal the complaint total on a news-heavy record,
                      # which read as "no duplicates" when it meant
                      # "nothing measured".
                      social_total=len(social_sel),
                      complainants=social_complainants,
                      attributed=social_attributed,
                      prev_total=prev_total,
                      by_sev=by_sev, entities_hit=len({r["entity_id"] for r in sel}),
                      topics_seen=len(topics_seen), located=located,
                      social_n=social_n, news_n=news_n, by_topic=by_topic,
                      prev_label=_period_before(win),
                      entity_heat=entity_heat, kind_heat=kind_heat,
                      state_heat=state_heat,
                      places=places,
                      districts=by_district.most_common(14),
                      district_total=len(by_district),
                      listed=listed, listed_more=max(0, len(sel) - len(listed)),
                      loc_choices=_place_choices(place_pool)
                      + ([loc_f] if loc_f and loc_f not in
                         _place_choices(place_pool) else []))
    finally:
        db.close()


@app.get("/dashboard")
def dashboard(request: Request):
    db = connect()
    try:
        user = require_login(db, request)
        entity, entities = resolve_entity(db, user, request.query_params.get("entity"))
        if entity is None:
            # The dashboard reads one entity at a time. A link carrying the
            # cross-entity "all" -- the queue's scope, say, followed here by
            # the tab bar -- lands on the same entity a bare visit would,
            # rather than erroring.
            entity = entities[0]
        win = date_window(request)
        stats = _entity_stats(db, entity["id"], win)
        return render(request, "dashboard.html", user=user, entity=entity,
                      entities=entities, stats=stats, win=win)
    finally:
        db.close()


def _category_rows(db, entities, key_fn, categories, win=None):
    """Group every classified item by a category instead of by entity.

    `key_fn` returns the categories one item belongs to -- one for severity,
    zero or more for risk areas. Each row keeps its per-entity split so every
    number still leads to the items behind it: the queue is per entity, so a
    cross-entity total that could not be opened would break the rule that
    every figure on a dashboard is a drill-down.

    Items still awaiting classification are excluded. They carry no verdict,
    and counting them would file every one of them under 'low'.
    """
    by_cat = {c: {"total": 0, "high": 0, "open": 0, "last": None,
                  "per_entity": Counter(), "open_per_entity": Counter()}
              for c in categories}
    names = {e["id"]: e["name"] for e in entities}
    win_sql, win_args = date_sql(win or {"key": ""})
    win_and = f" AND {win_sql}" if win_sql else ""

    for e in entities:
        rows = [prep_item(r) for r in q(
            db, "SELECT * FROM items WHERE entity_id = ? AND gated_out = 0"
                f" AND status != 'new' AND source_type != 'social'{win_and}",
            (e["id"], *win_args))]
        for it in rows:
            awaiting = it["status"] == "classified"
            for cat in key_fn(it):
                if cat not in by_cat:
                    continue
                bucket = by_cat[cat]
                bucket["total"] += 1
                bucket["per_entity"][e["id"]] += 1
                if it["severity_shown"] == "high":
                    bucket["high"] += 1
                if awaiting:
                    bucket["open"] += 1
                    bucket["open_per_entity"][e["id"]] += 1
                published = it["published_at"] or ""
                if published and (bucket["last"] or "") < published:
                    bucket["last"] = published

    out = []
    for cat in categories:
        b = by_cat[cat]
        out.append({
            "category": cat,
            "total": b["total"],
            "high": b["high"],
            "open": b["open"],
            "last": b["last"],
            "entities": [{"id": eid, "name": names[eid], "count": n}
                         for eid, n in b["per_entity"].most_common()],
        })
    return out


@app.get("/overview")
def overview(request: Request):
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        entities = q(db, "SELECT * FROM entities ORDER BY name")
        kinds = _present_kinds(entities)
        kind = request.query_params.get("kind", "")
        if kind and kind not in kinds:
            kind = ""
        if kind:
            entities = [e for e in entities if e["kind"] == kind]
        win = date_window(request)
        win_sql, win_args = date_sql(win)
        win_and = f" AND {win_sql}" if win_sql else ""
        rows = []
        for e in entities:
            items = [prep_item(r) for r in q(
                db, "SELECT * FROM items WHERE entity_id=?"
                    f" AND gated_out = 0 AND source_type != 'social'{win_and}",
                (e["id"], *win_args))]
            by_risk = Counter(a for it in items for a in it["risk_areas_shown"])
            top_risk = by_risk.most_common(1)
            open_count = one(db, "SELECT COUNT(*) n FROM items WHERE entity_id=? AND"
                                 " status IN ('new','classified') AND gated_out = 0"
                                 f" AND source_type != 'social'{win_and}",
                             (e["id"], *win_args))["n"]
            # "Latest item" answers "when did anything last land", so it
            # reads the whole record even inside a window -- an empty
            # column would otherwise say a bank went quiet when it did not.
            last = one(db, "SELECT MAX(published_at) m FROM items WHERE entity_id=?"
                           " AND source_type != 'social'",
                       (e["id"],))["m"]
            # Only a classified item has a verdict. Counting the rest as
            # "low" -- which is what reading severity off an unclassified
            # row does -- would quietly understate a backlog, so they get a
            # band of their own in the mix.
            by_sev = Counter(it["severity_shown"] for it in items if it["classified"])
            pending = sum(1 for it in items if not it["classified"])
            rows.append({
                "entity": e,
                "total": len(items),
                "high": by_sev.get("high", 0),
                # the whole severity split, so a row can show its mix rather
                # than only the count of the worst band
                "mix": dict({s: by_sev.get(s, 0) for s in taxonomy.SEVERITIES},
                            pending=pending),
                "complaints": sum(1 for it in items if it["complaint_topics"]),
                "open": open_count,
                "top_risk": top_risk[0][0] if top_risk else "—",
                "last": last,
            })
        rows.sort(key=lambda r: (-r["high"], -r["total"]))
        # The page's own totals, for the tiles: a supervisor opening the
        # leftmost screen asks "how much is there, and how bad" before
        # asking it entity by entity.
        totals = {
            "entities": len(rows),
            # not "items": Jinja resolves totals.items to dict.items()
            "n_items": sum(r["total"] for r in rows),
            "high": sum(r["high"] for r in rows),
            "open": sum(r["open"] for r in rows),
            "complaints": sum(r["complaints"] for r in rows),
        }

        # The same record, grouped three ways. Entity answers "who needs
        # attention", severity "how bad is it", risk "what kind of problem
        # keeps showing up" -- questions a supervisor asks separately.
        view = request.query_params.get("view") or "entity"
        if view not in ("entity", "severity", "risk"):
            view = "entity"
        sev_rows = risk_rows = None
        if view == "severity":
            sev_rows = _category_rows(
                db, entities,
                lambda it: [it["severity_shown"]], taxonomy.SEVERITIES, win)
        elif view == "risk":
            risk_rows = [r for r in _category_rows(
                db, entities,
                lambda it: it["risk_areas_shown"], taxonomy.RISK_AREAS, win)]
            risk_rows.sort(key=lambda r: (-r["high"], -r["total"], r["category"]))
        iwin_sql, iwin_args = date_sql(win, "i")
        iwin_and = f" AND {iwin_sql}" if iwin_sql else ""
        if kind:
            unclassified = one(db, "SELECT COUNT(*) n FROM items i"
                                   " JOIN entities e ON e.id = i.entity_id"
                                   " WHERE i.status = 'new' AND i.gated_out = 0"
                                   " AND i.source_type != 'social'"
                                   f" AND e.kind = ?{iwin_and}",
                               (kind, *iwin_args))["n"]
        else:
            unclassified = one(db, "SELECT COUNT(*) n FROM items i"
                                   " WHERE i.status = 'new' AND i.gated_out = 0"
                                   f" AND i.source_type != 'social'{iwin_and}",
                               tuple(iwin_args))["n"]
        return render(request, "overview.html", user=user, rows=rows, view=view,
                      sev_rows=sev_rows, risk_rows=risk_rows, win=win,
                      totals=totals,
                      unclassified=unclassified, kinds=kinds, kind=kind)
    finally:
        db.close()


# --- alerts and classification policy ---------------------------------------
# Two screens, one subject: what the classifier is told to look for. Alerts
# are the named things this team wants flagged; the policy texts are the
# standing definitions every item is judged against. They were one page
# until the alerts outgrew it. The stored table is still `factors` -- the
# word changed on screen, not in anyone's database.


def _alert_rows(db, user) -> list:
    """The alerts this reader may see, each carrying what it has caught."""
    if user["role"] == "superadmin":
        rows = q(db, "SELECT f.*, e.name AS entity_name, u.display_name AS author"
                     " FROM factors f LEFT JOIN entities e ON e.id = f.entity_id"
                     " LEFT JOIN users u ON u.id = f.created_by ORDER BY f.entity_id IS NULL DESC, f.name")
    else:
        rows = q(db, "SELECT f.*, e.name AS entity_name, u.display_name AS author"
                     " FROM factors f LEFT JOIN entities e ON e.id = f.entity_id"
                     " LEFT JOIN users u ON u.id = f.created_by"
                     " WHERE (f.entity_id IS NULL AND f.entity_kind IS NULL)"
                     "    OR f.entity_id = ?"
                     "    OR f.entity_kind = (SELECT kind FROM entities WHERE id = ?)"
                     " ORDER BY f.entity_id IS NULL DESC, f.name",
                 (user["entity_id"], user["entity_id"]))
    # What each alert has actually caught, split by workstream, so the
    # list reads as a watch list rather than a rulebook. Counts are
    # scoped to what this viewer's links can open: the super admin sees
    # the whole record, everyone else their own entity's slice.
    rows = [dict(r) for r in rows]
    scope_sql, scope_args = "", ()
    if user["role"] != "superadmin" and user["entity_id"]:
        scope_sql, scope_args = " AND entity_id = ?", (user["entity_id"],)
    news_n, social_n = Counter(), Counter()
    # gated_out is the only cut, so each count equals what its link
    # opens: the queue's "All" tab for news, the social tab's default
    # view for posts
    for it in q(db, "SELECT entity_id, source_type, factor_matches,"
                    "       set_aside,"
                    "       COALESCE(review_complaint_topics,"
                    "                complaint_topics, '[]') AS topics_shown"
                    " FROM items WHERE gated_out = 0"
                    f"   AND factor_matches != '[]'{scope_sql}", scope_args):
        try:
            names = json.loads(it["factor_matches"] or "[]")
        except (TypeError, ValueError):
            continue
        if it["source_type"] == "social":
            # count what the Social media tab's default view lists: a
            # grievance, not set aside -- so the linked count matches
            # the screen it opens
            if it["set_aside"] or it["topics_shown"] == "[]":
                continue
            tally = social_n
        else:
            tally = news_n
        for n in names:
            tally[(it["entity_id"], n)] += 1
    kind_of = {r["id"]: r["kind"] for r in q(db, "SELECT id, kind FROM entities")}

    def counts(f, tally):
        # each alert counts only where it actually applies, so the figure
        # equals the list its link opens
        return sum(v for (eid, n), v in tally.items() if n == f["name"] and (
            eid == f["entity_id"] if f["entity_id"]
            else kind_of.get(eid) == f["entity_kind"] if f["entity_kind"]
            else True))

    for f in rows:
        f["news_matches"] = counts(f, news_n)
        f["social_matches"] = counts(f, social_n)
    return rows


@app.get("/alerts")
def alerts_page(request: Request):
    db = connect()
    try:
        user = require_login(db, request)
        # only kinds the roster actually holds: an alert aimed at a kind
        # nobody supervises would be a rule that can never fire
        kinds = q(db, "SELECT kind, COUNT(*) AS n FROM entities"
                      " GROUP BY kind ORDER BY kind")
        return render(request, "alerts.html", user=user,
                      factors=_alert_rows(db, user), kinds=kinds,
                      # the durable record of a walk: a 20-second pop-up
                      # is easy to miss at the end of an hour-long run, so
                      # the page itself says where the last one stands
                      recheck_job=_latest_job(RECHECK_LABEL))
    finally:
        db.close()


@app.get("/policy")
def policy_page(request: Request):
    db = connect()
    try:
        user = require_login(db, request)
        return render(request, "policy.html", user=user,
                      severity_defs=get_setting(db, SEVERITY_DEFS_KEY,
                                                DEFAULT_SEVERITY_DEFS),
                      grievance_severity=get_setting(db, GRIEVANCE_SEVERITY_KEY,
                                                     DEFAULT_GRIEVANCE_SEVERITY),
                      risk_defs=get_setting(db, RISK_DEFS_KEY, DEFAULT_RISK_DEFS),
                      exclusion_rules=get_setting(db, EXCLUSION_RULES_KEY,
                                                  DEFAULT_EXCLUSION_RULES),
                      trusted_sources=get_setting(db, TRUSTED_SOURCES_KEY,
                                                  DEFAULT_TRUSTED_SOURCES),
                      rescore_job=_latest_job("Re-scoring complaints"))
    finally:
        db.close()


@app.get("/factors")
def factors_moved(request: Request):
    """Where this screen used to live. A bookmark from before the split
    lands on the policy texts, which link on to the alerts."""
    return RedirectResponse("/policy", status_code=302)


@app.post("/alerts")
async def alerts_add(request: Request):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "lead", "superadmin")
        name = (form.get("name") or "").strip()
        conditions = (form.get("conditions") or "").strip()
        scope = form.get("scope", "entity")
        if not name or not conditions:
            raise HTTPException(400, "Alert name and conditions are required")
        # Scope, from the form: "entity" is the author's own; "global" is
        # every entity; "kind:<name>" is every entity of that kind. The
        # last two reach beyond the author's own team, so both are the
        # super admin's to set -- a lead may not write a rule that fires
        # on another team's entities.
        entity_id, entity_kind = user["entity_id"], None
        if scope == "global" or scope.startswith("kind:"):
            if user["role"] != "superadmin":
                raise HTTPException(
                    403, "Only the super admin creates alerts beyond one entity")
            entity_id = None
            if scope.startswith("kind:"):
                entity_kind = scope[5:]
                if entity_kind not in taxonomy.ENTITY_KINDS:
                    raise HTTPException(400, "Unknown entity type")
        x(db, "INSERT INTO factors (entity_id, entity_kind, name, conditions,"
              " created_by) VALUES (?,?,?,?,?)",
          (entity_id, entity_kind, name, conditions, user["id"]))
    finally:
        db.close()
    return RedirectResponse("/alerts?msg=Alert+added", status_code=303)


@app.post("/settings/risk")
async def settings_risk(request: Request):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        text = " ".join((form.get("risk_defs") or "").split())
        if not text:
            raise HTTPException(400, "Risk definitions cannot be empty")
        set_setting(db, RISK_DEFS_KEY, text, user["id"])
    finally:
        db.close()
    return RedirectResponse(
        "/policy?msg=Risk+definitions+updated+—+applies+to+new+classifications",
        status_code=303)


@app.post("/settings/severity")
async def settings_severity(request: Request):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        text = " ".join((form.get("severity_defs") or "").split()).rstrip(".")
        if not text:
            raise HTTPException(400, "Severity definitions cannot be empty")
        set_setting(db, SEVERITY_DEFS_KEY, text, user["id"])
    finally:
        db.close()
    return RedirectResponse(
        "/policy?msg=Severity+criteria+updated+—+applies+to+new+classifications",
        status_code=303)


@app.post("/settings/grievance-severity")
async def settings_grievance_severity(request: Request):
    """The second severity scale: customer grievances only. Line breaks
    are kept -- the scale reads as three bands, and flattening them to one
    line would make the one text a supervisor edits most often the one
    hardest to read."""
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        text = (form.get("grievance_severity") or "").strip()
        if not text:
            raise HTTPException(400, "The grievance severity scale cannot be empty")
        set_setting(db, GRIEVANCE_SEVERITY_KEY, text, user["id"])
    finally:
        db.close()
    return RedirectResponse(
        "/policy?msg=Grievance+severity+scale+updated+—+applies+to+new+"
        "classifications.+Use+Re-score+to+apply+it+to+stored+complaints",
        status_code=303)


RECHECK_LABEL = "Checking items against alerts"


def _job_progress(job: dict):
    """The running note a long walk keeps up to date, so its own screen
    can say where it stands instead of leaving a silent hour."""
    def step(i: int, total: int) -> None:
        job["note"] = f"on item {i} of {total}"
    return step


def _running(label: str) -> bool:
    return any(j.get("label") == label and j.get("state") == "running"
               for j in FETCH_JOBS.values())


def _latest_job(label: str) -> dict | None:
    found = None
    for j in FETCH_JOBS.values():
        if j.get("label") == label:
            found = j
    return found


def _rescore_job(job_id: str) -> None:
    job = FETCH_JOBS.get(job_id)
    if job is None:
        return
    db = connect()
    try:
        result = rescore_grievances(db, progress=_job_progress(job))
        if result["failed"] and not result["scored"]:
            job.update(state="failed",
                       note=f"none of the {result['failed']} complaints could "
                            "be scored — is the Anthropic API key set?")
            return
        bits = [f"{result['scored']} complaint(s) re-scored",
                f"{result['changed']} changed severity"]
        if result["failed"]:
            bits.append(f"{result['failed']} could not be scored")
        if result["left_out"]:
            bits.append(f"{result['left_out']} not attempted (per-run cap; "
                        "run again for the rest)")
        job.update(state="done", note=", ".join(bits))
    except Exception as exc:
        log.exception("Re-score job %s failed", job_id)
        job.update(state="failed", note=f"{type(exc).__name__}: {exc}")
    finally:
        db.close()


@app.post("/policy/rescore")
async def policy_rescore(request: Request):
    """Walk the stored complaints and re-score each against the grievance
    severity scale now in force. One model call per complaint, so it runs
    in the background like a fetch and reports through the same toast.
    Touches only the classifier's severity; reviewers' corrections stand.
    """
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
    finally:
        db.close()
    # one at a time: with no guard, a second press while the first run is
    # quietly working would double the model spend for the same answer
    if _running("Re-scoring complaints"):
        return RedirectResponse(
            "/policy?msg=A+re-score+is+already+running+—+its+progress+"
            "shows+under+the+button", status_code=303)
    job_id = secrets.token_hex(8)
    while len(FETCH_JOBS) >= FETCH_JOBS_MAX:
        FETCH_JOBS.pop(next(iter(FETCH_JOBS)))
    FETCH_JOBS[job_id] = {"state": "running",
                          "label": "Re-scoring complaints", "note": ""}
    _spawn(asyncio.to_thread(_rescore_job, job_id))
    return RedirectResponse(
        "/policy?msg=Re-scoring+started+—+progress+shows+under+the+button,"
        f"+and+a+notice+pops+up+when+it+finishes&job={job_id}",
        status_code=303)


def _recheck_job(job_id: str) -> None:
    job = FETCH_JOBS.get(job_id)
    if job is None:
        return
    db = connect()
    try:
        result = recheck_factors(db, progress=_job_progress(job))
        if result["failed"] and not result["checked"]:
            job.update(state="failed",
                       note=f"none of the {result['failed']} items could be "
                            "checked — is the Anthropic API key set?")
            return
        bits = [f"{result['checked']} item(s) checked against the alerts",
                f"{result['changed']} changed flags"]
        if result["failed"]:
            bits.append(f"{result['failed']} could not be checked")
        if result["left_out"]:
            bits.append(f"{result['left_out']} not attempted (per-run cap; "
                        "run again for the rest)")
        job.update(state="done", note=", ".join(bits))
    except Exception as exc:
        log.exception("Alert re-check job %s failed", job_id)
        job.update(state="failed", note=f"{type(exc).__name__}: {exc}")
    finally:
        db.close()


@app.post("/alerts/recheck")
async def alerts_recheck(request: Request):
    """Walk the stored items and flag each against the alerts active now.
    One model call per item (none where an entity has no active alerts),
    so it runs in the background like a fetch and reports through the same
    toast. Touches only the classifier's factor_matches column.
    """
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
    finally:
        db.close()
    # one at a time, for the same reason as the re-score above
    if _running(RECHECK_LABEL):
        return RedirectResponse(
            "/alerts?msg=A+re-check+is+already+running+—+its+progress+"
            "shows+under+the+button", status_code=303)
    job_id = secrets.token_hex(8)
    while len(FETCH_JOBS) >= FETCH_JOBS_MAX:
        FETCH_JOBS.pop(next(iter(FETCH_JOBS)))
    FETCH_JOBS[job_id] = {"state": "running",
                          "label": RECHECK_LABEL, "note": ""}
    _spawn(asyncio.to_thread(_recheck_job, job_id))
    return RedirectResponse(
        "/alerts?msg=Alert+re-check+started+—+progress+shows+under+the+"
        f"button,+and+a+notice+pops+up+when+it+finishes&job={job_id}",
        status_code=303)


@app.post("/settings/exclusions")
async def settings_exclusions(request: Request):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        text = (form.get("exclusion_rules") or "").strip()
        if not text:
            raise HTTPException(400, "The negative list cannot be empty; "
                                     "describe at least one excluded item type")
        set_setting(db, EXCLUSION_RULES_KEY, text, user["id"])
    finally:
        db.close()
    return RedirectResponse(
        "/policy?msg=Negative+list+updated+—+applies+to+items+fetched+from+now+on",
        status_code=303)


@app.post("/settings/trusted")
async def settings_trusted(request: Request):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        text = (form.get("trusted_sources") or "").strip()
        if not text:
            raise HTTPException(400, "List at least one trusted source")
        set_setting(db, TRUSTED_SOURCES_KEY, text, user["id"])
        changed = recompute_source_tiers(db)
    finally:
        db.close()
    return RedirectResponse(
        f"/policy?msg=Trusted+sources+saved+—+{changed}+item(s)+re-tiered",
        status_code=303)


@app.post("/alerts/{factor_id}/toggle")
def alerts_toggle(request: Request, factor_id: int):
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "lead", "superadmin")
        f = one(db, "SELECT * FROM factors WHERE id = ?", (factor_id,))
        if not f:
            raise HTTPException(404, "Alert not found")
        if user["role"] != "superadmin" and f["entity_id"] != user["entity_id"]:
            raise HTTPException(403, "Not your team's alert")
        x(db, "UPDATE factors SET active = 1 - active WHERE id = ?", (factor_id,))
    finally:
        db.close()
    return RedirectResponse("/alerts?msg=Alert+updated", status_code=303)


# --- entities & ingestion ---------------------------------------------------

@app.get("/entities")
def entities_page(request: Request):
    db = connect()
    try:
        user = require_login(db, request)
        entities = visible_entities(db, user)
        rows = []
        for e in entities:
            n_items = one(db, "SELECT COUNT(*) n FROM items WHERE entity_id=?", (e["id"],))["n"]
            last_fetch = one(db, "SELECT * FROM fetch_log WHERE entity_id=?"
                                 " ORDER BY id DESC LIMIT 1", (e["id"],))
            # NOT "items": Jinja resolves r.items to dict.items (the method)
            # before it looks for the key, and renders the bound method.
            rows.append({"entity": e, "aliases": json.loads(e["aliases"]),
                         "languages": json.loads(e["languages"] or '["en"]'),
                         "n_items": n_items, "last_fetch": last_fetch})
        # latest status per broadcast feed (RBI, NSE, BSE) -- logged with a
        # NULL entity because one fetch serves every entity
        broadcast, seen = [], set()
        for r in q(db, "SELECT * FROM fetch_log WHERE entity_id IS NULL"
                       " ORDER BY id DESC LIMIT 30"):
            if r["source"] not in seen:
                seen.add(r["source"])
                broadcast.append(r)
        social_default = tuning.load(db)["social_lookback_days"]
        social_choices = (LOOKBACK_CHOICES if social_default in LOOKBACK_CHOICES
                          else tuple(sorted(set(LOOKBACK_CHOICES) | {social_default})))
        return render(request, "entities.html", user=user, rows=rows,
                      broadcast=broadcast, fetch_minutes=FETCH_MINUTES,
                      lookback_choices=LOOKBACK_CHOICES, lookback_default=LOOKBACK_DAYS,
                      social_choices=social_choices, social_default=social_default,
                      x_choices=X_LOOKBACK_CHOICES)
    finally:
        db.close()


def _parse_languages(raw: str | None) -> list[str]:
    """Comma-separated codes, keeping only editions Google News publishes and
    the order given. Always at least English -- an entity with no language
    would silently fetch nothing."""
    codes, seen = [], set()
    for c in (raw or "").replace(";", ",").split(","):
        c = c.strip().lower()
        if c in NEWS_EDITIONS and c not in seen:
            seen.add(c)
            codes.append(c)
    return codes or ["en"]


def _parse_aliases(raw: str | None) -> list[str]:
    """Aliases from a form, split on commas AND newlines.

    The edit box is a textarea, and a textarea invites one-name-per-line;
    splitting on commas alone silently glued two names into a single alias
    that matched nothing -- and the variant derivation then faithfully
    produced spelling variants of the glued garbage. Order is preserved,
    inner whitespace collapsed, case-insensitive duplicates dropped."""
    out, seen = [], set()
    for part in re.split(r"[,\r\n]+", raw or ""):
        part = " ".join(part.split())
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return out


@app.get("/entities/new")
def entities_new(request: Request):
    """The add-entity screen: name plus headquarters, and the RBI office
    resolves itself from the district. A two-office state offers both."""
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        return templates.TemplateResponse(request, "new_entity.html", {
            "user": user, "kinds": taxonomy.ENTITY_KINDS,
            "place_index": json.dumps(geography.place_index(),
                                      ensure_ascii=False),
            "districts": geography.all_districts(),
            "form": {}, "candidates": [], "need_choice": False,
            "unknown_district": False, "lookup": None,
        })
    finally:
        db.close()


@app.post("/entities/hq")
async def entities_hq(request: Request):
    """The add-entity screen's headquarters lookup: entity name in, the
    district found on the live web out -- verified against the geography
    tables before anything is filled in."""
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
    finally:
        db.close()
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "An entity name is required first")
    try:
        result = hq_lookup.lookup_headquarters(name, form.get("kind") or "")
    except hq_lookup.LookupUnavailable as exc:
        return JSONResponse({"found": False, "district": None,
                             "note": str(exc)})
    if result.get("district"):
        result["offices"] = geography.offices_for_district(result["district"])
    return JSONResponse(result)


@app.post("/entities/new")
async def entities_new_post(request: Request):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        name = (form.get("name") or "").strip()
        kind = form.get("kind") or taxonomy.ENTITY_KINDS[0]
        district = (form.get("district") or "").strip()
        manual = (form.get("office_manual") or "").strip()
        if not name:
            raise HTTPException(400, "Entity name is required")
        if kind not in taxonomy.ENTITY_KINDS:
            raise HTTPException(400, "Unknown entity kind")
        if one(db, "SELECT 1 x FROM entities WHERE lower(name) = ?", (name.lower(),)):
            raise HTTPException(400, "An entity with this name already exists")

        def ask_again(lookup=None, unknown=False):
            candidates = (geography.offices_for_district(district)
                          if district else [])
            return templates.TemplateResponse(request, "new_entity.html", {
                "user": user, "kinds": taxonomy.ENTITY_KINDS,
                "place_index": json.dumps(geography.place_index(),
                                          ensure_ascii=False),
                "districts": geography.all_districts(),
                "form": {"name": name, "kind": kind, "district": district},
                "candidates": candidates,
                "need_choice": bool(candidates),
                "unknown_district": bool(district) and not candidates,
                "lookup": lookup,
            })

        # The "Find headquarters online" button without JavaScript: run
        # the lookup here and show the same screen with the result.
        if form.get("action") == "lookup":
            try:
                found = hq_lookup.lookup_headquarters(name, kind)
            except hq_lookup.LookupUnavailable as exc:
                found = {"found": False, "note": str(exc)}
            if found.get("district"):
                district = found["district"]
            return ask_again(lookup=found)

        # The headquarters is always a district from the known list.
        if district:
            canon = geography.canonical_district(district)
            if not canon and not manual:
                return ask_again(unknown=True)
            district = canon or district

        candidates = geography.offices_for_district(district) if district else []
        allowed = {o for _, offs in candidates for o in offs}
        chosen = [o for o in form.getlist("offices") if o in allowed]
        if not chosen:
            if manual:
                chosen = [manual[:40]]
            elif len(allowed) == 1:
                chosen = list(allowed)
            else:
                # more than one office (or none we know) -- the person
                # decides, on the same screen with everything they typed.
                return ask_again()

        aliases = [name] + derive_aliases([name])
        x(db, "INSERT INTO entities (name, kind, aliases, languages,"
              " rbi_office, hq_district) VALUES (?,?,?,?,?,?)",
          (name, kind, json.dumps(aliases), json.dumps(["en"]),
           ", ".join(chosen) or None, district or None))
    finally:
        db.close()
    return RedirectResponse(
        f"/entities?msg={quote(name + ' added under ' + (', '.join(chosen) or 'no office'))}",
        status_code=303)


@app.post("/entities")
async def entities_add(request: Request):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        name = (form.get("name") or "").strip()
        kind = form.get("kind") or taxonomy.ENTITY_KINDS[0]
        aliases = _parse_aliases(form.get("aliases"))
        if not name:
            raise HTTPException(400, "Entity name is required")
        if name.lower() not in {a.lower() for a in aliases}:
            aliases.insert(0, name)
        # A roster entry carrying only its legal name finds nothing: the
        # press writes "X Co-operative Bank", never "X Co-Operative Bank
        # Ltd.". The mechanical variants are appended, after whatever the
        # team typed, so a human's chosen spellings still lead the query.
        aliases += derive_aliases(aliases)
        if kind not in taxonomy.ENTITY_KINDS:
            raise HTTPException(400, "Unknown entity kind")
        x(db, "INSERT INTO entities (name, kind, aliases, languages) VALUES (?,?,?,?)",
          (name, kind, json.dumps(aliases),
           json.dumps(_parse_languages(form.get("languages")))))
    finally:
        db.close()
    return RedirectResponse("/entities?msg=Entity+added", status_code=303)


def _entity_removal_plan(db, entity_id: int) -> dict:
    """Exactly what disappears with this entity, counted before anything is
    deleted. Removing an entity destroys review history that the rest of the
    app is careful never to overwrite, so the confirmation states the cost
    rather than implying it."""
    n = lambda sql: one(db, sql, (entity_id,))["n"]
    return {
        # NOT "items": Jinja resolves plan.items to dict.items (the method)
        # before it looks for the key, and renders the bound method.
        "stored_items": n("SELECT COUNT(*) n FROM items WHERE entity_id = ?"),
        "reviews": n("SELECT COUNT(*) n FROM reviews r JOIN items i ON i.id = r.item_id"
                     " WHERE i.entity_id = ?"),
        "open_actions": n("SELECT COUNT(*) n FROM items WHERE entity_id = ?"
                          "   AND action_status = 'open'"),
        "factors": n("SELECT COUNT(*) n FROM factors WHERE entity_id = ?"),
        "members": q(db, "SELECT display_name, role FROM users WHERE entity_id = ?"
                         " ORDER BY role, display_name", (entity_id,)),
    }


@app.get("/entities/{entity_id}/delete")
def entity_delete_confirm(request: Request, entity_id: int):
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        entity = one(db, "SELECT * FROM entities WHERE id = ?", (entity_id,))
        if not entity:
            raise HTTPException(404, "Entity not found")
        return render(request, "entity_delete.html", user=user, entity=entity,
                      plan=_entity_removal_plan(db, entity_id))
    finally:
        db.close()


@app.post("/entities/{entity_id}/delete")
async def entity_delete(request: Request, entity_id: int):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        entity = one(db, "SELECT * FROM entities WHERE id = ?", (entity_id,))
        if not entity:
            raise HTTPException(404, "Entity not found")
        # Typing the name is the guard. A misfired click must not be able to
        # destroy an entity's whole supervisory record.
        if (form.get("confirm") or "").strip() != entity["name"]:
            return render(request, "entity_delete.html", user=user, entity=entity,
                          plan=_entity_removal_plan(db, entity_id),
                          error="That does not match the entity name. Nothing was deleted.")
        remove_entity(db, entity_id)
        log.info("Entity %r removed by %s", entity["name"], user["username"])
    finally:
        db.close()
    return RedirectResponse(
        f"/entities?msg={quote(entity['name'] + ' removed')}", status_code=303)


@app.post("/entities/{entity_id}/aliases")
async def entities_aliases(request: Request, entity_id: int):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "lead", "superadmin")
        if user["role"] != "superadmin" and user["entity_id"] != entity_id:
            raise HTTPException(403, "Not your team's entity")
        e = one(db, "SELECT * FROM entities WHERE id = ?", (entity_id,))
        if not e:
            raise HTTPException(404, "Entity not found")
        aliases = _parse_aliases(form.get("aliases"))
        if not aliases:
            raise HTTPException(400, "At least one alias is required")
        aliases += derive_aliases(aliases)
        # The form posts both fields together; when it omits languages, keep
        # whatever the entity already had rather than resetting it to English.
        langs = (_parse_languages(form.get("languages")) if form.get("languages") is not None
                 else json.loads(e["languages"] or '["en"]'))
        # stored bare: the query builder writes "to:handle" itself
        if form.get("x_handle") is not None:
            seen: set[str] = set()
            handles = []
            for raw in re.split(r"[,\s]+", form.get("x_handle") or ""):
                h = raw.strip().lstrip("@")
                if not h:
                    continue
                if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", h):
                    raise HTTPException(
                        400, f"'{h}' is not an X handle: 1-15 letters, digits "
                             "or underscores, separated by commas")
                if h.lower() not in seen:
                    seen.add(h.lower())
                    handles.append(h)
            handle = ", ".join(handles)
        else:
            handle = e["x_handle"] or ""
        office = (form.get("rbi_office") or "").strip() \
            if form.get("rbi_office") is not None else (e["rbi_office"] or "")
        if len(office) > 40:
            raise HTTPException(400, "An office name is at most 40 characters")
        x(db, "UPDATE entities SET aliases = ?, languages = ?, x_handle = ?,"
              " rbi_office = ? WHERE id = ?",
          (json.dumps(aliases), json.dumps(langs), handle or None,
           office or None, entity_id))
    finally:
        db.close()
    return RedirectResponse("/entities?msg=Aliases+updated", status_code=303)


@app.get("/rd")
def rd_view(request: Request):
    """The RD View: one screen per RBI office, two tabs wide. The first
    tab is the entities headquartered in the region -- their news picture
    and their social-media grievances. The second is news about OTHER
    entities that mentions the region's places (states and districts from
    app/geography.py), because a Mumbai-headquartered bank's branch fraud
    in Nagpur belongs on the Nagpur RD's desk too."""
    db = connect()
    try:
        user = require_login(db, request)
        if user["role"] != "superadmin" and not user["rbi_office"]:
            raise HTTPException(
                403, "The RD View is for Regional Directors and the super admin")

        every = q(db, "SELECT * FROM entities ORDER BY name")
        if user["role"] == "superadmin":
            # Every office in the roster, not only those where an entity
            # is headquartered: the in-region tab exists precisely for
            # regions where the entities are NOT headquartered -- a PNB
            # branch fraud in Saharanpur belongs on the Kanpur desk even
            # when nothing is headquartered under Kanpur. Custom office
            # names typed on entities stay listed too.
            offices = sorted(set(RBI_OFFICES)
                             | {o for e in every for o in entity_offices(e)})
            # The dropdown lists offices alphabetically with the Central
            # Office pinned on top -- it supervises the largest entities.
            if "Central Office" in offices:
                offices.remove("Central Office")
                offices.insert(0, "Central Office")
            if any(not entity_offices(e) for e in every):
                offices.append(UNASSIGNED)
        else:
            offices = [user["rbi_office"]]

        # Land on an office that has something to show: the first with an
        # entity headquartered there. The full roster stays one click away.
        hq_offices = {o for e in every for o in entity_offices(e)}
        default_office = next((o for o in offices if o in hq_offices),
                              offices[0] if offices else "")
        selected = request.query_params.get("office") or default_office
        if selected and selected not in offices:
            raise HTTPException(403, "That office is not visible to you")
        # An office explicitly mapped to no states (the Central Office)
        # has no region to scan; a custom office name typed on an entity
        # keeps the narrow name-match behaviour it always had.
        has_region_tab = (bool(selected) and selected != UNASSIGNED
                          and geography.OFFICE_STATES.get(selected) != [])
        tab = request.query_params.get("tab", "hq")
        if tab not in ("hq", "region") or (tab == "region" and not has_region_tab):
            tab = "hq"
        sev = request.query_params.get("sev", "")
        if sev not in ("high", "medium", "low"):
            sev = ""
        ent_filter = request.query_params.get("ent", "")
        sort = request.query_params.get("sort", "sev")
        if sort not in ("sev", "date"):
            sort = "sev"
        kinds = _present_kinds(every)
        kind_f = request.query_params.get("kind", "")
        if kind_f and kind_f not in kinds:
            kind_f = ""
        win = date_window(request)
        win_sql, win_args = date_sql(win)
        win_and = f" AND {win_sql}" if win_sql else ""
        # Which stories this reader has already ticked off. Per user: one
        # Regional Director's reading says nothing about another's.
        read_ids = {r["item_id"] for r in q(
            db, "SELECT item_id FROM item_reads WHERE user_id = ?", (user["id"],))}
        read_f = request.query_params.get("read", "")
        if read_f not in ("unread", "read"):
            read_f = ""

        def _mark(it):
            it["is_read"] = it["id"] in read_ids
            return it

        def _by_read(rows):
            if read_f == "unread":
                return [it for it in rows if not it["is_read"]]
            if read_f == "read":
                return [it for it in rows if it["is_read"]]
            return rows

        if selected == UNASSIGNED:
            ents = [e for e in every if not entity_offices(e)]
        elif selected:
            ents = [e for e in every if selected in entity_offices(e)]
        else:
            ents = []
        if kind_f:
            ents = [e for e in ents if e["kind"] == kind_f]

        rows = []
        sev_counts = Counter()
        # everything in scope before the entity and read filters, so a
        # narrowed tile can say what it is leaving out
        scope_total = 0
        for e in ents:
            items = [prep_item(r) for r in q(
                db, "SELECT * FROM items WHERE entity_id=?"
                    f" AND gated_out = 0 AND source_type != 'social'{win_and}",
                (e["id"], *win_args))]
            # An entity with no news says nothing on an office's page --
            # but the Unassigned bucket is a to-do list, not a news view,
            # so it keeps everything that still needs an office.
            if not items and selected != UNASSIGNED:
                continue
            by_risk = Counter(a for it in items for a in it["risk_areas_shown"])
            for it in items:
                _mark(it)
            # The tiles count what the page is actually showing, so they
            # obey the entity and read filters. They do not obey the
            # severity filter: the tiles are what sets it, and a band that
            # zeroed itself could never be clicked back.
            scope_total += len(items)
            if (not ent_filter) or str(e["id"]) == ent_filter:
                sev_counts.update(it["severity_shown"] for it in _by_read(items))
            shown = items if not sev else [
                it for it in items if it["severity_shown"] == sev]
            unread_here = sum(1 for it in items if not it["is_read"])
            shown = _by_read(shown)
            grievances = [g for g in (prep_item(r) for r in q(
                db, "SELECT * FROM items WHERE entity_id=?"
                    f" AND source_type = 'social' AND gated_out = 0{win_and}",
                (e["id"], *win_args))) if g["complaint_topics"]]
            rows.append({
                "entity": e,
                "total": len(items),
                "high": sum(1 for it in items if it["severity_shown"] == "high"),
                "open": sum(1 for it in items if it["status"] in ("new", "classified")),
                "top_risk": by_risk.most_common(1)[0][0] if by_risk else "\u2014",
                "matching": len(shown),
                "unread": unread_here,
                # Every item, every severity. The default reads high
                # first, newest within each band; "Published date" reads
                # strictly newest-first across severities.
                "recent": (lambda by_date: by_date if sort == "date" else
                           sorted(by_date,
                                  key=lambda it: {"high": 0, "medium": 1}.get(
                                      it["severity_shown"], 2)))(
                    sorted(shown, key=lambda it: it["published_at"] or "",
                           reverse=True)),
                "social_total": len(grievances),
                # every topic with its count, busiest first -- the card
                # shows them as small tiles, not prose
                "social_topics": Counter(
                    t for g in grievances for t in g["complaint_topics"]).most_common(),
            })
        entity_choices = [(r["entity"]["id"], r["entity"]["name"]) for r in rows]
        if ent_filter:
            rows = [r for r in rows if str(r["entity"]["id"]) == ent_filter]
        if sev or read_f:
            rows = [r for r in rows if r["matching"]]
        rows.sort(key=lambda r: (-r["high"], -r["total"]))

        # In-region news from entities headquartered under other offices.
        region_rows = []
        region_sev = Counter()
        region_total = 0
        # the entities that actually turned up in this region, for the
        # picker: the tab lists many banks, so it needs its own roster
        region_entities: dict = {}
        place_terms = geography.office_places(selected) if has_region_tab else []
        exclusions = geography.office_exclusions(selected) if has_region_tab else []
        if tab == "region" and place_terms:
            sel_labels = geography.office_state_labels(selected)
            others = [e for e in every if selected not in entity_offices(e)
                      and (not kind_f or e["kind"] == kind_f)]
            for e in others:
                # A place that is part of the bank's own name proves
                # nothing about where a story happened: every Bank of
                # Maharashtra headline mentions Maharashtra.
                terms = [t for t in place_terms
                         if not place_mentions(e["name"], t)]
                # A sister office's entity -- same state, other office --
                # must not ride in on the bare state name either: every
                # story about a Jalna bank can say "Maharashtra", and one
                # that names no specific in-region place belongs to its
                # own office's desk, not to Mumbai's.
                e_labels = set()
                for o in entity_offices(e):
                    e_labels |= geography.office_state_labels(o)
                shared = sel_labels & e_labels
                if shared:
                    drop = geography.state_label_terms(selected, shared)
                    terms = [t for t in terms if t.lower() not in drop]
                if not terms:
                    continue
                # Merged stories may carry the place only in an attached
                # outlet's headline -- the face that survived clustering
                # need not be the one that named the district -- so the
                # scan reads those headlines too, and the classifier's
                # own geography verdict alongside.
                extra = {r["item_id"]: r["t"] for r in q(
                    db, "SELECT s.item_id, group_concat(COALESCE(s.title,''), ' ') AS t"
                        " FROM item_sources s JOIN items i ON i.id = s.item_id"
                        " WHERE i.entity_id = ? GROUP BY s.item_id",
                    (e["id"],))}
                for r in q(db, "SELECT * FROM items WHERE entity_id=?"
                               " AND gated_out = 0 AND source_type != 'social'"
                               f"{win_and}",
                           (e["id"], *win_args)):
                    it = prep_item(r)
                    text = " ".join(filter(None, (it.get("title"),
                                                  it.get("snippet"),
                                                  it.get("summary"),
                                                  it.get("geography"),
                                                  extra.get(it["id"]))))
                    hit = next((t for t in terms if place_mentions(text, t)), None)
                    # A story that names a sister office's district belongs
                    # there, however loudly it also says the state's name.
                    if hit and any(place_mentions(text, t) for t in exclusions):
                        continue
                    if hit:
                        # every bank that turns up stays in the picker,
                        # whatever the filters currently show
                        region_entities[e["id"]] = e["name"]
                        region_total += 1
                        if ent_filter and str(e["id"]) != ent_filter:
                            continue
                        _mark(it)
                        if read_f and not _by_read([it]):
                            continue
                        region_sev[it["severity_shown"]] += 1
                        if sev and it["severity_shown"] != sev:
                            continue
                        it["region_term"] = hit
                        it["entity_name"] = e["name"]
                        region_rows.append(it)
            region_rows.sort(key=lambda it: it["published_at"] or "", reverse=True)
            if sort == "sev":
                region_rows.sort(key=lambda it: {"high": 0, "medium": 1}.get(
                    it["severity_shown"], 2))
            # the region tab picks from the banks that appear in it, not
            # from the ones headquartered here
            entity_choices = sorted(region_entities.items(), key=lambda kv: kv[1])

        return render(
            request, "rd.html",
            user=user, offices=offices, selected=selected,
            rows=rows, unassigned_label=UNASSIGNED,
            tab=tab, has_region_tab=has_region_tab,
            region_desc=geography.describe(selected) if has_region_tab else "",
            region_rows=region_rows,
            sev=sev, ent_filter=ent_filter, sort=sort, win=win, read_f=read_f,
            tile_total_all=(region_total if tab == "region" else scope_total),
            kinds=kinds, kind=kind_f,
            sev_counts=sev_counts, region_sev=region_sev,
            entity_choices=entity_choices)
    finally:
        db.close()


@app.post("/rd/read")
async def rd_mark_read(request: Request):
    """Tick a story off, or un-tick it, on the RD View.

    A reading mark, not a verdict: it records that this reader has seen
    the item and changes nothing about the item itself. It is stored per
    user, so one office's reading never greys a story out for another.
    """
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        if user["role"] != "superadmin" and not user["rbi_office"]:
            raise HTTPException(
                403, "The RD View is for Regional Directors and the super admin")
        try:
            item_id = int(form.get("item_id") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "An item id is required")
        if not one(db, "SELECT id FROM items WHERE id = ?", (item_id,)):
            raise HTTPException(404, "Item not found")
        if form.get("read"):
            x(db, "INSERT OR IGNORE INTO item_reads (item_id, user_id)"
                  " VALUES (?,?)", (item_id, user["id"]))
        else:
            x(db, "DELETE FROM item_reads WHERE item_id = ? AND user_id = ?",
              (item_id, user["id"]))
    finally:
        db.close()
    # back to the exact screen the tick was made on, filters and all
    back = form.get("back") or "/rd"
    if not back.startswith("/rd"):
        back = "/rd"
    return RedirectResponse(back, status_code=303)


@app.post("/rd/users")
async def rd_create_user(request: Request):
    """A Regional Director login: a user whose beat is an office. Stored
    as role 'member' with rbi_office set, because the role CHECK on
    existing databases cannot grow a new value."""
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        form = await request.form()
        username = (form.get("username") or "").strip().lower()
        display = (form.get("display_name") or "").strip()
        password = form.get("password") or ""
        office = (form.get("rbi_office") or "").strip()
        if not re.fullmatch(r"[a-z0-9_.-]{3,30}", username):
            raise HTTPException(
                400, "Username: 3-30 lower-case letters, digits, . _ -")
        if not display:
            raise HTTPException(400, "A display name is required")
        if len(password) < 8:
            raise HTTPException(400, "Password needs at least 8 characters")
        if not office or office == UNASSIGNED or len(office) > 40:
            raise HTTPException(400, "A real RBI office is required")
        if one(db, "SELECT 1 x FROM users WHERE username = ?", (username,)):
            raise HTTPException(400, "That username is taken")
        x(db, "INSERT INTO users (username, password_hash, display_name, role,"
              " entity_id, rbi_office) VALUES (?,?,?,'member',NULL,?)",
          (username, hash_password(password), display, office))
    finally:
        db.close()
    return RedirectResponse(
        f"/rd?office={quote(office)}&msg=RD+login+created", status_code=303)


@app.post("/fetch")
async def fetch_now(request: Request):
    form = await request.form()
    entity_id = form.get("entity_id")
    # A window chosen for this fetch only. Anything unrecognised falls back to
    # the standing default rather than erroring: a bad value must not be able
    # to turn one Fetch press into a year-wide scan.
    raw_days = (form.get("days") or "").strip()
    allowed = set(LOOKBACK_CHOICES) | set(X_LOOKBACK_CHOICES)
    days = int(raw_days) if raw_days.isdigit() and int(raw_days) in allowed else None
    # News and social are separate buttons; anything unrecognised runs the
    # full fetch, same as before the split.
    channel = form.get("channel") or "all"
    if channel not in CHANNELS:
        channel = "all"
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "lead", "superadmin")
        if entity_id and user["role"] != "superadmin" \
                and str(user["entity_id"]) != str(entity_id):
            raise HTTPException(403, "Not your team's entity")
    finally:
        db.close()
    db = connect()
    try:
        ent_row = one(db, "SELECT name FROM entities WHERE id = ?",
                      (entity_id,)) if entity_id else None
    finally:
        db.close()
    what = ("Social media fetch" if channel == "social"
            else "News fetch" if channel == "news"
            else "X fetch" if channel == "x" else "Fetch")
    label = f"{what} — {ent_row['name'] if ent_row else 'all entities'}"
    job_id = secrets.token_hex(8)
    while len(FETCH_JOBS) >= FETCH_JOBS_MAX:
        FETCH_JOBS.pop(next(iter(FETCH_JOBS)))
    FETCH_JOBS[job_id] = {"state": "running", "label": label, "note": ""}
    _spawn(asyncio.to_thread(_fetch_job, job_id,
                             int(entity_id) if entity_id else None,
                             days, channel))
    db = connect()
    try:
        knobs = tuning.load(db)
    finally:
        db.close()
    if channel == "social":
        msg = (f"Social media fetch started — complaints from the last "
               f"{knobs['social_lookback_days']} days. You will be notified when done.")
    elif channel == "x":
        cap = knobs["x_max_posts"]
        msg = (f"X fetch started — up to {cap} posts from the last 7 days "
               f"(about ${cap * X_PRICE_PER_POST:.2f} of X credits). "
               "You will be notified when done.")
    else:
        window = days or knobs["lookback_days"]
        msg = (f"{what} started — searching the last {window} days. "
               "You will be notified when done.")
    return RedirectResponse(f"/entities?msg={quote(msg)}&job={job_id}",
                            status_code=303)


def _settings_groups() -> list[dict]:
    """The tuning knobs arranged into the subjects a reader thinks in.

    The grouping lives in app/tuning.py beside the settings themselves; a
    key that no group claims lands under "Other", so a setting added there
    always appears on the page even if nobody updates the grouping.
    """
    fields = {key: {"key": key, "kind": "number", "label": label, "help": note,
                    "bounds": bounds, "default": default}
              for key, label, note, bounds, default in tuning.SPEC}
    fields.update({key: {"key": key, "kind": "toggle", "label": label,
                         "help": note, "bounds": None, "default": default}
                   for key, label, note, default in tuning.TOGGLES})
    out, claimed = [], set()
    for title, blurb, keys in tuning.GROUPS:
        rows = [fields[k] for k in keys if k in fields]
        claimed.update(f["key"] for f in rows)
        if rows:
            out.append({"title": title, "blurb": blurb, "rows": rows})
    rest = [f for k, f in fields.items() if k not in claimed]
    if rest:
        out.append({"title": "Other", "blurb": "", "rows": rest})
    return out


# ══ Help ═════════════════════════════════════════════════════════════

def _visible_screens(user) -> dict:
    """Which screens and powers this reader actually has.

    The guide describes only these. Sending someone to look for a tab
    their role does not carry, or a button their role cannot press, is
    worse than saying nothing: they conclude the app is broken. The rules
    here are the same ones the masthead and the route guards apply.
    """
    boss = user["role"] == "superadmin"
    rd_only = bool(user["rbi_office"]) and not boss
    return {
        "overview": boss,
        "dos": not rd_only,
        "rd": boss or bool(user["rbi_office"]),
        "settings": boss,
        "roster": boss,                                    # add / remove entities
        "policy": boss,                                    # the four policy texts
        "alerts": user["role"] in ("lead", "superadmin"),
        "fetch": user["role"] in ("lead", "superadmin"),
        "insights_generate": user["role"] in ("lead", "superadmin"),
    }


@app.get("/help")
def help_page(request: Request):
    """How to use each screen, written for the officer signed in rather
    than for whoever installs the app."""
    db = connect()
    try:
        user = require_login(db, request)
        return render(request, "help.html", user=user,
                      sees=_visible_screens(user))
    finally:
        db.close()


@app.get("/settings")
def settings_page(request: Request):
    """Operational knobs, stored in the database: windows, budgets and
    caps that were env-vars-only before. Superadmin territory."""
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        return render(request, "settings.html",
                      user=user, groups=_settings_groups(),
                      people=_people(db),
                      entities=visible_entities(db, user),
                      demo_open=_demo_passwords_that_still_work(db),
                      public_mode=PUBLIC_MODE,
                      login_failures=recent_login_failures(db),
                      effective=tuning.load(db), overrides=tuning.overrides(db))
    finally:
        db.close()


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        try:
            tuning.save(db, form, user["id"])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    finally:
        db.close()
    return RedirectResponse("/settings?msg=Settings+saved", status_code=303)


# --- accounts ---------------------------------------------------------------
# The prototype shipped with three documented logins and no way to change
# or retire any of them, which is survivable on one laptop and fatal on a
# public address. These screens are the minimum that makes it hostable:
# change your own password, add a colleague, retire someone who has left.

MIN_PASSWORD = 8


def _password_fault(new: str, again: str) -> str | None:
    if len(new) < MIN_PASSWORD:
        return f"A password needs at least {MIN_PASSWORD} characters."
    if new != again:
        return "The two new passwords do not match."
    if new.lower() in {p.lower() for p in DEMO_LOGINS.values()}:
        return "That is one of the demo passwords. Choose another."
    return None


@app.get("/account")
def account_page(request: Request):
    db = connect()
    try:
        user = require_login(db, request)
        return render(request, "account.html", user=user,
                      demo_password=verify_password(
                          DEMO_LOGINS.get(user["username"], "\0"),
                          user["password_hash"]))
    finally:
        db.close()


@app.post("/account/password")
async def account_password(request: Request):
    """Change your own password. The current one is required: a session
    left open on a shared desk must not be enough to lock its owner out."""
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        current = form.get("current") or ""
        if not verify_password(current, user["password_hash"]):
            return render(request, "account.html", user=user,
                          demo_password=False,
                          error="That is not your current password.")
        fault = _password_fault(form.get("new") or "", form.get("again") or "")
        if fault:
            return render(request, "account.html", user=user,
                          demo_password=False, error=fault)
        x(db, "UPDATE users SET password_hash = ? WHERE id = ?",
          (hash_password(form.get("new")), user["id"]))
    finally:
        db.close()
    return RedirectResponse("/account?msg=Password+changed", status_code=303)


def _people(db) -> list:
    return q(db, "SELECT u.*, e.name AS entity_name FROM users u"
                 " LEFT JOIN entities e ON e.id = u.entity_id"
                 " ORDER BY COALESCE(u.disabled, 0), u.role != 'superadmin',"
                 "          u.display_name")


def _live_superadmins(db) -> int:
    return one(db, "SELECT COUNT(*) n FROM users WHERE role = 'superadmin'"
                   " AND COALESCE(disabled, 0) = 0")["n"]


@app.post("/people")
async def people_add(request: Request):
    """Add a colleague. A second super admin is deliberately allowed: one
    administrator who forgets a password is one locked-out application."""
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        username = (form.get("username") or "").strip().lower()
        display = (form.get("display_name") or "").strip()
        role = form.get("role") or "member"
        entity_id = form.get("entity_id") or ""
        if not re.fullmatch(r"[a-z0-9_.-]{3,30}", username):
            raise HTTPException(
                400, "Username: 3-30 lower-case letters, digits, . _ -")
        if not display:
            raise HTTPException(400, "A display name is required")
        if role not in ("member", "lead", "superadmin"):
            raise HTTPException(400, "Unknown role")
        as_guest = 1 if form.get("guest") else 0
        fault = _password_fault(form.get("password") or "",
                                form.get("password") or "")
        if fault:
            raise HTTPException(400, fault)
        if one(db, "SELECT 1 FROM users WHERE username = ?", (username,)):
            raise HTTPException(400, "That username is taken")
        x(db, "INSERT INTO users (username, password_hash, display_name, role,"
              " entity_id, guest) VALUES (?,?,?,?,?,?)",
          (username, hash_password(form.get("password")), display, role,
           int(entity_id) if entity_id.isdigit() else None, as_guest))
    finally:
        db.close()
    return RedirectResponse("/settings?msg=Account+added#people", status_code=303)


@app.post("/people/{uid}/password")
async def people_reset(request: Request, uid: int):
    """Set someone else's password — the way a forgotten one is recovered,
    since the prototype has no email to send a reset link to."""
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        target = one(db, "SELECT * FROM users WHERE id = ?", (uid,))
        if not target:
            raise HTTPException(404, "No such account")
        fault = _password_fault(form.get("password") or "",
                                form.get("password") or "")
        if fault:
            raise HTTPException(400, fault)
        x(db, "UPDATE users SET password_hash = ? WHERE id = ?",
          (hash_password(form.get("password")), uid))
    finally:
        db.close()
    return RedirectResponse("/settings?msg=Password+set#people", status_code=303)


@app.post("/people/{uid}/guest")
async def people_guest(request: Request, uid: int):
    """Turn guest on or off for somebody who already has an account.

    Takes effect on their very next click: the check happens on each
    request rather than at sign-in, so nobody keeps the run of the place
    until they happen to sign out.
    """
    form = await request.form()
    wanted = 1 if form.get("guest") else 0
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        if uid == user["id"] and wanted:
            raise HTTPException(
                400, "Making your own account a guest would leave you unable "
                     "to change it back. Ask another super admin.")
        x(db, "UPDATE users SET guest = ? WHERE id = ?", (wanted, uid))
    finally:
        db.close()
    return RedirectResponse("/settings?msg=Account+updated#people",
                            status_code=303)


@app.post("/people/{uid}/role")
async def people_role(request: Request, uid: int):
    """Change what an existing account is, without deleting it.

    Both fields post here, one cell at a time, so each sends only its own
    and an absent field means "leave that alone" rather than "clear it" --
    which matters for the entity, where empty is itself a value.
    """
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        target = one(db, "SELECT * FROM users WHERE id = ?", (uid,))
        if not target:
            raise HTTPException(404, "No such account")
        if "role" in form:
            role = form.get("role")
            if role not in ("member", "lead", "superadmin"):
                raise HTTPException(400, "Unknown role")
            if target["role"] == "superadmin" and role != "superadmin":
                if uid == user["id"]:
                    raise HTTPException(
                        400, "Stepping your own account down would leave you "
                             "unable to step it back up. Ask another super "
                             "admin.")
                if _live_superadmins(db) <= 1:
                    raise HTTPException(
                        400, "That is the last super admin. Promote somebody "
                             "else first, or the settings become unreachable.")
            x(db, "UPDATE users SET role = ? WHERE id = ?", (role, uid))
        if "entity_id" in form:
            raw = (form.get("entity_id") or "").strip()
            x(db, "UPDATE users SET entity_id = ? WHERE id = ?",
              (int(raw) if raw.isdigit() else None, uid))
    finally:
        db.close()
    return RedirectResponse("/settings?msg=Account+updated#people",
                            status_code=303)


@app.post("/people/{uid}/remove")
async def people_remove(request: Request, uid: int):
    """Retire an account: it can no longer sign in, and the session it
    already holds ends at the next click. Deliberately not a deletion --
    the reviews this person recorded are supervisory record, and their
    name belongs on their own rulings."""
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        target = one(db, "SELECT * FROM users WHERE id = ?", (uid,))
        if not target:
            raise HTTPException(404, "No such account")
        if target["id"] == user["id"]:
            raise HTTPException(400, "You cannot remove your own account")
        if (target["role"] == "superadmin" and not target["disabled"]
                and _live_superadmins(db) <= 1):
            raise HTTPException(
                400, "That is the only super admin left. Add another first, "
                     "or nobody can administer this application.")
        x(db, "UPDATE users SET disabled = 1 WHERE id = ?", (uid,))
    finally:
        db.close()
    return RedirectResponse("/settings?msg=Account+removed#people", status_code=303)


@app.post("/people/{uid}/restore")
def people_restore(request: Request, uid: int):
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        x(db, "UPDATE users SET disabled = 0 WHERE id = ?", (uid,))
    finally:
        db.close()
    return RedirectResponse("/settings?msg=Account+restored#people", status_code=303)


@app.get("/fetch/status")
def fetch_status(request: Request):
    """What the completion toast polls. Unknown ids answer 'unknown'
    rather than erroring, so a browser holding jobs from before a server
    restart just lets go of them."""
    db = connect()
    try:
        require_login(db, request)
    finally:
        db.close()
    job = FETCH_JOBS.get(request.query_params.get("job", ""))
    if not job:
        return JSONResponse({"state": "unknown"})
    return JSONResponse(job)
