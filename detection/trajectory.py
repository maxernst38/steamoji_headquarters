"""Turn raw per-frame positions into trajectories, and say where they look wrong.

Smoothing is deliberately light. The tempting move is to smooth hard enough that
the path looks clean, but that hides the failure this stage most needs to expose:
when two robots collide, SAM2 can swap their identities, and the giveaway is a
single impossible jump. Smoothed away, a swap becomes an invisible lie about
which robot went where.
"""
import numpy as np

# A VRC drivetrain tops out around 5 ft/s. Anything well beyond that between
# consecutive samples is a tracking artefact, not driving.
MAX_ROBOT_SPEED_IN_PER_S = 70.0
SMOOTH_WINDOW = 5


def smooth_path(points, window=SMOOTH_WINDOW):
    """Moving average over field coordinates, edges preserved."""
    if len(points) < 3 or window < 3:
        return np.asarray(points, dtype=np.float64)

    pts = np.asarray(points, dtype=np.float64)
    half = min(window, len(pts)) // 2
    smoothed = pts.copy()
    for i in range(len(pts)):
        lo, hi = max(0, i - half), min(len(pts), i + half + 1)
        smoothed[i] = pts[lo:hi].mean(axis=0)
    return smoothed


def analyse_track(samples, fps=30.0, stride=1):
    """Per-track summary, including the jumps that suggest an identity swap."""
    if not samples:
        return {"samples": 0}

    points = np.array([s["field"] for s in samples], dtype=np.float64)
    frames = np.array([s["frame"] for s in samples], dtype=np.float64)

    steps = np.linalg.norm(np.diff(points, axis=0), axis=1) if len(points) > 1 else np.array([])
    gaps_s = np.diff(frames) / float(fps) if len(frames) > 1 else np.array([])
    speeds = np.divide(steps, gaps_s, out=np.zeros_like(steps), where=gaps_s > 0)

    implausible = int((speeds > MAX_ROBOT_SPEED_IN_PER_S).sum())
    out_of_bounds = int(sum(1 for s in samples if not s["in_bounds"]))
    merged = int(sum(1 for s in samples if s.get("merged")))
    conflicts = int(sum(1 for s in samples if s.get("alliance_conflict")))
    oversized = int(sum(1 for s in samples if s.get("oversized")))
    reliable = int(sum(1 for s in samples if usable(s)))
    uncertain = int(sum(1 for s in samples if usable(s) and confidence(s) < 1.0))

    return {
        "samples": len(samples),
        "reliable": reliable,
        "uncertain_identity": uncertain,
        "longest_run": longest_run(samples),
        "merged": merged,
        "alliance_conflicts": conflicts,
        "oversized": oversized,
        "frame_range": [int(frames[0]), int(frames[-1])],
        "distance_in": float(steps.sum()),
        "max_step_in": float(steps.max()) if len(steps) else 0.0,
        "max_speed_in_per_s": float(speeds.max()) if len(speeds) else 0.0,
        "implausible_steps": implausible,
        "out_of_bounds": out_of_bounds,
        "suspect": implausible > 0 or out_of_bounds > len(samples) * 0.05,
    }


def summarise(tracks, fps=30.0, stride=1):
    return {track_id: analyse_track(samples, fps, stride) for track_id, samples in sorted(tracks.items())}


def usable(sample):
    """Whether the sample's *position* means anything.

    Only the bounds check gates this. A merged or oversized mask still sits on a
    real robot - when two robots collide they occupy nearly the same spot, so the
    position stays roughly right and it is the *identity* that is in doubt.
    Dropping those samples discarded good positions to avoid a different problem,
    and left tracks with unbroken runs under a second.
    """
    return bool(sample.get("in_bounds", True))


def confidence(sample):
    """How much to trust which robot this sample belongs to, 0-1.

    Distinct from `usable`: this grades identity, not location.
    """
    if not usable(sample):
        return 0.0
    if sample.get("alliance_conflict"):
        return 0.2
    if sample.get("merged") and sample.get("oversized"):
        return 0.3
    if sample.get("merged") or sample.get("oversized"):
        return 0.5
    return 1.0


def is_reliable(sample):
    """Kept for older trajectory files and callers that want a single boolean."""
    if "reliable" in sample:
        return bool(sample["reliable"])
    return usable(sample)


def path_segments(samples, smooth=True):
    """Runs of consecutive usable samples, each with a confidence per point.

    Returns [(points, confidences), ...]. Breaks happen only where a position is
    meaningless (off the field), not merely uncertain, so a collision now dims the
    path instead of severing it.
    """
    segments, current = [], []
    for sample in samples:
        if usable(sample):
            current.append((sample["field"], confidence(sample)))
        elif current:
            segments.append(current)
            current = []
    if current:
        segments.append(current)

    out = []
    for segment in segments:
        if len(segment) < 2:
            continue
        points = np.array([p for p, _ in segment], dtype=np.float64)
        weights = np.array([c for _, c in segment], dtype=np.float64)
        out.append((smooth_path(points) if smooth else points, weights))
    return out


def longest_run(samples):
    """Longest unbroken usable stretch, in samples - the figure that decides
    whether a track can support a cycle time or a path at all."""
    best = current = 0
    for sample in samples:
        current = current + 1 if usable(sample) else 0
        best = max(best, current)
    return best


def path_points(samples, smooth=True, in_bounds_only=True):
    """Field-coordinate path for rendering."""
    selected = [s for s in samples if usable(s)] if in_bounds_only else list(samples)
    if not selected:
        return np.zeros((0, 2))
    points = np.array([s["field"] for s in selected], dtype=np.float64)
    return smooth_path(points) if smooth else points
