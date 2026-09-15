"""Side-by-side validation video: camera view beside the bird's-eye it produces.

Static path plots show where the tracker thinks robots went, but not whether it
was ever holding the right object. Playing the segmentation next to the path as
it accumulates makes both failure modes obvious at a glance - a mask drifting
onto a game piece, or two tracks swapping identity on contact - because the
colours are shared across the two panels.
"""
import cv2
import numpy as np

from calibration.field import FIELD_SIZE_IN
from detection.render import TRACK_COLORS, _to_canvas, blank_field
from detection.trajectory import confidence, usable
from detection.pieces import BAND_COLOURS, read_field
from detection.video_writer import VideoWriter

PX_PER_IN = 4
MARGIN_PX = 40
TRAIL_THICKNESS = 2


def track_color(track_id):
    """Shared between both panels, so a robot is the same colour in each."""
    return TRACK_COLORS[track_id % len(TRACK_COLORS)]


def draw_pieces_overlay(frame, state):
    """Draw each Goal's column and what is stacked on it.

    The column is drawn even when empty, so an empty Goal is visibly empty rather
    than merely absent - and an occluded one is marked, because "hidden" and
    "nothing there" mean very different things to the event timeline.
    """
    for goal in state.get("goals", []):
        x0, y0, x1, y1 = goal["roi"]
        if goal["occluded"]:
            cv2.rectangle(frame, (x0, y0), (x1, y1), (120, 120, 120), 1)
            cv2.putText(frame, "hidden", (x0, y1 + 13), cv2.FONT_HERSHEY_SIMPLEX,
                        0.42, (150, 150, 150), 1, cv2.LINE_AA)
            continue

        shade = (255, 255, 255) if goal["height"] else (110, 110, 110)
        cv2.rectangle(frame, (x0, y0), (x1, y1), shade, 1)
        for index, colour in enumerate(goal["colours"]):
            cv2.circle(frame, (x0 - 8, y1 - 8 - index * 11), 4, BAND_COLOURS[colour], -1)
        if goal["height"]:
            cv2.putText(frame, str(goal["height"]), (x1 + 3, y1),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2, cv2.LINE_AA)
    return frame


def draw_pieces_birdseye(canvas, state, px_per_in=None, margin=None):
    """The nine Goals on the field plan, with what each is holding.

    Positions come from the layout, not from the detection, so a stack sits where
    its Goal actually is rather than where a floor-plane projection put it.
    """
    px_per_in = PX_PER_IN if px_per_in is None else px_per_in
    margin = MARGIN_PX if margin is None else margin
    for goal in state.get("goals", []):
        centre = tuple(_to_canvas([goal["pos"]], px_per_in, margin)[0])
        if goal["occluded"]:
            cv2.circle(canvas, centre, 7, (120, 120, 120), 1, cv2.LINE_AA)
            continue
        cv2.circle(canvas, centre, 5 + 2 * goal["height"], (95, 95, 95), 1, cv2.LINE_AA)
        for index, colour in enumerate(goal["colours"][:8]):
            cv2.circle(canvas, (centre[0], centre[1] - index * 5), 3,
                       BAND_COLOURS[colour], -1, cv2.LINE_AA)
    return canvas


def draw_camera_overlay(frame, outlines, positions, alpha=0.30, suspect=()):
    """Outline each tracked robot and mark the ground point the position comes from.

    Tracks flagged unreliable this frame are drawn dashed-thin and labelled, so the
    moment a merge happens is visible in the video rather than only in the numbers.
    """
    overlay = frame.copy()
    for track_id, contour in sorted(outlines.items()):
        cv2.drawContours(overlay, [contour], -1, track_color(track_id), cv2.FILLED)
    blended = cv2.addWeighted(frame, 1 - alpha, overlay, alpha, 0)

    for track_id, contour in sorted(outlines.items()):
        colour = track_color(track_id)
        flagged = track_id in suspect
        cv2.drawContours(blended, [contour], -1, colour, 1 if flagged else 2)
        label_at = tuple(contour.reshape(-1, 2).min(axis=0))
        label = f"robot {track_id}" + (" uncertain" if flagged else "")
        cv2.putText(blended, label, (label_at[0], max(label_at[1] - 8, 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 2, cv2.LINE_AA)

    for track_id, pixel in sorted(positions.items()):
        cv2.drawMarker(blended, (int(pixel[0]), int(pixel[1])), track_color(track_id),
                       cv2.MARKER_TRIANGLE_UP, 18, 2)
    return blended


def _draw_run(canvas, run, colour):
    """One unbroken stretch, faint where identity is uncertain."""
    if len(run) < 2:
        return
    points = _to_canvas([p for p, _ in run], PX_PER_IN, MARGIN_PX)
    for i in range(len(points) - 1):
        trust = 0.30 + 0.70 * float(run[i][1])
        shade = tuple(int(c * trust) for c in colour)
        cv2.line(canvas, tuple(points[i]), tuple(points[i + 1]), shade,
                 TRAIL_THICKNESS if run[i][1] >= 1.0 else 1, cv2.LINE_AA)


def draw_live_birdseye(trails, current, size_px, field_size_in=FIELD_SIZE_IN, pieces=None):
    """Field with each robot's path so far, its current position, and the stacks."""
    canvas = blank_field(PX_PER_IN, MARGIN_PX, field_size_in)
    if pieces:
        draw_pieces_birdseye(canvas, pieces)

    for track_id, entries in sorted(trails.items()):
        colour = track_color(track_id)
        run = []
        for entry in entries + [None]:
            if entry is None:
                _draw_run(canvas, run, colour)
                run = []
            else:
                run.append(entry)
    for track_id, point in sorted(current.items()):
        centre = tuple(_to_canvas([point], PX_PER_IN, MARGIN_PX)[0])
        cv2.circle(canvas, centre, 7, track_color(track_id), -1, cv2.LINE_AA)
        cv2.circle(canvas, centre, 7, (20, 20, 20), 1, cv2.LINE_AA)

    return cv2.resize(canvas, (size_px, size_px), interpolation=cv2.INTER_AREA)


def render_tracking_video(video_path, tracks, outlines, out_path, start_frame, end_frame,
                          stride, fps=10.0, field_size_in=FIELD_SIZE_IN, progress=None,
                          homography=None, playing_area=None, piece_every=5):
    """Write the camera view and the live bird's-eye side by side.

    Returns the codec actually used - the caller should surface it, because an
    mp4v fallback will not play in a browser.
    """
    samples_by_frame = {}
    for track_id, samples in tracks.items():
        for sample in samples:
            samples_by_frame.setdefault(sample["frame"], {})[track_id] = sample

    frame_indices = sorted(samples_by_frame)
    if not frame_indices:
        raise ValueError("no tracked frames to render")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"could not open video: {video_path}")

    probe = cap.read()[1]
    height, width = probe.shape[:2]
    panel = height
    writer = VideoWriter(out_path, (width + panel, height), fps)

    trails = {}
    piece_state = None
    try:
        for done, frame_index in enumerate(frame_indices):
            if progress and frame_indices:
                progress((done + 1) / len(frame_indices))
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = cap.read()
            if not ok:
                continue

            frame_samples = samples_by_frame[frame_index]
            current, pixels, suspect = {}, {}, set()
            for track_id, sample in frame_samples.items():
                # A merged sample keeps its place in the trail - the robot really is
                # there - but is marked so the uncertainty stays visible. Only an
                # off-field position breaks the trail.
                if usable(sample):
                    trails.setdefault(track_id, []).append((sample["field"], confidence(sample)))
                    current[track_id] = sample["field"]
                    if confidence(sample) < 1.0:
                        suspect.add(track_id)
                else:
                    trails.setdefault(track_id, []).append(None)
                    suspect.add(track_id)
                pixels[track_id] = sample["pixel"]

            # Pieces are re-detected periodically rather than every frame: they only
            # move when a robot moves them, and detection costs more than reusing.
            if homography is not None and playing_area is not None:
                if piece_state is None or done % piece_every == 0:
                    piece_state = read_field(frame, homography)

            left = draw_camera_overlay(frame, outlines.get(frame_index, {}), pixels, suspect=suspect)
            if piece_state:
                left = draw_pieces_overlay(left, piece_state)
            cv2.putText(left, f"frame {frame_index}", (14, 28), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (255, 255, 255), 2, cv2.LINE_AA)
            right = draw_live_birdseye(trails, current, panel, field_size_in,
                                       pieces=piece_state)
            writer.write(np.hstack([left, right]))
    finally:
        writer.close()
        cap.release()

    return writer.codec
