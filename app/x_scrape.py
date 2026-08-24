"""Collect complaints from X by driving a signed-in browser.

This exists because complaints reach a bank as replies to its grievance
handle, and mentions are only served to an authenticated session. No
unauthenticated route returns them: the syndication embed endpoint gives a
profile's own timeline, x.com/search returns a JavaScript shell to a
logged-out client, and Nitter depended on a guest-token endpoint X removed.

What that means in practice, stated plainly because it shapes how this
module is written:

  * X detects automated search and locks the account it is running as. Use
    an account you are prepared to lose, never a personal one.
  * The DOM is not an interface. Selectors here are X's internal test ids;
    they change without notice, and when they do this collector returns
    zero rather than raising. A silent zero looks exactly like "no
    complaints this week", which is the worst failure a supervisory tool
    can have -- so every path that yields nothing says why, loudly, and the
    reason reaches the Entities page.

Session state is written to disk and reused, so a fetch normally costs no
login at all. Logging in on every run is the fastest way to get locked out.
"""
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("suchak.x_scrape")

ENABLED = os.environ.get("SUCHAK_X_SCRAPE", "").strip().lower() in ("1", "true", "yes")
USER = os.environ.get("SUCHAK_X_USER", "")
PASS = os.environ.get("SUCHAK_X_PASS", "")
# X often asks for this when it does not recognise the device.
VERIFY = os.environ.get("SUCHAK_X_VERIFY", "")
STATE_PATH = os.environ.get(
    "SUCHAK_X_STATE", str(Path(__file__).resolve().parent.parent / ".x_session.json"))
MAX_POSTS = max(1, min(int(os.environ.get("SUCHAK_X_SCRAPE_MAX", "50")), 200))
MAX_SCROLLS = max(1, min(int(os.environ.get("SUCHAK_X_SCRAPE_SCROLLS", "12")), 40))
HEADLESS = os.environ.get("SUCHAK_X_HEADLESS", "1").strip().lower() not in ("0", "false", "no")
NAV_TIMEOUT_MS = 45_000

SEARCH_URL = "https://x.com/search?q={query}&f=live"


class ScrapeUnavailable(RuntimeError):
    """Raised when the collector cannot run at all, as opposed to running
    and finding nothing. The distinction is the whole point: one is a
    broken tool, the other is a real observation."""


def _sleep(a=0.6, b=1.6):
    time.sleep(random.uniform(a, b))


def _launch(pw):
    return pw.chromium.launch(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )


def _new_context(browser):
    """Reuse a saved session when there is one. Every avoided login is a
    meaningful reduction in the chance of the account being locked."""
    opts = {
        "viewport": {"width": 1400, "height": 1000},
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "locale": "en-IN",
    }
    if os.path.exists(STATE_PATH):
        try:
            return browser.new_context(storage_state=STATE_PATH, **opts), True
        except Exception as exc:
            log.warning("Saved X session unusable (%s); logging in again", exc)
    return browser.new_context(**opts), False


# X's login is a React flow whose field names have changed more than once,
# and which sometimes serves a consent, error or bot-check page instead. Each
# step therefore tries several selectors and, when none appears, reports what
# the page actually showed rather than a bare Playwright timeout.
USERNAME_SELECTORS = ('input[autocomplete="username"]', 'input[name="text"]',
                      'input[autocomplete="email"]')
PASSWORD_SELECTORS = ('input[name="password"]', 'input[type="password"]',
                      'input[autocomplete="current-password"]')
CHALLENGE_SELECTORS = ('input[data-testid="ocfEnterTextTextInput"]',
                       'input[data-testid="challenge_response"]')
# In a visible run the operator can finish the login by hand -- solve a
# CAPTCHA, approve a device, or just sign in. The collector's job then is to
# notice and capture the session, not to insist on driving every keystroke.
MANUAL_LOGIN_MS = int(os.environ.get("SUCHAK_X_MANUAL_LOGIN_SECONDS", "240")) * 1000
HOME_MARKERS = ('[data-testid="SideNav_AccountSwitcher_Button"]',
                '[data-testid="AppTabBar_Home_Link"]',
                '[data-testid="primaryColumn"]')
DEBUG_SHOT = os.environ.get(
    "SUCHAK_X_DEBUG_SHOT",
    str(Path(__file__).resolve().parent.parent / ".x_login_debug.png"))


def _signed_in(page) -> bool:
    """Already authenticated? A logged-in session lands on the timeline, and
    /i/flow/login redirects there rather than showing a form."""
    try:
        if re.search(r"https://(x|twitter)\.com/home", page.url):
            return True
        return any(page.locator(sel).first.count() for sel in HOME_MARKERS)
    except Exception:
        return False


def _await_step(page, selectors, timeout_ms: int):
    """Wait for whichever comes first: the session becoming authenticated, or
    one of these fields appearing. Returns ("home", None), ("field", locator)
    or ("none", None).

    Written as a race rather than a wait on one selector because the login
    can complete without this code touching it -- the operator finishing it
    in a visible window is a supported path, not an anomaly.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if _signed_in(page):
            return "home", None
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() and loc.is_visible():
                    return "field", loc
            except Exception:
                pass
        page.wait_for_timeout(500)
    return ("home", None) if _signed_in(page) else ("none", None)


def _first_visible(page, selectors, timeout_ms: int):
    """The first of these selectors to appear, or None. Polls rather than
    waiting on one selector, so a renamed field is not fatal."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                pass
        page.wait_for_timeout(400)
    return None


def _page_summary(page) -> str:
    """What the page is actually showing, for an error message worth reading."""
    try:
        text = " ".join(page.inner_text("body").split())[:300]
    except Exception:
        text = "(could not read the page)"
    try:
        page.screenshot(path=DEBUG_SHOT, full_page=False)
        shot = f" A screenshot was saved to {DEBUG_SHOT}."
    except Exception:
        shot = ""
    return f"URL {page.url} — page said: {text!r}.{shot}"


def _login(context) -> None:
    """Establish a session, however it gets established.

    Driving the form is the fast path, not the only one: X may already
    recognise the browser, and in a visible run the operator may complete the
    login by hand. Both end with a session worth saving, so every step here
    checks for that before insisting on the next field.
    """
    page = context.new_page()
    page.set_default_timeout(NAV_TIMEOUT_MS)
    manual_ok = not HEADLESS
    try:
        page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        _sleep(1.0, 2.0)

        if _signed_in(page):
            context.storage_state(path=STATE_PATH)
            log.info("Already signed in; session saved to %s", STATE_PATH)
            return

        if not (USER and PASS) and not manual_ok:
            raise ScrapeUnavailable(
                "No saved session and no credentials: set SUCHAK_X_USER and "
                "SUCHAK_X_PASS, or run with SUCHAK_X_HEADLESS=0 and sign in by hand")

        wait_ms = MANUAL_LOGIN_MS if manual_ok else 25_000
        state, field = _await_step(page, USERNAME_SELECTORS, wait_ms)
        if state == "home":
            context.storage_state(path=STATE_PATH)
            log.info("Signed in (completed outside this script); session saved")
            return
        if state == "none":
            raise ScrapeUnavailable(
                "No username field and no signed-in session. X may be serving a "
                "consent, error or bot-check page, or it has renamed the field. "
                + _page_summary(page))
        if not USER:
            raise ScrapeUnavailable(
                "A login form is showing but SUCHAK_X_USER is not set. Either set "
                "the credentials, or sign in by hand in this window.")
        field.fill(USER)
        page.keyboard.press("Enter")
        _sleep()

        state, challenge = _await_step(page, CHALLENGE_SELECTORS, 6_000)
        if state == "home":
            context.storage_state(path=STATE_PATH)
            return
        if state == "field":
            if not VERIFY:
                raise ScrapeUnavailable(
                    "X asked to verify the account (usually the email or phone on "
                    "it). Set SUCHAK_X_VERIFY to that value, or type it in this "
                    "window. " + _page_summary(page))
            challenge.fill(VERIFY)
            page.keyboard.press("Enter")
            _sleep()

        state, pwd = _await_step(page, PASSWORD_SELECTORS, wait_ms)
        if state == "home":
            context.storage_state(path=STATE_PATH)
            return
        if state == "none":
            raise ScrapeUnavailable(
                "The password field never appeared and the session is not signed "
                "in. The username step may have been rejected. " + _page_summary(page))
        pwd.fill(PASS)
        page.keyboard.press("Enter")

        state, _ = _await_step(page, PASSWORD_SELECTORS, wait_ms)
        if state == "home":
            _sleep(1.0, 2.0)
            context.storage_state(path=STATE_PATH)
            log.info("X session saved to %s", STATE_PATH)
            return
        if state == "field":
            raise ScrapeUnavailable(
                "Still on the password step: the credentials were rejected, or X "
                "wants another factor. " + _page_summary(page))
        raise ScrapeUnavailable("Login did not reach the timeline. " + _page_summary(page))
    finally:
        page.close()


# One JS pass over the rendered timeline. Fewer round trips than querying
# each field, and it keeps every selector this module depends on in one
# place -- when X changes its markup, this is the only thing to repair.
_EXTRACT = """
() => [...document.querySelectorAll('article[data-testid="tweet"]')].map(a => {
  const textEl = a.querySelector('div[data-testid="tweetText"]');
  const timeEl = a.querySelector('time[datetime]');
  const link = timeEl && timeEl.closest('a') ? timeEl.closest('a').getAttribute('href') : null;
  const nameEl = a.querySelector('div[data-testid="User-Name"]');
  let handle = null;
  if (nameEl) {
    const m = nameEl.innerText.match(/@([A-Za-z0-9_]{1,15})/);
    if (m) handle = m[1];
  }
  return {
    text: textEl ? textEl.innerText : null,
    when: timeEl ? timeEl.getAttribute('datetime') : null,
    href: link,
    handle: handle,
  };
}).filter(t => t.text && t.href)
"""


def scrape_query(query: str, max_posts: int = MAX_POSTS) -> list[dict]:
    """Posts matching an X search query, as normalized candidate dicts.

    Raises ScrapeUnavailable when the collector could not run. Returns an
    empty list only when it ran and the search genuinely had nothing.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScrapeUnavailable(
            "Playwright is not installed. pip install playwright && "
            "python -m playwright install chromium") from exc

    from urllib.parse import quote
    url = SEARCH_URL.format(query=quote(query))
    seen, out = set(), []

    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            context, reused = _new_context(browser)
            if not reused:
                _login(context)
            page = context.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)
            page.goto(url, wait_until="domcontentloaded")
            _sleep(2.0, 3.5)

            if "/i/flow/login" in page.url or "login" in page.url:
                raise ScrapeUnavailable(
                    "Redirected to the login page: the saved session has expired. "
                    f"Delete {STATE_PATH} and run again to sign in fresh.")

            try:
                page.wait_for_selector('article[data-testid="tweet"]', timeout=20_000)
            except Exception:
                body = (page.inner_text("body")[:400] if page.locator("body").count() else "")
                if re.search(r"rate limit|try again|unusual", body, re.I):
                    raise ScrapeUnavailable(f"X returned a rate-limit or block page: {body[:160]}")
                # No posts and no block page: a genuinely empty search.
                log.info("No posts for %r", query)
                return []

            stalled = 0
            for _ in range(MAX_SCROLLS):
                for t in page.evaluate(_EXTRACT):
                    href = t["href"]
                    if href in seen:
                        continue
                    seen.add(href)
                    out.append(t)
                if len(out) >= max_posts:
                    break
                before = len(seen)
                page.mouse.wheel(0, 3000)
                _sleep(1.2, 2.4)
                stalled = stalled + 1 if len(seen) == before else 0
                if stalled >= 3:      # the timeline stopped producing new posts
                    break
        finally:
            browser.close()

    return [_as_candidate(t) for t in out[:max_posts]]


def _as_candidate(t: dict) -> dict:
    text = " ".join((t["text"] or "").split())
    handle = t["handle"] or "i"
    href = t["href"] or ""
    return {
        "title": text if len(text) <= 120 else text[:117].rstrip() + "...",
        "url": f"https://x.com{href}" if href.startswith("/") else href,
        "source_name": f"@{handle}",
        "snippet": text,
        "published_at": _iso(t["when"]),
        "source_type": "social",
        # Addressed to the entity's own grievance handle, so attribution is
        # established by the query rather than by finding the bank's name in
        # text that will never contain it.
        "attribution_confident": True,
        "billed": False,
    }


def _iso(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None
