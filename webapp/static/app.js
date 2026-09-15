const el = (id) => document.getElementById(id);
const state = { video: null, segment: null, jobId: null, timer: null, saved: null, setup: null };

const PANELS = ["setup", "field-panel", "seed-panel", "progress-panel", "result-panel", "error-panel"];
function show(...ids) {
  PANELS.forEach((id) => el(id).classList.toggle("hidden", !ids.includes(id)));
}

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `request failed (${response.status})`);
  return data;
}

function timecode(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

async function loadVideos() {
  const { videos } = await api("/api/videos");
  const list = el("video-list");
  list.innerHTML = "";

  if (!videos.length) {
    list.innerHTML = '<div class="note">No videos found. Put a file in the videos/ folder.</div>';
    return;
  }

  videos.forEach((video) => {
    const row = document.createElement("div");
    row.className = "item";
    row.innerHTML = `
      <span class="name">${video.name}</span>
      <span class="meta">${video.size_mb} MB</span>`;
    // Every video is selectable. Setup is per match now, so whether anything is
    // usable cannot be answered at this level - and gating here made calibration
    // unreachable, since it lives behind the match list.
    row._video = video;
    row.onclick = () => selectVideo(video, row);
    list.appendChild(row);
  });
}

async function selectVideo(video, row) {
  document.querySelectorAll("#video-list .item").forEach((n) => n.classList.remove("selected"));
  row.classList.add("selected");
  state.video = video;
  state.segment = null;
  updateStart();

  const list = el("segment-list");
  list.className = "list muted";
  list.textContent = "Detecting matches…";
  try {
    const { segments } = await api(`/api/segments?video=${encodeURIComponent(video.name)}`);
    list.className = "list";
    list.innerHTML = "";
    if (!segments.length) {
      list.innerHTML = '<div class="note">No matches detected.</div>';
      return;
    }
    segments.forEach((segment) => {
      const item = document.createElement("div");
      item.className = "item";
      item.innerHTML = `
        <span class="name">Match ${segment.index + 1}</span>
        <span class="meta">${timecode(segment.start_s)} – ${timecode(segment.end_s)}
          &middot; ${Math.round(segment.duration_s)}s &middot; frames ${segment.start}–${segment.end}</span>
        <span class="tag setup"></span>
        <span class="tag saved hidden">processed</span>`;
      item.onclick = () => selectSegment(segment, item);
      list.appendChild(item);
      // Marked without blocking the list: the lookups are quick, but the matches
      // should be clickable the moment they appear.
      markIfSaved(video.name, segment, item);
      markSetup(video.name, segment, item);
    });
  } catch (error) {
    list.className = "list";
    list.innerHTML = `<div class="note warn">${error.message}</div>`;
  }
}

async function markSetup(videoName, segment, item) {
  const tag = item.querySelector(".tag.setup");
  try {
    const s = await api(
      `/api/setup?video=${encodeURIComponent(videoName)}&start=${segment.start}&end=${segment.end}`);
    if (s.ready) { tag.textContent = "ready"; tag.classList.add("ok"); }
    else if (s.field || s.robots) { tag.textContent = "setup started"; tag.classList.add("missing"); }
    else { tag.textContent = "needs setup"; tag.classList.add("missing"); }
  } catch (error) {
    tag.textContent = "";
  }
}

async function markIfSaved(videoName, segment, item) {
  try {
    const { result } = await api(
      `/api/results?video=${encodeURIComponent(videoName)}&start=${segment.start}&end=${segment.end}`);
    if (result) item.querySelector(".tag").classList.remove("hidden");
  } catch (error) {
    /* leave unmarked */
  }
}

async function selectSegment(segment, item) {
  document.querySelectorAll("#segment-list .item").forEach((n) => n.classList.remove("selected"));
  item.classList.add("selected");
  state.segment = segment;
  state.saved = null;
  state.setup = null;
  el("setup-note").textContent = "Checking setup…";
  await refreshSetup();

  try {
    const { result } = await api(
      `/api/results?video=${encodeURIComponent(state.video.name)}&start=${segment.start}&end=${segment.end}`);
    state.saved = result;
  } catch (error) {
    state.saved = null;
  }
  updateStart();
}

function savedAgo(seconds) {
  if (!seconds) return "";
  const mins = Math.max(0, (Date.now() / 1000 - seconds) / 60);
  if (mins < 2) return "moments ago";
  if (mins < 90) return `${Math.round(mins)} min ago`;
  if (mins < 2880) return `${Math.round(mins / 60)} h ago`;
  return `${Math.round(mins / 1440)} days ago`;
}

function updateStart() {
  const chosen = Boolean(state.video && state.segment);
  const ready = chosen && Boolean(state.setup && state.setup.ready);
  const start = el("start");
  const reprocess = el("reprocess");

  el("setup-buttons").classList.toggle("hidden", !chosen);
  reprocess.classList.toggle("hidden", !(ready && state.saved));

  if (!chosen) {
    start.disabled = true;
    start.textContent = "Start processing";
    el("setup-note").textContent = "";
    return;
  }
  if (!ready) {
    // Neither input has a safe default: an undrawn field means the wrong
    // geometry and unmarked robots mean motion's guess, which misses the ones
    // standing still. Both are required rather than filled in silently.
    start.disabled = true;
    start.textContent = "Start processing";
    const missing = [];
    if (state.setup && !state.setup.field) missing.push("select the field");
    if (state.setup && !state.setup.robots) missing.push("select the robots");
    el("setup-note").textContent = missing.length
      ? `Still to do: ${missing.join(" and ")}`
      : "Checking setup…";
    return;
  }
  start.disabled = false;
  if (state.saved) {
    // Already done, so the primary action is to look at it rather than spend
    // fifteen minutes producing identical files.
    start.textContent = "View result";
    el("setup-note").textContent = `Processed ${savedAgo(state.saved.saved_at)}`;
  } else {
    start.textContent = "Start processing";
    el("setup-note").textContent =
      `About ${Math.max(1, Math.round(state.segment.duration_s / 9))} min of processing`;
  }
}

async function startJob(force = false) {
  if (state.saved && !force) {
    showResult(state.saved);
    return;
  }

  el("start").disabled = true;
  el("reprocess").disabled = true;
  try {
    const response = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        video: state.video.name,
        start: state.segment.start,
        end: state.segment.end,
        force,
      }),
    });
    if (response.cached) showResult(response.result);
    else watchJob(response.job_id);
  } catch (error) {
    showError(error.message);
  } finally {
    el("reprocess").disabled = false;
  }
}

function watchJob(jobId) {
  state.jobId = jobId;
  el("bar").style.width = "0%";
  el("log").textContent = "";
  show("progress-panel");
  clearInterval(state.timer);
  state.timer = setInterval(() => pollJob(jobId), 500);
  pollJob(jobId);
}

async function pollJob(jobId) {
  let job;
  try {
    job = await api(`/api/jobs/${jobId}`);
  } catch (error) {
    clearInterval(state.timer);
    showError(error.message);
    return;
  }

  el("bar").style.width = `${(job.progress * 100).toFixed(1)}%`;
  el("stage-label").textContent = job.label || job.stage || "";
  el("stage-pct").textContent = `${(job.progress * 100).toFixed(0)}%`;
  if (job.log && job.log.length) {
    const log = el("log");
    const pinned = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    log.textContent = job.log.join("\n");
    if (pinned) log.scrollTop = log.scrollHeight;
  }

  if (job.state === "done") {
    clearInterval(state.timer);
    showResult(job.result);
  } else if (job.state === "error") {
    clearInterval(state.timer);
    showError(job.error);
  }
}

function showResult(result) {
  show("result-panel");
  const video = el("result-video");
  video.src = result.video_url ? `${result.video_url}?t=${Date.now()}` : "";
  el("paths-link").href = result.paths_url;
  el("seed-link").href = result.seed_url;
  const saved = result.reused ? ` &middot; saved ${savedAgo(result.saved_at)}, not reprocessed` : "";
  el("result-note").innerHTML = result.codec === "h264"
    ? `Tracked ${result.robots} robots.${saved}`
    : `<span class="warn">Encoded as ${result.codec}, which browsers cannot play.
       Install imageio-ffmpeg for H.264.</span>`;

  const rows = Object.entries(result.summary || {}).map(([id, s]) => `
    <tr>
      <td>Robot ${id}</td>
      <td class="num">${s.samples}</td>
      <td class="num">${s.reliable ?? "-"}</td>
      <td class="num">${Math.round(s.distance_in)}"</td>
      <td class="num">${(s.max_step_in ?? 0).toFixed(1)}"</td>
      <td class="num">${s.merged ?? 0}</td>
    </tr>`).join("");
  el("summary").innerHTML = rows
    ? `<table><thead><tr><th>Track</th><th>Samples</th><th>Reliable</th>
       <th>Travelled</th><th>Max step</th><th>Merged</th></tr></thead><tbody>${rows}</tbody></table>`
    : "";
}

function showError(message) {
  show("error-panel");
  el("error-text").textContent = message || "unknown error";
}

async function reset() {
  state.jobId = null;
  clearInterval(state.timer);
  show("setup");
  // Re-check: the match just processed is now saved, and should say so.
  if (state.video && state.segment) await selectSegment(state.segment,
    [...document.querySelectorAll("#segment-list .item")].find((n) => n.classList.contains("selected"))
    || document.createElement("div"));
  updateStart();
}

el("start").onclick = () => startJob(false);
el("reprocess").onclick = () => startJob(true);
el("again").onclick = reset;
el("retry").onclick = reset;

// A job survives a page reload, so reattach to one that is still running rather
// than stranding it with no way to see progress.
(async function init() {
  show("setup");
  await loadVideos();

  // A match page links here to set up or run its segment, so the video and the
  // frame range can arrive in the URL. Selecting them here means the match page
  // does not need its own copy of the field and seed editors yet.
  const params = new URLSearchParams(location.search);
  const wanted = params.get("video");
  let preselect = wanted;
  if (!preselect) {
    try {
      preselect = (await api("/api/config")).preselect;
    } catch (error) {
      preselect = null;
    }
  }
  if (preselect) {
    const row = [...document.querySelectorAll("#video-list .item")]
      .find((n) => n.querySelector(".name").textContent === preselect);
    if (row) {
      await selectVideo(row._video || { name: preselect }, row);
      const start = params.get("start");
      const end = params.get("end");
      if (start && end) {
        // Matched on the exact frame range: the segment list is detected from the
        // video, so a range that came from a saved match should already be in it.
        const item = [...document.querySelectorAll("#segment-list .item")].find((n) =>
          n.querySelector(".meta").textContent.includes(`frames ${start}\u2013${end}`));
        if (item) item.click();
        else el("setup-note").textContent =
          `Frames ${start}-${end} are not among the detected matches in this video.`;
      }
    }
  }
  try {
    const { jobs } = await api("/api/jobs");
    const running = jobs.find((job) => job.state === "running");
    if (running) watchJob(running.id);
  } catch (error) {
    /* no jobs yet */
  }
})();

/* ---- Marking robots by hand -------------------------------------------------
 * Motion cannot find a robot that is standing still, so the automatic guess is
 * only right about half the time. Boxes drawn here replace that guess, and are
 * kept as training labels for a detector that will not depend on movement.
 */
const seed = { image: null, frameIndex: null, boxes: [], scale: 1, drag: null };

function seedCanvas() { return el("seed-canvas"); }

function drawSeed() {
  const canvas = seedCanvas();
  const ctx = canvas.getContext("2d");
  ctx.drawImage(seed.image, 0, 0, canvas.width, canvas.height);

  const boxes = seed.drag ? seed.boxes.concat([seed.drag]) : seed.boxes;
  boxes.forEach((box, index) => {
    const [x, y, w, h] = box.map((v) => v * seed.scale);
    ctx.lineWidth = 2;
    ctx.strokeStyle = TRACK_COLOURS[index % TRACK_COLOURS.length];
    ctx.strokeRect(x, y, w, h);
    ctx.fillStyle = ctx.strokeStyle;
    ctx.font = "13px system-ui";
    ctx.fillText(`robot ${index}`, x + 3, Math.max(y - 5, 12));
  });
  el("seed-note").textContent =
    `${seed.boxes.length} robot${seed.boxes.length === 1 ? "" : "s"} marked` +
    (seed.boxes.length === 4 ? "" : " — a match normally has 4");
}

const TRACK_COLOURS = ["#50dc50", "#50a0ff", "#f0c83c", "#c878ff", "#78f0f0", "#ff8c8c"];

function canvasPoint(event) {
  const rect = seedCanvas().getBoundingClientRect();
  // The canvas is scaled twice: source pixels to canvas pixels, then CSS
  // scaling to fit the panel. Both have to be undone or the boxes land wrong.
  const cssScale = seedCanvas().width / rect.width;
  return {
    x: ((event.clientX - rect.left) * cssScale) / seed.scale,
    y: ((event.clientY - rect.top) * cssScale) / seed.scale,
  };
}

async function openSeedEditor() {
  el("seed-note").textContent = "Finding a frame…";
  show("seed-panel");
  try {
    const data = await api(
      `/api/seed-frame?video=${encodeURIComponent(state.video.name)}` +
      `&start=${state.segment.start}&end=${state.segment.end}`);
    seed.frameIndex = data.frame_index;
    seed.boxes = data.boxes.map((b) => b.map(Number));

    const image = new Image();
    image.onload = () => {
      seed.image = image;
      const canvas = seedCanvas();
      seed.scale = Math.min(1, 1100 / image.width);
      canvas.width = Math.round(image.width * seed.scale);
      canvas.height = Math.round(image.height * seed.scale);
      drawSeed();
    };
    image.src = data.image;
  } catch (error) {
    showError(error.message);
  }
}

seedCanvas().addEventListener("mousedown", (event) => {
  const p = canvasPoint(event);
  // Clicking inside an existing box removes it, so a wrong auto-box is one
  // click to delete rather than something to work around.
  const hit = seed.boxes.findIndex(([x, y, w, h]) =>
    p.x >= x && p.x <= x + w && p.y >= y && p.y <= y + h);
  if (hit >= 0 && event.shiftKey) {
    seed.boxes.splice(hit, 1);
    drawSeed();
    return;
  }
  seed.drag = [p.x, p.y, 0, 0];
});

seedCanvas().addEventListener("mousemove", (event) => {
  if (!seed.drag) return;
  const p = canvasPoint(event);
  seed.drag[2] = p.x - seed.drag[0];
  seed.drag[3] = p.y - seed.drag[1];
  drawSeed();
});

window.addEventListener("mouseup", () => {
  if (!seed.drag) return;
  let [x, y, w, h] = seed.drag;
  seed.drag = null;
  if (w < 0) { x += w; w = -w; }
  if (h < 0) { y += h; h = -h; }
  if (w > 8 && h > 8) seed.boxes.push([x, y, w, h]);   // ignore stray clicks
  drawSeed();
});

el("seed-clear").onclick = () => { seed.boxes = []; drawSeed(); };
el("seed-cancel").onclick = () => { show("setup"); };
el("seed-open").onclick = openSeedEditor;

el("seed-save").onclick = async () => {
  if (!seed.boxes.length) { el("seed-note").textContent = "Draw at least one box first."; return; }
  try {
    await api("/api/seeds", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        video: state.video.name,
        start: state.segment.start,
        end: state.segment.end,
        frame_index: seed.frameIndex,
        boxes: seed.boxes,
      }),
    });
    show("setup");
    // Hand-drawn seeds change the result, so any cached run no longer applies.
    await selectSegment(state.segment,
      [...document.querySelectorAll("#segment-list .item")]
        .find((n) => n.classList.contains("selected")) || document.createElement("div"));
  } catch (error) {
    showError(error.message);
  }
};

/* ---- Selecting the field ----------------------------------------------------
 * Four corners fully determine the homography, so the 24in tile grid can be
 * projected through them live. Aligning that grid to the mat's own lines is what
 * makes the result accurate - a quad that merely looks square can still be
 * several inches out, and nothing downstream would reveal it.
 */
const fieldState = { image: null, frameIndex: null, corners: [], scale: 1, dragging: null };
const FIELD_SIZE_IN = 144;

// Every Goal sits on a 24in tile corner, taken from Appendix A of the Game Manual.
// These are far easier to line up against than the mat's tile seams, which are
// faint in broadcast footage - a Goal is unmistakable, a seam often is not.
// pair_a is drawn red and pair_b blue. Which is truly which depends on the field
// setup, so you rotate the origin until the red rings land on the real red goals -
// that single act pins the orientation, which nothing else in the pipeline can
// establish on geometry alone.
const GOAL_POINTS = [
  { pos: [48, 24], kind: "short" }, { pos: [24, 48], kind: "short" },
  { pos: [120, 96], kind: "short" }, { pos: [96, 120], kind: "short" },
  { pos: [72, 72], kind: "tall" },
  { pos: [24, 96], kind: "alliance", pair: "a" }, { pos: [48, 120], kind: "alliance", pair: "a" },
  { pos: [96, 24], kind: "alliance", pair: "b" }, { pos: [120, 48], kind: "alliance", pair: "b" },
];
const GOAL_STYLE = {
  short: { colour: "rgba(245, 205, 70, 0.95)", label: "short" },
  tall: { colour: "rgba(255, 255, 255, 0.95)", label: "TALL" },
  a: { colour: "rgba(255, 70, 70, 0.98)", label: "RED goal" },
  b: { colour: "rgba(80, 165, 255, 0.98)", label: "BLUE goal" },
};
const MIDFIELD_VERTEX_IN = 24;

function fieldCanvas() { return el("field-canvas"); }

function homographyFromCorners(corners) {
  // Field inches -> image pixels, solved from the four correspondences. Standard
  // 8-unknown linear system; the grid is drawn by pushing field points through it.
  const dst = corners;
  const src = [[0, 0], [FIELD_SIZE_IN, 0], [FIELD_SIZE_IN, FIELD_SIZE_IN], [0, FIELD_SIZE_IN]];
  const A = [], b = [];
  for (let i = 0; i < 4; i++) {
    const [x, y] = src[i], [u, v] = dst[i];
    A.push([x, y, 1, 0, 0, 0, -x * u, -y * u]); b.push(u);
    A.push([0, 0, 0, x, y, 1, -x * v, -y * v]); b.push(v);
  }
  const h = solve(A, b);
  if (!h) return null;
  return [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1]];
}

function solve(A, b) {
  const n = b.length;
  const M = A.map((row, i) => row.concat([b[i]]));
  for (let col = 0; col < n; col++) {
    let pivot = col;
    for (let r = col + 1; r < n; r++) if (Math.abs(M[r][col]) > Math.abs(M[pivot][col])) pivot = r;
    if (Math.abs(M[pivot][col]) < 1e-9) return null;
    [M[col], M[pivot]] = [M[pivot], M[col]];
    for (let r = 0; r < n; r++) {
      if (r === col) continue;
      const f = M[r][col] / M[col][col];
      for (let c = col; c <= n; c++) M[r][c] -= f * M[col][c];
    }
  }
  // Full Gauss-Jordan leaves a diagonal matrix, so each unknown is just its row's
  // right-hand side over its pivot.
  return M.map((row, i) => row[n] / M[i][i]);
}

function project(H, x, y) {
  const d = H[2][0] * x + H[2][1] * y + H[2][2];
  return [(H[0][0] * x + H[0][1] * y + H[0][2]) / d, (H[1][0] * x + H[1][1] * y + H[1][2]) / d];
}

function drawField() {
  const canvas = fieldCanvas();
  const ctx = canvas.getContext("2d");
  ctx.drawImage(fieldState.image, 0, 0, canvas.width, canvas.height);

  const H = homographyFromCorners(fieldState.corners);
  if (H) {
    ctx.strokeStyle = "rgba(60, 220, 90, 0.85)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 6; i++) {
      const o = i * 24;
      for (const [a, c] of [[[o, 0], [o, FIELD_SIZE_IN]], [[0, o], [FIELD_SIZE_IN, o]]]) {
        const p = project(H, a[0], a[1]).map((v) => v * fieldState.scale);
        const q = project(H, c[0], c[1]).map((v) => v * fieldState.scale);
        ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]); ctx.stroke();
      }
    }
  }

  if (H) {
    // Midfield diamond
    const diamond = [[72, 72 - MIDFIELD_VERTEX_IN], [72 + MIDFIELD_VERTEX_IN, 72],
                     [72, 72 + MIDFIELD_VERTEX_IN], [72 - MIDFIELD_VERTEX_IN, 72]];
    ctx.strokeStyle = "rgba(255,255,255,0.9)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    diamond.forEach((p, i) => {
      const q = project(H, p[0], p[1]).map((v) => v * fieldState.scale);
      i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1]);
    });
    ctx.closePath();
    ctx.stroke();

    // Goal keypoints. Alliance goals are drawn the same as neutral ones on
    // purpose: which pair is red depends on the setup and is worked out from the
    // footage afterwards, so colouring them here would suggest an orientation
    // that has not been established yet.
    GOAL_POINTS.forEach((goal) => {
      const q = project(H, goal.pos[0], goal.pos[1]).map((v) => v * fieldState.scale);
      const style = GOAL_STYLE[goal.pair || goal.kind];
      const r = goal.kind === "tall" ? 14 : goal.kind === "alliance" ? 12 : 9;
      ctx.strokeStyle = style.colour;
      ctx.lineWidth = goal.kind === "alliance" ? 3 : 2;
      ctx.beginPath(); ctx.arc(q[0], q[1], r, 0, Math.PI * 2); ctx.stroke();
      ctx.fillStyle = style.colour;
      ctx.beginPath(); ctx.arc(q[0], q[1], 2.5, 0, Math.PI * 2); ctx.fill();
      ctx.font = "12px system-ui";
      ctx.fillText(style.label, q[0] + r + 4, q[1] + 4);
    });
  }

  const labels = ["(0,0)", "(144,0)", "(144,144)", "(0,144)"];
  fieldState.corners.forEach(([x, y], i) => {
    const px = x * fieldState.scale, py = y * fieldState.scale;
    ctx.fillStyle = "#ffd24a";
    ctx.beginPath(); ctx.arc(px, py, 7, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "#111"; ctx.lineWidth = 1; ctx.stroke();
    ctx.fillStyle = "#fff"; ctx.font = "12px system-ui";
    ctx.fillText(labels[i], px + 10, py - 10);
  });
}

function defaultCorners(width, height) {
  const half = Math.min(width, height) * 0.28;
  const cx = width / 2, cy = height / 2;
  return [[cx - half, cy - half], [cx + half, cy - half], [cx + half, cy + half], [cx - half, cy + half]];
}

async function openFieldEditor() {
  el("field-note").textContent = "Loading a frame…";
  show("field-panel");
  try {
    const data = await api(
      `/api/field?video=${encodeURIComponent(state.video.name)}` +
      `&start=${state.segment.start}&end=${state.segment.end}`);
    fieldState.frameIndex = data.frame_index;

    const image = new Image();
    image.onload = () => {
      fieldState.image = image;
      const canvas = fieldCanvas();
      fieldState.scale = Math.min(1, 1100 / image.width);
      canvas.width = Math.round(image.width * fieldState.scale);
      canvas.height = Math.round(image.height * fieldState.scale);
      // Reopening shows exactly what was drawn before, so a small correction does
      // not mean starting over.
      fieldState.corners = data.corners
        ? data.corners.map(([x, y]) => [Number(x), Number(y)])
        : defaultCorners(image.width, image.height);
      el("field-note").textContent = data.corners
        ? "Saved field loaded — adjust and save again to change it."
        : "Drag each corner onto the field's floor corners.";
      drawField();
    };
    image.src = data.image;
  } catch (error) {
    showError(error.message);
  }
}

function fieldPoint(event) {
  const rect = fieldCanvas().getBoundingClientRect();
  const cssScale = fieldCanvas().width / rect.width;
  return {
    x: ((event.clientX - rect.left) * cssScale) / fieldState.scale,
    y: ((event.clientY - rect.top) * cssScale) / fieldState.scale,
  };
}

fieldCanvas().addEventListener("mousedown", (event) => {
  const p = fieldPoint(event);
  let nearest = 0, best = Infinity;
  fieldState.corners.forEach(([x, y], i) => {
    const d = Math.hypot(x - p.x, y - p.y);
    if (d < best) { best = d; nearest = i; }
  });
  if (best * fieldState.scale < 30) fieldState.dragging = nearest;
});

fieldCanvas().addEventListener("mousemove", (event) => {
  if (fieldState.dragging === null) return;
  const p = fieldPoint(event);
  fieldState.corners[fieldState.dragging] = [p.x, p.y];
  drawField();
});

window.addEventListener("mouseup", () => { fieldState.dragging = null; });

el("field-rotate").onclick = () => {
  // Rotating which drawn corner is the origin turns the whole model a quarter
  // turn, swapping the alliance pairs every two presses.
  fieldState.corners.push(fieldState.corners.shift());
  drawField();
  el("field-note").textContent =
    "Rotated. Keep going until the red rings sit on the real red goals.";
};

el("field-reset").onclick = () => {
  fieldState.corners = defaultCorners(fieldState.image.width, fieldState.image.height);
  drawField();
};
el("field-cancel").onclick = () => show("setup");
el("field-open").onclick = openFieldEditor;

el("field-save").onclick = async () => {
  try {
    const saved = await api("/api/field", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        video: state.video.name,
        start: state.segment.start,
        end: state.segment.end,
        frame_index: fieldState.frameIndex,
        corners: fieldState.corners,
      }),
    });
    if (saved.warning) {
      // Saved either way - this is the frame disagreeing with the chosen
      // orientation, which is worth seeing rather than silently accepting.
      el("field-note").textContent = `Saved, but ${saved.warning}`;
      return;
    }
    show("setup");
    await refreshSetup();
  } catch (error) {
    showError(error.message);
  }
};

async function refreshSetup() {
  if (!(state.video && state.segment)) return;
  try {
    const setup = await api(
      `/api/setup?video=${encodeURIComponent(state.video.name)}` +
      `&start=${state.segment.start}&end=${state.segment.end}`);
    state.setup = setup;
    el("field-open").className = `status ${setup.field ? "done" : "pending"}`;
    el("field-open").textContent = setup.field ? "Field selected ✓" : "Select field";
    el("seed-open").className = `status ${setup.robots ? "done" : "pending"}`;
    el("seed-open").textContent = setup.robots
      ? `Robots selected ✓ (${setup.robot_count})` : "Select robots";
  } catch (error) {
    state.setup = null;
  }
  updateStart();
}
