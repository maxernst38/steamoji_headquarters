"""CLI for robot tracking and bird's-eye path tracing.

Usage:
    python track.py VIDEO --calibration calibration/<name>.json --start 255 --end 4605

The work lives in pipeline.run_tracking, which the web UI calls too, so the two
front ends cannot drift apart.
"""
import argparse
import os
import sys

from detection.tracker import DEFAULT_CHUNK, DEFAULT_STRIDE
from pipeline import calibration_for, run_tracking


def main():
    parser = argparse.ArgumentParser(description="Track robots and plot their paths on a bird's-eye field.")
    parser.add_argument("video_path")
    parser.add_argument("--calibration", default=None,
                        help="calibration JSON (default: the one matching the video's name)")
    parser.add_argument("--start", type=int, required=True, help="first frame of the match segment")
    parser.add_argument("--end", type=int, required=True, help="last frame of the match segment")
    parser.add_argument("--stride", type=int, default=DEFAULT_STRIDE,
                        help=f"process every Nth frame (default: {DEFAULT_STRIDE}, ~10fps from 30fps source)")
    parser.add_argument("--robots", type=int, default=4, help="robots expected on the field (default: 4)")
    parser.add_argument("--seed-search", type=int, default=2000,
                        help="frames from --start to search for a clean seeding frame (default: 2000)")
    parser.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                        help=f"frames per propagation chunk (default: {DEFAULT_CHUNK}); lower this if "
                             "the process is killed for memory")
    parser.add_argument("--background-span", type=int, default=3000,
                        help="minimum frames the median background is built from (default: 3000)")
    parser.add_argument("--no-video", action="store_true", help="skip the side-by-side validation video")
    parser.add_argument("--force", action="store_true",
                        help="reprocess even if this exact run was already saved")
    parser.add_argument("--output-prefix", default=None,
                        help="output name stem (default: derived from the video filename)")
    args = parser.parse_args()

    if not os.path.exists(args.video_path):
        raise SystemExit(f"video not found: {args.video_path}")

    calibration = args.calibration or calibration_for(args.video_path)
    if not calibration:
        raise SystemExit(
            f"no calibration found for {os.path.basename(args.video_path)} - "
            "run calibrate.py on it first"
        )

    def show(stage, label, fraction):
        sys.stdout.write(f"\r  {label:<22s} {100 * fraction:5.1f}%   ")
        sys.stdout.flush()
        if stage == "done":
            sys.stdout.write("\n")

    result = run_tracking(
        args.video_path, calibration, args.start, args.end, stride=args.stride,
        robots=args.robots, chunk=args.chunk, seed_search=args.seed_search,
        background_span=args.background_span, output_prefix=args.output_prefix,
        make_video=not args.no_video, progress=show, reuse=not args.force,
    )

    if result.get("reused"):
        print(f"\nreused the saved result - pass --force to process it again")
    print(f"trajectories: {result['trajectories']}")
    print(f"bird's-eye paths: {result['paths_png']}")
    if result.get("video"):
        print(f"validation video: {result['video']} ({result['codec']})")


if __name__ == "__main__":
    main()
