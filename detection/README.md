# detection

Finding robots, following them through a match, and drawing the result.

| file | role |
|---|---|
| `segments.py` | splits a broadcast into matches by detecting camera cuts |
| `motion.py` | background differencing; finds robot-shaped blobs and the ground-contact point |
| `tracker.py` | SAM2 video propagation, chunked; the core of tracking |
| `trajectory.py` | turns per-frame positions into paths, and grades their confidence |
| `render.py` | the static bird's-eye path plot |
| `visualize.py` | the side-by-side validation video |
| `video_writer.py` | H.264 output, because OpenCV here can only write browser-unplayable mp4v |
| `detector.py` | trained robot detector — **not working, off by default** |

## Two things worth knowing before changing anything here

**Motion cannot see a robot that is holding still.** This single fact causes most
tracking failure: a stationary robot is invisible, so it cannot be found and a
drifted track cannot be re-acquired. Per-frame motion finds all four robots in only
about 10 of 22 sampled frames. `motion.py` is therefore only good enough to
*suggest* seeds, never to drive tracking.

**Merged masks are kept, not discarded.** When two robots collide their masks fuse,
but both robots are then in the same place — the position stays roughly right and
only the *identity* is uncertain. Dropping those samples cut tracks into unusable
fragments (longest unbroken run fell to 0.7 seconds). They now stay in the path,
drawn faint, with `usable()` gating position and `confidence()` grading identity.
