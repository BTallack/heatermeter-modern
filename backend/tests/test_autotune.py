"""Tests for the PID auto-tune math and peak detection."""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import autotune


def test_compute_gains_basic():
    # Relay d=50%, oscillation a=10 deg, period Tu=600s.
    r = autotune.compute_gains(50, 10, 600, rule="ziegler_nichols")
    # Ku = 4*50/(pi*10) = 200/31.4 ~= 6.366
    assert abs(r.ku - (200 / math.pi / 10)) < 1e-6
    assert r.kp == 0.6 * r.ku
    # Ki = Kp / (0.5 * Tu)
    assert abs(r.ki - (r.kp / (0.5 * 600))) < 1e-6
    # Kd = Kp * 0.125 * Tu
    assert abs(r.kd - (r.kp * 0.125 * 600)) < 1e-6
    assert r.rule == "ziegler_nichols"


def test_compute_gains_rules_differ():
    zn = autotune.compute_gains(50, 10, 600, rule="ziegler_nichols")
    tl = autotune.compute_gains(50, 10, 600, rule="tyreus_luyben")
    # Tyreus-Luyben is gentler -> smaller Kp.
    assert tl.kp < zn.kp


def test_compute_gains_rejects_bad_input():
    for bad in [(50, 0, 600), (50, 10, 0)]:
        try:
            autotune.compute_gains(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad}")


def test_peak_detector_sine():
    # Feed a clean sine wave; detector should find its amplitude and period.
    pd = autotune.PeakDetector(hysteresis=0.2)
    period = 100.0
    amp = 10.0
    for i in range(600):          # 6 periods at 1s steps
        t = float(i)
        v = 225 + amp * math.sin(2 * math.pi * t / period)
        pd.add(t, v)
    assert pd.completed_cycles() >= 3
    # Peak-to-trough amplitude ~= 2*amp.
    a = pd.amplitude()
    assert a is not None
    assert abs(a - 2 * amp) < 2.0
    # Period within 5%.
    p = pd.period()
    assert p is not None
    assert abs(p - period) < period * 0.05


def test_peak_detector_flat_no_peaks():
    pd = autotune.PeakDetector()
    for i in range(100):
        pd.add(float(i), 225.0)
    assert pd.completed_cycles() == 0
    assert pd.amplitude() is None
    assert pd.period() is None
