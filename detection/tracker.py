"""Track robots through a match with SAM2 video propagation.

Motion finds robots but loses them the moment they hold still, so it cannot
produce a continuous path on its own. SAM2's video mode carries each object's
identity forward through stationary stretches and brief occlusions, which is what
a trajectory needs. Seeding happens at one good frame and propagates both
directions, so the seed can be chosen for clarity rather than being forced to be
the first frame of the match.
"""
import gc
import os
import shutil
import tempfile

import cv2
import numpy as np

from calibration.field import FIELD_SIZE_IN
from detection.motion import field_mask, ground_point

MODEL_ID = "facebook/sam2.1-hiera-large"
DEFAULT_STRIDE = 3          # ~10fps from 30fps source; ample for path tracing
MIN_MASK_AREA = 250

# SAM2 keeps a memory bank per frame, so its state grows with sequence length even
# when offloaded to CPU. A whole match in one pass needs roughly 15-20GB; chunking
# caps that at a constant cost, at the price of a seam between chunks.
DEFAULT_CHUNK = 150

# Genuine overlap (possible only with the non-overlap constraint off) is measured
# against mask area. Contact is not: when two masks merely abut, the shared band
# is a thin sliver proportional to the contact *length*, so an area-relative
# threshold never fires. Contact therefore uses an absolute pixel count, which is
# exactly zero for masks that are properly apart.
MERGE_OVERLAP_FRACTION = 0.15
MERGE_TOUCH_DILATE = 9
MIN_CONTACT_PIXELS = 40

# A mask suddenly far larger than that track's usual size has swallowed a
# neighbour, even if the overlap test misses it.
OVERSIZE_RATIO = 1.8

# How far a propagated box may sit from the detector's box before the detector is
# believed instead. Below this they are describing the same robot and the mask
# SAM2 already has is the better outline.
REANCHOR_DISTANCE_PX = 90


def extract_frames(video_path, start_frame, end_frame, stride, out_dir, progress=None):
    """Write the frame range to JPEGs, which is what SAM2's video state consumes.

    Returns the source frame index for each extracted position.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"could not open video: {video_path}")

    source_indices = []
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    index = start_frame
    while index < end_frame:
        ok, frame = cap.read()
        if not ok:
            break
        if (index - start_frame) % stride == 0:
            cv2.imwrite(os.path.join(out_dir, f"{len(source_indices):05d}.jpg"), frame,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
            source_indices.append(index)
            if progress and end_frame > start_frame:
                progress(min((index - start_frame) / float(end_frame - start_frame), 1.0))
        index += 1
    cap.release()
    return source_indices


def _nearest_position(source_indices, frame_index):
    return int(np.argmin([abs(i - frame_index) for i in source_indices]))


def _chunk_bounds(count, chunk_size, seed_position):
    """Split positions into chunks, and say which one holds the seed."""
    bounds = [(lo, min(lo + chunk_size, count)) for lo in range(0, count, chunk_size)]
    seed_chunk = next(i for i, (lo, hi) in enumerate(bounds) if lo <= seed_position < hi)
    return bounds, seed_chunk


def _chunk_dir(base_dir, source_indices, lo, hi, parent):
    """A directory of symlinks for one chunk, renumbered from zero.

    SAM2 loads every frame in the directory it is given, so each chunk needs its
    own. Symlinks avoid re-decoding the video for each one.
    """
    path = os.path.join(parent, f"chunk_{lo:06d}")
    os.makedirs(path, exist_ok=True)
    for local, position in enumerate(range(lo, hi)):
        target = os.path.join(base_dir, f"{position:05d}.jpg")
        link = os.path.join(path, f"{local:05d}.jpg")
        if not os.path.exists(link):
            os.symlink(os.path.abspath(target), link)
    return path


def _boxes_from_outlines(outlines_at_frame):
    boxes = {}
    for track_id, contour in outlines_at_frame.items():
        x, y, w, h = cv2.boundingRect(contour)
        boxes[track_id] = (x, y, w, h)
    return boxes


def _run_chunk(predictor, frames_dir, boxes, local_seed, directions, on_frame=None):
    """Propagate one chunk from `boxes` placed at `local_seed`.

    The predictor is passed in and reused; only the per-chunk state is built and
    freed here. Rebuilding the predictor per chunk reloads the weights every time,
    which cost several minutes over a full match and bought nothing.

    Yields (local_position, track_id, mask).
    """
    state = predictor.init_state(video_path=frames_dir, offload_video_to_cpu=True,
                                 offload_state_to_cpu=True)
    try:
        for track_id, (x, y, w, h) in sorted(boxes.items()):
            predictor.add_new_points_or_box(
                inference_state=state, frame_idx=local_seed, obj_id=int(track_id),
                box=np.array([x, y, x + w, y + h], dtype=np.float32),
            )
        for reverse in directions:
            for position, object_ids, mask_logits in predictor.propagate_in_video(
                state, start_frame_idx=local_seed, reverse=reverse
            ):
                if on_frame:
                    on_frame()
                for slot, track_id in enumerate(object_ids):
                    yield position, int(track_id), (mask_logits[slot] > 0).cpu().numpy().squeeze().astype(np.uint8)
    finally:
        del state
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass


def track_robots(video_path, seeds, seed_frame_index, start_frame, end_frame,
                 homography_pixel_to_field, stride=DEFAULT_STRIDE, work_dir=None,
                 field_size_in=FIELD_SIZE_IN, chunk_size=DEFAULT_CHUNK, verbose=True,
                 progress=None, extract_progress=None, detector=None):
    """Propagate the seeded robots across the range, one chunk at a time.

    With a `detector`, each chunk boundary becomes a re-anchoring point: boxes
    inherited from the previous chunk are checked against what the detector sees,
    and corrected where they disagree. Without it, a track that drifts onto the
    wrong object stays wrong for the rest of the match, because propagation has no
    way to notice.

    Returns ({track_id: [sample, ...]}, {frame_index: {track_id: contour}}).
    """
    from sam2.sam2_video_predictor import SAM2VideoPredictor

    temporary = work_dir is None
    root = work_dir or tempfile.mkdtemp(prefix="vex_frames_")
    base_dir = os.path.join(root, "all")
    os.makedirs(base_dir, exist_ok=True)
    predictor = SAM2VideoPredictor.from_pretrained(MODEL_ID, device="cuda")
    # Without this, two tracks can claim the same pixels when robots collide and
    # their masks merge into one blob. The constraint gives each pixel to a single
    # object, so masks cannot fuse - it does not guarantee the *right* object gets
    # the pixel, but it removes the merge failure entirely.
    predictor.non_overlap_masks = True

    try:
        probe = cv2.VideoCapture(video_path)
        background_shape = (int(probe.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                            int(probe.get(cv2.CAP_PROP_FRAME_WIDTH)), 3)
        probe.release()

        source_indices = extract_frames(video_path, start_frame, end_frame, stride, base_dir,
                                        progress=extract_progress)
        if not source_indices:
            raise ValueError("no frames extracted for tracking")
        seed_position = _nearest_position(source_indices, seed_frame_index)
        bounds, seed_chunk = _chunk_bounds(len(source_indices), chunk_size, seed_position)

        tracks, outlines = {}, {}

        def absorb(frame_index, track_id, mask, merged):
            sample, outline = _reduce_mask(mask, frame_index, homography_pixel_to_field, field_size_in)
            if sample is None:
                return
            sample["merged"] = bool(merged)
            tracks.setdefault(track_id, []).append(sample)
            if outline is not None:
                outlines.setdefault(frame_index, {})[track_id] = outline

        playing_area = field_mask(background_shape, homography_pixel_to_field) if detector else None

        def boxes_at(frame_index):
            inherited = _boxes_from_outlines(outlines.get(frame_index, {}))
            if detector is None:
                return inherited
            return _reanchor(detector, video_path, frame_index, inherited, playing_area,
                             expected=len(seeds), verbose=verbose)

        # The seed chunk runs both ways from the motion-derived boxes; every other
        # chunk inherits its boxes from the neighbour already processed, which is
        # what keeps a track's id the same across a seam.
        order = [(seed_chunk, [False, True])]
        order += [(i, [False]) for i in range(seed_chunk + 1, len(bounds))]
        order += [(i, [True]) for i in range(seed_chunk - 1, -1, -1)]

        # Chunks alone are too coarse a progress unit: a short job is a single
        # chunk, which would leave the bar frozen through the longest stage.
        # Frames within a chunk are counted too, weighted by how many passes the
        # chunk makes (the seed chunk runs both directions, so twice the frames).
        total_passes = sum(len(directions) * (bounds[i][1] - bounds[i][0]) for i, directions in order)
        frames_done = [0]

        for done, (index, directions) in enumerate(order):
            lo, hi = bounds[index]
            if index == seed_chunk:
                boxes = {i: seed["box"] for i, seed in enumerate(seeds)}
                local_seed = seed_position - lo
            elif directions == [False]:
                boxes = boxes_at(int(source_indices[lo - 1]))
                local_seed = 0
            else:
                boxes = boxes_at(int(source_indices[hi]))
                local_seed = hi - lo - 1

            if not boxes:
                if verbose:
                    print(f"   chunk {index + 1}/{len(bounds)}: no tracks survived the seam, stopping this direction")
                continue

            if verbose:
                print(f"   chunk {index + 1}/{len(bounds)} (frames "
                      f"{source_indices[lo]}-{source_indices[hi - 1]}), {len(boxes)} tracks")
            chunk_path = _chunk_dir(base_dir, source_indices, lo, hi, root)
            pending = {}

            def tick():
                frames_done[0] += 1
                if progress and total_passes:
                    progress(min(frames_done[0] / total_passes, 1.0))

            for local_position, track_id, mask in _run_chunk(predictor, chunk_path, boxes,
                                                             local_seed, directions, on_frame=tick):
                # Masks for one frame arrive one track at a time, so they are held
                # until the frame is complete and merges can be judged between them.
                group = pending.setdefault(local_position, {})
                group[track_id] = mask
                if len(group) >= len(boxes):
                    _flush_frame(group, lo, local_position, source_indices, absorb)
                    pending.pop(local_position, None)
            for local_position, group in sorted(pending.items()):
                _flush_frame(group, lo, local_position, source_indices, absorb)
            shutil.rmtree(chunk_path, ignore_errors=True)

        if progress:
            progress(1.0)
        for samples in tracks.values():
            samples.sort(key=lambda s: s["frame"])

        # Alliance colour is deliberately not part of this. VEX robots are built
        # from red-anodised parts whatever alliance they are on, so a colour ratio
        # reads nearly every robot as red - see detection/alliance.py.
        _mark_oversized(tracks)
        for samples in tracks.values():
            for sample in samples:
                sample["reliable"] = bool(
                    sample["in_bounds"] and not sample.get("merged") and not sample.get("oversized")
                )
        if verbose:
            flagged = sum(1 for v in tracks.values() for s in v if not s["reliable"])
            total = sum(len(v) for v in tracks.values())
            print(f"   {flagged}/{total} samples flagged unreliable (merged, oversized, or off-field)")
        return tracks, outlines
    finally:
        if temporary:
            shutil.rmtree(root, ignore_errors=True)


def _reduce_mask(mask, frame_index, homography_pixel_to_field, field_size_in):
    """Turn one mask into the position and outline we keep, or (None, None)."""
    area = int(mask.sum())
    if area < MIN_MASK_AREA:
        return None, None

    point = ground_point(mask)
    if point is None:
        return None, None

    projected = cv2.perspectiveTransform(
        np.array([[[point[0], point[1]]]], dtype=np.float64), homography_pixel_to_field
    )[0, 0]

    # Out-of-bounds samples are kept but marked: a robot apparently off the field
    # means the mask grabbed something else, and dropping those hides failures.
    in_bounds = bool(0 <= projected[0] <= field_size_in and 0 <= projected[1] <= field_size_in)
    sample = {
        "frame": frame_index,
        "field": [float(projected[0]), float(projected[1])],
        "pixel": [float(point[0]), float(point[1])],
        "area": area,
        "in_bounds": in_bounds,
    }

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    outline = max(contours, key=cv2.contourArea) if contours else None
    if outline is not None:
        # Kept so tracked runs can be exported as detector training labels. SAM2
        # carries a track through stretches where the robot is stationary, so
        # these boxes cover the still robots that motion alone never sees - which
        # is exactly the gap the detector has to close.
        x, y, w, h = cv2.boundingRect(outline)
        sample["box"] = [int(x), int(y), int(w), int(h)]
    return sample, outline


def _merged_tracks(masks_by_track):
    """Track ids whose masks are covering the same robot in this frame.

    Two robots in contact produce masks that fuse, or - with the non-overlap
    constraint on - masks that abut exactly along a shared edge. Dilating one
    before intersecting catches both cases, since a real gap survives dilation
    and a shared boundary does not.
    """
    merged = set()
    ids = sorted(masks_by_track)
    kernel = np.ones((MERGE_TOUCH_DILATE, MERGE_TOUCH_DILATE), np.uint8)

    for i, first in enumerate(ids):
        for second in ids[i + 1:]:
            a, b = masks_by_track[first], masks_by_track[second]
            area_a, area_b = int(a.sum()), int(b.sum())
            if area_a == 0 or area_b == 0:
                continue
            overlap = int(cv2.bitwise_and(a, b).sum())
            contact = int(cv2.bitwise_and(cv2.dilate(a, kernel), b).sum())
            if (overlap >= MERGE_OVERLAP_FRACTION * min(area_a, area_b)
                    or contact >= MIN_CONTACT_PIXELS):
                merged.add(first)
                merged.add(second)
    return merged


def _flush_frame(masks_by_track, lo, local_position, source_indices, absorb):
    """Reduce one frame's masks together, so merges between tracks are visible."""
    frame_index = int(source_indices[lo + local_position])
    merged = _merged_tracks(masks_by_track)
    for track_id, mask in sorted(masks_by_track.items()):
        absorb(frame_index, track_id, mask, track_id in merged)


def _mark_oversized(tracks):
    """Flag masks far larger than the track's own typical size.

    Scaled per track rather than globally: a robot near the camera covers several
    times the pixels of one at the far wall, so a single absolute threshold would
    flag the near robot constantly and never catch the far one.
    """
    for samples in tracks.values():
        areas = np.array([s["area"] for s in samples], dtype=np.float64)
        if len(areas) < 5:
            for sample in samples:
                sample["oversized"] = False
            continue
        typical = float(np.median(areas))
        for sample in samples:
            sample["oversized"] = bool(typical > 0 and sample["area"] > OVERSIZE_RATIO * typical)


def _reanchor(detector, video_path, frame_index, inherited, playing_area, expected, verbose=True):
    """Correct chunk-boundary boxes against what the detector actually sees.

    Three outcomes per track. A propagated box near a detection is left alone -
    they agree, and SAM2's outline is the better one. A propagated box with no
    detection nearby has drifted onto something that is not a robot, so it is
    replaced by a spare detection if one exists. A track missing entirely is
    revived from a spare detection, which is the recovery that propagation alone
    can never do.
    """
    from detection.detector import match_to_tracks

    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        return inherited

    detections = detector.detect(frame, playing_area)
    if not detections:
        return inherited

    positions = {}
    for track_id, (x, y, w, h) in inherited.items():
        positions[track_id] = (x + w / 2.0, y + h)

    pairs, spare, lost = match_to_tracks(detections, positions, max_distance_px=REANCHOR_DISTANCE_PX)

    corrected = dict(inherited)
    for track_id in lost:
        if spare:
            corrected[track_id] = detections[spare.pop(0)]["box"]
            if verbose:
                print(f"      re-anchored robot {track_id} onto a detection at frame {frame_index}")

    for track_id in range(expected):
        if track_id not in corrected and spare:
            corrected[track_id] = detections[spare.pop(0)]["box"]
            if verbose:
                print(f"      revived lost robot {track_id} at frame {frame_index}")

    return corrected
