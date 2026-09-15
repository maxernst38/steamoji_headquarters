"""Build a YOLO dataset of robot boxes from tracked runs and hand-drawn seeds.

The labels come from SAM2's tracked output rather than from motion, and that
distinction is the whole point: SAM2 carries a track through stretches where the
robot is standing still, so its boxes include the stationary robots that motion
never sees. Training on motion output would teach the detector the same blind spot
we are trying to remove.

Splitting is by frame range, never at random. Consecutive frames a tenth of a
second apart are near-identical, so a random split puts near-duplicates on both
sides and reports an accuracy the model does not have.
"""
import glob
import json
import os
import random

import cv2

from storage import seed_store

DATASET_DIR = os.path.join("training", "dataset")
DEFAULT_VAL_FRACTION = 0.3

# Only samples the tracker was confident about become labels: a merged mask spans
# two robots, so its box would teach the detector that two touching robots are one.
MIN_CONFIDENCE_FOR_LABEL = 1.0


def _samples_with_boxes(trajectory_file):
    with open(trajectory_file) as handle:
        data = json.load(handle)

    video = data.get("video")
    rows = []
    for samples in data.get("tracks", {}).values():
        for sample in samples:
            box = sample.get("box")
            if not box or not sample.get("in_bounds", True):
                continue
            if sample.get("merged") or sample.get("oversized"):
                continue
            rows.append((video, int(sample["frame"]), box))
    return rows


def gather(results_glob=("data/results/*_trajectories.json", "training/labels_source/*_trajectories.json")):
    """Every usable labelled box, grouped by (video, frame)."""
    by_frame = {}
    skipped_no_boxes = 0

    for pattern in results_glob:
        for path in sorted(glob.glob(pattern)):
            rows = _samples_with_boxes(path)
            if not rows:
                skipped_no_boxes += 1
                continue
            for video, frame, box in rows:
                by_frame.setdefault((video, frame), []).append(box)

    for record in seed_store.list_all():
        video = os.path.join("videos", record["video"])
        by_frame.setdefault((video, record["frame_index"]), []).extend(record["boxes"])

    return by_frame, skipped_no_boxes


def _write_split(frames, split, dataset_dir, sample_every, seen):
    images_dir = os.path.join(dataset_dir, "images", split)
    labels_dir = os.path.join(dataset_dir, "labels", split)
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(labels_dir, exist_ok=True)

    written = 0
    for index, ((video, frame), boxes) in enumerate(sorted(frames.items())):
        if index % sample_every:
            continue                      # neighbouring frames are near-duplicates
        if not os.path.exists(video):
            continue

        cap = seen.get(video)
        if cap is None:
            cap = seen[video] = cv2.VideoCapture(video)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
        ok, image = cap.read()
        if not ok:
            continue

        height, width = image.shape[:2]
        stem = f"{os.path.splitext(os.path.basename(video))[0][:40].replace(' ', '_')}_{frame:06d}"
        cv2.imwrite(os.path.join(images_dir, f"{stem}.jpg"), image, [cv2.IMWRITE_JPEG_QUALITY, 90])

        lines = []
        for x, y, w, h in boxes:
            cx, cy = (x + w / 2) / width, (y + h / 2) / height
            bw, bh = w / width, h / height
            if not (0 < bw <= 1 and 0 < bh <= 1 and 0 <= cx <= 1 and 0 <= cy <= 1):
                continue
            lines.append(f"0 {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
        with open(os.path.join(labels_dir, f"{stem}.txt"), "w") as handle:
            handle.write("\n".join(lines))
        written += 1
    return written


def export(dataset_dir=DATASET_DIR, sample_every=5, val_fraction=DEFAULT_VAL_FRACTION, log=print):
    """Write a YOLO dataset, split by time so no frame leaks across the boundary."""
    by_frame, skipped = gather()
    if not by_frame:
        raise SystemExit(
            "no labelled boxes found. Tracked runs recorded before boxes were added "
            "contain none - rerun tracking on a segment, or draw seeds in the UI."
        )
    if skipped:
        log(f"skipped {skipped} older trajectory file(s) that predate box recording")

    frames = sorted(by_frame)
    # The split point is a frame number, so training and validation come from
    # different parts of the match and cannot contain the same moment twice.
    cut = int(len(frames) * (1 - val_fraction))
    train_frames = {f: by_frame[f] for f in frames[:cut]}
    val_frames = {f: by_frame[f] for f in frames[cut:]}

    seen = {}
    try:
        n_train = _write_split(train_frames, "train", dataset_dir, sample_every, seen)
        n_val = _write_split(val_frames, "val", dataset_dir, sample_every, seen)
    finally:
        for cap in seen.values():
            cap.release()

    yaml_path = os.path.join(dataset_dir, "robots.yaml")
    with open(yaml_path, "w") as handle:
        handle.write(
            f"path: {os.path.abspath(dataset_dir)}\n"
            "train: images/train\n"
            "val: images/val\n"
            "names:\n  0: robot\n"
        )

    boxes = sum(len(v) for v in by_frame.values())
    log(f"{boxes} boxes over {len(by_frame)} frames -> {n_train} train / {n_val} val images")
    log(f"wrote {yaml_path}")
    return yaml_path, n_train, n_val


if __name__ == "__main__":
    export()
