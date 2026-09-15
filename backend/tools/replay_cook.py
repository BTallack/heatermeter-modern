#!/usr/bin/env python3
"""Replay a recorded cook through Pit Guard and the done-time predictor.

Pulls a session's full sample history + events from a running daemon and
re-runs the pure detectors over it, so thresholds and models can be judged
against what really happened instead of waiting for the next cook:

  python3 tools/replay_cook.py http://192.168.3.164:8080 7
  python3 tools/replay_cook.py http://192.168.3.164:8080 7 --band 40 --dwell 600

Prints every Pit Guard event the live config would have raised (with the last
lid event and where the pit went afterwards), and for each targeted food probe
a 30-minute timeline of what the predictor would have said versus when the
probe actually got there. Stdlib only; read-only against the daemon.
"""

import argparse
import bisect
import datetime
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from heatermeterd import pitguard, predict  # noqa: E402


def fetch(base, path):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=120) as r:
        return json.load(r)


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def clock(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def replay_pitguard(h, ev, cfg):
    g = pitguard.PitGuard(cfg)
    t = h["t"]
    fired = []
    for k, ts in enumerate(t):
        out = num(h["output_pct"][k])
        r = g.update(ts, num(h["set_point"][k]), num(h["pit"][k]),
                     out if out is not None else num(h["fan_pct"][k]),
                     num(h["lid_countdown"][k]))
        for e in r["events"]:
            fired.append((ts, k, e))
    lids = [e for e in ev if e.get("kind") in ("lid_open", "lid_closed")]
    print(f"Pit Guard ({len([e for e in lids if e['kind'] == 'lid_open'])} lid opens in the log): "
          f"{len(fired)} event(s)")
    for ts, k, e in fired:
        before = [l for l in lids if l["ts"] <= ts]
        lid = (f"{before[-1]['kind']} {round((ts - before[-1]['ts']) / 60)}m earlier"
               if before else "no lid event earlier")
        later = []
        for mins in (10, 20, 30):
            j = bisect.bisect_left(t, ts + mins * 60)
            if j < len(t) and t[j] - (ts + mins * 60) < 120:
                later.append(f"+{mins}m pit {round(num(h['pit'][j]) or 0)}° "
                             f"out {round(num(h['output_pct'][j]) or 0)}%")
        print(f"  {clock(ts)}  {e['kind']:10s} {e['label']:36s} {lid}; " + "; ".join(later))


def replay_predictor(h, ev, channel, target):
    t = h["t"]
    present = [(t[k], v) for k, v in enumerate(num(x) for x in h[channel]) if v is not None]
    if len(present) < 100:
        return
    pts = [a for a, _ in present]
    pv = [b for _, b in present]
    done_ts = next((ts for ts, v in present if v >= target), None)
    stall_edges = [(e["ts"], e["kind"] == "stall_start") for e in ev
                   if e.get("channel") == channel and e.get("kind") in ("stall_start", "stall_end")]
    print(f"\n{channel} -> {target:.0f}°: actually reached "
          f"{clock(done_ts) if done_ts else 'never (max %.1f°)' % max(pv)}")
    print("  time         temp  stall  model     conf    eta        band(min)    error")
    n = covered = 0
    now = present[0][0] + 3600
    while now < present[-1][0]:
        lo = bisect.bisect_left(pts, now - 3600)
        hi = bisect.bisect_right(pts, now)
        tss, vs = pts[lo:hi], pv[lo:hi]
        if len(vs) >= 10:
            k = bisect.bisect_right(t, now) - 1
            env = num(h["pit"][k]) or num(h["set_point"][k])
            stalled = False
            for ets, start in stall_edges:
                if ets <= now:
                    stalled = start
            p = predict.predict(tss, vs, target, env_temp=env,
                                window_seconds=900.0, stalled=stalled)
            if p.model == "plateau":
                eta = f"levelling ~{p.plateau_temp:.0f}°"
                band = ""
                err = ""
            elif p.eta_seconds is None:
                eta, band, err = "--", "", ""
            else:
                eta = f"{p.eta_seconds / 60:5.0f}m"
                band = (f"{(p.eta_low or p.eta_seconds) / 60:4.0f}.."
                        f"{(p.eta_high or p.eta_seconds) / 60:5.0f}")
                if done_ts:
                    e = (now + p.eta_seconds - done_ts) / 60
                    lo_ok = now + (p.eta_low or p.eta_seconds) <= done_ts
                    hi_ok = done_ts <= now + (p.eta_high or p.eta_seconds)
                    n += 1
                    covered += lo_ok and hi_ok
                    err = f"{e:+5.0f}m {'in band' if lo_ok and hi_ok else ''}"
                else:
                    err = ""
            print(f"  {clock(now)}  {vs[-1]:5.1f}   {'S' if stalled else ' '}    "
                  f"{p.model:8s}  {p.confidence:6s}  {eta:16s} {band:12s} {err}")
        now += 1800
    if n:
        print(f"  actual done time inside the reported band: {covered}/{n} estimates")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("base", help="daemon URL, e.g. http://192.168.3.164:8080")
    ap.add_argument("session", type=int)
    ap.add_argument("--band", type=float, help="override Pit Guard low_band")
    ap.add_argument("--dwell", type=int, help="override Pit Guard low_dwell_secs")
    ap.add_argument("--target", type=float,
                    help="food target to evaluate (default: from the food_target events)")
    args = ap.parse_args()

    h = fetch(args.base, f"/api/history?session_id={args.session}&limit=200000")
    ev = fetch(args.base, f"/api/events?session_id={args.session}&limit=5000")
    cfg = {k: v for k, v in fetch(args.base, "/api/pit-guard").items() if k != "live"}
    if args.band:
        cfg["low_band"] = args.band
    if args.dwell:
        cfg["low_dwell_secs"] = args.dwell
    if not h["t"]:
        sys.exit("no samples for that session")
    print(f"Session {args.session}: {clock(h['t'][0])} -> {clock(h['t'][-1])}, "
          f"{len(h['t'])} samples")
    replay_pitguard(h, ev, cfg)
    for ch in ("food1", "food2", "ambient"):
        tg = args.target or next((num(e.get("value")) for e in reversed(ev)
                                  if e.get("kind") == "food_target" and e.get("channel") == ch), None)
        if tg:
            replay_predictor(h, ev, ch, tg)


if __name__ == "__main__":
    main()
