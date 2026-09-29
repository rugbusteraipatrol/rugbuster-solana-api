// Creator Trace as a sphere of points. While the scan runs the sphere turns fast under a
// sweeping band; when the answer arrives it turns to one point at a time, lights it and
// writes that finding next to it, in the order things happened, and the verdict last.
// The token's image sits in the core. The page builds a plain model; this file only draws it.
import * as THREE from "three";

const MUTED = "#6b6690";
const R = 1.6;
const STEP = (Math.PI * 2) / 9, FACE = 0.7;
const LAT = [0.36, -0.3, 0.08, -0.46, 0.44, -0.12, 0.24, 0];
const ORDER = [
  ["funding", "funding"],
  ["creator", "t+0 · created"],
  ["buyers", "first 5 seconds"],
  ["exit", "creator exit"],
  ["holders", "holders now"],
  ["contract", "contract"],
  ["history", "creator record"],
];

const ease = (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);

function spriteTexture(ring) {
  const c = document.createElement("canvas"); c.width = c.height = 128;
  const g = c.getContext("2d");
  if (ring) { g.strokeStyle = "#fff"; g.lineWidth = 5; g.beginPath(); g.arc(64, 64, 54, 0, Math.PI * 2); g.stroke(); }
  else {
    const r = g.createRadialGradient(64, 64, 0, 64, 64, 64);
    r.addColorStop(0, "rgba(255,255,255,1)"); r.addColorStop(0.18, "rgba(255,255,255,.85)"); r.addColorStop(0.45, "rgba(255,255,255,.18)"); r.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = r; g.fillRect(0, 0, 128, 128);
  }
  return new THREE.CanvasTexture(c);
}

function sphereGeometry(N) {
  const pos = new Float32Array(N * 3), seed = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    // Fibonacci sphere: even spacing, no clumps at the poles
    const y = 1 - (i / (N - 1)) * 2, r = Math.sqrt(1 - y * y), th = i * 2.39996;
    pos.set([Math.cos(th) * r * R, y * R, Math.sin(th) * r * R], i * 3);
    seed[i] = Math.random();
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.setAttribute("aSeed", new THREE.BufferAttribute(seed, 1));
  return g;
}

export function createTrace(container) {
  const size = () => [container.clientWidth, container.clientHeight];
  let [W, H] = size();
  const DPR = Math.min(window.devicePixelRatio || 1, 1.75);

  const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(DPR);
  renderer.setSize(W, H);
  renderer.domElement.className = "t3d";
  container.appendChild(renderer.domElement);

  const layer = document.createElement("div");
  layer.className = "t3d-labels";
  container.appendChild(layer);

  // the token's image in the core
  const core = document.createElement("div");
  core.className = "t3d-core sp-core";
  core.innerHTML = `<div class="t3d-hex"><img alt=""><span>MINT</span></div>`;
  layer.appendChild(core);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(40, W / H, 0.1, 50);
  camera.position.z = 8.2;
  const SHIFT = -0.1; // sphere sits left of centre so the labels have room on the right

  const uniforms = {
    uTime: { value: 0 }, uPix: { value: 800 }, uSize: { value: 0.022 }, uOpacity: { value: 1 },
    uScan: { value: 0 }, uTint: { value: new THREE.Color(MUTED) }, uTintAmt: { value: 0 },
  };
  const material = new THREE.ShaderMaterial({
    uniforms, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: `
      uniform float uTime, uPix, uSize, uScan, uTintAmt; uniform vec3 uTint;
      attribute float aSeed;
      varying vec3 vCol; varying float vAlpha;
      void main() {
        vec3 pos = position;
        float n = sin(pos.x * 2.1 + uTime * .9 + aSeed * 6.283) * sin(pos.y * 2.3 + uTime * .7) * sin(pos.z * 1.9 + uTime * 1.1 + aSeed * 3.);
        pos += normalize(pos) * n * .11;
        vec4 mv = modelViewMatrix * vec4(pos, 1.);
        gl_Position = projectionMatrix * mv;
        gl_PointSize = uSize * uPix * (.55 + aSeed * .9) / -mv.z;
        float h = clamp(pos.y * .28 + .5, 0., 1.);
        vec3 c = mix(vec3(.6, .27, 1.), vec3(.1, .83, 1.), smoothstep(0., .6, h));
        c = mix(c, vec3(.08, .95, .58), smoothstep(.55, 1., h));
        c = mix(c, uTint, uTintAmt);
        float sy = sin(uTime * 1.7) * 1.7;
        float band = exp(-pow((pos.y - sy) / .13, 2.)) * uScan;
        c = mix(c, vec3(1.), band * .85);
        c = mix(c, vec3(1.), smoothstep(.05, .11, n * .11));
        vCol = c; vAlpha = (.5 + .5 * aSeed) * (1. + band * 1.5);
      }`,
    fragmentShader: `
      uniform float uOpacity; varying vec3 vCol; varying float vAlpha;
      void main() {
        float r = length(gl_PointCoord - .5);
        if (r > .5) discard;
        gl_FragColor = vec4(vCol, smoothstep(.5, .08, r) * vAlpha * uOpacity);
      }`,
  });

  const group = new THREE.Group();
  const main = new THREE.Points(sphereGeometry(7000), material);
  const backMat = material.clone();
  backMat.uniforms = { ...uniforms, uOpacity: { value: 0.3 } };
  const back = new THREE.Points(sphereGeometry(2000), backMat);
  back.scale.setScalar(1.35);
  group.add(back, main);
  scene.add(group);

  const dotTex = spriteTexture(false), ringTex = spriteTexture(true);
  let markers = [], mode = "idle", reveal = null, model = null, tintTarget = 0, spin = 0;
  const clock = new THREE.Clock();

  function clearMarkers() {
    markers.forEach((m) => { main.remove(m.obj); m.el.remove(); });
    markers = [];
  }

  function placeMarkers(list) {
    clearMarkers();
    let target = spin;
    list.forEach((f, i) => {
      target -= STEP * (f.verdict ? 1.25 : 1);
      const phi = -target + FACE, lat = LAT[i % LAT.length], rr = R * 1.04;
      const obj = new THREE.Object3D();
      obj.position.set(Math.sin(phi) * Math.cos(lat) * rr, Math.sin(lat) * rr, Math.cos(phi) * Math.cos(lat) * rr);
      const color = new THREE.Color(f.color);
      const dot = new THREE.Sprite(new THREE.SpriteMaterial({ map: dotTex, color, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false }));
      const ring = new THREE.Sprite(new THREE.SpriteMaterial({ map: ringTex, color, transparent: true, opacity: 0, depthWrite: false }));
      dot.scale.setScalar(f.verdict ? 0.55 : 0.36); ring.scale.setScalar(0.1);
      obj.add(dot, ring); main.add(obj);
      const el = document.createElement("div");
      el.className = "fmark" + (f.verdict ? " fm-verdict" : "");
      el.style.setProperty("--c", f.color);
      el.innerHTML = `<span class="fm-line"></span><div class="fm-box"><i>// ${f.title}</i><b></b></div>`;
      el.querySelector("b").textContent = f.value;
      layer.appendChild(el);
      markers.push({ obj, dot, ring, el, target, lit: 0, verdict: !!f.verdict });
    });
  }

  function findingsFrom(m) {
    const list = ORDER.map(([key, title]) => {
      const n = m.nodes[key] || {};
      return { title, value: n.value || "not read", color: n.lit ? n.color : MUTED };
    });
    list.push({ title: "verdict", value: `${m.label || "—"} · ${m.score ?? "–"}/100`, color: m.verdictColor, verdict: true });
    return list;
  }

  function startReveal() {
    if (!model) return;
    placeMarkers(findingsFrom(model));
    mode = "reveal";
    reveal = { i: 0, phase: "turn", t0: clock.elapsedTime, from: spin };
    tintTarget = 0;
  }

  function idle() {
    clearMarkers(); model = null; reveal = null; mode = "idle"; tintTarget = 0;
    core.classList.remove("on");
  }
  function reveal_() { /* the sweep itself is the progress cue */ }
  function setScanning(on) {
    if (on) { clearMarkers(); mode = "scanning"; reveal = null; tintTarget = 0; }
    else if (mode === "scanning") { mode = "idle"; if (model) startReveal(); }
  }
  function result(m) {
    model = m;
    core.style.setProperty("--c", m.verdictColor);
    core.querySelector("span").textContent = m.symbol || "TOKEN";
    core.classList.add("on");
    if (mode !== "scanning") startReveal();
  }
  function setImage(src) {
    const img = core.querySelector("img");
    if (src) { img.src = src; core.classList.add("has-img"); } else { img.removeAttribute("src"); core.classList.remove("has-img"); }
  }
  function setSymbol(text) { core.querySelector("span").textContent = text || "MINT"; }

  layer.addEventListener("click", () => { if (model && mode !== "scanning") startReveal(); });

  function resize() {
    [W, H] = size(); if (!W || !H) return;
    renderer.setSize(W, H);
    camera.aspect = W / H; camera.updateProjectionMatrix();
    uniforms.uPix.value = (H * DPR) / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)));
  }
  new ResizeObserver(resize).observe(container);
  resize();

  const wpos = new THREE.Vector3(), cpos = new THREE.Vector3(), edge = new THREE.Vector3();
  function frame() {
    const dt = Math.min(clock.getDelta(), 0.05), t = clock.elapsedTime;
    uniforms.uTime.value = t;

    uniforms.uScan.value += ((mode === "scanning" ? 1 : 0) - uniforms.uScan.value) * Math.min(1, dt * 5);
    if (mode === "scanning") spin -= dt * 1.1;
    else if (mode === "reveal" && reveal) {
      const m = markers[reveal.i];
      if (reveal.phase === "turn") {
        const q = Math.min(1, (t - reveal.t0) / 1.1);
        spin = reveal.from + (m.target - reveal.from) * ease(q);
        if (q >= 1) {
          reveal.phase = "hold"; reveal.t0 = t; m.lit = t;
          if (m.verdict) { tintTarget = 0.3; uniforms.uTint.value.set(model.verdictColor); }
        }
      } else if (t - reveal.t0 > (m.verdict ? 0.2 : 0.95)) {
        if (reveal.i < markers.length - 1) { reveal.i += 1; reveal.phase = "turn"; reveal.t0 = t; reveal.from = spin; }
        else { mode = "done"; reveal = null; }
      }
    } else spin -= dt * (mode === "done" ? 0.1 : 0.18);
    uniforms.uTintAmt.value += (tintTarget - uniforms.uTintAmt.value) * Math.min(1, dt * 2);

    const visH = 2 * camera.position.z * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    group.position.x = SHIFT * visH * camera.aspect;
    group.rotation.y = spin;
    back.rotation.y = -t * 0.05;
    group.updateMatrixWorld();

    // the core image sits on the sphere's centre, sized to the sphere
    group.getWorldPosition(cpos);
    edge.set(R, 0, 0).add(cpos).project(camera);
    const c2 = cpos.clone().project(camera);
    const radiusPx = Math.abs(edge.x - c2.x) * 0.5 * W;
    core.style.transform = `translate(${(c2.x * 0.5 + 0.5) * W}px, ${(-c2.y * 0.5 + 0.5) * H}px) translate(-50%, -50%)`;
    core.style.setProperty("--core-s", `${Math.round(radiusPx * 0.5)}px`);
    const cx = (c2.x * 0.5 + 0.5) * W;

    // lit points pulse once and keep glowing; labels follow their point and hide behind the sphere
    const current = reveal ? reveal.i : -1;
    markers.forEach((m, i) => {
      m.obj.getWorldPosition(wpos);
      const facing = (wpos.z - cpos.z) / R;
      const vis = THREE.MathUtils.smoothstep(facing, 0.05, 0.45);
      if (m.lit) {
        const age = t - m.lit;
        m.dot.material.opacity = Math.min(1, age * 4) * (0.75 + Math.sin(t * 3 + i) * 0.25) * Math.max(0.15, vis);
        const pulse = Math.min(1, age / 0.9);
        m.ring.scale.setScalar((m.verdict ? 0.35 : 0.2) + pulse * (m.verdict ? 0.6 : 0.4));
        m.ring.material.opacity = (1 - pulse) * 0.9 * vis + (m.verdict ? 0.35 * vis : 0);
      }
      wpos.project(camera);
      const x = (wpos.x * 0.5 + 0.5) * W, y = (-wpos.y * 0.5 + 0.5) * H;
      const old = i !== current && !m.verdict;
      // older labels only on the right half, so none of them crosses the core image
      const side = old ? THREE.MathUtils.smoothstep((x - cx) / Math.max(1, radiusPx), 0.15, 0.4) : 1;
      m.el.classList.toggle("fm-left", x > W - 230);
      m.el.classList.toggle("fm-old", old);
      m.el.style.transform = `translate(${x}px, ${y}px)`;
      m.el.style.opacity = m.lit ? vis * side * (old ? 0.6 : 1) : 0;
    });

    renderer.render(scene, camera);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  idle();
  return { idle, reveal: reveal_, result, setScanning, setImage, setSymbol, replay: startReveal };
}
