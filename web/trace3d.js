// Creator Trace as a time tunnel. The camera flies through the token's life:
// each gate is a moment (funding, creation, the first five seconds, the exit,
// what holds now, the creator's record) and the last gate is the verdict.
// The page builds a plain model from the API answer; this file only draws it.
import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";

const MUTED = "#5b5675";
const SEGMENTS = 60;

// Where each moment sits along the tunnel (0..1), in the order it happened.
const EVENTS = [
  { key: "funding", u: 0.12, title: "funding" },
  { key: "creator", u: 0.22, title: "t+0 · created", image: true },
  { key: "buyers", u: 0.32, title: "first 5 seconds" },
  { key: "exit", u: 0.42, title: "creator exit" },
  { key: "holders", u: 0.52, title: "holders now" },
  { key: "contract", u: 0.61, title: "contract" },
  { key: "history", u: 0.70, title: "creator record" },
  { key: "verdict", u: 0.80, title: "verdict", image: true },
];

function glowTexture() {
  const c = document.createElement("canvas"); c.width = c.height = 64;
  const g = c.getContext("2d");
  const r = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  r.addColorStop(0, "rgba(255,255,255,1)"); r.addColorStop(0.3, "rgba(255,255,255,.45)"); r.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = r; g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
}

function polygon(radius, sides, rot = 0) {
  const pts = [];
  for (let i = 0; i <= sides; i++) { const a = rot + (i / sides) * Math.PI * 2; pts.push(new THREE.Vector3(Math.cos(a) * radius, Math.sin(a) * radius, 0)); }
  return new THREE.BufferGeometry().setFromPoints(pts);
}

const ease = (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);

export function createTrace(container) {
  const size = () => [container.clientWidth, container.clientHeight];
  let [W, H] = size();

  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
  renderer.setSize(W, H);
  renderer.domElement.className = "t3d";
  container.appendChild(renderer.domElement);

  const labelRenderer = new CSS2DRenderer();
  labelRenderer.setSize(W, H);
  labelRenderer.domElement.className = "t3d-labels";
  container.appendChild(labelRenderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x05040a);
  scene.fog = new THREE.Fog(0x05040a, 5, 34);

  const camera = new THREE.PerspectiveCamera(68, W / H, 0.05, 200);

  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(W, H), 0.75, 0.4, 0.12);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());

  const glow = glowTexture();
  const clock = new THREE.Clock();

  // --- the tunnel path ---
  const pts = [];
  for (let i = 0; i <= SEGMENTS; i++) pts.push(new THREE.Vector3(Math.sin(i * 0.33) * 2.4, Math.cos(i * 0.21) * 1.4, -i * 5));
  const path = new THREE.CatmullRomCurve3(pts);
  const frames = path.computeFrenetFrames(600, false);
  const pointAt = (u) => path.getPointAt(THREE.MathUtils.clamp(u, 0, 1));
  const frameAt = (u) => {
    const i = Math.round(THREE.MathUtils.clamp(u, 0, 1) * 600);
    return { n: frames.normals[i], b: frames.binormals[i], t: frames.tangents[i] };
  };

  // wall: a wireframe tube with a scrolling shimmer
  const tubeMat = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, side: THREE.BackSide,
    uniforms: { uTime: { value: 0 }, uA: { value: new THREE.Color("#9945ff") }, uB: { value: new THREE.Color("#19d3ff") } },
    vertexShader: "varying vec2 vUv; varying float vDepth; void main(){ vUv = uv; vec4 mv = modelViewMatrix * vec4(position, 1.0); vDepth = -mv.z; gl_Position = projectionMatrix * mv; }",
    fragmentShader: `
      uniform float uTime; uniform vec3 uA; uniform vec3 uB; varying vec2 vUv; varying float vDepth;
      void main(){
        vec2 p = vec2(vUv.x * 320.0, vUv.y * 16.0);
        vec2 g = abs(fract(p - 0.5) - 0.5) / fwidth(p);
        float line = 1.0 - min(min(g.x, g.y), 1.0);
        float band = 0.5 + 0.5 * sin(vUv.x * 240.0 - uTime * 3.0);
        vec3 col = mix(uA, uB, vUv.y);
        float fade = 1.0 - smoothstep(8.0, 32.0, vDepth);
        gl_FragColor = vec4(col, line * (0.16 + band * 0.12) * fade);
      }`,
  });
  scene.add(new THREE.Mesh(new THREE.TubeGeometry(path, 900, 3.2, 16, false), tubeMat));

  // plain rings every few metres give the sense of speed
  const ringGeo = polygon(2.9, 6, Math.PI / 6);
  for (let i = 0; i < 90; i++) {
    const u = i / 90;
    const m = new THREE.LineBasicMaterial({ color: i % 5 === 0 ? "#19d3ff" : "#9945ff", transparent: true, opacity: i % 5 === 0 ? 0.35 : 0.14 });
    const ring = new THREE.Line(ringGeo, m);
    ring.position.copy(pointAt(u)); ring.lookAt(pointAt(Math.min(1, u + 0.001)));
    scene.add(ring);
  }

  // speed streaks
  const STREAKS = 260;
  const streakPos = new Float32Array(STREAKS * 6);
  const streakGeo = new THREE.BufferGeometry();
  streakGeo.setAttribute("position", new THREE.BufferAttribute(streakPos, 3));
  const streakMat = new THREE.LineBasicMaterial({ color: "#19d3ff", transparent: true, opacity: 0.5 });
  scene.add(new THREE.LineSegments(streakGeo, streakMat));
  const streakData = Array.from({ length: STREAKS }, () => ({ u: Math.random(), a: Math.random() * Math.PI * 2, r: 0.6 + Math.random() * 2.2 }));

  // --- event gates ---
  const gates = {};
  for (const ev of EVENTS) {
    const g = new THREE.Group();
    g.position.copy(pointAt(ev.u)); g.lookAt(pointAt(Math.min(1, ev.u + 0.001)));
    const hexMat = new THREE.LineBasicMaterial({ color: MUTED, transparent: true, opacity: 0.9 });
    const hex = new THREE.Line(polygon(2.35, 6, Math.PI / 6), hexMat);
    const innerMat = new THREE.LineBasicMaterial({ color: MUTED, transparent: true, opacity: 0.5 });
    const inner = new THREE.Line(polygon(2.05, 64), innerMat);
    const ticks = new THREE.Group();
    for (let i = 0; i < 24; i++) {
      const a = (i / 24) * Math.PI * 2, r1 = 2.5, r2 = i % 6 === 0 ? 2.75 : 2.6;
      ticks.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(Math.cos(a) * r1, Math.sin(a) * r1, 0), new THREE.Vector3(Math.cos(a) * r2, Math.sin(a) * r2, 0)]), innerMat));
    }
    const haloMat = new THREE.SpriteMaterial({ map: glow, color: MUTED, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false });
    const halo = new THREE.Sprite(haloMat); halo.scale.setScalar(3.4);
    g.add(hex, inner, ticks, halo);

    const el = document.createElement("div"); el.className = "tt-label";
    el.innerHTML = `<i>// ${ev.title}</i><b></b>`;
    const label = new CSS2DObject(el); label.position.set(0, 2.95, 0); g.add(label);

    let imgEl = null;
    if (ev.image) {
      imgEl = document.createElement("div"); imgEl.className = "t3d-core tt-img";
      imgEl.innerHTML = `<div class="t3d-hex"><img alt=""><span>MINT</span></div>`;
      g.add(new CSS2DObject(imgEl));
    }
    scene.add(g);
    gates[ev.key] = { ev, g, hex, hexMat, innerMat, haloMat, ticks, el, imgEl };
  }

  // bots that fly past the camera in the first-five-seconds stretch
  let bolts = [];
  function launchBolts(buyers) {
    for (const b of bolts) scene.remove(b.mesh);
    bolts = [];
    const u0 = EVENTS.find((e) => e.key === "buyers").u;
    buyers.forEach((b, i) => {
      const color = b.kind === "bot" ? "#ff3d6e" : "#19d3ff";
      const geo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, 0, 3.2)]);
      const mesh = new THREE.Line(geo, new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.95 }));
      const head = new THREE.Sprite(new THREE.SpriteMaterial({ map: glow, color, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
      head.scale.setScalar(0.5); mesh.add(head);
      scene.add(mesh);
      bolts.push({ mesh, a: (i / Math.max(1, buyers.length)) * Math.PI * 2, r: 1.1 + (i % 3) * 0.4, u: u0 - 0.035 - i * 0.004, speed: 0.05 + Math.random() * 0.03 });
    });
  }

  // exit burst
  let burst = null;
  function makeBurst(color) {
    if (burst) scene.remove(burst.points);
    const n = 420, pos = new Float32Array(n * 3), vel = [];
    for (let i = 0; i < n; i++) vel.push(new THREE.Vector3().randomDirection().multiplyScalar(2 + Math.random() * 5));
    const geo = new THREE.BufferGeometry(); geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    const points = new THREE.Points(geo, new THREE.PointsMaterial({ color, size: 0.09, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false }));
    points.position.copy(pointAt(EVENTS.find((e) => e.key === "exit").u));
    scene.add(points);
    burst = { points, vel, fired: null };
  }

  // --- flight state ---
  let mode = "idle";        // idle | scanning | tour | done
  let camU = 0.02, tour = null, model = null;

  function setGate(key, color, value, lit) {
    const gt = gates[key]; if (!gt) return;
    gt.hexMat.color.set(color); gt.innerMat.color.set(color); gt.haloMat.color.set(color);
    gt.hexMat.opacity = lit ? 1 : 0.55;
    gt.el.querySelector("b").textContent = value || "";
    gt.el.style.setProperty("--c", color);
    if (gt.imgEl) gt.imgEl.style.setProperty("--c", color);
  }

  function idle() {
    mode = "idle"; model = null; tour = null;
    for (const key in gates) { setGate(key, MUTED, "", false); gates[key].haloMat.opacity = 0; }
    for (const b of bolts) scene.remove(b.mesh);
    bolts = [];
    if (burst) { scene.remove(burst.points); burst = null; }
    tubeMat.uniforms.uA.value.set("#9945ff");
  }

  function reveal() { /* the warp itself is the progress cue */ }

  function setScanning(on) {
    if (on) mode = "scanning";
    else if (mode === "scanning") { mode = "idle"; if (model) startTour(); }
  }

  function startTour() {
    if (!model) return;
    camU = 0.02; mode = "tour";
    tour = { i: 0, from: camU, phase: "fly", t0: clock.elapsedTime };
    for (const key in gates) gates[key].haloMat.opacity = 0;
    launchBolts(model.buyers || []);
    makeBurst(model.nodes.exit.color);
  }

  function result(m) {
    model = m;
    for (const ev of EVENTS) {
      if (ev.key === "verdict") setGate("verdict", m.verdictColor, `${m.label || "—"} · ${m.score ?? "–"}/100`, true);
      else { const n = m.nodes[ev.key]; setGate(ev.key, n.color, n.value, n.lit); }
    }
    tubeMat.uniforms.uA.value.set(m.verdictColor);
    for (const key of ["creator", "verdict"]) gates[key].imgEl.querySelector("span").textContent = m.symbol || "TOKEN";
    if (mode !== "scanning") startTour();
  }

  function setImage(src) {
    for (const key of ["creator", "verdict"]) {
      const el = gates[key].imgEl, img = el.querySelector("img");
      if (src) { img.src = src; el.classList.add("has-img"); } else { img.removeAttribute("src"); el.classList.remove("has-img"); }
    }
  }
  function setSymbol(text) { for (const key of ["creator", "verdict"]) gates[key].imgEl.querySelector("span").textContent = text || "MINT"; }

  labelRenderer.domElement.addEventListener("click", () => { if (model && mode !== "scanning") startTour(); });

  // --- loop ---
  const tmp = new THREE.Vector3();
  const buyersU = EVENTS.find((e) => e.key === "buyers").u;
  function frame() {
    const dt = Math.min(clock.getDelta(), 0.05), t = clock.elapsedTime;
    tubeMat.uniforms.uTime.value = t;

    if (mode === "idle" || mode === "scanning") {
      camU += (mode === "scanning" ? 0.06 : 0.004) * dt;
      if (camU > 0.9) camU = 0.02;
    }

    if (mode === "tour" && tour) {
      const target = EVENTS[tour.i];
      const stopAt = target.u - 0.028;
      if (tour.phase === "fly") {
        const k = Math.min(1, (t - tour.t0) / 1.05);
        camU = tour.from + (stopAt - tour.from) * ease(k);
        if (k >= 1) {
          tour.phase = "hold"; tour.t0 = t; gates[target.key].haloMat.opacity = 0.16;
          if (target.key === "exit" && burst && model.nodes.exit.lit) burst.fired = t;
        }
      } else if (target.key !== "verdict" && t - tour.t0 > 0.75) {
        gates[target.key].haloMat.opacity = 0.05;
        tour.i += 1; tour.from = camU; tour.phase = "fly"; tour.t0 = t;
      } else if (target.key === "verdict") {
        mode = "done";
      }
    }

    // camera on the path, looking ahead, with a slow roll
    camera.position.copy(pointAt(camU));
    const fr = frameAt(camU);
    camera.up.copy(fr.n).applyAxisAngle(fr.t, Math.sin(t * 0.25) * 0.15);
    camera.lookAt(pointAt(Math.min(1, camU + 0.02)));
    camera.fov = THREE.MathUtils.lerp(camera.fov, mode === "scanning" ? 92 : 68, 0.06);
    camera.updateProjectionMatrix();

    // streaks stream past; faster while scanning
    const sv = mode === "scanning" ? 0.09 : 0.012;
    for (let i = 0; i < STREAKS; i++) {
      const s = streakData[i];
      s.u -= sv * dt * (0.6 + (i % 5) * 0.2);
      if (s.u < camU - 0.01 || s.u > camU + 0.3) s.u = camU + 0.02 + Math.random() * 0.28;
      const f = frameAt(s.u), c = pointAt(s.u);
      const off = tmp.copy(f.n).multiplyScalar(Math.cos(s.a) * s.r).addScaledVector(f.b, Math.sin(s.a) * s.r);
      const a = c.clone().add(off), b = a.clone().addScaledVector(f.t, mode === "scanning" ? -2.2 : -0.4);
      streakPos.set([a.x, a.y, a.z, b.x, b.y, b.z], i * 6);
    }
    streakGeo.attributes.position.needsUpdate = true;
    streakMat.opacity = mode === "scanning" ? 0.85 : 0.35;

    // gates turn; labels show only for the stretch ahead
    for (const key in gates) {
      const gt = gates[key];
      gt.hex.rotation.z += 0.002;
      gt.ticks.rotation.z -= 0.004;
      const d = gt.ev.u - camU;
      // only the gate right ahead speaks, so labels never stack
      gt.el.style.opacity = d > -0.006 && d < 0.06 ? 1 : 0;
      if (gt.imgEl) gt.imgEl.style.opacity = d > -0.01 && d < 0.075 ? 1 : 0;
    }

    // bots circle through the first-five-seconds stretch
    for (const b of bolts) {
      b.mesh.visible = mode === "tour" || mode === "done";
      if (!b.mesh.visible) continue;
      b.u += b.speed * dt * 0.25;
      if (b.u > buyersU + 0.03) b.u -= 0.07;
      const f = frameAt(b.u), c = pointAt(b.u);
      b.a += dt * 0.8;
      b.mesh.position.copy(c).addScaledVector(f.n, Math.cos(b.a) * b.r).addScaledVector(f.b, Math.sin(b.a) * b.r);
      b.mesh.lookAt(b.mesh.position.clone().add(f.t));
    }

    // the exit bursts once the camera reaches it
    if (burst && burst.fired) {
      const age = t - burst.fired, arr = burst.points.geometry.attributes.position.array;
      burst.vel.forEach((v, i) => { arr[i * 3] = v.x * age; arr[i * 3 + 1] = v.y * age; arr[i * 3 + 2] = v.z * age; });
      burst.points.geometry.attributes.position.needsUpdate = true;
      burst.points.material.opacity = Math.max(0, 1 - age * 0.45);
    }

    composer.render();
    labelRenderer.render(scene, camera);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  new ResizeObserver(() => {
    [W, H] = size(); if (!W || !H) return;
    renderer.setSize(W, H); composer.setSize(W, H); labelRenderer.setSize(W, H);
    camera.aspect = W / H; camera.updateProjectionMatrix();
  }).observe(container);

  idle();
  return { idle, reveal, result, setScanning, setImage, setSymbol, replay: startTour };
}
