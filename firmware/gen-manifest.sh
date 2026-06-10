#!/usr/bin/env bash
#
# Recompute the sha256 of every hex listed in firmware/manifest.json and write
# the values back, so the committed manifest always matches the committed hexes.
# Run this after building or replacing a .hex. Human-authored fields (changelog,
# eeprom_reset, board_rev, min_compat) are preserved; only sha256 is refreshed.
#
#   bash firmware/gen-manifest.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 - "$HERE/manifest.json" "$HERE" <<'PY'
import hashlib, json, os, sys
manifest_path, fwdir = sys.argv[1], sys.argv[2]
with open(manifest_path) as f:
    m = json.load(f)
changed = 0
for img in m.get("images", []):
    p = os.path.join(fwdir, img["file"])
    if not os.path.exists(p):
        sys.exit(f"missing hex: {p}")
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    if img.get("sha256") != h:
        img["sha256"] = h
        changed += 1
with open(manifest_path, "w") as f:
    json.dump(m, f, indent=2)
    f.write("\n")
print(f"manifest updated ({changed} sha256 change(s))")
PY
