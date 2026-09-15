"""The tracking pipeline as one callable, shared by the CLI and the web UI.

Both front ends go through `run_tracking` so the UI cannot drift from the command
line. Progress is reported per stage rather than as a single number: the stages
differ in duration by more than an order of magnitude, and a single bar would sit
apparently frozen through tracking, which is most of the run.
"""
import json
import os
import time

import cv2

from calibration.background import median_background
from calibration.field import FIELD_SIZE_IN
from calibration.homography import load_calibration
from detection.motion import field_mask, find_seed_frame, GROUND_POINT_BIAS_NOTE
from detection.detector import DEFAULT_WEIGHTS, RobotDetector, available as detector_available
from detection.render import draw_paths
from detection.tracker import track_robots, DEFAULT_CHUNK, DEFAULT_STRIDE
from detection.trajectory import path_segments, summarise
from detection.visualize import render_tracking_video
from storage import calibration_store
from storage import results_store
from storage import seed_store

RESULTS_DIR = results_store.RESULTS_DIR

# Rough share of total runtime, measured on a full match: tracking dominates, and
# weighting the bar by these keeps it moving at a believable rate.
STAGE_WEIGHTS = {
    "background": 0.06,
    "seed": 0.04,
    "extract": 0.10,
    "track": 0.65,
    "render": 0.15,
}
STAGE_LABELS = {
    "background": "Building background",
    "seed": "Finding robots",
    "extract": "Extracting frames",
    "track": "Tracking robots",
    "render": "Rendering video",
}


class ProgressReporter:
    """Turns per-stage fractions into one overall fraction plus a label."""

    def __init__(self, callback=None):
        self.callback = callback
        self.stage = None
        self.fraction = 0.0

    def stage_callback(self, stage):
        def report(fraction):
            self.stage = stage
            self.fraction = max(0.0, min(float(fraction), 1.0))
            if self.callback:
                self.callback(stage, STAGE_LABELS.get(stage, stage), self.overall())
        return report

    def overall(self):
        done = 0.0
        for name, weight in STAGE_WEIGHTS.items():
            if name == self.stage:
                return done + weight * self.fraction
            done += weight
        return done


def calibration_for(video_path, calibration_dir="calibration"):
    """The calibration JSON matching a video, by filename stem."""
    stem = os.path.splitext(os.path.basename(video_path))[0]
    candidate = os.path.join(calibration_dir, f"{stem}.json")
    return candidate if os.path.exists(candidate) else None


class MissingSetup(Exception):
    """A required input has not been provided for this match."""


def run_tracking(video_path, calibration_path, start, end, stride=DEFAULT_STRIDE,
                 robots=4, chunk=DEFAULT_CHUNK, seed_search=2000, background_span=3000,
                 output_prefix=None, make_video=True, progress=None, log=print, reuse=True,
                 manual_seeds=True, use_detector=False):
    """Track a match and produce trajectories, a path plot and a validation video.

    With `reuse`, an identical previous run is returned instead of being redone.
    Returns a dict describing every artefact produced.
    """
    # Both inputs are required, per match. Falling back to an automatic guess for
    # either is what previously let a run proceed on a stale homography or on
    # robots motion had not actually found, with nothing saying so.
    field = calibration_store.load(video_path, start, end)
    if field is None:
        raise MissingSetup(
            f"no field drawn for frames {start}-{end}. Draw it with 'Select field' "
            "in the UI - each match needs its own, because the camera can change "
            "between matches."
        )

    hand_drawn = seed_store.load(video_path, start, end)
    if hand_drawn is None:
        raise MissingSetup(
            f"no robots marked for frames {start}-{end}. Mark them with 'Select robots' "
            "in the UI - motion detection alone misses robots that are standing still."
        )
    key = results_store.key_for(
        video_path, calibration_path, start=start, end=end, stride=stride,
        robots=robots, seed_search=seed_search, background_span=background_span,
        seeds=hand_drawn.get("saved_at"),
        field=field.get("saved_at"),
        detector=os.path.getmtime(DEFAULT_WEIGHTS) if (use_detector and detector_available()) else None,
    )
    if reuse:
        saved = results_store.load(key)
        if saved:
            log(f"reusing the result saved {_age(saved.get('saved_at'))} - "
                "pass reuse=False or --force to process it again")
            if progress:
                progress("done", "Loaded saved result", 1.0)
            return dict(saved, reused=True)

    reporter = ProgressReporter(progress)
    stem = output_prefix or os.path.splitext(os.path.basename(video_path))[0]
    os.makedirs(RESULTS_DIR, exist_ok=True)

    homography = field["homography_pixel_to_field"]

    # The background window is deliberately independent of the tracked range: a
    # median over a short window absorbs any robot that lingers, and those robots
    # then vanish from the difference entirely.
    probe = cv2.VideoCapture(video_path)
    total_frames = int(probe.get(cv2.CAP_PROP_FRAME_COUNT)) or end
    source_fps = probe.get(cv2.CAP_PROP_FPS) or 30.0
    probe.release()

    bg_start, bg_end = start, end
    if bg_end - bg_start < background_span:
        pad = (background_span - (bg_end - bg_start)) // 2
        bg_start = max(0, bg_start - pad)
        bg_end = min(total_frames, bg_end + pad)
        log(f"widening background window to frames {bg_start}-{bg_end}")

    log(f"building median background over frames {bg_start}-{bg_end}...")
    background, bg_frames = median_background(video_path, bg_start, bg_end, step=30,
                                              progress=reporter.stage_callback("background"))
    if len(bg_frames) < 40:
        log(f"warning: only {len(bg_frames)} background samples; stationary robots may be missed")
    playing_area = field_mask(background.shape, homography)

    seed_index = hand_drawn["frame_index"]
    seeds = seed_store.to_seeds(hand_drawn, homography)
    seed_frame = _frame_at(video_path, seed_index)
    log(f"using {len(seeds)} hand-drawn robots from frame {seed_index}")
    reporter.stage_callback("seed")(1.0)

    seed_png = os.path.join(RESULTS_DIR, f"{stem}_seed.png")
    annotated = seed_frame.copy()
    for index, seed in enumerate(seeds):
        x, y, w, h = seed["box"]
        cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 255, 255), 2)
        cv2.putText(annotated, f"robot {index}", (x, y - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 255, 255), 2, cv2.LINE_AA)
    cv2.imwrite(seed_png, annotated)

    detector = None
    if use_detector and detector_available():
        detector = RobotDetector()
        log("detector loaded - tracks will be re-anchored at each chunk boundary")
    elif use_detector:
        log(f"no detector weights at {DEFAULT_WEIGHTS}; tracking without re-anchoring")

    log(f"tracking frames {start}-{end} at stride {stride}...")
    tracks, outlines = track_robots(
        video_path, seeds, seed_index, start, end, homography, stride=stride, chunk_size=chunk,
        detector=detector,
        progress=reporter.stage_callback("track"),
        extract_progress=reporter.stage_callback("extract"),
    )

    stats = summarise(tracks)
    for track_id, summary in stats.items():
        log(f"robot {track_id}: {summary['samples']} samples "
            f"({summary.get('reliable', 0)} reliable), {summary['distance_in']:.0f}in travelled, "
            f"max step {summary['max_step_in']:.1f}in, merged {summary.get('merged', 0)}")

    trajectories_path = os.path.join(RESULTS_DIR, f"{stem}_trajectories.json")
    with open(trajectories_path, "w") as handle:
        json.dump({
            "video": video_path,
            "calibration": calibration_store.calibration_path(video_path, start, end),
            "frame_range": [start, end],
            "stride": stride,
            "seed_frame": seed_index,
            "ground_point_note": GROUND_POINT_BIAS_NOTE,
            "summary": stats,
            "tracks": {str(k): v for k, v in tracks.items()},
        }, handle, indent=2)

    paths_png = os.path.join(RESULTS_DIR, f"{stem}_paths.png")
    canvas = draw_paths({tid: path_segments(s) for tid, s in tracks.items()},
                        title=f"{stem}  frames {start}-{end}")
    cv2.imwrite(paths_png, canvas)

    video_out, codec = None, None
    if make_video:
        log("rendering side-by-side validation video...")
        video_out = os.path.join(RESULTS_DIR, f"{stem}_tracking.mp4")
        codec = render_tracking_video(video_path, tracks, outlines, video_out, start, end, stride,
                                      homography=homography, playing_area=playing_area,
                                      fps=source_fps / float(stride),
                                      progress=reporter.stage_callback("render"))
        if codec != "h264":
            log(f"warning: fell back to {codec}; this will not play in a browser")

    result = {
        "stem": stem,
        "video_path": video_path,
        "calibration": calibration_store.calibration_path(video_path, start, end),
        "frame_range": [start, end],
        "stride": stride,
        "seed_frame": seed_index,
        "robots": len(seeds),
        "seed_source": "manual",
        "summary": stats,
        "trajectories": trajectories_path,
        "paths_png": paths_png,
        "seed_png": seed_png,
        "video": video_out,
        "codec": codec,
    }
    result = results_store.save(key, result)

    if progress:
        progress("done", "Finished", 1.0)
    return dict(result, reused=False)


def _frame_at(video_path, frame_index):
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise ValueError(f"could not read frame {frame_index} from {video_path}")
    return frame


def _age(saved_at):
    if not saved_at:
        return "earlier"
    seconds = max(time.time() - float(saved_at), 0)
    if seconds < 90:
        return "moments ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"
