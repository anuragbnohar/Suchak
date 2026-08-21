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

from . import taxonomy
from .auth import get_user, require_login, require_role, verify_password
from .classify import (DEFAULT_EXCLUSION_RULES, DEFAULT_SEVERITY_DEFS,
                       EXCLUSION_RULES_KEY, SEVERITY_DEFS_KEY,
                       similar_reviewed, suggest_action)
from .db import connect, get_setting, init_db, one, q, set_setting, x
from .ingest import run_cycle
from .seed import seed_if_empty

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("suchak")

FETCH_MINUTES = int(os.environ.get("SUCHAK_FETCH_MINUTES", "30"))
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
    finally:
        db.close()
    if FETCH_MINUTES > 0:
        _spawn(_periodic_fetch())
        log.info("Background fetch every %s minutes", FETCH_MINUTES)
    yield


app = FastAPI(title="Suchak", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SUCHAK_SECRET", secrets.token_hex(32)),
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")


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
    # a reviewer's severity correction wins over the classifier's verdict
    d["severity_shown"] = d.get("review_severity") or d.get("severity") or "low"
    return d


def visible_entities(db, user) -> list:
    if user["role"] == "superadmin":
        return q(db, "SELECT * FROM entities ORDER BY name")
    return q(db, "SELECT * FROM entities WHERE id = ? ", (user["entity_id"],))


def resolve_entity(db, user, requested: str | None):
    entities = visible_entities(db, user)
    if not entities:
        raise HTTPException(404, "No entities configured")
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

        where, params = ["i.entity_id = ?"], [entity["id"]]
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
            where.append("i.risk_areas LIKE ?")
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
        if complaints or topic:
            where.append("i.complaint_topics != '[]'")
        if topic:
            where.append("i.complaint_topics LIKE ?")
            params.append(f'%"{topic}"%')

        rows = q(
            db,
            "SELECT i.*, u.display_name AS reviewer_name,"
            " (SELECT COUNT(*) FROM item_sources s WHERE s.item_id = i.id) AS extra_sources"
            " FROM items i LEFT JOIN users u ON u.id = i.reviewed_by"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY CASE COALESCE(i.review_severity, i.severity)"
            "   WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
            " CASE i.actionability WHEN 'action_recommended' THEN 0"
            "   WHEN 'review_recommended' THEN 1 ELSE 2 END,"
            " i.relevance DESC, i.published_at DESC LIMIT 200",
            params,
        )
        counts = {r["s"]: r["n"] for r in q(
            db, "SELECT CASE WHEN gated_out = 1 THEN 'filtered'"
                "        WHEN status IN ('new','classified') THEN 'open'"
                "        ELSE status END s,"
                " COUNT(*) n FROM items WHERE entity_id = ? GROUP BY s", (entity["id"],))}
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
            ("on", on_day), ("factor", factor), ("org", org),
            ("complaints", "1" if complaints else ""), ("topic", topic)) if v}
        filter_qs = "".join(f"&{k}={quote(str(v))}" for k, v in extras.items())
        return render(request, "queue.html", user=user, entity=entity,
                      entities=entities, items=prepped, grouped=grouped,
                      status=status, risk=risk, counts=counts,
                      extras=extras, filter_qs=filter_qs)
    finally:
        db.close()


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
                      item=prep_item(row), sources=sources,
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

        x(db, "UPDATE items SET status=?, reviewed_by=?, reviewed_at=?, review_relevant=?,"
              " review_severity=?, review_risk_areas=?, review_actionable=?, review_action=?,"
              " review_notes=? WHERE id=?",
          (status, user["id"], datetime.now(timezone.utc).isoformat(timespec="seconds"),
           relevant, severity, json.dumps(risk_areas), actionable, action,
           (form.get("notes") or "").strip() or None, item_id))
    finally:
        db.close()
    return RedirectResponse(f"/queue?msg=Review+saved", status_code=303)


# --- dashboards -------------------------------------------------------------

def _entity_stats(db, entity_id: int, days: int = 14) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = [prep_item(r) for r in q(
        db, "SELECT * FROM items WHERE entity_id = ? AND published_at >= ?"
            " AND gated_out = 0", (entity_id, since))]

    by_risk, by_sev, by_factor, by_day = Counter(), Counter(), Counter(), Counter()
    by_topic, complaints_total = Counter(), 0
    linkages = Counter()
    for it in rows:
        for a in it["risk_areas"]:
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
                         " status IN ('new','classified') AND gated_out = 0",
                     (entity_id,))["n"]
    high_recent = [prep_item(r) for r in q(
        db, "SELECT * FROM items WHERE entity_id=?"
            " AND COALESCE(review_severity, severity)='high' AND published_at >= ?"
            " AND gated_out = 0 ORDER BY published_at DESC LIMIT 6", (entity_id, since))]

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
                    " AND gated_out = 0", (e["id"], since))]
            by_risk = Counter(a for it in items for a in it["risk_areas"])
            top_risk = by_risk.most_common(1)
            open_count = one(db, "SELECT COUNT(*) n FROM items WHERE entity_id=? AND"
                                 " status IN ('new','classified') AND gated_out = 0",
                             (e["id"],))["n"]
            last = one(db, "SELECT MAX(published_at) m FROM items WHERE entity_id=?",
                       (e["id"],))["m"]
            rows.append({
                "entity": e,
                "total7": len(items),
                "high7": sum(1 for it in items if it["severity_shown"] == "high"),
                "open": open_count,
                "top_risk": top_risk[0][0] if top_risk else "—",
                "last": last,
            })
        rows.sort(key=lambda r: (-r["high7"], -r["total7"]))
        return render(request, "overview.html", user=user, rows=rows)
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
        return render(request, "factors.html", user=user, factors=rows,
                      severity_defs=severity_defs, exclusion_rules=exclusion_rules)
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
            rows.append({"entity": e, "aliases": json.loads(e["aliases"]),
                         "items": n_items, "last_fetch": last_fetch})
        # latest status per broadcast feed (RBI, NSE, BSE) -- logged with a
        # NULL entity because one fetch serves every entity
        broadcast, seen = [], set()
        for r in q(db, "SELECT * FROM fetch_log WHERE entity_id IS NULL"
                       " ORDER BY id DESC LIMIT 30"):
            if r["source"] not in seen:
                seen.add(r["source"])
                broadcast.append(r)
        return render(request, "entities.html", user=user, rows=rows,
                      broadcast=broadcast)
    finally:
        db.close()


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
        x(db, "INSERT INTO entities (name, kind, aliases) VALUES (?,?,?)",
          (name, kind, json.dumps(aliases)))
    finally:
        db.close()
    return RedirectResponse("/entities?msg=Entity+added", status_code=303)


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
        x(db, "UPDATE entities SET aliases = ? WHERE id = ?",
          (json.dumps(aliases), entity_id))
    finally:
        db.close()
    return RedirectResponse("/entities?msg=Aliases+updated", status_code=303)


@app.post("/fetch")
async def fetch_now(request: Request):
    form = await request.form()
    entity_id = form.get("entity_id")
    db = connect()
    try:
        user = require_login(db, request)
        require_role(user, "lead", "superadmin")
        if entity_id and user["role"] != "superadmin" \
                and str(user["entity_id"]) != str(entity_id):
            raise HTTPException(403, "Not your team's entity")
    finally:
        db.close()
    _spawn(asyncio.to_thread(run_cycle, int(entity_id) if entity_id else None))
    return RedirectResponse(
        "/entities?msg=Fetch+started+in+background+—+refresh+in+a+minute",
        status_code=303)
