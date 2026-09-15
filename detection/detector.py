"""Find robots by appearance, in any frame, moving or not.

OFF BY DEFAULT - the first trained model does not work, and the reason is worth
recording so the mistake is not repeated.

Labels were bootstrapped from SAM2 tracked output. But that tracker was itself
losing robots and drifting onto scenery: one track died after 391 of 1447 samples,
another sat nearly motionless on a static object for a whole match. Those errors
became labels, so the model learned that a stack of game pieces is a robot - it
fires at 0.92 confidence on a piece stack while ignoring the robots either side
of it (`docs/detector-failure.png`).

The held-out numbers hid this completely: precision 0.871, recall 0.683, mAP50
0.802. They are measured against validation labels drawn from the same faulty
tracker, so a model that faithfully reproduces the tracker's mistakes scores well.
Agreement with a flawed teacher is not accuracy.

The way out is labels that do not come from the thing being fixed - frames
labelled by hand. `seed_store` already collects them, so widening that from four
boxes per match to a proper labelling pass is the honest path.

Everything degrades gracefully when no weights exist: `available()` is false and
callers fall back to motion.
"""
import os

import numpy as np

DEFAULT_WEIGHTS = os.path.join("training", "weights", "robots", "weights", "best.pt")
DEFAULT_CONFIDENCE = 0.35
DEFAULT_IMAGE_SIZE = 960


def available(weights=DEFAULT_WEIGHTS):
    return os.path.exists(weights)


class RobotDetector:
    """Loaded once and reused; loading per call would dominate the runtime."""

    def __init__(self, weights=DEFAULT_WEIGHTS, confidence=DEFAULT_CONFIDENCE,
                 image_size=DEFAULT_IMAGE_SIZE, device="cuda"):
        if not available(weights):
            raise FileNotFoundError(
                f"no detector weights at {weights} - run training/train_detector.py first"
            )
        from ultralytics import YOLO

        self.model = YOLO(weights)
        self.confidence = confidence
        self.image_size = image_size
        self.device = device

    def detect(self, frame_bgr, playing_area=None):
        """Boxes as [{"box": (x, y, w, h), "score": float}, ...].

        `playing_area` drops detections whose base falls outside the field, which
        removes robots waiting in the pits or on a neighbouring field without
        needing the model to have learned the field boundary.
        """
        results = self.model.predict(frame_bgr, imgsz=self.image_size, conf=self.confidence,
                                     device=self.device, verbose=False)
        found = []
        for result in results:
            for box, score in zip(result.boxes.xyxy.cpu().numpy(),
                                  result.boxes.conf.cpu().numpy()):
                x1, y1, x2, y2 = box
                w, h = x2 - x1, y2 - y1
                if playing_area is not None:
                    # The base is what matters: a robot is on the field if its
                    # wheels are, regardless of how far its lift reaches.
                    bx, by = int(round(x1 + w / 2)), int(round(min(y2, playing_area.shape[0] - 1)))
                    if not (0 <= bx < playing_area.shape[1] and playing_area[by, bx]):
                        continue
                found.append({
                    "box": (int(round(x1)), int(round(y1)), int(round(w)), int(round(h))),
                    "score": float(score),
                })
        found.sort(key=lambda d: -d["score"])
        return found


def match_to_tracks(detections, track_positions, max_distance_px=140):
    """Greedily pair detections with the tracks they most likely belong to.

    Greedy on distance rather than the Hungarian assignment: with at most four
    robots the two agree almost always, and greedy makes an unmatched detection
    obvious instead of forcing a pairing to complete the assignment.

    Returns (pairs, unmatched_detections, unmatched_track_ids).
    """
    pairs = {}
    remaining = list(range(len(detections)))
    open_tracks = dict(track_positions)

    candidates = []
    for index, detection in enumerate(detections):
        x, y, w, h = detection["box"]
        base = np.array([x + w / 2.0, y + h])
        for track_id, position in open_tracks.items():
            candidates.append((float(np.linalg.norm(base - np.asarray(position))), index, track_id))
    candidates.sort()

    for distance, index, track_id in candidates:
        if distance > max_distance_px or index not in remaining or track_id not in open_tracks:
            continue
        pairs[track_id] = index
        remaining.remove(index)
        open_tracks.pop(track_id)

    return pairs, remaining, list(open_tracks)
