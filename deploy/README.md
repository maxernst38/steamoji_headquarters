# Hosting the site

Two ways, both documented here:

- **[vercel.md](vercel.md)** — serverless, $0/month, data ships with each nightly
  deployment. No server to maintain; every change is a deploy.
- **This file** — a small VPS with systemd and Caddy, ~$5/month. A real machine
  with a disk, which is more forgiving if you later want to run jobs on it.


The hosted copy is **read-only**: with `VEX_READ_ONLY=1` the write routes and
the video workspace are not registered at all, so there is no route that can
change the catalog and nothing to defend. Your own machine keeps every form,
because adding matches and running the tracker is the local workflow.

That also decides the size of the box. The workspace is what pulls in OpenCV,
so a read-only server installs `requirements-web.txt` — Flask, requests, numpy,
gunicorn — and not the analysis stack. Measured: **113 MB** resident with both
catalogs loaded and every heavy page hit.

| | |
|---|---|
| Server | ~$4–6/month for the smallest tier anyone sells |
| Domain | ~$11/year (Cloudflare Registrar sells at cost) |
| Data on the server | ~42 MB |
| Bandwidth | HTML and thumbnails; YouTube serves the video |

## What runs

```
students → https://scout.yourdomain.com
              │  Cloudflare DNS, A record → the server's IP
              ▼
           Caddy ── TLS + password ──▶ gunicorn ──▶ Flask (VEX_READ_ONLY=1)
                                           ▲
        systemd timer, 00:00 local ────────┘   python -m integrations.refresh
                                           │
                                     /srv/vex-tracker/data
```

## Setting it up

Ubuntu 24.04 on any VPS. As root unless noted.

**1. A user and the code**

```
adduser --system --group --home /srv/vex-tracker vex
apt update && apt install -y python3-venv git caddy
sudo -u vex git clone https://github.com/maxernst38/steamoji_headquarters.git /srv/vex-tracker
cd /srv/vex-tracker
sudo -u vex python3 -m venv .venv
sudo -u vex .venv/bin/pip install -r requirements-web.txt
```

**2. The data and the secrets**

`data/` is gitignored, so the clone has none. Copy up what the site needs —
about 42 MB — from your own machine, and leave `data/results` behind: it is
206 MB of processed video output that the server has no use for.

```
rsync -av --progress \
    data/catalog data/catalog_viqrc data/event_details data/cache \
    data/webcasts.json data/team_media.json data/vex_token data/youtube_key \
    vex@your.server.ip:/srv/vex-tracker/data/
ssh vex@your.server.ip 'chmod 600 /srv/vex-tracker/data/vex_token /srv/vex-tracker/data/youtube_key'
```

The API cache is worth carrying: it makes the first refresh on the server cheap
instead of a 35-minute cold walk.

**3. The timezone**, which is also the YouTube quota reset:

```
timedatectl set-timezone America/Los_Angeles
```

**4. The services**

```
cp deploy/vex-tracker.service deploy/vex-refresh.service deploy/vex-refresh.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vex-tracker vex-refresh.timer
systemctl list-timers vex-refresh   # confirms when it next fires
```

**5. The domain and TLS**

Buy the domain (Cloudflare Registrar is at cost), add an **A record** to the
server's IP, then:

```
caddy hash-password          # paste the hash into the Caddyfile
cp deploy/Caddyfile /etc/caddy/Caddyfile   # with your domain in it
systemctl reload caddy
```

Caddy gets and renews the certificate itself. If you use Cloudflare's proxy
(the orange cloud), set SSL mode to **Full (strict)** so it does not talk
plain HTTP to your server.

## Day to day

```
./deploy/deploy.sh                      # pull main and restart
systemctl status vex-tracker            # is the site up
journalctl -u vex-refresh -n 50         # what last night's refresh did
systemctl start vex-refresh             # run a refresh now
```

## When the Learn guides land

`feature/learn-guides` adds two dependencies. Uncomment `markdown` and `pyyaml`
in `requirements-web.txt` before deploying that branch, or the site will fail to
start.

## What this deliberately does not do

- **No auto-deploy on push.** `deploy.sh` is run by you, so main merging does not
  change the live site mid-competition.
- **No video.** Footage is a YouTube link and a start time; nothing is stored or
  served from here.
- **No analysis.** The tracker needs a GPU and 1.2 GB of PyTorch. It stays on your
  machine, and its results are not copied up.
