"""Find robots by what moves on the field.

The camera is fixed within a match, so anything that differs from the median
background is either a robot, a game piece being moved, or a person. Restricting
the difference to the field polygon removes the crowd by construction, and the
remaining candidates are filtered by real-world size rather than pixel size -
apparent size varies enormously with depth in an oblique view, so a pixel
threshold that works near the camera fails at the far wall.
"""
import itertools

import cv2
import numpy as np

from calibration.field import FIELD_SIZE_IN

DIFF_THRESHOLD = 38
MIN_BLOB_AREA_PX = 900

# A VRC robot starts within an 18in cube and may expand, so real-world width is a
# far more discriminating filter than pixel area.
MIN_ROBOT_WIDTH_IN = 6.0
MAX_ROBOT_WIDTH_IN = 34.0
MAX_ASPECT_RATIO = 2.5


def field_polygon(homography_pixel_to_field, size_in=FIELD_SIZE_IN):
    """The field's outline in pixel coordinates."""
    corners = np.array([[[0, 0]], [[size_in, 0]], [[size_in, size_in]], [[0, size_in]]], dtype=np.float64)
    inverse = np.linalg.inv(homography_pixel_to_field)
    return cv2.perspectiveTransform(corners, inverse).reshape(-1, 2)


def field_mask(shape, homography_pixel_to_field, size_in=FIELD_SIZE_IN):
    mask = np.zeros(shape[:2], np.uint8)
    cv2.fillPoly(mask, [field_polygon(homography_pixel_to_field, size_in).astype(np.int32)], 255)
    return mask


def motion_mask(frame, background, playing_area, threshold=DIFF_THRESHOLD):
    """Foreground within the field, cleaned of speckle."""
    diff = cv2.absdiff(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.cvtColor(background, cv2.COLOR_BGR2GRAY))
    mask = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)[1]
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    return cv2.bitwise_and(mask, playing_area)


def ground_point(mask, bottom_rows=4):
    """Where a silhouette meets the floor: median x across its lowest rows.

    The median over several rows rather than the single lowest pixel keeps a stray
    wire or antenna from dragging the point sideways. Note this is the robot's
    near edge, not its centre - see `GROUND_POINT_BIAS_NOTE`.
    """
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return None
    lowest = ys.max()
    band = xs[ys >= lowest - bottom_rows]
    return float(np.median(band)), float(lowest)


GROUND_POINT_BIAS_NOTE = (
    "Ground points are the robot's camera-facing edge, roughly half a robot depth "
    "(~9in) nearer the camera than its centre, and that offset rotates with the robot."
)


def _real_width_in(box, homography_pixel_to_field):
    """Width of a blob's base in inches, by projecting its bottom corners."""
    x, y, w, h = box
    corners = np.array([[[float(x), float(y + h)]], [[float(x + w), float(y + h)]]])
    projected = cv2.perspectiveTransform(corners, homography_pixel_to_field).reshape(-1, 2)
    return float(np.linalg.norm(projected[1] - projected[0]))


def robot_candidates(mask, homography_pixel_to_field, min_area=MIN_BLOB_AREA_PX):
    """Blobs whose shape and real-world footprint are consistent with a robot.

    Returns [{"box", "area", "width_in", "ground", "field"}, ...].
    """
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    candidates = []

    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        if area < min_area:
            continue

        x = int(stats[index, cv2.CC_STAT_LEFT])
        y = int(stats[index, cv2.CC_STAT_TOP])
        w = int(stats[index, cv2.CC_STAT_WIDTH])
        h = int(stats[index, cv2.CC_STAT_HEIGHT])

        # The blue elevation bar reads as 95x23px; robots are never that flat.
        if h == 0 or w / float(h) > MAX_ASPECT_RATIO:
            continue

        width_in = _real_width_in((x, y, w, h), homography_pixel_to_field)
        if not MIN_ROBOT_WIDTH_IN <= width_in <= MAX_ROBOT_WIDTH_IN:
            continue

        blob = (labels == index).astype(np.uint8)
        ground = ground_point(blob)
        if ground is None:
            continue
        field = cv2.perspectiveTransform(
            np.array([[[ground[0], ground[1]]]], dtype=np.float64), homography_pixel_to_field
        )[0, 0]

        candidates.append({
            "box": (x, y, w, h),
            "area": area,
            "width_in": width_in,
            "ground": (float(ground[0]), float(ground[1])),
            "field": (float(field[0]), float(field[1])),
        })

    candidates.sort(key=lambda c: -c["area"])
    return candidates


TYPICAL_ROBOT_WIDTH_IN = 18.0

# Two robots are solid 18in bodies, so their ground points cannot be closer than
# roughly this even when touching. Anything nearer is one robot split in two.
MIN_ROBOT_SEPARATION_IN = 14.0

# Mean distance from a real robot's width, averaged over the chosen set. Frames
# worse than this are seeding on merged pairs or fragments.
MAX_SEED_WIDTH_DEVIATION_IN = 4.0
MAX_CANDIDATES_TO_SEARCH = 8


def _rate_subset(subset):
    """(crowded pairs, mean width deviation) for one candidate set - lower is better."""
    widths = np.array([c["width_in"] for c in subset], dtype=np.float64)
    deviation = float(np.abs(widths - TYPICAL_ROBOT_WIDTH_IN).mean())

    positions = np.array([c["field"] for c in subset], dtype=np.float64)
    crowded, separation = 0, 0.0
    if len(positions) > 1:
        distances = np.linalg.norm(positions[:, None, :] - positions[None, :, :], axis=2)
        upper = distances[np.triu_indices(len(positions), k=1)]
        crowded = int((upper < MIN_ROBOT_SEPARATION_IN).sum())
        separation = float(upper.mean())
    return crowded, deviation, separation


def best_subset(candidates, expected):
    """Pick the `expected` candidates that look most like a set of real robots.

    Frames commonly show the robots plus something else - a game piece being
    carried, a hand reaching in, the elevation bar. Insisting on exactly `expected`
    blobs throws away those frames, and they are often the cleanest ones available:
    the best early frame in this footage has four textbook 18in robots alongside
    one spurious blob.
    """
    if len(candidates) <= expected:
        return candidates, _rate_subset(candidates) if candidates else (99, 1e6, 0.0)

    pool = candidates[:MAX_CANDIDATES_TO_SEARCH]
    best, rating = None, None
    for subset in itertools.combinations(pool, expected):
        current = _rate_subset(subset)
        key = (current[0], current[1], -current[2])
        if rating is None or key < rating:
            best, rating = list(subset), key
    return best, _rate_subset(best)


def find_seed_frame(video_path, background, playing_area, homography_pixel_to_field,
                    start_frame, end_frame, step=30, expected=4, threshold=DIFF_THRESHOLD,
                    progress=None):
    """Search a window for the frame that yields the cleanest set of robots.

    A stationary robot leaves no trace in a single frame's difference, so no one
    frame is guaranteed to show all of them. Unioning motion across a window was
    tried first and does not work: over even a few seconds each robot smears along
    its own path into one connected blob far too wide to pass as a robot, which
    left zero candidates. Searching frames instead keeps every candidate compact,
    and across a match many individual frames do show all four.

    Returns (frame_index, frame, candidates).
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"could not open video: {video_path}")

    # Frames that clear the quality bar, kept in time order, plus the best-scoring
    # frame as a fallback when nothing clears it.
    acceptable, best = [], None
    wanted = list(range(start_frame, end_frame, step))
    for done, index in enumerate(wanted):
        if progress and wanted:
            progress((done + 1) / len(wanted))
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = cap.read()
        if not ok:
            continue

        found = robot_candidates(motion_mask(frame, background, playing_area, threshold),
                                 homography_pixel_to_field)
        if not found:
            continue

        # Extra blobs are tolerated and the best four chosen; requiring an exact
        # count discards the cleanest frames. Separation still matters, because
        # four blobs bunched together are fragments of one robot, not four robots.
        candidates, (crowded, width_penalty, separation) = best_subset(found, expected)

        count_penalty = abs(len(candidates) - expected)
        score = (count_penalty, crowded, width_penalty, -separation)
        if best is None or score < best[0]:
            best = (score, index, frame.copy(), candidates)
        if (count_penalty == 0 and crowded == 0
                and width_penalty <= MAX_SEED_WIDTH_DEVIATION_IN and not acceptable):
            # Earliest good frame wins over the best good frame. Everything before
            # the seed is reached by propagating backwards, which is where tracks
            # get lost and mislabelled, so the seed should sit as close to the
            # start of the match as quality allows rather than mid-match.
            acceptable.append((index, frame.copy(), candidates))

    cap.release()
    if acceptable:
        return acceptable[0]
    if best is None:
        raise ValueError(f"no readable frames in [{start_frame}, {end_frame})")

    _, index, frame, candidates = best
    return index, frame, candidates
