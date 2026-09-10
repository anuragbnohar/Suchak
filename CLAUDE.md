# Working on Drishti

Drishti (दृष्टि, formerly Suchak) watches news, social media and public
grievance forums for what is being said about regulated entities, classifies
it, and puts it in front of a supervisor to rule on. FastAPI, Jinja2, one
SQLite file. No build step, no JavaScript framework.

**This repository is public.** No key, token, password or server address
belongs in it — not in code, not in a comment, not in this file. Secrets live
in environment variables, and on the server in `/etc/drishti/drishti.env`,
which only root can read.

## Where it runs

Live at **rdrishti.in**, on a small Ubuntu droplet in Bangalore, reached
through a Cloudflare tunnel — nothing is opened on the server and its address
is not published. `HOSTING.md` is the whole story; the short version:

| | |
|---|---|
| App | `/opt/drishti`, owned by the `drishti` account, read-only to the service |
| Database | `/var/lib/drishti/suchak.db` |
| Settings and keys | `/etc/drishti/drishti.env` |
| Service | `systemd`, restarts after a crash or reboot |
| Update | `sudo drishti-update` — backs the database up first, then fetches |
| Undo | `sudo drishti-rollback` — safe; every migration only adds columns |

`drishti-update` replaces the copy of itself and of `drishti-rollback` from
the code it fetched, so a fix to those scripts arrives by the ordinary route.

The laptop copy is retired. Two copies drifting apart is how supervisory work
gets lost.

## Shipping a change

1. Build it.
2. Test it — see below.
3. **Bump `APP_BUILD`** in `app/main.py`. It is stamped in the page footer and
   on the stylesheet URL; it is how the user confirms a change actually
   arrived, and how a cached stylesheet is prevented from outliving it.
4. Commit and push to the working branch.
5. Reply in plain language with what changed and the one command to pick it
   up. The user is not a programmer: no jargon, no shell unless it is a line
   to paste.

Templates do not auto-reload (`templates.env.auto_reload = False`), so a
template change needs a restart to be seen.

## Testing

Suites live in the scratchpad as `t_*.py`, one file per subject, each printing
`ALL PASS` or the failures. Set `SUCHAK_DB` to a temporary path **before**
importing anything from `app`. Run the lot before every push; there are
seventy-odd and they take a few minutes.

Prefer a test that reads the truth from the source over one that repeats a
list by hand. `t_guest.py` reads every `@app.post` out of `main.py` and fails
on any route not recorded in its `DECIDED` map, so a handler written next year
is a decision about what a guest may do whether or not its author thought so.
(This file previously claimed that check existed when it did not, and a new
route did slip past unchecked before it was built. Do not describe a safety
net here without opening the test and reading it.)

Screenshots catch what assertions do not: staggered rows, controls pushed off
a panel, a heading that tells somebody to open something that is not there.
Take one for anything visual, and measure in the browser rather than trusting
the eye.

## Names that are deliberately wrong

Renaming these would break an installation that already exists, so the storage
name stayed and only what a person reads changed. Do not "fix" them.

| Stored as | Read as |
|---|---|
| `suchak.db`, `SUCHAK_*` env vars | Drishti |
| `factors` table, `items.factor_matches`, `?factor=`, `.factor-chip` | Alert |
| the classifier prompt's word "factor" | Alert |

## Who may do what

Two separate ideas, and they compose:

- **Role** — `member`, `lead`, `superadmin` — decides what somebody *sees*.
  The Overview tab and sight of every entity are superadmin only.
- **Guest** (`users.guest`) — decides what they may *do*. A guest reviews,
  sets aside, closes to-dos and raises alerts as their role allows, and is
  refused anything that spends money (Fetch, Generate insights, Re-check
  alerts, Re-score) or reshapes the installation (Settings, Policy, People,
  entities).

Super admin **plus** guest is the combination for somebody who should see the
whole picture and change nothing that matters.

The refusal lives in one place, `require_login` in `app/auth.py`, which every
route already calls. Add to `GUEST_BLOCKED` or `GUEST_BLOCKED_ACTIONS` rather
than checking inside a handler — the handler somebody writes next year will
not remember to check. Changing your own password is always allowed.

A fourth role is not possible: SQLite cannot widen the `role` CHECK on a
database that already exists. That is why `guest` and `rbi_office` are
columns.

## Things learned the hard way

- **A tick is not a verdict.** `review_relevant IS NOT NULL` is what marks a
  real review — that is the test `classify.py` uses when gathering precedents
  for the model. The Reviewed tick on the queue and social cards leaves it
  NULL, so a ticked item is never held up as an example of anything.
- **Reviewer wins.** `COALESCE(review_x, x)` everywhere; a human ruling
  outranks the model's.
- **Never state a number you cannot stand behind.** Say "not recorded"
  rather than repeating the row count under a different name.
- **Only print what is true.** The sign-in screen names a demo login only
  while that password still works.
- **Migrations add columns.** `MIGRATIONS` is a list of
  `(table, column, declaration)` and nothing else, which is what makes
  rolling back safe: an older build simply ignores a column it does not know.
  One column has been renamed (`read_only` → `guest`), by a one-off step that
  runs before the migrations and carries the value over; the reader that uses
  it tolerates the column being absent, so a rollback past it degrades rather
  than crashes. Follow that shape if a rename is ever needed again.
- **A shared conversation is not a shared person.** Repeat complaints fold by
  author, never by `thread_key`: on X the conversation id belongs to the root
  of the conversation, so a bank's own post that fifty customers reply to
  gives all fifty replies one thread key. `app/grouping.py` uses the thread
  only to describe a group that is already one person's. The same rule as
  Phase 1's counting — an item naming nobody is folded with nothing, so the
  figure overstates the number of people rather than understating it.
- **Fold after the sort, never before.** The lead of a group is whichever of
  its posts the screen's own ordering put first, which is the only reason a
  card can be trusted to carry the most serious thing that person said. Cap
  the list after folding too, or one persistent customer takes six of the
  sixty places.
- Fetching is manual. A timer would bill the account with nobody watching.
- `pkill` in this sandbox returns 144 and kills the rest of a compound
  command; run it on its own.
