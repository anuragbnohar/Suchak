"""Operational settings the superadmin can change from the app.

Each knob has a code default (the same environment variables as before,
so nothing changes for existing setups) and may carry a stored override
in the settings table. The override wins; clearing the field on the
Settings page removes it. Values are snapshotted into ingest.TUNING at
the start of every fetch, so a fetch runs under one consistent set.
"""
import json
import os

from .db import get_setting, set_setting

TUNING_KEY = "fetch_tuning"

# key, label, plain-language help, (min, max), default
SPEC = [
    ("lookback_days", "News window (days)",
     "How far back a news fetch looks when no window is chosen on the "
     "button. The fetch form still offers 7/30/90/365 per run.",
     (1, 365), int(os.environ.get("SUCHAK_LOOKBACK_DAYS", "7"))),
    ("social_lookback_days", "Social media window (days)",
     "How far back every social fetch looks. Complaints age slowly, so "
     "this is deliberately long.",
     (7, 730), int(os.environ.get("SUCHAK_SOCIAL_LOOKBACK_DAYS", "365"))),
    ("news_query_aliases", "Google News aliases per search",
     "How many spellings of a bank's name go into one Google News "
     "search. Google returns worse results when a search carries too "
     "many; attribution still checks every alias.",
     (1, 12), int(os.environ.get("SUCHAK_QUERY_ALIASES", "6"))),
    ("body_checks", "Article body reads per fetch",
     "How many rejected articles a fetch may open and read in full, "
     "looking for the bank's name in the text. Each read takes a few "
     "seconds; unread rejects wait on the Rejected tab.",
     (0, 20), int(os.environ.get("SUCHAK_BODY_CHECKS", "8"))),
    ("x_max_posts", "X posts per fetch (paid)",
     "The hard ceiling on posts one press of Fetch X may read for one "
     "bank. X bills about $0.005 per post, so this is the spend control: "
     "10 posts is about $0.05 per press.",
     (1, 100), int(os.environ.get("SUCHAK_X_MAX_POSTS", "50"))),
    ("reddit_max", "Reddit posts per fetch",
     "Cap on posts collected from Reddit for one bank in one fetch.",
     (10, 200), int(os.environ.get("SUCHAK_REDDIT_MAX", "50"))),
    ("forums_max", "consumercomplaints.in complaints per fetch",
     "Cap on complaints collected from the forum for one bank in one "
     "fetch.",
     (5, 200), int(os.environ.get("SUCHAK_FORUMS_MAX", "50"))),
    ("yt_comment_videos", "YouTube videos per search",
     "How many videos each of the two YouTube searches (name, and "
     "complaint-focused) may return per bank.",
     (1, 15), int(os.environ.get("SUCHAK_YT_COMMENT_VIDEOS", "6"))),
    ("yt_comments_per_video", "YouTube comments per video",
     "How many comments are read under each video.",
     (5, 100), int(os.environ.get("SUCHAK_YT_COMMENTS_PER_VIDEO", "20"))),
    ("yt_comments_max", "YouTube comments cap per fetch",
     "Total comments per bank per fetch, across all its videos.",
     (10, 200), int(os.environ.get("SUCHAK_YT_COMMENTS_MAX", "60"))),
]

# On/off settings. key, label, help, default (1 = on)
TOGGLES = [
    ("x_only_complaints", "X: fetch only complaint-like posts",
     "Adds the complaint vocabulary to the X search itself, so X returns "
     "(and bills for) fewer posts. Off means every post addressed to the "
     "bank is fetched and the classifier sorts them afterwards.", 1),
    ("x_author_handles", "X: also fetch the author's handle (costs extra)",
     "Off, a post is stored as \"X post\" and its link uses the "
     "id-only form, which opens the same tweet. On, X is also asked for "
     "each author's user record so the card can show @who complained -- "
     "X's price list bills user records separately, at twice the price "
     "of a post, so check the console's Usage page before leaving this on.", 0),
    ("x_exclude_replies", "X: fetch posts only, not replies",
     "Skips replies inside conversation threads. Because X's to: search "
     "matches replies only, switching this on also changes that search to "
     "@handle, which finds standalone posts addressed to the bank.", 1),
]

DEFAULTS = {key: default for key, _, _, _, default in SPEC}
DEFAULTS.update({key: default for key, _, _, default in TOGGLES})
BOUNDS = {key: bounds for key, _, _, bounds, _ in SPEC}
TOGGLE_KEYS = {key for key, _, _, _ in TOGGLES}


def _stored(db) -> dict:
    raw = get_setting(db, TUNING_KEY, "") or ""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def load(db) -> dict:
    """Every knob's effective value: stored override or code default."""
    stored = _stored(db)
    out = dict(DEFAULTS)
    for key, value in stored.items():
        if key in DEFAULTS:
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                pass
    return out


def overrides(db) -> dict:
    """Only the stored overrides, for the Settings page to show which
    fields differ from the defaults."""
    return {k: v for k, v in _stored(db).items() if k in DEFAULTS}


def save(db, form: dict, user_id: int) -> None:
    """Store the overrides from the Settings form. A blank field means
    'use the default'; anything else must be a whole number in range."""
    stored = {}
    if form.get("toggles_present"):
        # An unticked checkbox sends nothing, so absence means "off" --
        # but only when the form that posted actually carried them.
        for key, _label, _help, default in TOGGLES:
            value = 1 if form.get(key) else 0
            if value != default:
                stored[key] = value
    for key, label, _help, (lo, hi), default in SPEC:
        raw = (form.get(key) or "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"{label}: {raw!r} is not a whole number")
        if not (lo <= value <= hi):
            raise ValueError(f"{label}: must be between {lo} and {hi}")
        if value != default:
            stored[key] = value
    set_setting(db, TUNING_KEY, json.dumps(stored), user_id)
