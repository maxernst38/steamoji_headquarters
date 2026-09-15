"""Per-pixel median background over a range of frames.

The camera is fixed within a shot, so a median across the shot removes robots,
people, and anything else transient, leaving the static field. Later stages use
the same background as their motion-detection reference.
"""
import cv2
import numpy as np


def sample_frames(video_path, start_frame, end_frame, step=30, progress=None):
    """Read every `step`-th frame in [start_frame, end_frame)."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"could not open video: {video_path}")

    wanted = list(range(start_frame, end_frame, step))
    frames = []
    for done, idx in enumerate(wanted):
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if ok:
            frames.append(frame)
        if progress and wanted:
            progress((done + 1) / len(wanted))
    cap.release()

    if not frames:
        raise ValueError(f"no frames read from {video_path} in [{start_frame}, {end_frame})")
    return frames


def median_background(video_path, start_frame, end_frame, step=30, progress=None):
    """Median frame over the range. ~1 frame/sec is plenty to erase movement."""
    frames = sample_frames(video_path, start_frame, end_frame, step, progress=progress)
    return np.median(np.stack(frames), axis=0).astype(np.uint8), frames
