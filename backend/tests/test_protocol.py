"""Tests for the pure protocol module. Run with pytest, or via run_tests.py."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import protocol


# -- checksums ------------------------------------------------------------

def test_checksum_known_values():
    # Externally verifiable XORs anchor the algorithm to the firmware's spec.
    assert protocol.checksum_hex("A") == "41"          # 0x41
    assert protocol.checksum_hex("AB") == "03"         # 0x41 ^ 0x42
    assert protocol.checksum_hex("") == "00"


def test_frame_roundtrip():
    line = protocol.frame("HMSU,225,198.5,,,72,30,28,0,30,50")
    assert line.startswith("$HMSU,")
    assert line.endswith("\n")
    s = protocol.parse(line)
    assert s is not None
    assert s.type == "HMSU"
    assert s.checksum_ok is True


def test_bad_checksum_detected():
    line = protocol.frame("HMSU,225,198.5,,,72,30,28,0,30,50").rstrip("\n")
    # Corrupt the two checksum chars.
    corrupted = line[:-2] + ("00" if line[-2:] != "00" else "FF") + "\n"
    s = protocol.parse(corrupted)
    assert s is not None
    assert s.checksum_ok is False


def test_missing_checksum_not_ok():
    s = protocol.parse("$HMSU,225,198.5\n")
    assert s is not None
    assert s.checksum_ok is False
    assert s.checksum is None


# -- parsing --------------------------------------------------------------

def test_non_sentence_returns_none():
    assert protocol.parse("") is None
    assert protocol.parse("   ") is None
    assert protocol.parse("random log noise") is None
    assert protocol.parse("/set?sp=225F") is None


def test_parse_status_fields():
    line = protocol.frame("HMSU,225,198.5,145.0,,72.0,35,33,0,35,50")
    s = protocol.parse(line)
    st = protocol.Status.from_sentence(s)
    assert st.set_point == 225.0
    assert st.pit == 198.5
    assert st.food1 == 145.0
    assert st.food2 is None            # blank probe -> None
    assert st.ambient == 72.0
    assert st.output_pct == 35.0
    assert st.output_avg == 33.0
    assert st.lid_countdown == 0
    assert st.fan_pct == 35.0
    assert st.servo_pct == 50.0


def test_status_handles_real_unplugged_fields():
    # Real firmware uses "U" for unplugged/disabled probes (verified on hardware:
    # $HMSU,375,74.5,U,U,U,100,99,0,30,0*4D).
    line = protocol.frame("HMSU,375,74.5,U,U,U,100,99,0,30,0")
    s = protocol.parse(line)
    assert s.checksum_ok is True
    st = protocol.Status.from_sentence(s)
    assert st.set_point == 375.0
    assert st.pit == 74.5
    assert st.food1 is None        # "U" -> None
    assert st.food2 is None
    assert st.ambient is None
    assert st.output_pct == 100.0
    assert st.fan_pct == 30.0


def test_status_handles_short_line():
    s = protocol.parse(protocol.frame("HMSU,225,198.5"))
    st = protocol.Status.from_sentence(s)
    assert st.set_point == 225.0
    assert st.pit == 198.5
    assert st.food1 is None
    assert st.servo_pct is None


def test_status_pid_mode_field_fork_firmware():
    # Fork firmware (20260601-hm1+) appends field 11 = PID mode.
    line = protocol.frame("HMSU,225,198.5,145.0,,72.0,35,33,0,35,50,2")
    st = protocol.Status.from_sentence(protocol.parse(line))
    assert st.servo_pct == 50.0
    assert st.pid_mode == 2
    assert st.to_dict()["pid_mode_label"] == "At temp"


def test_status_pid_mode_absent_on_stock_firmware():
    # Stock firmware sends 10 fields; pid_mode decodes to None (no label).
    line = protocol.frame("HMSU,225,198.5,145.0,,72.0,35,33,0,35,50")
    st = protocol.Status.from_sentence(protocol.parse(line))
    assert st.pid_mode is None
    assert st.to_dict()["pid_mode_label"] is None


def test_ucid_and_other_types_parse():
    s = protocol.parse(protocol.frame("UCID,HeaterMeter,20210202B"))
    assert s.type == "UCID"
    assert s.fields == ["HeaterMeter", "20210202B"]


# -- command builders -----------------------------------------------------

def test_set_setpoint():
    assert protocol.set_setpoint(225) == "/set?sp=225F\n"
    assert protocol.set_setpoint(107, "C") == "/set?sp=107C\n"


def test_manual_output():
    assert protocol.set_manual_output(0) == "/set?sp=-0\n"
    assert protocol.set_manual_output(40) == "/set?sp=-40\n"


def test_set_pid():
    assert protocol.set_pid("p", 4.0) == "/set?pidp=4.0\n"
    assert protocol.set_pid("I", 0.02) == "/set?pidi=0.02\n"
    try:
        protocol.set_pid("x", 1)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for bad PID param")


def test_lcd_and_lid_and_internals():
    assert protocol.set_lcd(backlight=50) == "/set?lb=50,\n"
    assert protocol.set_lcd(home_mode=255) == "/set?lb=,255\n"
    assert protocol.set_lcd(leds=[13, 10, 11, 0]) == "/set?lb=,,13,10,11,0\n"
    assert protocol.lid_open_now() == "/set?ld=,,1\n"
    assert protocol.lid_open_cancel() == "/set?ld=,,0\n"
    assert protocol.set_pid_internals(True) == "/set?tp=1\n"
    assert protocol.set_pid_internals(False) == "/set?tp=0\n"
    assert protocol.set_noise_pin(2) == "/set?tp=,2\n"
    # LED stimulus + home mode tables are populated.
    assert protocol.LED_STIMULI[10] == "Lid Open"
    assert "rotating" in protocol.HOME_MODES[255].lower()
    assert 254 not in protocol.HOME_MODES   # 4-line mode removed (2-line hardware)


def test_probe_helpers():
    assert protocol.set_probe_name(0, "Brisket") == "/set?pn0=Brisket\n"
    assert protocol.set_probe_offsets([None, None, None, -2]) == "/set?po=,,,-2\n"
    assert protocol.set_probe_offsets([1, 2, 3, 4]) == "/set?po=1,2,3,4\n"


def test_config_and_reboot():
    assert protocol.request_config() == "/config\n"
    assert protocol.reboot() == "/reboot\n"


def test_command_normalises():
    assert protocol.command("set?sp=225F") == "/set?sp=225F\n"
    assert protocol.command("/config\n") == "/config\n"


def test_ad8495_preset_is_thermocouple():
    from heatermeterd import protocol
    cmd = protocol.set_probe_preset(0, "ad8495_ktype")
    assert "pc0=" in cmd and cmd.strip().endswith(",3")   # type 3 = TC_ANALOG
    # A thermistor preset still writes type 1.
    assert protocol.set_probe_preset(1, "thermoworks_pro").strip().endswith(",1")


def test_clean_probe_name_guards_command_channel():
    from heatermeterd import protocol
    # Leaked command fragments are rejected (the SET?PO=0.0 bug).
    assert protocol.clean_probe_name("set?po=0.0,0.0,0.0,0.0") == ""
    assert protocol.clean_probe_name("/set?pn3=x") == ""
    # Real names pass; protocol-breaking chars are stripped; length capped.
    assert protocol.clean_probe_name("Beef - Medium") == "Beef - Medium"
    assert protocol.clean_probe_name("Beef, Medium") == "Beef Medium"
    assert protocol.clean_probe_name("x" * 30) == "x" * 22
    # set_probe_name applies it, so the host can never emit a command-like name.
    assert protocol.set_probe_name(3, "set?po=0.0").strip() == "/set?pn3="
    assert protocol.set_probe_name(1, "Brisket").strip() == "/set?pn1=Brisket"


def test_state_sanitizes_corrupt_probe_name():
    from heatermeterd import protocol
    from heatermeterd.state import HeaterMeterState
    st = HeaterMeterState()
    st.ingest_line(protocol.frame("HMPN,Pit Temp,Food 1,Food 2,set?po=0.0"))
    # The leaked-command name self-heals to '' (UI shows the slot default).
    assert st.probe_names == ["Pit Temp", "Food 1", "Food 2", ""]
