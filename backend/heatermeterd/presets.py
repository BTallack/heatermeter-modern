"""Meat / doneness presets.

A curated lookup table of common BBQ targets so a user can pick "Brisket" or
"Chicken - done" and have the food-probe target temperature + name filled in for
them, instead of memorising numbers. Temperatures are in Fahrenheit (the
firmware default units); the API converts if the board is in Celsius.

Values follow common USDA safe-minimum and pitmaster finish temperatures. These
are guidance, not a food-safety guarantee; the user can always override.
"""

from __future__ import annotations

# Each preset: stable key -> {label, temp_f, category, note}
MEAT_PRESETS = [
    # Poultry
    {"key": "chicken_done", "label": "Chicken (done)", "temp_f": 165,
     "category": "Poultry", "note": "USDA safe minimum"},
    {"key": "chicken_breast", "label": "Chicken Breast", "temp_f": 165,
     "category": "Poultry", "note": "Juicy; pull ~160-165"},
    {"key": "chicken_thigh", "label": "Chicken Thigh (tender)", "temp_f": 180,
     "category": "Poultry", "note": "Renders better at higher temp"},
    {"key": "turkey", "label": "Turkey", "temp_f": 165,
     "category": "Poultry", "note": "USDA safe minimum"},
    {"key": "duck_breast", "label": "Duck Breast", "temp_f": 135,
     "category": "Poultry", "note": "Medium-rare"},
    {"key": "ground_poultry", "label": "Ground Chicken/Turkey", "temp_f": 165,
     "category": "Poultry", "note": "USDA ground poultry"},
    # Pork
    {"key": "pork_chop", "label": "Pork Chop / Loin", "temp_f": 145,
     "category": "Pork", "note": "USDA + 3 min rest"},
    {"key": "pork_ribs", "label": "Pork Ribs", "temp_f": 195,
     "category": "Pork", "note": "Tender / bend test"},
    {"key": "pork_shoulder", "label": "Pork Shoulder (pulled)", "temp_f": 203,
     "category": "Pork", "note": "Probe-tender for pulling"},
    {"key": "pork_belly", "label": "Pork Belly / Burnt Ends", "temp_f": 200,
     "category": "Pork", "note": "Sliceable / burnt ends"},
    {"key": "ham_reheat", "label": "Ham (reheat)", "temp_f": 140,
     "category": "Pork", "note": "Fully-cooked ham, warmed through"},
    {"key": "sausage", "label": "Sausage", "temp_f": 160,
     "category": "Pork", "note": "USDA safe minimum"},
    # Beef
    {"key": "beef_rare", "label": "Beef - Rare", "temp_f": 125,
     "category": "Beef", "note": "Pull temp"},
    {"key": "beef_medium_rare", "label": "Beef - Medium Rare", "temp_f": 135,
     "category": "Beef", "note": "Pull temp"},
    {"key": "beef_medium", "label": "Beef - Medium", "temp_f": 145,
     "category": "Beef", "note": "Pull temp"},
    {"key": "beef_medium_well", "label": "Beef - Medium Well", "temp_f": 150,
     "category": "Beef", "note": "Pull temp"},
    {"key": "beef_well", "label": "Beef - Well Done", "temp_f": 160,
     "category": "Beef", "note": "Pull temp"},
    {"key": "prime_rib", "label": "Prime Rib (med-rare)", "temp_f": 128,
     "category": "Beef", "note": "Pull ~128, rises on rest"},
    {"key": "tri_tip", "label": "Tri-Tip (med-rare)", "temp_f": 130,
     "category": "Beef", "note": "Pull for medium-rare"},
    {"key": "ground_beef", "label": "Ground Beef / Burgers", "temp_f": 160,
     "category": "Beef", "note": "USDA ground beef"},
    {"key": "brisket", "label": "Brisket", "temp_f": 203,
     "category": "Beef", "note": "Probe-tender, point/flat"},
    {"key": "beef_ribs", "label": "Beef Ribs", "temp_f": 203,
     "category": "Beef", "note": "Probe-tender"},
    # Lamb
    {"key": "lamb_med_rare", "label": "Lamb - Medium Rare", "temp_f": 135,
     "category": "Lamb", "note": "Pull temp"},
    {"key": "lamb_done", "label": "Lamb - Done", "temp_f": 145,
     "category": "Lamb", "note": "USDA + 3 min rest"},
    # Seafood
    {"key": "fish", "label": "Fish", "temp_f": 145,
     "category": "Seafood", "note": "USDA safe minimum"},
    {"key": "salmon", "label": "Salmon (medium)", "temp_f": 125,
     "category": "Seafood", "note": "Chef's medium; USDA is 145"},
    {"key": "shrimp", "label": "Shrimp", "temp_f": 120,
     "category": "Seafood", "note": "Opaque and firm"},
]

# Concise probe NAME for each preset. The HeaterMeter firmware stores probe
# names in EEPROM capped at 13 characters, so the descriptive dropdown `label`
# (e.g. "Ground Chicken/Turkey") would be chopped if used verbatim. Derive a
# short name from the label (drop any "(...)" qualifier and anything after "/")
# and curate the handful that are still too long. Capped at 13 to match the board.
_PROBE_NAME_OVERRIDES = {
    "chicken_breast": "Chkn Breast",
    "ground_poultry": "Grnd Poultry",
    "pork_shoulder": "Pulled Pork",
    "beef_medium_rare": "Beef Med-Rare",
    "beef_medium_well": "Beef Med-Well",
    "beef_well": "Beef Well",
    "lamb_med_rare": "Lamb Med-Rare",
}


def _short_probe_name(preset: dict) -> str:
    n = _PROBE_NAME_OVERRIDES.get(preset["key"])
    if not n:
        n = preset["label"].split("(")[0].split("/")[0].strip()
    return n[:13]


for _p in MEAT_PRESETS:
    _p.setdefault("name", _short_probe_name(_p))

# Common pit/smoker target temperatures, for the setpoint quick-picks.
PIT_PRESETS = [
    {"key": "low_slow", "label": "Low & Slow", "temp_f": 225},
    {"key": "smoke_250", "label": "Smoke", "temp_f": 250},
    {"key": "roast_325", "label": "Roast", "temp_f": 325},
    {"key": "hot_fast", "label": "Hot & Fast", "temp_f": 350},
    {"key": "sear", "label": "Sear", "temp_f": 450},
]

# PID tuning presets. These are STARTING POINTS, not final tunings - the right
# constants are cooker-specific, which is what the auto-tune feature is for.
# Kb (bias) is the feed-forward term; Kp/Ki/Kd are the standard PID gains.
# The firmware default (DEFAULT_CONFIG in hmcore.cpp) is b=0, p=4, i=0.02, d=5.
PID_PRESETS = [
    {"key": "hm_default", "label": "HeaterMeter Default",
     "b": 0.0, "p": 4.0, "i": 0.02, "d": 5.0,
     "note": "Firmware default - good first cook on most pits"},
    {"key": "kamado", "label": "Kamado / Ceramic",
     "b": 0.0, "p": 3.0, "i": 0.01, "d": 5.0,
     "note": "Well-insulated, needs less airflow - gentler gains"},
    {"key": "kettle", "label": "Kettle / Thin Metal",
     "b": 0.0, "p": 6.0, "i": 0.02, "d": 3.0,
     "note": "Leaky/responsive cooker - more proportional, less derivative"},
    {"key": "offset", "label": "Large / Offset Smoker",
     "b": 0.0, "p": 5.0, "i": 0.005, "d": 5.0,
     "note": "Big thermal mass - slow integral to avoid overshoot"},
]

# Blower/fan presets. Only the safe, percentage-based fields are bundled here
# (min/max/startup/floor). Servo travel (servo_min/max in 10us units) is a
# per-build hardware calibration and is deliberately left out of presets.
BLOWER_PRESETS = [
    {"key": "standard", "label": "Standard (blower only)",
     "fan_low": 0, "fan_high": 100, "max_startup": 100, "fan_active_floor": 0,
     "note": "Full range, full-speed startup"},
    {"key": "quiet", "label": "Quiet / Low airflow",
     "fan_low": 0, "fan_high": 50, "max_startup": 50, "fan_active_floor": 0,
     "note": "Caps the blower at 50% - quieter, for small/sealed cookers"},
    {"key": "gentle_start", "label": "Gentle Startup",
     "fan_low": 0, "fan_high": 100, "max_startup": 40, "fan_active_floor": 0,
     "note": "Limits startup surge to avoid overshooting on light"},
    {"key": "high_output", "label": "High Output",
     "fan_low": 10, "fan_high": 100, "max_startup": 100, "fan_active_floor": 0,
     "note": "Min floor 10% to keep a strong fire on big cooks"},
]

# Multi-stage cook program presets. Each one is a ready-to-run template the
# user can load into the program builder and tweak. Stage shape matches the
# cookprogram API: {name, setpoint (F number, or "off" to shut the fan), advance
# {type: probe|time|manual, channel+temp | seconds}}. "food1" is the primary
# food probe. A trailing low-setpoint "Hold" stage is the keep-warm pattern; an
# "off" stage shuts the cooker down. Temps follow common pitmaster finish
# points (see MEAT_PRESETS) - guidance, not a food-safety guarantee.
def _probe(channel, temp):
    return {"type": "probe", "channel": channel, "temp": temp}


def _time(minutes):
    return {"type": "time", "seconds": minutes * 60}


_MANUAL = {"type": "manual"}

PROGRAM_PRESETS = [
    # -- Beef ----------------------------------------------------------------
    {"key": "brisket_low_slow", "label": "Brisket (Low & Slow)", "category": "Beef",
     "note": "Smoke to the stall, wrap, finish probe-tender, then hold.",
     "stages": [
        {"name": "Smoke", "setpoint": 225, "advance": _probe("food1", 165)},
        {"name": "Wrap & cook", "setpoint": 250, "advance": _probe("food1", 203)},
        {"name": "Rest / hold", "setpoint": 150, "advance": _MANUAL},
     ]},
    {"key": "brisket_hot_fast", "label": "Brisket (Hot & Fast)", "category": "Beef",
     "note": "Higher pit for a shorter cook; wrap through the stall.",
     "stages": [
        {"name": "Cook", "setpoint": 300, "advance": _probe("food1", 170)},
        {"name": "Wrap", "setpoint": 300, "advance": _probe("food1", 204)},
        {"name": "Rest / hold", "setpoint": 150, "advance": _MANUAL},
     ]},
    {"key": "beef_short_ribs", "label": "Beef Short Ribs", "category": "Beef",
     "note": "Big beefy ribs, cooked to jiggly-tender.",
     "stages": [
        {"name": "Smoke", "setpoint": 250, "advance": _probe("food1", 165)},
        {"name": "Cook", "setpoint": 275, "advance": _probe("food1", 203)},
        {"name": "Hold", "setpoint": 150, "advance": _MANUAL},
     ]},
    {"key": "prime_rib", "label": "Prime Rib (reverse sear)", "category": "Beef",
     "note": "Slow roast to medium-rare pull temp, then sear hot off-program.",
     "stages": [
        {"name": "Slow roast", "setpoint": 225, "advance": _probe("food1", 120)},
        {"name": "Pull to sear", "setpoint": "off", "advance": _MANUAL},
     ]},
    {"key": "tri_tip", "label": "Tri-Tip (reverse sear)", "category": "Beef",
     "note": "Smoke low, pull at medium-rare, sear over direct heat.",
     "stages": [
        {"name": "Smoke", "setpoint": 225, "advance": _probe("food1", 120)},
        {"name": "Pull to sear", "setpoint": "off", "advance": _MANUAL},
     ]},
    {"key": "reverse_sear_steak", "label": "Reverse Sear Steak", "category": "Beef",
     "note": "Gentle cook to just under target, then a hot sear finishes it.",
     "stages": [
        {"name": "Slow cook", "setpoint": 225, "advance": _probe("food1", 115)},
        {"name": "Pull to sear", "setpoint": "off", "advance": _MANUAL},
     ]},
    # -- Pork ----------------------------------------------------------------
    {"key": "pulled_pork", "label": "Pulled Pork (Pork Butt)", "category": "Pork",
     "note": "Smoke through the stall, wrap, finish at pulling tenderness.",
     "stages": [
        {"name": "Smoke", "setpoint": 225, "advance": _probe("food1", 165)},
        {"name": "Wrap & cook", "setpoint": 250, "advance": _probe("food1", 203)},
        {"name": "Rest / hold", "setpoint": 150, "advance": _MANUAL},
     ]},
    {"key": "ribs_321", "label": "Pork Ribs (3-2-1)", "category": "Pork",
     "note": "Spare ribs: 3h smoke, 2h wrapped, 1h sauced. Time-based.",
     "stages": [
        {"name": "Smoke", "setpoint": 225, "advance": _time(180)},
        {"name": "Wrapped", "setpoint": 225, "advance": _time(120)},
        {"name": "Sauce / firm up", "setpoint": 225, "advance": _time(60)},
        {"name": "Hold", "setpoint": 150, "advance": _MANUAL},
     ]},
    {"key": "ribs_221", "label": "Baby Back Ribs (2-2-1)", "category": "Pork",
     "note": "Lighter baby backs: 2h smoke, 2h wrapped, 1h firm. Time-based.",
     "stages": [
        {"name": "Smoke", "setpoint": 225, "advance": _time(120)},
        {"name": "Wrapped", "setpoint": 225, "advance": _time(120)},
        {"name": "Firm up", "setpoint": 225, "advance": _time(60)},
        {"name": "Hold", "setpoint": 150, "advance": _MANUAL},
     ]},
    {"key": "pork_belly_ends", "label": "Pork Belly Burnt Ends", "category": "Pork",
     "note": "Cube, smoke to tender, then sauce. Pull when probe-soft.",
     "stages": [
        {"name": "Smoke", "setpoint": 250, "advance": _probe("food1", 195)},
        {"name": "Sauce / set", "setpoint": 250, "advance": _time(45)},
        {"name": "Hold", "setpoint": 150, "advance": _MANUAL},
     ]},
    {"key": "pork_loin", "label": "Pork Loin", "category": "Pork",
     "note": "Roast to a touch under 145; carryover finishes it on the rest.",
     "stages": [
        {"name": "Roast", "setpoint": 350, "advance": _probe("food1", 142)},
        {"name": "Rest", "setpoint": "off", "advance": _MANUAL},
     ]},
    # -- Poultry -------------------------------------------------------------
    {"key": "whole_chicken", "label": "Whole Chicken", "category": "Poultry",
     "note": "Roast hot for crisp skin; pull ~157, carryover to 165.",
     "stages": [
        {"name": "Roast", "setpoint": 325, "advance": _probe("food1", 157)},
        {"name": "Rest", "setpoint": 150, "advance": _MANUAL},
     ]},
    {"key": "chicken_thighs", "label": "Chicken Thighs / Wings", "category": "Poultry",
     "note": "Hotter pit; thighs are best rendered at ~180.",
     "stages": [
        {"name": "Cook", "setpoint": 375, "advance": _probe("food1", 180)},
        {"name": "Hold", "setpoint": 150, "advance": _MANUAL},
     ]},
    {"key": "turkey", "label": "Turkey", "category": "Poultry",
     "note": "Roast to 160 in the breast; carryover takes it to 165.",
     "stages": [
        {"name": "Roast", "setpoint": 325, "advance": _probe("food1", 160)},
        {"name": "Rest", "setpoint": 150, "advance": _MANUAL},
     ]},
    # -- Seafood & utility ---------------------------------------------------
    {"key": "smoked_salmon", "label": "Smoked Salmon", "category": "Seafood",
     "note": "Low and slow cold-ish smoke; pull at 135 for a moist fillet.",
     "stages": [
        {"name": "Low smoke", "setpoint": 180, "advance": _probe("food1", 135)},
        {"name": "Done", "setpoint": "off", "advance": _MANUAL},
     ]},
    {"key": "keep_warm", "label": "Keep Warm", "category": "Utility",
     "note": "Single low-setpoint holding stage. Holds until you stop it.",
     "stages": [
        {"name": "Hold", "setpoint": 150, "advance": _MANUAL},
     ]},
]

_BY_KEY = {p["key"]: p for p in MEAT_PRESETS}
_PROGRAM_BY_KEY = {p["key"]: p for p in PROGRAM_PRESETS}


def get_meat_preset(key: str):
    return _BY_KEY.get(key)


def get_program_preset(key: str):
    return _PROGRAM_BY_KEY.get(key)


def all_presets() -> dict:
    """Everything the UI needs to render the preset pickers."""
    return {"meat": MEAT_PRESETS, "pit": PIT_PRESETS,
            "pid": PID_PRESETS, "blower": BLOWER_PRESETS,
            "program": PROGRAM_PRESETS}
