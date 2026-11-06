import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const CAM_COLORS = [
  new THREE.Vector3(0.937, 0.267, 0.267), // cam_A: Red (#ef4444)
  new THREE.Vector3(0.133, 0.773, 0.369), // cam_B: Green (#22c55e)
  new THREE.Vector3(0.231, 0.510, 0.965), // cam_C: Blue (#3b82f6)
  new THREE.Vector3(0.961, 0.620, 0.043), // cam_D: Amber (#f59e0b)
];

// ---------------------------------------------------------------------------
// 1. 깊이 그림자 맵 셰이더 (카메라별 선형 깊이 z_cv / uFar 를 RGBA 32비트로 패킹)
// ---------------------------------------------------------------------------
const depthVertexShader = /* glsl */ `
  varying float vDepthCv;
  void main() {
    vec4 worldPos = modelMatrix * vec4(position, 1.0);
    vec4 viewPos = viewMatrix * worldPos;
    // OpenGL 카메라 공간에서 정면은 -Z 이므로 OpenCV 양수 깊이 z_cv = -viewPos.z
    vDepthCv = -viewPos.z;
    gl_Position = projectionMatrix * viewPos;
  }
`;

const depthFragmentShader = /* glsl */ `
  #include <packing>
  uniform float uFar;
  varying float vDepthCv;
  void main() {
    float normDepth = clamp(vDepthCv / uFar, 0.0, 1.0);
    gl_FragColor = packDepthToRGBA(normDepth);
  }
`;

// ---------------------------------------------------------------------------
// 2. 시점 의존 텍스처(VDT) 셰이더
//    - uRenderMode == 0: 왼쪽 화면 (원래 구운 텍스처 / 정점색 그대로)
//    - uRenderMode == 1: 오른쪽 화면 (4대 카메라 시점 의존 투영 + 미관측 원래 텍스처 폴백)
//    - uRenderMode == 2: 오른쪽 화면 (카메라별 가중치 색상 시각화)
// ---------------------------------------------------------------------------
const vdtVertexShader = /* glsl */ `
  #ifdef USE_VERTEX_COLOR
    attribute vec3 color;
    varying vec3 vColor;
  #endif
  varying vec2 vUv;
  varying vec3 vWorldPos;
  varying vec3 vWorldNormal;

  void main() {
    #ifdef USE_VERTEX_COLOR
      vColor = color;
    #endif
    #ifdef USE_UV_ATTR
      vUv = uv;
    #else
      vUv = vec2(0.0);
    #endif
    vec4 worldPosition = modelMatrix * vec4(position, 1.0);
    vWorldPos = worldPosition.xyz;
    vWorldNormal = normalize(mat3(modelMatrix) * normal);
    gl_Position = projectionMatrix * viewMatrix * worldPosition;
  }
`;

const vdtFragmentShader = /* glsl */ `
  #include <packing>

  uniform int uRenderMode;          // 0: Original, 1: VDT, 2: Weight Vis
  uniform int uHasBaseMap;          // 1: 텍스처 아틀라스 있음
  uniform int uHasVertexColor;      // 1: COLOR_0 정점색 있음 (unobserved_neutral)
  uniform int uIsEstimatedPlane;    // 1: 추정 바닥 평면 (투영 제외, 원본 유지)
  uniform int uProjectUnobserved;   // 1: 미관측 TSDF 면에도 투영 허용
  uniform int uColorCorrection;     // 1: 카메라별 노출/색 보정 적용
  uniform int uTopK;                // 2 또는 3 (상위 K대 카메라 블렌딩)
  uniform float uFar;
  uniform vec2 uShadowTexelSize;

  uniform sampler2D uBaseMap;
  uniform vec3 uBaseFactorLinear;

  uniform sampler2D uCamTex0;
  uniform sampler2D uCamTex1;
  uniform sampler2D uCamTex2;
  uniform sampler2D uCamTex3;

  uniform sampler2D uCamDepth0;
  uniform sampler2D uCamDepth1;
  uniform sampler2D uCamDepth2;
  uniform sampler2D uCamDepth3;

  uniform mat3 uCamR[4];            // GLB World -> OpenCV Camera 회전 행렬
  uniform vec3 uCamT[4];            // GLB World -> OpenCV Camera 이동 벡터
  uniform vec4 uCamIntr[4];         // (fx/W, fy/H, cx/W, cy/H) 정규화 내부 파라미터
  uniform vec3 uCamPos[4];          // GLB World 카메라 중심 위치
  uniform vec3 uCamGain[4];         // 카메라별 sRGB 노출/색 보정 계수
  uniform vec3 uCamVisColor[4];     // 가중치 시각화용 카메라 고유색

  #ifdef USE_VERTEX_COLOR
    varying vec3 vColor;
  #endif
  varying vec2 vUv;
  varying vec3 vWorldPos;
  varying vec3 vWorldNormal;

  vec3 linearToSrgb(vec3 lin) {
    vec3 c = clamp(lin, 0.0, 1.0);
    vec3 lo = c * 12.92;
    vec3 hi = 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055;
    return mix(lo, hi, step(vec3(0.0031308), c));
  }

  vec3 getOriginalBaseColor(vec3 N) {
    if (uHasBaseMap == 1) {
      return texture2D(uBaseMap, vUv).rgb;
    }
    vec3 lin = uBaseFactorLinear;
    #ifdef USE_VERTEX_COLOR
      if (uHasVertexColor == 1) {
        lin *= vColor;
      }
    #endif
    vec3 srgb = linearToSrgb(lin);
    if (uIsEstimatedPlane == 0) {
      // 텍스처 없는 정점색 메시에 입체감을 주는 완만한 조명 (SceneBackground 규약과 동일 취지)
      vec3 lightDir = normalize(vec3(0.35, 0.88, 0.32));
      float shade = 0.78 + 0.22 * abs(dot(N, lightDir));
      srgb *= shade;
    }
    return srgb;
  }

  float readShadowDepth(int idx, vec2 uv) {
    vec4 rgba;
    if (idx == 0) rgba = texture2D(uCamDepth0, uv);
    else if (idx == 1) rgba = texture2D(uCamDepth1, uv);
    else if (idx == 2) rgba = texture2D(uCamDepth2, uv);
    else rgba = texture2D(uCamDepth3, uv);
    return unpackRGBAToDepth(rgba) * uFar;
  }

  vec3 readCamPhoto(int idx, vec2 uv) {
    vec3 c;
    if (idx == 0) c = texture2D(uCamTex0, uv).rgb;
    else if (idx == 1) c = texture2D(uCamTex1, uv).rgb;
    else if (idx == 2) c = texture2D(uCamTex2, uv).rgb;
    else c = texture2D(uCamTex3, uv).rgb;
    if (uColorCorrection == 1) {
      c = clamp(c * uCamGain[idx], 0.0, 1.0);
    }
    return c;
  }

  float evaluateCameraWeight(int idx, vec3 P, vec3 N, vec3 viewDir, out vec2 outUv) {
    // 1. GLB 월드 점 P -> OpenCV 카메라 좌표 p_cv = R * P + t
    vec3 pCv = uCamR[idx] * P + uCamT[idx];
    float zCv = pCv.z;
    if (zCv <= 0.20 || zCv >= uFar - 0.5) {
      outUv = vec2(0.0);
      return 0.0;
    }

    // 2. 정규화 이미지 좌표 (u_img, v_img) in [0, 1]^2 (왼쪽 위 원점)
    vec4 intr = uCamIntr[idx];
    float uImg = intr.x * (pCv.x / zCv) + intr.z;
    float vImg = intr.y * (pCv.y / zCv) + intr.w;

    float margin = 0.005;
    if (uImg <= margin || uImg >= 1.0 - margin || vImg <= margin || vImg >= 1.0 - margin) {
      outUv = vec2(0.0);
      return 0.0;
    }

    // WebGL 텍스처 UV (아래쪽 원점 -> 1.0 - vImg)
    vec2 uv = vec2(uImg, 1.0 - vImg);
    outUv = uv;

    // 3. 표면 법선 정면성 검사 (표면이 카메라를 향해야 함)
    // 바닥 근처(GLB Y < 0.20m)는 TSDF 2~4cm 잔물결로 인한 법선 뒤집힘·떨림을 막기 위해 거시적 바닥 법선(+Y)으로 안정화
    vec3 effN = N;
    bool isFloor = (P.y < 0.20) && (abs(N.y) > 0.35);
    if (isFloor) {
      vec3 upN = (effN.y < 0.0) ? -effN : effN;
      effN = normalize(mix(upN, vec3(0.0, 1.0, 0.0), 0.80));
    }

    vec3 toCam = normalize(uCamPos[idx] - P);
    float cosNorm = dot(effN, toCam);
    float minCosNorm = isFloor ? 0.08 : ((uHasBaseMap == 1) ? 0.15 : 0.45);
    if (cosNorm <= minCosNorm) {
      return 0.0;
    }
    float wNorm = smoothstep(minCosNorm, minCosNorm + 0.20, cosNorm) * pow(cosNorm, 1.3);

    // 4. 깊이 그림자 맵 가림 검사
    //    - 중심+주변 4탭 중 가장 가까운 표면 깊이 차이(minSignedDiff)로 자기 가림(shadow acne)을 방지하고,
    //    - 바닥(isFloor)은 TSDF 3cm 잔물결의 얕은 자체 그림자(0.05~0.20m)를 허용하되 실제 가구 가림(>0.45m)은 차단한다.
    vec2 dx = vec2(uShadowTexelSize.x * 1.0, 0.0);
    vec2 dy = vec2(0.0, uShadowTexelSize.y * 1.0);
    float d0 = readShadowDepth(idx, uv);
    float d1 = readShadowDepth(idx, uv + dx);
    float d2 = readShadowDepth(idx, uv - dx);
    float d3 = readShadowDepth(idx, uv + dy);
    float d4 = readShadowDepth(idx, uv - dy);

    float minSignedDiff = min(zCv - d0, min(min(zCv - d1, zCv - d2), min(zCv - d3, zCv - d4)));
    float dMin = min(d0, min(min(d1, d2), min(d3, d4)));

    float slopeScale = 1.0 / max(cosNorm, 0.20);
    float tolOccl = isFloor
      ? max(0.22, 0.042 * zCv * slopeScale)
      : ((uHasBaseMap == 1) ? max(0.075, 0.022 * zCv * slopeScale) : max(0.045, 0.012 * zCv * slopeScale));
    if (minSignedDiff > tolOccl) {
      return 0.0;
    }

    // 전경 물체 실루엣 테두리 번짐 차단
    float tolCliff = isFloor
      ? max(0.35, 0.065 * zCv * slopeScale)
      : max(0.20, 0.045 * zCv * slopeScale);
    float cliffDiff = zCv - dMin;
    if (cliffDiff > tolCliff) {
      return 0.0;
    }
    float wDepth = (1.0 - smoothstep(tolOccl * 0.55, tolOccl, max(0.0, minSignedDiff))) *
                   (1.0 - smoothstep(tolCliff * 0.65, tolCliff, max(0.0, cliffDiff)));

    // 5. 화면 가장자리 감쇠 (렌즈 왜곡 펴기 경계 부근 부드럽게 감쇠)
    float edgeDist = min(min(uImg, 1.0 - uImg), min(vImg, 1.0 - vImg));
    float wBorder = smoothstep(margin, 0.09, edgeDist);

    // 6. 시점 각도 가중치 (Unstructured Lumigraph: 보는 방향 viewDir 과 toCam 의 각도 차가 작을수록 크게)
    float cosAngle = clamp(dot(viewDir, toCam), -1.0, 1.0);
    // 카메라 바로 근처에서는 해당 카메라 가중치가 지배적이 되도록 epipolar 일관성 부여
    float wView = exp(8.5 * (cosAngle - 1.0)) / max(1.0 - cosAngle, 0.001);

    // 거리 감쇠 (더 가까운 카메라가 해상도가 높음)
    float dist = length(uCamPos[idx] - P);
    float wDist = 1.0 / (1.0 + 0.05 * dist * dist);

    return wDepth * wNorm * wBorder * wView * wDist;
  }

  void main() {
    vec3 N = normalize(vWorldNormal);
    vec3 origColor = getOriginalBaseColor(N);

    // 왼쪽 화면(원래 구운 텍스처) 또는 추정 바닥 평면 또는 미관측 투영 끔 상태
    if (uRenderMode == 0 || uIsEstimatedPlane == 1 || (uHasBaseMap == 0 && uProjectUnobserved == 0)) {
      if (uRenderMode == 2) {
        float luma = dot(origColor, vec3(0.2126, 0.7152, 0.0722));
        gl_FragColor = vec4(vec3(luma * 0.35 + 0.08), 1.0);
      } else {
        gl_FragColor = vec4(origColor, 1.0);
      }
      return;
    }

    vec3 viewDir = normalize(cameraPosition - vWorldPos);

    vec2 uv0, uv1, uv2, uv3;
    float w0 = evaluateCameraWeight(0, vWorldPos, N, viewDir, uv0);
    float w1 = evaluateCameraWeight(1, vWorldPos, N, viewDir, uv1);
    float w2 = evaluateCameraWeight(2, vWorldPos, N, viewDir, uv2);
    float w3 = evaluateCameraWeight(3, vWorldPos, N, viewDir, uv3);

    // 4개 가중치 내림차순 정렬 네트워크 (sw0 >= sw1 >= sw2 >= sw3)
    float sw0 = w0, sw1 = w1, sw2 = w2, sw3 = w3;
    float tmp;
    if (sw0 < sw1) { tmp = sw0; sw0 = sw1; sw1 = tmp; }
    if (sw2 < sw3) { tmp = sw2; sw2 = sw3; sw3 = tmp; }
    if (sw0 < sw2) { tmp = sw0; sw0 = sw2; sw2 = tmp; }
    if (sw1 < sw3) { tmp = sw1; sw1 = sw3; sw3 = tmp; }
    if (sw1 < sw2) { tmp = sw1; sw1 = sw2; sw2 = tmp; }

    // 상위 K대(2 또는 3)만 남기되 C0 연속성을 위해 (K+1)번째 가중치와 1위 대비 최소 비율(14%) 중 큰 값을 문턱값으로 차감
    float kThreshold = (uTopK <= 2) ? sw2 : sw3;
    float threshold = max(kThreshold, sw0 * 0.14);
    float tw0 = max(0.0, w0 - threshold);
    float tw1 = max(0.0, w1 - threshold);
    float tw2 = max(0.0, w2 - threshold);
    float tw3 = max(0.0, w3 - threshold);

    float sumW = tw0 + tw1 + tw2 + tw3;
    if (sumW <= 1e-6) {
      // 동점 예외 처리
      tw0 = w0; tw1 = w1; tw2 = w2; tw3 = w3;
      sumW = tw0 + tw1 + tw2 + tw3;
    }

    if (sumW <= 1e-6) {
      // 아무 카메라도 보지 못하는 영역 -> 지어내지 않고 원래 구운 텍스처 그대로 사용
      if (uRenderMode == 2) {
        float luma = dot(origColor, vec3(0.2126, 0.7152, 0.0722));
        gl_FragColor = vec4(vec3(luma * 0.35 + 0.08), 1.0);
      } else {
        gl_FragColor = vec4(origColor, 1.0);
      }
      return;
    }

    float nw0 = tw0 / sumW;
    float nw1 = tw1 / sumW;
    float nw2 = tw2 / sumW;
    float nw3 = tw3 / sumW;

    if (uRenderMode == 2) {
      vec3 weightCol = nw0 * uCamVisColor[0] + nw1 * uCamVisColor[1] + nw2 * uCamVisColor[2] + nw3 * uCamVisColor[3];
      float luma = dot(origColor, vec3(0.2126, 0.7152, 0.0722));
      gl_FragColor = vec4(mix(weightCol, vec3(luma), 0.22), 1.0);
      return;
    }

    vec3 vdtColor = vec3(0.0);
    if (nw0 > 0.0) vdtColor += nw0 * readCamPhoto(0, uv0);
    if (nw1 > 0.0) vdtColor += nw1 * readCamPhoto(1, uv1);
    if (nw2 > 0.0) vdtColor += nw2 * readCamPhoto(2, uv2);
    if (nw3 > 0.0) vdtColor += nw3 * readCamPhoto(3, uv3);

    // 관측 경계에서 원래 구운 텍스처와 미세하게 블렌딩해 급격한 경계 단차 완화
    float confidence = clamp(sw0 * 25.0, 0.0, 1.0);
    vec3 finalColor = mix(origColor, vdtColor, confidence);
    gl_FragColor = vec4(finalColor, 1.0);
  }
`;

// ---------------------------------------------------------------------------
// 3. 메인 애플리케이션 초기화 및 렌더 루프
// ---------------------------------------------------------------------------
const canvas = document.getElementById("webgl-canvas");
const fpsEl = document.getElementById("fps-val");
const viewNameEl = document.getElementById("view-name");
const metaInfoEl = document.getElementById("meta-info");
const legendBox = document.getElementById("legend-box");

const renderer = new THREE.WebGLRenderer({
  canvas,
  antialias: true,
  preserveDrawingBuffer: true,
  powerPreference: "high-performance",
});
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.autoClear = false;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0f17);

const camera = new THREE.PerspectiveCamera(60, (window.innerWidth * 0.5) / window.innerHeight, 0.1, 100.0);
camera.position.set(4.0, 3.6, -4.0);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.target.set(3.8, 0.8, -4.0);
controls.update();

// 1x1 더미 텍스처
const dummyTex = new THREE.DataTexture(new Uint8Array([128, 128, 128, 255]), 1, 1);
dummyTex.colorSpace = THREE.NoColorSpace;
dummyTex.needsUpdate = true;

// 모든 VDT 재질이 공유하는 유니폼
const sharedUniforms = {
  uRenderMode: { value: 1 },
  uProjectUnobserved: { value: 1 },
  uColorCorrection: { value: 1 },
  uTopK: { value: 3 },
  uFar: { value: 25.0 },
  uShadowTexelSize: { value: new THREE.Vector2(1 / 1536, 1 / 864) },
  uCamTex0: { value: dummyTex },
  uCamTex1: { value: dummyTex },
  uCamTex2: { value: dummyTex },
  uCamTex3: { value: dummyTex },
  uCamDepth0: { value: dummyTex },
  uCamDepth1: { value: dummyTex },
  uCamDepth2: { value: dummyTex },
  uCamDepth3: { value: dummyTex },
  uCamR: { value: [new THREE.Matrix3(), new THREE.Matrix3(), new THREE.Matrix3(), new THREE.Matrix3()] },
  uCamT: { value: [new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3()] },
  uCamIntr: { value: [new THREE.Vector4(), new THREE.Vector4(), new THREE.Vector4(), new THREE.Vector4()] },
  uCamPos: { value: [new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3()] },
  uCamGain: {
    value: [
      new THREE.Vector3(1, 1, 1),
      new THREE.Vector3(1, 1, 1),
      new THREE.Vector3(1, 1, 1),
      new THREE.Vector3(1, 1, 1),
    ],
  },
  uCamVisColor: { value: CAM_COLORS },
};

let camerasMeta = null;
let gltfRoot = null;
const shadowMeshes = [];
const vdtMaterials = [];
let frustumGroup = null;
let currentViewKey = "obl1";
let currentFps = 60.0;

function loadTextureNoColorSpace(url) {
  return new Promise((resolve, reject) => {
    const loader = new THREE.TextureLoader();
    loader.load(
      url,
      (tex) => {
        tex.colorSpace = THREE.NoColorSpace;
        tex.minFilter = THREE.LinearFilter;
        tex.magFilter = THREE.LinearFilter;
        tex.generateMipmaps = false;
        tex.wrapS = THREE.ClampToEdgeWrapping;
        tex.wrapT = THREE.ClampToEdgeWrapping;
        tex.needsUpdate = true;
        resolve(tex);
      },
      undefined,
      reject
    );
  });
}

function buildCameraRigFromMeta(camMeta) {
  const cam = new THREE.Camera();
  cam.matrixAutoUpdate = false;
  cam.matrixWorld.fromArray(camMeta.three_matrix_world_col_major);
  cam.matrixWorldInverse.fromArray(camMeta.three_view_matrix_col_major);
  cam.projectionMatrix.fromArray(camMeta.three_projection_matrix_col_major);
  cam.projectionMatrixInverse.copy(cam.projectionMatrix).invert();
  return cam;
}

function renderShadowMaps(shadowLongSide = 1536) {
  if (!camerasMeta || shadowMeshes.length === 0) return;

  const aspect = camerasMeta.cameras[0].image_size[1] / camerasMeta.cameras[0].image_size[0];
  const w = shadowLongSide;
  const h = Math.max(1, Math.round(shadowLongSide * aspect));
  sharedUniforms.uShadowTexelSize.value.set(1.0 / w, 1.0 / h);

  const depthMat = new THREE.ShaderMaterial({
    vertexShader: depthVertexShader,
    fragmentShader: depthFragmentShader,
    uniforms: { uFar: { value: sharedUniforms.uFar.value } },
    side: THREE.DoubleSide,
  });

  // 4대 카메라 깊이 그림자 렌더용 임시 Scene
  const shadowScene = new THREE.Scene();
  shadowScene.background = new THREE.Color(0xffffff);
  const tempClones = [];
  for (const srcMesh of shadowMeshes) {
    const m = new THREE.Mesh(srcMesh.geometry, depthMat);
    m.matrixAutoUpdate = false;
    m.matrix.copy(srcMesh.matrixWorld);
    m.matrixWorld.copy(srcMesh.matrixWorld);
    shadowScene.add(m);
    tempClones.push(m);
  }

  const prevTarget = renderer.getRenderTarget();
  const prevScissor = renderer.getScissorTest();
  renderer.setScissorTest(false);

  for (let i = 0; i < 4; i++) {
    const key = `uCamDepth${i}`;
    const oldRt = sharedUniforms[key].value;
    if (oldRt && oldRt.isRenderTarget) {
      oldRt.dispose();
    }
    const rt = new THREE.WebGLRenderTarget(w, h, {
      minFilter: THREE.NearestFilter,
      magFilter: THREE.NearestFilter,
      format: THREE.RGBAFormat,
      type: THREE.UnsignedByteType,
      depthBuffer: true,
      stencilBuffer: false,
    });
    rt.texture.colorSpace = THREE.NoColorSpace;
    rt.texture.generateMipmaps = false;

    const camRig = buildCameraRigFromMeta(camerasMeta.cameras[i]);
    renderer.setRenderTarget(rt);
    renderer.setClearColor(0xffffff, 1.0);
    renderer.clear(true, true, false);
    renderer.render(shadowScene, camRig);

    sharedUniforms[key].value = rt.texture;
    sharedUniforms[key]._rt = rt;
  }

  renderer.setRenderTarget(prevTarget);
  renderer.setScissorTest(prevScissor);
  depthMat.dispose();
}

function buildFrustumHelpers(cameras) {
  const group = new THREE.Group();
  group.visible = false;
  const hexColors = [0xef4444, 0x22c55e, 0x3b82f6, 0xf59e0b];
  cameras.forEach((c, idx) => {
    const sphere = new THREE.Mesh(
      new THREE.SphereGeometry(0.14, 16, 16),
      new THREE.MeshBasicMaterial({ color: hexColors[idx] })
    );
    sphere.position.fromArray(c.position_glb);
    group.add(sphere);

    const dir = new THREE.Vector3().fromArray(c.forward_glb).normalize();
    const arrow = new THREE.ArrowHelper(dir, sphere.position, 0.85, hexColors[idx], 0.22, 0.14);
    group.add(arrow);
  });
  return group;
}

function computeWeldedSmoothNormals(meshes) {
  // UV 경계에서 분리된 정점들을 5cm 격자 키로 묶어 거시적 표면 법선을 구한 뒤
  // 원래 정점 법선과 섞어 TSDF 잔물결로 인한 법선 떨림(얼룩무늬)을 완화한다.
  const gridSum = new Map();
  const cell = 0.06; // 6cm

  for (const node of meshes) {
    if (!node.geometry.attributes.normal) {
      node.geometry.computeVertexNormals();
    }
    const pos = node.geometry.attributes.position;
    const norm = node.geometry.attributes.normal;
    for (let i = 0; i < pos.count; i++) {
      const kx = Math.round(pos.getX(i) / cell);
      const ky = Math.round(pos.getY(i) / cell);
      const kz = Math.round(pos.getZ(i) / cell);
      const key = `${kx},${ky},${kz}`;
      let acc = gridSum.get(key);
      if (!acc) {
        acc = [0, 0, 0];
        gridSum.set(key, acc);
      }
      acc[0] += norm.getX(i);
      acc[1] += norm.getY(i);
      acc[2] += norm.getZ(i);
    }
  }

  for (const node of meshes) {
    const pos = node.geometry.attributes.position;
    const norm = node.geometry.attributes.normal;
    for (let i = 0; i < pos.count; i++) {
      const kx = Math.round(pos.getX(i) / cell);
      const ky = Math.round(pos.getY(i) / cell);
      const kz = Math.round(pos.getZ(i) / cell);
      const acc = gridSum.get(`${kx},${ky},${kz}`);
      if (acc) {
        const len = Math.hypot(acc[0], acc[1], acc[2]);
        if (len > 1e-6) {
          const sx = acc[0] / len;
          const sy = acc[1] / len;
          const sz = acc[2] / len;
          const ox = norm.getX(i);
          const oy = norm.getY(i);
          const oz = norm.getZ(i);
          const mx = ox * 0.3 + sx * 0.7;
          const my = oy * 0.3 + sy * 0.7;
          const mz = oz * 0.3 + sz * 0.7;
          const mlen = Math.hypot(mx, my, mz) || 1.0;
          norm.setXYZ(i, mx / mlen, my / mlen, mz / mlen);
        }
      }
    }
    norm.needsUpdate = true;
  }
}

function createVdtMaterialForMesh(mesh) {
  const origMat = Array.isArray(mesh.material) ? mesh.material[0] : mesh.material;
  const baseMap = origMat && origMat.map ? origMat.map : null;
  if (baseMap) {
    baseMap.colorSpace = THREE.NoColorSpace;
    baseMap.needsUpdate = true;
  }

  const hasVertexColor = Boolean(mesh.geometry.attributes.color);
  const isEstimatedPlane =
    mesh.name === "estimated_floor_plane" || (origMat && origMat.name === "estimated_floor_plane");

  const factorColor = origMat && origMat.color ? origMat.color.clone() : new THREE.Color(1, 1, 1);

  const defines = {};
  if (hasVertexColor) defines.USE_VERTEX_COLOR = "";
  if (mesh.geometry.attributes.uv) defines.USE_UV_ATTR = "";

  const mat = new THREE.ShaderMaterial({
    name: `vdt_${mesh.name}`,
    defines,
    vertexShader: vdtVertexShader,
    fragmentShader: vdtFragmentShader,
    side: THREE.DoubleSide,
    uniforms: {
      ...sharedUniforms,
      uHasBaseMap: { value: baseMap ? 1 : 0 },
      uHasVertexColor: { value: hasVertexColor ? 1 : 0 },
      uIsEstimatedPlane: { value: isEstimatedPlane ? 1 : 0 },
      uBaseMap: { value: baseMap || dummyTex },
      uBaseFactorLinear: { value: new THREE.Vector3(factorColor.r, factorColor.g, factorColor.b) },
    },
  });
  vdtMaterials.push(mat);
  return mat;
}

export function applyPresetView(viewKey) {
  if (!camerasMeta) return;
  currentViewKey = viewKey;
  viewNameEl.textContent = viewKey;

  document.querySelectorAll("#control-bar button[data-view]").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("data-view") === viewKey);
  });

  const prevDamping = controls.enableDamping;
  controls.enableDamping = false;

  const camEntry = camerasMeta.cameras.find((c) => c.camera_id === viewKey);
  if (camEntry) {
    camera.fov = camEntry.vfov_deg;
    camera.up.fromArray(camEntry.up_glb);
    camera.position.fromArray(camEntry.position_glb);
    controls.target.fromArray(camEntry.look_target_glb);
    camera.updateProjectionMatrix();
    controls.update();
    controls.enableDamping = prevDamping;
    return;
  }

  const freeEntry = camerasMeta.free_views[viewKey];
  if (freeEntry) {
    camera.fov = freeEntry.vfov_deg;
    camera.up.fromArray(freeEntry.up_glb);
    camera.position.fromArray(freeEntry.position_glb);
    controls.target.fromArray(freeEntry.target_glb);
    camera.updateProjectionMatrix();
    controls.update();
  }
  controls.enableDamping = prevDamping;
}

export function renderSingleFrame() {
  const fullW = window.innerWidth;
  const fullH = window.innerHeight;
  const halfW = Math.floor(fullW * 0.5);

  camera.aspect = halfW / Math.max(1, fullH);
  camera.updateProjectionMatrix();

  renderer.setScissorTest(true);

  // 1) 왼쪽: 원래 구운 텍스처 (uRenderMode = 0)
  sharedUniforms.uRenderMode.value = 0;
  renderer.setViewport(0, 0, halfW, fullH);
  renderer.setScissor(0, 0, halfW, fullH);
  renderer.setClearColor(0x0b0f17, 1.0);
  renderer.clear(true, true, false);
  renderer.render(scene, camera);

  // 2) 오른쪽: 시점 의존 텍스처(uRenderMode = 1) 또는 가중치 시각화(uRenderMode = 2)
  const showWeights = document.getElementById("chk-weights").checked;
  sharedUniforms.uRenderMode.value = showWeights ? 2 : 1;
  renderer.setViewport(halfW, 0, fullW - halfW, fullH);
  renderer.setScissor(halfW, 0, fullW - halfW, fullH);
  renderer.setClearColor(0x0b0f17, 1.0);
  renderer.clear(true, true, false);
  renderer.render(scene, camera);

  renderer.setScissorTest(false);
}

export function captureComparisonDataUrl(mimeType = "image/jpeg", quality = 0.92) {
  renderSingleFrame();
  const w = canvas.width;
  const h = canvas.height;
  const outCanvas = document.createElement("canvas");
  outCanvas.width = w;
  outCanvas.height = h;
  const ctx = outCanvas.getContext("2d");
  ctx.drawImage(canvas, 0, 0);

  // 중앙 구분선
  ctx.fillStyle = "rgba(255, 255, 255, 0.55)";
  ctx.fillRect(Math.floor(w * 0.5) - 1, 0, 2, h);

  // 상단 라벨 바
  const scale = Math.max(1, w / 1600);
  const pad = Math.round(14 * scale);
  const boxH = Math.round(34 * scale);
  ctx.font = `600 ${Math.round(15 * scale)}px "Segoe UI", "Malgun Gothic", sans-serif`;

  const leftText = `원래 맵 (12차 texrecon 구운 텍스처)  |  시점: ${currentViewKey}`;
  const showWeights = document.getElementById("chk-weights").checked;
  const rightText = showWeights
    ? `시점 의존 텍스처 - 카메라별 가중치 시각화 (A:빨강 B:초록 C:파랑 D:노랑)  |  ${currentViewKey}`
    : `시점 의존 텍스처 (VDT 실시간 투영, 상위 ${sharedUniforms.uTopK.value}대 블렌딩)  |  시점: ${currentViewKey}`;

  ctx.fillStyle = "rgba(15, 23, 42, 0.85)";
  ctx.fillRect(pad, pad, Math.min(w * 0.46, ctx.measureText(leftText).width + 24 * scale), boxH);
  ctx.fillRect(
    Math.floor(w * 0.5) + pad,
    pad,
    Math.min(w * 0.46, ctx.measureText(rightText).width + 24 * scale),
    boxH
  );

  ctx.fillStyle = "#e2e8f0";
  ctx.fillText(leftText, pad + 12 * scale, pad + 22 * scale);
  ctx.fillStyle = "#38bdf8";
  ctx.fillText(rightText, Math.floor(w * 0.5) + pad + 12 * scale, pad + 22 * scale);

  return outCanvas.toDataURL(mimeType, quality);
}

async function init() {
  const params = new URLSearchParams(window.location.search);
  const dataPrefix = params.get("data") || ".";

  const resp = await fetch(`${dataPrefix}/cameras.json`);
  if (!resp.ok) {
    throw new Error(`cameras.json 로드 실패: HTTP ${resp.status}`);
  }
  camerasMeta = await resp.json();
  sharedUniforms.uFar.value = camerasMeta.cameras[0].far || 25.0;

  // 카메라 유니폼 설정
  camerasMeta.cameras.forEach((c, idx) => {
    const R = c.R;
    // Three.js Matrix3.set() 은 행 우선(row-major) 인자를 받아 내부 열 우선 배열에 저장하며,
    // GLSL uniform mat3 에 전달되면 uCamR[idx] * P 가 정확히 행렬-벡터 곱 R @ P 가 된다.
    sharedUniforms.uCamR.value[idx].set(
      R[0][0], R[0][1], R[0][2],
      R[1][0], R[1][1], R[1][2],
      R[2][0], R[2][1], R[2][2]
    );
    sharedUniforms.uCamT.value[idx].fromArray(c.t);

    const W = c.image_size[0];
    const H = c.image_size[1];
    const K = c.K;
    sharedUniforms.uCamIntr.value[idx].set(K[0][0] / W, K[1][1] / H, K[0][2] / W, K[1][2] / H);
    sharedUniforms.uCamPos.value[idx].fromArray(c.position_glb);
    sharedUniforms.uCamGain.value[idx].fromArray(c.color_gain_srgb || [1, 1, 1]);
  });

  // 카메라 사진 4장 병렬 로드
  const textures = await Promise.all(
    camerasMeta.cameras.map((c) => loadTextureNoColorSpace(`${dataPrefix}/${c.image}`))
  );
  sharedUniforms.uCamTex0.value = textures[0];
  sharedUniforms.uCamTex1.value = textures[1];
  sharedUniforms.uCamTex2.value = textures[2];
  sharedUniforms.uCamTex3.value = textures[3];

  // GLB 로드
  const gltfLoader = new GLTFLoader();
  const gltf = await new Promise((resolve, reject) => {
    gltfLoader.load(`${dataPrefix}/${camerasMeta.scene_glb}`, resolve, undefined, reject);
  });

  gltfRoot = gltf.scene;
  gltfRoot.updateMatrixWorld(true);

  let totalTriangles = 0;
  const allMeshes = [];
  gltfRoot.traverse((node) => {
    if (!node.isMesh) return;
    if (!node.geometry.attributes.normal) {
      node.geometry.computeVertexNormals();
    }
    const triCount = node.geometry.index
      ? node.geometry.index.count / 3
      : node.geometry.attributes.position.count / 3;
    totalTriangles += Math.round(triCount);

    if (node.name !== "estimated_floor_plane") {
      shadowMeshes.push(node);
    }
    allMeshes.push(node);
  });

  computeWeldedSmoothNormals(shadowMeshes);
  for (const node of allMeshes) {
    node.material = createVdtMaterialForMesh(node);
  }

  scene.add(gltfRoot);

  frustumGroup = buildFrustumHelpers(camerasMeta.cameras);
  scene.add(frustumGroup);

  const initShadowRes = parseInt(document.getElementById("sel-shadow-res").value, 10) || 1536;
  renderShadowMaps(initShadowRes);

  metaInfoEl.textContent = `삼각형 ${totalTriangles.toLocaleString()}개 | 그림자맵 4x${initShadowRes}px | 보정 ${camerasMeta.calibration_version.slice(0, 22)}`;

  const initialView = params.get("view") || "obl1";
  applyPresetView(initialView);

  // 전역 노출 (자동 캡처 및 디버깅용)
  window.__vdtApp = {
    ready: true,
    applyPresetView,
    renderSingleFrame,
    captureComparisonDataUrl,
    setWeightVis(on) {
      const el = document.getElementById("chk-weights");
      el.checked = Boolean(on);
      legendBox.classList.toggle("visible", Boolean(on));
      renderSingleFrame();
    },
    getFps() {
      return currentFps;
    },
    getMeta() {
      return camerasMeta;
    },
  };
}

// UI 이벤트 바인딩
document.querySelectorAll("#control-bar button[data-view]").forEach((btn) => {
  btn.addEventListener("click", () => {
    applyPresetView(btn.getAttribute("data-view"));
  });
});

document.getElementById("chk-weights").addEventListener("change", (e) => {
  legendBox.classList.toggle("visible", e.target.checked);
});

document.getElementById("chk-color-corr").addEventListener("change", (e) => {
  sharedUniforms.uColorCorrection.value = e.target.checked ? 1 : 0;
});

document.getElementById("chk-project-unobs").addEventListener("change", (e) => {
  sharedUniforms.uProjectUnobserved.value = e.target.checked ? 1 : 0;
});

document.getElementById("chk-frustums").addEventListener("change", (e) => {
  if (frustumGroup) frustumGroup.visible = e.target.checked;
});

document.getElementById("sel-topk").addEventListener("change", (e) => {
  sharedUniforms.uTopK.value = parseInt(e.target.value, 10) || 3;
});

document.getElementById("sel-shadow-res").addEventListener("change", (e) => {
  const res = parseInt(e.target.value, 10) || 1536;
  renderShadowMaps(res);
  if (camerasMeta) {
    metaInfoEl.textContent = `그림자맵 4x${res}px 재렌더 완료 | 보정 ${camerasMeta.calibration_version.slice(0, 22)}`;
  }
});

document.getElementById("btn-capture").addEventListener("click", () => {
  const dataUrl = captureComparisonDataUrl("image/jpeg", 0.94);
  const a = document.createElement("a");
  const modeSuffix = document.getElementById("chk-weights").checked ? "_weights" : "";
  a.href = dataUrl;
  a.download = `compare_${currentViewKey}${modeSuffix}.jpg`;
  a.click();
});

controls.addEventListener("start", () => {
  document.querySelectorAll("#control-bar button[data-view]").forEach((b) => b.classList.remove("active"));
  viewNameEl.textContent = `${currentViewKey} (자유 조작)`;
});

window.addEventListener("resize", () => {
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// FPS 측정 및 애니메이션 루프
let frameCount = 0;
let lastFpsTime = performance.now();

function animate(now) {
  requestAnimationFrame(animate);
  controls.update();
  renderSingleFrame();

  frameCount++;
  const elapsed = now - lastFpsTime;
  if (elapsed >= 500) {
    currentFps = Math.round((frameCount * 1000) / elapsed);
    fpsEl.textContent = `${currentFps} fps`;
    frameCount = 0;
    lastFpsTime = now;
  }
}

async function runAutoShotsIfRequested() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("autoshots") !== "1") return;

  const waitFrames = (n) =>
    new Promise((resolve) => {
      let c = 0;
      function step() {
        c++;
        if (c >= n) resolve();
        else requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    });

  // 워밍업
  await waitFrames(15);

  const views = ["cam_A", "cam_B", "cam_C", "cam_D", "top", "obl1", "obl2", "obl3"];
  const savedFiles = [];

  // 1) 기본 [원래 | 시점 의존] 캡처 (8개 시점)
  window.__vdtApp.setWeightVis(false);
  for (const v of views) {
    applyPresetView(v);
    await waitFrames(3);
    const dataUrl = captureComparisonDataUrl("image/jpeg", 0.94);
    const filename = `compare_${v}.jpg`;
    await fetch("/__save_shot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename, dataUrl }),
    });
    savedFiles.push(filename);
  }

  // 2) 가중치 시각화 켜고 주요 시점 캡처
  window.__vdtApp.setWeightVis(true);
  for (const v of ["top", "obl1", "obl2", "cam_A"]) {
    applyPresetView(v);
    await waitFrames(3);
    const dataUrl = captureComparisonDataUrl("image/jpeg", 0.94);
    const filename = `compare_${v}_weights.jpg`;
    await fetch("/__save_shot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename, dataUrl }),
    });
    savedFiles.push(filename);
  }
  window.__vdtApp.setWeightVis(false);

  // 3) 그림자 맵 해상도별(1024, 1536, 2048) 프레임 시간 및 FPS 벤치마크
  const perfByShadowRes = {};
  const gl = renderer.getContext();
  const debugInfo = gl.getExtension("WEBGL_debug_renderer_info");
  const gpuRenderer = debugInfo ? gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);

  for (const res of [1024, 1536, 2048]) {
    const t0Shadow = performance.now();
    renderShadowMaps(res);
    gl.finish();
    const shadowBakeMs = Number((performance.now() - t0Shadow).toFixed(2));

    // rAF 루프 FPS 및 순수 GPU+CPU 프레임 렌더 시간 측정 (90 프레임)
    await waitFrames(10);
    const tStart = performance.now();
    const frameTimes = [];
    for (let i = 0; i < 60; i++) {
      applyPresetView(views[i % views.length]);
      const tf0 = performance.now();
      renderSingleFrame();
      gl.finish();
      frameTimes.push(performance.now() - tf0);
      await waitFrames(1);
    }
    const totalElapsedMs = performance.now() - tStart;
    const rafFps = Number(((60 * 1000) / totalElapsedMs).toFixed(1));
    frameTimes.sort((a, b) => a - b);
    const meanFrameMs = Number((frameTimes.reduce((a, b) => a + b, 0) / frameTimes.length).toFixed(2));
    const p95FrameMs = Number(frameTimes[Math.floor(frameTimes.length * 0.95)].toFixed(2));
    const theoreticalFps = Number((1000.0 / Math.max(meanFrameMs, 0.1)).toFixed(1));

    perfByShadowRes[String(res)] = {
      shadow_map_resolution: `${res}x${Math.round((res * 1152) / 2048)}`,
      shadow_bake_once_ms: shadowBakeMs,
      mean_frame_render_ms: meanFrameMs,
      p95_frame_render_ms: p95FrameMs,
      raf_fps: rafFps,
      render_throughput_fps: theoreticalFps,
    };
  }

  renderShadowMaps(1536);
  applyPresetView("obl1");

  await fetch("/__done", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      gpu_renderer: gpuRenderer,
      viewport_size: [window.innerWidth, window.innerHeight],
      saved_files: savedFiles,
      performance_by_shadow_res: perfByShadowRes,
    }),
  });
}

init()
  .then(() => {
    requestAnimationFrame(animate);
    runAutoShotsIfRequested().catch((e) => console.error("autoshots error:", e));
  })
  .catch((err) => {
    console.error(err);
    metaInfoEl.textContent = `오류: ${err.message}`;
  });
