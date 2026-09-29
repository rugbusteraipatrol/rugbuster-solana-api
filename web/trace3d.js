// Creator Trace in 3D. The page builds a plain model from the API answer and
// hands it here; this file only draws it. If WebGL is not available the page
// keeps its 2D trace.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";

const MUTED = "#5b5675";
const KEYS = ["funding", "creator", "contract", "history", "buyers", "holders", "exit"];
const LABELS = { funding: "funding", creator: "creator", contract: "contract", history: "history", buyers: "first 5 s", holders: "holders", exit: "exit" };
const POS = {
  funding: [-3.5, 3.1, -1.0],
  creator: [-2.9, 1.4, 1.0],
  contract: [-3.8, -0.6, 0.1],
  history: [-2.5, -2.7, 0.9],
  buyers: [2.8, 2.0, 0.6],
  holders: [3.8, -0.3, -0.8],
  exit: [2.7, -2.6, 1.2],
};

function glowTexture() {
  const c = document.createElement("canvas"); c.width = c.height = 128;
  const g = c.getContext("2d");
  const r = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  r.addColorStop(0, "rgba(255,255,255,1)"); r.addColorStop(0.25, "rgba(255,255,255,.5)"); r.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = r; g.fillRect(0, 0, 128, 128);
  return new THREE.CanvasTexture(c);
}

function circle(radius, segments = 128) {
  const pts = [];
  for (let i = 0; i <= segments; i++) { const a = (i / segments) * Math.PI * 2; pts.push(new THREE.Vector3(Math.cos(a) * radius, Math.sin(a) * radius, 0)); }
  return new THREE.BufferGeometry().setFromPoints(pts);
}

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
  scene.background = new THREE.Color(0x07060d);
  scene.fog = new THREE.FogExp2(0x07060d, 0.035);

  const camera = new THREE.PerspectiveCamera(42, W / H, 0.1, 200);
  camera.position.set(0, 1.4, 14.5);

  const controls = new OrbitControls(camera, labelRenderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.06;
  controls.enablePan = false; controls.minDistance = 8; controls.maxDistance = 20;
  controls.autoRotate = true; controls.autoRotateSpeed = 0.55;
  controls.minPolarAngle = Math.PI * 0.22; controls.maxPolarAngle = Math.PI * 0.62;

  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(W, H), W < 500 ? 0.5 : 0.62, 0.35, 0.18);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());

  const glow = glowTexture();
  const clock = new THREE.Clock();

  // --- floor grid, scrolling toward the viewer ---
  const gridMat = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false,
    uniforms: { uTime: { value: 0 }, uA: { value: new THREE.Color("#9945ff") }, uB: { value: new THREE.Color("#14f195") } },
    vertexShader: "varying vec3 vPos; void main(){ vPos = position; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
    fragmentShader: `
      uniform float uTime; uniform vec3 uA; uniform vec3 uB; varying vec3 vPos;
      void main(){
        vec2 p = vPos.xy * 0.9; p.y += uTime * 0.5;
        vec2 g = abs(fract(p - 0.5) - 0.5) / fwidth(p);
        float line = 1.0 - min(min(g.x, g.y), 1.0);
        float d = length(vPos.xy);
        float fade = 1.0 - smoothstep(3.0, 22.0, d);
        vec3 col = mix(uA, uB, smoothstep(-12.0, 12.0, vPos.x));
        gl_FragColor = vec4(col, line * 0.3 * fade);
      }`,
  });
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(70, 70, 1, 1), gridMat);
  floor.rotation.x = -Math.PI / 2; floor.position.y = -4.2;
  scene.add(floor);

  // --- dust ---
  const dustGeo = new THREE.BufferGeometry();
  const N = 900, arr = new Float32Array(N * 3), cols = new Float32Array(N * 3);
  const palette = [new THREE.Color("#9945ff"), new THREE.Color("#19d3ff"), new THREE.Color("#14f195")];
  for (let i = 0; i < N; i++) {
    const r = 9 + Math.random() * 22, t = Math.random() * Math.PI * 2, u = Math.random() * 2 - 1;
    arr.set([r * Math.sqrt(1 - u * u) * Math.cos(t), r * u * 0.6, r * Math.sqrt(1 - u * u) * Math.sin(t)], i * 3);
    const c = palette[i % 3]; cols.set([c.r, c.g, c.b], i * 3);
  }
  dustGeo.setAttribute("position", new THREE.BufferAttribute(arr, 3));
  dustGeo.setAttribute("color", new THREE.BufferAttribute(cols, 3));
  const dust = new THREE.Points(dustGeo, new THREE.PointsMaterial({ size: 0.045, vertexColors: true, transparent: true, opacity: 0.7, depthWrite: false }));
  scene.add(dust);

  // --- core ---
  const core = new THREE.Group(); scene.add(core);
  const icoMat = new THREE.LineBasicMaterial({ color: "#19d3ff", transparent: true, opacity: 0.85 });
  const ico = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.IcosahedronGeometry(1.45, 1)), icoMat);
  core.add(ico);
  const innerMat = new THREE.MeshBasicMaterial({ color: "#9945ff", transparent: true, opacity: 0.07, depthWrite: false });
  core.add(new THREE.Mesh(new THREE.IcosahedronGeometry(1.4, 1), innerMat));
  const coreHalo = new THREE.Sprite(new THREE.SpriteMaterial({ map: glow, color: "#9945ff", transparent: true, opacity: 0.3, blending: THREE.AdditiveBlending, depthWrite: false }));
  coreHalo.scale.setScalar(3.1); core.add(coreHalo);

  const rings = [];
  [[2.1, "#9945ff", 0.55, [1.2, 0.3, 0]], [2.5, "#19d3ff", 0.35, [0.2, 1.1, 0.4]], [2.9, "#14f195", 0.18, [1.57, 0, 0]]].forEach(([r, color, opacity, rot], i) => {
    const m = new THREE.LineDashedMaterial({ color, transparent: true, opacity, dashSize: i === 2 ? 0.05 : 0.35, gapSize: i === 2 ? 0.12 : 0.18 });
    const line = new THREE.Line(circle(r), m); line.computeLineDistances();
    line.rotation.set(...rot); scene.add(line); rings.push(line);
  });

  // token image lives in an HTML label so any image host works (no CORS for WebGL)
  const coreEl = document.createElement("div"); coreEl.className = "t3d-core";
  coreEl.innerHTML = `<div class="t3d-hex"><img alt=""><span>MINT</span></div>`;
  const coreLabel = new CSS2DObject(coreEl); core.add(coreLabel);

  // scan pulses
  const pulses = [];
  function spawnPulse(color = "#14f195") {
    const m = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.6, side: THREE.DoubleSide, depthWrite: false, blending: THREE.AdditiveBlending });
    const ring = new THREE.Mesh(new THREE.RingGeometry(1.5, 1.54, 96), m);
    ring.rotation.x = -Math.PI / 2 + (Math.random() - 0.5) * 0.6; ring.rotation.y = (Math.random() - 0.5) * 0.6;
    scene.add(ring); pulses.push({ ring, born: clock.elapsedTime });
  }

  // --- nodes ---
  const nodes = {};
  function makeNode(key) {
    const g = new THREE.Group(); g.position.set(...POS[key]);
    const shellMat = new THREE.LineBasicMaterial({ color: MUTED, transparent: true, opacity: 0.9 });
    const shell = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.OctahedronGeometry(0.26)), shellMat);
    const dotMat = new THREE.MeshBasicMaterial({ color: MUTED });
    dotMat.toneMapped = false;
    const dot = new THREE.Mesh(new THREE.SphereGeometry(0.07, 16, 16), dotMat);
    const haloMat = new THREE.SpriteMaterial({ map: glow, color: MUTED, transparent: true, opacity: 0.3, blending: THREE.AdditiveBlending, depthWrite: false });
    const halo = new THREE.Sprite(haloMat); halo.scale.setScalar(0.85);
    const ringMat = new THREE.LineBasicMaterial({ color: MUTED, transparent: true, opacity: 0.5 });
    const orbit = new THREE.Line(circle(0.48, 48), ringMat);
    g.add(shell, dot, halo, orbit);
    const el = document.createElement("div"); el.className = "t3d-label";
    el.innerHTML = `<i>// ${LABELS[key]}</i><b></b>`;
    const label = new CSS2DObject(el); label.position.set(0, 0.62, 0); g.add(label);
    g.scale.setScalar(0.001); scene.add(g);
    nodes[key] = { g, shell, shellMat, dotMat, haloMat, ringMat, orbit, el, label, target: 1, shown: false, spin: 0.4 + Math.random() * 0.6 };
  }
  KEYS.forEach(makeNode);

  // --- edges ---
  let edges = [], particles = [], swarm = [];
  function clearEdges() {
    for (const e of edges) { scene.remove(e.line); e.line.geometry.dispose(); }
    for (const p of particles) scene.remove(p.s);
    for (const s of swarm) { scene.remove(s.mesh); if (s.line) scene.remove(s.line); }
    edges = []; particles = []; swarm = [];
  }
  function addEdge(a, b, color, lit, speed = 1) {
    const va = new THREE.Vector3(...a), vb = new THREE.Vector3(...b);
    const mid = va.clone().add(vb).multiplyScalar(0.5);
    const ctrl = mid.clone().multiplyScalar(1.12).add(new THREE.Vector3(0, 0.6, 0));
    const curve = new THREE.QuadraticBezierCurve3(va, ctrl, vb);
    const pts = curve.getPoints(80);
    const geo = new THREE.BufferGeometry().setFromPoints(pts);
    const mat = new THREE.LineBasicMaterial({ color, transparent: true, opacity: lit ? 1 : 0.18 });
    const line = new THREE.Line(geo, mat); geo.setDrawRange(0, lit ? 0 : 81);
    scene.add(line);
    edges.push({ line, geo, lit, grow: 0 });
    if (lit) {
      for (let i = 0; i < 3; i++) {
        const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: glow, color, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
        s.scale.setScalar(0.18); scene.add(s);
        particles.push({ s, curve, t: i / 3, speed: 0.22 * speed * (0.8 + Math.random() * 0.4) });
      }
    }
  }
  const CORE = [0, 0, 0];

  function setNode(key, color, value, show = true) {
    const n = nodes[key];
    n.shellMat.color.set(color); n.dotMat.color.set(color); n.haloMat.color.set(color); n.ringMat.color.set(color);
    n.el.querySelector("b").textContent = value || "";
    n.el.style.setProperty("--c", color);
    if (show && !n.shown) { n.shown = true; n.bornAt = clock.elapsedTime; }
  }

  // --- state ---
  let scanning = false, lastPulse = 0, verdictColor = null;

  function idle() {
    clearEdges();
    verdictColor = null;
    icoMat.color.set("#19d3ff"); coreHalo.material.color.set("#9945ff");
    coreEl.style.setProperty("--c", "#9945ff");
    for (const k of KEYS) {
      setNode(k, MUTED, "", true);
      addEdge(POS[k], k === "funding" ? POS.creator : CORE, MUTED, false);
    }
  }

  function reveal(step) {
    // light the node the log line is about, as a hint while the scan runs
    const order = ["creator", "creator", "creator", "buyers", "history", "contract", "holders", "exit"];
    const key = order[Math.min(step, order.length - 1)];
    const n = nodes[key]; if (n) { n.haloMat.color.set("#14f195"); n.haloMat.opacity = 0.9; n.flash = clock.elapsedTime; }
    spawnPulse();
  }

  function result(model) {
    clearEdges();
    verdictColor = model.verdictColor;
    icoMat.color.set(model.verdictColor); coreHalo.material.color.set(model.verdictColor);
    coreEl.style.setProperty("--c", model.verdictColor);
    coreEl.querySelector("span").textContent = model.symbol || "TOKEN";
    for (const k of KEYS) {
      const m = model.nodes[k];
      setNode(k, m.color, m.value, true);
      nodes[k].haloMat.opacity = 0.32;
      const from = POS[k], to = k === "funding" ? POS.creator : CORE;
      if (k === "exit") addEdge(CORE, POS.exit, m.color, m.lit, 1.8);
      else addEdge(from, to, m.color, m.lit);
    }
    // the bot swarm around the first-buyers node
    const base = new THREE.Vector3(...POS.buyers);
    (model.buyers || []).forEach((b, i, all) => {
      const a = (i / Math.max(1, all.length)) * Math.PI * 2;
      const off = new THREE.Vector3(Math.cos(a) * 0.95, 0.55 + Math.sin(a * 2) * 0.35, Math.sin(a) * 0.95);
      const color = b.kind === "bot" ? "#ff3d6e" : "#19d3ff";
      const mesh = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.TetrahedronGeometry(0.13)), new THREE.LineBasicMaterial({ color }));
      mesh.position.copy(base).add(off); scene.add(mesh);
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([mesh.position.clone(), base.clone()]), new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.5 }));
      scene.add(line);
      swarm.push({ mesh, line, off, a, speed: 0.4 + Math.random() * 0.3 });
    });
  }

  function setScanning(on) { scanning = on; controls.autoRotateSpeed = on ? 2.4 : 0.55; }

  function setImage(src) {
    const img = coreEl.querySelector("img");
    if (src) { img.src = src; coreEl.classList.add("has-img"); } else { img.removeAttribute("src"); coreEl.classList.remove("has-img"); }
  }
  function setSymbol(text) { coreEl.querySelector("span").textContent = text || "MINT"; }

  // --- loop ---
  function frame() {
    const dt = Math.min(clock.getDelta(), 0.05), t = clock.elapsedTime;
    gridMat.uniforms.uTime.value = t;
    dust.rotation.y = t * 0.012;
    const speed = scanning ? 1.6 : 0.25;
    ico.rotation.y += 0.004 * (scanning ? 6 : 1); ico.rotation.x += 0.002 * (scanning ? 6 : 1);
    rings[0].rotation.z += 0.004 * speed * 4; rings[1].rotation.z -= 0.003 * speed * 4; rings[2].rotation.z += 0.0015 * speed * 4;
    coreHalo.material.opacity = 0.24 + Math.sin(t * (scanning ? 8 : 2)) * 0.08;

    if (scanning && t - lastPulse > 0.55) { spawnPulse(); lastPulse = t; }
    for (let i = pulses.length - 1; i >= 0; i--) {
      const p = pulses[i], age = t - p.born;
      p.ring.scale.setScalar(1 + age * 3.2); p.ring.material.opacity = Math.max(0, 0.6 - age * 0.35);
      if (age > 1.8) { scene.remove(p.ring); p.ring.geometry.dispose(); pulses.splice(i, 1); }
    }

    for (const k of KEYS) {
      const n = nodes[k];
      const s = n.shown ? Math.min(1, (t - (n.bornAt || 0)) * 2.2) : 0.001;
      const ease = 1 - Math.pow(1 - s, 3);
      n.g.scale.setScalar(Math.max(0.001, ease));
      n.shell.rotation.y += 0.01 * n.spin; n.shell.rotation.x += 0.006 * n.spin;
      n.orbit.rotation.x = Math.PI / 2 + Math.sin(t * 0.7 + n.spin) * 0.4; n.orbit.rotation.y += 0.01;
      n.g.position.y = POS[k][1] + Math.sin(t * 0.8 + n.spin * 5) * 0.08;
      if (n.flash && t - n.flash > 0.6) { n.haloMat.opacity = 0.32; n.flash = 0; }
    }
    if (verdictColor) nodes.exit.haloMat.opacity = 0.25 + Math.abs(Math.sin(t * 3)) * 0.35;

    for (const e of edges) if (e.lit && e.grow < 81) { e.grow = Math.min(81, e.grow + 2.2); e.geo.setDrawRange(0, Math.floor(e.grow)); }
    for (const p of particles) { p.t = (p.t + p.speed * dt) % 1; p.s.position.copy(p.curve.getPoint(p.t)); }
    const base = nodes.buyers.g.position;
    for (const s of swarm) {
      s.a += s.speed * dt;
      const off = new THREE.Vector3(Math.cos(s.a) * 0.95, s.off.y, Math.sin(s.a) * 0.95);
      s.mesh.position.copy(base).add(off); s.mesh.rotation.x += 0.03; s.mesh.rotation.y += 0.02;
      s.line.geometry.setFromPoints([s.mesh.position.clone(), base.clone()]);
    }

    controls.update();
    composer.render();
    labelRenderer.render(scene, camera);
    raf = requestAnimationFrame(frame);
  }
  let raf = requestAnimationFrame(frame);

  new ResizeObserver(() => {
    [W, H] = size(); if (!W || !H) return;
    renderer.setSize(W, H); composer.setSize(W, H); labelRenderer.setSize(W, H);
    camera.aspect = W / H; camera.updateProjectionMatrix();
    camera.position.z = W < 500 ? 16 : 13;
  }).observe(container);

  idle();
  return { idle, reveal, result, setScanning, setImage, setSymbol };
}
