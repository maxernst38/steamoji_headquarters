"""Where the data lives, in one place.

Everything used to say `os.path.join("data", ...)`, which is right on a
working machine and wrong everywhere else. A hosted copy reads a published
snapshot instead - a subset, built by tools/snapshot.py, that deliberately
excludes the API token and the YouTube key.

    VEX_DATA_DIR=site-data python ui.py      # read a snapshot
    python ui.py                             # the working data, as before

Read once at import, because every store turns it into a module-level default.
"""
import os

DEFAULT = "data"

# Vercel sets VERCEL on every build and every request, so a deployment finds
# its snapshot without anything being configured in a dashboard.
if os.environ.get("VEX_DATA_DIR"):
    ROOT = os.environ["VEX_DATA_DIR"]
elif os.environ.get("VERCEL") and os.path.isdir("site-data"):
    ROOT = "site-data"
else:
    ROOT = DEFAULT


def path(*parts):
    """A path inside the data root."""
    return os.path.join(ROOT, *parts)
