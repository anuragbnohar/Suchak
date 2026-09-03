"""Suchak — supervisory intelligence prototype. FastAPI app and routes."""
import asyncio
import json
import logging
import os
import re
import secrets
from urllib.parse import quote
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import (forums, geography, hq_lookup, insights as insights_mod,
               reddit_source, taxonomy, tuning, x_scrape)
from .matching import derive_aliases, place_mentions
from .auth import (get_user, hash_password, require_login, require_role,
                   verify_password)
from .classify import (classify_item,
                       DEFAULT_EXCLUSION_RULES, DEFAULT_RISK_DEFS,
                       DEFAULT_SEVERITY_DEFS,
                       EXCLUSION_RULES_KEY, RISK_DEFS_KEY, SEVERITY_DEFS_KEY,
                       similar_reviewed, suggest_action)
from .db import (connect, get_setting, init_db, one, q, remove_entity,
                 set_setting, x)
from .ingest import (CHANNELS, LOOKBACK_CHOICES, LOOKBACK_DAYS, NEWS_EDITIONS, SOCIAL_LOOKBACK_DAYS,
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = connect()
    try:
        if seed_if_empty(db):
            log.info("Seeded demo entities, users, factors and items")
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


app = FastAPI(title="Suchak", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SUCHAK_SECRET", secrets.token_hex(32)),
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Shown in every page's footer. Stale local files have now cost four
# debugging rounds -- the fix on GitHub, the report from an old copy on
# disk -- so the running build identifies itself where a screenshot
# always includes it. Bump on every user-visible change.
APP_BUILD = "2026-09-04.5"

templates = Jinja2Templates(directory=BASE_DIR / "templates")
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


def render(request: Request, name: str, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx["taxonomy"] = taxonomy
    ctx["request"] = request
    if ctx.get("user") is not None and "todo_count" not in ctx:
        ctx["todo_count"] = _open_action_count(ctx["user"])
    return templates.TemplateResponse(request, name, ctx)


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
    return q(db, "SELECT * FROM entities WHERE id = ? ", (user["entity_id"],))


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

@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html", error=None)


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip().lower()
    password = form.get("password") or ""
    db = connect()
    try:
        user = one(db, "SELECT * FROM users WHERE username = ?", (username,))
        if not user or not verify_password(password, user["password_hash"]):
            return render(request, "login.html", error="Invalid username or password.")
        request.session["uid"] = user["id"]
    finally:
        db.close()
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
        # No rolling window: supervision reads the whole record, and a
        # fraud case four months old is exactly what belongs on screen.
        # A single day is still selectable by clicking the dashboard's
        # activity chart, which is a drill-down, not a filter.

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
        if factor:
            where.append("i.factor_matches LIKE ?")
            params.append(f'%"{factor}"%')
        if org:
            where.append("i.relationships LIKE ?")
            params.append(f'%"name": "{org}"%')
        if src == "trusted":
            where.append("i.source_tier IN ('official','trusted')")
        if complaints or topic:
            where.append("i.complaint_topics != '[]'")
        if topic:
            where.append("i.complaint_topics LIKE ?")
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
            ("complaints", "1" if complaints else ""), ("topic", topic)) if v}
        filter_qs = "".join(f"&{k}={quote(str(v))}" for k, v in extras.items())
        return render(request, "queue.html", user=user, entity=entity,
                      entity_qs="all" if entity is None else entity["id"],
                      office=request.query_params.get("office") or None,
                      entities=entities, items=prepped, grouped=grouped,
                      status=status, risk=risk, counts=counts,
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
        action = form.get("action") or None
        if action not in taxonomy.ACTIONS:
            action = None
        status = "reviewed" if relevant else "dismissed"
        notes = (form.get("notes") or "").strip() or None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        x(db, "UPDATE items SET status=?, reviewed_by=?, reviewed_at=?, review_relevant=?,"
              " review_severity=?, review_risk_areas=?, review_actionable=?, review_action=?,"
              " review_notes=? WHERE id=?",
          (status, user["id"], now, relevant, severity, json.dumps(risk_areas),
           actionable, action, notes, item_id))

        # The items row holds the CURRENT verdict, which the queue, dashboards
        # and learning loop read. This table holds every verdict ever recorded,
        # so a later reviewer can never erase who said what before them.
        x(db, "INSERT INTO reviews (item_id, user_id, created_at, relevant, severity,"
              " risk_areas, actionable, action, notes) VALUES (?,?,?,?,?,?,?,?,?)",
          (item_id, user["id"], now, relevant, severity, json.dumps(risk_areas),
           actionable, action, notes))

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
            msg = (f"Could not generate insights: {type(exc).__name__}: "
                   f"{str(exc)[:120]}")
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

        if entity is None:
            ids = [e["id"] for e in entities]
            scope = f"entity_id IN ({','.join('?' * len(ids))})"
            args = list(ids)
        else:
            scope, args = "entity_id = ?", [entity["id"]]
        rows = [prep_item(r) for r in q(
            db, "SELECT i.*, e.name AS entity_name FROM items i"
                " JOIN entities e ON e.id = i.entity_id"
                f" WHERE i.source_type = 'social' AND i.gated_out = 0 AND i.{scope}",
            tuple(args))]
        # Posts the classifier dropped by what reviewers taught it. Shown
        # on their own tab: a learned gate that no one can inspect is a
        # silent loss, which is the one thing this pipeline never allows.
        learned = [prep_item(r) for r in q(
            db, "SELECT i.*, e.name AS entity_name FROM items i"
                " JOIN entities e ON e.id = i.entity_id"
                " WHERE i.source_type = 'social' AND i.gated_out = 1"
                " AND i.gate_reason LIKE 'matches posts this team set aside%'"
                f" AND i.{scope}", tuple(args))]
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
        by_source = Counter(r["platform"] for r in grievances
                            if not topic or topic in r["complaint_topics"])
        pool = (learned if view_learned else
                set_aside_rows if view_aside else grievances)
        shown = [r for r in pool
                 if (not topic or topic in r["complaint_topics"])
                 and (not src or r["platform"] == src)]
        shown.sort(key=lambda r: (taxonomy.SEVERITY_RANK.get(r["severity_shown"], 3),
                                  r["published_at"] or ""), reverse=False)
        shown.reverse()
        shown.sort(key=lambda r: taxonomy.SEVERITY_RANK.get(r["severity_shown"], 3))

        handles = [e for e in entities if e["x_handle"]]
        return render(request, "social.html", user=user, entity=entity,
                      entity_qs="all" if entity is None else entity["id"],
                      office=request.query_params.get("office") or None,
                      entities=entities, rows=shown, topic=topic, src=src,
                      view_aside=view_aside, view_learned=view_learned,
                      learned_count=len(learned),
                      set_aside_count=len(set_aside_rows),
                      set_aside_reasons=taxonomy.SOCIAL_SET_ASIDE,
                      by_topic=[(t, by_topic.get(t, 0)) for t in taxonomy.COMPLAINT_TOPICS],
                      by_source=[(p, by_source.get(p, 0)) for p in
                                 taxonomy.SOCIAL_PLATFORMS + ["Other"]],
                      total_grievances=len(grievances),
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


def _entity_stats(db, entity_id: int) -> dict:
    rows = [prep_item(r) for r in q(
        db, "SELECT * FROM items WHERE entity_id = ?"
            " AND gated_out = 0 AND source_type != 'social'",
        (entity_id,))]

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

    today = datetime.now(timezone.utc).date()
    trend = []
    for offset in range(TREND_DAYS - 1, -1, -1):
        d = today - timedelta(days=offset)
        trend.append({"date": d.strftime("%d %b"), "iso": d.isoformat(),
                      "count": by_day.get(d.isoformat(), 0)})
    max_trend = max((t["count"] for t in trend), default=0)

    open_count = one(db, "SELECT COUNT(*) n FROM items WHERE entity_id=? AND"
                         " status IN ('new','classified') AND gated_out = 0"
                         " AND source_type != 'social'",
                     (entity_id,))["n"]
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
            " AND gated_out = 0 AND source_type != 'social'"
            " ORDER BY published_at DESC LIMIT 6", (entity_id,))]

    return {
        "total": len(rows),
        "by_risk": [(a, by_risk.get(a, 0)) for a in taxonomy.RISK_AREAS],
        "max_risk": max(by_risk.values(), default=0),
        "by_sev": {s: by_sev.get(s, 0) for s in taxonomy.SEVERITIES},
        "by_factor": by_factor.most_common(8),
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


@app.get("/dashboard")
def dashboard(request: Request):
    db = connect()
    try:
        user = require_login(db, request)
        entity, entities = resolve_entity(db, user, request.query_params.get("entity"))
        stats = _entity_stats(db, entity["id"])
        return render(request, "dashboard.html", user=user, entity=entity,
                      entities=entities, stats=stats)
    finally:
        db.close()


def _category_rows(db, entities, key_fn, categories):
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

    for e in entities:
        rows = [prep_item(r) for r in q(
            db, "SELECT * FROM items WHERE entity_id = ? AND gated_out = 0"
                " AND status != 'new' AND source_type != 'social'", (e["id"],))]
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
        rows = []
        for e in entities:
            items = [prep_item(r) for r in q(
                db, "SELECT * FROM items WHERE entity_id=?"
                    " AND gated_out = 0 AND source_type != 'social'",
                (e["id"],))]
            by_risk = Counter(a for it in items for a in it["risk_areas_shown"])
            top_risk = by_risk.most_common(1)
            open_count = one(db, "SELECT COUNT(*) n FROM items WHERE entity_id=? AND"
                                 " status IN ('new','classified') AND gated_out = 0"
                                 " AND source_type != 'social'",
                             (e["id"],))["n"]
            last = one(db, "SELECT MAX(published_at) m FROM items WHERE entity_id=?"
                           " AND source_type != 'social'",
                       (e["id"],))["m"]
            rows.append({
                "entity": e,
                "total": len(items),
                "high": sum(1 for it in items if it["severity_shown"] == "high"),
                "open": open_count,
                "top_risk": top_risk[0][0] if top_risk else "—",
                "last": last,
            })
        rows.sort(key=lambda r: (-r["high"], -r["total"]))

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
                lambda it: [it["severity_shown"]], taxonomy.SEVERITIES)
        elif view == "risk":
            risk_rows = [r for r in _category_rows(
                db, entities,
                lambda it: it["risk_areas_shown"], taxonomy.RISK_AREAS)]
            risk_rows.sort(key=lambda r: (-r["high"], -r["total"], r["category"]))
        unclassified = one(db, "SELECT COUNT(*) n FROM items"
                               " WHERE status = 'new' AND gated_out = 0"
                               " AND source_type != 'social'")["n"]
        return render(request, "overview.html", user=user, rows=rows, view=view,
                      sev_rows=sev_rows, risk_rows=risk_rows,
                      unclassified=unclassified)
    finally:
        db.close()


# --- factors ----------------------------------------------------------------

@app.get("/factors")
def factors_page(request: Request):
    db = connect()
    try:
        user = require_login(db, request)
        if user["role"] == "superadmin":
            rows = q(db, "SELECT f.*, e.name AS entity_name, u.display_name AS author"
                         " FROM factors f LEFT JOIN entities e ON e.id = f.entity_id"
                         " LEFT JOIN users u ON u.id = f.created_by ORDER BY f.entity_id IS NULL DESC, f.name")
        else:
            rows = q(db, "SELECT f.*, e.name AS entity_name, u.display_name AS author"
                         " FROM factors f LEFT JOIN entities e ON e.id = f.entity_id"
                         " LEFT JOIN users u ON u.id = f.created_by"
                         " WHERE f.entity_id IS NULL OR f.entity_id = ?"
                         " ORDER BY f.entity_id IS NULL DESC, f.name", (user["entity_id"],))
        severity_defs = get_setting(db, SEVERITY_DEFS_KEY, DEFAULT_SEVERITY_DEFS)
        risk_defs = get_setting(db, RISK_DEFS_KEY, DEFAULT_RISK_DEFS)
        exclusion_rules = get_setting(db, EXCLUSION_RULES_KEY, DEFAULT_EXCLUSION_RULES)
        trusted_sources = get_setting(db, TRUSTED_SOURCES_KEY, DEFAULT_TRUSTED_SOURCES)
        return render(request, "factors.html", user=user, factors=rows,
                      severity_defs=severity_defs, risk_defs=risk_defs,
                      exclusion_rules=exclusion_rules,
                      trusted_sources=trusted_sources)
    finally:
        db.close()


@app.post("/factors")
async def factors_add(request: Request):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "lead", "superadmin")
        name = (form.get("name") or "").strip()
        conditions = (form.get("conditions") or "").strip()
        scope = form.get("scope", "entity")
        if not name or not conditions:
            raise HTTPException(400, "Factor name and conditions are required")
        entity_id = None if (scope == "global" and user["role"] == "superadmin") \
            else user["entity_id"]
        if entity_id is None and user["role"] != "superadmin":
            raise HTTPException(403, "Only the super admin creates global factors")
        x(db, "INSERT INTO factors (entity_id, name, conditions, created_by) VALUES (?,?,?,?)",
          (entity_id, name, conditions, user["id"]))
    finally:
        db.close()
    return RedirectResponse("/factors?msg=Factor+added", status_code=303)


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
        "/factors?msg=Risk+definitions+updated+—+applies+to+new+classifications",
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
        "/factors?msg=Severity+criteria+updated+—+applies+to+new+classifications",
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
        "/factors?msg=Negative+list+updated+—+applies+to+items+fetched+from+now+on",
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
        f"/factors?msg=Trusted+sources+saved+—+{changed}+item(s)+re-tiered",
        status_code=303)


@app.post("/factors/{factor_id}/toggle")
def factors_toggle(request: Request, factor_id: int):
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "lead", "superadmin")
        f = one(db, "SELECT * FROM factors WHERE id = ?", (factor_id,))
        if not f:
            raise HTTPException(404, "Factor not found")
        if user["role"] != "superadmin" and f["entity_id"] != user["entity_id"]:
            raise HTTPException(403, "Not your team's factor")
        x(db, "UPDATE factors SET active = 1 - active WHERE id = ?", (factor_id,))
    finally:
        db.close()
    return RedirectResponse("/factors?msg=Factor+updated", status_code=303)


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
        return render(request, "entities.html", user=user, rows=rows,
                      broadcast=broadcast, fetch_minutes=FETCH_MINUTES,
                      lookback_choices=LOOKBACK_CHOICES, lookback_default=LOOKBACK_DAYS)
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
            offices = sorted({o for e in every for o in entity_offices(e)})
            if any(not entity_offices(e) for e in every):
                offices.append(UNASSIGNED)
        else:
            offices = [user["rbi_office"]]

        selected = request.query_params.get("office") or (offices[0] if offices else "")
        if selected and selected not in offices:
            raise HTTPException(403, "That office is not visible to you")
        has_region_tab = bool(selected) and selected != UNASSIGNED
        tab = request.query_params.get("tab", "hq")
        if tab not in ("hq", "region") or (tab == "region" and not has_region_tab):
            tab = "hq"
        sev = request.query_params.get("sev", "")
        if sev not in ("high", "medium", "low"):
            sev = ""
        ent_filter = request.query_params.get("ent", "")

        if selected == UNASSIGNED:
            ents = [e for e in every if not entity_offices(e)]
        elif selected:
            ents = [e for e in every if selected in entity_offices(e)]
        else:
            ents = []

        rows = []
        sev_counts = Counter()
        for e in ents:
            items = [prep_item(r) for r in q(
                db, "SELECT * FROM items WHERE entity_id=?"
                    " AND gated_out = 0 AND source_type != 'social'",
                (e["id"],))]
            # An entity with no news says nothing on an office's page --
            # but the Unassigned bucket is a to-do list, not a news view,
            # so it keeps everything that still needs an office.
            if not items and selected != UNASSIGNED:
                continue
            sev_counts.update(it["severity_shown"] for it in items)
            by_risk = Counter(a for it in items for a in it["risk_areas_shown"])
            shown = items if not sev else [
                it for it in items if it["severity_shown"] == sev]
            grievances = [g for g in (prep_item(r) for r in q(
                db, "SELECT * FROM items WHERE entity_id=?"
                    " AND source_type = 'social' AND gated_out = 0",
                (e["id"],))) if g["complaint_topics"]]
            rows.append({
                "entity": e,
                "total": len(items),
                "high": sum(1 for it in items if it["severity_shown"] == "high"),
                "open": sum(1 for it in items if it["status"] in ("new", "classified")),
                "top_risk": by_risk.most_common(1)[0][0] if by_risk else "\u2014",
                "matching": len(shown),
                # unfiltered: the five latest; severity-filtered: everything
                # that matches, because the filter IS the reading list
                "recent": sorted(shown, key=lambda it: it["published_at"] or "",
                                 reverse=True)[:50 if sev else 5],
                "social_total": len(grievances),
                "social_topics": [t for t, _ in Counter(
                    t for g in grievances for t in g["complaint_topics"]).most_common(3)],
                "social_recent": sorted(grievances,
                                        key=lambda g: g["published_at"] or "",
                                        reverse=True)[:3],
            })
        entity_choices = [(r["entity"]["id"], r["entity"]["name"]) for r in rows]
        if ent_filter:
            rows = [r for r in rows if str(r["entity"]["id"]) == ent_filter]
        if sev:
            rows = [r for r in rows if r["matching"]]
        rows.sort(key=lambda r: (-r["high"], -r["total"]))

        # In-region news from entities headquartered under other offices.
        region_rows = []
        place_terms = geography.office_places(selected) if has_region_tab else []
        exclusions = geography.office_exclusions(selected) if has_region_tab else []
        if tab == "region" and place_terms:
            others = [e for e in every if selected not in entity_offices(e)]
            for e in others:
                # A place that is part of the bank's own name proves
                # nothing about where a story happened: every Bank of
                # Maharashtra headline mentions Maharashtra.
                terms = [t for t in place_terms
                         if not place_mentions(e["name"], t)]
                if not terms:
                    continue
                for r in q(db, "SELECT * FROM items WHERE entity_id=?"
                               " AND gated_out = 0 AND source_type != 'social'",
                           (e["id"],)):
                    it = prep_item(r)
                    text = " ".join(filter(None, (it.get("title"),
                                                  it.get("snippet"),
                                                  it.get("summary"))))
                    hit = next((t for t in terms if place_mentions(text, t)), None)
                    # A story that names a sister office's district belongs
                    # there, however loudly it also says the state's name.
                    if hit and any(place_mentions(text, t) for t in exclusions):
                        continue
                    if hit:
                        if sev and it["severity_shown"] != sev:
                            continue
                        it["region_term"] = hit
                        it["entity_name"] = e["name"]
                        region_rows.append(it)
            region_rows.sort(key=lambda it: it["published_at"] or "", reverse=True)

        return templates.TemplateResponse(request, "rd.html", {
            "user": user, "offices": offices, "selected": selected,
            "rows": rows, "unassigned_label": UNASSIGNED,
            "tab": tab, "has_region_tab": has_region_tab,
            "region_desc": geography.describe(selected) if has_region_tab else "",
            "region_rows": region_rows,
            "sev": sev, "ent_filter": ent_filter,
            "sev_counts": sev_counts, "entity_choices": entity_choices,
            "msg": request.query_params.get("msg"),
        })
    finally:
        db.close()


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
    days = int(raw_days) if raw_days.isdigit() and int(raw_days) in LOOKBACK_CHOICES else None
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


@app.get("/settings")
def settings_page(request: Request):
    """Operational knobs, stored in the database: windows, budgets and
    caps that were env-vars-only before. Superadmin territory."""
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        return templates.TemplateResponse(request, "settings.html", {
            "user": user, "spec": tuning.SPEC,
            "toggles": tuning.TOGGLES, "effective": tuning.load(db),
            "overrides": tuning.overrides(db),
            "msg": request.query_params.get("msg"),
        })
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
