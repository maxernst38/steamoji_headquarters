"""Build the data snapshot a hosted copy reads.

    python -m tools.snapshot                 # data/ -> site-data/
    python -m tools.snapshot --out published

The hosted site is read-only and has no disk to write to, so its data ships
with the deployment. This copies the files the pages actually read and nothing
else.

The list is an allowlist, not an exclude list, and that is the whole point:
`data/` holds the VEX API token and the YouTube key, and a snapshot built by
excluding things is one forgotten rule away from publishing them. Anything not
named here does not travel. The tool also refuses to write a file whose name
looks like a credential, so a mistake in the list is caught rather than
deployed.
"""
import argparse
import os
import shutil

from storage import paths

# (name in the data root, whether it must exist)
INCLUDE = [
    ("catalog", True),                 # V5RC teams, events, matches
    ("catalog_viqrc", False),          # the same for VIQRC
    ("event_details", False),          # awards, rankings, skills
    ("webcasts.json", False),          # one stream link per event
    ("team_media.json", False),        # YouTube channels and robot videos
    ("team_media_viqrc.json", False),
]

# Never, whatever the list above says.
SECRETS = ("vex_token", "youtube_key", ".env", "id_rsa")


def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(root, name))
               for root, _, names in os.walk(path) for name in names)


def build(source=None, out="site-data", log=print):
    """Copy the published subset into `out`, replacing whatever was there."""
    source = source or paths.ROOT
    if os.path.abspath(source) == os.path.abspath(out):
        raise SystemExit("the snapshot cannot be built on top of its own source")

    # Replaced rather than merged: a record deleted upstream has to disappear
    # here too, or the site would keep serving it forever.
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out, exist_ok=True)

    total, written = 0, []
    for name, required in INCLUDE:
        if any(secret in name for secret in SECRETS):
            raise SystemExit(f"refusing to publish {name}: it looks like a credential")
        origin = os.path.join(source, name)
        if not os.path.exists(origin):
            if required:
                raise SystemExit(f"missing {origin} - is {source} the right data directory?")
            log(f"  (skipped {name}, not present)")
            continue
        target = os.path.join(out, name)
        if os.path.isdir(origin):
            # Dotfiles are working state, not data: .lock is the cross-process
            # write lock, which a read-only copy never takes.
            shutil.copytree(origin, target,
                            ignore=lambda _, names: [n for n in names if n.startswith(".")])
        else:
            shutil.copy2(origin, target)
        size = _size(target)
        total += size
        written.append((name, size))
        log(f"  {name:<24} {size / 1e6:6.1f} MB")

    leaked = [f for f in os.listdir(out) if any(s in f for s in SECRETS)]
    if leaked:
        raise SystemExit(f"a credential reached the snapshot: {leaked}")

    log(f"  {'total':<24} {total / 1e6:6.1f} MB in {out}/")
    return {"out": out, "bytes": total, "files": written}


def main():
    parser = argparse.ArgumentParser(description="Build the published data snapshot")
    parser.add_argument("--source", help=f"data directory to read (default {paths.ROOT})")
    parser.add_argument("--out", default="site-data", help="where to write it (default site-data)")
    args = parser.parse_args()
    build(args.source, args.out)


if __name__ == "__main__":
    main()
