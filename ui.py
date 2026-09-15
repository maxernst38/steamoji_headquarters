"""Launch the analysis UI.

Opens on the teams table. The video-first setup and processing flow is still
there at /workspace, reached from a match page or from the nav.

Usage:
    python ui.py                       # open the UI in a browser
    python ui.py --video "match.mp4"   # preselect a video in the workspace
    python ui.py --no-browser          # serve only, e.g. to open from another machine
"""
import argparse
import threading
import webbrowser
from urllib.parse import quote

from webapp import server


def main():
    parser = argparse.ArgumentParser(description="VEX match analysis UI")
    parser.add_argument("--video", default=None, help="preselect this video in the workspace")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    args = parser.parse_args()

    server.PRESELECT_VIDEO = args.video

    # A named video means the workspace, not the teams table: preselection only
    # has meaning in the flow that takes a video.
    url = f"http://{args.host}:{args.port}/"
    if args.video:
        url += f"workspace?video={quote(args.video)}"
    if not args.no_browser:
        # Delayed so the server is accepting connections before the tab loads;
        # opening first shows an error page on a cold start.
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print(f"VEX match analysis UI: {url}")
    if args.video:
        print(f"preselecting {args.video}")
    print("press Ctrl+C to stop\n")

    # threaded=True so progress polling is answered while a job holds a worker.
    server.app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
