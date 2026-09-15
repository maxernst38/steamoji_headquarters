"""Find the game pieces, and work out which of them are scored.

`<SC3>` defines a Pin half as scoring if it is "nested inside the transparent half
of a Cup or is not covered by a Cup" - that is, if it is *visible*. An occluded
half does not score. So counting visible colour bands is counting scored halves
rather than approximating them, which is what makes this tractable from one camera.

Colour separates the pieces cleanly here: red, yellow and blue form distinct
populations on the field with nothing in between.

Stacks are read per Goal rather than found anywhere in the frame. Goals never
move, and `<SC2>` only counts a Pin nested with a Goal, so the nine known
positions are the only places a scored stack can be. Reading a column above each
one beats grouping blobs three ways over:

- a stack's identity is its Goal, so following one over time needs no matching
  between frames - the association problem behind the robot-tracking identity
  swaps simply does not arise
- position comes from the layout instead of from pixels, removing the elevation
  error: a stack's lowest band sits about 5in up on its Goal, and projecting that
  through a floor-plane homography displaced the nine measured stacks by 7 to 37in
- the answer is capped at nine, where blob grouping once reported thirteen stacks
  on a field that has nine Goals
"""
import cv2
import numpy as np

from calibration.field_layout import GOALS, quadrant_of

RED, YELLOW, BLUE = "red", "yellow", "blue"

# BGR, for drawing detections back onto the frame.
BAND_COLOURS = {RED: (60, 60, 255), YELLOW: (0, 215, 255), BLUE: (255, 140, 0)}

# Hue bands, OpenCV's 0-179 scale. Red wraps, so it needs two.
HUE_BANDS = {
    RED: (((0, 10),), ((170, 180),)),
    YELLOW: (((11, 35),),),
    BLUE: (((86, 130),),),
}
MIN_SATURATION = 110
MIN_VALUE = 70
MIN_BAND_AREA = 30

# A stack of six Pins stands about 20in; 26in leaves headroom without reaching
# the next Goal, which is at least 33.9in away.
MAX_STACK_IN = 26.0
COLUMN_WIDTH_IN = 7.0

# Above this share of a Goal's column covered by a robot, the count is unknown
# rather than zero.
OCCLUSION_FRACTION = 0.35


def _colour_mask(hsv, colour):
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    vivid = (sat > MIN_SATURATION) & (val > MIN_VALUE)
    selected = np.zeros(hue.shape, bool)
    for group in HUE_BANDS[colour]:
        for low, high in group:
            selected |= (hue >= low) & (hue <= high)
    return (vivid & selected).astype(np.uint8)


def goal_columns(homography, shape, max_stack_in=MAX_STACK_IN, width_in=COLUMN_WIDTH_IN):
    """The image region above each Goal where its stack appears.

    Sized in inches and converted per Goal, because a Goal at the far wall covers
    a fraction of the pixels of one near the camera - a fixed pixel box would be
    far too small at one end of the field and would swallow neighbours at the other.
    """
    inverse = np.linalg.inv(homography)
    columns = []
    for goal in GOALS:
        x_in, y_in = goal["pos"]
        base = cv2.perspectiveTransform(np.array([[[x_in, y_in]]], dtype=np.float64), inverse)[0, 0]
        beside = cv2.perspectiveTransform(
            np.array([[[x_in + 6.0, y_in]]], dtype=np.float64), inverse)[0, 0]
        px_per_in = max(float(np.linalg.norm(beside - base)) / 6.0, 0.8)

        half_width = max(int(round(width_in * px_per_in / 2)), 6)
        height = max(int(round(max_stack_in * px_per_in)), 20)
        cx, cy = int(round(base[0])), int(round(base[1]))
        columns.append({
            "goal": goal,
            "base_px": (cx, cy),
            "roi": (max(cx - half_width, 0), max(cy - height, 0),
                    min(cx + half_width, shape[1] - 1), min(cy + 4, shape[0] - 1)),
            "px_per_in": round(px_per_in, 2),
        })
    return columns


def read_goal(frame_bgr, column, min_band_area=MIN_BAND_AREA):
    """Colour bands stacked on one Goal, ordered bottom-up.

    Bands are found inside that Goal's own column, so a neighbouring stack cannot
    contribute to this Goal's count.
    """
    x0, y0, x1, y1 = column["roi"]
    if x1 <= x0 or y1 <= y0:
        return {"colours": [], "height": 0, "bands": []}

    hsv = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    found = []
    for colour in (RED, YELLOW, BLUE):
        mask = cv2.morphologyEx(_colour_mask(hsv, colour), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        count, _, stats, centroids = cv2.connectedComponentsWithStats(mask)
        for index in range(1, count):
            if int(stats[index, cv2.CC_STAT_AREA]) < min_band_area:
                continue
            found.append({"colour": colour, "y": float(centroids[index][1]),
                          "area": int(stats[index, cv2.CC_STAT_AREA])})

    found.sort(key=lambda b: -b["y"])
    return {"colours": [b["colour"] for b in found], "height": len(found), "bands": found}


def read_field(frame_bgr, homography, occupied_mask=None):
    """Every Goal's stack, keyed by Goal.

    `occupied_mask` marks pixels covered by robots. A Goal behind a robot reports
    as occluded with an unknown count rather than as empty, because otherwise every
    robot driving past a Goal produces a phantom descore in the event timeline.
    """
    goals = []
    for column in goal_columns(homography, frame_bgr.shape):
        x0, y0, x1, y1 = column["roi"]
        occluded = False
        if occupied_mask is not None and x1 > x0 and y1 > y0:
            occluded = float((occupied_mask[y0:y1, x0:x1] > 0).mean()) > OCCLUSION_FRACTION

        reading = {"colours": [], "height": 0} if occluded else read_goal(frame_bgr, column)
        goal = column["goal"]
        goals.append({
            "pos": list(goal["pos"]),
            "kind": goal["kind"],
            "pair": goal.get("pair"),
            "quadrant": quadrant_of(*goal["pos"]),
            "height": reading["height"],
            "colours": reading["colours"],
            "occluded": occluded,
            "roi": column["roi"],
        })
    return {"goals": goals}


def tally(state, toggle_colours=None):
    """Points from a field reading, following `<SC3>` and `<SC5>`.

    Alliance halves score 5 each. A yellow half scores 10, but only for the
    Alliance whose colour the Toggle in that Quadrant is set to; a yellow Toggle
    means those halves score for nobody. `toggle_colours` is therefore required
    rather than assumed - without it the yellow halves are reported unowned instead
    of being quietly awarded to someone.
    """
    points = {"red": 0, "blue": 0}
    unowned_yellow = 0

    for goal in state["goals"]:
        if goal["occluded"]:
            continue
        for colour in goal["colours"]:
            if colour in (RED, BLUE):
                points[colour] += 5
            else:
                owner = (toggle_colours or {}).get(goal["quadrant"])
                if owner in ("red", "blue"):
                    points[owner] += 10
                else:
                    unowned_yellow += 1

    return {
        "points": points,
        "yellow_halves_unowned": unowned_yellow,
        "occluded_goals": sum(1 for g in state["goals"] if g["occluded"]),
    }
