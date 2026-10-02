/* The drive simulator on /tools/sim.

   A tank drivetrain, simulated from the two numbers that define a motor - its
   free speed and its stall torque - rather than from a top speed typed in by
   hand. That is the whole point of the page: a gear ratio, a wheel size or an
   extra ten pounds have to *do* something, and they only do if the model has
   force and mass in it.

   Everything here is in SI internally (metres, kilograms, newtons, radians)
   and converted at the edges, because mixing inches into F = ma is how sign
   and factor-of-twelve errors get in. The panel and the readouts are in the
   units a team actually uses.

   Field coordinates are canvas coordinates: x right, y down, heading measured
   from +x. With y pointing down, a positive rotation reads as clockwise on
   screen, and the standard rigid-body formulas come through unchanged. */
(() => {
  'use strict';

  const root = document.getElementById('sim');
  if (!root) return;
  const CONFIG = JSON.parse(root.dataset.config);
  const canvas = root.querySelector('#sim-field');
  const ctx = canvas.getContext('2d');

  const IN = 0.0254;
  const LB = 0.45359237;
  const G = 9.80665;
  const RPM = Math.PI / 30;          // rev/min -> rad/s
  const STORE = 'vex.drivesim.' + CONFIG.program;
  const DT = 0.002;                  // physics step; small enough that the
                                     // torque-speed curve stays stable

  // --- the parameters -----------------------------------------------------
  // One list drives the inputs, the saved settings and the reset, so there is
  // no second place for a parameter to be half-added.
  const MOTOR_OPTIONS = CONFIG.motors.map((m) => ({ value: m.key, label: m.label }));
  const PARAMS = [
    { key: 'motor', group: 'Drivetrain', label: 'Motor', kind: 'select', options: MOTOR_OPTIONS },
    { key: 'motorsPerSide', group: 'Drivetrain', label: 'Motors a side', min: 1, max: 4, step: 1 },
    { key: 'ratio', group: 'Drivetrain', label: 'Gear ratio', min: 0.4, max: 3, step: 0.01,
      note: 'Motor turns per wheel turn. Above 1 is geared down for torque, below 1 is geared up for speed.' },
    { key: 'wheel', group: 'Drivetrain', label: 'Wheel', unit: 'in', min: 2, max: 5, step: 0.25 },

    { key: 'mass', group: 'Robot', label: 'Weight', unit: 'lb', min: 2, max: 40, step: 0.5 },
    { key: 'length', group: 'Robot', label: 'Length', unit: 'in', min: 6, max: 24, step: 0.5 },
    { key: 'width', group: 'Robot', label: 'Width', unit: 'in', min: 6, max: 24, step: 0.5 },
    { key: 'track', group: 'Robot', label: 'Track', unit: 'in', min: 4, max: 22, step: 0.5,
      note: 'Left wheels to right wheels. A wider track turns more slowly and more steadily.' },
    { key: 'wheelbase', group: 'Robot', label: 'Wheelbase', unit: 'in', min: 2, max: 22, step: 0.5,
      note: 'Front wheels to back. A longer wheelbase drives straighter and fights you harder in a turn.' },

    { key: 'friction', group: 'Surface and battery', label: 'Grip', min: 0.3, max: 1.6, step: 0.05,
      note: 'How well the wheels hold the tiles. Traction wheels grip about 1.1 on foam, omnis about 0.8.' },
    { key: 'efficiency', group: 'Surface and battery', label: 'Efficiency', unit: '%', min: 50, max: 100, step: 1,
      note: 'How much of the motor\u2019s torque survives the gears and chain.' },
    { key: 'rolling', group: 'Surface and battery', label: 'Rolling drag', min: 0, max: 0.25, step: 0.005,
      note: 'Tiles, bearings and gearbox together. This is what stops a robot left to coast.' },
    { key: 'scrub', group: 'Surface and battery', label: 'Turning scrub', min: 0, max: 1.2, step: 0.05,
      note: 'How hard the wheels have to skid sideways to turn. Zero is a pivot on the spot.' },
    { key: 'battery', group: 'Surface and battery', label: 'Battery', unit: 'V',
      min: CONFIG.battery.min, max: CONFIG.battery.max, step: 0.1,
      note: 'A full pack spins the motors past their rated speed; a flat one never reaches it.' },

    { key: 'control', group: 'Driver feel', label: 'Controls', kind: 'select',
      options: [{ value: 'tank', label: 'Tank (W/S, I/K)' }, { value: 'arcade', label: 'Arcade (WASD)' }],
      note: 'Tank drives each side on its own. Arcade drives forwards and turns, and mixes the two.' },
    { key: 'brake', group: 'Driver feel', label: 'On release', kind: 'select',
      options: [{ value: 'brake', label: 'Brake' }, { value: 'coast', label: 'Coast' }],
      note: 'Brake holds the motors against the motion. Coast lets the robot roll on.' },
    { key: 'curve', group: 'Driver feel', label: 'Input curve', min: 1, max: 4, step: 0.1,
      note: '1 is linear. Higher makes small stick movements gentler.' },
    { key: 'ramp', group: 'Driver feel', label: 'Ramp', unit: 's', min: 0, max: 1, step: 0.02,
      note: 'How long a key takes to reach full power. A key is either down or up, so this is what gives it a stick\u2019s feel.' },
  ];

  if (CONFIG.layout) {
    PARAMS.push(
      { key: 'goalSize', group: 'Field', label: 'Goal radius', unit: 'in', min: 4, max: 18, step: 0.5,
        note: 'The Goal positions come from the game manual; this does not. Measure a real Goal and put it here.' },
      { key: 'midfield', group: 'Field', label: 'Midfield', kind: 'select',
        options: [{ value: 'solid', label: 'Solid' },
                  { value: 'over', label: 'Drive over' },
                  { value: 'off', label: 'Off' }],
        note: 'Solid makes the Midfield a wall. Drive over makes its edge a lip you need speed to climb, which is what lets a robot park in it.' });
  }

  const params = load();

  function load() {
    const out = Object.assign({}, CONFIG.defaults);
    try {
      const saved = JSON.parse(localStorage.getItem(STORE) || '{}');
      for (const spec of PARAMS) {
        if (!(spec.key in saved)) continue;
        const value = saved[spec.key];
        if (spec.kind === 'select') {
          if (spec.options.some((o) => o.value === value)) out[spec.key] = value;
        } else if (typeof value === 'number' && isFinite(value)) {
          out[spec.key] = Math.min(spec.max, Math.max(spec.min, value));
        }
      }
    } catch (err) {
      /* A browser with storage blocked is not a reason to have no simulator. */
    }
    return out;
  }

  function save() {
    try { localStorage.setItem(STORE, JSON.stringify(params)); } catch (err) { /* as above */ }
  }

  // --- what the parameters mean -------------------------------------------
  // Everything the physics needs, derived once whenever a parameter changes
  // rather than recomputed sixty times a second.
  let derived = describe(params);

  function describe(p) {
    const motor = CONFIG.motors.find((m) => m.key === p.motor) || CONFIG.motors[0];
    const radius = (p.wheel / 2) * IN;
    const mass = p.mass * LB;
    const track = p.track * IN;
    const wheelbase = p.wheelbase * IN;
    const freeMotor = motor.rpm * RPM;
    // A rectangular slab about its centre. Close enough for a robot whose
    // mass is spread over its base, and it is the term that decides whether
    // a turn snaps or swings.
    const inertia = mass * (Math.pow(p.length * IN, 2) + Math.pow(p.width * IN, 2)) / 12;
    const gearing = p.ratio * (p.efficiency / 100);
    return {
      motor, radius, mass, track, wheelbase, inertia, freeMotor,
      // Force one side makes at a standstill, and the most the tiles will let
      // it put down. Whichever is smaller is what the robot actually has.
      stallForce: p.motorsPerSide * motor.stall * gearing / radius,
      traction: p.friction * mass * G / 2,
      // A motor spins past its rated speed on a fresh pack and short of it on
      // a flat one, so the quoted free speed follows the battery setting;
      // otherwise the measured top speed can come out above the theoretical
      // one, which reads as a bug even though it is not.
      freeSpeed: (freeMotor / p.ratio) * radius * (p.battery / CONFIG.nominal),
      wheelRpm: (motor.rpm / p.ratio) * (p.battery / CONFIG.nominal),
      gearing,
    };
  }

  // --- the field ----------------------------------------------------------
  // The layout arrives in field inches, the same numbers the tracker uses, and
  // is converted once: everything from here on is metres.
  const field = { w: CONFIG.field.width * IN, h: CONFIG.field.height * IN };
  const LAYOUT = CONFIG.layout;
  const GOALS = LAYOUT ? LAYOUT.goals.map((goal) => ({
    kind: goal.kind, pair: goal.pair, x: goal.x * IN, y: goal.y * IN,
  })) : [];
  // The Midfield is a square stood on one corner, so it is described by its
  // centre and the distance to a vertex rather than as a general polygon:
  // inside is |dx| + |dy| <= reach, which is the same test the tracker's
  // `in_midfield` makes.
  const TOGGLES = LAYOUT ? LAYOUT.toggles.map((toggle) => ({
    name: toggle.name, x: toggle.x * IN, y: toggle.y * IN,
  })) : [];
  const MIDFIELD = LAYOUT && LAYOUT.midfield.length === 4 ? (() => {
    const points = LAYOUT.midfield.map(([x, y]) => [x * IN, y * IN]);
    const x = (points[0][0] + points[2][0]) / 2;
    const y = (points[1][1] + points[3][1]) / 2;
    return { x, y, reach: Math.max(...points.map(([px, py]) => Math.abs(px - x) + Math.abs(py - y))) };
  })() : null;
  // How far a robot has to climb to get into the Midfield, and over how much
  // travel. Neither is a figure from the manual - the lip is not dimensioned
  // in what we have - so they are constants here rather than sliders nobody
  // could set honestly.
  const LIP = 1.5 * IN;
  const CLIMB = 3 * IN;

  function goalRadius(goal) {
    // The tall Goal reads as the bigger obstacle it is; the rest share the
    // one radius the panel sets.
    return params.goalSize * IN * (goal.kind === 'tall' ? 1.25 : 1);
  }

  // --- the robot ----------------------------------------------------------
  const robot = blank();
  let trail = [];
  let showTrail = true;
  let running = true;

  function blank() {
    const start = CONFIG.start || { x: CONFIG.field.width / 2, y: CONFIG.field.height / 2, heading: -90 };
    return {
      x: start.x * IN, y: start.y * IN,
      theta: start.heading * Math.PI / 180,
      v: 0, omega: 0,
      current: 0, slip: false, volts: params.battery,
      cmdL: 0, cmdR: 0,
    };
  }

  function reset() {
    Object.assign(robot, blank());
    trail = [];
  }

  // --- the model ----------------------------------------------------------
  // One step of the drivetrain. `r` is a robot-shaped object rather than the
  // robot itself so the same code can answer "how fast does this build get to
  // five feet" without anything appearing on screen.
  function step(r, p, d, cmdL, cmdR, dt) {
    // Under load the pack sags, and a sagging pack is why a fast drive gets
    // slower late in a match. Current is taken from the previous step, which
    // is a step out of date and invisible at 500Hz.
    const volts = Math.max(CONFIG.nominal * 0.4, p.battery - r.current * CONFIG.resistance);
    const duty = volts / CONFIG.nominal;
    const stall = d.motor.stall;

    let current = 0;
    let slip = false;
    const force = [0, 0];
    const cmds = [cmdL, cmdR];
    for (let side = 0; side < 2; side++) {
      const cmd = cmds[side];
      // Left wheels sit on the outside of a clockwise turn, so they run
      // faster than the centre by omega * track/2; the right side by as much
      // less. This is the only place the two sides differ.
      const vSide = r.v + (side === 0 ? 1 : -1) * r.omega * d.track / 2;
      const speed = (vSide / d.radius) * p.ratio / d.freeMotor;   // share of free speed

      let torque;
      if (cmd === 0 && p.brake === 'coast') {
        torque = 0;                       // freewheeling: nothing holds it back
      } else {
        // A motor's torque is what is left of stall once back-EMF has eaten
        // into the applied voltage. Commanding nothing while moving is
        // therefore a brake, not a coast, which is exactly how the real
        // brake mode behaves.
        torque = stall * (cmd * duty - speed);
      }
      // The motor's own current limit means it can never beat its stall
      // torque, however far the command and the back-EMF diverge.
      torque = Math.max(-stall, Math.min(stall, torque));
      current += Math.abs(torque) / stall * d.motor.amps * p.motorsPerSide;

      let f = p.motorsPerSide * torque * d.gearing / d.radius;
      if (Math.abs(f) > d.traction) {     // wheels break loose
        f = Math.sign(f) * d.traction;
        slip = true;
      }
      force[side] = f;
    }

    // tanh rather than a sign test: at a standstill the resisting force has
    // to fade out, or it flips sign every step and the robot buzzes.
    const rolling = p.rolling * d.mass * G * Math.tanh(r.v / 0.05);
    const accel = (force[0] + force[1] - rolling) / d.mass;

    // Turning a tank drive means skidding every wheel sideways. The moment
    // that resists it is the lateral friction at each wheel acting at its
    // distance from the centre, which is what makes a long robot turn slowly.
    const scrub = Math.tanh(r.omega / 0.15) * p.scrub * p.friction * d.mass * G * d.wheelbase / 2;
    const alpha = ((force[0] - force[1]) * d.track / 2 - scrub) / d.inertia;

    r.v += accel * dt;
    r.omega += alpha * dt;
    if (cmdL === 0 && cmdR === 0) {
      // Stop rather than creep: below these the robot is not moving in any
      // sense a driver would recognise.
      if (Math.abs(r.v) < 0.01) r.v = 0;
      if (Math.abs(r.omega) < 0.03) r.omega = 0;
    }
    r.x += r.v * Math.cos(r.theta) * dt;
    r.y += r.v * Math.sin(r.theta) * dt;
    r.theta += r.omega * dt;
    r.current = current;
    r.volts = volts;
    r.slip = slip;
  }

  function walls(r, p) {
    const c = Math.cos(r.theta), s = Math.sin(r.theta);
    const L = p.length * IN / 2, W = p.width * IN / 2;
    // Half the footprint along each axis, for a rectangle turned to theta.
    const hx = L * Math.abs(c) + W * Math.abs(s);
    const hy = L * Math.abs(s) + W * Math.abs(c);
    let hitX = false, hitY = false;
    if (r.x < hx) { r.x = hx; hitX = true; } else if (r.x > field.w - hx) { r.x = field.w - hx; hitX = true; }
    if (r.y < hy) { r.y = hy; hitY = true; } else if (r.y > field.h - hy) { r.y = field.h - hy; hitY = true; }
    // A wall takes away the motion into it and leaves what runs along it, so
    // a robot pressed at an angle slides along the perimeter instead of
    // sticking to it. A tank robot only moves along its heading, so what is
    // left is that heading's share of the free direction.
    if (hitX) { r.v *= s * s; r.omega *= 0.6; }
    if (hitY) { r.v *= c * c; r.omega *= 0.6; }
  }

  // The robot's four corners, which is what every collision here is against.
  function corners(r, p) {
    const c = Math.cos(r.theta), s = Math.sin(r.theta);
    const L = p.length * IN / 2, W = p.width * IN / 2;
    return [[1, 1], [1, -1], [-1, -1], [-1, 1]].map(([along, across]) => [
      r.x + along * L * c - across * W * s,
      r.y + along * L * s + across * W * c,
    ]);
  }

  // Push the robot out along `n` and take away the motion into it, leaving
  // what runs along it - the same response as a wall, so a robot pressed on a
  // Goal slides round it instead of sticking.
  function deflect(r, nx, ny, depth) {
    r.x += nx * depth;
    r.y += ny * depth;
    const along = Math.cos(r.theta) * nx + Math.sin(r.theta) * ny;
    r.v *= 1 - along * along;
  }

  // A rectangle against a circle: the closest point on the rectangle to the
  // centre decides both whether they touch and which way to push.
  function hitGoal(r, p, goal) {
    const radius = goalRadius(goal);
    const c = Math.cos(r.theta), s = Math.sin(r.theta);
    const dx = goal.x - r.x, dy = goal.y - r.y;
    // Into the robot's own frame, where the rectangle is axis-aligned.
    const along = dx * c + dy * s;
    const across = -dx * s + dy * c;
    const L = p.length * IN / 2, W = p.width * IN / 2;
    const nearAlong = Math.max(-L, Math.min(L, along));
    const nearAcross = Math.max(-W, Math.min(W, across));
    let offAlong = along - nearAlong, offAcross = across - nearAcross;
    let distance = Math.hypot(offAlong, offAcross);
    if (distance >= radius) return;
    if (distance < 1e-6) {
      // Centre swallowed by the robot: push it out the nearest side.
      offAlong = along >= 0 ? 1 : -1;
      offAcross = 0;
      distance = 1e-6;
    }
    // Back to field axes, pointing from the Goal to the robot.
    const ux = -(offAlong / distance), uy = -(offAcross / distance);
    deflect(r, ux * c - uy * s, ux * s + uy * c, radius - distance);
  }

  const insideMidfield = (x, y) =>
    Math.abs(x - MIDFIELD.x) + Math.abs(y - MIDFIELD.y) <= MIDFIELD.reach;

  // The Midfield, in whichever of the three ways the panel asks for.
  function hitMidfield(r, p, dt) {
    if (!MIDFIELD || p.midfield === 'off') return;
    // How far the robot's centre is from the edge, positive outside. The
    // diamond's edges run at 45 degrees, so the L1 distance is a root-two
    // longer than the real one.
    const edge = (Math.abs(r.x - MIDFIELD.x) + Math.abs(r.y - MIDFIELD.y) - MIDFIELD.reach) / Math.SQRT2;

    if (p.midfield === 'over') {
      // Climbing the lip, written as the ramp it is rather than as a speed
      // threshold: resisting at g * slope over the CLIMB the lip is crossed
      // in costs exactly the m*g*h of getting up it, so a robot with the
      // speed rolls over and one without stops on it and slides back off.
      if (Math.abs(edge) > CLIMB / 2) return;
      const slowing = G * (LIP / CLIMB) * dt;
      r.v -= Math.sign(r.v) * Math.min(Math.abs(r.v), slowing);
      return;
    }

    // Solid: the nearest of the four edges pushes back. Their normals are the
    // diagonals, so which one is nearest follows from the signs of dx and dy.
    const points = corners(r, p);
    let depth = 0;
    for (const [x, y] of points) {
      const past = (MIDFIELD.reach - (Math.abs(x - MIDFIELD.x) + Math.abs(y - MIDFIELD.y))) / Math.SQRT2;
      if (past > depth) depth = past;
    }
    if (depth <= 0) return;
    const nx = (r.x >= MIDFIELD.x ? 1 : -1) / Math.SQRT2;
    const ny = (r.y >= MIDFIELD.y ? 1 : -1) / Math.SQRT2;
    deflect(r, nx, ny, depth);
  }

  function obstacles(r, p, dt) {
    for (const goal of GOALS) hitGoal(r, p, goal);
    hitMidfield(r, p, dt);
  }

  // --- what the keys are asking for ---------------------------------------
  // Every key either mode uses. They are held as themselves rather than as
  // what they mean, because what they mean depends on the mode and switching
  // mode with a key down must not leave a side stuck on.
  const KEYS = new Set(['w', 'a', 's', 'd', 'i', 'k',
                        'arrowup', 'arrowdown', 'arrowleft', 'arrowright']);
  const held = new Set();
  // Two axes. Tank reads them as the left and right sides, arcade as drive
  // and turn; both are ramped and curved the same way before they are used.
  const command = { a: 0, b: 0 };

  function axis(up, down) {
    return (up.some((key) => held.has(key)) ? 1 : 0) - (down.some((key) => held.has(key)) ? 1 : 0);
  }

  function targets() {
    if (params.control === 'arcade') {
      return [axis(['w', 'arrowup'], ['s', 'arrowdown']),
              axis(['d', 'arrowright'], ['a', 'arrowleft'])];
    }
    return [axis(['w'], ['s']), axis(['i', 'arrowup'], ['k', 'arrowdown'])];
  }

  // What the two axes ask of each side of the drivetrain. Full forward and a
  // full turn together would ask for twice what a motor has, so the pair is
  // scaled down rather than clipped: turning while driving flat out slows the
  // inside wheels instead of doing nothing.
  function sides() {
    const first = curved(command.a), second = curved(command.b);
    if (params.control !== 'arcade') return [first, second];
    const left = first + second, right = first - second;
    const over = Math.max(1, Math.abs(left), Math.abs(right));
    return [left / over, right / over];
  }

  function ramp(now, want, dt) {
    if (params.ramp <= 0) return want;
    const limit = (2 / params.ramp) * dt;   // full reverse to full forward
    return now + Math.max(-limit, Math.min(limit, want - now));
  }

  function curved(value) {
    if (params.curve <= 1) return value;
    return Math.sign(value) * Math.pow(Math.abs(value), params.curve);
  }

  window.addEventListener('keydown', (event) => {
    const tag = (event.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
    const key = event.key.toLowerCase();
    if (KEYS.has(key)) {
      held.add(key);
      event.preventDefault();             // the arrow keys would scroll
      return;
    }
    if (key === 'r') { reset(); event.preventDefault(); }
    else if (key === 't') { toggleTrail(); event.preventDefault(); }
    else if (key === ' ') { stop(); event.preventDefault(); }
  });

  window.addEventListener('keyup', (event) => {
    held.delete(event.key.toLowerCase());
  });

  // Keys held when the window loses focus would otherwise stay held forever.
  window.addEventListener('blur', () => held.clear());

  function stop() {
    held.clear();
    command.a = command.b = 0;
    robot.v = 0;
    robot.omega = 0;
  }

  function toggleTrail() {
    showTrail = !showTrail;
    if (!showTrail) trail = [];
    const button = root.querySelector('[data-action="trail"]');
    if (button) button.setAttribute('aria-pressed', String(showTrail));
  }

  // --- the loop -----------------------------------------------------------
  let last = 0;
  let carry = 0;
  let sinceTrail = 0;

  function frame(now) {
    requestAnimationFrame(frame);
    const dt = last ? Math.min(0.1, (now - last) / 1000) : 0;   // a backgrounded
    last = now;                                                 // tab must not
    if (!running || dt <= 0) { paint(); return; }               // fast-forward

    carry += dt;
    let drive = [0, 0];
    while (carry >= DT) {
      const want = targets();
      command.a = ramp(command.a, want[0], DT);
      command.b = ramp(command.b, want[1], DT);
      drive = sides();
      step(robot, params, derived, drive[0], drive[1], DT);
      walls(robot, params);
      obstacles(robot, params, DT);
      carry -= DT;
    }
    // The bars and the wheels show what each side was actually given, which
    // in arcade is the mix rather than either axis.
    robot.cmdL = drive[0];
    robot.cmdR = drive[1];

    sinceTrail += dt;
    if (showTrail && sinceTrail > 0.04 && Math.abs(robot.v) > 0.02) {
      sinceTrail = 0;
      trail.push([robot.x, robot.y]);
      if (trail.length > 1200) trail.shift();
    }
    paint();
    readout();
  }

  // --- drawing ------------------------------------------------------------
  // Colours come from the stylesheet rather than from here, so the field is
  // black and red under V5 and white and blue under IQ without this file
  // knowing either palette.
  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  let theme = readTheme();

  function readTheme() {
    return {
      floor: token('--sunken'), line: token('--line'), wall: token('--muted'),
      body: token('--panel-alt'), edge: token('--text'), accent: token('--accent'),
      trail: token('--link'), slip: token('--warn'),
      // The alliances' own colours, which are the game's rather than the
      // theme's and so stay red and blue in both programs.
      red: token('--alliance-red'), blue: token('--alliance-blue'),
      neutral: token('--muted'),
    };
  }

  let scale = 1, offX = 0, offY = 0;
  let view = { width: 0, height: 0 };
  const stage = root.querySelector('.sim-stage');
  const layout = root.querySelector('.sim-layout');
  const frameBox = canvas.parentElement;

  // The field takes whatever height the rest of the page has left. That is
  // searched for rather than calculated: the button row under the field wraps
  // when the field is narrow, so the space the field can have depends on how
  // much it takes, and a formula chases its own tail. Asking the browser
  // whether a given height fits has no such problem.
  const ASPECT = CONFIG.field.width / CONFIG.field.height;

  function fits() {
    const page = document.documentElement;
    return page.scrollHeight <= page.clientHeight;
  }

  function set(height) {
    root.style.setProperty('--sim-field', Math.round(height) + 'px');
  }

  function fitField() {
    if (window.matchMedia('(max-width: 1080px)').matches) {
      root.style.removeProperty('--sim-field');   // stacked: the stylesheet decides
      return;
    }
    // Never wider than its share of the page, or the sliders lose the second
    // column that is the reason they all fit on one screen.
    let high = Math.max(240, (layout.clientWidth * 0.58 - 30) / ASPECT);
    set(high);
    if (fits()) return;
    let low = 240;
    for (let step = 0; step < 9; step++) {      // to within a couple of pixels
      const middle = (low + high) / 2;
      set(middle);
      if (fits()) low = middle; else high = middle;
    }
    set(low);
  }

  // The canvas has the field's own aspect, so this is a straight scale with a
  // hair of margin; the backing store is matched to the device's pixels so
  // the lines are not soft on a laptop screen.
  function resize() {
    const dpr = window.devicePixelRatio || 1;
    const box = canvas.getBoundingClientRect();
    view = { width: box.width, height: box.height };
    canvas.width = Math.round(box.width * dpr);
    canvas.height = Math.round(box.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    scale = Math.min(box.width / field.w, box.height / field.h) * 0.98;
    offX = (box.width - field.w * scale) / 2;
    offY = (box.height - field.h * scale) / 2;
  }

  const px = (m) => m * scale;

  function paint() {
    // The size is whatever the last resize measured: reading the element's
    // box here would force a layout on every frame.
    ctx.clearRect(0, 0, view.width, view.height);
    ctx.save();
    ctx.translate(offX, offY);

    ctx.fillStyle = theme.floor;
    ctx.fillRect(0, 0, px(field.w), px(field.h));

    // The tile grid is the scale reference: every square is two feet.
    const tile = CONFIG.field.tile * IN;
    ctx.strokeStyle = theme.line;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let x = tile; x < field.w - 1e-6; x += tile) {
      ctx.moveTo(px(x), 0); ctx.lineTo(px(x), px(field.h));
    }
    for (let y = tile; y < field.h - 1e-6; y += tile) {
      ctx.moveTo(0, px(y)); ctx.lineTo(px(field.w), px(y));
    }
    ctx.stroke();

    ctx.strokeStyle = theme.wall;
    ctx.lineWidth = 2;
    ctx.strokeRect(0, 0, px(field.w), px(field.h));

    drawLayout();

    if (showTrail && trail.length > 1) {
      ctx.strokeStyle = theme.trail;
      ctx.globalAlpha = 0.5;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(px(trail[0][0]), px(trail[0][1]));
      for (let i = 1; i < trail.length; i++) ctx.lineTo(px(trail[i][0]), px(trail[i][1]));
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    drawRobot();
    ctx.restore();
  }

  // The field the game is actually played on, drawn from the same coordinates
  // the tracker attributes a stack to a Goal with.
  function drawLayout() {
    if (!LAYOUT) return;

    // Quadrants are triangles, not quarters: the Autonomous Line runs corner
    // to corner, so the boundaries are the diagonals.
    ctx.strokeStyle = theme.line;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, 0); ctx.lineTo(px(field.w), px(field.h));
    ctx.moveTo(px(field.w), 0); ctx.lineTo(0, px(field.h));
    ctx.stroke();

    if (MIDFIELD) {
      const points = [[0, -1], [1, 0], [0, 1], [-1, 0]].map(([dx, dy]) =>
        [px(MIDFIELD.x + dx * MIDFIELD.reach), px(MIDFIELD.y + dy * MIDFIELD.reach)]);
      ctx.beginPath();
      ctx.moveTo(points[0][0], points[0][1]);
      for (const [x, y] of points.slice(1)) ctx.lineTo(x, y);
      ctx.closePath();
      ctx.fillStyle = theme.edge;
      // A hint of fill for a raised area, not a block: the robot is meant to
      // be visible when it is parked in there.
      ctx.globalAlpha = params.midfield === 'off' ? 0.04 : 0.09;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = theme.edge;
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    // One Toggle on the middle of each wall, drawn just inside it.
    const long = 26 * IN, thick = 2.5 * IN;
    ctx.fillStyle = theme.neutral;
    for (const toggle of TOGGLES) {
      const upright = toggle.x <= 0 || toggle.x >= field.w - 1e-6;
      const w = upright ? thick : long, h = upright ? long : thick;
      let x = toggle.x, y = toggle.y;
      if (x <= 0) x = thick / 2; else if (x >= field.w - 1e-6) x = field.w - thick / 2;
      if (y <= 0) y = thick / 2; else if (y >= field.h - 1e-6) y = field.h - thick / 2;
      ctx.fillRect(px(x - w / 2), px(y - h / 2), px(w), px(h));
    }

    for (const goal of GOALS) {
      const colour = goal.pair ? (goal.pair === LAYOUT.red_pair ? theme.red : theme.blue)
                               : theme.neutral;
      const radius = px(goalRadius(goal));
      ctx.beginPath();
      ctx.arc(px(goal.x), px(goal.y), radius, 0, Math.PI * 2);
      ctx.fillStyle = colour;
      ctx.globalAlpha = 0.22;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = colour;
      ctx.lineWidth = 2;
      ctx.stroke();
      if (goal.kind === 'tall') {       // the one Goal that is not short
        ctx.beginPath();
        ctx.arc(px(goal.x), px(goal.y), radius * 0.6, 0, Math.PI * 2);
        ctx.stroke();
      }
    }
  }

  function drawRobot() {
    const L = px(params.length * IN), W = px(params.width * IN);
    ctx.save();
    ctx.translate(px(robot.x), px(robot.y));
    ctx.rotate(robot.theta);

    ctx.fillStyle = theme.body;
    ctx.strokeStyle = robot.slip ? theme.slip : theme.edge;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.rect(-L / 2, -W / 2, L, W);
    ctx.fill();
    ctx.stroke();

    // Wheels at the corners of the track and wheelbase, shaded by how hard
    // that side is being driven: the drivetrain's state, visible at a glance.
    const halfTrack = px(params.track * IN) / 2;
    const halfBase = px(params.wheelbase * IN) / 2;
    const wheelL = Math.max(4, px(params.wheel * IN));
    for (const side of [-1, 1]) {           // -1 left (screen), +1 right
      const cmd = side === -1 ? robot.cmdL : robot.cmdR;
      ctx.globalAlpha = 0.25 + 0.75 * Math.min(1, Math.abs(cmd));
      ctx.fillStyle = theme.accent;
      for (const along of [-1, 1]) {
        ctx.fillRect(along * halfBase - wheelL / 2, side * halfTrack - 2.5, wheelL, 5);
      }
    }
    ctx.globalAlpha = 1;

    // Which way is forward.
    ctx.fillStyle = theme.accent;
    ctx.beginPath();
    ctx.moveTo(L / 2, 0);
    ctx.lineTo(L / 2 - Math.min(14, L * 0.3), -Math.min(9, W * 0.28));
    ctx.lineTo(L / 2 - Math.min(14, L * 0.3), Math.min(9, W * 0.28));
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  // --- the numbers on screen ----------------------------------------------
  const out = {
    speed: root.querySelector('#sim-speed'),
    turn: root.querySelector('#sim-turn'),
    volts: root.querySelector('#sim-volts'),
    state: root.querySelector('#sim-state'),
    barL: root.querySelector('#sim-bar-l'),
    barR: root.querySelector('#sim-bar-r'),
  };

  function readout() {
    const fps = robot.v / IN / 12;
    out.speed.textContent = Math.abs(fps).toFixed(2);
    out.turn.textContent = Math.round(robot.omega * 180 / Math.PI);
    out.volts.textContent = robot.volts.toFixed(1);
    out.state.textContent = robot.slip ? 'wheels slipping' : '';
    out.state.classList.toggle('on', robot.slip);
    bar(out.barL, robot.cmdL);
    bar(out.barR, robot.cmdR);
  }

  function bar(element, value) {
    if (!element) return;
    element.querySelector('span').style.width = Math.min(100, Math.abs(value) * 100) + '%';
    element.classList.toggle('back', value < -0.01);
  }

  // --- what this build can do ---------------------------------------------
  // Measured by running the same model rather than by a formula, so the
  // figures include the losses and the battery sag the driver will feel.
  function sprint(p, d) {
    const test = { x: 0, y: 0, theta: 0, v: 0, omega: 0, current: 0, slip: false, volts: p.battery };
    const goal = 60 * IN;
    let time = 0, reached = null;
    for (let i = 0; i < 2500; i++) {         // five seconds is plenty
      step(test, p, d, 1, 1, DT);
      time += DT;
      if (reached === null && test.x >= goal) reached = time;
    }
    return { sprint: reached, top: test.v };
  }

  function stats() {
    const d = derived;
    const run = sprint(params, d);
    const limited = d.stallForce > d.traction;
    // Short enough to sit in a card: the numbers are the point, and the long
    // version of each is on the help page the parameters explain themselves on.
    const rows = [
      ['Free speed', (d.freeSpeed / IN / 12).toFixed(2) + ' ft/s',
       Math.round(d.wheelRpm) + ' rpm, no load'],
      ['Top speed', (run.top / IN / 12).toFixed(2) + ' ft/s', 'with drag and sag'],
      ['0 to 5 ft', run.sprint === null ? 'over 5 s' : run.sprint.toFixed(2) + ' s', 'from a standstill'],
      ['Limited by', limited ? 'traction' : 'motors',
       limited ? 'wheels slip first' : 'motors run out first'],
      ['Pushing force', (2 * Math.min(d.stallForce, d.traction) / (LB * G)).toFixed(1) + ' lb',
       'at a standstill'],
    ];
    const host = root.querySelector('#sim-stats');
    host.innerHTML = '';
    for (const [name, value, note] of rows) {
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = '<span class="k"></span><span class="v"></span><span class="f"></span>';
      card.querySelector('.k').textContent = name;
      card.querySelector('.v').textContent = value;
      card.querySelector('.f').textContent = note;
      host.appendChild(card);
    }
  }

  // --- the control panel --------------------------------------------------
  function buildPanel() {
    const host = root.querySelector('#sim-params');
    host.innerHTML = '';
    let group = null, body = null;
    for (const spec of PARAMS) {
      if (spec.group !== group) {
        group = spec.group;
        const section = document.createElement('section');
        section.className = 'sim-group';
        const title = document.createElement('h3');
        title.textContent = group;
        section.appendChild(title);
        body = document.createElement('div');
        body.className = 'sim-group-body';
        section.appendChild(body);
        host.appendChild(section);
      }
      body.appendChild(spec.kind === 'select' ? selectRow(spec) : rangeRow(spec));
    }
  }

  // Rows are one line each so that every parameter is on screen beside the
  // field. What used to be a line of explanation under the slider is the
  // label's tooltip instead, and the label says so by being underlined.
  function label(spec) {
    const text = document.createElement('span');
    text.className = 'sim-label' + (spec.note ? ' more' : '');
    text.textContent = spec.label + (spec.unit ? ' (' + spec.unit + ')' : '');
    if (spec.note) text.title = spec.note;
    return text;
  }

  function rangeRow(spec) {
    const row = document.createElement('label');
    row.className = 'sim-row';
    row.appendChild(label(spec));

    const slider = document.createElement('input');
    slider.type = 'range';
    slider.min = spec.min; slider.max = spec.max; slider.step = spec.step;
    slider.value = params[spec.key];

    const number = document.createElement('input');
    number.type = 'number';
    number.className = 'sim-num';
    number.min = spec.min; number.max = spec.max; number.step = spec.step;
    number.value = params[spec.key];

    // The slider is for exploring, the box for a value someone already knows.
    // Each writes the other so they can never disagree.
    const apply = (raw, echo) => {
      let value = parseFloat(raw);
      if (!isFinite(value)) return;
      value = Math.min(spec.max, Math.max(spec.min, value));
      params[spec.key] = value;
      echo.value = value;
      changed();
    };
    slider.addEventListener('input', () => apply(slider.value, number));
    number.addEventListener('change', () => apply(number.value, slider));

    row.appendChild(slider);
    row.appendChild(number);
    row.dataset.key = spec.key;
    return row;
  }

  function selectRow(spec) {
    const row = document.createElement('label');
    row.className = 'sim-row sim-row-wide';
    row.appendChild(label(spec));
    const select = document.createElement('select');
    for (const option of spec.options) {
      const element = document.createElement('option');
      element.value = option.value;
      element.textContent = option.label;
      select.appendChild(element);
    }
    select.value = params[spec.key];
    select.addEventListener('change', () => { params[spec.key] = select.value; changed(); });
    row.appendChild(select);
    row.dataset.key = spec.key;
    return row;
  }

  function refreshPanel() {
    for (const row of root.querySelectorAll('.sim-row')) {
      const value = params[row.dataset.key];
      for (const input of row.querySelectorAll('input, select')) input.value = value;
    }
  }

  let mode = null;

  function changed() {
    derived = describe(params);
    if (params.control !== mode) {
      mode = params.control;
      root.dataset.control = mode;       // the key legend follows this
      stop();
    }
    stats();
    save();
    restyle();
  }

  // A parameter can change the height of the panel - a figure that wraps onto
  // another line - and with it how much room the field has. Re-measuring on
  // every drag of a slider would be wasteful, so it happens only when the
  // page has stopped fitting or has obvious room to spare.
  function restyle() {
    const slack = document.documentElement.clientHeight - stage.getBoundingClientRect().bottom;
    if (!fits() || slack > 28) fitField();
    resize();
    paint();
  }

  function buildPresets() {
    const host = root.querySelector('#sim-presets');
    for (const [name, values] of Object.entries(CONFIG.presets)) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'small secondary';
      button.textContent = name;
      button.addEventListener('click', () => {
        Object.assign(params, values);
        refreshPanel();
        changed();
      });
      host.appendChild(button);
    }
  }

  root.addEventListener('click', (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    if (action === 'reset') reset();
    else if (action === 'trail') toggleTrail();
    else if (action === 'pause') {
      running = !running;
      button.textContent = running ? 'Pause' : 'Resume';
      button.setAttribute('aria-pressed', String(!running));
      if (running) last = 0;              // do not integrate the paused time
    } else if (action === 'defaults') {
      Object.assign(params, CONFIG.defaults);
      refreshPanel();
      changed();
    }
  });

  window.addEventListener('resize', () => { fitField(); resize(); paint(); });
  // The palette changes when the program does, and this page can be open
  // across that switch.
  new MutationObserver(() => { theme = readTheme(); paint(); })
    .observe(document.documentElement, { attributes: true, attributeFilter: ['data-program'] });

  buildPanel();
  buildPresets();
  changed();
  fitField();
  resize();
  requestAnimationFrame(frame);
})();
