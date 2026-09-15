"""Robot boxes drawn by hand, kept per match.

Motion cannot see a robot that is holding still, so automatic seeding is a guess
that is right about half the time. Drawing the boxes once removes that guess
entirely for the start of a match.

The boxes are worth more than the run they seed: each one is a labelled robot in a
real frame, which is exactly what training a detector needs. This store is the
collection point for that dataset as much as it is a cache.
"""
import json
import os
import time

SEED_DIR = os.path.join("data", "seeds")


def _slug(text):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)[:80]


def seed_path(video_name, start, end, seed_dir=SEED_DIR):
    return os.path.join(seed_dir, f"{_slug(os.path.basename(video_name))}_f{start}-{end}.json")


def save(video_name, start, end, frame_index, boxes, seed_dir=SEED_DIR, source="manual"):
    """Persist boxes as [x, y, w, h] in source-frame pixels."""
    os.makedirs(seed_dir, exist_ok=True)
    record = {
        "video": os.path.basename(video_name),
        "frame_range": [int(start), int(end)],
        "frame_index": int(frame_index),
        "boxes": [[int(round(v)) for v in box] for box in boxes],
        "source": source,
        "saved_at": time.time(),
    }
    path = seed_path(video_name, start, end, seed_dir)
    temporary = f"{path}.tmp"
    with open(temporary, "w") as handle:
        json.dump(record, handle, indent=2)
    os.replace(temporary, path)
    return record


def load(video_name, start, end, seed_dir=SEED_DIR):
    path = seed_path(video_name, start, end, seed_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    return record if record.get("boxes") else None


def list_all(seed_dir=SEED_DIR):
    """Every saved seed set - the training-label pool."""
    if not os.path.isdir(seed_dir):
        return []
    records = []
    for name in sorted(os.listdir(seed_dir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(seed_dir, name)) as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        if record.get("boxes"):
            records.append(record)
    return records


def to_seeds(record, homography=None):
    """Convert stored boxes into the shape `track_robots` expects.

    `find_seed_frame` returns dicts carrying width and field position too, but the
    tracker only reads "box", so hand-drawn boxes need nothing further. The extra
    fields are filled where a homography is available so logging and filtering
    behave the same for manual and automatic seeds.
    """
    import cv2
    import numpy as np

    seeds = []
    for box in record["boxes"]:
        x, y, w, h = box
        entry = {"box": (int(x), int(y), int(w), int(h)), "area": int(w * h), "source": "manual"}
        if homography is not None:
            corners = np.array([[[float(x), float(y + h)]], [[float(x + w), float(y + h)]]])
            projected = cv2.perspectiveTransform(corners, homography).reshape(-1, 2)
            entry["width_in"] = float(np.linalg.norm(projected[1] - projected[0]))
            centre = np.array([[[float(x + w / 2), float(y + h)]]])
            field = cv2.perspectiveTransform(centre, homography)[0, 0]
            entry["ground"] = (float(x + w / 2), float(y + h))
            entry["field"] = (float(field[0]), float(field[1]))
        seeds.append(entry)
    return seeds
