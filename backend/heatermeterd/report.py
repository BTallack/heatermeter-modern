"""Per-cook report: a self-contained, printable HTML page (pure, testable).

Builds a single HTML string for one cook session: header, summary stats, an
inline SVG temperature chart (no JS, prints cleanly), the auto timeline events,
and the user's notes. Photos are referenced by URL (served by the same host),
everything else is inline. The API serves it at /api/report/{session_id}, so it
sits behind the optional auth gate like the rest of the data.
"""

from __future__ import annotations

import html
import time
from typing import Optional

# Series drawn on the chart: (column, label, color, dashed)
_SERIES = (
    ("set_point", "Set", "#9aa0a6", True),
    ("pit", "Pit", "#ff5630", False),
    ("food1", "Food 1", "#36b37e", False),
    ("food2", "Food 2", "#00b8d9", False),
    ("ambient", "Ambient", "#a78bfa", False),
)

_EVENT_COLORS = {
    "lid_open": "#ffab00", "lid_closed": "#ffab00",
    "stall_start": "#a78bfa", "stall_end": "#a78bfa",
    "target": "#36b37e", "probe_done": "#36b37e", "cook_complete": "#36b37e",
    "food_target": "#36b37e",
    "setpoint": "#9aa0a6", "stage": "#9aa0a6", "program_done": "#9aa0a6",
    "disconnect": "#ff5630", "fault": "#ff5630", "reconnect": "#6b9080",
    "alarm_low": "#ff8b00", "overtemp": "#ff5630",
}


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _fmt_clock(ts: Optional[float]) -> str:
    if not ts:
        return "--"
    return time.strftime("%I:%M %p", time.localtime(ts)).lstrip("0")


def _fmt_date(ts: Optional[float]) -> str:
    if not ts:
        return "--"
    return time.strftime("%B %d, %Y", time.localtime(ts))


def _fmt_dur(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return "--"
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def compute_stats(columns: dict) -> dict:
    """Summary statistics for the report header table."""
    t = columns.get("t") or []
    out = {"samples": len(t),
           "start_ts": t[0] if t else None, "end_ts": t[-1] if t else None,
           "duration": (t[-1] - t[0]) if len(t) > 1 else None}
    for col in ("pit", "food1", "food2", "ambient", "fan_pct"):
        vals = [f for v in (columns.get(col) or []) if (f := _num(v)) is not None]
        if vals:
            out[col] = {"min": min(vals), "max": max(vals),
                        "avg": sum(vals) / len(vals)}
        else:
            out[col] = None
    return out


def _ticks(lo: float, hi: float, n: int = 5) -> list:
    """A few round tick values spanning [lo, hi]."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(1, n)
    step = max(1, round(raw / 25) * 25) if raw > 12 else max(1, round(raw / 5) * 5)
    first = int(lo // step + 1) * step
    return [v for v in range(first, int(hi) + 1, int(step))]


def build_svg(columns: dict, events: Optional[list] = None, *,
              width: int = 720, height: int = 300, unit: str = "F") -> str:
    """Inline SVG chart of the cook: temp series + event markers. Pure string
    building; returns an empty-data placeholder when there are no samples."""
    t = columns.get("t") or []
    if len(t) < 2:
        return ("<svg xmlns='http://www.w3.org/2000/svg' width='720' height='60'>"
                "<text x='10' y='35' font-size='14' fill='#888'>"
                "No samples recorded for this cook.</text></svg>")
    t0, t1 = t[0], t[-1]
    span = max(1.0, t1 - t0)
    # Temperature extent over all drawn series.
    lo, hi = None, None
    for col, *_ in _SERIES:
        for v in (columns.get(col) or []):
            f = _num(v)
            if f is None:
                continue
            lo = f if lo is None else min(lo, f)
            hi = f if hi is None else max(hi, f)
    if lo is None:
        lo, hi = 0.0, 100.0
    pad = max(5.0, (hi - lo) * 0.08)
    lo, hi = lo - pad, hi + pad
    ml, mr, mt, mb = 16, 44, 10, 26     # margins (temp axis on the right)
    pw, ph = width - ml - mr, height - mt - mb

    def x(ts):
        return ml + (ts - t0) / span * pw

    def y(v):
        return mt + (1 - (v - lo) / (hi - lo)) * ph

    parts = [f"<svg xmlns='http://www.w3.org/2000/svg' "
             f"viewBox='0 0 {width} {height}' "
             f"style='width:100%;max-width:{width}px;height:auto' "
             f"font-family='sans-serif'>"]
    # Grid + temp ticks (right side).
    for tv in _ticks(lo, hi):
        yy = y(tv)
        parts.append(f"<line x1='{ml}' y1='{yy:.1f}' x2='{ml + pw}' y2='{yy:.1f}' "
                     "stroke='#ddd' stroke-width='1'/>")
        parts.append(f"<text x='{ml + pw + 6}' y='{yy + 4:.1f}' font-size='11' "
                     f"fill='#666'>{tv}°</text>")
    # X ticks: 5 evenly spaced clock labels (edge labels anchored inward so
    # they are not clipped at the SVG bounds).
    for i in range(5):
        ts = t0 + span * i / 4
        xx = x(ts)
        anchor = "start" if i == 0 else ("end" if i == 4 else "middle")
        parts.append(f"<text x='{xx:.1f}' y='{height - 8}' font-size='11' "
                     f"fill='#666' text-anchor='{anchor}'>{_fmt_clock(ts)}</text>")
    # Event markers (under the series). Forecast samples are data, not markers.
    for ev in (events or []):
        ts = ev.get("ts")
        if ts is None or ts < t0 or ts > t1 or ev.get("kind") == "prediction":
            continue
        color = _EVENT_COLORS.get(ev.get("kind"), "#9aa0a6")
        xx = x(ts)
        parts.append(f"<line x1='{xx:.1f}' y1='{mt}' x2='{xx:.1f}' "
                     f"y2='{mt + ph}' stroke='{color}' stroke-width='1' "
                     "stroke-dasharray='2,4' opacity='0.6'/>")
        parts.append(f"<circle cx='{xx:.1f}' cy='{mt + ph - 4}' r='3' "
                     f"fill='{color}'/>")
    # Series.
    for col, _label, color, dashed in _SERIES:
        vals = columns.get(col) or []
        pts = []
        for i, ts in enumerate(t):
            f = _num(vals[i]) if i < len(vals) else None
            if f is not None:
                pts.append(f"{x(ts):.1f},{y(f):.1f}")
        if len(pts) < 2:
            continue
        dash = " stroke-dasharray='6,4'" if dashed else ""
        parts.append(f"<polyline points='{' '.join(pts)}' fill='none' "
                     f"stroke='{color}' stroke-width='1.6'{dash}/>")
    parts.append("</svg>")
    return "".join(parts)


def build_report_html(session: dict, columns: dict, events: list, notes: list,
                      *, probe_names: Optional[list] = None,
                      unit: str = "F", insights: Optional[dict] = None) -> str:
    """The full printable report page for one cook session."""
    names = probe_names or ["Pit", "Food 1", "Food 2", "Ambient"]
    e = html.escape
    stats = compute_stats(columns)
    name = e(session.get("name") or f"Cook #{session.get('id', '?')}")
    started = session.get("started_ts") or stats["start_ts"]
    probe_idx = {"pit": 0, "food1": 1, "food2": 2, "ambient": 3}
    legend = " ".join(
        f"<span class='leg'><span class='sw' style='background:{c}'></span>"
        f"{e(names[probe_idx[col]] if col in probe_idx else lbl)}</span>"
        for col, lbl, c, _d in _SERIES
        if (columns.get(col) and any(_num(v) is not None for v in columns[col])))

    def stat_cells(key, label):
        s = stats.get(key)
        if not s:
            return ""
        return (f"<tr><td>{e(label)}</td><td>{s['min']:.0f}°</td>"
                f"<td>{s['avg']:.0f}°</td><td>{s['max']:.0f}°</td></tr>")

    fan = stats.get("fan_pct")
    completed = session.get("completed_ts")
    rows = "".join((
        stat_cells("pit", names[0]),
        stat_cells("food1", names[1]),
        stat_cells("food2", names[2]),
        stat_cells("ambient", names[3]),
    ))

    ev_rows = "".join(
        f"<li><span class='t'>{_fmt_clock(ev.get('ts'))}</span> "
        f"<span class='dot' style='background:"
        f"{_EVENT_COLORS.get(ev.get('kind'), '#9aa0a6')}'></span> "
        f"{e(ev.get('label') or ev.get('kind') or '')}</li>"
        for ev in events if ev.get("kind") != "prediction")

    # Prediction vs actual: for each probe that reached its target, compare the
    # logged forecasts (kind=prediction, value=predicted done epoch) against the
    # actual target time. Shows the earliest forecast and how far off it was.
    accuracy = []
    for ev in events:
        if ev.get("kind") != "target" or not ev.get("channel"):
            continue
        actual = ev["ts"]
        preds = [p for p in events
                 if p.get("kind") == "prediction"
                 and p.get("channel") == ev["channel"]
                 and p.get("value") is not None and p["ts"] < actual]
        if not preds:
            continue
        first = preds[0]
        err_min = (first["value"] - actual) / 60.0
        lead_h = (actual - first["ts"]) / 3600.0
        direction = "late" if err_min > 0 else "early"
        accuracy.append(
            f"<li><span class='t'>{e(ev.get('label') or ev['channel'])}</span> "
            f"first forecast {_fmt_clock(first['value'])} "
            f"(made {lead_h:.1f}h ahead) vs actual {_fmt_clock(actual)} - "
            f"{abs(err_min):.0f} min {direction}</li>")
    acc_rows = "".join(accuracy)
    note_rows = "".join(
        f"<li><span class='t'>{_fmt_clock(n.get('ts'))}</span> "
        f"{e(n.get('text') or '')}"
        + (f"<br><img src='/api/photo/{e(n['photo'])}' alt=''>"
           if n.get("photo") else "")
        + "</li>"
        for n in notes)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{name} - HeaterMeter cook report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, 'Segoe UI', sans-serif; color: #222;
         max-width: 780px; margin: 24px auto; padding: 0 16px; background: #fff; }}
  h1 {{ margin: 0 0 2px; font-size: 26px; }}
  .sub {{ color: #777; margin-bottom: 18px; }}
  .badge {{ display: inline-block; background: #e6f6ec; color: #1c7c3c;
            border-radius: 6px; padding: 2px 8px; font-size: 12px; }}
  table {{ border-collapse: collapse; margin: 12px 0 18px; }}
  td, th {{ padding: 4px 14px 4px 0; text-align: left; font-size: 14px; }}
  th {{ color: #888; font-weight: 600; }}
  .leg {{ margin-right: 14px; font-size: 12px; color: #555; }}
  .sw {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px;
         margin-right: 4px; vertical-align: -1px; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: 3px 0; font-size: 14px; border-bottom: 1px solid #f1f1f1; }}
  .t {{ color: #999; font-variant-numeric: tabular-nums; margin-right: 8px; }}
  .dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%;
          margin-right: 6px; }}
  img {{ max-width: 260px; border-radius: 8px; margin: 6px 0; }}
  h2 {{ font-size: 16px; margin: 22px 0 6px; }}
  .meta {{ color: #555; font-size: 14px; }}
  @media print {{ body {{ margin: 0 auto; }} .noprint {{ display: none; }} }}
</style></head><body>
<h1>{name}</h1>
<div class="sub">{_fmt_date(started)} · {_fmt_clock(started)}
 to {_fmt_clock(stats["end_ts"])} · {_fmt_dur(stats["duration"])}
 {'<span class="badge">Completed</span>' if completed else ''}</div>
<div class="meta">{legend}</div>
{build_svg(columns, events, unit=unit)}
<table>
<tr><th>Probe</th><th>Min</th><th>Avg</th><th>Max</th></tr>
{rows}
{f"<tr><td>Fan</td><td>{fan['min']:.0f}%</td><td>{fan['avg']:.0f}%</td><td>{fan['max']:.0f}%</td></tr>" if fan else ""}
</table>
{f"<h2>Prediction accuracy</h2><ul>{acc_rows}</ul>" if acc_rows else ""}
{f"<h2>Timeline</h2><ul>{ev_rows}</ul>" if ev_rows else ""}
{f"<h2>Notes</h2><ul>{note_rows}</ul>" if note_rows else ""}
<p class="noprint meta">Use your browser's Print to save this as a PDF.</p>
</body></html>"""
