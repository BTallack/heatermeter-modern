"""Pure logic for the in-software AVR firmware updater.

Everything here is unit-testable without hardware: manifest loading and
validation, sha256 hashing, IPC request marshalling, progress/result parsing,
and the pre-flight safety guard. The privileged flash itself runs in a separate
root helper (deploy/hm-flash); the daemon orchestration that drives it lives in
service.py. Nothing in this module shells out or touches the board.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

# Manifest schema version this code understands.
MANIFEST_SCHEMA = 1

# Required keys on each manifest image entry.
_IMAGE_REQUIRED = ("version", "file", "sha256")

# A sha256 hex digest is 64 lowercase hex chars.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Valid IPC actions the root helper understands.
ACTIONS = ("flash", "rollback", "backup")


class ManifestError(ValueError):
    """Raised when a firmware manifest is missing or malformed."""


def sha256_file(path: str, _bufsize: int = 65536) -> str:
    """Return the lowercase hex sha256 of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def validate_manifest(d: dict) -> dict:
    """Validate a parsed manifest dict; return it unchanged or raise.

    Checks the schema version, that ``images`` is a non-empty list, and that
    each image has the required fields with a well-formed sha256 and a unique
    version string.
    """
    if not isinstance(d, dict):
        raise ManifestError("manifest is not an object")
    schema = d.get("schema")
    if schema != MANIFEST_SCHEMA:
        raise ManifestError(f"unsupported manifest schema {schema!r}")
    images = d.get("images")
    if not isinstance(images, list) or not images:
        raise ManifestError("manifest has no images")
    seen = set()
    for img in images:
        if not isinstance(img, dict):
            raise ManifestError("image entry is not an object")
        for k in _IMAGE_REQUIRED:
            if not img.get(k):
                raise ManifestError(f"image missing required field {k!r}")
        sha = str(img["sha256"]).lower()
        if not _SHA256_RE.match(sha):
            raise ManifestError(
                f"image {img['version']!r} has a malformed sha256")
        ver = img["version"]
        if ver in seen:
            raise ManifestError(f"duplicate image version {ver!r}")
        seen.add(ver)
    return d


def load_manifest(path: str) -> dict:
    """Load and validate the manifest JSON at *path*."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except FileNotFoundError as e:
        raise ManifestError(f"manifest not found: {path}") from e
    except json.JSONDecodeError as e:
        raise ManifestError(f"manifest is not valid JSON: {e}") from e
    return validate_manifest(d)


def find_image(manifest: dict, version: str) -> Optional[dict]:
    """Return the manifest image entry for *version*, or None."""
    for img in manifest.get("images", []):
        if img.get("version") == version:
            return img
    return None


def clean_version(version: Optional[str]) -> Optional[str]:
    """Strip the trailing board-rev letter the firmware appends in $UCID.

    The board reports e.g. ``20260602-hm3B`` (the trailing ``B`` is the
    compile-time HM_BOARD_REV), while the manifest lists ``20260602-hm3``. A
    manifest version ends in a digit, so this only ever trims the board side.
    """
    if not version:
        return version
    m = re.match(r"^(.*[0-9a-z])([A-Z])$", version)
    return m.group(1) if m else version


def versions_match(manifest_version: str, board_version: Optional[str]) -> bool:
    """True if *manifest_version* equals the board's $UCID version, tolerating
    the board-rev suffix on the board side (e.g. ``hm3`` matches ``hm3B``)."""
    if not board_version:
        return False
    return clean_version(board_version) == clean_version(manifest_version)


def build_request(job_id: str, version: str, action: str = "flash",
                  eeprom_reset: bool = False,
                  rollback_hex: Optional[str] = None,
                  requested_by: str = "web") -> dict:
    """Build the IPC request dict the daemon writes for the root helper.

    *version* selects which manifest image to flash. *action* is one of
    ``flash``/``rollback``/``backup``. *rollback_hex* is the absolute path of a
    previously-captured backup hex, required only for ``rollback``.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if action == "rollback" and not rollback_hex:
        raise ValueError("rollback requires rollback_hex")
    req = {
        "job_id": job_id,
        "version": version,
        "action": action,
        "eeprom_reset": bool(eeprom_reset),
        "requested_by": requested_by,
    }
    if rollback_hex:
        req["rollback_hex"] = rollback_hex
    return req


def parse_progress_line(line: str) -> Optional[dict]:
    """Parse one JSONL progress line from the helper; None if blank/garbage."""
    line = (line or "").strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def parse_result(d: dict) -> dict:
    """Normalise a helper result dict into a known shape."""
    if not isinstance(d, dict):
        d = {}
    return {
        "job_id": d.get("job_id"),
        "status": d.get("status"),          # "ok" | "error"
        "message": d.get("message", ""),
        "signature": d.get("signature", ""),
        "read_version": d.get("read_version", ""),
    }


def preflight_guard(status: dict, *, tuner_running: bool = False,
                    program_running: bool = False) -> Optional[str]:
    """Return a refusal reason if it is unsafe to flash now, else None.

    Refuses while the cooker is actively driving a fire, so a mid-flash board
    reset cannot leave the blower running unattended: when the PID mode is
    Starting up / Recovering / At temp (fork firmware), when there is any fan or
    output percentage, or when an auto-tune or cook program is running. Allows
    when the cooker is Off or idle with no output.
    """
    if tuner_running:
        return "An auto-tune is running. Stop it before updating firmware."
    if program_running:
        return "A cook program is running. Stop it before updating firmware."
    mode = status.get("pid_mode")
    if mode in (0, 1, 2):
        label = {0: "starting up", 1: "recovering", 2: "at temp"}[mode]
        return (f"The cooker is {label}. Idle it (set to Off) before updating "
                "firmware.")

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    if _num(status.get("output_pct")) > 0 or _num(status.get("fan_pct")) > 0:
        return "The blower is running. Idle the cooker before updating firmware."
    return None
