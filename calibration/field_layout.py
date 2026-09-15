"""Where everything stands on a V5RC Override field, in field inches.

Taken from the official Field Overview drawing in Appendix A of the Game Manual
(v1.0, 2 July 2026), then snapped to the 24in tile grid the Goals are built on.

Measuring the drawing put every Goal within ~3in of a grid intersection, and the
error was a constant 6% of the distance from the centre - the centre Goal was
right to 0.1in while the outer ones were off by 3in, all pulled inward. That is a
scale error, not scatter: the grey region I measured included the perimeter wall,
so px-per-inch came out ~6% small. The Goals sit on tile corners, so the grid is
the ground truth and the measurement only had to identify which corner.

These positions are worth having because they are fixed. Goals never move, so a
stack can be attributed to the Goal it stands on instead of being inferred purely
from pixels, and a detection nowhere near a Goal is suspect by construction.
"""
FIELD_SIZE_IN = 144.0
CENTRE = (72.0, 72.0)

# Nine Goals: four neutral Short, one neutral Tall at the centre, and two
# Alliance pairs on opposite diagonals.
NEUTRAL_SHORT_GOALS = [(48.0, 24.0), (24.0, 48.0), (120.0, 96.0), (96.0, 120.0)]
TALL_GOAL = (72.0, 72.0)

# Which pair belongs to which Alliance is NOT a property of the game - it depends
# on how the Field was set up at the event and which corner the calibration used
# as origin. Hard-coding it produced exactly one bug: the layout looked right, sat
# on the correct goals, and silently attributed red's goals to blue. So the pairs
# are named by position and the owner is resolved per match from the footage.
ALLIANCE_GOAL_PAIRS = {
    "pair_a": [(24.0, 96.0), (48.0, 120.0)],
    "pair_b": [(96.0, 24.0), (120.0, 48.0)],
}

GOALS = (
    [{"kind": "short", "owner": None, "pos": p} for p in NEUTRAL_SHORT_GOALS]
    + [{"kind": "tall", "owner": None, "pos": TALL_GOAL}]
    + [{"kind": "alliance", "owner": None, "pair": name, "pos": p}
       for name, pts in ALLIANCE_GOAL_PAIRS.items() for p in pts]
)


def resolve_alliance_owners(frame_bgr, homography, sample_radius_px=26):
    """Decide which Alliance pair is red for THIS match, from the frame itself.

    Samples the pixels around each pair's Goals and compares how red versus blue
    they are. This is measured rather than assumed because the answer changes with
    the Field setup and with which corner the calibration called (0, 0).

    Returns {"pair_a": "red"|"blue", "pair_b": ...} or None if neither reads clearly.
    """
    import cv2
    import numpy as np

    inverse = np.linalg.inv(homography)
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    vivid = (sat > 110) & (val > 60)
    is_red = vivid & ((hue <= 8) | (hue >= 172))
    is_blue = vivid & (hue >= 95) & (hue <= 128)

    scores = {}
    for name, points in ALLIANCE_GOAL_PAIRS.items():
        red_total = blue_total = 0
        for x_in, y_in in points:
            p = cv2.perspectiveTransform(np.array([[[x_in, y_in]]], dtype=np.float64), inverse)[0, 0]
            cx, cy = int(round(p[0])), int(round(p[1]))
            y0, y1 = max(cy - sample_radius_px, 0), cy + sample_radius_px
            x0, x1 = max(cx - sample_radius_px, 0), cx + sample_radius_px
            red_total += int(is_red[y0:y1, x0:x1].sum())
            blue_total += int(is_blue[y0:y1, x0:x1].sum())
        scores[name] = (red_total, blue_total)

    a_red, a_blue = scores["pair_a"]
    b_red, b_blue = scores["pair_b"]
    # Compare across pairs rather than within one: red pins litter the whole field,
    # so "more red than blue here" is weak, but "redder than the other pair" is not.
    if a_red + b_blue == 0 and b_red + a_blue == 0:
        return None
    if a_red + b_blue >= b_red + a_blue:
        return {"pair_a": "red", "pair_b": "blue", "evidence": scores}
    return {"pair_a": "blue", "pair_b": "red", "evidence": scores}


def goals_with_owners(owners=None):
    """The nine Goals, with Alliance owners filled in if they have been resolved."""
    resolved = []
    for goal in GOALS:
        entry = dict(goal)
        if owners and entry.get("pair"):
            entry["owner"] = owners.get(entry["pair"])
        resolved.append(entry)
    return resolved

# One Toggle centred on each perimeter wall, about 26in long.
# Centred on each wall; the small inset from 0 and 144 is the wall thickness.
TOGGLES = {
    "north": (72.0, 0.0),
    "west": (0.0, 72.0),
    "east": (144.0, 72.0),
    "south": (72.0, 144.0),
}

# The Midfield is a square rotated 45 degrees about the centre. Measured at 24.6in
# from the white tape, which under the same 6% scale error corrects to 24in - one
# tile, and consistent with its vertices meeting the grid like everything else.
MIDFIELD_VERTEX_IN = 24.0


def midfield_polygon():
    x, y = CENTRE
    r = MIDFIELD_VERTEX_IN
    return [(x, y - r), (x + r, y), (x, y + r), (x - r, y)]


def in_midfield(x_in, y_in):
    """Inside the Midfield diamond - `<SC6>` scores 8 points per Robot here."""
    return abs(x_in - CENTRE[0]) + abs(y_in - CENTRE[1]) <= MIDFIELD_VERTEX_IN


def quadrant_of(x_in, y_in):
    """Which Quadrant a point falls in.

    Quadrants are *triangular*, not the four rectangles a naive split would give:
    the glossary calls them "four designated triangular areas", and the Autonomous
    Line runs diagonally across the Field. So the boundaries are the diagonals, and
    a point belongs to the triangle it is nearest the wall of.

    This matters for scoring: `<SC5>a` ties a yellow half's owner to the Toggle in
    its Quadrant, so getting the Quadrant wrong hands the points to the wrong side.
    """
    dx, dy = x_in - CENTRE[0], y_in - CENTRE[1]
    if abs(dx) >= abs(dy):
        return "east" if dx > 0 else "west"
    return "south" if dy > 0 else "north"


def nearest_goal(x_in, y_in, max_distance_in=14.0):
    """The Goal a stack at this position is standing on, or None.

    A stack must be nested with a Goal to be Placed at all (`<SC2>`), so a detected
    stack with no Goal near it is a false positive rather than something scoring.
    """
    best, best_d = None, None
    for goal in GOALS:
        gx, gy = goal["pos"]
        d = ((x_in - gx) ** 2 + (y_in - gy) ** 2) ** 0.5
        if best_d is None or d < best_d:
            best, best_d = goal, d
    if best_d is not None and best_d <= max_distance_in:
        return dict(best, distance_in=round(best_d, 1))
    return None


def render_map(px_per_in=6, margin=95, show_coordinates=True):
    """A top-down schematic of the field, drawn from the constants above.

    Rendered from the same numbers the pipeline uses, so if a coordinate is wrong
    it is wrong here too - the map is a check on the data, not a separate drawing
    that could quietly disagree with it.
    """
    import cv2
    import numpy as np

    side = int(FIELD_SIZE_IN * px_per_in) + 2 * margin
    canvas = np.full((side, side, 3), 22, np.uint8)

    def to_px(x_in, y_in):
        return int(round(x_in * px_per_in + margin)), int(round(y_in * px_per_in + margin))

    # 24in tile grid
    for i in range(7):
        o = i * 24.0
        cv2.line(canvas, to_px(o, 0), to_px(o, FIELD_SIZE_IN), (52, 52, 52), 1, cv2.LINE_AA)
        cv2.line(canvas, to_px(0, o), to_px(FIELD_SIZE_IN, o), (52, 52, 52), 1, cv2.LINE_AA)

    # quadrant diagonals - the Quadrants are triangles, not rectangles
    cv2.line(canvas, to_px(0, 0), to_px(144, 144), (95, 95, 95), 1, cv2.LINE_AA)
    cv2.line(canvas, to_px(144, 0), to_px(0, 144), (95, 95, 95), 1, cv2.LINE_AA)

    cv2.polylines(canvas, [np.array([to_px(*p) for p in midfield_polygon()], np.int32)],
                  True, (235, 235, 235), 2, cv2.LINE_AA)
    cv2.rectangle(canvas, to_px(0, 0), to_px(144, 144), (215, 215, 215), 2)

    colours = {"red": (60, 60, 255), "blue": (255, 150, 40), None: (70, 210, 245)}
    for goal in GOALS:
        x_in, y_in = goal["pos"]
        point = to_px(x_in, y_in)
        colour = colours[goal["owner"]]
        radius = 17 if goal["kind"] == "tall" else 12
        cv2.circle(canvas, point, radius, colour, 2, cv2.LINE_AA)
        cv2.circle(canvas, point, 3, colour, -1, cv2.LINE_AA)
        if show_coordinates:
            cv2.putText(canvas, f"({x_in:g},{y_in:g})", (point[0] - 34, point[1] - radius - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.38, colour, 1, cv2.LINE_AA)
            cv2.putText(canvas, goal["kind"], (point[0] - 16, point[1] + radius + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.36, colour, 1, cv2.LINE_AA)

    for name, (x_in, y_in) in TOGGLES.items():
        point = to_px(x_in, y_in)
        cv2.drawMarker(canvas, point, (60, 230, 245), cv2.MARKER_SQUARE, 18, 2)
        cv2.putText(canvas, f"{name} toggle ({x_in:g},{y_in:g})", (point[0] - 52, point[1] - 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.36, (60, 230, 245), 1, cv2.LINE_AA)

    for label, (x_in, y_in) in (("(0,0)", (0, 0)), ("(144,0)", (144, 0)),
                                ("(144,144)", (144, 144)), ("(0,144)", (0, 144))):
        point = to_px(x_in, y_in)
        cv2.putText(canvas, label, (point[0] - 26, point[1] - 8), cv2.FONT_HERSHEY_SIMPLEX,
                    0.42, (215, 215, 215), 1, cv2.LINE_AA)

    cv2.putText(canvas, "V5RC Override - field layout, inches", (margin, 32),
                cv2.FONT_HERSHEY_SIMPLEX, 0.62, (235, 235, 235), 1, cv2.LINE_AA)
    cv2.putText(canvas, "from Game Manual v1.0 Appendix A; Midfield vertices "
                f"{MIDFIELD_VERTEX_IN:g}in from centre", (margin, 54),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (150, 150, 150), 1, cv2.LINE_AA)
    return canvas
