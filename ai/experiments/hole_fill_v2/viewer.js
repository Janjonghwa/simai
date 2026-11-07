import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const canvas = document.getElementById("webgl-canvas");
const viewNameEl = document.getElementById("view-name");
const metaInfoEl = document.getElementById("meta-info");
const matModeLabelEl = document.getElementById("mat-mode-label");
const divV = document.getElementById("div-v");
const divH = document.getElementById("div-h");
const badgeTl = document.getElementById("badge-tl");
const badgeTr = document.getElementById("badge-tr");
const badgeBl = document.getElementById("badge-bl");
const badgeBr = document.getElementById("badge-br");

const renderer = new THREE.WebGLRenderer({
  canvas,
  antialias: true,
  preserveDrawingBuffer: true,
  powerPreference: "high-performance",
});
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.NoToneMapping;
renderer.autoClear = false;

const camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.1, 100.0);
camera.position.set(4.0, 3.6, -4.0);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.target.set(3.8, 0.8, -4.0);
controls.update();

const VARIANT_SPECS = {
  orig: {
    key: "orig",
    file: "scene_orig.glb",
    title: "① 12차 원본 (current-scene-textured.glb)",
    sub: "바닥·벽 받침 평면 없음 (구멍 = 어두운 빈 공간)",
    color: "#e2e8f0",
  },
  tone: {
    key: "tone",
    file: "scene_tone.glb",
    title: "② 방안 a: 자연 톤 (scene_tone.glb)",
    sub: "사진 깊이일치 투영 + 거리에 따른 완만한 색 번짐",
    color: "#38bdf8",
  },
  tone_grid: {
    key: "tone_grid",
    file: "scene_tone_grid.glb",
    title: "② 방안 a-grid: 자연 톤 + 1m 격자 (scene_tone_grid.glb)",
    sub: "사진 깊이일치 투영 + 자연 톤 + 은은한 1m 격자",
    color: "#38bdf8",
  },
  lama: {
    key: "lama",
    file: "scene_lama.glb",
    title: "③ 방안 b: LaMa 인페인팅 (scene_lama.glb)",
    sub: "사진 깊이일치 투영 + big-lama.pt 구조 연장",
    color: "#34d399",
  },
  classic: {
    key: "classic",
    file: "scene_classic.glb",
    title: "④ 방안 c: 고전 인페인팅 (scene_classic.glb)",
    sub: "사진 깊이일치 투영 + OpenCV Telea + NS 다중 해상도",
    color: "#fbbf24",
  },
  r2_12th_hybrid: {
    key: "r2_12th_hybrid",
    file: "scene_r2_12th_hybrid.glb",
    title: "② 2차(12차 메시, 평면 걷어내기 + 바닥톤·벽LaMa)",
    sub: "평면 6cm/25° 메시 제거 + 경계 Lab 색통계 매칭 + 10cm 페더링",
    color: "#38bdf8",
  },
  r2_12th_tone: {
    key: "r2_12th_tone",
    file: "scene_r2_12th_tone.glb",
    title: "② 2차(12차 메시, 평면 걷어내기 + 전체 자연 톤)",
    sub: "평면 6cm/25° 메시 제거 + 경계 Lab 색통계 매칭 + 10cm 페더링",
    color: "#38bdf8",
  },
  r2_da3_hybrid: {
    key: "r2_da3_hybrid",
    file: "scene_r2_da3_hybrid.glb",
    title: "③ 2차(DA3 정리 메시, 평면 걷어내기 + 바닥톤·벽LaMa)",
    sub: "-528 DA3 clean 메시 + 평면 6cm/25° 제거 + 경계 Lab 매칭",
    color: "#34d399",
  },
  r2_da3_tone: {
    key: "r2_da3_tone",
    file: "scene_r2_da3_tone.glb",
    title: "③ 2차(DA3 정리 메시, 평면 걷어내기 + 전체 자연 톤)",
    sub: "-528 DA3 clean 메시 + 평면 6cm/25° 제거 + 전체 톤",
    color: "#34d399",
  },
  r3_12th_hybrid: {
    key: "r3_12th_hybrid",
    file: "scene_r3_12th_hybrid.glb",
    title: "③ 3차(12차 메시, 관측>=90% 걷어내기 + 3mm 선명화 + 개구부 보존)",
    sub: "관측>=90%만 제거 + 단일카메라 지배적 선택 + 3mm 벽 + see_through",
    color: "#34d399",
  },
  r3_12th_tone: {
    key: "r3_12th_tone",
    file: "scene_r3_12th_tone.glb",
    title: "③ 3차(12차 메시, 관측>=90% 걷어내기 + 3mm 선명화 + 전체 톤)",
    sub: "관측>=90%만 제거 + 단일카메라 지배적 선택 + 3mm 벽 + see_through",
    color: "#34d399",
  },
  r3_da3_hybrid: {
    key: "r3_da3_hybrid",
    file: "scene_r3_da3_hybrid.glb",
    title: "③ 3차(DA3 메시, 관측>=90% 걷어내기 + 3mm 선명화 + 개구부 보존)",
    sub: "DA3 clean + 관측>=90%만 제거 + 단일카메라 선명화 + 3mm 벽 + see_through",
    color: "#34d399",
  },
  r3_da3_tone: {
    key: "r3_da3_tone",
    file: "scene_r3_da3_tone.glb",
    title: "③ 3차(DA3 메시, 관측>=90% 걷어내기 + 3mm 선명화 + 전체 톤)",
    sub: "DA3 clean + 관측>=90%만 제거 + 단일카메라 선명화 + 3mm 벽 + see_through",
    color: "#34d399",
  },
  final_wm2: {
    key: "final_wm2",
    file: "scene_final_wm2.glb",
    title: "② 최종 GLB (WM2 2cm 정밀 메시 + 1~5차 통합 + 압축)",
    sub: "-537 WorldMirror 2.0 + 수평 가구 평면 굽기 + 3mm 벽 + see_through + 압축",
    color: "#38bdf8",
  },
  final_da3: {
    key: "final_da3",
    file: "scene_final_da3.glb",
    title: "③ 최종 GLB (DA3 정리 메시 + 1~5차 통합 + 압축)",
    sub: "-528 DA3 clean + 수평 가구 평면 굽기 + 3mm 벽 + see_through + 압축",
    color: "#34d399",
  },
  final_wm2_uncompressed: {
    key: "final_wm2_uncompressed",
    file: "scene_final_wm2_uncompressed.glb",
    title: "① 비압축 PNG 원본 (scene_final_wm2_uncompressed.glb)",
    sub: "무손실 PNG 아틀라스·평면 텍스처",
    color: "#e2e8f0",
  },
  final_wm2_r4: {
    key: "final_wm2_r4",
    file: "scene_final_wm2_r4.glb",
    title: "① 최종(4차, 수정 전: texrecon 가구 면 그대로)",
    sub: "테이블·벤치 위 VDT 포즈 오차 + 등받이 가림 + 조각보 이음새",
    color: "#fbbf24",
  },
  final_da3_r4: {
    key: "final_da3_r4",
    file: "scene_final_da3_r4.glb",
    title: "① 최종 DA3(4차, 수정 전: texrecon 가구 면 그대로)",
    sub: "DA3 메시 가구 면 수정 전",
    color: "#fbbf24",
  },
};

const variantScenes = {};
let camerasMeta = null;
let reportMeta = null;
let currentViewKey = "obl1";
let currentLayout = "grid4";
let useToneGrid = false;
let currentMatMode = "basic"; // "basic" | "standard"
let splitLeftKey = "orig";
let splitRightKey = "tone";
let singleKey = "tone";

/**
 * 표준 Three.js 조명만 추가된 Scene 생성 (커스텀 셰이더 전혀 없음)
 */
function createStandardScene() {
  const s = new THREE.Scene();
  // 모든 칸에 동일한 차분한 어두운 슬레이트 배경색 적용
  s.background = new THREE.Color(0x111827);

  const amb = new THREE.AmbientLight(0xffffff, 2.75);
  s.add(amb);

  const hemi = new THREE.HemisphereLight(0xf8fafc, 0x334155, 0.35);
  hemi.position.set(0, 10, 0);
  s.add(hemi);

  const dir1 = new THREE.DirectionalLight(0xffffff, 0.30);
  dir1.position.set(4.5, 9.0, 3.5);
  s.add(dir1);

  return s;
}

/**
 * GLTFLoader 가 로드한 메시의 재질을 표준 Three.js MeshBasicMaterial / MeshStandardMaterial 로 설정
 */
function applyStandardMaterialMode(root, mode) {
  root.traverse((node) => {
    if (!node.isMesh) return;
    if (!node.geometry.attributes.normal) {
      node.geometry.computeVertexNormals();
    }
    if (!node.userData.origGltfMaterial) {
      node.userData.origGltfMaterial = node.material;
    }
    const origMat = node.userData.origGltfMaterial;
    const hasMap = Boolean(origMat.map);
    const hasVertexColor = Boolean(node.geometry.attributes.color);
    const side = origMat.side !== undefined ? origMat.side : THREE.FrontSide;

    if (origMat.map) {
      origMat.map.colorSpace = THREE.SRGBColorSpace;
      origMat.map.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
    }

    const alphaTest = origMat.alphaTest || 0;
    const transparent = Boolean(origMat.transparent);

    if (mode === "basic") {
      node.material = new THREE.MeshBasicMaterial({
        map: origMat.map || null,
        color: hasMap ? new THREE.Color(0xffffff) : origMat.color.clone(),
        vertexColors: hasVertexColor,
        side,
        alphaTest,
        transparent,
      });
    } else {
      // 표준 MeshStandardMaterial
      node.material = new THREE.MeshStandardMaterial({
        map: origMat.map || null,
        color: hasMap ? new THREE.Color(0xffffff) : origMat.color.clone(),
        vertexColors: hasVertexColor,
        roughness: 1.0,
        metalness: 0.0,
        side,
        alphaTest,
        transparent,
      });
    }
  });
}

export function setMaterialMode(mode) {
  currentMatMode = mode;
  for (const key of Object.keys(variantScenes)) {
    applyStandardMaterialMode(variantScenes[key], mode);
  }
  if (matModeLabelEl) {
    matModeLabelEl.textContent =
      mode === "hybrid"
        ? "표준 Basic(텍스처)+Standard(미관측)"
        : mode === "standard"
        ? "표준 MeshStandardMaterial"
        : "표준 MeshBasicMaterial";
  }
  renderSingleFrame();
}

export function setCameraView(viewKey) {
  if (!camerasMeta) return;
  currentViewKey = viewKey;
  if (viewNameEl) viewNameEl.textContent = viewKey;

  document.querySelectorAll("#control-bar button[data-view]").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("data-view") === viewKey);
  });

  const prevDamping = controls.enableDamping;
  controls.enableDamping = false;

  const camEntry = camerasMeta.cameras.find((c) => c.camera_id === viewKey);
  if (camEntry) {
    camera.fov = camEntry.vfov_deg;
    camera.userData.baseFov = camEntry.vfov_deg;
    camera.up.fromArray(camEntry.up_glb);
    camera.position.fromArray(camEntry.position_glb);
    controls.target.fromArray(camEntry.look_target_glb);
    camera.updateProjectionMatrix();
    controls.update();
    controls.enableDamping = prevDamping;
    renderSingleFrame();
    return;
  }

  const freeEntry =
    camerasMeta.free_views[viewKey] ||
    {
      table_mix_close: {
        vfov_deg: 42.0,
        up_glb: [0.0, 1.0, 0.0],
        position_glb: [2.45, 2.15, -1.25],
        target_glb: [1.1, 0.72, -2.1],
      },
      bench_south_close: {
        vfov_deg: 44.0,
        up_glb: [0.0, 1.0, 0.0],
        position_glb: [5.35, 2.15, -1.1],
        target_glb: [3.85, 0.55, -2.1],
      },
      tables_north_close: {
        vfov_deg: 46.0,
        up_glb: [0.0, 1.0, 0.0],
        position_glb: [3.65, 2.35, -3.65],
        target_glb: [2.45, 0.68, -5.25],
      },
    }[viewKey];
  if (freeEntry) {
    camera.fov = freeEntry.vfov_deg;
    camera.userData.baseFov = freeEntry.vfov_deg;
    camera.up.fromArray(freeEntry.up_glb);
    camera.position.fromArray(freeEntry.position_glb);
    controls.target.fromArray(freeEntry.target_glb);
    camera.updateProjectionMatrix();
    controls.update();
  }
  controls.enableDamping = prevDamping;
  renderSingleFrame();
}

function renderPane(sceneKey, x, y, w, h) {
  const sc = variantScenes[sceneKey];
  if (!sc) return;
  const aspect = w / Math.max(1, h);
  const baseFov = camera.userData.baseFov || 60;
  camera.aspect = aspect;
  if (aspect < 1.25) {
    // 3칸 가로 분할(aspect ≈ 0.63)에서 좌우 화각이 잘리지 않도록 수직 FOV를 보정해 수평 시야를 보존
    const factor = Math.min(1.55, 1.25 / Math.max(aspect, 0.35));
    const halfRad = Math.atan(Math.tan(THREE.MathUtils.degToRad(baseFov * 0.5)) * factor);
    camera.fov = THREE.MathUtils.radToDeg(halfRad * 2.0);
  } else {
    camera.fov = baseFov;
  }
  camera.updateProjectionMatrix();
  renderer.setViewport(x, y, w, h);
  renderer.setScissor(x, y, w, h);
  renderer.clear(true, true, false);
  renderer.render(sc, camera);
}

function updateDomOverlays() {
  const toneSpec = useToneGrid ? VARIANT_SPECS.tone_grid : VARIANT_SPECS.tone;
  if (currentLayout === "grid4") {
    divV.style.display = "block";
    divH.style.display = "block";
    badgeTl.style.display = "block";
    badgeTr.style.display = "block";
    badgeBl.style.display = "block";
    badgeBr.style.display = "block";
    badgeTr.innerHTML = `${toneSpec.title}<span class="sub">${toneSpec.sub}</span>`;
  } else if (currentLayout === "split2") {
    divV.style.display = "block";
    divH.style.display = "none";
    badgeTl.style.display = "block";
    badgeTr.style.display = "block";
    badgeBl.style.display = "none";
    badgeBr.style.display = "none";
    const lSpec = VARIANT_SPECS[splitLeftKey] || VARIANT_SPECS.orig;
    const rSpec = VARIANT_SPECS[splitRightKey] || toneSpec;
    badgeTl.innerHTML = `${lSpec.title}<span class="sub">${lSpec.sub}</span>`;
    badgeTr.innerHTML = `${rSpec.title}<span class="sub">${rSpec.sub}</span>`;
  } else {
    divV.style.display = "none";
    divH.style.display = "none";
    badgeTl.style.display = "block";
    badgeTr.style.display = "none";
    badgeBl.style.display = "none";
    badgeBr.style.display = "none";
    const sSpec = VARIANT_SPECS[singleKey] || toneSpec;
    badgeTl.innerHTML = `${sSpec.title}<span class="sub">${sSpec.sub}</span>`;
  }
}

export function renderSingleFrame() {
  const fullW = window.innerWidth;
  const fullH = window.innerHeight;
  const halfW = Math.floor(fullW * 0.5);
  const halfH = Math.floor(fullH * 0.5);
  const toneKey = useToneGrid ? "tone_grid" : "tone";

  renderer.setScissorTest(true);
  if (currentLayout === "grid4") {
    // 상단 좌: orig, 상단 우: tone, 하단 좌: lama, 하단 우: classic
    renderPane("orig", 0, halfH, halfW, fullH - halfH);
    renderPane(toneKey, halfW, halfH, fullW - halfW, fullH - halfH);
    renderPane("lama", 0, 0, halfW, halfH);
    renderPane("classic", halfW, 0, fullW - halfW, halfH);
  } else if (currentLayout === "split2") {
    renderPane(splitLeftKey, 0, 0, halfW, fullH);
    renderPane(splitRightKey, halfW, 0, fullW - halfW, fullH);
  } else {
    renderPane(singleKey, 0, 0, fullW, fullH);
  }
  renderer.setScissorTest(false);
}

/**
 * 4칸 비교 [① 12차 원본 | ② 방안 a 자연 톤 | ③ 방안 b LaMa | ④ 방안 c 고전 인페인트] 캡처 DataURL 반환
 */
export function captureGrid4DataUrl(mimeType = "image/jpeg", quality = 0.94, useGridForTone = false) {
  const fullW = window.innerWidth;
  const fullH = window.innerHeight;
  const halfW = Math.floor(fullW * 0.5);
  const halfH = Math.floor(fullH * 0.5);
  const toneKey = useGridForTone ? "tone_grid" : "tone";

  renderer.setScissorTest(true);
  renderPane("orig", 0, halfH, halfW, fullH - halfH);
  renderPane(toneKey, halfW, halfH, fullW - halfW, fullH - halfH);
  renderPane("lama", 0, 0, halfW, halfH);
  renderPane("classic", halfW, 0, fullW - halfW, halfH);
  renderer.setScissorTest(false);

  const w = canvas.width;
  const h = canvas.height;
  const outCanvas = document.createElement("canvas");
  outCanvas.width = w;
  outCanvas.height = h;
  const ctx = outCanvas.getContext("2d");
  ctx.drawImage(canvas, 0, 0);

  // 십자 구분선
  ctx.fillStyle = "rgba(255, 255, 255, 0.58)";
  ctx.fillRect(Math.floor(w * 0.5) - 1, 0, 2, h);
  ctx.fillRect(0, Math.floor(h * 0.5) - 1, w, 2);

  const scale = Math.max(1, w / 1600);
  const pad = Math.round(10 * scale);
  const boxH = Math.round(28 * scale);
  ctx.font = `600 ${Math.round(13 * scale)}px "Segoe UI", "Malgun Gothic", sans-serif`;

  const tSpec = VARIANT_SPECS[toneKey];
  const labels = [
    {
      text: `${VARIANT_SPECS.orig.title} | ${currentViewKey}`,
      color: VARIANT_SPECS.orig.color,
      cx: pad,
      cy: pad,
    },
    {
      text: `${tSpec.title} | ${currentViewKey}`,
      color: tSpec.color,
      cx: Math.floor(w * 0.5) + pad,
      cy: pad,
    },
    {
      text: `${VARIANT_SPECS.lama.title} | ${currentViewKey}`,
      color: VARIANT_SPECS.lama.color,
      cx: pad,
      cy: Math.floor(h * 0.5) + pad,
    },
    {
      text: `${VARIANT_SPECS.classic.title} | ${currentViewKey}`,
      color: VARIANT_SPECS.classic.color,
      cx: Math.floor(w * 0.5) + pad,
      cy: Math.floor(h * 0.5) + pad,
    },
  ];

  for (const lb of labels) {
    const tw = Math.min(w * 0.48, ctx.measureText(lb.text).width + 20 * scale);
    ctx.fillStyle = "rgba(15, 23, 42, 0.88)";
    ctx.fillRect(lb.cx, lb.cy, tw, boxH);
    ctx.fillStyle = lb.color;
    ctx.fillText(lb.text, lb.cx + 10 * scale, lb.cy + 19 * scale);
  }

  renderSingleFrame();
  return outCanvas.toDataURL(mimeType, quality);
}

/**
 * 2칸 좌/우 비교 캡처 DataURL 반환
 */
export function captureSplit2DataUrl(
  leftKey = "tone",
  rightKey = "tone_grid",
  mimeType = "image/jpeg",
  quality = 0.94
) {
  const fullW = window.innerWidth;
  const fullH = window.innerHeight;
  const halfW = Math.floor(fullW * 0.5);

  renderer.setScissorTest(true);
  renderPane(leftKey, 0, 0, halfW, fullH);
  renderPane(rightKey, halfW, 0, fullW - halfW, fullH);
  renderer.setScissorTest(false);

  const w = canvas.width;
  const h = canvas.height;
  const outCanvas = document.createElement("canvas");
  outCanvas.width = w;
  outCanvas.height = h;
  const ctx = outCanvas.getContext("2d");
  ctx.drawImage(canvas, 0, 0);

  ctx.fillStyle = "rgba(255, 255, 255, 0.58)";
  ctx.fillRect(Math.floor(w * 0.5) - 1, 0, 2, h);

  const scale = Math.max(1, w / 1600);
  const pad = Math.round(12 * scale);
  const boxH = Math.round(30 * scale);
  ctx.font = `600 ${Math.round(13.5 * scale)}px "Segoe UI", "Malgun Gothic", sans-serif`;

  const lSpec = VARIANT_SPECS[leftKey];
  const rSpec = VARIANT_SPECS[rightKey];
  const labels = [
    { text: `${lSpec.title} | ${currentViewKey}`, color: lSpec.color, cx: pad, cy: pad },
    {
      text: `${rSpec.title} | ${currentViewKey}`,
      color: rSpec.color,
      cx: Math.floor(w * 0.5) + pad,
      cy: pad,
    },
  ];
  for (const lb of labels) {
    const tw = Math.min(w * 0.48, ctx.measureText(lb.text).width + 22 * scale);
    ctx.fillStyle = "rgba(15, 23, 42, 0.88)";
    ctx.fillRect(lb.cx, lb.cy, tw, boxH);
    ctx.fillStyle = lb.color;
    ctx.fillText(lb.text, lb.cx + 10 * scale, lb.cy + 20 * scale);
  }

  renderSingleFrame();
  return outCanvas.toDataURL(mimeType, quality);
}

/**
 * 3칸 가로 비교 [① 1차 결과 | ② 2차(12차 메시) | ③ 2차(DA3 정리 메시)] 캡처 DataURL 반환
 */
export function captureSplit3DataUrl(
  k1 = "tone",
  k2 = "r2_12th_hybrid",
  k3 = "r2_da3_hybrid",
  mimeType = "image/jpeg",
  quality = 0.94,
  customTitles = null
) {
  const fullW = window.innerWidth;
  const fullH = window.innerHeight;
  const w1 = Math.floor(fullW / 3);
  const w2 = Math.floor((fullW * 2) / 3) - w1;
  const w3 = fullW - w1 - w2;

  renderer.setScissorTest(true);
  renderPane(k1, 0, 0, w1, fullH);
  renderPane(k2, w1, 0, w2, fullH);
  renderPane(k3, w1 + w2, 0, w3, fullH);
  renderer.setScissorTest(false);

  const w = canvas.width;
  const h = canvas.height;
  const outCanvas = document.createElement("canvas");
  outCanvas.width = w;
  outCanvas.height = h;
  const ctx = outCanvas.getContext("2d");
  ctx.drawImage(canvas, 0, 0);

  const x1 = Math.floor(w / 3);
  const x2 = Math.floor((w * 2) / 3);
  ctx.fillStyle = "rgba(255, 255, 255, 0.58)";
  ctx.fillRect(x1 - 1, 0, 2, h);
  ctx.fillRect(x2 - 1, 0, 2, h);

  const scale = Math.max(1, w / 1600);
  const pad = Math.round(10 * scale);
  const boxH = Math.round(28 * scale);
  ctx.font = `600 ${Math.round(12.5 * scale)}px "Segoe UI", "Malgun Gothic", sans-serif`;

  const s1 = VARIANT_SPECS[k1] || VARIANT_SPECS.tone;
  const s2 = VARIANT_SPECS[k2] || VARIANT_SPECS.r2_12th_hybrid;
  const s3 = VARIANT_SPECS[k3] || VARIANT_SPECS.r2_da3_hybrid;
  const t1 = customTitles ? customTitles[0] : `① 1차 결과 (${s1.file})`;
  const t2 = customTitles ? customTitles[1] : s2.title;
  const t3 = customTitles ? customTitles[2] : s3.title;

  const labels = [
    { text: `${t1} | ${currentViewKey}`, color: "#fbbf24", cx: pad, cy: pad, maxW: x1 - pad * 2 },
    { text: `${t2} | ${currentViewKey}`, color: s2.color, cx: x1 + pad, cy: pad, maxW: x2 - x1 - pad * 2 },
    { text: `${t3} | ${currentViewKey}`, color: s3.color, cx: x2 + pad, cy: pad, maxW: w - x2 - pad * 2 },
  ];

  for (const lb of labels) {
    const tw = Math.min(lb.maxW, ctx.measureText(lb.text).width + 18 * scale);
    ctx.fillStyle = "rgba(15, 23, 42, 0.88)";
    ctx.fillRect(lb.cx, lb.cy, tw, boxH);
    ctx.fillStyle = lb.color;
    ctx.fillText(lb.text, lb.cx + 9 * scale, lb.cy + 19 * scale);
  }

  renderSingleFrame();
  return outCanvas.toDataURL(mimeType, quality);
}

async function init() {
  const params = new URLSearchParams(window.location.search);
  const dataPrefix = params.get("data") || ".";

  const [camResp, repResp] = await Promise.all([
    fetch(`${dataPrefix}/cameras.json`),
    fetch(`${dataPrefix}/report.json`).catch(() => null),
  ]);
  if (!camResp.ok) {
    throw new Error(`cameras.json 로드 실패: HTTP ${camResp.status}`);
  }
  camerasMeta = await camResp.json();
  if (repResp && repResp.ok) {
    reportMeta = await repResp.json();
  }

  const loader = new GLTFLoader();
  const loadGlb = (url) =>
    new Promise((resolve, reject) => {
      loader.load(url, resolve, undefined, reject);
    });

  const autoMode = params.get("autoshots");
  let keys;
  if (autoMode === "r5") {
    keys = ["final_wm2_r4", "final_wm2", "final_da3_r4", "final_da3"];
  } else if (autoMode === "r4") {
    keys = ["orig", "final_wm2", "final_da3", "final_wm2_uncompressed"];
  } else if (autoMode === "r3") {
    keys = ["orig", "r2_da3_hybrid", "r3_da3_hybrid", "r2_12th_hybrid", "r3_12th_hybrid"];
  } else if (autoMode === "r2") {
    keys = ["lama", "tone", "r2_12th_hybrid", "r2_12th_tone", "r2_da3_hybrid", "r2_da3_tone"];
  } else if (autoMode === "1") {
    keys = ["orig", "tone", "tone_grid", "lama", "classic"];
  } else {
    keys = Object.keys(VARIANT_SPECS);
  }

  const gltfs = await Promise.all(
    keys.map((k) =>
      loadGlb(`${dataPrefix}/${VARIANT_SPECS[k].file}`).catch((err) => {
        console.warn(`Optional GLB skip (${k}):`, err);
        return null;
      })
    )
  );

  keys.forEach((k, idx) => {
    if (!gltfs[idx]) return;
    const sc = createStandardScene();
    const root = gltfs[idx].scene;
    root.updateMatrixWorld(true);
    applyStandardMaterialMode(root, currentMatMode);
    sc.add(root);
    variantScenes[k] = sc;
  });

  if (metaInfoEl) {
    if (reportMeta && reportMeta.floating_removal) {
      const fr = reportMeta.floating_removal;
      metaInfoEl.textContent = `조각 제거: ${fr.removed_components}개 (${fr.removed_faces}면) | 평면 4개(바닥+벽3) 구움 완료`;
    } else {
      metaInfoEl.textContent = `표준 Three.js GLB 5종 로드 완료`;
    }
  }

  // UI 이벤트 바인딩
  document.querySelectorAll("#control-bar button[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => {
      setCameraView(btn.getAttribute("data-view"));
    });
  });

  const selLayout = document.getElementById("sel-layout");
  if (selLayout) {
    selLayout.addEventListener("change", () => {
      currentLayout = selLayout.value;
      updateDomOverlays();
      renderSingleFrame();
    });
  }

  const chkToneGrid = document.getElementById("chk-tone-grid");
  if (chkToneGrid) {
    chkToneGrid.addEventListener("change", () => {
      useToneGrid = Boolean(chkToneGrid.checked);
      if (splitRightKey === "tone" || splitRightKey === "tone_grid") {
        splitRightKey = useToneGrid ? "tone_grid" : "tone";
      }
      updateDomOverlays();
      renderSingleFrame();
    });
  }

  const selMatMode = document.getElementById("sel-mat-mode");
  if (selMatMode) {
    selMatMode.addEventListener("change", () => {
      setMaterialMode(selMatMode.value);
    });
  }

  const btnCapture = document.getElementById("btn-capture");
  if (btnCapture) {
    btnCapture.addEventListener("click", () => {
      const url = captureGrid4DataUrl("image/jpeg", 0.94, useToneGrid);
      const a = document.createElement("a");
      a.href = url;
      a.download = `hole_fill_v2_${currentViewKey}.jpg`;
      a.click();
    });
  }

  window.addEventListener("resize", () => {
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderSingleFrame();
  });

  controls.addEventListener("change", () => {
    renderSingleFrame();
  });

  setCameraView("obl1");
  updateDomOverlays();

  window.__holeFillV2App = {
    ready: true,
    setView: setCameraView,
    setMaterialMode,
    setToneGrid: (v) => {
      useToneGrid = Boolean(v);
      updateDomOverlays();
      renderSingleFrame();
    },
    captureGrid4DataUrl,
    captureSplit2DataUrl,
    captureSplit3DataUrl,
    renderSingleFrame,
  };

  if (params.get("autoshots") === "1") {
    await runAutoShots();
  } else if (params.get("autoshots") === "r2") {
    await runAutoShotsR2();
  } else if (params.get("autoshots") === "r3") {
    await runAutoShotsR3();
  } else if (params.get("autoshots") === "r4") {
    await runAutoShotsR4();
  } else if (params.get("autoshots") === "r5") {
    await runAutoShotsR5();
  }
}

async function postJson(url, payload) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    throw new Error(`POST ${url} failed: HTTP ${resp.status}`);
  }
}

async function runAutoShots() {
  const views = ["cam_A", "cam_B", "cam_C", "cam_D", "top", "obl1", "obl2", "obl3"];
  const savedFiles = [];

  // 1. 8개 시점 4칸 비교 캡처 ([① 12차 원본 | ② 방안 a 자연 톤 | ③ 방안 b LaMa | ④ 방안 c 고전])
  for (const v of views) {
    setCameraView(v);
    await new Promise((r) => setTimeout(r, 80));
    const dataUrl = captureGrid4DataUrl("image/jpeg", 0.94, false);
    const filename = `compare_${v}.jpg`;
    await postJson("/__save_shot", { filename, dataUrl });
    savedFiles.push(filename);
  }

  // 2. 방안 a (자연 톤) vs 방안 a-grid (1m 격자) 2칸 비교 캡처 (top, obl1)
  for (const v of ["top", "obl1"]) {
    setCameraView(v);
    await new Promise((r) => setTimeout(r, 60));
    const dataUrl = captureSplit2DataUrl("tone", "tone_grid", "image/jpeg", 0.94);
    const filename = `tone_vs_grid_${v}.jpg`;
    await postJson("/__save_shot", { filename, dataUrl });
    savedFiles.push(filename);
  }

  // 3. 순수 MeshStandardMaterial (PBR 조명) 4칸 비교 캡처 (obl1)
  setMaterialMode("standard");
  setCameraView("obl1");
  await new Promise((r) => setTimeout(r, 80));
  const dataUrlStd = captureGrid4DataUrl("image/jpeg", 0.94, false);
  const stdFile = "compare_obl1_pbr_standard.jpg";
  await postJson("/__save_shot", { filename: stdFile, dataUrl: dataUrlStd });
  savedFiles.push(stdFile);

  await postJson("/__done", { ok: true, count: savedFiles.length, files: savedFiles });
}

async function runAutoShotsR2() {
  const views = [
    "cam_A",
    "cam_B",
    "cam_C",
    "cam_D",
    "top",
    "obl1",
    "obl2",
    "obl3",
    "wall_close",
  ];
  const savedFiles = [];

  // 1. 9개 시점 3칸 비교 캡처: [① 1차 결과 (바닥톤+벽LaMa) | ② 2차(12차 메시) | ③ 2차(DA3 정리 메시)]
  for (const v of views) {
    setCameraView(v);
    await new Promise((r) => setTimeout(r, 90));
    const dataUrl = captureSplit3DataUrl(
      "lama",
      "r2_12th_hybrid",
      "r2_da3_hybrid",
      "image/jpeg",
      0.95,
      [
        "① 1차 결과 (바닥톤+벽LaMa, 평면중복)",
        "② 2차 (12차 메시 + 평면단일화·톤일치)",
        "③ 2차 (DA3 정리 메시 -528 + 평면단일화)",
      ]
    );
    const filename = `compare_r2_${v}.jpg`;
    await postJson("/__save_shot", { filename, dataUrl });
    savedFiles.push(filename);
  }

  // 2. 핵심 시점 3칸 비교 캡처 (전체 자연 톤 기준): [① 1차 톤 | ② 2차(12차 톤) | ③ 2차(DA3 톤)]
  for (const v of ["wall_close", "obl1", "top", "cam_D"]) {
    setCameraView(v);
    await new Promise((r) => setTimeout(r, 80));
    const dataUrl = captureSplit3DataUrl(
      "tone",
      "r2_12th_tone",
      "r2_da3_tone",
      "image/jpeg",
      0.95,
      [
        "① 1차 결과 (자연 톤, 평면중복)",
        "② 2차 (12차 메시 + 전체 자연 톤)",
        "③ 2차 (DA3 정리 메시 + 전체 자연 톤)",
      ]
    );
    const filename = `compare_r2_tone_${v}.jpg`;
    await postJson("/__save_shot", { filename, dataUrl });
    savedFiles.push(filename);
  }

  // 3. 나무 벽 클로즈업(wall_close)에서 방안 1(바닥톤+벽LaMa) vs 방안 2(전체 톤) 2칸 비교
  setCameraView("wall_close");
  await new Promise((r) => setTimeout(r, 80));
  const dataUrlWall = captureSplit2DataUrl(
    "r2_da3_hybrid",
    "r2_da3_tone",
    "image/jpeg",
    0.95
  );
  const wallFile = "compare_r2_da3_hybrid_vs_tone_wall_close.jpg";
  await postJson("/__save_shot", { filename: wallFile, dataUrl: dataUrlWall });
  savedFiles.push(wallFile);

  await postJson("/__done", { ok: true, count: savedFiles.length, files: savedFiles });
}

async function runAutoShotsR3() {
  // 3차 지시 4번 + 3차 추가:
  // [① 12차 원본 | ② 2차 | ③ 3차] 3칸 비교 캡처
  // 필수 시점: ssafy_sign (파란 SSAFY 안내판 정면 근접), left_frame (왼쪽 끝 액자 벽 근접),
  // wall_close (나무 벽 근접), obl3 (비스듬히 3), door_corridor (문·복도 개구부 확인 캡처) + 기본 시점
  const views = [
    "ssafy_sign",
    "left_frame",
    "wall_close",
    "obl3",
    "door_corridor",
    "obl1",
    "obl2",
    "top",
    "cam_A",
    "cam_B",
    "cam_C",
    "cam_D",
  ];
  const savedFiles = [];

  // 1. 주력(DA3 정리 메시 + hybrid) 3칸 비교: [① 12차 원본 | ② 2차(DA3) | ③ 3차(DA3)]
  for (const v of views) {
    setCameraView(v);
    await new Promise((r) => setTimeout(r, 90));
    const dataUrl = captureSplit3DataUrl(
      "orig",
      "r2_da3_hybrid",
      "r3_da3_hybrid",
      "image/jpeg",
      0.95,
      [
        "① 12차 원본 (current-scene-textured.glb)",
        "② 2차 (DA3 + 무조건 걷어내기·5mm·개구부 막힘)",
        "③ 3차 (DA3 + 관측>=90% 제거·3mm 선명화·see_through)",
      ]
    );
    const filename = `compare_r3_${v}.jpg`;
    await postJson("/__save_shot", { filename, dataUrl });
    savedFiles.push(filename);
  }

  // 2. 12차 메시 기준 3칸 비교: [① 12차 원본 | ② 2차(12차 메시) | ③ 3차(12차 메시)] (핵심 5개 시점)
  for (const v of ["ssafy_sign", "left_frame", "wall_close", "obl3", "door_corridor"]) {
    setCameraView(v);
    await new Promise((r) => setTimeout(r, 90));
    const dataUrl = captureSplit3DataUrl(
      "orig",
      "r2_12th_hybrid",
      "r3_12th_hybrid",
      "image/jpeg",
      0.95,
      [
        "① 12차 원본 (current-scene-textured.glb)",
        "② 2차 (12차 메시 + 무조건 걷어내기·5mm·개구부 막힘)",
        "③ 3차 (12차 메시 + 관측>=90% 제거·3mm 선명화·see_through)",
      ]
    );
    const filename = `compare_r3_12th_${v}.jpg`;
    await postJson("/__save_shot", { filename, dataUrl });
    savedFiles.push(filename);
  }

  await postJson("/__done", { ok: true, count: savedFiles.length, files: savedFiles });
}

async function runAutoShotsR4() {
  // 4차 지시 5번:
  // [① 12차 원본 | ② 최종(WM2) | ③ 최종(DA3)] 3칸 비교 캡처
  // 8개 시점(카메라 4 + 자유 4) + ssafy_sign + left_frame + wall_close + door_corridor
  const views = [
    "cam_A",
    "cam_B",
    "cam_C",
    "cam_D",
    "top",
    "obl1",
    "obl2",
    "obl3",
    "ssafy_sign",
    "left_frame",
    "wall_close",
    "door_corridor",
  ];
  const savedFiles = [];

  for (const v of views) {
    setCameraView(v);
    await new Promise((r) => setTimeout(r, 90));
    const dataUrl = captureSplit3DataUrl(
      "orig",
      "final_wm2",
      "final_da3",
      "image/jpeg",
      0.95,
      [
        "① 12차 원본 (current-scene-textured.glb)",
        "② 최종 GLB (WM2 -537 정밀 기하 + 1~4차 통합 + 압축)",
        "③ 최종 GLB (DA3 -528 정리 기하 + 1~4차 통합 + 압축)",
      ]
    );
    const filename = `compare_final_${v}.jpg`;
    await postJson("/__save_shot", { filename, dataUrl });
    savedFiles.push(filename);
  }

  // 4차 지시 4번: 텍스처 압축 전후 화질 차이가 눈에 띄지 않음을 보여 주는 비교 캡처 1장 이상
  if (variantScenes.final_wm2_uncompressed && variantScenes.final_wm2) {
    for (const v of ["ssafy_sign", "obl1"]) {
      setCameraView(v);
      await new Promise((r) => setTimeout(r, 90));
      const dataUrlComp = captureSplit2DataUrl(
        "final_wm2_uncompressed",
        "final_wm2",
        "image/jpeg",
        0.96
      );
      const compFile = v === "ssafy_sign" ? "compression_quality_compare.jpg" : `compression_quality_compare_${v}.jpg`;
      await postJson("/__save_shot", { filename: compFile, dataUrl: dataUrlComp });
      savedFiles.push(compFile);
    }
  }

  await postJson("/__done", { ok: true, count: savedFiles.length, files: savedFiles });
}

async function runAutoShotsR5() {
  // 5차 지시 2번:
  // 같은 시점(obl2, top, cam_B 포함 8개 시점 + 테이블·벤치 근접 시점 3곳)에서
  // [① 최종(4차, 수정 전) | ② 수정(5차, 수평 가구 평면 + 일관성 선택)] 2칸 비교 캡처
  const views = [
    "obl2",
    "top",
    "cam_B",
    "cam_A",
    "cam_C",
    "cam_D",
    "obl1",
    "obl3",
    "table_mix_close",
    "bench_south_close",
    "tables_north_close",
  ];
  const savedFiles = [];

  for (const v of views) {
    setCameraView(v);
    await new Promise((r) => setTimeout(r, 90));
    const dataUrl = captureSplit2DataUrl("final_wm2_r4", "final_wm2", "image/jpeg", 0.95);
    const filename = `compare_r5_${v}.jpg`;
    await postJson("/__save_shot", { filename, dataUrl });
    savedFiles.push(filename);
  }

  if (variantScenes.final_da3_r4 && variantScenes.final_da3) {
    for (const v of ["obl2", "top", "cam_B"]) {
      setCameraView(v);
      await new Promise((r) => setTimeout(r, 90));
      const dataUrlDa3 = captureSplit2DataUrl("final_da3_r4", "final_da3", "image/jpeg", 0.95);
      const da3File = `compare_r5_da3_${v}.jpg`;
      await postJson("/__save_shot", { filename: da3File, dataUrl: dataUrlDa3 });
      savedFiles.push(da3File);
    }
  }

  await postJson("/__done", { ok: true, count: savedFiles.length, files: savedFiles });
}

init().catch((err) => {
  console.error("Viewer initialization error:", err);
  if (metaInfoEl) {
    metaInfoEl.textContent = `오류: ${err.message}`;
    metaInfoEl.style.color = "#f87171";
  }
  window.__holeFillV2App = {
    ready: false,
    error: err.message,
  };
});
