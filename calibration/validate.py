"""Render a top-down (bird's-eye) view of the field from the calibrated homography,
with a reference tile grid overlaid, so calibration can be checked visually."""
import cv2
import numpy as np

from calibration.field import FIELD_SIZE_IN, TILE_SIZE_IN

MARGIN_IN = 12.0


def render_birdseye(frame, H_pixel_to_field, px_per_in=4.0, field_size_in=FIELD_SIZE_IN):
    """Warp `frame` into a top-down field view using H (pixel -> field inches),
    then draw a tile-grid overlay. Returns the birdseye BGR image.

    `field_size_in` must match the units the homography maps into, or the field
    will not fit the canvas.
    """
    canvas_size_in = field_size_in + 2 * MARGIN_IN
    canvas_px = int(round(canvas_size_in * px_per_in))

    # field(inches) -> birdseye pixel space, with a margin so out-of-bounds
    # points (e.g. clicked wall points) are still visible.
    scale = np.array(
        [
            [px_per_in, 0, MARGIN_IN * px_per_in],
            [0, px_per_in, MARGIN_IN * px_per_in],
            [0, 0, 1],
        ]
    )
    H_full = scale @ H_pixel_to_field

    birdseye = cv2.warpPerspective(frame, H_full, (canvas_px, canvas_px))

    grid_steps = int(field_size_in / TILE_SIZE_IN) + 1
    for i in range(grid_steps):
        offset_in = i * TILE_SIZE_IN
        p = int(round((offset_in + MARGIN_IN) * px_per_in))
        field_px = int(round(field_size_in * px_per_in))
        margin_px = int(round(MARGIN_IN * px_per_in))
        cv2.line(birdseye, (p, margin_px), (p, margin_px + field_px), (0, 255, 0), 1)
        cv2.line(birdseye, (margin_px, p), (margin_px + field_px, p), (0, 255, 0), 1)

    return birdseye


def draw_clicked_points_on_frame(frame, points):
    """Mark the calibration points on the frame.

    Coordinates may be ints (click tool) or floats (drag tool), so they are
    rounded here rather than relying on the caller's type.
    """
    img = frame.copy()
    for idx, (pixel, _field) in enumerate(points):
        x, y = int(round(pixel[0])), int(round(pixel[1]))
        cv2.drawMarker(img, (x, y), (0, 0, 255), markerType=cv2.MARKER_CROSS, markerSize=14, thickness=2)
        cv2.putText(img, str(idx), (x + 8, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
    return img


def reverse_projection_overlay(frame, H_pixel_to_field, step_in=TILE_SIZE_IN, field_size_in=FIELD_SIZE_IN):
    """Draw the field's grid back onto the original frame using the calibration.

    This is the honest check on a calibration. The bird's-eye render is dominated
    by smear - every pixel above the floor plane stretches away from the camera -
    whereas here the grid is drawn over features you can actually recognise, with
    no resampling. Lines that track the mat's seams and stop at the wall base mean
    the homography is right.
    """
    overlay = frame.copy()
    inverse = np.linalg.inv(H_pixel_to_field)

    def to_pixel(x_in, y_in):
        point = np.array([[[float(x_in), float(y_in)]]])
        return cv2.perspectiveTransform(point, inverse)[0, 0]

    steps = int(round(field_size_in / step_in))
    for i in range(steps + 1):
        offset = i * step_in
        for start, end in (((offset, 0.0), (offset, field_size_in)),
                           ((0.0, offset), (field_size_in, offset))):
            p, q = to_pixel(*start), to_pixel(*end)
            cv2.line(overlay, tuple(np.round(p).astype(int)), tuple(np.round(q).astype(int)),
                     (0, 220, 0), 1, cv2.LINE_AA)

    corners = np.array([to_pixel(0, 0), to_pixel(field_size_in, 0),
                        to_pixel(field_size_in, field_size_in), to_pixel(0, field_size_in)])
    cv2.polylines(overlay, [np.round(corners).astype(np.int32)], True, (0, 0, 255), 2, cv2.LINE_AA)
    for corner, label in zip(corners, [(0, 0), (field_size_in, 0),
                                       (field_size_in, field_size_in), (0, field_size_in)]):
        point = tuple(np.round(corner).astype(int))
        cv2.circle(overlay, point, 5, (0, 255, 255), -1)
        cv2.putText(overlay, f"({label[0]:g},{label[1]:g})", (point[0] + 9, point[1] - 9),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2, cv2.LINE_AA)
    return overlay
