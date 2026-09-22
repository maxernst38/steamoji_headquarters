"""The field homography, stored per match rather than per video.

A broadcast switches cameras between matches - it does so in the sample footage -
so one homography per file is wrong for every match after the first. Nothing about
a stale homography looks wrong downstream: positions stay plausible, paths stay
smooth, and the numbers are simply about a different camera's geometry. Keying on
the match makes that impossible.

Each match's field is drawn fresh; nothing is inherited from a neighbouring match,
so a quad can never be silently reused across a camera change.
"""
import json
import os
import time

import numpy as np

CALIBRATION_DIR = os.path.join("data", "calibrations")


def _slug(text):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)[:80]


def calibration_path(video_name, start, end, directory=CALIBRATION_DIR):
    return os.path.join(directory, f"{_slug(os.path.basename(video_name))}_f{start}-{end}.json")


def save(video_name, start, end, frame_index, corners, homography, directory=CALIBRATION_DIR):
    """Persist the drawn quad and the homography it produces.

    The corners are kept alongside the matrix so the editor can reopen exactly
    what was drawn, rather than making the user start over to make a small change.
    """
    os.makedirs(directory, exist_ok=True)
    record = {
        "video": os.path.basename(video_name),
        "frame_range": [int(start), int(end)],
        "frame_index": int(frame_index),
        "corners": [[float(x), float(y)] for x, y in corners],
        "homography_pixel_to_field": np.asarray(homography).tolist(),
        "saved_at": time.time(),
    }
    path = calibration_path(video_name, start, end, directory)
    temporary = f"{path}.{os.getpid()}.tmp"   # unique: two writers must not share it
    with open(temporary, "w") as handle:
        json.dump(record, handle, indent=2)
    os.replace(temporary, path)
    return record


def load(video_name, start, end, directory=CALIBRATION_DIR):
    path = calibration_path(video_name, start, end, directory)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if not record.get("homography_pixel_to_field"):
        return None
    record["homography_pixel_to_field"] = np.array(record["homography_pixel_to_field"])
    return record


def exists(video_name, start, end, directory=CALIBRATION_DIR):
    return load(video_name, start, end, directory) is not None


def list_all(directory=CALIBRATION_DIR):
    """Every saved calibration - the set of matches someone has set a field for."""
    if not os.path.isdir(directory):
        return []
    records = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name)) as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        if record.get("homography_pixel_to_field") and record.get("frame_range"):
            records.append(record)
    return records
