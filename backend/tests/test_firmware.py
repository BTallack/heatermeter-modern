"""Unit tests for the firmware-updater pure logic (no hardware).

Covers manifest parse/validate, sha256 verification against the committed
firmware hexes, the IPC request/progress/result marshalling, version
normalisation, and the pre-flight safety guard. Written dependency-free so the
tiny run_tests.py runner can execute it (also runs under pytest).
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import firmware as fw
from heatermeterd import protocol
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store

REPO_FIRMWARE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "firmware"))


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    raise AssertionError(f"expected {getattr(exc, '__name__', exc)}")


def _good_manifest():
    return {
        "schema": 1,
        "images": [
            {
                "version": "20260602-hm3",
                "file": "heatermeter-20260602-hm3.hex",
                "sha256": "a" * 64,
                "changelog": "scroll + rotation",
                "eeprom_reset": False,
                "board_rev": "B",
            },
            {
                "version": "20260602-hm2",
                "file": "heatermeter-20260602-hm2.hex",
                "sha256": "b" * 64,
                "eeprom_reset": True,
                "board_rev": "B",
            },
        ],
    }


# -- manifest validation ----------------------------------------------------

def test_validate_manifest_ok():
    d = fw.validate_manifest(_good_manifest())
    assert len(d["images"]) == 2


def test_validate_manifest_rejects_bad_schema():
    m = _good_manifest(); m["schema"] = 2
    _raises(fw.ManifestError, fw.validate_manifest, m)


def test_validate_manifest_rejects_empty_images():
    _raises(fw.ManifestError, fw.validate_manifest, {"schema": 1, "images": []})


def test_validate_manifest_rejects_missing_field():
    m = _good_manifest(); del m["images"][0]["sha256"]
    _raises(fw.ManifestError, fw.validate_manifest, m)


def test_validate_manifest_rejects_bad_sha():
    m = _good_manifest(); m["images"][0]["sha256"] = "not-a-sha"
    _raises(fw.ManifestError, fw.validate_manifest, m)


def test_validate_manifest_rejects_duplicate_version():
    m = _good_manifest(); m["images"][1]["version"] = m["images"][0]["version"]
    _raises(fw.ManifestError, fw.validate_manifest, m)


def test_load_manifest_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "manifest.json")
        with open(p, "w") as fh:
            json.dump(_good_manifest(), fh)
        m = fw.load_manifest(p)
        img = fw.find_image(m, "20260602-hm2")
        assert img and img["eeprom_reset"] is True
        assert fw.find_image(m, "nope") is None


def test_load_manifest_missing():
    with tempfile.TemporaryDirectory() as d:
        _raises(fw.ManifestError, fw.load_manifest, os.path.join(d, "nope.json"))


def test_load_manifest_bad_json():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "manifest.json")
        with open(p, "w") as fh:
            fh.write("{ not json")
        _raises(fw.ManifestError, fw.load_manifest, p)


# -- sha256 against the real committed hexes --------------------------------

def test_sha256_matches_committed_sidecars():
    """sha256_file must reproduce the committed *.hex.sha256 sidecar values."""
    checked = 0
    for hexname in ("heatermeter-20210202.hex", "heatermeter-20260601-hm1.hex"):
        hexpath = os.path.join(REPO_FIRMWARE, hexname)
        sidecar = hexpath + ".sha256"
        if not (os.path.exists(hexpath) and os.path.exists(sidecar)):
            continue
        want = open(sidecar).read().split()[0].strip().lower()
        assert fw.sha256_file(hexpath) == want
        checked += 1
    if checked == 0:
        print("    (skipped: no firmware hex/sidecar present)")


def test_sha256_detects_tamper():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "f.bin")
        with open(p, "wb") as fh:
            fh.write(b"hello")
        a = fw.sha256_file(p)
        with open(p, "wb") as fh:
            fh.write(b"hellp")
        assert fw.sha256_file(p) != a


# -- version normalisation --------------------------------------------------

def test_clean_version_strips_board_rev():
    assert fw.clean_version("20260602-hm3B") == "20260602-hm3"
    assert fw.clean_version("20210202B") == "20210202"


def test_clean_version_leaves_manifest_version():
    assert fw.clean_version("20260602-hm3") == "20260602-hm3"
    assert fw.clean_version(None) is None


def test_versions_match_tolerates_board_rev():
    assert fw.versions_match("20260602-hm3", "20260602-hm3B")
    assert fw.versions_match("20210202", "20210202B")
    assert not fw.versions_match("20260602-hm3", "20260602-hm2B")
    assert not fw.versions_match("20260602-hm3", None)


# -- IPC marshalling --------------------------------------------------------

def test_build_request_flash():
    r = fw.build_request("J1", "20260602-hm3", eeprom_reset=False)
    assert r["job_id"] == "J1"
    assert r["action"] == "flash"
    assert r["eeprom_reset"] is False
    assert "rollback_hex" not in r


def test_build_request_rollback_requires_hex():
    _raises(ValueError, fw.build_request, "J1", "20260602-hm3", action="rollback")
    r = fw.build_request("J1", "20260602-hm3", action="rollback",
                         rollback_hex="/spool/J0-backup-flash.hex")
    assert r["rollback_hex"].endswith("backup-flash.hex")


def test_build_request_rejects_bad_action():
    _raises(ValueError, fw.build_request, "J1", "v", action="explode")


def test_build_request_roundtrips_through_json():
    r = fw.build_request("J2", "20260602-hm2", eeprom_reset=True)
    assert json.loads(json.dumps(r)) == r


def test_parse_progress_line():
    assert fw.parse_progress_line('{"step":"spi_on","msg":"ok"}')["step"] == "spi_on"
    assert fw.parse_progress_line("") is None
    assert fw.parse_progress_line("   ") is None
    assert fw.parse_progress_line("not json") is None
    assert fw.parse_progress_line("[1,2,3]") is None


def test_parse_result_normalises():
    r = fw.parse_result({"job_id": "J1", "status": "ok",
                         "read_version": "20260602-hm3B"})
    assert r["status"] == "ok"
    assert r["read_version"] == "20260602-hm3B"
    assert r["message"] == ""
    assert fw.parse_result(None)["status"] is None


# -- pre-flight guard -------------------------------------------------------

def test_guard_allows_idle_off():
    assert fw.preflight_guard(
        {"pid_mode": 4, "output_pct": 0, "fan_pct": 0}) is None


def test_guard_allows_stock_idle():
    assert fw.preflight_guard(
        {"pid_mode": None, "output_pct": 0, "fan_pct": 0}) is None


def test_guard_refuses_active_modes():
    for mode in (0, 1, 2):
        reason = fw.preflight_guard(
            {"pid_mode": mode, "output_pct": 0, "fan_pct": 0})
        assert reason and "firmware" in reason


def test_guard_refuses_running_blower():
    assert fw.preflight_guard({"pid_mode": None, "fan_pct": 35}) is not None
    assert fw.preflight_guard({"pid_mode": 4, "output_pct": 20}) is not None


def test_guard_refuses_tuner_and_program():
    assert fw.preflight_guard({"pid_mode": 4}, tuner_running=True) is not None
    assert fw.preflight_guard({"pid_mode": 4}, program_running=True) is not None


# -- daemon orchestration (with a fake link, no hardware) -------------------

class _FakeLink:
    """Records sent lines and pause/resume, so the orchestration can be driven
    deterministically without a real serial port or the root helper."""

    def __init__(self):
        self.sent = []
        self.paused = False
        self.resumed = False
        self.on_line = None
        self.loop = None

    def start(self, on_line, loop):
        self.on_line = on_line
        self.loop = loop

    def send(self, line):
        self.sent.append(line)

    def pause(self):
        self.paused = True

    def resume(self, on_line=None, loop=None):
        self.paused = False
        self.resumed = True

    def close(self):
        pass


def _fast_service(tmp):
    """A service with a fake link plus a temp spool/manifest, firmware timing
    knobs shrunk so the whole flow runs in well under a second."""
    spool = os.path.join(tmp, "firmware", "spool")
    os.makedirs(spool, exist_ok=True)
    manifest_path = os.path.join(tmp, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(_good_manifest(), f)
    link = _FakeLink()
    svc = HeaterMeterService(link, Store(":memory:"))
    svc.firmware_dir = os.path.join(tmp, "firmware")
    svc.firmware_spool = spool
    svc.firmware_manifest_path = manifest_path
    for attr, val in (("_fw_poll_interval", 0.01), ("_fw_resume_delay", 0.01),
                      ("_fw_verify_delay", 0.05), ("_fw_restore_delay", 0.02),
                      ("_fw_idle_delay", 0.02), ("_fw_send_gap", 0.005)):
        setattr(svc, attr, val)
    return svc, link, spool


def _write_helper_result(spool, job_id, *, status="ok",
                         steps=("spi_on", "flashed", "spi_off"),
                         read_version="20260602-hm3B", make_backup=True):
    with open(os.path.join(spool, f"{job_id}.progress.jsonl"), "w") as f:
        for s in steps:
            f.write(json.dumps({"step": s, "msg": s}) + "\n")
    if make_backup:
        open(os.path.join(spool, f"{job_id}-backup-flash.hex"), "w").close()
    res = {"job_id": job_id, "status": status, "message": "",
           "signature": "0x1e950f", "read_version": read_version}
    with open(os.path.join(spool, f"{job_id}.result.json"), "w") as f:
        json.dump(res, f)


def test_flash_orchestration_happy_path():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            svc, link, spool = _fast_service(tmp)
            await svc.start()
            svc._on_line(protocol.frame("HMSU,225,198,,,,0,0,0,0,0,4"))  # idle
            r = svc.start_firmware_flash("20260602-hm3")
            assert r["ok"], r
            job_id = r["job_id"]
            assert link.paused is True
            req = json.load(open(os.path.join(spool, "request.json")))
            assert req["job_id"] == job_id and req["action"] == "flash"
            assert req["eeprom_reset"] is False
            assert svc.firmware_status["state"] == "flashing"

            _write_helper_result(spool, job_id, read_version="20260602-hm3B")
            # Simulate the rebooted board answering the post-flash /ucid.
            svc._on_line(protocol.frame("UCID,HeaterMeter,20260602-hm3B"))
            await asyncio.sleep(0.3)

            assert svc.firmware_job is None
            assert svc.firmware_status["state"] == "success"
            assert link.resumed is True
            steps = [s.get("step") for s in svc.firmware_status.get("steps", [])]
            assert "flashed" in steps
            assert any("sp=O" in s for s in link.sent)  # idled after reset
            assert svc._fw_last_backup_hex and svc._fw_last_backup_hex.endswith(
                f"{job_id}-backup-flash.hex")
            assert svc.firmware_status.get("verified") is True
            await svc.stop()
    asyncio.run(scenario())


def test_flash_orchestration_eeprom_restore():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            svc, link, spool = _fast_service(tmp)
            await svc.start()
            svc._on_line(protocol.frame("HMPC,0,1.0,2.0,3.0,5.0,3"))  # pit type 3
            svc._on_line(protocol.frame("HMPN,Pit,Pork,Beef,Amb"))
            svc._on_line(protocol.frame("HMSU,225,198,,,,0,0,0,0,0,4"))
            r = svc.start_firmware_flash("20260602-hm2")  # eeprom_reset in manifest
            assert r["ok"], r
            job_id = r["job_id"]
            req = json.load(open(os.path.join(spool, "request.json")))
            assert req["eeprom_reset"] is True
            link.sent.clear()
            _write_helper_result(spool, job_id, read_version="20260602-hm2B")
            await asyncio.sleep(0.3)
            assert svc.firmware_status["state"] == "success"
            # pit (probe0) restored WITH type 3, and the cooker idled.
            assert any(s.startswith("/set?pc0=") and s.rstrip().endswith(",3")
                       for s in link.sent)
            assert any("pn0=Pit" in s for s in link.sent)
            assert any("sp=O" in s for s in link.sent)
            await svc.stop()
    asyncio.run(scenario())


def test_build_restore_commands_order_and_pit_type():
    svc = HeaterMeterService(_FakeLink(), Store(":memory:"))
    snap = {
        "probe_coeffs": {
            0: {"a": "7.3e-4", "b": "2.1e-4", "c": "9.5e-8", "r": "5.0", "type": "3"},
            1: {"a": "1", "b": "2", "c": "3", "r": "10000", "type": "1"},
        },
        "probe_names": ["Pit", "Pork", "Beef", "Amb"],
        "probe_offsets": ["", "", "", ""],
        "alarms": ["0", "203L", "", "", "", "", "", ""],
        "pid": {"b": "4", "p": "5", "i": "0.02", "d": "5"},
        "fan": {"low": "0", "high": "100"},
        "display": {"backlight": "80", "home_mode": "255", "leds": ["0", "0", "0", "0"]},
        "lid_detect": {"offset_percent": "6", "duration": "240"},
        "home_rotate": 3,
    }
    cmds = svc._build_restore_commands(snap)
    pc0 = next(c for c in cmds if c.startswith("/set?pc0="))
    assert pc0.rstrip().endswith(",3")
    pn0 = next(c for c in cmds if "pn0=" in c)
    assert cmds.index(pc0) < cmds.index(pn0)          # coeffs before names
    assert any("al=0,203," in c for c in cmds)         # ringing suffix stripped
    assert cmds[-1].rstrip().endswith("sp=O")          # idle last


def test_flash_refused_while_cooking():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            svc, link, spool = _fast_service(tmp)
            await svc.start()
            svc._on_line(protocol.frame("HMSU,225,198,,,,50,50,0,80,0,2"))  # at temp
            r = svc.start_firmware_flash("20260602-hm3")
            assert not r["ok"]
            assert "firmware" in r["error"]
            assert link.paused is False
            await svc.stop()
    asyncio.run(scenario())


def test_poll_ignores_stale_job_result():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            svc, link, spool = _fast_service(tmp)
            await svc.start()
            svc._on_line(protocol.frame("HMSU,225,198,,,,0,0,0,0,0,4"))
            r = svc.start_firmware_flash("20260602-hm3")
            job_id = r["job_id"]
            # A result file for the right path but a DIFFERENT job id must not
            # be accepted (guards against a stale helper run).
            with open(os.path.join(spool, f"{job_id}.result.json"), "w") as f:
                json.dump({"job_id": "WRONG", "status": "ok"}, f)
            svc._fw_poll()
            assert svc.firmware_job is not None
            assert svc.firmware_status["state"] == "flashing"
            await svc.stop()
    asyncio.run(scenario())
