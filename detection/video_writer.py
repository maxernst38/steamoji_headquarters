"""Write video that a browser will actually play.

OpenCV in this environment can only produce mp4v (MPEG-4 Part 2): its avc1 and
H264 writers fail to open, and there is no system ffmpeg. Chrome and Firefox
refuse mp4v in an HTML5 video element, so a file written that way appears as a
black box in the UI with no error anywhere.

imageio-ffmpeg ships its own ffmpeg binary through pip - no sudo, which matters
because apt here needs an interactive password - and encodes real H.264. The
OpenCV path stays as a fallback so a missing encoder degrades to an unplayable
file rather than a failed run, and the caller is told which was used.
"""
import cv2
import numpy as np


class VideoWriter:
    """Frame sink that prefers H.264 and falls back to OpenCV's mp4v."""

    def __init__(self, path, size, fps, prefer_h264=True):
        self.path = str(path)
        self.width, self.height = int(size[0]), int(size[1])
        self.fps = float(fps)
        self.codec = None
        self._ffmpeg = None
        self._opencv = None

        if prefer_h264 and self._open_h264():
            self.codec = "h264"
        else:
            self._open_opencv()
            self.codec = "mp4v"

    def _open_h264(self):
        try:
            import imageio_ffmpeg
        except ImportError:
            return False
        try:
            # macro_block_size=1 keeps the exact frame size; the default rounds up
            # to a multiple of 16 and would silently letterbox the panels.
            self._ffmpeg = imageio_ffmpeg.write_frames(
                self.path, (self.width, self.height), fps=self.fps, codec="libx264",
                quality=7, macro_block_size=1, ffmpeg_log_level="error",
                output_params=["-pix_fmt", "yuv420p"],
            )
            self._ffmpeg.send(None)
            return True
        except Exception:
            self._ffmpeg = None
            return False

    def _open_opencv(self):
        self._opencv = cv2.VideoWriter(
            self.path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (self.width, self.height)
        )
        if not self._opencv.isOpened():
            raise RuntimeError(f"could not open any video writer for {self.path}")

    def write(self, frame_bgr):
        if frame_bgr.shape[0] != self.height or frame_bgr.shape[1] != self.width:
            frame_bgr = cv2.resize(frame_bgr, (self.width, self.height))
        if self._ffmpeg is not None:
            self._ffmpeg.send(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB).tobytes())
        else:
            self._opencv.write(frame_bgr)

    def close(self):
        if self._ffmpeg is not None:
            self._ffmpeg.close()
            self._ffmpeg = None
        if self._opencv is not None:
            self._opencv.release()
            self._opencv = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def browser_playable(codec):
    return codec == "h264"
