# Hosting on Vercel

The site is read-only, and that is what makes Vercel possible: with nothing to
write at runtime, the data can ship inside the deployment and a new deployment
each night *is* the update.

```
GitHub Actions, 00:30 Pacific
   └─ python -m integrations.refresh     (token and key from repo secrets)
   └─ python -m tools.snapshot           (data/ -> site-data/, 13.8 MB)
   └─ force-push main + snapshot to the `live` branch
        └─ Vercel deploys `live`
                 ↓
students → scout.yourdomain.com → Flask, as one serverless function
```

Measured locally: importing the app takes **105 ms**, the first page **144 ms**,
later pages **67 ms**. Vercel's own cold start sits on top of that.

## Why the refresh is not a Vercel cron

Vercel's cron calls an HTTP endpoint, with a timeout measured in seconds. The
refresh takes minutes and writes to disk, and a function has neither. So it
runs in Actions, where it has both, and Vercel only ever serves the result.

## Setting it up

**1. Repository secrets** — Settings → Secrets → Actions:

| Secret | What |
|---|---|
| `VEX_EVENTS_TOKEN` | your VEX Events API token |
| `YOUTUBE_API_KEY` | the YouTube Data API key |

Both are read from the environment by the refresh, so **neither is ever
written to the runner's disk**. That is also why the publish step cannot leak
one: `data/` is gitignored, and the snapshot is an allowlist that refuses to
copy anything whose name looks like a credential.

**2. Run the workflow once by hand** — Actions → *Refresh data and publish* →
Run workflow. The first run has no previous snapshot to build on, so it
imports from scratch and takes about 35 minutes. Later runs seed from the last
publish and take a few minutes.

**3. Point Vercel at the `live` branch** — import the repository, then in
Settings → Git set the **production branch to `live`**, not `main`. `main`
holds the code; `live` holds the code *and* the data snapshot, and is rebuilt
every night.

No environment variables need setting: Vercel sets `VERCEL` itself, which
forces read-only mode and points the data root at `site-data/`.

**4. The domain** — add it in Vercel, then the CNAME (or A record) it gives you
at Cloudflare. Vercel issues the certificate.

## What lives where

| | |
|---|---|
| `main` | the code. Never carries data; `site-data/` is gitignored here |
| `live` | what Vercel deploys: main's tree plus the snapshot, force-pushed nightly |
| `data/` | your working copy, and the only place the tokens exist |
| `site-data/` | the published subset: both catalogs, event details, webcasts, team media |

## Day to day

- **Publish now:** Actions → Run workflow. Code changes on `main` reach the site
  on the next run, scheduled or manual — deliberately, so a merge does not
  change the live site during a competition.
- **See what happened:** the workflow log shows the refresh's own output, the
  same lines you would see locally.
- **Roll back:** Vercel's deployment list; promote an earlier one.

## Limits worth knowing

- **Hobby is non-commercial.** A school team tool qualifies; read the terms
  yourself.
- **250 MB function bundle.** Flask, numpy and 13.8 MB of data are nowhere near
  it, but PyTorch alone would break it — which is why the analysis stack is
  excluded in `.vercelignore` and never imported in read-only mode.
- **Seconds per request.** Fine for pages, and the reason no long job can ever
  run there.
- **A deploy is the only way data changes.** There is no writable disk, by
  design.

## When the Learn guides land

Uncomment `markdown` and `pyyaml` in `requirements.txt`, or the function will
fail to import.
