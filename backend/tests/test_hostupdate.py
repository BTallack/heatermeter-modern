"""Unit tests for the host-app updater.

Covers the pure logic (manifest validate, version compare, artifact hashing +
verification, extracted-tree check, IPC marshalling, progress/result parsing,
pre-flight guard) and the daemon orchestration end-to-end against a LOCAL fake
release server (a throwaway http.server serving a manifest + a tarball with a
known sha256). No real network, no GitHub/R2, no hardware. Dependency-free so
the tiny run_tests.py runner can execute it (also runs under pytest).
"""

import asyncio
import functools
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import hostupdate as hu
from heatermeterd import protocol
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    raise AssertionError(f"expected {getattr(exc, '__name__', exc)}")


def _manifest(version="0.4.0", sha="a" * 64,
              url="https://example.test/heatermeter-0.4.0.tar.gz"):
    return {"schema": 1, "version": version, "url": url, "sha256": sha,
            "changelog": "auto timeline + alerts"}


# -- manifest validation ----------------------------------------------------

def test_validate_manifest_ok():
    d = hu.validate_manifest(_manifest())
    assert d["version"] == "0.4.0"


def test_validate_manifest_rejects_bad_schema():
    m = _manifest(); m["schema"] = 2
    _raises(hu.ManifestError, hu.validate_manifest, m)


def test_validate_manifest_rejects_missing_field():
    m = _manifest(); del m["url"]
    _raises(hu.ManifestError, hu.validate_manifest, m)


def test_validate_manifest_rejects_bad_sha():
    m = _manifest(sha="nope")
    _raises(hu.ManifestError, hu.validate_manifest, m)


def test_validate_manifest_rejects_non_http_url():
    m = _manifest(url="file:///etc/passwd")
    _raises(hu.ManifestError, hu.validate_manifest, m)
    m2 = _manifest(url="ftp://example.test/x.tar.gz")
    _raises(hu.ManifestError, hu.validate_manifest, m2)


def test_parse_manifest_bad_json():
    _raises(hu.ManifestError, hu.parse_manifest, "{ not json")


def test_parse_manifest_roundtrip():
    d = hu.parse_manifest(json.dumps(_manifest()))
    assert d["sha256"] == "a" * 64


# -- version comparison -----------------------------------------------------

def test_parse_version():
    assert hu.parse_version("0.4.0")[:3] == (0, 4, 0)
    assert hu.parse_version("v1.2.3")[:3] == (1, 2, 3)
    assert hu.parse_version("0.4.0b2")[3] == 2
    assert hu.parse_version("0.4.0")[3] == float("inf")
    assert hu.parse_version("garbage") is None


def test_is_newer():
    assert hu.is_newer("0.3.0", "0.4.0")
    assert hu.is_newer("0.4.0b1", "0.4.0")        # release beats its beta
    assert hu.is_newer("0.4.0b1", "0.4.0b2")
    assert not hu.is_newer("0.4.0", "0.4.0")
    assert not hu.is_newer("0.4.0", "0.3.9")
    assert not hu.is_newer("0.4.0", "0.4.0b9")    # beta is older than release
    assert not hu.is_newer("0.3.0", "garbage")
    assert hu.is_newer("garbage", "0.1.0")        # unknown current -> offer it


# -- artifact hashing / verification ----------------------------------------

def test_sha256_and_verify_artifact():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "a.bin")
        with open(p, "wb") as f:
            f.write(b"hello world")
        want = hashlib.sha256(b"hello world").hexdigest()
        assert hu.sha256_file(p) == want
        assert hu.verify_artifact(p, want)
        assert hu.verify_artifact(p, want.upper())     # case-insensitive
        assert not hu.verify_artifact(p, "b" * 64)     # wrong
        assert not hu.verify_artifact(p, "short")      # malformed
        assert not hu.verify_artifact(os.path.join(d, "missing"), want)


# -- IPC marshalling --------------------------------------------------------

def test_build_request_update_requires_tarball_sha():
    _raises(ValueError, hu.build_request, "J1", "0.4.0")
    r = hu.build_request("J1", "0.4.0", tarball="/s/J1.tar.gz", sha256="A" * 64,
                         install_root="/opt/hm")
    assert r["action"] == "update"
    assert r["sha256"] == "a" * 64           # lowercased
    assert r["tarball"].endswith(".tar.gz")
    assert r["install_root"] == "/opt/hm"


def test_build_request_rollback_no_artifact():
    r = hu.build_request("J1", "0.3.0", action="rollback")
    assert r["action"] == "rollback"
    assert "tarball" not in r and "sha256" not in r


def test_build_request_rejects_bad_action():
    _raises(ValueError, hu.build_request, "J1", "v", action="explode")


def test_build_request_roundtrips_through_json():
    r = hu.build_request("J2", "0.4.0", tarball="/s/x.tgz", sha256="c" * 64)
    assert json.loads(json.dumps(r)) == r


def test_parse_progress_and_result():
    assert hu.parse_progress_line('{"step":"swap","msg":"ok"}')["step"] == "swap"
    assert hu.parse_progress_line("") is None
    assert hu.parse_progress_line("nope") is None
    r = hu.parse_result({"job_id": "J", "status": "ok", "version": "0.4.0"})
    assert r["status"] == "ok" and r["version"] == "0.4.0"
    assert hu.parse_result(None)["status"] is None


# -- pre-flight guard -------------------------------------------------------

def test_guard_allows_idle():
    assert hu.preflight_guard({"pid_mode": 4, "output_pct": 0, "fan_pct": 0}) is None
    assert hu.preflight_guard({"pid_mode": None, "output_pct": 0}) is None


def test_guard_refuses_cooking():
    for mode in (0, 1, 2):
        assert hu.preflight_guard({"pid_mode": mode}) is not None
    assert hu.preflight_guard({"pid_mode": None, "fan_pct": 30}) is not None
    assert hu.preflight_guard({"pid_mode": 4, "output_pct": 10}) is not None
    assert hu.preflight_guard({"pid_mode": 4}, tuner_running=True) is not None
    assert hu.preflight_guard({"pid_mode": 4}, program_running=True) is not None


# -- extracted-tree sanity check --------------------------------------------

def test_validate_tree():
    with tempfile.TemporaryDirectory() as d:
        # missing everything
        assert hu.validate_tree(d) is not None
        for rel in hu.REQUIRED_TREE:
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
        assert hu.validate_tree(d) is None
        # remove one -> fails again
        os.remove(os.path.join(d, hu.REQUIRED_TREE[-1]))
        assert hu.validate_tree(d) is not None


# -- config -----------------------------------------------------------------

def _svc(tmp):
    svc = HeaterMeterService(_FakeLink(), Store(":memory:"))
    svc.hostupdate_dir = os.path.join(tmp, "hostupdate")
    svc.hostupdate_spool = os.path.join(svc.hostupdate_dir, "spool")
    svc.hostupdate_staging = os.path.join(svc.hostupdate_dir, "staging")
    svc.hostupdate_config_path = os.path.join(tmp, "hostupdate.json")
    svc.install_root = os.path.join(tmp, "install")
    os.makedirs(svc.hostupdate_spool, exist_ok=True)
    os.makedirs(svc.hostupdate_staging, exist_ok=True)
    for a, v in (("_hu_poll_interval", 0.01), ("_hu_timeout", 0.3)):
        setattr(svc, a, v)
    return svc


def test_config_save_get_and_url_validation():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _svc(tmp)
        assert svc.get_host_update_config() == {"manifest_url": "", "auto_check": False}
        r = svc.save_host_update_config(
            {"manifest_url": "https://x.test/u.json", "auto_check": True})
        assert r["ok"] and r["manifest_url"].endswith("u.json")
        assert svc.get_host_update_config()["auto_check"] is True
        bad = svc.save_host_update_config({"manifest_url": "ftp://x/y"})
        assert not bad["ok"]
        # empty url is allowed (disables updating)
        assert svc.save_host_update_config({"manifest_url": ""})["ok"]


# -- daemon orchestration against a LOCAL fake release server ----------------

class _FakeLink:
    def __init__(self):
        self.sent = []
    def start(self, on_line, loop):
        self.on_line = on_line
    def send(self, line):
        self.sent.append(line)
    def pause(self):
        pass
    def resume(self, on_line=None, loop=None):
        pass
    def close(self):
        pass


class _Release:
    """A throwaway HTTP server serving manifest.json + the artifact tarball."""

    def __init__(self, root):
        self.root = root
        handler = functools.partial(_QuietHandler, directory=root)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def _make_artifact(path):
    """Write a minimal but well-shaped release tarball; return its sha256."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in hu.REQUIRED_TREE:
            data = b"# release file\n"
            ti = tarfile.TarInfo(rel)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    raw = buf.getvalue()
    with open(path, "wb") as f:
        f.write(raw)
    return hashlib.sha256(raw).hexdigest()


def _serve_release(root, version="99.9.0", break_sha=False):
    artifact = os.path.join(root, "heatermeter.tar.gz")
    sha = _make_artifact(artifact)
    rel = _Release(root)
    manifest = {"schema": 1, "version": version,
                "url": f"{rel.base}/heatermeter.tar.gz",
                "sha256": ("d" * 64) if break_sha else sha,
                "changelog": "test build"}
    with open(os.path.join(root, "update.json"), "w") as f:
        json.dump(manifest, f)
    return rel, f"{rel.base}/update.json", sha


def test_check_reports_available_update():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            relroot = os.path.join(tmp, "rel"); os.makedirs(relroot)
            rel, manifest_url, _ = _serve_release(relroot, version="99.9.0")
            try:
                svc = _svc(tmp)
                await svc.start()
                svc.save_host_update_config({"manifest_url": manifest_url})
                r = await svc.check_host_update()
                assert r["ok"], r
                assert r["version"] == "99.9.0"
                assert r["update_available"] is True   # current 0.3.0 < 0.4.0
                # cached for the listing
                assert svc.host_update_listing()["available"]["version"] == "99.9.0"
                await svc.stop()
            finally:
                rel.stop()
    asyncio.run(scenario())


def test_apply_downloads_verifies_and_writes_request():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            relroot = os.path.join(tmp, "rel"); os.makedirs(relroot)
            rel, manifest_url, sha = _serve_release(relroot, version="99.9.0")
            try:
                svc = _svc(tmp)
                await svc.start()
                svc._on_line(protocol.frame("HMSU,225,198,,,,0,0,0,0,0,4"))  # idle
                svc.save_host_update_config({"manifest_url": manifest_url})
                r = await svc.start_host_update()
                assert r["ok"], r
                job_id = r["job_id"]
                req = json.load(open(os.path.join(svc.hostupdate_spool, "request.json")))
                assert req["job_id"] == job_id and req["action"] == "update"
                assert req["sha256"] == sha
                assert req["version"] == "99.9.0"
                assert os.path.exists(req["tarball"])
                # downloaded artifact really matches the manifest sha
                assert hu.sha256_file(req["tarball"]) == sha
                assert svc.hostupdate_status["state"] == "applying"

                # Simulate the helper finishing OK (in reality it restarts us).
                with open(os.path.join(svc.hostupdate_spool,
                                       "hostupdate.result.json"), "w") as f:
                    json.dump({"job_id": job_id, "status": "ok",
                               "version": "0.4.0", "action": "update"}, f)
                await asyncio.sleep(0.1)
                assert svc.hostupdate_job is None
                assert svc.hostupdate_status["state"] == "success"
                await svc.stop()
            finally:
                rel.stop()
    asyncio.run(scenario())


def test_apply_rejects_integrity_mismatch():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            relroot = os.path.join(tmp, "rel"); os.makedirs(relroot)
            rel, manifest_url, _ = _serve_release(relroot, version="99.9.0",
                                                  break_sha=True)
            try:
                svc = _svc(tmp)
                await svc.start()
                svc._on_line(protocol.frame("HMSU,225,198,,,,0,0,0,0,0,4"))
                svc.save_host_update_config({"manifest_url": manifest_url})
                r = await svc.start_host_update()
                assert not r["ok"]
                assert "integrity" in r["error"].lower()
                # nothing handed off to the helper
                assert not os.path.exists(
                    os.path.join(svc.hostupdate_spool, "request.json"))
                assert svc.hostupdate_job is None
                await svc.stop()
            finally:
                rel.stop()
    asyncio.run(scenario())


def test_apply_refused_while_cooking():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            relroot = os.path.join(tmp, "rel"); os.makedirs(relroot)
            rel, manifest_url, _ = _serve_release(relroot)
            try:
                svc = _svc(tmp)
                await svc.start()
                svc._on_line(protocol.frame("HMSU,225,198,,,,50,50,0,80,0,2"))  # at temp
                svc.save_host_update_config({"manifest_url": manifest_url})
                r = await svc.start_host_update()
                assert not r["ok"]
                assert "software" in r["error"].lower()
                assert not os.path.exists(
                    os.path.join(svc.hostupdate_spool, "request.json"))
                await svc.stop()
            finally:
                rel.stop()
    asyncio.run(scenario())


def test_apply_unconfigured():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            svc = _svc(tmp)
            await svc.start()
            svc._on_line(protocol.frame("HMSU,225,198,,,,0,0,0,0,0,4"))
            r = await svc.start_host_update()
            assert not r["ok"] and "not configured" in r["error"].lower()
            await svc.stop()
    asyncio.run(scenario())


def test_boot_result_surfaced_and_acked():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            svc = _svc(tmp)
            # Pretend a prior update finished just before this (re)start.
            with open(os.path.join(svc.hostupdate_spool,
                                   "hostupdate.result.json"), "w") as f:
                json.dump({"job_id": "J9", "status": "ok", "version": "0.4.0",
                           "action": "update", "message": "Updated to 0.4.0"}, f)
            await svc.start()
            assert svc.hostupdate_status["state"] == "success"
            assert svc.hostupdate_status["version"] == "0.4.0"
            # ack clears the file and resets to idle
            svc.ack_host_update()
            assert svc.hostupdate_status["state"] == "idle"
            assert not os.path.exists(
                os.path.join(svc.hostupdate_spool, "hostupdate.result.json"))
            await svc.stop()
    asyncio.run(scenario())
