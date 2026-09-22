#!/usr/bin/env bash
# Fetch the published data snapshot into site-data/, at build time.
#
# The site's data cannot live in the repository: it is 13.8MB that changes
# every night, and committing that daily would bloat the history for nothing.
# So `main` holds only code, the nightly workflow publishes the snapshot as a
# release asset, and every Vercel build pulls it in here. That is what lets
# Vercel deploy `main` on every push and still serve current data.
#
# Overridable with DATA_URL, for a fork or a different host.
set -euo pipefail

OWNER="${VERCEL_GIT_REPO_OWNER:-maxernst38}"
REPO="${VERCEL_GIT_REPO_SLUG:-steamoji_headquarters}"
URL="${DATA_URL:-https://github.com/${OWNER}/${REPO}/releases/download/data-latest/site-data.tar.gz}"

echo "==> fetching the data snapshot"
echo "    $URL"

if ! curl -fsSL --retry 3 -o /tmp/site-data.tar.gz "$URL"; then
  echo
  echo "Could not download the data snapshot." >&2
  echo "Run the 'Refresh data and publish' workflow once - it builds the" >&2
  echo "release this build reads - then deploy again." >&2
  exit 1
fi

rm -rf site-data
tar xzf /tmp/site-data.tar.gz
du -sh site-data
