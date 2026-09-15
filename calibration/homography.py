"""Compute, evaluate, and persist a pixel -> field-coordinate (inches) homography."""
import json

import cv2
import numpy as np

from calibration.field import FIELD_SIZE_IN, TILE_SIZE_IN


# RANSAC's threshold is in destination units (inches). Hand-clicking an oblique
# view carries several inches of noise, so a tight threshold discards good points.
OUTLIER_THRESHOLD_IN = 12.0


def compute_homography(points):
    """points: list of ((px, py), (field_x_in, field_y_in)).

    Returns (H, inlier_mask) where H maps pixel coordinates -> field coordinates (inches).
    With more than 4 points, RANSAC rejects gross blunders and the final fit is a
    least-squares refit over the surviving inliers.
    """
    pixel_pts = np.array([p[0] for p in points], dtype=np.float64)
    field_pts = np.array([p[1] for p in points], dtype=np.float64)

    if len(points) <= 4:
        H, mask = cv2.findHomography(pixel_pts, field_pts, 0)
        if H is None:
            raise ValueError("Homography computation failed - check that points are not collinear")
        return H, mask

    _, mask = cv2.findHomography(pixel_pts, field_pts, cv2.RANSAC, OUTLIER_THRESHOLD_IN)
    if mask is None:
        raise ValueError("Homography computation failed - check that points are not collinear")

    inliers = mask.ravel().astype(bool)
    if inliers.sum() < 4:
        raise ValueError("Fewer than 4 consistent points - check the clicked coordinates")

    H, _ = cv2.findHomography(pixel_pts[inliers], field_pts[inliers], 0)
    if H is None:
        raise ValueError("Homography computation failed - check that points are not collinear")
    return H, mask


def reprojection_error(H, points):
    """For each (pixel, field) correspondence, map pixel -> field via H and compare
    to the stated field coordinate. Returns per-point distances (inches) plus mean/max.
    """
    pixel_pts = np.array([[p[0]] for p in points], dtype=np.float64)
    field_pts = np.array([p[1] for p in points], dtype=np.float64)

    projected = cv2.perspectiveTransform(pixel_pts, H).reshape(-1, 2)
    distances = np.linalg.norm(projected - field_pts, axis=1)

    return {
        "per_point_in": distances.tolist(),
        "mean_in": float(distances.mean()),
        "max_in": float(distances.max()),
    }


def save_calibration(path, *, video_path, frame_index, points, H, error):
    data = {
        "video_path": str(video_path),
        "frame_index": frame_index,
        "field": {
            "size_in": FIELD_SIZE_IN,
            "tile_size_in": TILE_SIZE_IN,
            "units": "inches",
            "origin": "one field corner, axes along the field edges",
        },
        "points": [
            {"pixel": list(pixel), "field": list(field)} for pixel, field in points
        ],
        "homography_pixel_to_field": H.tolist(),
        "reprojection_error": error,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return data


def load_calibration(path):
    with open(path) as f:
        data = json.load(f)
    data["homography_pixel_to_field"] = np.array(data["homography_pixel_to_field"])
    return data
