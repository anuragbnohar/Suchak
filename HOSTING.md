# Putting Drishti on rdrishti.in

Drishti has no hostname written into it anywhere, so hosting it needs no
change to the application. What it needs is a machine that stays on.

There are two ways to do this, and only one of them survives the lid closing.

| | Laptop | Small server |
|---|---|---|
| Up when the laptop sleeps | no | **yes** |
| Cost | nothing | about ₹530/month |
| Setup | 20 minutes | 40 minutes, once |
| Good for | showing someone this week | anything people rely on |

**If you want the site up regardless of your laptop, use the server.** That is
the main path below. The laptop route is kept at the end for a quick demo.

Before either: finish the hardening in
**[README.md → Putting it on an address other people can reach](README.md)**.
If `python run.py` does not already start cleanly with `SUCHAK_PUBLIC=1`, stop
and do that first.

---

## What you are building

A small Linux server runs Drishti and nothing else. Cloudflare's tunnel program
runs beside it and dials *out* to Cloudflare. When somebody visits
`rdrishti.in`, Cloudflare passes the request down that connection.

No port is opened on the server, its address is never published, and the HTTPS
certificate is issued and renewed for you. Your laptop is not involved at all
once this is done.

---

## Step 1 — Rent the server

Any small Linux server will do; Drishti is not demanding. Take the smallest
size — 1 GB of memory is ample.

**DigitalOcean** is the easiest to start with. You are buying **one Droplet**
— their word for a small server. Nothing else on the site is needed.

At digitalocean.com, **Create → Droplet**:

| Screen | Choose |
|---|---|
| Region | **Bangalore (BLR1)** |
| Image | **Ubuntu 24.04 (LTS) x64** |
| Droplet type | **Basic** (shared CPU) |
| CPU option | **Regular · SSD · $6/month** — 1 GB / 1 CPU / 25 GB |
| Authentication | **SSH Key** (below) — or a password if you must |
| Hostname | `drishti` |

Then **Create Droplet**. You get an IP address like `164.52.x.x`. That is your
server.

Bangalore keeps the data in India, which is the better answer if anyone ever
asks where a supervisory tool lives.

**$6 is genuinely enough, not a corner cut.** Drishti holds about 60 MB of
memory and does not grow as people use it — measured over a few hundred page
loads. The 1 GB size has room to spare, and systemd restarts the app if it ever
is killed.

### Tick these while ordering

- **Backups** (+$1.20/month) — worth it. This is what saves you if the server
  itself is lost, which the update backups do not cover.
- **Monitoring** — free. Emails you if the machine is struggling.

**IPv4 or IPv6?** Not a choice you have to make: every droplet gets a public
IPv4 address automatically, and that is the one you will `ssh` to. IPv6 is a
free tick-box under *Advanced Options*. Leave it **unticked** — nothing here
uses it. The tunnel dials outward to Cloudflare, so no visitor ever connects to
the server's address at all, and a second address reachable from the internet
is one more thing that has to stay shut.

### Decline these

DigitalOcean will offer a good deal more. None of it applies here:

| Offered | Why not |
|---|---|
| Managed Database | Drishti's database is a file on the disk. This would be ₹1,300+/month for nothing. |
| Block storage volume | 25 GB is far more than this needs. |
| Load Balancer, Kubernetes, App Platform | For sites across many servers. You have one. |
| Premium / CPU-Optimized droplets | Several times the price for speed nothing here needs. |
| Domains / DNS | You already have `rdrishti.in`, and its DNS goes to Cloudflare, not here. |

**Paying from India.** New accounts usually get a free trial credit, so the
first months may cost nothing. When it does start charging, some Indian cards
refuse recurring international payments under the e-mandate rules — if the card
is declined, PayPal or a different card is the usual fix. That is a bank
setting, not a problem with the server.

Alternatives, if you prefer: **AWS Lightsail** (Mumbai, $5), **Linode**
(Mumbai), or **E2E Networks** — an Indian company with Indian data centres,
if that matters for how this is described internally.

### Making an SSH key (worth the two minutes)

In PowerShell on your laptop:

```powershell
ssh-keygen -t ed25519
```

Press Enter at every prompt. Then show the public half and copy it:

```powershell
Get-Content ~\.ssh\id_ed25519.pub
```

Paste that into DigitalOcean's **New SSH Key** box. You will then sign in to
the server with no password at all, and nobody can guess their way in.

---

## Step 2 — Set Drishti up, in one command

Connect to the server from PowerShell:

```powershell
ssh root@YOUR-SERVER-IP
```

Say `yes` to the fingerprint question the first time. Then paste this:

```bash
curl -fsSL https://raw.githubusercontent.com/anuragbnohar/Suchak/claude/file-review-suggestions-9nqs4a/deploy/server-setup.sh -o setup.sh
sudo bash setup.sh
```

It installs Python, fetches the code, creates an account for Drishti to run
under, generates a session secret, asks once for your Anthropic API key, and
registers Drishti as a service that starts itself after a reboot or a crash.

The key is typed straight into the server and stored in a file only root can
read. It never passes through this chat, a browser, or the repository.

It is safe to run again later; it updates rather than replaces, and never
touches the database or the key you typed.

When it finishes, check:

```bash
sudo systemctl status drishti
```

`active (running)` in green is what you want.

---

## Step 3 — The tunnel

At **one.dash.cloudflare.com** (Cloudflare Zero Trust; pick any team name and
the Free plan on first visit):

1. **Networks → Tunnels → Create a tunnel → Cloudflared**. Name it `drishti`.
2. Choose the **Debian** tab. Copy the command it shows — one long line with a
   long token in it.
3. Paste it into your server's SSH window.

The tunnel should go **HEALTHY** within seconds.

> Already installed the tunnel on your laptop? Run
> `cloudflared service uninstall` there first, then use the same token on the
> server. Running it in both places splits traffic between them.

The token is a password for your tunnel. Do not put it in email or chat.

---

## Step 4 — Point the domain at it

Your domain must be on Cloudflare first: at **dash.cloudflare.com**, *Add a
site* → `rdrishti.in` → **Free** plan. It gives you two nameservers; put those
into the nameserver setting wherever you bought the domain. Wait for Cloudflare
to say **Active** — usually minutes for a `.in`.

Then, on the tunnel's **Public Hostname** tab, **Add a public hostname**:

| Field | Value |
|---|---|
| Subdomain | *leave empty* |
| Domain | `rdrishti.in` |
| Path | *leave empty* |
| Type | `HTTP` |
| URL | `localhost:8000` |

`HTTP` there is correct and is not a downgrade. It describes only the hop
inside the server, from the tunnel program to Python. The public half of the
journey is HTTPS, which is why `SUCHAK_PUBLIC=1` marks the sign-in cookie
HTTPS-only.

Visit **https://rdrishti.in**. You should get the Drishti sign-in page.

---

## Step 5 — Who is allowed in

Once the address answers publicly, anybody can reach the sign-in page. Drishti's
own passwords are what stand behind it, and from build .58 a wrong guess costs
something: eight failures on one account name from one address and that
combination is refused for fifteen minutes, whether or not the password is
right, and every wrong password waits a second before it answers. A person
mistyping notices neither; a program working through a word list finds both
ruinous. The count is per name **and** per address on purpose — otherwise
anyone could lock a colleague out of their own account by failing on it
deliberately. Failed attempts are summarised on **Settings**, so an attempt on
the door is something you can see rather than something buried in a log.

That is enough for a prototype behind a domain nobody advertises. **Cloudflare
Access** is the stronger option, and worth adding if it fits: it turns people
away by email address before Drishti is reached at all.

It fits when your users number **under 50** — that is Cloudflare's free tier.
Beyond that it is roughly **$3 per user per month**, which for a hundred
colleagues is about ₹26,000 a month, out of all proportion to what this is. It
also means two sign-ins for everyone: an emailed code, then Drishti's own.

If it does fit:

1. **Zero Trust → Access → Applications → Add an application → Self-hosted**
2. Name `Drishti`, domain `rdrishti.in` (subdomain empty, matching Step 4).
3. **Add a policy**: name `Team`, action **Allow**, and under *Include* choose
   **Emails** — then list each colleague's address. Start with only your own,
   to check it before anyone depends on it.
4. Save.

**Past fifty users, neither of these is really the answer.** A sign-in this
application implements itself is past what it was built for at that scale; the
right answer is authentication issued by RBI's own IT, so people use the
credentials they already have and leaving the organisation removes their access
without anybody remembering to do it.

---

## Step 6 — Your account

**If you are bringing your laptop's database across** (next section), your
existing accounts come with it and there is nothing to do here.

**If you are starting fresh**, a hosted copy deliberately begins with an empty
roster — the demo logins in the README would otherwise be three published
passwords on the open internet. Make the first account on the server:

```bash
cd /opt/drishti
sudo -u drishti .venv/bin/python -m app.newuser
```

It asks for a username, your name, and a password twice, and makes a super
admin. Everyone else is added afterwards from **Settings → People** in the
browser.

---

## Bringing your existing data across

Your reviews, entities and alerts live in one file, `suchak.db`. To move them:

**On your laptop**, stop Drishti (Ctrl+C in its window), then make a clean copy
and send it up:

```powershell
cd C:\path\to\Suchak
python -c "import sqlite3; s=sqlite3.connect('suchak.db'); d=sqlite3.connect('drishti-copy.db'); s.backup(d); d.close(); s.close()"
scp drishti-copy.db root@YOUR-SERVER-IP:/tmp/
```

**On the server**, put it in place:

```bash
sudo systemctl stop drishti
sudo mv /tmp/drishti-copy.db /var/lib/drishti/suchak.db
sudo chown drishti:drishti /var/lib/drishti/suchak.db
sudo systemctl start drishti
```

Sign in with the password you already use. Your laptop copy is untouched, so
nothing is lost if this goes wrong — you can simply try again.

---

## Living with it

```bash
sudo systemctl status drishti     # is it running?
sudo journalctl -u drishti -f     # what is it saying? (Ctrl+C to stop watching)
sudo systemctl restart drishti    # restart it
sudo drishti-update               # fetch and run the newest version
sudo drishti-rollback             # undo the last update
sudoedit /etc/drishti/drishti.env # add a key, then restart
```

**Adding a source key later** — YouTube, X — means editing
`/etc/drishti/drishti.env` and restarting. The optional lines are already in
that file, commented out with a `#`; delete the `#`, add the key, save, then
`sudo systemctl restart drishti`.

**Automatic fetching.** Drishti fetches only when somebody presses Fetch. Now
that it is always on, `SUCHAK_FETCH_MINUTES` in that file would make it sweep
every entity on a timer instead. Think before switching it on: it bills your
Anthropic account with nobody watching.

**Backups.** `drishti-update` keeps the last ten copies of the database in
`/var/lib/drishti/backups/`. Those protect you from a bad update, not from
losing the server itself — that is what the Droplet backups you ticked when
ordering are for. You can also pull a copy down to your laptop now and then:

```powershell
scp root@YOUR-SERVER-IP:/var/lib/drishti/backups/*.db .
```

---

## Getting new versions of the application

This is the only part of your routine that changes. Everything up to GitHub
stays as it is: a change is built, tested and pushed to the branch. What
changes is how it reaches you — instead of downloading a ZIP and extracting it
over your folder, the server fetches it itself.

From PowerShell on your laptop, in one line:

```powershell
ssh root@YOUR-SERVER-IP "/usr/local/bin/drishti-update"
```

Or, if you are already signed in to the server, just `sudo drishti-update`.

That is the whole thing. It:

- compares what you have against what is on the branch, and stops there if
  there is nothing new — so running it out of habit costs nothing;
- **copies the database first**, because a new version can change the database
  the moment it starts;
- fetches the new version, installs anything new it needs, and restarts;
- checks that Drishti actually came back, and if it did not, prints the exact
  command to return to the version that was working.

Confirm it landed the way you always have: **the build number in the page
footer**. If the footer still shows the old number, your browser is showing you
a cached page — reload with Ctrl+F5.

### If a new version turns out to be wrong

```powershell
ssh root@YOUR-SERVER-IP "/usr/local/bin/drishti-rollback"
```

This puts back the version that was running before the last update, and says
so. It is safe: every database change this application has ever made *adds* a
column — nothing is dropped or rewritten — so an older version simply ignores
what it does not recognise, and your reviews are untouched either way.

`sudo drishti-update` brings you forward again whenever you are ready.

### What does not happen by itself

The server never updates on its own. A change I push sits on the branch until
you run the command. That is deliberate: a supervisory tool should not change
under the people using it because something was pushed, and an update that
lands while nobody is watching is an update nobody notices has broken.

It *can* be automated, if you would rather. I would not, for the reason above.

---

## What it costs

| | |
|---|---|
| Droplet (the server) | about ₹530/month ($6) |
| Droplet backups | about ₹110/month ($1.20) — recommended |
| Cloudflare tunnel and Access | free (Access is free to 50 people) |
| Domain | already bought, roughly ₹800/year |
| Anthropic | only what classification actually uses |

---

## The laptop route, if you only need a demo

Everything above except the server: install `cloudflared` on Windows with the
command from Cloudflare's **Windows** tab, point the public hostname at
`localhost:8000`, and leave `python run.py` running. Cloudflare's half restarts
with the laptop; Python does not, so the window must stay open. The site is up
only while the laptop is.

---

## Checking it worked

- **https://rdrishti.in** shows the sign-in page, with a padlock in the bar.
- The **build number in the page footer** matches the version you expect.
- Signing in works and stays signed in as you move between pages. (If it signs
  you straight back out, confirm the address is `https://`, not `http://`.)
- Close your laptop entirely and load the site from your phone. That is the
  test that this page exists for.
- Try it **from a colleague's machine in the office**, early. Office web
  filters, not Cloudflare, are the usual reason a link does not open — and you
  want to find that out now, not during a meeting.

---

## A word about the name

`rdrishti.in` is registered to you personally. Keep it presented as a prototype
you built, and avoid wording that implies an official RBI system or an official
publication. If Drishti is ever adopted properly, the address and the server
should be issued by RBI's own IT rather than carried on a domain and a rented
machine in your name — that is a question of institutional record, not of
technology.
