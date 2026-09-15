"""Draw robot paths on a bird's-eye view of the field.

This renders the field's geometry directly rather than warping video pixels. A
warped frame smears everything above the floor into long streaks, so it is a poor
backdrop for reading paths - the geometry is what carries the information here.
"""
import cv2
import numpy as np

from calibration.field import FIELD_SIZE_IN, TILE_SIZE_IN

PX_PER_IN = 4
MARGIN_PX = 40

BACKGROUND = (28, 28, 28)
GRID = (70, 70, 70)
BOUNDARY = (200, 200, 200)
TRACK_COLORS = [(80, 220, 80), (80, 160, 255), (240, 200, 60), (200, 120, 255),
                (120, 240, 240), (255, 140, 140)]


def _to_canvas(points, px_per_in=PX_PER_IN, margin=MARGIN_PX):
    pts = np.asarray(points, dtype=np.float64)
    if len(pts) == 0:
        return pts.reshape(0, 2).astype(np.int32)
    return np.round(pts * px_per_in + margin).astype(np.int32)


def blank_field(px_per_in=PX_PER_IN, margin=MARGIN_PX, field_size_in=FIELD_SIZE_IN):
    """Field outline with its 24in tile grid."""
    side = int(round(field_size_in * px_per_in)) + 2 * margin
    canvas = np.full((side, side, 3), BACKGROUND, np.uint8)

    steps = int(round(field_size_in / TILE_SIZE_IN))
    for i in range(steps + 1):
        offset = i * TILE_SIZE_IN
        p0 = _to_canvas([[offset, 0]], px_per_in, margin)[0]
        p1 = _to_canvas([[offset, field_size_in]], px_per_in, margin)[0]
        q0 = _to_canvas([[0, offset]], px_per_in, margin)[0]
        q1 = _to_canvas([[field_size_in, offset]], px_per_in, margin)[0]
        cv2.line(canvas, tuple(p0), tuple(p1), GRID, 1, cv2.LINE_AA)
        cv2.line(canvas, tuple(q0), tuple(q1), GRID, 1, cv2.LINE_AA)

    corners = _to_canvas([[0, 0], [field_size_in, 0], [field_size_in, field_size_in], [0, field_size_in]],
                         px_per_in, margin)
    cv2.polylines(canvas, [corners], True, BOUNDARY, 2, cv2.LINE_AA)

    for label, corner in (("(0,0)", [0, 0]), (f"({field_size_in:g},{field_size_in:g})",
                                              [field_size_in, field_size_in])):
        point = _to_canvas([corner], px_per_in, margin)[0]
        cv2.putText(canvas, label, (point[0] + 6, point[1] - 6), cv2.FONT_HERSHEY_SIMPLEX,
                    0.4, BOUNDARY, 1, cv2.LINE_AA)
    return canvas


def _unpack(segment):
    """Accept (points, confidences) or bare points, so older callers still work."""
    if isinstance(segment, tuple) and len(segment) == 2:
        points = np.asarray(segment[0], dtype=np.float64)
        return points, np.asarray(segment[1], dtype=np.float64)
    points = np.asarray(segment, dtype=np.float64)
    return points, np.ones(len(points))


def draw_paths(tracks_segments, px_per_in=PX_PER_IN, margin=MARGIN_PX, field_size_in=FIELD_SIZE_IN,
               fade=True, title=None):
    """tracks_segments: {track_id: [(Nx2 points, N confidences), ...]}.

    Segments break only where a position is meaningless (off the field). Stretches
    where the robot merged with another stay in the path, drawn dimmed: the
    position is still roughly right there - colliding robots occupy the same spot -
    so only the identity is in doubt, and cutting them left runs too short to read.
    """
    canvas = blank_field(px_per_in, margin, field_size_in)

    for index, (track_id, segments) in enumerate(sorted(tracks_segments.items())):
        colour = TRACK_COLORS[index % len(TRACK_COLORS)]
        if isinstance(segments, np.ndarray):
            segments = [segments]          # a single bare path was passed

        last_end = None
        for segment in segments:
            points, weights = _unpack(segment)
            if len(points) < 2:
                continue
            canvas_pts = _to_canvas(points, px_per_in, margin)

            for i in range(len(canvas_pts) - 1):
                # Direction of travel from brightening along the path, and
                # identity confidence from how solid the line is.
                travel = 0.35 + 0.65 * (i / max(len(canvas_pts) - 2, 1)) if fade else 1.0
                trust = 0.30 + 0.70 * float(weights[i])
                shade = tuple(int(c * travel * trust) for c in colour)
                thickness = 2 if weights[i] >= 1.0 else 1
                cv2.line(canvas, tuple(canvas_pts[i]), tuple(canvas_pts[i + 1]), shade,
                         thickness, cv2.LINE_AA)

            cv2.circle(canvas, tuple(canvas_pts[0]), 6, colour, 1, cv2.LINE_AA)
            last_end = tuple(canvas_pts[-1])

        if last_end is not None:
            cv2.circle(canvas, last_end, 5, colour, -1, cv2.LINE_AA)
            cv2.putText(canvas, f"robot {track_id}", (last_end[0] + 8, last_end[1] - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA)

    legend = "hollow = start, filled = end, brightening = direction, faint = identity uncertain"
    cv2.putText(canvas, legend, (12, canvas.shape[0] - 14), cv2.FONT_HERSHEY_SIMPLEX,
                0.42, (170, 170, 170), 1, cv2.LINE_AA)
    if title:
        cv2.putText(canvas, title, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1, cv2.LINE_AA)
    return canvas
