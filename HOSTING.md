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

**DigitalOcean** is the easiest to start with. At digitalocean.com:

1. **Create → Droplet**
2. Region: **Bangalore (BLR1)** — keeps the data in India, which is the
   better answer if anyone ever asks where a supervisory tool lives.
3. Image: **Ubuntu 24.04 (LTS)**
4. Size: **Basic → Regular → $6/month** (1 GB / 1 CPU / 25 GB)
5. Authentication: **SSH Key** if you can (below), otherwise a password.
6. Hostname: `drishti`. Create.

You get an IP address like `164.52.x.x`. That is your server.

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

## Step 5 — Decide who is allowed in (do not skip)

As it stands, anyone in the world reaches your sign-in page, with Drishti's own
passwords the only thing behind it. Cloudflare **Access** puts a second door in
front and turns away anyone whose email you have not listed — before they see
Drishti at all. Free for up to 50 people.

1. **Zero Trust → Access → Applications → Add an application → Self-hosted**
2. Name `Drishti`, domain `rdrishti.in` (subdomain empty, matching Step 4).
3. **Add a policy**: name `Team`, action **Allow**, and under *Include* choose
   **Emails** — then list each colleague's address.
4. Save.

Visitors now get a one-time code emailed to them before Drishti's own sign-in
appears. Somebody who guesses a Drishti password still cannot get in.

For a tool carrying grievance data about regulated entities, this is ten
minutes well spent.

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
sudoedit /etc/drishti/drishti.env # add a key, then restart
```

**Updating replaces the ZIP ritual.** `sudo drishti-update` fetches the newest
version, backs the database up first (a new version can bring database changes,
which run the moment it starts), restarts, and tells you if it failed to come
back — with the command to return to the version that worked. Confirm it landed
by checking the build number in the page footer, exactly as you do now.

**Backups.** `drishti-update` keeps the last ten copies in
`/var/lib/drishti/backups/`. That protects you from a bad update, not from
losing the server. For that, turn on your provider's backups (DigitalOcean:
about $1.20/month), or pull a copy down to your laptop now and then:

```powershell
scp root@YOUR-SERVER-IP:/var/lib/drishti/backups/*.db .
```

**Adding a source key later** — YouTube, X — means editing
`/etc/drishti/drishti.env` and restarting. The optional lines are already in
the file, commented out with a `#`; delete the `#`, add the key, save,
`sudo systemctl restart drishti`.

**Automatic fetching.** Drishti fetches only when somebody presses Fetch. Now
that it is always on, `SUCHAK_FETCH_MINUTES` in that file would make it sweep
every entity on a timer instead. Think before switching it on: it bills your
Anthropic account with nobody watching.

---

## What it costs

| | |
|---|---|
| Server | about ₹530/month ($6) |
| Server backups | about ₹110/month, optional |
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
