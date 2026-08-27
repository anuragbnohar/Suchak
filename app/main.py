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
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import forums, reddit_source, taxonomy, x_scrape
from .auth import get_user, require_login, require_role, verify_password
from .classify import (DEFAULT_EXCLUSION_RULES, DEFAULT_SEVERITY_DEFS,
                       EXCLUSION_RULES_KEY, SEVERITY_DEFS_KEY,
                       similar_reviewed, suggest_action)
from .db import (connect, get_setting, init_db, one, q, remove_entity,
                 set_setting, x)
from .ingest import (CHANNELS, LOOKBACK_CHOICES, LOOKBACK_DAYS, NEWS_EDITIONS, SOCIAL_LOOKBACK_DAYS,
                     X_BEARER, X_ENABLED, X_MAX_POSTS, run_cycle)
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
APP_BUILD = "2026-08-25.3"

templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals["app_build"] = APP_BUILD


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


def visible_entities(db, user) -> list:
    if user["role"] == "superadmin":
        return q(db, "SELECT * FROM entities ORDER BY name")
    return q(db, "SELECT * FROM entities WHERE id = ? ", (user["entity_id"],))


def resolve_entity(db, user, requested: str | None):
    """The entity a page is scoped to, or None for "every entity I can see".

    Only the super admin has a cross-entity scope, and it exists so the
    severity and risk views can hand a total somewhere to open: a count of
    high-severity items across the portfolio has no single entity behind it.
    """
    entities = visible_entities(db, user)
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
        entity, entities = resolve_entity(db, user, request.query_params.get("entity"))
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
        try:
            days = max(0, min(int(request.query_params.get("days", "0")), 365))
        except ValueError:
            days = 0

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
            where.append("i.gated_out = 1")
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
        if days:
            where.append("i.published_at >= ?")
            params.append((datetime.now(timezone.utc) - timedelta(days=days))
                          .isoformat())
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
            db, "SELECT CASE WHEN gated_out = 1 THEN 'filtered'"
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
            ("risk", risk), ("sev", sev), ("days", days or ""),
            ("on", on_day), ("factor", factor), ("org", org), ("src", src),
            ("complaints", "1" if complaints else ""), ("topic", topic)) if v}
        filter_qs = "".join(f"&{k}={quote(str(v))}" for k, v in extras.items())
        return render(request, "queue.html", user=user, entity=entity,
                      entity_qs="all" if entity is None else entity["id"],
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
                      similar=similar_prepped, suggestion=suggestion)
    finally:
        db.close()


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
        entity, entities = resolve_entity(db, user, request.query_params.get("entity"))
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

        # Posts still awaiting classification carry no verdict yet; counting
        # them as "no complaint found" would misstate a fetch in progress.
        pending = sum(1 for r in rows if r["status"] == "new")
        grievances = [r for r in rows if r["complaint_topics"]]
        by_topic = Counter(t for r in grievances for t in r["complaint_topics"])
        shown = [r for r in grievances if not topic or topic in r["complaint_topics"]]
        shown.sort(key=lambda r: (taxonomy.SEVERITY_RANK.get(r["severity_shown"], 3),
                                  r["published_at"] or ""), reverse=False)
        shown.reverse()
        shown.sort(key=lambda r: taxonomy.SEVERITY_RANK.get(r["severity_shown"], 3))

        handles = [e for e in entities if e["x_handle"]]
        return render(request, "social.html", user=user, entity=entity,
                      entity_qs="all" if entity is None else entity["id"],
                      entities=entities, rows=shown, topic=topic,
                      by_topic=[(t, by_topic.get(t, 0)) for t in taxonomy.COMPLAINT_TOPICS],
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

def _entity_stats(db, entity_id: int, days: int = 14) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = [prep_item(r) for r in q(
        db, "SELECT * FROM items WHERE entity_id = ? AND published_at >= ?"
            " AND gated_out = 0 AND source_type != 'social'",
        (entity_id, since))]

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
    for offset in range(days - 1, -1, -1):
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
            " AND COALESCE(review_severity, severity)='high' AND published_at >= ?"
            " AND gated_out = 0 AND source_type != 'social'"
            " ORDER BY published_at DESC LIMIT 6", (entity_id, since))]

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
        "days": days,
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


def _category_rows(db, entities, since, key_fn, categories):
    """Group the window's classified items by a category instead of by entity.

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
            fresh = (it["published_at"] or "") >= since
            awaiting = it["status"] == "classified"
            for cat in key_fn(it):
                if cat not in by_cat:
                    continue
                bucket = by_cat[cat]
                if fresh:
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
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        entities = q(db, "SELECT * FROM entities ORDER BY name")
        rows = []
        for e in entities:
            items = [prep_item(r) for r in q(
                db, "SELECT * FROM items WHERE entity_id=? AND published_at >= ?"
                    " AND gated_out = 0 AND source_type != 'social'",
                (e["id"], since))]
            by_risk = Counter(a for it in items for a in it["risk_areas_shown"])
            top_risk = by_risk.most_common(1)
            open_count = one(db, "SELECT COUNT(*) n FROM items WHERE entity_id=? AND"
                                 " status IN ('new','classified') AND gated_out = 0"
                                 " AND source_type != 'social'",
                             (e["id"],))["n"]
            last = one(db, "SELECT MAX(published_at) m FROM items WHERE entity_id=?"
                           " AND source_type != 'social'",
                       (e["id"],))["m"]
            # Items older than the window still exist and are still reviewable.
            # Without this the row reads as a contradiction -- 0 items beside 4
            # awaiting review -- when the truth is simply that the coverage is
            # older than seven days, which for a small entity is normal.
            total_all = one(db, "SELECT COUNT(*) n FROM items WHERE entity_id=?"
                                " AND gated_out = 0 AND source_type != 'social'",
                            (e["id"],))["n"]
            rows.append({
                "entity": e,
                "total7": len(items),
                "total_all": total_all,
                "older": total_all - len(items),
                "high7": sum(1 for it in items if it["severity_shown"] == "high"),
                "open": open_count,
                "top_risk": top_risk[0][0] if top_risk else "—",
                "last": last,
            })
        rows.sort(key=lambda r: (-r["high7"], -r["total7"]))

        # The same seven days, grouped three ways. Entity answers "who needs
        # attention", severity "how bad is the week", risk "what kind of
        # problem is showing up" -- questions a supervisor asks separately.
        view = request.query_params.get("view") or "entity"
        if view not in ("entity", "severity", "risk"):
            view = "entity"
        sev_rows = risk_rows = None
        if view == "severity":
            sev_rows = _category_rows(
                db, entities, since,
                lambda it: [it["severity_shown"]], taxonomy.SEVERITIES)
        elif view == "risk":
            risk_rows = [r for r in _category_rows(
                db, entities, since,
                lambda it: it["risk_areas_shown"], taxonomy.RISK_AREAS)]
            risk_rows.sort(key=lambda r: (-r["high"], -r["total"], r["category"]))
        unclassified = one(db, "SELECT COUNT(*) n FROM items"
                               " WHERE status = 'new' AND gated_out = 0"
                               " AND source_type != 'social'")["n"]
        return render(request, "overview.html", user=user, rows=rows, view=view,
                      sev_rows=sev_rows, risk_rows=risk_rows,
                      unclassified=unclassified, days=7)
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
        exclusion_rules = get_setting(db, EXCLUSION_RULES_KEY, DEFAULT_EXCLUSION_RULES)
        trusted_sources = get_setting(db, TRUSTED_SOURCES_KEY, DEFAULT_TRUSTED_SOURCES)
        return render(request, "factors.html", user=user, factors=rows,
                      severity_defs=severity_defs, exclusion_rules=exclusion_rules,
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


@app.post("/entities")
async def entities_add(request: Request):
    form = await request.form()
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "superadmin")
        name = (form.get("name") or "").strip()
        kind = form.get("kind") or taxonomy.ENTITY_KINDS[0]
        aliases = [a.strip() for a in (form.get("aliases") or "").split(",") if a.strip()]
        if not name:
            raise HTTPException(400, "Entity name is required")
        if name not in aliases:
            aliases.insert(0, name)
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
        aliases = [a.strip() for a in (form.get("aliases") or "").split(",") if a.strip()]
        if not aliases:
            raise HTTPException(400, "At least one alias is required")
        # The form posts both fields together; when it omits languages, keep
        # whatever the entity already had rather than resetting it to English.
        langs = (_parse_languages(form.get("languages")) if form.get("languages") is not None
                 else json.loads(e["languages"] or '["en"]'))
        # stored bare: the query builder writes "to:handle" itself
        handle = (form.get("x_handle") or "").strip().lstrip("@") \
            if form.get("x_handle") is not None else (e["x_handle"] or "")
        if handle and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            raise HTTPException(400, "An X handle is 1-15 letters, digits or underscores")
        x(db, "UPDATE entities SET aliases = ?, languages = ?, x_handle = ? WHERE id = ?",
          (json.dumps(aliases), json.dumps(langs), handle or None, entity_id))
    finally:
        db.close()
    return RedirectResponse("/entities?msg=Aliases+updated", status_code=303)


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
    _spawn(asyncio.to_thread(run_cycle, int(entity_id) if entity_id else None,
                             days, channel))
    if channel == "social":
        msg = (f"Social media fetch started — complaints from the last "
               f"{SOCIAL_LOOKBACK_DAYS} days. Refresh in a minute.")
    else:
        window = days or LOOKBACK_DAYS
        what = "News fetch" if channel == "news" else "Fetch"
        msg = f"{what} started — searching the last {window} days. Refresh in a minute."
    return RedirectResponse(f"/entities?msg={quote(msg)}", status_code=303)
