"""Starting numbers for the drive simulator, per program.

The simulation itself runs in the browser - it is a toy to steer, not a
service - but what a *realistic* drivetrain looks like differs between V5 and
IQ, and that belongs on the server with the rest of the per-program knowledge
rather than being guessed at in JavaScript.

Motor figures are the published free speed and stall torque of the motor at
its nominal battery voltage, which is all the torque-speed model needs: a
motor's torque falls from `stall` at rest to zero at `free`, and everything
else - acceleration, top speed, whether a gear ratio is sensible - follows
from those two numbers plus the robot's weight.

Field sizes are the nominal playing area. VEX foam tiles are two feet square,
so a V5RC field is six by six of them and a VIQRC field three by four.
"""

from calibration import field_layout


def _override():
    """The Override field, as the simulator needs it.

    Read from `calibration.field_layout` rather than copied, because that
    module is already the one place the Goal positions live: the tracker
    attributes a stack to a Goal with these numbers, and a practice field that
    quietly disagreed with them would teach the wrong distances.

    That module imports OpenCV inside the two functions that need it, so it
    costs nothing to import here and works on the hosted copy.
    """
    return {
        "game": "Override",
        "size": field_layout.FIELD_SIZE_IN,
        "goals": [{"kind": goal["kind"], "pair": goal.get("pair"),
                   "x": goal["pos"][0], "y": goal["pos"][1]}
                  for goal in field_layout.GOALS],
        "toggles": [{"name": name, "x": x, "y": y}
                    for name, (x, y) in field_layout.TOGGLES.items()],
        "midfield": [list(point) for point in field_layout.midfield_polygon()],
        # Which pair is red is a property of how the Field was set up at the
        # event, not of the game - see the note in field_layout - so for a
        # practice field it is simply a choice, made here so that the two
        # diagonals read as the two alliances.
        "red_pair": "pair_b",
    }


# Newton-metres and RPM at the nominal battery voltage; amps at stall, which
# is what the battery-sag model needs.
V5_MOTORS = [
    {"key": "v5-100", "label": "V5 11W · 100 rpm (red)", "rpm": 100, "stall": 2.10, "amps": 2.5},
    {"key": "v5-200", "label": "V5 11W · 200 rpm (green)", "rpm": 200, "stall": 1.05, "amps": 2.5},
    {"key": "v5-600", "label": "V5 11W · 600 rpm (blue)", "rpm": 600, "stall": 0.35, "amps": 2.5},
]

IQ_MOTORS = [
    {"key": "iq-120", "label": "IQ smart motor · 120 rpm", "rpm": 120, "stall": 0.414, "amps": 1.2},
]

CONFIG = {
    "v5rc": {
        # 12ft square, 24in tiles.
        "field": {"width": field_layout.FIELD_SIZE_IN, "height": field_layout.FIELD_SIZE_IN,
                  "tile": 24.0},
        "layout": _override(),
        # Where the robot appears. Not the centre: the tall Goal stands there,
        # and a robot that starts inside the furniture is a confusing first
        # thing to see. This is the middle of the near wall, facing up the
        # field, which is roughly where a driver stands.
        "start": {"x": 72.0, "y": 126.0, "heading": -90.0},
        "motors": V5_MOTORS,
        # A 12V battery reads about 12.8V full and sags under load; the
        # resistance is what turns a stalled drivetrain into a voltage drop.
        "nominal": 12.0,
        "battery": {"min": 9.0, "max": 12.8},
        "resistance": 0.05,
        "defaults": {
            "motor": "v5-600", "motorsPerSide": 3, "ratio": 1.67, "wheel": 3.25,
            "mass": 15.0, "length": 17.5, "width": 17.5, "track": 13.0, "wheelbase": 10.0,
            "efficiency": 85, "friction": 1.0, "rolling": 0.08, "scrub": 0.5,
            "battery": 12.4, "control": "tank", "brake": "brake", "curve": 1.0, "ramp": 0.20,
            "goalSize": 9.0, "midfield": "solid",
        },
        # Ratios are motor turns per wheel turn, so a bigger number is geared
        # down. 1.67 is 36:60, 1.33 is 36:48 - both common on a real drive.
        "presets": {
            "Speed build": {"motor": "v5-600", "ratio": 1.33, "wheel": 3.25, "motorsPerSide": 3},
            "Balanced": {"motor": "v5-600", "ratio": 1.67, "wheel": 3.25, "motorsPerSide": 3},
            "Torque build": {"motor": "v5-200", "ratio": 1.0, "wheel": 4.0, "motorsPerSide": 3},
            "Heavy robot": {"mass": 24.0, "motor": "v5-600", "ratio": 1.67},
        },
    },
    "viqrc": {
        # 6ft by 8ft, the same 24in tiles.
        "field": {"width": 96.0, "height": 72.0, "tile": 24.0},
        # No IQ field drawing has been worked out yet, so the field is bare
        # tiles rather than invented furniture. Adding one means writing the
        # IQ equivalent of calibration/field_layout.py and pointing at it here.
        "layout": None,
        "start": {"x": 48.0, "y": 54.0, "heading": -90.0},
        "motors": IQ_MOTORS,
        "nominal": 7.2,
        "battery": {"min": 6.0, "max": 7.6},
        "resistance": 0.15,
        "defaults": {
            "motor": "iq-120", "motorsPerSide": 1, "ratio": 1.0, "wheel": 3.25,
            "mass": 5.0, "length": 11.0, "width": 11.0, "track": 9.0, "wheelbase": 7.0,
            "efficiency": 85, "friction": 1.0, "rolling": 0.08, "scrub": 0.5,
            "battery": 7.2, "control": "tank", "brake": "brake", "curve": 1.0, "ramp": 0.20,
            "goalSize": 9.0, "midfield": "off",
        },
        "presets": {
            "Standard": {"motor": "iq-120", "ratio": 1.0, "wheel": 3.25, "motorsPerSide": 1},
            "Geared up": {"ratio": 0.6, "wheel": 3.25},
            "Geared down": {"ratio": 1.67, "wheel": 2.5},
            "Two motors a side": {"motorsPerSide": 2},
        },
    },
}


def config(code):
    """Everything the browser needs to set up the simulator for one program."""
    data = dict(CONFIG.get(code) or CONFIG["v5rc"])
    data["program"] = code
    return data
