"""The V5 parts list behind Tools → Parts.

The list lives here, in code, and the site only ever changes a part's status.
That split is the access model: students can mark a part missing, low or in
stock from the hosted site, but adding, renaming or removing a part is an edit
to this file and goes through a commit like any other change. The standalone
index this came from let any visitor add parts, and put their names into the
page unescaped.

IDs are the same slugs the standalone index used, so its data.json imports
as-is (`python -m tools.import_parts`). Renaming a part changes its ID and
drops its status, so rename with care.
"""
import re

CATALOG = [
    ("Structure & Framing", [
        "C-Channel 1x1x35", "C-Channel 1x2x35", "C-Channel 1x3x35", "C-Channel 2x2x35",
        "Angle 1x1x35", "Angle 1x2x35", "Flat Plate 2x2", "Flat Plate 5x5", "Flat Plate 5x15",
        "L-Bracket", "Gusset Plate — Small", "Gusset Plate — Large", "Standoff Plate",
        "Bearing Flat Plate", "Corner Bracket",
    ]),
    ("Shafts, Bearings & Spacers", [
        'Steel Shaft 1"', 'Steel Shaft 2"', 'Steel Shaft 3"', 'Steel Shaft 4"', 'Steel Shaft 5"',
        "Shaft Collar", "Bearing Flat", "Pillow Block Bearing", "Round Bearing Insert",
        'Spacer 0.05"', 'Spacer 0.1"', 'Spacer 0.25"', "Rubber Shaft Insert", "Shaft Coupler",
    ]),
    ("Gears, Sprockets & Belts", [
        "Gear — 12t", "Gear — 24t", "Gear — 36t", "Gear — 60t", "Gear — 84t",
        "High Strength Gear Set", "Sprocket — 12t", "Sprocket — 30t", "High Strength Chain",
        "Pulley — Small", "Pulley — Large", "Timing Belt",
    ]),
    ("Wheels", [
        'Omni Wheel 2.75"', 'Omni Wheel 3.25"', 'Omni Wheel 4"',
        'Traction Wheel 2.75"', 'Traction Wheel 3.25"', 'Traction Wheel 4"',
        "High Traction Wheel", "Mecanum Wheel Set", "Wheel Hub / Insert", "Wheel Spacer",
    ]),
    ("Motors & Actuators", [
        "V5 Smart Motor (11W)", "V5 Smart Motor (5.5W)", "V5 Servo", "Motor Sprocket Adapter",
    ]),
    ("Electronics & Control", [
        "V5 Robot Brain", "V5 Controller", "V5 Robot Radio", "V5 Battery", "Battery Charger",
        "Smart Cable — Short", "Smart Cable — Long", "Motor Cable", "3-Wire Extension Cable",
        "USB Cable (Micro-B)",
    ]),
    ("Sensors", [
        "Inertial Sensor (IMU)", "Distance Sensor", "Optical Sensor", "Rotation Sensor",
        "Vision Sensor", "GPS Sensor", "Limit Switch", "Bumper Switch", "Potentiometer",
        "Line Tracker Sensor", "Electromagnet",
    ]),
    ("Pneumatics", [
        "Single-Acting Cylinder", "Dual-Acting Cylinder", "Solenoid Valve", "Air Tank (Reservoir)",
        "Pneumatic Tubing", "Push-to-Connect Fitting", "Y-Fitting", "Hand Pump", "Pressure Gauge",
    ]),
    ("Fasteners & Hardware", [
        'Screw 6-32 x 0.375"', 'Screw 6-32 x 0.5"', 'Screw 6-32 x 0.75"', 'Screw 6-32 x 1"',
        'Screw 6-32 x 1.5"', 'Standoff 1"', 'Standoff 2"', 'Standoff 3"', "Nylock Nut",
        "Keps Nut", "Zip Ties", "Rivets",
    ]),
]

# Cycle order on the page. An unrecorded part counts as missing, as it did in
# the standalone index: a fresh list is a shopping list until someone audits.
STATES = ("missing", "low", "have")
LABELS = {"missing": "Need it", "low": "Low stock", "have": "In stock"}
DEFAULT = "missing"


def slug(category, name):
    """The standalone index's ID: `(cat + "__" + name)`, lowercased, runs of non-alphanumerics to `-`."""
    return re.sub(r"[^a-z0-9]+", "-", f"{category}__{name}".lower())


GROUPS = [{"category": category,
           "parts": [{"id": slug(category, name), "name": name} for name in names]}
          for category, names in CATALOG]
IDS = frozenset(part["id"] for group in GROUPS for part in group["parts"])
NAMES = {part["id"]: part["name"] for group in GROUPS for part in group["parts"]}

assert len(IDS) == sum(len(names) for _, names in CATALOG), "two parts share an ID"
