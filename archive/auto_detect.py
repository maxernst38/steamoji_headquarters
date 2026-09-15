"""Detect the field's four floor-level corners from a shot's median background.

Colour alone cannot find the field: the interior and its surroundings are
statistically identical in hue and saturation, and the field's own white floor
markings are as bright as the perimeter wall. So the boundary is found
geometrically instead - search for four lines forming a convex quad, scored by
how well each edge behaves like a floor line: playing surface on the inside,
something other than playing surface on the outside.
"""
import itertools

import cv2
import numpy as np

MAT_S_MAX, MAT_V_MIN, MAT_V_MAX = 70, 90, 165

MIN_SEGMENT_LENGTH = 60
ANGLE_MERGE_DEG = 4.0
OFFSET_MERGE_PX = 18.0
MAX_LINES_PER_GROUP = 14

EDGE_SAMPLES = 28
EDGE_PROBE_PX = 9
REGION_BETA = 0.25
MIN_AREA_FRACTION, MAX_AREA_FRACTION = 0.04, 0.60


class DetectionFailed(Exception):
    """Raised when the field cannot be detected with acceptable confidence."""


def mat_mask(median_bgr):
    """Desaturated, mid-brightness pixels - the playing surface, minus its markings."""
    hsv = cv2.cvtColor(median_bgr, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1], hsv[:, :, 2]
    return ((sat < MAT_S_MAX) & (val > MAT_V_MIN) & (val < MAT_V_MAX)).astype(np.uint8)


def candidate_lines(median_bgr):
    """Detect line segments and merge collinear ones into distinct infinite lines.

    Returns a list of (theta, rho, weight) in Hesse normal form.
    """
    gray = cv2.cvtColor(median_bgr, cv2.COLOR_BGR2GRAY)
    detector = cv2.createLineSegmentDetector()
    segments = detector.detect(gray)[0]
    if segments is None or len(segments) == 0:
        raise DetectionFailed("no line segments detected")

    raw = []
    for seg in segments.reshape(-1, 4):
        x1, y1, x2, y2 = map(float, seg)
        length = float(np.hypot(x2 - x1, y2 - y1))
        if length < MIN_SEGMENT_LENGTH:
            continue
        theta = np.arctan2(y2 - y1, x2 - x1) % np.pi
        # perpendicular distance from origin to the line through the segment
        rho = x1 * np.sin(theta) - y1 * np.cos(theta)
        raw.append((theta, rho, length))

    if not raw:
        raise DetectionFailed(f"no line segments longer than {MIN_SEGMENT_LENGTH}px")

    return _merge_lines(raw)


def _merge_lines(raw):
    """Length-weighted merge of near-identical lines."""
    merged = []
    for theta, rho, weight in sorted(raw, key=lambda r: -r[2]):
        for i, (mt, mr, mw) in enumerate(merged):
            dtheta = abs(theta - mt)
            dtheta = min(dtheta, np.pi - dtheta)
            if np.degrees(dtheta) < ANGLE_MERGE_DEG and abs(rho - mr) < OFFSET_MERGE_PX:
                total = mw + weight
                merged[i] = (
                    (mt * mw + theta * weight) / total,
                    (mr * mw + rho * weight) / total,
                    total,
                )
                break
        else:
            merged.append((theta, rho, weight))
    return merged


def group_by_orientation(lines):
    """Split lines into two orientation families (the quad's two pairs of sides).

    Angles are doubled before clustering so that 179 deg and 1 deg count as close.
    """
    if len(lines) < 4:
        raise DetectionFailed(f"only {len(lines)} distinct lines found, need at least 4")

    features = np.array([[np.cos(2 * t), np.sin(2 * t)] for t, _, _ in lines], np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.1)
    _, labels, _ = cv2.kmeans(features, 2, None, criteria, 8, cv2.KMEANS_PP_CENTERS)
    labels = labels.ravel()

    groups = []
    for k in (0, 1):
        group = [ln for ln, lab in zip(lines, labels) if lab == k]
        group.sort(key=lambda ln: -ln[2])
        groups.append(group[:MAX_LINES_PER_GROUP])

    if len(groups[0]) < 2 or len(groups[1]) < 2:
        raise DetectionFailed("could not find two lines in each orientation family")
    return groups


def _intersect(line_a, line_b):
    (ta, ra), (tb, rb) = line_a[:2], line_b[:2]
    a = np.array([[np.sin(ta), -np.cos(ta)], [np.sin(tb), -np.cos(tb)]])
    det = np.linalg.det(a)
    if abs(det) < 1e-6:
        return None
    return np.linalg.solve(a, np.array([ra, rb]))


def quad_from_lines(a1, a2, b1, b2):
    """Corners of the quad bounded by two lines from each orientation family."""
    corners = []
    for pair in ((a1, b1), (a1, b2), (a2, b2), (a2, b1)):
        point = _intersect(*pair)
        if point is None:
            return None
        corners.append(point)
    return np.array(corners)


def score_quad(corners, mat, shape):
    """Agreement between the quad's interior and the playing-surface mask, as F1.

    Precision alone would favour a small quad sitting entirely on clean surface;
    recall alone would favour a huge quad swallowing the mat-coloured venue floor
    beyond the far wall. Balancing them picks out the field itself.

    Edge-contrast scoring was tried first and is unusable here: the true edges are
    broken up by people leaning over the wall, so they score *worse* than lines
    tracing the broadcast overlay's borders.
    """
    filled = np.zeros(shape, np.uint8)
    cv2.fillPoly(filled, [corners.astype(np.int32)], 1)

    interior = filled > 0
    total_mat = float((mat > 0).sum())
    if not interior.any() or total_mat == 0:
        return -1.0

    hit = float((mat[interior] > 0).sum())
    precision = hit / float(interior.sum())
    recall = hit / total_mat
    if precision + recall == 0:
        return 0.0

    # Precision-weighted: plain F1 rewards swallowing the mat-coloured venue floor
    # beyond the far wall, which scores higher than the field itself.
    beta_sq = REGION_BETA ** 2
    region = (1 + beta_sq) * precision * recall / (beta_sq * precision + recall)

    return 0.5 * region + 0.5 * _inside_is_mat(corners, mat, shape)


def _inside_is_mat(corners, mat, shape):
    """Fraction of each edge with playing surface just inside it.

    This is what separates the floor-contact line from the wall's outer edge:
    inside the floor line is grey mat, inside the wall's outer edge is bright wall.
    Using the wall's edge instead would put every position off by 14-22 inches.
    """
    height, width = shape
    centroid = corners.mean(axis=0)
    scores = []

    for i in range(4):
        p, q = corners[i], corners[(i + 1) % 4]
        edge = q - p
        length = float(np.linalg.norm(edge))
        if length < 30:
            return 0.0
        normal = np.array([-edge[1], edge[0]]) / length
        if normal @ (p + 0.5 * edge - centroid) < 0:
            normal = -normal

        ts = np.linspace(0.15, 0.85, EDGE_SAMPLES)[:, None]
        pts = np.round(p + ts * edge - EDGE_PROBE_PX * normal).astype(int)
        ok = (pts[:, 0] >= 0) & (pts[:, 0] < width) & (pts[:, 1] >= 0) & (pts[:, 1] < height)
        if ok.sum() < EDGE_SAMPLES // 2:
            return 0.0
        scores.append(float((mat[pts[ok, 1], pts[ok, 0]] > 0).mean()))

    return float(np.mean(scores))


def _plausible(corners, shape):
    height, width = shape
    pts = corners.astype(np.float32)
    if not cv2.isContourConvex(pts):
        return False
    area = cv2.contourArea(pts)
    frame_area = float(height * width)
    if not MIN_AREA_FRACTION * frame_area <= area <= MAX_AREA_FRACTION * frame_area:
        return False
    if np.any(corners < -0.5 * max(height, width)) or np.any(corners[:, 0] > 1.5 * width) or np.any(corners[:, 1] > 1.5 * height):
        return False
    return True


def detect_field_corners(median_bgr, min_score=0.55):
    """Search for the best field quad. Returns (corners, diagnostics)."""
    mat = mat_mask(median_bgr)
    shape = median_bgr.shape[:2]
    groups = group_by_orientation(candidate_lines(median_bgr))

    best, best_score, evaluated = None, -1.0, 0
    for a1, a2 in itertools.combinations(groups[0], 2):
        for b1, b2 in itertools.combinations(groups[1], 2):
            corners = quad_from_lines(a1, a2, b1, b2)
            if corners is None or not _plausible(corners, shape):
                continue
            evaluated += 1
            score = score_quad(corners, mat, shape)
            if score > best_score:
                best, best_score = corners, score

    if best is None:
        raise DetectionFailed("no geometrically plausible quad among the detected lines")
    if best_score < min_score:
        raise DetectionFailed(f"best quad scored {best_score:.2f}, below the {min_score:.2f} threshold")

    return order_corners(best), {"score": best_score, "quads_evaluated": evaluated, "mat": mat}


def order_corners(corners):
    """Canonical image-space order: origin at lowest x+y, then clockwise.

    Deterministic, but not physically meaningful - it does not establish which
    real field corner is the origin. See `orientation_confirmed`.
    """
    corners = np.array(corners, dtype=np.float64)
    start = int(np.argmin(corners.sum(axis=1)))
    ordered = np.roll(corners, -start, axis=0)
    area = sum(
        ordered[i][0] * ordered[(i + 1) % 4][1] - ordered[(i + 1) % 4][0] * ordered[i][1]
        for i in range(4)
    )
    if area > 0:
        ordered = np.vstack([ordered[0], ordered[1:][::-1]])
    return ordered
