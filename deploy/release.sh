#!/usr/bin/env bash
#
# Cut a release the in-app updater can install with one click.
#
#   bash deploy/release.sh "What changed in this release"
#
# Reads the version from APP_VERSION in backend/heatermeterd/api.py, builds the
# frontend, packages backend/heatermeterd + the built frontend/dist into a
# tarball, writes the update.json manifest (sha256-pinned), then tags and
# publishes a GitHub release carrying both assets under STABLE names, so the
# updater's channel URL never changes:
#
#   https://github.com/<owner>/<repo>/releases/latest/download/update.json
#
# Paste that URL once into Settings -> Software Update; every later release is
# then a one-click in-app update (downloaded, sha256-verified, applied by the
# root helper, health-checked, auto-rolled-back on failure).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

CHANGELOG="${1:-}"
if [ -z "$CHANGELOG" ]; then
  echo "usage: bash deploy/release.sh \"changelog text\"" >&2
  exit 1
fi

command -v gh >/dev/null || { echo "gh (GitHub CLI) is required" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "run: gh auth login" >&2; exit 1; }

VERSION="$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' backend/heatermeterd/api.py)"
[ -n "$VERSION" ] || { echo "could not read APP_VERSION" >&2; exit 1; }
TAG="v$VERSION"

if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty; commit first" >&2
  exit 1
fi
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "tag $TAG already exists; bump APP_VERSION first" >&2
  exit 1
fi

OWNER_REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"

echo ">>> Building frontend..."
(cd frontend && npm run build >/dev/null)

echo ">>> Packaging $TAG..."
OUT="$(mktemp -d)"
tar -czf "$OUT/heatermeter.tar.gz" backend/heatermeterd frontend/dist

if command -v shasum >/dev/null; then
  SHA="$(shasum -a 256 "$OUT/heatermeter.tar.gz" | cut -d' ' -f1)"
else
  SHA="$(sha256sum "$OUT/heatermeter.tar.gz" | cut -d' ' -f1)"
fi

python3 - "$OUT/update.json" <<PY
import json, sys
json.dump({
    "schema": 1,
    "version": "$VERSION",
    "url": "https://github.com/$OWNER_REPO/releases/latest/download/heatermeter.tar.gz",
    "sha256": "$SHA",
    "changelog": """$CHANGELOG""",
}, open(sys.argv[1], "w"), indent=2)
PY

echo ">>> Tagging + publishing release $TAG on $OWNER_REPO..."
git tag "$TAG"
git push origin main --tags
gh release create "$TAG" "$OUT/heatermeter.tar.gz" "$OUT/update.json" \
  --title "$TAG" --notes "$CHANGELOG"

rm -rf "$OUT"
echo
echo "Released $TAG (sha256 $SHA)."
echo "Update channel URL (set once in Settings -> Software Update):"
echo "  https://github.com/$OWNER_REPO/releases/latest/download/update.json"
