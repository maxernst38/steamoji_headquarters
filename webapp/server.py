"""Web UI for VEX match analysis.

A browser rather than a desktop window, for three reasons: a native <video
controls> gives correct scrubbing and frame stepping of the validation footage,
which is the whole point of producing it; the stats pages planned next are far
easier in HTML; and it avoids the GUI toolkit problems already hit on this
machine during calibration.

Processing runs on a worker thread and the page polls for progress, so a reload
reattaches to a running job instead of losing it.
"""
import base64
import datetime as _dt
import os

from storage import paths
import threading
import traceback
import uuid
from urllib.parse import quote

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, send_from_directory

from storage import calibration_store
from storage import catalog
from storage import event_details
from storage import regions
from storage import webcasts
from storage import team_media
from webapp import programs
from webapp import videolinks
from analysis import bracket as bracket_module
from analysis import importance as importance_module
from analysis import ratings as ratings_module
from storage import results_store
from storage import seed_store
# Set on the hosted copy, unset locally. See `local_only` below.
#
# Forced on Vercel, which sets VERCEL itself: a serverless deployment has no
# writable disk, so the write routes could not work there even if someone
# deployed without setting the flag. Better to be read-only by construction
# than to depend on a dashboard setting being remembered.
READ_ONLY = (os.environ.get("VEX_READ_ONLY", "").strip().lower() in ("1", "true", "yes")
             or bool(os.environ.get("VERCEL")))

# Everything below belongs to the video workspace, and every one of these
# pulls in OpenCV - about 90MB installed, and useless on a server that holds
# no video. A read-only deployment therefore never imports them, and needs
# neither OpenCV nor NumPy's presence here; the routes that use them are not
# registered either. RESULTS_DIR is spelled out rather than imported so the
# results store still resolves.
RESULTS_DIR = paths.path("results")
if not READ_ONLY:
    import cv2
    import numpy as np
    from calibration.background import median_background
    from calibration.field import FIELD_SIZE_IN
    from calibration.field_layout import resolve_alliance_owners
    from calibration.homography import compute_homography, load_calibration
    from detection.motion import field_mask, find_seed_frame
    from detection.segments import find_segments
    from pipeline import MissingSetup, RESULTS_DIR, run_tracking

# Rows rendered per page. The whole-table-in-the-page approach worked at one
# event and stopped working at a season: 613 teams already produced 323KB of
# HTML and 1367 matches produced 903KB, both growing linearly with the import.
TEAM_ROWS = 200

# Sortable columns on the teams table: key, label, and whether it reads as a
# number. Sorting is applied to the whole filtered set before the row cap -
# sorting only the visible page would answer "the best win rate among the 200
# most-played teams", which is not the question the header implies.
TEAM_COLUMNS = (
    ("number", "Team", False),
    ("name", "Name", False),
    ("organization", "Organization", False),
    ("location", "Location", False),
    ("grade", "Grade", False),
    ("matches", "Matches", True),
    ("wins", "Record", True),
    ("win_rate", "Win rate", True),
    ("ccwm", "CCWM", True),
    ("opr", "OPR", True),
    ("dpr", "DPR", True),
    ("elo", "Elo", True),
    ("channel", "YouTube", False),
    ("with_video", "Footage", True),
)
# The same table under cooperative scoring. Record, win rate and the
# least-squares ratings are all built on beating an opponent, which a Teamwork
# match does not have, so what IQ actually measures takes their place.
TEAM_COLUMNS_COOP = (
    ("number", "Team", False),
    ("name", "Name", False),
    ("organization", "Organization", False),
    ("location", "Location", False),
    ("grade", "Grade", False),
    ("matches", "Matches", True),
    ("avg_score", "Average", True),
    ("high_score", "High score", True),
    ("total_score", "Total", True),
    ("channel", "YouTube", False),
    ("with_video", "Footage", True),
)

TEAM_COLUMN_KEYS = {key for key, _, _ in TEAM_COLUMNS}
DEFAULT_TEAM_SORT = "elo"
DEFAULT_TEAM_SORT_COOP = "avg_score"

# Scouting is local, so the region you compete in is a better starting view than
# every team in the world. "all" is the explicit opt-out rather than an empty
# value, because a blank <select> submits as "" and would be indistinguishable
# from "no region given" - which is what triggers this default.
DEFAULT_REGION = "United States · Pacific Northwest"
ALL_REGIONS = "all"

# High School and Middle School are separate competitions, so a team you might
# face is a team in your own grade. No default is applied: unlike region, there
# is no way to guess which one the user cares about.
GRADES = ("High School", "Middle School", "Blended")
TEAM_NUMBER = __import__("re").compile(r"^(\d*)(.*)$")
MATCH_ROWS = 200
EVENT_ROWS = 200

VIDEO_DIR = "videos"
VIDEO_SUFFIXES = (".mp4", ".mkv", ".mov", ".avi")

# Set by ui.py --video; the page asks for it rather than having it templated in.
PRESELECT_VIDEO = None

app = Flask(__name__, static_folder="static", static_url_path="/static",
            template_folder="templates")

# Without this, Jinja caches every template for the life of the process, so a
# template edit is invisible until the server is restarted - which looks exactly
# like the edit not having worked. The cost is an mtime check per render.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


# A read-only server does not register the routes that change anything, or the
# video workspace - not 403, absent - so there is no surface to defend and no
# password needed in front of the site. Local use keeps every form, because
# adding matches and running the tracker is the workflow this tool exists for.
def local_only(route):
    """Register a route only where this is the working copy, not the server."""
    def decorate(view):
        return view if READ_ONLY else route(view)
    return decorate

# One job at a time: the work is GPU-bound, so queueing more would only hide
# contention behind a longer wait.
_jobs = {}
_jobs_lock = threading.Lock()
_segment_cache = {}


def _list_videos():
    if not os.path.isdir(VIDEO_DIR):
        return []
    names = sorted(n for n in os.listdir(VIDEO_DIR) if n.lower().endswith(VIDEO_SUFFIXES))
    videos = []
    for name in names:
        path = os.path.join(VIDEO_DIR, name)
        # No calibration field here any more: it is per match, so whether a video
        # is usable is only answerable once a match is chosen.
        videos.append({
            "name": name,
            "path": path,
            "size_mb": round(os.path.getsize(path) / 1e6, 1),
        })
    return videos


def _active_job():
    with _jobs_lock:
        for job in _jobs.values():
            if job["state"] == "running":
                return job
    return None


def _result_payload(record):
    """Manifest as the page wants it: media paths become URLs."""
    def url(path):
        return f"/media/{os.path.basename(path)}" if path else None

    return {
        "video_url": url(record.get("video")),
        "paths_url": url(record.get("paths_png")),
        "seed_url": url(record.get("seed_png")),
        "codec": record.get("codec"),
        "robots": record.get("robots"),
        "summary": record.get("summary"),
        "saved_at": record.get("saved_at"),
        "reused": record.get("reused", True),
    }


def _lookup(video_path, calibration, start, end, stride=3):
    saved_seeds = seed_store.load(video_path, start, end)
    field = calibration_store.load(video_path, start, end)
    if not (saved_seeds and field):
        return None
    return results_store.find(video_path, calibration_store.calibration_path(video_path, start, end),
                              start=start, end=end, stride=stride,
                              robots=4, seed_search=2000, background_span=3000,
                              seeds=saved_seeds.get("saved_at"), field=field.get("saved_at"),
                              detector=None)


def _run_job(job_id, video_path, calibration, start, end, stride, prefix):
    def progress(stage, label, fraction):
        with _jobs_lock:
            job = _jobs[job_id]
            job["stage"] = stage
            job["label"] = label
            job["progress"] = round(float(fraction), 4)

    def log(message):
        with _jobs_lock:
            _jobs[job_id]["log"].append(str(message))
            del _jobs[job_id]["log"][:-200]

    try:
        result = run_tracking(video_path, calibration, start, end, stride=stride,
                              output_prefix=prefix, progress=progress, log=log,
                              reuse=_jobs[job_id].get("reuse", True))
        with _jobs_lock:
            job = _jobs[job_id]
            job["state"] = "done"
            job["progress"] = 1.0
            job["label"] = "Finished"
            job["result"] = _result_payload(result)
    except Exception as error:                     # surfaced in the UI, not swallowed
        with _jobs_lock:
            job = _jobs[job_id]
            job["state"] = "error"
            job["error"] = f"{type(error).__name__}: {error}"
        traceback.print_exc()


@local_only(app.route("/workspace"))
def workspace():
    """The original video-first flow: pick a video, pick a segment, set up, run.

    Kept reachable while the match page grows into its replacement, because it is
    the only way to process a segment that no match record points at yet.
    """
    return send_from_directory(app.static_folder, "index.html")


@local_only(app.route("/api/config"))
def api_config():
    return jsonify({"preselect": PRESELECT_VIDEO})


@local_only(app.route("/api/videos"))
def api_videos():
    return jsonify({"videos": _list_videos()})


@local_only(app.route("/api/segments"))
def api_segments():
    """Detected matches within a video, so the user never types frame numbers."""
    name = request.args.get("video", "")
    path = os.path.join(VIDEO_DIR, os.path.basename(name))
    if not os.path.exists(path):
        return jsonify({"error": "video not found"}), 404

    if path not in _segment_cache:
        _segment_cache[path] = find_segments(path)
    return jsonify({"segments": _segment_cache[path]})


@local_only(app.route("/api/results"))
def api_results():
    """The saved result for a match, or null - so the page can offer it instead
    of spending fifteen minutes recomputing something already done."""
    name = os.path.basename(request.args.get("video", ""))
    path = os.path.join(VIDEO_DIR, name)
    if not os.path.exists(path):
        return jsonify({"result": None})

    try:
        start, end = int(request.args["start"]), int(request.args["end"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "start and end are required"}), 400

    record = _lookup(path, None, start, end)
    return jsonify({"result": _result_payload(record) if record else None})


@local_only(app.route("/api/setup"))
def api_setup():
    """Whether this match has its field drawn and its robots marked.

    Both are prerequisites, and the page shows them as red or green from this,
    so the state always reflects what is actually on disk rather than what the
    browser happens to remember.
    """
    name = os.path.basename(request.args.get("video", ""))
    path = os.path.join(VIDEO_DIR, name)
    try:
        start, end = int(request.args["start"]), int(request.args["end"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "start and end are required"}), 400

    field = calibration_store.load(path, start, end)
    seeds = seed_store.load(path, start, end)
    return jsonify({
        "field": bool(field),
        "field_saved_at": (field or {}).get("saved_at"),
        "robots": bool(seeds),
        "robot_count": len((seeds or {}).get("boxes", [])),
        "ready": bool(field and seeds),
    })


@local_only(app.route("/api/field"))
def api_field():
    """A frame to draw the field quad on, plus whatever was drawn before."""
    name = os.path.basename(request.args.get("video", ""))
    path = os.path.join(VIDEO_DIR, name)
    if not os.path.exists(path):
        return jsonify({"error": "video not found"}), 404
    try:
        start, end = int(request.args["start"]), int(request.args["end"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "start and end are required"}), 400

    saved = calibration_store.load(path, start, end)
    # A frame a little way in, so the field is set up and the pre-match graphics
    # have cleared.
    frame_index = saved["frame_index"] if saved else min(start + 300, end - 1)
    image = _frame_jpeg(path, frame_index)
    if image is None:
        return jsonify({"error": "could not read that frame"}), 500

    return jsonify({
        "frame_index": frame_index,
        "corners": saved["corners"] if saved else None,
        "field_size_in": FIELD_SIZE_IN,
        "image": "data:image/jpeg;base64," + image,
    })


@local_only(app.route("/api/field", methods=["POST"]))
def api_save_field():
    payload = request.get_json(silent=True) or {}
    name = os.path.basename(payload.get("video", ""))
    path = os.path.join(VIDEO_DIR, name)
    if not os.path.exists(path):
        return jsonify({"error": "video not found"}), 404
    try:
        start, end = int(payload["start"]), int(payload["end"])
        frame_index = int(payload["frame_index"])
        corners = [(float(x), float(y)) for x, y in payload["corners"]]
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "start, end, frame_index and corners are required"}), 400
    if len(corners) != 4:
        return jsonify({"error": "exactly four corners are required"}), 400

    field = [(0.0, 0.0), (FIELD_SIZE_IN, 0.0), (FIELD_SIZE_IN, FIELD_SIZE_IN), (0.0, FIELD_SIZE_IN)]
    try:
        homography, _ = compute_homography(list(zip(corners, field)))
    except ValueError as error:
        return jsonify({"error": f"those corners do not form a usable field: {error}"}), 400

    record = calibration_store.save(path, start, end, frame_index, corners, homography)

    # Cross-check the orientation the user set against the frame itself. Aligning
    # the red rings to the red goals is the authoritative act; this only warns when
    # the picture disagrees, because a wrong orientation credits every point to the
    # opposing alliance and nothing downstream would notice.
    warning = None
    frame = _frame_bgr(path, frame_index)
    if frame is not None:
        owners = resolve_alliance_owners(frame, np.asarray(homography))
        if owners and owners.get("pair_a") != "red":
            warning = ("the frame looks like the RED goals are where the blue rings are - "
                       "press Rotate origin twice if the colours do not match the field")
    return jsonify({"saved": True, "warning": warning})


@local_only(app.route("/api/seed-frame"))
def api_seed_frame():
    """A frame to draw robot boxes on, with the automatic guess as a starting point.

    Returns any hand-drawn boxes already saved, otherwise what motion detection
    found - so the usual interaction is correcting a few boxes rather than drawing
    four from scratch.
    """
    name = os.path.basename(request.args.get("video", ""))
    path = os.path.join(VIDEO_DIR, name)
    if not os.path.exists(path):
        return jsonify({"error": "video not found"}), 404
    try:
        start, end = int(request.args["start"]), int(request.args["end"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "start and end are required"}), 400

    field = calibration_store.load(path, start, end)
    if field is None:
        return jsonify({"error": "draw the field first - the suggestion needs it"}), 400

    saved = seed_store.load(path, start, end)
    if saved:
        frame_index, boxes, source = saved["frame_index"], saved["boxes"], "manual"
    else:
        homography = field["homography_pixel_to_field"]
        background, _ = median_background(path, start, min(end, start + 3000), step=30)
        frame_index, _, seeds = find_seed_frame(
            path, background, field_mask(background.shape, homography), homography,
            start, min(start + 2000, end), step=30, expected=4,
        )
        boxes = [list(seed["box"]) for seed in seeds]
        source = "motion"

    image = _frame_jpeg(path, frame_index)
    if image is None:
        return jsonify({"error": "could not read that frame"}), 500
    return jsonify({
        "frame_index": frame_index,
        "boxes": boxes,
        "source": source,
        "image": "data:image/jpeg;base64," + image,
    })


@local_only(app.route("/api/seeds", methods=["POST"]))
def api_save_seeds():
    payload = request.get_json(silent=True) or {}
    name = os.path.basename(payload.get("video", ""))
    path = os.path.join(VIDEO_DIR, name)
    if not os.path.exists(path):
        return jsonify({"error": "video not found"}), 404
    try:
        start, end = int(payload["start"]), int(payload["end"])
        frame_index = int(payload["frame_index"])
        boxes = [[float(v) for v in box] for box in payload["boxes"]]
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "start, end, frame_index and boxes are required"}), 400
    if not boxes:
        return jsonify({"error": "at least one robot box is required"}), 400

    record = seed_store.save(path, start, end, frame_index, boxes)
    return jsonify({"saved": True, "boxes": record["boxes"]})


def _frame_bgr(video_path, frame_index):
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _frame_jpeg(video_path, frame_index):
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        return None
    ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return base64.b64encode(buffer).decode() if ok else None


@local_only(app.route("/api/jobs", methods=["POST"]))
def api_start_job():
    payload = request.get_json(force=True) or {}
    name = os.path.basename(payload.get("video", ""))
    path = os.path.join(VIDEO_DIR, name)

    if not os.path.exists(path):
        return jsonify({"error": "video not found"}), 400

    running = _active_job()
    if running:
        return jsonify({"error": "a job is already running", "job_id": running["id"]}), 409

    try:
        start = int(payload["start"])
        end = int(payload["end"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "start and end frames are required"}), 400
    if end <= start:
        return jsonify({"error": "end must be after start"}), 400

    # Both prerequisites are checked before a job starts, so a missing one is an
    # immediate message rather than a failure fifteen minutes in.
    if not calibration_store.exists(path, start, end):
        return jsonify({"error": "draw the field for this match first (Select field)"}), 400
    if not seed_store.load(path, start, end):
        return jsonify({"error": "mark the robots for this match first (Select robots)"}), 400
    calibration = calibration_store.calibration_path(path, start, end)

    force = bool(payload.get("force"))
    if not force:
        existing = _lookup(path, calibration, start, end, int(payload.get("stride", 3)))
        if existing:
            # Nothing to run: hand back the saved result rather than starting a
            # job that would only overwrite identical files.
            return jsonify({"cached": True, "result": _result_payload(existing)})

    job_id = uuid.uuid4().hex[:12]
    prefix = f"{os.path.splitext(name)[0]}_f{start}-{end}"
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "state": "running", "stage": "background",
            "label": "Starting", "progress": 0.0, "log": [],
            "video": name, "start": start, "end": end, "result": None, "error": None,
            "reuse": not force,
        }

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, path, calibration, start, end, int(payload.get("stride", 3)), prefix),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id})


@local_only(app.route("/api/jobs/<job_id>"))
def api_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        return jsonify({k: v for k, v in job.items() if k != "log"} | {"log": job["log"][-12:]})


@local_only(app.route("/api/jobs"))
def api_jobs():
    """Lets a reloaded page find the job that is still running."""
    with _jobs_lock:
        return jsonify({"jobs": [
            {k: v for k, v in job.items() if k not in ("log", "result")} for job in _jobs.values()
        ]})


@local_only(app.route("/media/<path:name>"))
def media(name):
    leaf = os.path.basename(name)
    for directory in (RESULTS_DIR,):
        path = os.path.abspath(os.path.join(directory, leaf))
        if path.startswith(os.path.abspath(directory)) and os.path.exists(path):
            break
    else:
        return jsonify({"error": "not found"}), 404
    # conditional=True enables range requests; without it the browser cannot seek
    # within the video and the scrubber silently does nothing.
    return send_file(path, conditional=True)



# ---------------------------------------------------------------- team-centric pages
#
# Server-rendered rather than another single-page app. These are documents - a
# table, a team's season, a match - and they benefit from being linkable and
# from working before any JavaScript runs. The genuinely interactive parts (the
# field quad and the robot boxes) stay as canvas widgets on the pages that need
# them.


def _match_status(match):
    """Whether a match has footage, its setup done, and a finished run.

    Everything is answered from disk rather than from the match record, because
    the artefacts are keyed by (video, frame range) and can be created or removed
    by the older workspace flow without the catalog hearing about it.
    """
    blank = {"video": False, "field": False, "robots": False, "ready": False,
             "analysed": False, "path": None, "result": None}
    segment = catalog.segment_of(match)
    if not segment:
        return blank

    name, start, end = segment
    path = os.path.join(VIDEO_DIR, os.path.basename(name))
    if not os.path.exists(path):
        return blank

    field = calibration_store.exists(path, start, end)
    robots = bool(seed_store.load(path, start, end))
    result = _lookup(path, None, start, end) if (field and robots) else None
    return {"video": True, "field": field, "robots": robots,
            "ready": bool(field and robots), "analysed": bool(result),
            "path": path, "result": result}


# --- programs -------------------------------------------------------------
#
# V5RC and VIQRC share these pages entirely; the program decides which records
# they are drawn from and which palette they are drawn in. Every read of the
# catalog goes through the three helpers below rather than through `catalog`
# directly, so a page cannot accidentally show one program's data inside the
# other's colours.


def _program():
    """The program this request is looking at."""
    return programs.get(programs.current(request))


@app.context_processor
def _program_context():
    """`program`, `programs` and `here` for every template.

    `tab` is defaulted here and overridden by whatever a view passes, so only
    the Learn and Practice views have to name their tab.
    """
    chosen = _program()
    code = chosen["code"]
    # Three cached reads: the catalog is parsed once per file change, so this
    # costs a stat per table rather than a parse.
    empty = not (_teams(code) or _events(code) or _matches(code))
    return {"program": chosen, "programs": programs.ordered(), "tab": "scout",
            "read_only": READ_ONLY,
            "program_empty": empty, "here": request.full_path.rstrip("?") or "/",
            "asset_version": _asset_version()}


def _asset_version():
    """The stylesheet's mtime, appended to its URL so an edit is never cached.

    Flask serves static files with `no-cache`, which asks the browser to
    revalidate - but a page restored from the back/forward cache, or a reload
    that hits the memory cache, can still use an old copy. A changing URL
    cannot be reused by mistake.
    """
    try:
        return int(os.stat(os.path.join(app.static_folder, "style.css")).st_mtime)
    except OSError:
        return 0


@app.route("/program/<code>")
def page_program(code):
    """Switch program and return to the page the switch was pressed on."""
    chosen = programs.normalise(code)
    if not chosen:
        abort(404)
    # Only same-site paths: `next` comes off a URL, so an absolute one would
    # turn this route into an open redirect.
    target = request.args.get("next") or "/"
    if not target.startswith("/") or target.startswith("//"):
        target = "/"
    response = redirect(target)
    response.set_cookie(programs.COOKIE, chosen, max_age=programs.COOKIE_MAX_AGE,
                        samesite="Lax")
    return response


def _dir(code=None):
    """The catalog directory for this program.

    A directory rather than a filter over one shared table: team numbers are
    reused across programs, so a single table keyed by number would merge an IQ
    team into the V5 team that happens to share its number. The `program` field
    on each record stays as a cross-check, not as the thing keeping them apart.
    """
    return programs.catalog_dir(code or _program()["code"])


def _scoring(code=None):
    """ALLIANCE_SCORING or COOPERATIVE_SCORING for the current program."""
    return programs.scoring(code or _program()["code"])


def _team_columns(code=None):
    """(columns, default sort, valid keys) for the program's teams table."""
    if _scoring(code) == catalog.COOPERATIVE_SCORING:
        return TEAM_COLUMNS_COOP, DEFAULT_TEAM_SORT_COOP, {k for k, _, _ in TEAM_COLUMNS_COOP}
    return TEAM_COLUMNS, DEFAULT_TEAM_SORT, TEAM_COLUMN_KEYS


def _events(code=None):
    return catalog.list_events(_dir(code))


def _matches(code=None):
    return catalog.list_matches(_dir(code))


def _teams(code=None):
    return catalog.list_teams(_dir(code))


def _team_index(code=None):
    return catalog.team_index(_dir(code), _scoring(code))


# Ratings are derived, not stored, and cost ~80ms over the whole catalog. Cached
# on the match file's own mtime so a page load does not re-solve 42 least-squares
# systems, and an import invalidates it without anything having to say so.
_ratings_cache = {}


def _ratings(code=None):
    """({event: {team: opr/dpr/ccwm}}, {team: elo}) for one program.

    Solved per program, not once for the catalog: Elo is a single sequential
    pass over every scored match, so pooling two programs would rate a V5RC
    team on VIQRC results if a team number appeared in both.
    """
    code = code or _program()["code"]
    # Nothing to solve under cooperative scoring: OPR separates a team from its
    # partners by who they beat, and Elo moves on a result. Both would return
    # numbers, and all of them would be meaningless.
    if _scoring(code) == catalog.COOPERATIVE_SCORING:
        return {}, {}
    path = os.path.join(_dir(code), catalog.MATCHES_FILE)
    try:
        stat = os.stat(path)
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    cached = _ratings_cache.get(code)
    if not cached or cached[0] != stamp:
        events, matches = _events(code), _matches(code)
        cached = (stamp, (ratings_module.by_event(matches, events),
                          ratings_module.elo(matches, events)))
        _ratings_cache[code] = cached
    return cached[1]


def _attach_ratings(rows):
    """Season averages of each team's per-event ratings, plus their Elo.

    Averaged across a team's events rather than taking their best: OPR rests on
    about five matches per event, so a best-of is mostly a measure of which
    event was noisiest in their favour. The event count travels with it so a
    single-event average is not read as a season judgement.
    """
    per_event, elo = _ratings()
    for row in rows:
        number = row["number"]
        mine = [r[number] for r in per_event.values() if number in r]
        for field in ("opr", "dpr", "ccwm"):
            row[field] = round(sum(m[field] for m in mine) / len(mine), 1) if mine else None
        row["rated_events"] = len(mine)
        entry = elo.get(number) or {}
        row["elo"] = entry.get("elo")
        row["elo_pool"] = entry.get("pool")
    return rows


def _attach_channels(rows):
    """Each team's YouTube channel, read once for the whole table.

    The title is stored flat as `channel` rather than the record itself, so the
    column sorts as text - teams with a channel first, A-Z, and the rest last,
    which is the same "unknown goes to the bottom" rule every other column
    follows. A team with only a suggestion is marked as such rather than shown
    as having one, because a suggestion is a guess nobody has confirmed.
    """
    table = team_media.load_all(programs.media_file(_program()["code"]))
    for row in rows:
        record = table.get(str(row["number"]).upper()) or {}
        channel = record.get("channel") or {}
        row["channel"] = channel.get("title")
        row["channel_url"] = channel.get("url")
        row["channel_reason"] = channel.get("reason")
        row["channel_club"] = channel.get("kind") == "organization"
        row["channel_confirmed"] = channel.get("source") == "manual"
        row["channel_suggested"] = 0 if channel else len(record.get("channel_suggestions") or [])
    return rows


def _location_label(location):
    """"City, Region" where a region exists, "City, Country" where it does not.

    Region is null for much of the world - the imported teams include Chinese
    Taipei and New Zealand entries with a city and no region - so falling back
    to country keeps the column meaningful instead of showing a bare city.
    """
    location = location or {}
    city = location.get("city")
    tail = location.get("region") or location.get("country")
    if city and tail:
        return f"{city}, {tail}"
    return city or tail or ""


def _number_key(number):
    """Team numbers sort as a number then a letter: 10B, 1000A, 10004N.

    Plain string ordering puts 10004N before 1000A, which is wrong for the one
    column people scan to find a specific team.
    """
    digits, letters = TEAM_NUMBER.match(str(number or "")).groups()
    return (int(digits) if digits else 0, letters.upper())


def _sorted_teams(rows, sort, descending):
    """Sort the full set, keeping unknown values last in both directions.

    A team with no record has `None` for win rate. Letting that sort as a low
    value would fill the bottom of the table with teams that never played, and
    reversing would put them at the top - either way crowding out the teams the
    column was meant to rank.

    Ties break on matches played, so a 2-0 team does not outrank a 14-0 one on
    a 100% win rate. Done as successive stable sorts, least significant first,
    because `reverse=True` would otherwise flip the tie-breakers too.
    """
    def value(row):
        return row.get(sort)

    known = [r for r in rows if value(r) not in (None, "")]
    unknown = [r for r in rows if value(r) in (None, "")]

    known.sort(key=lambda r: _number_key(r["number"]))
    if sort != "matches":
        known.sort(key=lambda r: r.get("matches") or 0, reverse=True)
    if sort == "number":
        known.sort(key=lambda r: _number_key(r["number"]), reverse=descending)
    else:
        known.sort(key=value, reverse=descending)

    unknown.sort(key=lambda r: _number_key(r["number"]))
    return known + unknown


def _match_when(match, event):
    """When a match was played: exact from the schedule, approximate from the event.

    `scheduled` carries the event's own local time and is kept as written rather
    than converted - a match at 10:36 happened at 10:36 where it was played, and
    shifting it into this machine's timezone would misreport it.

    Only 85% of matches have one. The rest fall back to the event's dates, which
    is genuinely approximate: 169 events run more than one day, so an event start
    date can be a day or two off the match. That is flagged rather than dressed
    up as a real timestamp.
    """
    scheduled = str(match.get("scheduled") or "")
    if len(scheduled) >= 16 and scheduled[10] == "T":
        return {"date": scheduled[:10], "time": scheduled[11:16], "exact": True}

    event = event or {}
    start, end = event.get("start"), event.get("end")
    if not start:
        return {"date": None, "time": None, "exact": False}
    span = start if (not end or end == start) else f"{start} – {end}"
    return {"date": span, "time": None, "exact": False}


def _team_rows(number, matches):
    """One row per match from this team's point of view."""
    events = _events()
    rows = []
    # A cooperative match is reported as two one-team alliances, so the team
    # opposite is the one played *with*: it belongs under partners, and there
    # is no score against.
    coop = _scoring() == catalog.COOPERATIVE_SCORING
    for match in matches:
        side = catalog.alliance_of(match, number)
        other = "blue" if side == "red" else "red"
        alliances = match.get("alliances", {})
        mine = alliances.get(side, {}) if side else {}
        theirs = alliances.get(other, {}) if side else {}
        beside = [t for t in mine.get("teams", []) if t != catalog.team_key(number)]
        rows.append({
            "match": match,
            "side": None if coop else side,
            "partners": list(theirs.get("teams", [])) if coop else beside,
            "opponents": [] if coop else list(theirs.get("teams", [])),
            "score": mine.get("score"),
            "against": None if coop else theirs.get("score"),
            "outcome": catalog.outcome(match, number, _scoring()),
            "footage": _footage(match),
            "status": _match_status(match),
            "when": _match_when(match, events.get(str(match.get("event") or "").upper())),
        })
    return rows


def _org_key(name):
    """Organizations compared ignoring case and spacing.

    40 organizations are registered under more than one capitalization
    ("CROWN POINT HIGH SCHOOL" / "Crown Point High School"). Compared exactly,
    the filter would split one club's teams across two options.
    """
    return " ".join(str(name or "").split()).lower()


def _organization_options(rows):
    """[(label, team count)] for the rows given, busiest first.

    Each group is labelled with its most common spelling, ties going to the
    alphabetically first, so the label is stable between page loads.
    """
    spellings = {}
    for row in rows:
        name = " ".join(str(row.get("organization") or "").split())
        if name:
            group = spellings.setdefault(_org_key(name), {})
            group[name] = group.get(name, 0) + 1
    options = []
    for group in spellings.values():
        label = sorted(group.items(), key=lambda item: (-item[1], item[0]))[0][0]
        options.append((label, sum(group.values())))
    return sorted(options, key=lambda item: (-item[1], item[0].lower()))


@app.route("/")
def page_teams():
    rows = _attach_channels(_attach_ratings(_team_index()))
    total = len(rows)
    query = (request.args.get("q") or "").strip()
    if query:
        # Searched server-side, not just filtered in the page: at season scale
        # the full table is far too large to ship to the browser, so the
        # in-page filter now only refines what the server already narrowed.
        needle = query.lower()
        rows = [r for r in rows
                if needle in " ".join(str(r.get(f) or "")
                                      for f in ("number", "name", "organization")).lower()]
    # Grouped before the location dict is flattened to a label, and counted over
    # everything the search matched so the menu shows what is actually there.
    for row in rows:
        row["region_group"] = regions.group_of(row.get("location"))
        row["location"] = _location_label(row.get("location"))
    region_counts = {}
    for row in rows:
        region_counts[row["region_group"]] = region_counts.get(row["region_group"], 0) + 1
    region_counts = dict(sorted(region_counts.items(), key=lambda i: regions.sort_key(i[0])))

    grade_counts = {}
    for row in rows:
        if row.get("grade"):
            grade_counts[row["grade"]] = grade_counts.get(row["grade"], 0) + 1
    grade_counts = dict(sorted(grade_counts.items()))
    grade = request.args.get("grade") or ""
    if grade:
        rows = [r for r in rows if r.get("grade") == grade]
        region_counts = {}
        for row in rows:
            region_counts[row["region_group"]] = region_counts.get(row["region_group"], 0) + 1
        region_counts = dict(sorted(region_counts.items(), key=lambda i: regions.sort_key(i[0])))

    requested = request.args.get("region")
    # The default narrows browsing, never an explicit search: someone who types
    # a team number wants that team, not "that team if it happens to be local".
    # Searching "exothermic" under the default silently dropped their Indiana
    # team, which is the kind of omission nothing on the page would explain.
    defaulted = requested is None and not query
    region = DEFAULT_REGION if defaulted else (requested or "")
    if region == ALL_REGIONS:
        region = ""
    # A default that hides everything is worse than no default at all.
    if defaulted and region and not region_counts.get(region):
        region = ""
        defaulted = False
    if region:
        rows = [r for r in rows if regions.covers(r["region_group"], region)]

    # Offered only once a region is chosen: every region together is ~1,350
    # organizations, too many for a dropdown. The search box still finds any.
    org_options = _organization_options(rows) if region else []
    # A chosen organization with no teams under the current region and grade is
    # dropped, rather than left filtering the table down to nothing.
    org_labels = {_org_key(label): label for label, _ in org_options}
    org = org_labels.get(_org_key(request.args.get("org")), "")
    if org:
        rows = [r for r in rows if _org_key(r.get("organization")) == _org_key(org)]
    matched = len(rows)

    columns, default_sort, column_keys = _team_columns()
    sort = request.args.get("sort") or default_sort
    if sort not in column_keys:
        sort = default_sort
    # Numbers default to biggest-first and text to A-Z, which is what each
    # column is usually being asked for.
    numeric = dict((key, is_num) for key, _, is_num in columns)[sort]
    direction = request.args.get("dir")
    if direction not in ("asc", "desc"):
        direction = "desc" if numeric else "asc"
    rows = _sorted_teams(rows, sort, direction == "desc")

    return render_template("teams.html", section="teams", rows=rows[:TEAM_ROWS],
                           query=query, total=total, matched=matched, limit=TEAM_ROWS,
                           columns=columns, sort=sort, dir=direction,
                           region=region, region_counts=region_counts,
                           region_options=regions.options(region_counts),
                           defaulted=defaulted, all_regions=ALL_REGIONS,
                           grade=grade, grade_counts=grade_counts,
                           org=org, org_options=org_options,
                           event_count=len(_events()),
                           match_count=len(_matches()))


@app.route("/team/<number>")
def page_team(number):
    team = catalog.get_team(number, _dir())
    if team is None:
        abort(404, f"no team {number} in the catalog")

    matches = catalog.matches_for_team(number, _dir())
    rows = _team_rows(number, matches)

    per_event, elo = _ratings()
    events_by_key = _events()
    rated = []
    for key, table in per_event.items():
        if number in table:
            rated.append({"event": events_by_key.get(key, {"key": key, "name": key}),
                          **table[number]})
    rated.sort(key=lambda r: str(r["event"].get("start") or ""))
    season = _attach_ratings([dict(team, **catalog.team_record(matches, number, _scoring()))])[0]

    return render_template(
        "team.html", section="teams", team=team,
        rated=rated, season=season, elo=elo.get(number),
        partners=catalog.partners(matches, number, _scoring()), all_teams=_teams(),
        record=catalog.team_record(matches, number, _scoring()),
        matches=rows,
        videos=[r for r in rows if r["status"]["video"] or r["footage"]],
        analysed_count=sum(1 for r in rows if r["status"]["analysed"]),
        events=_events(),
        media=_team_media(number),
    )


def _team_media(number):
    """The team's YouTube channel and robot videos, ready to render.

    This season's videos come first, newest first within each season, so an
    old robot is never the first thing a scout sees.
    """
    record = team_media.get(number, programs.media_file(_program()['code'])) or {}

    def ordered(videos):
        newest = sorted(videos, key=lambda v: v.get("published") or "", reverse=True)
        return sorted(newest, key=lambda v: v.get("season") != "current")   # stable

    checked = record.get("checked_at")
    return {
        "channel": record.get("channel"),
        "videos": ordered(record.get("videos") or []),
        "channel_suggestions": record.get("channel_suggestions") or [],
        "video_suggestions": ordered(record.get("video_suggestions") or []),
        "checked": _dt.datetime.fromtimestamp(checked).date().isoformat() if checked else None,
    }


@local_only(app.route("/team/<number>/media", methods=["POST"]))
def page_team_media(number):
    """Confirm a suggested channel or video, or remove one for good."""
    team = catalog.get_team(number, _dir())
    if team is None:
        abort(404, f"no team {number} in the catalog")
    action, kind, item_id = (request.form.get(f) or "" for f in ("action", "kind", "id"))
    if kind not in ("channel", "video") or not item_id:
        abort(400, "kind must be channel or video, with an id")
    if action == "confirm":
        team_media.confirm(team["number"], kind, item_id,
                           programs.media_file(_program()["code"]))
    elif action == "remove":
        team_media.remove(team["number"], kind, item_id,
                          programs.media_file(_program()["code"]))
    else:
        abort(400, "action must be confirm or remove")
    return redirect(f"/team/{quote(team['number'])}#videos")


AWARD_HIGHLIGHTS = ("Tournament Champions", "Tournament Finalists", "Excellence Award")


@app.route("/event/<path:key>")
def page_event(key):
    event = catalog.get_event(key, _dir())
    if event is None:
        abort(404, f"no event {key} in the catalog")
    resolved = event["key"]

    matches = [m for m in _matches().values()
               if str(m.get("event") or "").upper() == resolved.upper()]
    bracket = bracket_module.build(matches)
    detail = event_details.load(resolved) or {}

    awards = sorted(detail.get("awards") or [], key=lambda a: a.get("order") or 0)
    # Champions come from the award when the event published one, and from the
    # bracket otherwise. They agree where both exist; the award is preferred
    # because it is what the event itself recorded.
    def award_winners(title):
        for award in awards:
            if (award.get("title") or "").startswith(title):
                return award.get("winners") or []
        return []

    champion = award_winners("Tournament Champions") or bracket["champion"]
    finalist = award_winners("Tournament Finalists") or bracket["finalist"]

    skills = sorted((detail.get("skills") or []),
                    key=lambda s: (s.get("rank") is None, s.get("rank") or 0))
    combined = {}
    for row in skills:
        entry = combined.setdefault(row["team"], {"team": row["team"], "driver": None,
                                                  "programming": None, "rank": row.get("rank")})
        if row.get("type") in ("driver", "programming"):
            entry[row["type"]] = row.get("score")
        if row.get("rank") is not None:
            entry["rank"] = min(entry["rank"] or row["rank"], row["rank"])
    for entry in combined.values():
        entry["total"] = (entry["driver"] or 0) + (entry["programming"] or 0)
    skill_rows = sorted(combined.values(), key=lambda e: -e["total"])

    status = catalog.event_status(event)
    roster = _event_roster(event, matches, detail, status)

    return render_template(
        "event.html", section="events", event=event, roster=roster,
        status=status, grade=catalog.event_grade(event),
        location=_location_label(event.get("location")),
        bracket=bracket, awards=awards, highlights=AWARD_HIGHLIGHTS,
        champion=champion, finalist=finalist,
        rankings=(detail.get("rankings") or []), skills=skill_rows,
        detail_present=event_details.has_content(detail),
        match_count=len(matches),
        scored=sum(1 for m in matches
                   if (m["alliances"]["red"]["score"] is not None
                       and m["alliances"]["blue"]["score"] is not None)),
        with_footage=sum(1 for m in matches if catalog.segment_of(m)),
        webcast=_webcast(event),
    )


def _event_roster(event, matches, detail, status):
    """The teams at an event, each with their season record and Elo.

    Before the event this is the registration list. Once it has happened, the
    teams that actually played (in a match or on the rankings) replace it, since
    registered teams can withdraw; if nothing was recorded, registration is all
    there is, and `attended` is False so the page can say so.
    """
    registered = [str(t).upper() for t in (event.get("teams") or [])]
    played = {t for m in matches for t in catalog.teams_in(m)}
    played |= {str(r["team"]).upper() for r in (detail.get("rankings") or []) if r.get("team")}
    attended = status in ("past", "ongoing") and bool(played)
    numbers = sorted(played if attended else registered)

    teams, elo = _teams(), _ratings()[1]
    by_team = {}
    for match in _matches().values():
        for number in catalog.teams_in(match):
            by_team.setdefault(number, []).append(match)
    scoring = _scoring()
    rows = []
    for number in numbers:
        team = teams.get(number) or {}
        record = catalog.team_record(by_team.get(number, []), number, scoring)
        rows.append({"number": number, "name": team.get("name"),
                     "organization": team.get("organization"),
                     "location": team.get("location"),
                     "elo": (elo.get(number) or {}).get("elo"), **record})
    rows.sort(key=lambda r: (r["elo"] is None, -(r["elo"] or 0), r["number"]))
    return {"rows": rows, "attended": attended, "registered": len(registered)}


def _webcast(event, table=None):
    """The event's stream link from the scraped webcast table, or None.

    The Stream column, filter and sort all read from this one function.
    """
    table = webcasts.load_all() if table is None else table
    return webcasts.describe(webcasts.get(event.get("sku") or event.get("key"), table=table), table)


@app.route("/learn")
def page_learn():
    """The Learn tab, still being written."""
    return render_template("learn.html", tab="learn")


@app.route("/practice")
def page_practice():
    """Placeholder for the Practice tab."""
    return render_template("practice.html", tab="practice")


@app.route("/help")
def page_help():
    """Explains every derived number in the UI, with live figures.

    The numbers are computed rather than written into the prose so the page
    cannot drift from what the code actually does - a help page that quietly
    goes stale is worse than none.
    """
    import statistics
    from analysis import importance as imp

    per_event, elo = _ratings()
    values = sorted(e["elo"] for e in elo.values())
    events = _events()

    field_means, field_tops = [], []
    for event in events.values():
        rated = sorted((elo[t]["elo"] for t in (event.get("teams") or []) if t in elo), reverse=True)
        if len(rated) >= imp.MIN_RATED_TEAMS:
            field_means.append(statistics.mean(rated))
            field_tops.append(statistics.mean(rated[:10]))

    def spread(rows):
        if not rows:
            return None
        rows = sorted(rows)
        return {"min": round(rows[0]), "max": round(rows[-1]),
                "median": round(rows[len(rows) // 2]), "span": round(rows[-1] - rows[0])}

    sample = per_event.get("RE-V5RC-26-4244") or {}
    quality = next(iter(sample.values()), {}).get("quality") if sample else None

    return render_template(
        "help.html", section="help",
        elo_count=len(values),
        elo_spread=spread(values),
        elo_mean=round(statistics.mean(values)) if values else None,
        elo_sd=round(statistics.pstdev(values)) if values else None,
        elo_k=ratings_module.ELO_K, elo_start=ratings_module.ELO_START,
        pools=len(ratings_module.components(list(_matches().values()))),
        field_mean_spread=spread(field_means), field_top_spread=spread(field_tops),
        floor=imp.ELO_FLOOR, ceiling=imp.ELO_CEILING, weights=imp.WEIGHTS,
        levels=imp.LEVEL_SCORES, min_rated=imp.MIN_RATED_TEAMS,
        size_reference=imp.SIZE_REFERENCE,
        quality=quality,
        rated_events=len(field_means), total_events=len(events),
        grade_majority=catalog.GRADE_MAJORITY,
    )


@app.route("/matches")
def page_matches():
    events = _events()
    matches = sorted(_matches().values(), key=lambda m: catalog.sort_key(m, events))
    chosen = request.args.get("event")
    if chosen:
        folded = chosen.upper()
        matches = [m for m in matches if str(m.get("event") or "").upper() == folded]
    if request.args.get("footage"):
        matches = [m for m in matches if catalog.segment_of(m)]
    query = (request.args.get("q") or "").strip()
    if query:
        # Team numbers are matched whole ("929U" finds 929U, not 1929U's
        # matches); anything else matches as text in the event or match name.
        needle = query.lower()

        def hit(match):
            if any(team.lower() == needle for team in catalog.teams_in(match)):
                return True
            event = events.get(str(match.get("event") or "").upper()) or {}
            return needle in " ".join(str(v or "") for v in (
                match.get("name"), match.get("event"), event.get("name"))).lower()
        matches = [m for m in matches if hit(m)]

    total = len(matches)
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1
    pages = max((total + MATCH_ROWS - 1) // MATCH_ROWS, 1)
    page = min(page, pages)
    window = matches[(page - 1) * MATCH_ROWS: page * MATCH_ROWS]

    with_matches = {str(m.get("event") or "").upper() for m in _matches().values()}
    selectable = {k: v for k, v in events.items() if k.upper() in with_matches}
    addable = {k: v for k, v in events.items()
               if catalog.event_status(v) in ("past", "ongoing")}
    return render_template("matches.html", section="matches", events=events,
                           selectable=selectable, addable=addable, chosen=chosen,
                           rows=[{"match": m, "status": _match_status(m),
                                  "footage": _footage(m),
                                  "when": _match_when(m, events.get(str(m.get("event") or "").upper()))}
                                 for m in window],
                           rounds=catalog.ROUNDS, videos=_list_videos(),
                           total=total, page=page, pages=pages,
                           footage_only=bool(request.args.get("footage")), query=query)


def _days_from_today(value, today=None):
    """Absolute days between an event's start and today, for proximity sorting.

    Events with no usable date sort last rather than being treated as today,
    which would push undated records to the top of the one view meant to show
    what is happening now.
    """
    today = today or _dt.date.today()
    text = str(value or "")[:10]
    try:
        return (0, abs((_dt.date.fromisoformat(text) - today).days))
    except ValueError:
        return (1, 0)


# (key, header, numeric). Numeric columns start highest-first on their first
# click; text columns start A-Z.
EVENT_COLUMNS = (
    ("name", "Event", False),
    ("sku", "SKU", False),
    ("importance", "Importance", True),
    ("status", "Status", False),
    ("grade", "Grade", False),
    ("start", "Dates", False),
    ("where", "Where", False),
    ("matches", "Matches", True),
    ("stream", "Stream", False),
)
EVENT_COLUMN_KEYS = {key for key, _, _ in EVENT_COLUMNS}
EVENT_COLUMN_NUMERIC = {key: numeric for key, _, numeric in EVENT_COLUMNS}
DEFAULT_EVENT_SORT = "importance"
STATUS_ORDER = {"past": 0, "ongoing": 1, "upcoming": 2}

# What the Stream column's button says, in the order the column sorts. An
# upcoming event with no link is "none": the state describes the link, and
# whether an event has happened is the Status filter's job.
STREAM_STATES = {"stream": "Stream", "upcoming": "Upcoming", "none": "No stream"}


def _stream_state(row):
    if not row.get("webcast"):
        return "none"
    return "upcoming" if row["status"] == "upcoming" else "stream"


def _event_sort_value(row, sort):
    """The value a column sorts on, or None when the event has none."""
    if sort == "importance":
        return row["importance"]["score"]
    if sort == "status":
        return STATUS_ORDER.get(row["status"])
    if sort == "where":
        label = row.get("location_label")
        return (regions.sort_key(row["region_group"]), label.lower()) if label else None
    if sort == "stream":
        return list(STREAM_STATES).index(_stream_state(row))
    if sort == "near":
        rank, days = _days_from_today(row.get("start"))
        return None if rank else days
    if sort == "matches":
        return row.get("matches") or 0
    value = row.get(sort)
    if value in (None, ""):
        return None
    return value.lower() if isinstance(value, str) else value


def _sorted_events(rows, sort, descending):
    """Sort by one column, keeping unknown values last in both directions.

    Ties break on start date then name - except importance, which breaks on
    closeness to today, so of two equally important events the sooner one leads.
    Successive stable sorts, least significant first; Python keeps equal items in
    order even with reverse=True, so the tie-breakers are not flipped.
    """
    known = [r for r in rows if _event_sort_value(r, sort) is not None]
    unknown = [r for r in rows if _event_sort_value(r, sort) is None]
    for group in (known, unknown):
        group.sort(key=lambda r: (r.get("name") or "").lower())
        if sort == "importance":
            group.sort(key=lambda r: _days_from_today(r.get("start")))
        else:
            group.sort(key=lambda r: str(r.get("start") or "9999"))
    known.sort(key=lambda r: _event_sort_value(r, sort), reverse=descending)
    return known + unknown


@app.route("/events")
def page_events():
    events = _events()
    counts = {}
    for match in _matches().values():
        counts[match.get("event")] = counts.get(match.get("event"), 0) + 1

    _, elo = _ratings()
    webcast_table = webcasts.load_all()
    rows = []
    for event in events.values():
        rows.append({**event, "status": catalog.event_status(event),
                     "webcast": _webcast(event, webcast_table),
                     "matches": counts.get(event["key"], 0),
                     "region_group": regions.group_of(event.get("location")),
                     "grade": catalog.event_grade(event),
                     "importance": importance_module.score_event(event, elo),
                     "location_label": _location_label(event.get("location"))})

    query = (request.args.get("q") or "").strip()
    if query:
        needle = query.lower()
        rows = [r for r in rows
                if needle in " ".join(str(r.get(f) or "")
                                      for f in ("name", "sku", "key", "location_label")).lower()]

    region_counts = {}
    for row in rows:
        region_counts[row["region_group"]] = region_counts.get(row["region_group"], 0) + 1
    region_counts = dict(sorted(region_counts.items(), key=lambda i: regions.sort_key(i[0])))

    grade_counts = {}
    for row in rows:
        if row.get("grade"):
            grade_counts[row["grade"]] = grade_counts.get(row["grade"], 0) + 1
    grade_counts = dict(sorted(grade_counts.items()))
    grade = request.args.get("grade") or ""
    if grade:
        rows = [r for r in rows if r.get("grade") == grade]

    requested = request.args.get("region")
    # As on the Teams page, the region default narrows browsing, never a search.
    defaulted = requested is None and not query
    region = DEFAULT_REGION if defaulted else (requested or "")
    if region == ALL_REGIONS:
        region = ""
    if defaulted and region and not region_counts.get(region):
        region = ""
        defaulted = False
    if region:
        rows = [r for r in rows if regions.covers(r["region_group"], region)]

    # Old links used ?order=; keep them working.
    legacy = request.args.get("order")
    near = request.args.get("near") == "1" or legacy == "near"
    sort = request.args.get("sort") or ("start" if legacy == "date" else DEFAULT_EVENT_SORT)
    if sort not in EVENT_COLUMN_KEYS:
        sort = DEFAULT_EVENT_SORT
    default_dir = "desc" if EVENT_COLUMN_NUMERIC[sort] else "asc"
    dir_ = request.args.get("dir") if request.args.get("dir") in ("asc", "desc") else default_dir
    # Closest to today has one sensible direction: nearest first.
    rows = _sorted_events(rows, "near" if near else sort, dir_ == "desc" and not near)

    chosen = request.args.get("status")
    if chosen:
        rows = [r for r in rows if r["status"] == chosen]
    stream_counts = {state: 0 for state in STREAM_STATES}
    for row in rows:
        stream_counts[_stream_state(row)] += 1
    stream = request.args.get("stream") if request.args.get("stream") in STREAM_STATES else ""
    if stream:
        rows = [r for r in rows if _stream_state(r) == stream]
    tally = {}
    for event in events.values():
        state = catalog.event_status(event)
        tally[state] = tally.get(state, 0) + 1

    shown = len(rows)
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1
    pages = max((shown + EVENT_ROWS - 1) // EVENT_ROWS, 1)
    page = min(page, pages)
    rows = rows[(page - 1) * EVENT_ROWS: page * EVENT_ROWS]

    return render_template("events.html", section="events", rows=rows, counts=counts,
                           tally=tally, chosen=chosen, total=len(events),
                           shown=shown, page=page, pages=pages,
                           region=region, region_counts=region_counts,
                           region_options=regions.options(region_counts),
                           defaulted=defaulted, all_regions=ALL_REGIONS,
                           grade=grade, grade_counts=grade_counts,
                           columns=EVENT_COLUMNS, sort=sort, dir=dir_, near=near, query=query,
                           stream=stream, stream_counts=stream_counts, stream_states=STREAM_STATES,
                           today=_dt.date.today().isoformat())



def _teams_field(raw):
    """Accept "929U, 1234A" or "929u 1234a" - scouting notes are typed, not pasted."""
    return [part for part in (raw or "").replace(",", " ").split() if part]


def _optional_int(raw):
    """Blank means unknown, which is not the same as zero.

    A score of 0 is a real result; an empty box is a match nobody has entered a
    score for. Coercing the second into the first would show a team a loss it
    never played.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _video_field(form):
    """The footage record: a link with a start time, a local segment, or both.

    An event is streamed as one long video, so a match is a timestamp inside
    it rather than a video of its own. A link alone is now enough to record -
    it used to need a local file and a frame range, which meant footage could
    only be recorded on a machine holding the video.
    """
    name = os.path.basename((form.get("video_file") or "").strip())
    start = _optional_int(form.get("start_frame"))
    end = _optional_int(form.get("end_frame"))
    url = (form.get("url") or "").strip()
    at = videolinks.parse_time(form.get("watch_start"))
    if at is None:
        at = videolinks.start_in_url(url)

    record = {}
    if name and start is not None and end is not None:
        record.update({"file": name, "start_frame": start, "end_frame": end})
    if url:
        record["url"] = url
        record["watch_start"] = at
    return record or None


def _footage(match):
    """The match's YouTube link, ready to render, or None."""
    video = (match or {}).get("video") or {}
    return videolinks.describe(video.get("url"), video.get("watch_start"))


@local_only(app.route("/events/new", methods=["POST"]))
def page_add_event():
    form = request.form
    name = (form.get("name") or "").strip()
    if not name:
        abort(400, "an event name is required")
    event = catalog.save_event(
        name=name, sku=(form.get("sku") or "").strip() or None,
        season=(form.get("season") or "").strip() or None,
        start=(form.get("start") or "").strip() or None,
        end=(form.get("end") or "").strip() or None,
        level=(form.get("level") or "").strip() or None,
        location={"city": (form.get("city") or "").strip() or None,
                  "region": (form.get("region") or "").strip() or None},
        directory=_dir(),
    )
    return redirect(f"/matches?event={event['key']}")


@local_only(app.route("/matches/new", methods=["POST"]))
def page_add_match():
    form = request.form
    event = (form.get("event") or "").strip()
    if not catalog.get_event(event, _dir()):
        abort(400, "choose an event that exists")
    match = catalog.save_match(
        event=event,
        round_slug=(form.get("round") or "unknown").strip(),
        instance=_optional_int(form.get("instance")) or 1,
        number=_optional_int(form.get("number")) or 1,
        red=_teams_field(form.get("red")), blue=_teams_field(form.get("blue")),
        red_score=_optional_int(form.get("red_score")),
        blue_score=(_optional_int(form.get("red_score"))
                    if _scoring() == catalog.COOPERATIVE_SCORING
                    else _optional_int(form.get("blue_score"))),
        video=_video_field(form), directory=_dir(),
    )
    return redirect(f"/match/{match['key']}")


@app.route("/match/<path:key>")
def page_match(key):
    match = catalog.get_match(key, _dir())
    if match is None:
        abort(404, f"no match {key} in the catalog")

    status = _match_status(match)
    workspace = "/workspace"
    segment = catalog.segment_of(match)
    if segment:
        name, start, end = segment
        workspace = f"/workspace?video={quote(name)}&start={start}&end={end}"

    event = catalog.get_event(match.get("event"), _dir())
    return render_template(
        "match.html", section="matches", match=match,
        event=event, when=_match_when(match, event),
        round_label=catalog.ROUND_LABELS.get(match.get("round"), "Unspecified"),
        event_status=catalog.event_status(catalog.get_event(match.get("event"), _dir())),
        footage=_footage(match),
        status=status, result=status["result"] and _result_payload(status["result"]),
        videos=_list_videos(), workspace_url=workspace,
    )


@local_only(app.route("/match/<path:key>/alliances", methods=["POST"]))
def page_match_alliances(key):
    match = catalog.get_match(key, _dir())
    if match is None:
        abort(404, f"no match {key} in the catalog")
    form = request.form
    red_score = _optional_int(form.get("red_score"))
    blue_score = _optional_int(form.get("blue_score"))
    # One Teamwork score, stored on both sides the way the API reports it, so
    # editing it by hand cannot leave the two halves disagreeing.
    if _scoring() == catalog.COOPERATIVE_SCORING:
        blue_score = red_score
    catalog.save_match(
        event=match["event"], round_slug=match["round"], instance=match["instance"],
        number=match["number"], key=match["key"],
        red=_teams_field(form.get("red")), blue=_teams_field(form.get("blue")),
        red_score=red_score, blue_score=blue_score,
        video=match.get("video"), directory=_dir(),
    )
    return redirect(f"/match/{key}")


@local_only(app.route("/match/<path:key>/video", methods=["POST"]))
def page_match_video(key):
    match = catalog.get_match(key, _dir())
    if match is None:
        abort(404, f"no match {key} in the catalog")
    alliances = match.get("alliances", {})
    catalog.save_match(
        event=match["event"], round_slug=match["round"], instance=match["instance"],
        number=match["number"], key=match["key"],
        red=alliances.get("red", {}).get("teams", []),
        blue=alliances.get("blue", {}).get("teams", []),
        red_score=alliances.get("red", {}).get("score"),
        blue_score=alliances.get("blue", {}).get("score"),
        video=_video_field(request.form), directory=_dir(),
        name=match.get("name"), field=match.get("field"),
        api_id=match.get("api_id"), scheduled=match.get("scheduled"),
        # Linking footage says nothing about where the match data came from, so
        # provenance is carried through rather than reset to "manual".
        source=match.get("source", "manual"),
    )
    return redirect(f"/match/{key}")


def create_app():
    return app
