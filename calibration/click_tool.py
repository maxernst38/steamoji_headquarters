"""Interactive tool to collect pixel <-> field-coordinate point correspondences
by clicking on a displayed frame.

Controls:
  left-click   mark a point, then enter its real-world field coordinate in the
               terminal as "x,y" (inches)
  u            undo the last point
  q / ESC      finish (requires at least 4 points)
"""
import cv2

from calibration.field import FIELD_SIZE_IN

WINDOW_NAME = "Field Calibration - click a point, then enter its field (x,y) in inches"
OUT_OF_BOUNDS_SLACK_IN = 6.0
MARKER_COLOR = (0, 0, 255)
TEXT_COLOR = (0, 255, 0)


class _ClickState:
    def __init__(self):
        self.pending_pixel = None


def _on_mouse(event, x, y, flags, state):
    if event == cv2.EVENT_LBUTTONDOWN:
        state.pending_pixel = (x, y)


def _redraw(base_image, points):
    img = base_image.copy()
    for idx, (pixel, field) in enumerate(points):
        cv2.drawMarker(img, pixel, MARKER_COLOR, markerType=cv2.MARKER_CROSS, markerSize=14, thickness=2)
        label = f"{idx}: field({field[0]:g},{field[1]:g})"
        cv2.putText(img, label, (pixel[0] + 8, pixel[1] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_COLOR, 1, cv2.LINE_AA)
    return img


def collect_point_correspondences(frame, min_points=4):
    """Display `frame` and let the user click reference points, prompting for each
    point's known real-world field coordinate (inches) in the terminal.

    Returns a list of ((pixel_x, pixel_y), (field_x_in, field_y_in)) tuples.
    """
    state = _ClickState()
    points = []

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WINDOW_NAME, _on_mouse, state)

    print(
        "Click a visible field reference point (an outer corner or a tile-grid line "
        "intersection), then type its known field coordinate.\n"
        "Keys: 'u' = undo last point, 'q' / ESC = finish (need >= 4 points).\n"
    )

    cv2.imshow(WINDOW_NAME, frame)
    while True:
        key = cv2.waitKey(20) & 0xFF

        if state.pending_pixel is not None:
            pixel = state.pending_pixel
            state.pending_pixel = None
            cv2.imshow(WINDOW_NAME, _redraw(frame, points))
            cv2.waitKey(1)
            raw = input(f"Point clicked at pixel {pixel}. Enter field coordinate as 'x,y' (inches), or blank to discard: ").strip()
            if raw:
                try:
                    x_str, y_str = raw.split(",")
                    field_point = (float(x_str), float(y_str))
                except ValueError:
                    print("  could not parse, discarding that point")
                else:
                    limit = FIELD_SIZE_IN + OUT_OF_BOUNDS_SLACK_IN
                    if any(not -OUT_OF_BOUNDS_SLACK_IN <= c <= limit for c in field_point):
                        print(
                            f"  !! {field_point} is outside the {FIELD_SIZE_IN:g}in field. "
                            f"Coordinates run 0 to {FIELD_SIZE_IN:g} inches (tile lines every 24in)."
                        )
                        if input("  keep it anyway? [y/N]: ").strip().lower() != "y":
                            print("  discarded")
                            cv2.imshow(WINDOW_NAME, _redraw(frame, points))
                            continue
                    points.append((pixel, field_point))
                    print(f"  recorded point {len(points) - 1}: pixel={pixel} field={field_point}")
            cv2.imshow(WINDOW_NAME, _redraw(frame, points))

        elif key == ord("u"):
            if points:
                removed = points.pop()
                print(f"  undid point: {removed}")
                cv2.imshow(WINDOW_NAME, _redraw(frame, points))

        elif key in (ord("q"), 27):
            if len(points) >= min_points:
                break
            print(f"  need at least {min_points} points, have {len(points)}")

    cv2.destroyWindow(WINDOW_NAME)
    return points
