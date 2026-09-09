# Putting Drishti on rdrishti.in

This is the whole job, in order, for a Windows laptop. Nothing in the
application changes — Drishti has no hostname written into it anywhere, so it
serves `rdrishti.in` exactly as it serves `localhost`.

Read **[README.md → Putting it on an address other people can reach](README.md)**
first and finish the hardening it describes. If `SUCHAK_PUBLIC=1` is not set
and every demo password has not been changed, stop here and do that. The rest
of this file assumes `python run.py` already starts cleanly with public mode on.

## What you are building

Your laptop keeps Drishti running on `localhost:8000`, exactly as it does now.
A small Cloudflare program runs alongside it and makes an outbound connection
to Cloudflare. When somebody visits `rdrishti.in`, Cloudflare hands the request
down that connection.

Nothing is opened on your router, no port is forwarded, and your home or office
IP address is never published. HTTPS is terminated by Cloudflare and the
certificate is issued and renewed for you.

**The catch, stated plainly:** the site is up only while your laptop is on and
`python run.py` is running. Close the window or shut the lid and the address
stops answering. That is fine for showing colleagues a prototype. It is not
fine for something people are told to rely on — see *When the laptop is not
enough* at the end.

## Step 1 — Point the domain at Cloudflare

1. Sign up at **dash.cloudflare.com** (free).
2. **Add a site** → type `rdrishti.in` → choose the **Free** plan.
3. Cloudflare shows you **two nameservers**, something like
   `xxx.ns.cloudflare.com`. Copy both.
4. Sign in wherever you bought the domain. Find **Nameservers** (sometimes
   under *DNS*, *Manage domain*, or *Custom nameservers*). Replace what is
   there with Cloudflare's two.
5. Wait. Cloudflare emails you when the domain says **Active** — usually a few
   minutes for a `.in`, occasionally up to a day.

Do not go on until the domain shows **Active** in Cloudflare.

## Step 2 — Create the tunnel

1. Go to **one.dash.cloudflare.com** (Cloudflare Zero Trust). On first visit it
   asks you to pick a team name — any name — and a plan. Choose **Free**.
2. **Networks → Tunnels → Create a tunnel → Cloudflared**.
3. Name it `drishti`. Save.
4. It now shows install commands. Choose the **Windows** tab and copy the
   command. It is one long line containing a very long token.
5. Open **PowerShell as Administrator** (right-click Start → *Terminal
   (Admin)*) and paste it. This installs the Cloudflare program and registers
   it as a Windows service, so it starts by itself whenever the laptop boots.
6. Back in the browser the tunnel should turn **HEALTHY** within a few seconds.

The token in that command is a password for your tunnel. Do not paste it into
email, chat, or any file you commit.

## Step 3 — Send the domain to the application

Still in the tunnel's setup page, open the **Public Hostname** tab and
**Add a public hostname**:

| Field | Value |
|---|---|
| Subdomain | *leave empty* |
| Domain | `rdrishti.in` |
| Path | *leave empty* |
| Type | `HTTP` |
| URL | `localhost:8000` |

Save.

`HTTP` is correct and is not a downgrade. That setting describes only the hop
inside your own laptop, from Cloudflare's program to Python. The public half of
the journey is HTTPS, which is why `SUCHAK_PUBLIC=1` marks the sign-in cookie
HTTPS-only.

Now start the application in an ordinary PowerShell window:

```powershell
cd C:\path\to\Suchak
python run.py
```

Visit **https://rdrishti.in**. You should get the Drishti sign-in page.

If you want `drishti.rdrishti.in` rather than the bare domain, put `drishti` in
the Subdomain field instead of leaving it empty. You can add both.

## Step 4 — Decide who is allowed in (do not skip)

Right now anyone in the world can reach your sign-in page. Drishti's own
passwords are the only thing between them and the data. Cloudflare **Access**
puts a second door in front, and refuses anyone whose email you have not
listed — before they see Drishti at all. It is free for up to 50 people.

1. **Zero Trust → Access → Applications → Add an application → Self-hosted**.
2. Name: `Drishti`. Domain: `rdrishti.in` (leave subdomain empty to match
   what you set in Step 3).
3. **Add a policy**: name it `Team`, action **Allow**, and under *Include*
   choose **Emails** — then type each colleague's email address, one per line.
4. Save.

From then on, visiting `rdrishti.in` asks for an email address, sends a
one-time code to it, and only then shows Drishti's own sign-in page. Somebody
who guesses a Drishti password still cannot get in without an email you
listed. Removing a colleague is deleting a line here.

For a tool that carries grievance data about regulated entities, this is worth
the ten minutes.

## Step 5 — Make it survive a reboot

The Cloudflare half already does; the Windows service handles it. Python does
not — closing the PowerShell window stops the site.

The simple answer is to leave the window open and minimised. If you would
rather it started on its own:

1. Open **Task Scheduler** → **Create Task**.
2. *General*: name `Drishti`, tick **Run whether user is logged on or not**.
3. *Triggers* → New → **At startup**.
4. *Actions* → New → Program: `python`, Arguments: `run.py`, Start in:
   `C:\path\to\Suchak`.
5. Save.

Task Scheduler runs under a different account, so it sees the machine-wide
environment variables `setx` writes but not anything you typed into one
PowerShell window. If Drishti refuses to start from the task, that is almost
always a missing `SUCHAK_SECRET` — the refusal message names what it wants.

## Checking it worked

- **https://rdrishti.in** shows the sign-in page, with a padlock in the
  address bar.
- The **build number in the page footer** matches the build you are running.
- Signing in works, and stays signed in as you move between pages. (If it
  signs you straight back out, the cookie is being dropped — confirm the
  address really is `https://` and not `http://`.)
- Try it **from a colleague's machine in the office**, early. Office web
  filters, not Cloudflare, are the usual reason a link does not open.

## When the laptop is not enough

Move Drishti onto a small always-on server — any ₹400–800/month Linux VPS is
ample — the day either of these becomes true:

- People start relying on it being there, rather than looking at it when you
  show them.
- It holds review decisions you would be sorry to lose.

The database is a single file, `suchak.db`. Moving to a server is: copy the
folder and that file across, set the same environment variables, run the same
`python run.py`, and point the same tunnel at it. Nothing about Cloudflare or
the domain changes.

## A word about the name

`rdrishti.in` is yours personally. Keep it presented as a prototype you built,
and avoid wording that implies it is an official RBI system or an official
publication. If Drishti is ever adopted properly, the address should be issued
by RBI's own IT rather than carried on a domain in your name — that is a
question of institutional record, not of technology.
