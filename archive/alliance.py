"""Identify which alliance a tracked robot belongs to, from its bumper colour.

NOT CURRENTLY USED. Measured against four confirmed-distinct robots, three read
95-100% red in a match the scoreboard says is two red against two blue: VEX robots
are built from red-anodised aluminium and red rubber bands regardless of alliance,
and both alliances' game pieces cover the field. Unlike FRC, where bumpers are
large and definitive, VRC's alliance marker is a small licence plate that a colour
ratio drowns out. Kept because starting position - alliances begin on opposite
sides - is a sounder basis, and that needs the calibration orientation resolved.

The idea was that alliance is a hard constraint rather than a hint - a robot
cannot change alliance mid-match, so a track whose colour flips has swapped onto
a different robot. That reasoning still holds; what fails is reading the alliance
from colour ratios in this game.
"""
import cv2
import numpy as np

# Red wraps around the hue circle, so it needs two bands.
RED_BANDS = ((0, 12), (168, 180))
BLUE_BAND = (95, 135)
MIN_SATURATION = 90
MIN_VALUE = 50

# Below this share of coloured pixels the reading is not trustworthy - a robot
# seen edge-on, in shadow, or mostly occluded shows very little bumper.
MIN_COLOURED_FRACTION = 0.02
MIN_DOMINANCE = 1.6

RED, BLUE = "red", "blue"


def colour_counts(frame_bgr, mask):
    """Red and blue pixel counts inside the mask."""
    if mask is None or mask.sum() == 0:
        return 0, 0, 0

    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

    inside = mask > 0
    vivid = inside & (sat >= MIN_SATURATION) & (val >= MIN_VALUE)

    red = np.zeros_like(vivid)
    for low, high in RED_BANDS:
        red |= vivid & (hue >= low) & (hue <= high)
    blue = vivid & (hue >= BLUE_BAND[0]) & (hue <= BLUE_BAND[1])

    return int(red.sum()), int(blue.sum()), int(inside.sum())


def alliance_of(frame_bgr, mask):
    """Return (alliance, confidence) where alliance is "red", "blue", or None.

    None means "cannot tell from this frame", which is different from "neither" -
    the caller should carry the track's established alliance rather than treating
    an unreadable frame as a contradiction.
    """
    red, blue, total = colour_counts(frame_bgr, mask)
    if total == 0:
        return None, 0.0

    coloured = red + blue
    if coloured / float(total) < MIN_COLOURED_FRACTION:
        return None, 0.0

    winner, dominant, other = (RED, red, blue) if red >= blue else (BLUE, blue, red)
    if dominant < max(other * MIN_DOMINANCE, 1):
        return None, 0.0

    return winner, float(dominant) / float(coloured)


def anchor_alliances(samples_by_track):
    """The alliance each track belongs to, by majority vote over its readings.

    Voting across the whole track rather than trusting the seed frame: a single
    frame can catch a robot from an angle that hides its bumper, and one bad
    reading should not define the track's identity for the whole match.
    """
    anchors = {}
    for track_id, samples in samples_by_track.items():
        votes = {}
        for sample in samples:
            reading = sample.get("alliance")
            if reading:
                votes[reading] = votes.get(reading, 0) + 1
        anchors[track_id] = max(votes, key=votes.get) if votes else None
    return anchors


def contradiction_flags(samples_by_track, anchors):
    """Mark samples whose colour contradicts their track's alliance.

    A contradiction means the mask is sitting on a robot from the other alliance,
    so the position belongs to a different robot than the id claims.
    """
    flagged = 0
    for track_id, samples in samples_by_track.items():
        anchor = anchors.get(track_id)
        for sample in samples:
            reading = sample.get("alliance")
            wrong = bool(anchor and reading and reading != anchor)
            sample["alliance_conflict"] = wrong
            flagged += int(wrong)
    return flagged
