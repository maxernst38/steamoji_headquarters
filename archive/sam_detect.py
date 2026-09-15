"""Detect the field quad by combining SAM's region proposal with the mat colour mask.

Neither signal works alone. Colour segmentation leaks across the far wall into the
venue floor beyond, because the two are statistically identical in hue and
saturation. SAM reliably finds the field as an object and stops at it, but returns
the field *including its perimeter walls*, since that is the more natural object
boundary. Intersecting the two keeps SAM's global extent and the mask's local
precision about where the floor actually ends.

Measured against a hand-clicked calibration of the same view: SAM alone IoU 0.53,
intersection 0.65, fitted quad 0.75 at ~30px mean corner error.
"""
import cv2
import numpy as np

from archive.auto_detect import mat_mask, order_corners, DetectionFailed

MODEL_ID = "facebook/sam2.1-hiera-large"
POSITIVE_PROMPTS = 14
CLOSE_KERNEL = 11

# The wall's checkered trim is the one landmark that is always present on a VEX
# field and cannot be confused with the surface's own markings: the trim
# alternates, the floor lines are solid. A local-contrast test separates them
# where a brightness threshold cannot - brightness flags 100% of the floor's white
# markings as wall, this flags 23%.
CHECKER_WINDOW = 11
CHECKER_STD = 36
CHECKER_DILATE = 9
CHECKER_SAT_MAX = 80


def checker_mask(image_bgr):
    """High local contrast plus desaturation: the wall trim, not the floor lines."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    window = (CHECKER_WINDOW, CHECKER_WINDOW)
    mean = cv2.blur(gray, window)
    variance = np.maximum(cv2.blur(gray * gray, window) - mean * mean, 0)
    saturation = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)[:, :, 1]

    mask = ((np.sqrt(variance) > CHECKER_STD) & (saturation < CHECKER_SAT_MAX)).astype(np.uint8)
    if CHECKER_DILATE > 1:
        mask = cv2.dilate(mask, np.ones((CHECKER_DILATE, CHECKER_DILATE), np.uint8))
    return mask


def _prompt_points(mat, shape):
    """Positive prompts on the playing surface, negatives on the surrounding scene.

    Prompts are derived from the colour mask rather than hand-placed, so this stays
    automatic. Negatives sit at the frame's edges, which are reliably crowd, venue
    floor, or broadcast overlay - never field.
    """
    height, width = shape
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mat)
    if count <= 1:
        raise DetectionFailed("no playing-surface-coloured region to prompt from")

    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    ys, xs = np.where(labels == biggest)
    picks = np.linspace(0, len(xs) - 1, POSITIVE_PROMPTS).astype(int)
    positive = np.stack([xs[picks], ys[picks]], axis=1).astype(float)

    border = np.array(
        [
            [8, 8], [width - 8, 8], [8, height - 8], [width - 8, height - 8],
            [width // 2, height - 20], [40, height // 2], [width - 40, 60],
        ],
        dtype=float,
    )
    return positive, border


def sam_field_region(image_bgr, predictor=None):
    """SAM's mask for the field, chosen as the largest plausible returned mask."""
    from sam2.sam2_image_predictor import SAM2ImagePredictor

    if predictor is None:
        predictor = SAM2ImagePredictor.from_pretrained(MODEL_ID, device="cuda")

    mat = mat_mask(image_bgr)
    positive, border = _prompt_points(mat, image_bgr.shape[:2])

    # Sample negatives on trim that is not surface-coloured, so a stray prompt
    # never lands on the floor itself.
    trim = cv2.bitwise_and(checker_mask(image_bgr), cv2.bitwise_not(mat * 255))
    ys, xs = np.where(trim > 0)
    if len(xs) >= 18:
        picks = np.linspace(0, len(xs) - 1, 18).astype(int)
        negative = np.vstack([np.stack([xs[picks], ys[picks]], axis=1).astype(float), border])
    else:
        negative = border

    predictor.set_image(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB))
    masks, _, _ = predictor.predict(
        point_coords=np.vstack([positive, negative]),
        point_labels=np.array([1] * len(positive) + [0] * len(negative)),
        multimask_output=True,
    )

    frame_area = float(image_bgr.shape[0] * image_bgr.shape[1])
    candidates = [(m > 0).astype(np.uint8) for m in masks]
    candidates = [m for m in candidates if 0.04 * frame_area <= m.sum() <= 0.60 * frame_area]
    if not candidates:
        raise DetectionFailed("SAM returned no mask of plausible field size")
    return max(candidates, key=lambda m: int(m.sum())), mat


def field_region(image_bgr, predictor=None):
    """SAM's region intersected with the colour mask, hole-filled."""
    sam, mat = sam_field_region(image_bgr, predictor)

    # Removing the trim from the surface mask is what pulls the far edge back onto
    # the floor: the trim's mid-grey squares otherwise pass the colour test, and
    # they sit exactly where perspective makes each pixel worth the most inches.
    mat = cv2.bitwise_and(mat, cv2.bitwise_not(checker_mask(image_bgr) * 255))

    region = cv2.bitwise_and(sam, mat)
    region = cv2.morphologyEx(region, cv2.MORPH_CLOSE, np.ones((CLOSE_KERNEL, CLOSE_KERNEL), np.uint8))

    count, labels, stats, _ = cv2.connectedComponentsWithStats(region)
    if count <= 1:
        raise DetectionFailed("no region left after intersecting SAM with the colour mask")
    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    region = ((labels == biggest) * 255).astype(np.uint8)

    # Game pieces punch holes in the surface; filling them cannot leak outward.
    contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(region)
    cv2.drawContours(filled, contours, -1, 255, cv2.FILLED)
    return filled


def detect_field_corners(image_bgr, predictor=None):
    """Corners of the field quad. Returns (ordered_corners, diagnostics)."""
    region = field_region(image_bgr, predictor)

    contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise DetectionFailed("no region boundary to fit")
    hull = cv2.convexHull(max(contours, key=cv2.contourArea))

    perimeter = cv2.arcLength(hull, True)
    for epsilon in np.arange(0.005, 0.15, 0.002):
        approx = cv2.approxPolyDP(hull, epsilon * perimeter, True)
        if len(approx) == 4:
            corners = order_corners(approx.reshape(4, 2).astype(float))
            return corners, {"region": region, "epsilon": float(epsilon)}

    raise DetectionFailed("could not reduce the region hull to four corners")
