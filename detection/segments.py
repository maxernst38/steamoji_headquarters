"""Split a broadcast into its individual matches by finding camera cuts.

A tournament stream cuts between fields, replays and graphics, and each match is
one continuous shot. Detecting the cuts means the UI can offer real matches to
choose from instead of asking for frame numbers nobody knows.

Masking the scoreboard overlay before differencing is required, not an
optimisation: the score and timer change constantly, and unmasked they register
as cuts throughout the match.
"""
import cv2
import numpy as np

SAMPLE_STEP = 15
THUMB_SIZE = (160, 90)
CUT_SIGMA = 4.0

# The broadcast overlay occupies a top bar and a right-hand panel. Fractions
# rather than pixels so the same masking works at any resolution.
OVERLAY_TOP_FRACTION = 0.13
OVERLAY_RIGHT_FRACTION = 0.13

MIN_SEGMENT_SECONDS = 20.0


def _thumbnails(video_path, step=SAMPLE_STEP):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    frames, indices = [], []
    index = 0
    while index < total:
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.cvtColor(cv2.resize(frame, THUMB_SIZE), cv2.COLOR_BGR2GRAY).astype(np.float32)
        small[: int(THUMB_SIZE[1] * OVERLAY_TOP_FRACTION), :] = 0
        small[:, int(THUMB_SIZE[0] * (1 - OVERLAY_RIGHT_FRACTION)) :] = 0
        frames.append(small)
        indices.append(index)
        index += step
    cap.release()
    return frames, indices, fps, total


def find_cuts(video_path, step=SAMPLE_STEP, sigma=CUT_SIGMA, progress=None):
    """Frame indices where the shot changes."""
    frames, indices, fps, total = _thumbnails(video_path, step)
    if len(frames) < 3:
        return [], fps, total

    diffs = np.array([np.abs(frames[i] - frames[i - 1]).mean() for i in range(1, len(frames))])
    threshold = diffs.mean() + sigma * diffs.std()
    cuts = [indices[i + 1] for i, value in enumerate(diffs) if value > threshold]
    if progress:
        progress(1.0)
    return cuts, fps, total


def find_segments(video_path, step=SAMPLE_STEP, min_seconds=MIN_SEGMENT_SECONDS, progress=None):
    """Continuous shots long enough to be a match.

    Returns [{"index", "start", "end", "start_s", "end_s", "duration_s"}, ...].
    Short shots between matches - replays, graphics, bracket screens - are dropped
    by the duration floor, since a VRC match runs about 1:45.
    """
    cuts, fps, total = find_cuts(video_path, step, progress=progress)

    boundaries = [0] + list(cuts) + [total]
    segments = []
    for start, end in zip(boundaries, boundaries[1:]):
        duration = (end - start) / float(fps)
        if duration < min_seconds:
            continue
        segments.append({
            "index": len(segments),
            "start": int(start),
            "end": int(end),
            "start_s": round(start / float(fps), 1),
            "end_s": round(end / float(fps), 1),
            "duration_s": round(duration, 1),
        })
    return segments
