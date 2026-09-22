# vex-tracker

Turns VEX VRC match video into per-robot paths on a bird's-eye field, as the
foundation for scouting analysis — which teams perform well, how they play, and
what their numbers are.

## Running it

```bash
conda env create -f environment.yml     # first time only
conda activate vex-tracker
python ui.py                            # opens the browser UI
```

The whole per-match workflow lives in the UI:

1. **Choose a video** from `videos/`
2. **Choose a match** — matches are found automatically by detecting camera cuts,
   so you never type frame numbers
3. **Select field** and **Select robots** — both required, red until done
4. **Start processing** — roughly 15 minutes for a full match
5. **Watch the result** — the camera view beside the bird's-eye path it produced

Finished runs are saved, so returning to a match offers the result instantly
rather than reprocessing.

`python track.py VIDEO --start N --end N` does the same from the command line,
through the same pipeline.

## Why both setup steps are mandatory

Neither has a safe default, and both used to be guessed:

- **The field** was stored once per video, but the broadcast camera changes between
  matches — so every match after the first was silently using the wrong geometry.
  Positions stayed plausible while describing a different camera's field.
- **The robots** fell back to motion detection, which finds all four in only about
  half of frames. A robot standing still leaves no trace at all.

Both failures are invisible downstream, which is why they are now explicit.

## Layout

```
ui.py                 launches the web UI (this is the main entry point)
track.py              same pipeline from the command line
calibrate.py          desktop field calibrator; the UI is the primary path now
pipeline.py           the whole run as one callable, shared by UI and CLI

calibration/          field geometry: homography, the drag tool, validation views
detection/            finding and following robots, and drawing the output
storage/              what persists between runs (see storage/README.md)
integrations/         outside data sources (see integrations/README.md)
  refresh.py            one command to update every source, re-fetching only what moved
analysis/             ratings derived from match results (see analysis/README.md)
webapp/               Flask server, team-centric pages, and the setup workspace
  programs.py           V5RC or VIQRC: which catalog a page reads, how a match scores, and the palette
training/             detector training (currently not working — see below)
archive/              approaches that failed, kept for their findings

videos/               input footage (not versioned)
data/                 everything persisted, per match (see data/README.md)
  calibrations/         the field you drew
  seeds/                the robot boxes you drew
  catalog/              teams, events and matches (V5RC)
  catalog_viqrc/        the same tables for VEX IQ — separate because team numbers are reused
  cache/                cached API responses
  vex_token             your VEX Events API token (never committed)
  results/              finished runs
docs/                 evidence images referenced from code comments
deploy/               hosting: a VPS with systemd and Caddy, or Vercel (see deploy/README.md)
api/                  Vercel's entry point: the same Flask app as a function
tools/                snapshot.py builds the published data subset
```

## How a run works

| stage | what it does |
|---|---|
| background | median frame over a wide window; the static field with people and robots erased |
| seed | your drawn boxes become the tracker's starting objects |
| extract | frames written out at ~10fps, which is ample for path tracing |
| track | SAM2 propagates each robot's mask, in chunks so memory stays flat |
| render | camera view and live bird's-eye written side by side |

Positions come from each mask's **ground-contact point** — the bottom edge, where
the robot meets the floor — projected through the homography into field inches.
Using a mask's centroid instead would put positions 14–22 inches out, because
anything above the floor plane violates the homography's assumption.

## Current state

**Works:** calibration, match segmentation, seeding, tracking, path rendering,
result caching, the UI.

**Known limits:**

- Tracks still swap identity when robots collide. Merged stretches are kept and
  drawn faint rather than discarded, since colliding robots are in the same place —
  the position is roughly right, only the identity is doubtful.
- A track that dies stays dead; nothing re-acquires it.
- The trained detector **does not work** and is off by default. It fires
  confidently on a stack of game pieces (`docs/detector-failure.png`) because its
  labels were bootstrapped from a tracker that was itself losing robots. Fixing it
  needs labels drawn by hand rather than harvested from the thing being fixed.

**Not built yet:** game piece detection, scoring, team identity from the
RobotEvents API, and the analysis/stats pages.
