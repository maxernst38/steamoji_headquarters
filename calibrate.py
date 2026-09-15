"""CLI entrypoint for Step 1: field calibration.

Usage:
    python calibrate.py path/to/match_video.mp4 [--frame-index N] [--output calibration/out.json]

By default this drags a quad onto the field; --click uses the older
point-by-point tool, which is still the better option when you want to place
more than four correspondences.
"""
import argparse
import os
import os
import sys

import cv2

CALIBRATION_OUTPUT_DIR = os.path.join("data", "calibrations")

from calibration.background import median_background
from calibration.click_tool import collect_point_correspondences
from calibration.drag_tool import collect_quad_correspondences
from calibration.field import FIELD_SIZE_IN
from calibration.homography import compute_homography, reprojection_error, save_calibration
from calibration.validate import render_birdseye, draw_clicked_points_on_frame, reverse_projection_overlay


def extract_frame(video_path, frame_index):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise SystemExit(f"could not open video: {video_path}")

    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise SystemExit(f"could not read frame {frame_index} from {video_path}")
    return frame


def main():
    parser = argparse.ArgumentParser(description="Calibrate a fixed-camera VEX match video against the field's floor plane.")
    parser.add_argument("video_path")
    parser.add_argument("--frame-index", type=int, default=0, help="frame to calibrate against (default: 0)")
    parser.add_argument("--output", default=None, help="calibration JSON output path (default: calibration/<video_stem>.json)")
    parser.add_argument("--click", action="store_true", help="use the point-by-point click tool instead of dragging a quad")
    parser.add_argument("--scale", type=float, default=1.5, help="initial view magnification for the drag tool (default: 1.5)")
    parser.add_argument(
        "--median-seconds",
        type=float,
        default=0.0,
        help="calibrate against a median over this many seconds from --frame-index, which removes "
             "people and robots from the view (0 = use the single frame)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.video_path):
        raise SystemExit(f"video not found: {args.video_path}")

    output_path = args.output
    if output_path is None:
        stem = os.path.splitext(os.path.basename(args.video_path))[0]
        output_path = os.path.join("calibration", f"{stem}.json")

    if args.median_seconds > 0:
        cap = cv2.VideoCapture(args.video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        cap.release()
        end = args.frame_index + int(round(args.median_seconds * fps))
        print(f"building median over frames {args.frame_index}-{end} to clear people and robots...")
        frame, _ = median_background(args.video_path, args.frame_index, end, step=10)
    else:
        frame = extract_frame(args.video_path, args.frame_index)

    if args.click:
        points = collect_point_correspondences(frame)
    else:
        points = collect_quad_correspondences(frame, scale=args.scale)
        if points is None:
            raise SystemExit("calibration cancelled")
    print(f"\ncollected {len(points)} point correspondences, computing homography...")

    H, inlier_mask = compute_homography(points)
    error = reprojection_error(H, points)
    print(f"reprojection error: mean={error['mean_in']:.2f}in max={error['max_in']:.2f}in")

    for idx, ((pixel, field), err) in enumerate(zip(points, error["per_point_in"])):
        flag = ""
        if inlier_mask is not None and not inlier_mask.ravel()[idx]:
            flag = "  <-- rejected as an outlier, re-check this one"
        print(f"  point {idx}: pixel={pixel} field={field} error={err:.2f}in{flag}")

    data = save_calibration(
        output_path,
        video_path=args.video_path,
        frame_index=args.frame_index,
        points=points,
        H=H,
        error=error,
    )
    print(f"saved calibration to {output_path}")


    # Its own folder rather than a shared scratch directory: these are the
    # artefacts you check a calibration against, not throwaway diagnostics.
    os.makedirs(CALIBRATION_OUTPUT_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(output_path))[0]

    projection = reverse_projection_overlay(frame, H, field_size_in=FIELD_SIZE_IN)
    projection_path = os.path.join(CALIBRATION_OUTPUT_DIR, f"{stem}_projection.png")
    cv2.imwrite(projection_path, projection)
    print(f"saved projected-field overlay to {projection_path}")

    calib_frame_path = os.path.join(CALIBRATION_OUTPUT_DIR, f"{stem}_calibration_frame.png")
    cv2.imwrite(calib_frame_path, frame)
    print(f"saved the frame it was calibrated against to {calib_frame_path}")

    annotated = draw_clicked_points_on_frame(frame, points)
    annotated_path = os.path.join(CALIBRATION_OUTPUT_DIR, f"{stem}_points.png")
    cv2.imwrite(annotated_path, annotated)
    print(f"saved annotated source frame to {annotated_path}")

    birdseye = render_birdseye(frame, H, field_size_in=FIELD_SIZE_IN)
    birdseye_path = os.path.join(CALIBRATION_OUTPUT_DIR, f"{stem}_birdseye.png")
    cv2.imwrite(birdseye_path, birdseye)
    print(f"saved bird's-eye view to {birdseye_path}")
    print(
        f"\ncheck {projection_path} first: the green grid should track the mat and stop at the wall base. "
        "It is the more reliable of the two, since the bird's-eye view smears everything above the floor."
    )


if __name__ == "__main__":
    main()
