"""Tests for push notifications (ntfy) config + the alarm debounce/repeat logic.

No network: notify.send is not called against a real server, and the alarm
notifier's _push is monkeypatched to record calls.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import notify
from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


def _svc():
    svc = HeaterMeterService(SimLink(), Store(":memory:"))
    svc.notify_config_path = os.path.join(tempfile.mkdtemp(), "notify.json")
    return svc


def test_notify_defaults_and_persistence():
    svc = _svc()
    assert svc.notify_effective_config()["enabled"] is False
    svc.save_notify_file({**notify.default_config(),
                          "enabled": True, "topic": "t", "token": "secret"})
    eff = svc.notify_effective_config()
    assert eff["enabled"] and eff["topic"] == "t"
    assert (os.stat(svc.notify_config_path).st_mode & 0o777) == 0o600  # token lives here


def test_notify_status_masks_token():
    svc = _svc()
    svc.save_notify_file({**notify.default_config(),
                          "enabled": True, "topic": "t", "token": "abc"})
    pub = svc.notify_status_public()
    assert "token" not in pub
    assert pub["has_token"] is True
    assert pub["topic"] == "t"


def test_notify_send_requires_topic():
    assert notify.send({"topic": ""}, "t", "m")["ok"] is False


def test_alarm_debounce_and_repeat():
    svc = _svc()
    svc.save_notify_file({**notify.default_config(), "enabled": True, "topic": "t",
                          "debounce_sec": 10, "repeat_min": 1})
    pushes = []
    svc._push = lambda *a, **k: pushes.append(a)
    names = ["Pit", "Food 1", "Food 2", "Ambient"]
    k = "1:high"
    svc._maybe_notify_alarm(k, True, 100.0, 1, "high", names, names)   # edge: start timer
    assert len(pushes) == 0
    svc._maybe_notify_alarm(k, True, 105.0, 1, "high", names, names)   # 5s < debounce
    assert len(pushes) == 0
    svc._maybe_notify_alarm(k, True, 111.0, 1, "high", names, names)   # 11s >= debounce -> push
    assert len(pushes) == 1
    svc._maybe_notify_alarm(k, True, 150.0, 1, "high", names, names)   # <60s since last -> no
    assert len(pushes) == 1
    svc._maybe_notify_alarm(k, True, 175.0, 1, "high", names, names)   # >=60s -> repeat
    assert len(pushes) == 2
    svc._maybe_notify_alarm(k, False, 180.0, 1, "high", names, names)  # cleared -> reset
    assert k not in svc._alarm_notify


def test_alarm_no_push_when_disabled():
    svc = _svc()  # notify disabled by default
    pushes = []
    svc._push = lambda *a, **k: pushes.append(a)
    names = ["Pit", "Food 1", "Food 2", "Ambient"]
    for t in (100.0, 200.0, 300.0):
        svc._maybe_notify_alarm("0:high", True, t, 0, "high", names, names)
    assert pushes == []


def _rising_food1(svc, target=200.0, last=198.0):
    """Set a food1 target + a steady rising food1 series so the predictor gives a
    short ETA. food1 lives at alarms[3]; its temp at status.food1."""
    svc.save_notify_file({**notify.default_config(), "enabled": True, "topic": "t"})
    svc.state.alarms = ["-1", "-1", "-1", str(target), "-1", "-1", "-1", "-1"]
    svc.state.status.food1 = last
    svc.state.status.pit = 250.0
    base = 1000.0
    n = 25
    ts_list = [base + i * 30 for i in range(n)]          # 30s apart, ~12 min
    vals = [last - (n - 1 - i) * 0.5 for i in range(n)]  # +1 deg/min, ends at `last`
    svc.store.recent_series = lambda col, secs, now: (ts_list, vals) if col == "food1" else ([], [])
    return base + (n - 1) * 30


def test_eta_push_when_almost_done():
    svc = _svc()
    last_ts = _rising_food1(svc, target=200.0, last=198.0)
    pushes = []
    svc._push = lambda *a, **k: pushes.append(a)
    svc._check_eta_push(last_ts + 1)
    assert len(pushes) == 1                         # fired once, almost done
    assert "almost done" in pushes[0][0].lower()
    svc._check_eta_push(last_ts + 5)                # throttled + already notified
    assert len(pushes) == 1


def test_eta_push_skips_without_target():
    svc = _svc()
    svc.save_notify_file({**notify.default_config(), "enabled": True, "topic": "t"})
    svc.state.alarms = ["-1"] * 8                   # no targets set
    svc.store.recent_series = lambda col, secs, now: ([], [])
    pushes = []
    svc._push = lambda *a, **k: pushes.append(a)
    svc._check_eta_push(1000.0)
    assert pushes == []
