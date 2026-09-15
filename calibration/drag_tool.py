"""Field calibration by dragging a quad onto the field.

Four corners fully determine the homography, so the 24in tile grid can be
reverse-projected live while dragging. Aligning that grid to the mat's real tile
seams is easier and more accurate than judging four corners in isolation - and it
still works when a corner itself is hidden behind a referee, since the interior
lines tell you whether the fit is right.

Controls:
  drag a corner      move it
  arrow keys         nudge the selected corner one source pixel
  r                  rotate which corner is field origin (0,0)
  f                  flip the corner winding (mirrors the coordinate frame)
  + / -              zoom the view
  g                  cycle grid density (24in tiles / 12in / off)
  s or Enter         save and exit
  q or Esc           cancel
"""
import cv2
import numpy as np

from calibration.field import FIELD_SIZE_IN, TILE_SIZE_IN

WINDOW_NAME = "Field Calibration - drag the quad onto the field"
HANDLE_RADIUS = 9
GRAB_RADIUS = 22
MAGNIFIER_SIZE = 190
MAGNIFIER_ZOOM = 5

QUAD_COLOR = (0, 255, 255)
GRID_COLOR = (0, 220, 0)
ACTIVE_COLOR = (0, 128, 255)
TEXT_COLOR = (255, 255, 255)


class _State:
    def __init__(self, corners, scale):
        self.corners = corners          # source-image coords, clockwise from origin
        self.scale = scale
        self.active = None              # index being dragged or last selected
        self.dragging = False
        self.grid_step = TILE_SIZE_IN
        self.cursor = None


def _default_quad(shape):
    """A centred square, sized so it is easy to grab and drag outward."""
    height, width = shape[:2]
    half = min(height, width) * 0.28
    cx, cy = width / 2.0, height / 2.0
    return np.array(
        [[cx - half, cy - half], [cx + half, cy - half], [cx + half, cy + half], [cx - half, cy + half]],
        dtype=np.float64,
    )


def _field_corners(size_in=FIELD_SIZE_IN):
    return np.array([[0, 0], [size_in, 0], [size_in, size_in], [0, size_in]], dtype=np.float32)


def _field_to_image(corners, size_in=FIELD_SIZE_IN):
    """Homography mapping field inches -> source image pixels, from the four corners."""
    return cv2.getPerspectiveTransform(_field_corners(size_in), corners.astype(np.float32))


def _project(matrix, points):
    pts = np.array(points, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, matrix).reshape(-1, 2)


def _draw_grid(canvas, state, size_in=FIELD_SIZE_IN):
    if state.grid_step <= 0:
        return
    try:
        matrix = _field_to_image(state.corners, size_in)
    except cv2.error:
        return

    steps = int(round(size_in / state.grid_step))
    for i in range(steps + 1):
        offset = i * state.grid_step
        for a, b in (((offset, 0.0), (offset, size_in)), ((0.0, offset), (size_in, offset))):
            p, q = _project(matrix, [a, b]) * state.scale
            # interior lines thinner than the boundary
            thickness = 2 if i in (0, steps) else 1
            cv2.line(canvas, tuple(np.round(p).astype(int)), tuple(np.round(q).astype(int)),
                     GRID_COLOR, thickness, cv2.LINE_AA)


def _draw_magnifier(canvas, frame, state):
    """Zoomed inset around the active corner, so placement can be pixel-accurate."""
    if state.active is None:
        return
    cx, cy = state.corners[state.active]
    half = MAGNIFIER_SIZE // (2 * MAGNIFIER_ZOOM)
    x0, y0 = int(round(cx)) - half, int(round(cy)) - half
    patch = cv2.getRectSubPix(frame, (2 * half, 2 * half), (float(cx), float(cy)))
    inset = cv2.resize(patch, (MAGNIFIER_SIZE, MAGNIFIER_SIZE), interpolation=cv2.INTER_NEAREST)

    centre = MAGNIFIER_SIZE // 2
    cv2.drawMarker(inset, (centre, centre), ACTIVE_COLOR, cv2.MARKER_CROSS, 26, 1)
    cv2.rectangle(inset, (0, 0), (MAGNIFIER_SIZE - 1, MAGNIFIER_SIZE - 1), TEXT_COLOR, 1)

    h, w = canvas.shape[:2]
    canvas[h - MAGNIFIER_SIZE - 8:h - 8, w - MAGNIFIER_SIZE - 8:w - 8] = inset


def _render(frame, state, size_in=FIELD_SIZE_IN):
    canvas = cv2.resize(frame, None, fx=state.scale, fy=state.scale, interpolation=cv2.INTER_LINEAR)
    _draw_grid(canvas, state, size_in)

    labels = [(0, 0), (size_in, 0), (size_in, size_in), (0, size_in)]
    for idx, corner in enumerate(state.corners):
        point = tuple(np.round(corner * state.scale).astype(int))
        colour = ACTIVE_COLOR if idx == state.active else QUAD_COLOR
        cv2.circle(canvas, point, HANDLE_RADIUS, colour, -1)
        cv2.circle(canvas, point, HANDLE_RADIUS, (0, 0, 0), 1)
        cv2.putText(canvas, f"({labels[idx][0]:g},{labels[idx][1]:g})",
                    (point[0] + 12, point[1] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, TEXT_COLOR, 2, cv2.LINE_AA)

    grid_label = "off" if state.grid_step <= 0 else f"{state.grid_step:g}in"
    help_lines = [
        "drag corners onto the field, then align the green grid to the mat's tile seams",
        f"arrows nudge | r rotate origin | f flip | g grid ({grid_label}) | +/- zoom ({state.scale:.2f}x) | s save | q cancel",
    ]
    for i, text in enumerate(help_lines):
        y = canvas.shape[0] - 16 - 30 * (len(help_lines) - 1 - i)
        cv2.putText(canvas, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(canvas, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, TEXT_COLOR, 1, cv2.LINE_AA)

    _draw_magnifier(canvas, frame, state)
    return canvas


def _on_mouse(event, x, y, flags, state):
    source = np.array([x / state.scale, y / state.scale])
    state.cursor = source

    if event == cv2.EVENT_LBUTTONDOWN:
        distances = np.linalg.norm(state.corners - source, axis=1)
        nearest = int(np.argmin(distances))
        if distances[nearest] * state.scale <= GRAB_RADIUS:
            state.active = nearest
            state.dragging = True
    elif event == cv2.EVENT_MOUSEMOVE and state.dragging and state.active is not None:
        state.corners[state.active] = source
    elif event == cv2.EVENT_LBUTTONUP:
        state.dragging = False


def collect_quad_correspondences(frame, scale=1.5, size_in=FIELD_SIZE_IN, initial_corners=None):
    """Drag a quad onto the field. Returns [((px, py), (field_x, field_y)), ...] or None if cancelled."""
    corners = _default_quad(frame.shape) if initial_corners is None else np.array(initial_corners, dtype=np.float64)
    state = _State(corners, scale)

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WINDOW_NAME, _on_mouse, state)
    cv2.resizeWindow(WINDOW_NAME, int(frame.shape[1] * scale), int(frame.shape[0] * scale))

    print(__doc__.split("Controls:")[1])

    while True:
        cv2.imshow(WINDOW_NAME, _render(frame, state, size_in))
        key = cv2.waitKey(16) & 0xFF

        if key in (ord("s"), 13):
            break
        if key in (ord("q"), 27):
            cv2.destroyWindow(WINDOW_NAME)
            return None
        if key == ord("r"):
            state.corners = np.roll(state.corners, -1, axis=0)
            if state.active is not None:
                state.active = (state.active - 1) % 4
        elif key == ord("f"):
            state.corners = state.corners[::-1].copy()
        elif key in (ord("+"), ord("=")):
            state.scale = min(state.scale * 1.15, 4.0)
            cv2.resizeWindow(WINDOW_NAME, int(frame.shape[1] * state.scale), int(frame.shape[0] * state.scale))
        elif key in (ord("-"), ord("_")):
            state.scale = max(state.scale / 1.15, 0.4)
            cv2.resizeWindow(WINDOW_NAME, int(frame.shape[1] * state.scale), int(frame.shape[0] * state.scale))
        elif key == ord("g"):
            state.grid_step = {TILE_SIZE_IN: TILE_SIZE_IN / 2, TILE_SIZE_IN / 2: 0.0, 0.0: TILE_SIZE_IN}[state.grid_step]
        elif state.active is not None and key in (81, 82, 83, 84):
            delta = {81: (-1, 0), 82: (0, -1), 83: (1, 0), 84: (0, 1)}[key]
            state.corners[state.active] += delta

    cv2.destroyWindow(WINDOW_NAME)
    field = [(0.0, 0.0), (size_in, 0.0), (size_in, size_in), (0.0, size_in)]
    return [((float(c[0]), float(c[1])), field[i]) for i, c in enumerate(state.corners)]
