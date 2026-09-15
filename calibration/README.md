# calibration

Mapping pixels to real field inches, so positions mean something.

| file | role |
|---|---|
| `field.py` | VEX field constants: 144in square, 24in tile grid |
| `homography.py` | solves and stores the pixel → field transform |
| `drag_tool.py` | desktop calibrator: drag a quad, with the live tile grid |
| `click_tool.py` | older point-by-point calibrator, still useful for >4 points |
| `validate.py` | bird's-eye warp and the reverse-projection overlay |
| `background.py` | median background over a frame range |

## The homography only describes the floor

It maps one plane. Feed it a point that is *not* on the floor — a robot's centroid,
the top of a lift — and the answer is wrong by 14–22 inches, because the camera ray
through that point meets the floor somewhere behind the robot. Always project the
**ground-contact point**.

## Judge a calibration by the reverse projection, not the bird's-eye view

`reverse_projection_overlay` draws the field grid back onto the original frame, over
features you can recognise. `render_birdseye` warps the image instead, which smears
everything above the floor into long streaks and makes a good calibration look
broken. When they disagree, trust the overlay.

## Calibration is per match, not per video

The broadcast camera changes between matches. A homography scoped to a whole file
is wrong for every match after the first, and nothing downstream reveals it —
positions stay plausible while describing different geometry. See `storage/`.
