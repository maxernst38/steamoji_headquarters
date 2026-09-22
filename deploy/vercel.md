# Hosting on Vercel

The site is read-only, and that is what makes Vercel possible: with nothing to
write at runtime, the data can ship inside the deployment and a new deployment
each night *is* the update.

```
push to main ──────────────▶ Vercel builds ──▶ fetches the data snapshot (1.1 MB)
                                           └──▶ deploys

GitHub Actions, 00:30 Pacific
   └─ python -m integrations.refresh     (token and key from repo secrets)
   └─ python -m tools.snapshot           (data/ -> site-data/, 13.8 MB)
   └─ publishes site-data.tar.gz as the `data-latest` release
   └─ pokes a Vercel deploy hook, so new data goes live without a code change
```

**The data does not live in the repository.** It is 13.8 MB that changes every
night, and committing that daily would bloat the history for nothing. `main`
holds code only; the snapshot is a release asset; every build downloads it
through `tools/fetch_data.sh`. That is what lets Vercel deploy `main` on every
push and still serve current data.

Measured locally: importing the app takes **105 ms**, the first page **144 ms**,
later pages **67 ms**. Vercel's own cold start sits on top of that.

## Why the refresh is not a Vercel cron

Vercel's cron calls an HTTP endpoint, with a timeout measured in seconds. The
refresh takes minutes and writes to disk, and a function has neither. So it
runs in Actions, where it has both, and Vercel only ever serves the result.

## Setting it up

### 1. Repository secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | What |
|---|---|
| `VEX_EVENTS_TOKEN` | your VEX Events API token |
| `YOUTUBE_API_KEY` | the YouTube Data API key |

Both are read from the environment by the refresh, so **neither is ever written
to the runner's disk**. The snapshot is an allowlist that refuses to copy
anything whose name looks like a credential, and `data/` is gitignored, so the
publish step has nothing to leak.

### 2. Run the workflow once, by hand

Actions → **Refresh data and publish** → Run workflow.

This is not optional and it comes first: it creates the `data-latest` release
that every build downloads. Without it the Vercel build fails on purpose,
telling you to do this. The first run imports from scratch and takes about 35
minutes; later runs seed from the published snapshot and take a few.

### 3. Import the project into Vercel

1. vercel.com → **Add New… → Project**
2. **Import Git Repository** → authorise GitHub if asked → pick
   `maxernst38/steamoji_headquarters`
3. **Framework Preset: Other.** Leave Root Directory as `./`, and leave the
   Build and Output settings alone — `vercel.json` already sets them.
4. **Deploy.**

No environment variables to add: Vercel sets `VERCEL` itself, which forces
read-only mode and points the data root at `site-data/`.

### 4. Set the production branch to `main`

**This one matters:** this repository's default branch is `dev`, and Vercel
makes the default branch production unless told otherwise. Settings → Git →
**Production Branch → `main`** → Save.

From then on every push to `main` deploys automatically, which is what you
asked for. Pushes to `dev` and pull requests get preview deployments at their
own URLs; if you would rather they did not build at all, Settings → Git →
Ignored Build Step.

### 5. A deploy hook, so new data goes live on its own

Without this the site only changes when you push code — the nightly data would
sit in a release nobody deployed.

1. Vercel → Settings → Git → **Deploy Hooks** → create one named `nightly` for
   branch `main`, and copy the URL
2. GitHub → Settings → Secrets → Actions → add it as **`VERCEL_DEPLOY_HOOK`**

The workflow posts to it after publishing the snapshot. If the secret is
missing the workflow says so and carries on.

### 6. The domain

Vercel → Settings → **Domains** → add yours. It will show a CNAME (or an A
record for a bare domain); add that at Cloudflare. Vercel issues the
certificate. If Cloudflare's proxy is on (orange cloud), set SSL mode to
**Full (strict)**.

## What lives where

| | |
|---|---|
| `main` | what Vercel deploys. Code only — `site-data/` is gitignored |
| `data-latest` release | the snapshot, rebuilt nightly, downloaded by every build |
| `data/` | your working copy, and the only place the tokens exist |
| `site-data/` | the published subset: both catalogs, event details, webcasts, team media |

## Day to day

- **New code goes live** on every push to `main`, through Vercel's Git
  integration. If you would rather approve each release, turn on Settings → Git
  → *Deployment Protection*, or keep merging to `dev` and only merge `dev` into
  `main` when you want the site to change.
- **New data goes live** nightly, or when you run the workflow by hand.
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
