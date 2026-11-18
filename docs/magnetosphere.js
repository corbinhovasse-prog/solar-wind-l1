/* Simplified 3D model of how the solar wind meets Earth's magnetic field.
   Units: Earth radii (Re). +X points to the Sun, +Y is north.
   The magnetopause size and shape come from Shue et al. (1998); the rest is illustrative. */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const el = document.getElementById("magneto-view");
const $ = id => document.getElementById(id);
const state = { bz: -5, v: 450, n: 6 };

/* ---------- physics ---------- */
const pdyn = (n, v) => 1.6726e-6 * n * v * v;                         // nPa
function shue(bz, dp) {                                                 // standoff r0 (Re) and flaring alpha
  const r0 = (10.22 + 1.29 * Math.tanh(0.184 * (bz + 8.14))) * Math.pow(dp, -1 / 6.6);
  const alpha = (0.58 - 0.007 * bz) * (1 + 0.024 * Math.log(dp));
  return { r0, alpha };
}
const mpRadius = (theta, mp) => mp.r0 * Math.pow(2 / (1 + Math.cos(theta)), mp.alpha);
const coupling = (v, bz) => bz < 0 ? Math.pow(v, 4 / 3) * Math.pow(-bz, 2 / 3) / 1e3 : 0;  // Newell, By = 0
const STRONG = coupling(700, -15);

/* ---------- scene ---------- */
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true });
} catch (e) {
  el.innerHTML = '<div class="magneto-fallback">3D needs WebGL, which this browser has turned off.</div>';
  throw e;
}
el.innerHTML = "";
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
el.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x05070d);
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 400);
camera.position.set(-9, 15, 42);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(3, 0, 0);
controls.enableDamping = true;
controls.minDistance = 8; controls.maxDistance = 120;
// phones: one finger scrolls the page as normal; two fingers rotate and pinch-zoom the model
controls.touches = { ONE: null, TWO: THREE.TOUCH.DOLLY_ROTATE };
renderer.domElement.style.touchAction = "pan-y";
// on touch screens, turn the model slowly by itself so the 3D shape is visible without any gestures
if (window.matchMedia("(pointer: coarse)").matches) {
  controls.autoRotate = true; controls.autoRotateSpeed = 0.7;
  controls.addEventListener("start", () => { controls.autoRotate = false; });
}

scene.add(new THREE.AmbientLight(0xffffff, 0.35));
const sunLight = new THREE.DirectionalLight(0xffffff, 1.6);
sunLight.position.set(50, 4, 0);
scene.add(sunLight);

const earth = new THREE.Mesh(new THREE.SphereGeometry(1, 48, 32), new THREE.MeshPhongMaterial({ color: 0x2f6fc4, shininess: 12 }));
scene.add(earth);
const sun = new THREE.Mesh(new THREE.SphereGeometry(1.6, 24, 16), new THREE.MeshBasicMaterial({ color: 0xffd36b }));
sun.position.set(46, 0, 0);
scene.add(sun);

/* aurora rings near 67 degrees magnetic latitude */
const auroraMat = new THREE.MeshBasicMaterial({ color: 0x4dff8a, transparent: true, opacity: 0.05, blending: THREE.AdditiveBlending, depthWrite: false });
const colat = 23 * Math.PI / 180;
for (const s of [1, -1]) {
  const ring = new THREE.Mesh(new THREE.TorusGeometry(Math.sin(colat) * 1.03, 0.09, 10, 64), auroraMat);
  ring.rotation.x = Math.PI / 2;
  ring.position.y = s * Math.cos(colat) * 1.02;
  scene.add(ring);
}

/* groups rebuilt when the sliders move */
const dynamic = new THREE.Group();
scene.add(dynamic);
let mp = shue(state.bz, pdyn(state.n, state.v));

function rebuild() {
  dynamic.clear();
  mp = shue(state.bz, pdyn(state.n, state.v));
  /* magnetopause surface */
  const rings = 40, segs = 48, tmax = 2.4, pos = [];
  for (let i = 0; i <= rings; i++) {
    const th = tmax * i / rings, r = mpRadius(th, mp);
    for (let j = 0; j <= segs; j++) {
      const ph = 2 * Math.PI * j / segs;
      pos.push(r * Math.cos(th), r * Math.sin(th) * Math.cos(ph), r * Math.sin(th) * Math.sin(ph));
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  const idx = [];
  for (let i = 0; i < rings; i++) for (let j = 0; j < segs; j++) {
    const a = i * (segs + 1) + j, b = a + segs + 1;
    idx.push(a, b, a + 1, b, b + 1, a + 1);
  }
  g.setIndex(idx);
  dynamic.add(new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: 0x6da7ec, transparent: true, opacity: 0.08, side: THREE.DoubleSide, depthWrite: false })));
  dynamic.add(new THREE.LineSegments(new THREE.WireframeGeometry(g), new THREE.LineBasicMaterial({ color: 0x6da7ec, transparent: true, opacity: 0.07 })));

  /* closed dipole field lines, clipped at the magnetopause */
  const lineMat = new THREE.LineBasicMaterial({ color: 0x9ec5f4, transparent: true, opacity: 0.55 });
  for (const L of [2.2, 3.2, 4.6, 6.5, 8.5]) for (let k = 0; k < 8; k++) {
    const ph = k * Math.PI / 4, pts = [];
    const t0 = Math.asin(Math.sqrt(1 / L));
    for (let t = t0; t <= Math.PI - t0 + 1e-6; t += 0.03) {
      const r = L * Math.sin(t) ** 2;
      const p = new THREE.Vector3(r * Math.sin(t) * Math.cos(ph), r * Math.cos(t), r * Math.sin(t) * Math.sin(ph));
      const th = Math.atan2(Math.hypot(p.y, p.z), p.x);
      if (p.length() > mpRadius(th, mp) * 0.97) { pts.length = 0; break; }
      pts.push(p);
    }
    if (pts.length > 2) dynamic.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), lineMat));
  }

  /* open field lines stretched into the tail: many when Bz is south, none when north */
  const south = Math.max(-state.bz, 0);
  const nOpen = Math.round(Math.min(south / 2, 8));
  const openMat = new THREE.LineBasicMaterial({ color: 0xeb6834, transparent: true, opacity: 0.65 });
  for (let i = 0; i < nOpen; i++) for (const s of [1, -1]) {
    const z = (i - (nOpen - 1) / 2) * 0.6;
    const c = new THREE.CatmullRomCurve3([
      new THREE.Vector3(0.2, s * 0.98, z * 0.08), new THREE.Vector3(-1.5, s * 3.2, z * 0.4),
      new THREE.Vector3(-8, s * mp.r0 * 0.75, z), new THREE.Vector3(-30, s * mp.r0 * 1.05, z * 1.4)]);
    dynamic.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(c.getPoints(60)), openMat));
  }
  readout();
}

/* ---------- solar wind particles ---------- */
const N = 900;
const P = new Float32Array(N * 3), mode = new Uint8Array(N), path = [], prog = new Float32Array(N);
const spawn = i => {
  P[i * 3] = 30 + Math.random() * 12;
  const a = Math.random() * 2 * Math.PI, r = Math.sqrt(Math.random()) * 13;
  P[i * 3 + 1] = r * Math.cos(a); P[i * 3 + 2] = r * Math.sin(a);
  mode[i] = 0; prog[i] = 0;
};
for (let i = 0; i < N; i++) { spawn(i); P[i * 3] = -30 + Math.random() * 72; }
const C = new Float32Array(N * 3);
const WIND = [1.0, 0.89, 0.6], CAUGHT = [0.3, 1.0, 0.55];   // yellow solar wind, green once captured
const paint = (i, c) => { C[i * 3] = c[0]; C[i * 3 + 1] = c[1]; C[i * 3 + 2] = c[2]; };
for (let i = 0; i < N; i++) paint(i, WIND);
const pg = new THREE.BufferGeometry();
pg.setAttribute("position", new THREE.BufferAttribute(P, 3));
pg.setAttribute("color", new THREE.BufferAttribute(C, 3));
const pts = new THREE.Points(pg, new THREE.PointsMaterial({ size: 0.2, vertexColors: true, transparent: true, opacity: 0.9 }));
scene.add(pts);

let aurora = 0;
const clock = new THREE.Clock();
function step(dt) {
  const speed = state.v / 400 * 7, c = coupling(state.v, state.bz), capture = Math.min(c / STRONG, 1) * 0.7;
  for (let i = 0; i < N; i++) {
    let x = P[i * 3], y = P[i * 3 + 1], z = P[i * 3 + 2];
    if (mode[i] === 2) {                                   // funnelled toward a cusp
      prog[i] += dt * 0.55;
      const [a, b, e] = path[i], t = Math.min(prog[i], 1), u = 1 - t;
      for (let k = 0; k < 3; k++) P[i * 3 + k] = u * u * a[k] + 2 * u * t * b[k] + t * t * e[k];
      if (t >= 1) { aurora += 0.012; spawn(i); paint(i, WIND); }
      continue;
    }
    x -= speed * dt;
    const rr = Math.hypot(x, y, z), th = Math.atan2(Math.hypot(y, z), x), rm = mpRadius(th, mp);
    if (rr < rm && th < 2.3) {
      if (mode[i] === 0 && Math.random() < capture) {      // reconnection: field lines link, particles stream poleward
        const s = y >= 0 ? 1 : -1;
        path[i] = [[x, y, z], [x * 0.35, s * rm * 0.85, z * 0.4], [Math.sin(colat) * 0.5, s * Math.cos(colat) * 1.03, z * 0.04]];
        mode[i] = 2; prog[i] = 0; paint(i, CAUGHT);
        P[i * 3] = x; continue;
      }
      const k = rm / Math.max(rr, 1e-3);                   // otherwise slide around the shield
      x *= k; y *= k; z *= k; mode[i] = 1;
    }
    if (x < -32) { spawn(i); continue; }
    P[i * 3] = x; P[i * 3 + 1] = y; P[i * 3 + 2] = z;
  }
  pg.attributes.position.needsUpdate = true;
  pg.attributes.color.needsUpdate = true;
  aurora *= Math.pow(0.35, dt);
  auroraMat.opacity = Math.min(0.06 + aurora, 0.95);
}

const HOME = new THREE.Vector3(-9, 15, 42);
function resize() {
  const w = el.clientWidth, h = el.clientHeight;
  renderer.setSize(w, h);
  camera.aspect = w / h; camera.updateProjectionMatrix();
  // narrow (portrait) screens see less sideways, so pull the camera back to keep the whole shield in view
  const pull = Math.min(Math.max(1.6 / camera.aspect, 1), 2.4);
  camera.position.copy(HOME).sub(controls.target).multiplyScalar(pull).add(controls.target);
}
new ResizeObserver(resize).observe(el);
resize();
// only animate while the model is on screen: saves battery on phones
let onScreen = true;
new IntersectionObserver(([e]) => { onScreen = e.isIntersecting; }).observe(el);
(function loop() {
  requestAnimationFrame(loop);
  const dt = Math.min(clock.getDelta(), 0.05);
  if (!onScreen) return;
  step(dt);
  controls.update();
  renderer.render(scene, camera);
})();

/* ---------- controls ---------- */
function readout() {
  const dp = pdyn(state.n, state.v), c = coupling(state.v, state.bz), level = c / STRONG;
  const words = c === 0 ? "almost none: the shield is closed" : level < 0.15 ? "low" : level < 0.4 ? "moderate" : level < 0.75 ? "high" : "very high: storm-level driving";
  $("m-bz-out").textContent = `${state.bz > 0 ? "+" : ""}${state.bz} nT`;
  $("m-v-out").textContent = `${state.v} km/s`;
  $("m-n-out").textContent = `${state.n} /cm³`;
  $("m-readout").innerHTML =
    `<div><span>Ram pressure</span><b>${dp.toFixed(2)} nPa</b></div>` +
    `<div><span>Shield's front edge</span><b>${mp.r0.toFixed(1)} Earth radii</b></div>` +
    `<div><span>from Earth's centre</span><b>${Math.round(mp.r0 * 6371).toLocaleString()} km</b></div>` +
    `<div><span>Flank flaring (α)</span><b>${mp.alpha.toFixed(2)}</b></div>` +
    `<div><span>Coupling</span><b>${c.toFixed(1)}</b></div>` +
    `<div style="display:block;margin-top:6px">Energy getting in: <b>${words}</b></div>`;
}
function bind(id, key) {
  const inp = $(id);
  inp.value = state[key];
  inp.addEventListener("input", () => { state[key] = +inp.value; rebuild(); });
}
function useLive(D) {
  const L = (D || window.L1DATA || {}).latest || {};
  if (L.bz != null) state.bz = Math.round(L.bz * 2) / 2;
  if (L.speed != null) state.v = Math.round(L.speed / 10) * 10;
  if (L.density != null) state.n = Math.round(L.density * 2) / 2;
  $("m-bz").value = state.bz; $("m-v").value = state.v; $("m-n").value = state.n;
  rebuild();
}
bind("m-bz", "bz"); bind("m-v", "v"); bind("m-n", "n");
$("m-live").addEventListener("click", () => useLive());
if (window.L1DATA) useLive(window.L1DATA); else { rebuild(); window.addEventListener("l1data", e => useLive(e.detail), { once: true }); }
