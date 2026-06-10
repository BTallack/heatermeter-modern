"""Pure logic for the in-software host-app updater.

The host updater pulls a new build of this Python+Svelte application from a
configurable release channel (a manifest URL the operator sets), verifies it by
SHA-256, and applies it by swapping the daemon code and the pre-built frontend,
then restarting the service. It deliberately mirrors the AVR firmware updater
(firmware.py): a single root helper (deploy/hm-update) does the privileged swap
and restart, while the unprivileged daemon only downloads, verifies, and hands
off a request via a spool file.

Everything here is pure and unit-testable: manifest parse/validate, version
comparison, artifact hashing/verification, the extracted-tree sanity check, the
IPC request marshalling, progress/result parsing, and the pre-flight guard. No
network, no filesystem mutation beyond reading the file passed to sha256_file,
and no shelling out. The download itself lives in service.py (urllib, run in an
executor); the swap + restart lives in the helper.

Host self-update is host-AGNOSTIC: this module never assumes GitHub, R2, or any
particular host. The operator configures a manifest URL; whatever serves that
JSON + the artifact it points at is the release channel.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Optional, Tuple

# Manifest schema version this code understands.
MANIFEST_SCHEMA = 1

# Required keys on a host-update manifest.
_REQUIRED = ("schema", "version", "url", "sha256")

# A sha256 hex digest is 64 lowercase hex chars.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Only these URL schemes are accepted for an artifact, so a manifest can never
# point the downloader at a local file path or an exotic scheme.
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Valid IPC actions the root helper understands.
ACTIONS = ("update", "rollback")

# Files an extracted release tree MUST contain, so a truncated or wrong-shaped
# artifact is rejected before anything is swapped into place.
REQUIRED_TREE = (
    "backend/heatermeterd/__init__.py",
    "backend/heatermeterd/service.py",
    "frontend/dist/index.html",
)


class ManifestError(ValueError):
    """Raised when a host-update manifest is missing or malformed."""


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


def verify_artifact(path: str, expected_sha: str) -> bool:
    """True if the file at *path* hashes to *expected_sha* (timing-safe)."""
    want = str(expected_sha or "").lower().strip()
    if not _SHA256_RE.match(want):
        return False
    try:
        got = sha256_file(path)
    except OSError:
        return False
    return hmac.compare_digest(got, want)


def validate_manifest(d: dict) -> dict:
    """Validate a parsed host-update manifest; return it unchanged or raise.

    A manifest describes a single available build for one channel (the channel
    is whichever URL the operator configured, e.g. update.json vs
    update-beta.json). Required: schema, version, url (http/https), sha256.
    Optional: changelog, min_python, size, published.
    """
    if not isinstance(d, dict):
        raise ManifestError("manifest is not an object")
    for k in _REQUIRED:
        if d.get(k) in (None, ""):
            raise ManifestError(f"manifest missing required field {k!r}")
    if d.get("schema") != MANIFEST_SCHEMA:
        raise ManifestError(f"unsupported manifest schema {d.get('schema')!r}")
    if not isinstance(d.get("version"), str):
        raise ManifestError("manifest version must be a string")
    if not _URL_RE.match(str(d.get("url"))):
        raise ManifestError("manifest url must be http(s)")
    if not _SHA256_RE.match(str(d.get("sha256")).lower()):
        raise ManifestError("manifest has a malformed sha256")
    return d


def parse_manifest(text: str) -> dict:
    """Parse manifest JSON text (as fetched from the URL) and validate it."""
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise ManifestError(f"manifest is not valid JSON: {e}") from e
    return validate_manifest(d)


def parse_version(v: Optional[str]) -> Optional[Tuple[int, int, int, float]]:
    """Parse ``MAJOR.MINOR.PATCH`` with an optional ``bN`` beta suffix.

    Returns a comparable tuple ``(major, minor, patch, beta)`` where a final
    release uses ``inf`` for the beta slot, so ``0.4.0`` sorts AFTER ``0.4.0b9``.
    Returns None when the string is not a recognisable version.
    """
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:b(\d+))?", str(v or "").strip())
    if not m:
        return None
    beta = int(m.group(4)) if m.group(4) is not None else float("inf")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), beta)


def is_newer(current: Optional[str], available: Optional[str]) -> bool:
    """True if *available* is a strictly newer version than *current*."""
    pa = parse_version(available)
    if pa is None:
        return False
    pc = parse_version(current)
    if pc is None:
        return True
    return pa > pc


def build_request(job_id: str, version: str, *, action: str = "update",
                  tarball: Optional[str] = None, sha256: Optional[str] = None,
                  install_root: Optional[str] = None,
                  requested_by: str = "web") -> dict:
    """Build the IPC request dict the daemon writes for the root helper.

    For ``update`` the helper re-verifies *tarball* against *sha256*, extracts
    it, sanity-checks the tree, backs up the live install, swaps the new code +
    dist into *install_root*, and restarts the service. For ``rollback`` the
    helper restores the previous backup; *tarball*/*sha256* are not required.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if action == "update" and not (tarball and sha256):
        raise ValueError("update requires tarball and sha256")
    req = {
        "job_id": job_id,
        "version": version,
        "action": action,
        "requested_by": requested_by,
    }
    if tarball:
        req["tarball"] = tarball
    if sha256:
        req["sha256"] = str(sha256).lower()
    if install_root:
        req["install_root"] = install_root
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
        "version": d.get("version", ""),
        "action": d.get("action", ""),
    }


def preflight_guard(status: dict, *, tuner_running: bool = False,
                    program_running: bool = False) -> Optional[str]:
    """Return a refusal reason if it is unsafe to update now, else None.

    A host update restarts the daemon, which briefly drops monitoring, logging,
    alerts, and fan/keep-warm automation while the board keeps running its own
    PID. That gap is unacceptable mid-cook, so refuse while the cooker is
    actively driving a fire (PID Starting up / Recovering / At temp, any fan or
    output percentage, or an auto-tune / cook program running) and allow when
    the cooker is Off or idle. The board itself is never touched.
    """
    if tuner_running:
        return "An auto-tune is running. Stop it before updating the software."
    if program_running:
        return "A cook program is running. Stop it before updating the software."
    mode = status.get("pid_mode")
    if mode in (0, 1, 2):
        label = {0: "starting up", 1: "recovering", 2: "at temp"}[mode]
        return (f"The cooker is {label}. Idle it (set to Off) before updating "
                "the software, so monitoring is not interrupted mid-cook.")

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    if _num(status.get("output_pct")) > 0 or _num(status.get("fan_pct")) > 0:
        return ("The blower is running. Idle the cooker before updating the "
                "software.")
    return None


def validate_tree(root: str) -> Optional[str]:
    """Return a reason if an extracted release tree at *root* is missing any
    required file, else None. Used by the helper before swapping anything in,
    and importable by tests. Kept here (pure, path-only) for reuse."""
    import os
    for rel in REQUIRED_TREE:
        if not os.path.isfile(os.path.join(root, rel)):
            return f"release artifact is missing {rel}"
    return None
