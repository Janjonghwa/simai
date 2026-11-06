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
// 0. 배경 그라데이션 패스 셰이더 (2차 지시 #2: 어두운 슬레이트-블루 그라데이션)
// ---------------------------------------------------------------------------
const bgVertexShader = /* glsl */ `
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = vec4(position.xy, 0.9999, 1.0);
  }
`;

const bgFragmentShader = /* glsl */ `
  varying vec2 vUv;
  void main() {
    // 교실 톤에 맞춘 어두운 슬레이트-블루 수직 그라데이션
    vec3 bottomCol = vec3(0.118, 0.153, 0.208); // #1e2735
    vec3 midCol    = vec3(0.157, 0.204, 0.271); // #283445
    vec3 topCol    = vec3(0.094, 0.125, 0.173); // #18202c
    vec3 c = (vUv.y < 0.48)
      ? mix(bottomCol, midCol, smoothstep(0.0, 0.48, vUv.y))
      : mix(midCol, topCol, smoothstep(0.48, 1.0, vUv.y));
    gl_FragColor = vec4(c, 1.0);
  }
`;

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
    // 모든 TSDF 메시(바닥·벽·가구)의 카메라별 실제 깊이를 기록한다.
    // 형상이 없는 구멍 픽셀은 클리어 값(1.0 -> uFar = 25.0m)으로 남아 깊이 맵이 비어 있음을 나타낸다.
    float normDepth = clamp(vDepthCv / uFar, 0.0, 1.0);
    gl_FragColor = packDepthToRGBA(normDepth);
  }
`;

// ---------------------------------------------------------------------------
// 2. 시점 의존 텍스처(VDT) 및 미관측 표시 층(바닥·벽) 통합 셰이더
//    - uRenderMode == 0: 원래 구운 텍스처 / 정점색 그대로
//    - uRenderMode == 1: 4대 카메라 시점 의존 투영 + 미관측 표시 층 + 구멍 경계 페더링
//    - uRenderMode == 2: 카메라별 가중치 색상 시각화
//    - uRenderMode == 3: 2차 지시 #1 단색 진단 보기 (#b8c4d0, DoubleSide)
// ---------------------------------------------------------------------------
const vdtVertexShader = /* glsl */ `
  #ifdef USE_VERTEX_COLOR
    attribute vec3 color;
    varying vec3 vColor;
  #endif
  #ifdef USE_BOUNDARY_DIST
    attribute float aBoundaryDist;
    varying float vBoundaryDist;
  #else
    varying float vBoundaryDist;
  #endif
  varying vec2 vUv;
  varying vec3 vWorldPos;
  varying vec3 vWorldNormal;

  void main() {
    #ifdef USE_VERTEX_COLOR
      vColor = color;
    #endif
    #ifdef USE_BOUNDARY_DIST
      vBoundaryDist = aBoundaryDist;
    #else
      vBoundaryDist = 1.0;
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

  uniform int uRenderMode;          // 0: Original, 1: VDT, 2: Weight Vis, 3: Solid Diagnostic (#b8c4d0)
  uniform int uHasBaseMap;          // 1: 텍스처 아틀라스 있음
  uniform int uHasVertexColor;      // 1: COLOR_0 정점색 있음 (unobserved_neutral)
  uniform int uIsEstimatedPlane;    // 1: 12차 GLB 내 기존 estimated_floor_plane
  uniform int uIsIndicatorPlane;    // 1: 2차 지시 #3/#4 미관측 표시 층 (바닥 또는 벽 평면)
  uniform int uIndicatorKind;       // 0: 바닥 표시 층, 1: 벽 표시 층
  uniform int uProjectUnobserved;   // 1: 미관측 TSDF 면에도 투영 허용
  uniform int uColorCorrection;     // 1: 카메라별 노출/색 보정 적용
  uniform int uUseBgTone;           // 1: 2차 지시 #2 배경 슬레이트 톤 + 완만한 거리 안개
  uniform int uShowFloorLayer;      // 1: 2차 지시 #3 바닥 표시 층 켜짐
  uniform int uShowWallLayer;       // 1: 2차 지시 #4 벽 표시 층 켜짐
  uniform int uEdgeFeather;         // 1: 2차 지시 #5 메시 구멍 경계 부드럽게 섞기
  uniform int uIndicatorDepthMatch; // 1: 3차 지시 #1 표시 층 깊이 일치(5cm 이내 + 형상 존재)일 때만 사진 투영
  uniform int uFeatherPass;         // 0: 단일 패스, 1: 불투명 내부 패스, 2: 반투명 구멍 테두리 패스
  uniform int uTopK;                // 2 또는 3 (상위 K대 카메라 블렌딩)
  uniform float uFar;
  uniform vec2 uShadowTexelSize;

  uniform sampler2D uBaseMap;
  uniform vec3 uBaseFactorLinear;

  // 미관측 표시 층(바닥·벽) 전용 유니폼
  uniform vec3 uIndicatorCalmColor;     // 미관측 영역 차분한 단색 톤 (sRGB)
  uniform vec3 uIndicatorObsColor;      // 해당 평면 관측 중앙값 색 (sRGB, 전경 오투영 억제 기준)
  uniform vec2 uWallTangentWorldXY;     // 벽 평면의 수평 접선 벡터 (World XY)
  uniform vec3 uFloorCalmColorShared;   // 메시 경계 페더링이 참조하는 바닥 단색 톤

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
  varying float vBoundaryDist;
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
      vec3 lightDir = normalize(vec3(0.35, 0.88, 0.32));
      float shade = 0.78 + 0.22 * abs(dot(N, lightDir));
      srgb *= shade;
    }
    return srgb;
  }

  // 1m 주 격자 + 0.5m 보조 격자 (화면 미분 fwidth 로 모아레·계단 현상 방지)
  vec3 evalCalmGridColor(vec2 coordMeters, vec3 baseTone, float viewDist) {
    vec2 fw = max(fwidth(coordMeters), vec2(1e-4));
    vec2 dist1m = abs(fract(coordMeters - 0.5) - 0.5) / fw;
    float line1m = 1.0 - smoothstep(0.0, 1.35, min(dist1m.x, dist1m.y));

    vec2 distHalf = abs(fract(coordMeters * 2.0 - 0.5) - 0.5) / (fw * 2.0);
    float lineHalf = 1.0 - smoothstep(0.0, 1.25, min(distHalf.x, distHalf.y));

    // 거리가 멀어질수록 격자선을 완만하게 감쇠해 자글거림 방지
    float fade = 1.0 - smoothstep(5.0, 18.0, viewDist);
    vec3 gridCol = baseTone + vec3(0.075) * line1m * fade + vec3(0.025) * lineHalf * fade * 0.6;
    return clamp(gridCol, 0.0, 1.0);
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

  float evaluateCameraWeight(
    int idx,
    vec3 P,
    vec3 N,
    vec3 viewDir,
    out vec2 outUv,
    out float outAbsDepthDiff
  ) {
    outUv = vec2(0.0);
    outAbsDepthDiff = 99.0;

    // 1. GLB 월드 점 P -> OpenCV 카메라 좌표 p_cv = R * P + t
    vec3 pCv = uCamR[idx] * P + uCamT[idx];
    float zCv = pCv.z;
    if (zCv <= 0.20 || zCv >= uFar - 0.5) {
      return 0.0;
    }

    // 2. 정규화 이미지 좌표 (u_img, v_img) in [0, 1]^2 (왼쪽 위 원점)
    vec4 intr = uCamIntr[idx];
    float uImg = intr.x * (pCv.x / zCv) + intr.z;
    float vImg = intr.y * (pCv.y / zCv) + intr.w;

    float margin = 0.005;
    if (uImg <= margin || uImg >= 1.0 - margin || vImg <= margin || vImg >= 1.0 - margin) {
      return 0.0;
    }

    // WebGL 텍스처 UV (아래쪽 원점 -> 1.0 - vImg)
    vec2 uv = vec2(uImg, 1.0 - vImg);
    outUv = uv;

    // 3. 표면 법선 정면성 검사
    vec3 effN = N;
    bool isFloor = (P.y < 0.18) || (P.y < 0.30 && abs(N.y) > 0.40) || (uIsIndicatorPlane == 1 && uIndicatorKind == 0);
    if (isFloor) {
      vec3 upN = (effN.y < 0.0) ? -effN : effN;
      effN = normalize(mix(upN, vec3(0.0, 1.0, 0.0), 0.92));
    }

    vec3 toCam = normalize(uCamPos[idx] - P);
    float cosNorm = dot(effN, toCam);
    float minCosNorm = isFloor ? 0.08 : ((uHasBaseMap == 1 || uIsIndicatorPlane == 1) ? 0.15 : 0.45);
    if (cosNorm <= minCosNorm) {
      return 0.0;
    }
    float wNorm = smoothstep(minCosNorm, minCosNorm + 0.20, cosNorm) * pow(cosNorm, 1.3);

    // 4. 깊이 그림자 맵 가림 검사 (5-tap)
    vec2 dx = vec2(uShadowTexelSize.x * 1.2, 0.0);
    vec2 dy = vec2(0.0, uShadowTexelSize.y * 1.2);
    float d0 = readShadowDepth(idx, uv);
    float d1 = readShadowDepth(idx, uv + dx);
    float d2 = readShadowDepth(idx, uv - dx);
    float d3 = readShadowDepth(idx, uv + dy);
    float d4 = readShadowDepth(idx, uv - dy);

    float minSignedDiff = min(zCv - d0, min(min(zCv - d1, zCv - d2), min(zCv - d3, zCv - d4)));
    float dMin = min(d0, min(min(d1, d2), min(d3, d4)));
    outAbsDepthDiff = min(abs(zCv - d0), min(abs(zCv - d1), abs(zCv - d2)));

    float wDepth = 1.0;

    if (uIsIndicatorPlane == 1 && uIndicatorDepthMatch == 1) {
      // 3차 지시 #1: 바닥·벽 표시 층에 사진을 비출 때는 그 카메라의 깊이 그림자 맵에 맵 형상이 있고
      // (d < uFar - 0.5), 그 깊이가 표시 층 점의 깊이와 5cm(0.05m) 안으로 일치할 때만 비춘다.
      // 깊이 맵이 비어 있으면(형상 없음 = 카메라가 무엇을 봤는지 모름) 사진을 쓰지 않고 차분한 톤+격자만 표시한다.
      // 근거: TSDF 바닥면 높이 사분위 편차(±2.5cm) 및 RANSAC 벽 평면 inlier 허용치(4cm)를 감안한 물리 정합 임계값 = 5cm(0.05m).
      const float INDICATOR_DEPTH_MATCH_TOL = 0.05;
      bool hasMapGeom = (d0 < uFar - 0.5) && (d1 < uFar - 0.5) && (d2 < uFar - 0.5) && (d3 < uFar - 0.5) && (d4 < uFar - 0.5);
      float maxAbsDiff5 = max(
        abs(zCv - d0),
        max(max(abs(zCv - d1), abs(zCv - d2)), max(abs(zCv - d3), abs(zCv - d4)))
      );
      if (!hasMapGeom || maxAbsDiff5 > INDICATOR_DEPTH_MATCH_TOL) {
        return 0.0;
      }
      wDepth = 1.0 - smoothstep(INDICATOR_DEPTH_MATCH_TOL * 0.5, INDICATOR_DEPTH_MATCH_TOL, maxAbsDiff5);
    } else {
      float slopeScale = 1.0 / max(cosNorm, 0.20);
      float tolOccl = (uIsIndicatorPlane == 1)
        ? (isFloor ? max(0.45, 0.065 * zCv * slopeScale) : max(0.22, 0.040 * zCv * slopeScale))
        : (isFloor
            ? max(0.25, 0.045 * zCv * slopeScale)
            : ((uHasBaseMap == 1)
                ? max(0.085, 0.024 * zCv * slopeScale)
                : max(0.045, 0.012 * zCv * slopeScale)));
      if (minSignedDiff > tolOccl) {
        return 0.0;
      }

      // 전경 물체 실루엣 테두리 번짐 차단
      float tolCliff = (uIsIndicatorPlane == 1)
        ? (isFloor ? max(0.55, 0.080 * zCv * slopeScale) : max(0.32, 0.055 * zCv * slopeScale))
        : (isFloor
            ? max(0.36, 0.062 * zCv * slopeScale)
            : max(0.20, 0.045 * zCv * slopeScale));
      float cliffDiff = zCv - dMin;
      if (cliffDiff > tolCliff) {
        return 0.0;
      }

      wDepth = (1.0 - smoothstep(tolOccl * 0.55, tolOccl, max(0.0, minSignedDiff))) *
               (1.0 - smoothstep(tolCliff * 0.65, tolCliff, max(0.0, cliffDiff)));
    }

    // 5. 화면 가장자리 감쇠
    float edgeDist = min(min(uImg, 1.0 - uImg), min(vImg, 1.0 - vImg));
    float wBorder = smoothstep(margin, 0.09, edgeDist);

    // 6. 시점 각도 가중치 (Unstructured Lumigraph)
    float cosAngle = clamp(dot(viewDir, toCam), -1.0, 1.0);
    float wView = exp(8.5 * (cosAngle - 1.0)) / max(1.0 - cosAngle, 0.001);

    // 거리 감쇠
    float dist = length(uCamPos[idx] - P);
    float wDist = 1.0 / (1.0 + 0.05 * dist * dist);

    return wDepth * wNorm * wBorder * wView * wDist;
  }

  vec3 applySceneFog(vec3 col, float viewDist) {
    if (uUseBgTone == 0 || uRenderMode != 1) return col;
    vec3 fogColor = vec3(0.145, 0.190, 0.255); // #253041
    float fogFactor = smoothstep(8.0, 25.0, viewDist) * 0.38;
    return mix(col, fogColor, fogFactor);
  }

  void main() {
    vec3 N = normalize(vWorldNormal);
    vec3 toViewer = cameraPosition - vWorldPos;
    float viewDist = length(toViewer);
    vec3 viewDir = toViewer / max(viewDist, 1e-6);

    // 바닥·벽 미관측 표시 층은 항상 방 안쪽을 향하는 단면(FrontSide)으로만 보이게 하여
    // 벽 바깥/근처에 위치한 카메라(cam_C, cam_D 등)의 시야를 뒷면이 가리지 않도록 한다.
    if (uIsIndicatorPlane == 1) {
      if (dot(N, viewDir) <= 0.02) {
        discard;
      }
    }

    // -----------------------------------------------------------------------
    // 모드 3: 2차 지시 #1 단색 진단 보기 (#b8c4d0, DoubleSide)
    // -----------------------------------------------------------------------
    if (uRenderMode == 3) {
      if (uIsIndicatorPlane == 1 || uIsEstimatedPlane == 1) {
        discard;
      }
      vec3 diagBase = vec3(0.7216, 0.7686, 0.8157); // #b8c4d0
      vec3 lightDir1 = normalize(vec3(0.38, 0.86, 0.33));
      vec3 lightDir2 = normalize(vec3(-0.45, 0.55, -0.70));
      float ndl = 0.65 * abs(dot(N, lightDir1)) + 0.35 * abs(dot(N, lightDir2));
      float shade = 0.80 + 0.24 * ndl;
      gl_FragColor = vec4(diagBase * shade, 1.0);
      return;
    }

    // -----------------------------------------------------------------------
    // 미관측 표시 층(바닥·벽) 기본 단색 + 1m 격자 계산
    // World 좌표: (x_world, y_world, z_world) = (vWorldPos.x, -vWorldPos.z, vWorldPos.y)
    // -----------------------------------------------------------------------
    vec2 worldXY = vec2(vWorldPos.x, -vWorldPos.z);
    vec2 gridCoord = (uIndicatorKind == 0)
      ? worldXY
      : vec2(dot(worldXY, uWallTangentWorldXY), vWorldPos.y);
    vec3 indicatorBaseColor = evalCalmGridColor(gridCoord, uIndicatorCalmColor, viewDist);
    vec3 floorGridAtPos = evalCalmGridColor(worldXY, uFloorCalmColorShared, viewDist);

    vec3 origColor = (uIsIndicatorPlane == 1) ? indicatorBaseColor : getOriginalBaseColor(N);

    // 모드 0(원래 맵) 또는 기존 estimated_floor_plane 또는 미관측 투영 비활성화
    if (uRenderMode == 0 || uIsEstimatedPlane == 1 || (uIsIndicatorPlane == 0 && uHasBaseMap == 0 && uProjectUnobserved == 0)) {
      if (uRenderMode == 2) {
        float luma = dot(origColor, vec3(0.2126, 0.7152, 0.0722));
        gl_FragColor = vec4(vec3(luma * 0.35 + 0.08), 1.0);
      } else {
        gl_FragColor = vec4(origColor, 1.0);
      }
      return;
    }

    // 2차 지시 #5: 구멍 경계 섞기 켜짐 시, 바닥/벽의 미관측 삼각형 폴백 색상을
    // 하부 표시 층(바닥·벽 단색+격자) 톤에 맞춰 들쭉날쭉한 톱니 단차와 어두운 구운 그림자 조각을 해소
    bool isMeshFloor = (uIsIndicatorPlane == 0) && ((vWorldPos.y < 0.18) || (vWorldPos.y < 0.30 && abs(N.y) > 0.40));
    if (uIsIndicatorPlane == 0 && uEdgeFeather == 1) {
      if (isMeshFloor && uShowFloorLayer == 1) {
        origColor = mix(origColor, floorGridAtPos, (uHasBaseMap == 0) ? 0.95 : 0.85);
      } else if (uHasBaseMap == 0 && uUseBgTone == 1) {
        origColor = mix(origColor, vec3(0.28, 0.31, 0.36), 0.45);
      }
    }

    // 표시 층(바닥·벽)은 깊이 버퍼 z-fighting 방지를 위해 뒤로(바닥 -6.1cm, 벽 -1.8cm) 물려 렌더링하지만,
    // 카메라 투영·차폐 검사는 실제 TSDF 표면 위치(projPos)에서 수행해 구멍 경계의 시차(parallax)와 자체 그림자를 제거한다.
    vec3 projPos = vWorldPos;
    if (uIsIndicatorPlane == 1) {
      projPos = (uIndicatorKind == 0)
        ? (vWorldPos + vec3(0.0, 0.061, 0.0))
        : (vWorldPos + N * 0.018);
    }

    vec2 uv0, uv1, uv2, uv3;
    float ad0, ad1, ad2, ad3;
    float w0 = evaluateCameraWeight(0, projPos, N, viewDir, uv0, ad0);
    float w1 = evaluateCameraWeight(1, projPos, N, viewDir, uv1, ad1);
    float w2 = evaluateCameraWeight(2, projPos, N, viewDir, uv2, ad2);
    float w3 = evaluateCameraWeight(3, projPos, N, viewDir, uv3, ad3);

    // 4개 가중치 내림차순 정렬 네트워크 (sw0 >= sw1 >= sw2 >= sw3)
    float sw0 = w0, sw1 = w1, sw2 = w2, sw3 = w3;
    float tmp;
    if (sw0 < sw1) { tmp = sw0; sw0 = sw1; sw1 = tmp; }
    if (sw2 < sw3) { tmp = sw2; sw2 = sw3; sw3 = tmp; }
    if (sw0 < sw2) { tmp = sw0; sw0 = sw2; sw2 = tmp; }
    if (sw1 < sw3) { tmp = sw1; sw1 = sw3; sw3 = tmp; }
    if (sw1 < sw2) { tmp = sw1; sw1 = sw2; sw2 = tmp; }

    float kThreshold = (uTopK <= 2) ? sw2 : sw3;
    float threshold = max(kThreshold, sw0 * 0.14);
    float tw0 = max(0.0, w0 - threshold);
    float tw1 = max(0.0, w1 - threshold);
    float tw2 = max(0.0, w2 - threshold);
    float tw3 = max(0.0, w3 - threshold);

    float sumW = tw0 + tw1 + tw2 + tw3;
    if (sumW <= 1e-6) {
      tw0 = w0; tw1 = w1; tw2 = w2; tw3 = w3;
      sumW = tw0 + tw1 + tw2 + tw3;
    }

    const float FEATHER_COLOR_BAND = 0.11; // 열린 구멍 경계 11cm 폭 소프트 블렌딩

    if (sumW <= 1e-6) {
      if (uRenderMode == 2) {
        float luma = dot(origColor, vec3(0.2126, 0.7152, 0.0722));
        gl_FragColor = vec4(vec3(luma * 0.35 + 0.08), 1.0);
      } else {
        vec3 fallbackCol = origColor;
        if (uIsIndicatorPlane == 0 && uEdgeFeather == 1) {
          if (isMeshFloor && uShowFloorLayer == 1) {
            // 카메라가 전혀 못 보는 바닥 구멍 주변 삼각형은 바닥 표시 층 톤으로 매끄럽게 통합
            float tCol = smoothstep(0.0, FEATHER_COLOR_BAND, vBoundaryDist);
            fallbackCol = mix(floorGridAtPos, mix(floorGridAtPos, fallbackCol, 0.20), tCol);
          } else if (vBoundaryDist < FEATHER_COLOR_BAND) {
            float tCol = smoothstep(0.0, FEATHER_COLOR_BAND, vBoundaryDist);
            fallbackCol = mix(vec3(0.157, 0.204, 0.271), fallbackCol, tCol);
          }
        }
        gl_FragColor = vec4(applySceneFog(fallbackCol, viewDist), 1.0);
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

    vec3 c0 = (nw0 > 0.0) ? readCamPhoto(0, uv0) : vec3(0.0);
    vec3 c1 = (nw1 > 0.0) ? readCamPhoto(1, uv1) : vec3(0.0);
    vec3 c2 = (nw2 > 0.0) ? readCamPhoto(2, uv2) : vec3(0.0);
    vec3 c3 = (nw3 > 0.0) ? readCamPhoto(3, uv3) : vec3(0.0);

    vec3 vdtColor = nw0 * c0 + nw1 * c1 + nw2 * c2 + nw3 * c3;
    float confidence = clamp(sw0 * 25.0, 0.0, 1.0);

    // -----------------------------------------------------------------------
    // 2차 지시 #3 & #4: 바닥·벽 미관측 표시 층의 사진 투영
    // - 차폐 검사(evaluateCameraWeight)를 통과한 영역에는 실제 카메라 사진을 그대로 투영하여
    //   메시는 비었지만 사진에 찍힌 바닥·벽을 인접 TSDF 삼각형과 이음매 없이 메운다.
    // -----------------------------------------------------------------------
    vec3 finalColor = mix(origColor, vdtColor, confidence);

    // -----------------------------------------------------------------------
    // 2차 지시 #5: 메시 구멍 가장자리(열린 경계 모서리) 11cm 구간 부드럽게 섞기
    // - 열린 구멍 경계에 가까워질수록(vBoundaryDist -> 0) 하부 표시 층(바닥/벽) 또는
    //   슬레이트 배경 톤과 부드럽게 섞어, 삼각형 톱니 경계가 튀지 않게 완화한다.
    // -----------------------------------------------------------------------
    if (uIsIndicatorPlane == 0 && uEdgeFeather == 1 && vBoundaryDist < FEATHER_COLOR_BAND) {
      float tFeather = smoothstep(0.0, FEATHER_COLOR_BAND, vBoundaryDist);
      vec3 edgeTarget = finalColor;
      if (isMeshFloor) {
        edgeTarget = (uShowFloorLayer == 1)
          ? ((uIndicatorDepthMatch == 1) ? mix(floorGridAtPos, finalColor, 0.15) : mix(floorGridAtPos, vdtColor, confidence))
          : vec3(0.157, 0.204, 0.271);
      } else if (vWorldPos.y > 2.15 || (uShowWallLayer == 0 && vWorldPos.y > 0.30)) {
        // 벽 상단이나 배경과 맞닿는 외곽 열린 경계는 슬레이트 배경 톤으로 부드럽게 감쇠
        edgeTarget = mix(vec3(0.157, 0.204, 0.271), finalColor, 0.30);
      } else {
        edgeTarget = mix(origColor, finalColor, 0.50);
      }
      finalColor = mix(edgeTarget, finalColor, tFeather);
    }

    finalColor = applySceneFog(finalColor, viewDist);
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
const rightSubLabelEl = document.getElementById("vdt-sub-label");

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
scene.background = null;

// 바닥·벽 미관측 표시 층 전용 Scene (clearDepth 로 TSDF 메시를 절대 가리지 않게 분리)
const indicatorScene = new THREE.Scene();
indicatorScene.background = null;

// 배경 그라데이션 전용 풀스크린 쿼드 Scene
const bgScene = new THREE.Scene();
const bgCam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
const bgQuad = new THREE.Mesh(
  new THREE.PlaneGeometry(2, 2),
  new THREE.ShaderMaterial({
    vertexShader: bgVertexShader,
    fragmentShader: bgFragmentShader,
    depthTest: false,
    depthWrite: false,
  })
);
bgScene.add(bgQuad);

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
  uUseBgTone: { value: 1 },
  uShowFloorLayer: { value: 1 },
  uShowWallLayer: { value: 1 },
  uEdgeFeather: { value: 1 },
  uIndicatorDepthMatch: { value: 1 },
  uFeatherPass: { value: 0 },
  uTopK: { value: 3 },
  uFar: { value: 25.0 },
  uShadowTexelSize: { value: new THREE.Vector2(1 / 1536, 1 / 864) },
  uFloorCalmColorShared: { value: new THREE.Vector3(0.32, 0.35, 0.39) },
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
let wallsMeta = null;
let gltfRoot = null;
let estimatedFloorNode = null;
let floorIndicatorMesh = null;
let wallIndicatorGroup = null;
const shadowMeshes = [];
const tsdfMaterials = [];
const allVdtMaterials = [];
let frustumGroup = null;
let currentViewKey = "obl1";
let currentFps = 60.0;
let boundaryStats = { openEdges: 0, boundaryVertices: 0 };

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

  // 4대 카메라 깊이 그림자 렌더용 임시 Scene (TSDF 메시만 포함, 표시 층 평면은 제외)
  const shadowScene = new THREE.Scene();
  shadowScene.background = new THREE.Color(0xffffff);
  for (const srcMesh of shadowMeshes) {
    const m = new THREE.Mesh(srcMesh.geometry, depthMat);
    m.matrixAutoUpdate = false;
    m.matrix.copy(srcMesh.matrixWorld);
    m.matrixWorld.copy(srcMesh.matrixWorld);
    shadowScene.add(m);
  }

  const prevTarget = renderer.getRenderTarget();
  const prevScissor = renderer.getScissorTest();
  renderer.setScissorTest(false);

  for (let i = 0; i < 4; i++) {
    const key = `uCamDepth${i}`;
    const oldRt = sharedUniforms[key]._rt;
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
  // UV 경계에서 분리된 정점들을 6cm 격자 키로 묶어 거시적 표면 법선을 구한 뒤
  // 원래 정점 법선과 섞어 TSDF 잔물결로 인한 법선 떨림(얼룩무늬)을 완화한다.
  const gridSum = new Map();
  const cell = 0.06;

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

/**
 * 2차 지시 #5: 메시 구멍 가장자리(열린 경계 모서리)까지의 정점별 거리(m) 계산.
 *
 * 방법 및 근거:
 * 1. `textured_0000..0008` 과 `unobserved_neutral` 은 UV 아틀라스 경계를 따라 정점이 복제되어 있으므로,
 *    먼저 1 mm 공간 해시 격자로 동위치 정점들을 위상적으로 용접(weld)한다.
 * 2. 용접된 정점 인덱스 기준으로 모든 삼각형 모서리(무방향 간선)의 공유 삼각형 개수를 센다.
 *    공유 삼각형이 1개뿐인 간선이 진짜 TSDF 형상 구멍(열린 경계, open boundary edge)이다.
 * 3. 열린 경계 간선의 양 끝점과 중점들을 8 cm 3차원 공간 해시 격자에 넣은 뒤,
 *    모든 메시 정점에서 가장 가까운 열린 경계 점까지의 유클리드 거리 `aBoundaryDist` (최대 0.12 m 캡)를
 *    계산해 정점 속성(`BufferAttribute`)으로 바인딩한다.
 * 4. 프래그먼트 셰이더에서 보간된 `vBoundaryDist` 로 구멍 테두리 8 cm 구간의 색을 하부 표시 층 톤으로 섞고,
 *    바깥 4 cm 구간을 2패스 알파 페더링(`smoothstep(0, 0.04, vBoundaryDist)`)하여 내부 면은 전혀 투명해지지
 *    않으면서 구멍 가장자리의 톱니 모양 삼각형 경계만 부드럽게 녹아들게 한다.
 */
function computeOpenBoundaryDistances(meshes) {
  const weldMap = new Map();
  const weldedCoords = [];

  function getWeldedId(x, y, z) {
    const key = `${Math.round(x * 1000)},${Math.round(y * 1000)},${Math.round(z * 1000)}`;
    let id = weldMap.get(key);
    if (id === undefined) {
      id = weldedCoords.length;
      weldMap.set(key, id);
      weldedCoords.push([x, y, z]);
    }
    return id;
  }

  const edgeCounts = new Map();
  const meshVertexWeldedIds = [];

  for (const node of meshes) {
    const pos = node.geometry.attributes.position;
    const vIds = new Int32Array(pos.count);
    for (let i = 0; i < pos.count; i++) {
      vIds[i] = getWeldedId(pos.getX(i), pos.getY(i), pos.getZ(i));
    }
    meshVertexWeldedIds.push(vIds);

    const idxAttr = node.geometry.index;
    const triCount = idxAttr ? idxAttr.count / 3 : pos.count / 3;
    for (let t = 0; t < triCount; t++) {
      const i0 = idxAttr ? idxAttr.getX(t * 3) : t * 3;
      const i1 = idxAttr ? idxAttr.getX(t * 3 + 1) : t * 3 + 1;
      const i2 = idxAttr ? idxAttr.getX(t * 3 + 2) : t * 3 + 2;
      const w0 = vIds[i0];
      const w1 = vIds[i1];
      const w2 = vIds[i2];
      if (w0 === w1 || w1 === w2 || w2 === w0) continue;

      const edges = [
        w0 < w1 ? `${w0}_${w1}` : `${w1}_${w0}`,
        w1 < w2 ? `${w1}_${w2}` : `${w2}_${w1}`,
        w2 < w0 ? `${w2}_${w0}` : `${w0}_${w2}`,
      ];
      for (const ek of edges) {
        edgeCounts.set(ek, (edgeCounts.get(ek) || 0) + 1);
      }
    }
  }

  // 공유 삼각형 개수가 1인 열린 경계 간선의 샘플 점(양 끝점 + 중점) 수집
  const samplePts = [];
  const isBoundaryVertex = new Uint8Array(weldedCoords.length);
  let openEdgeCount = 0;

  for (const [ek, cnt] of edgeCounts.entries()) {
    if (cnt !== 1) continue;
    openEdgeCount++;
    const sep = ek.indexOf("_");
    const a = parseInt(ek.slice(0, sep), 10);
    const b = parseInt(ek.slice(sep + 1), 10);
    const pa = weldedCoords[a];
    const pb = weldedCoords[b];
    if (!isBoundaryVertex[a]) {
      isBoundaryVertex[a] = 1;
      samplePts.push(pa);
    }
    if (!isBoundaryVertex[b]) {
      isBoundaryVertex[b] = 1;
      samplePts.push(pb);
    }
    samplePts.push([(pa[0] + pb[0]) * 0.5, (pa[1] + pb[1]) * 0.5, (pa[2] + pb[2]) * 0.5]);
  }

  // 8cm 격자 해시로 각 용접 정점의 최근접 열린 경계 거리(최대 0.12m) 계산
  const cell = 0.08;
  const buckets = new Map();
  for (let i = 0; i < samplePts.length; i++) {
    const p = samplePts[i];
    const bx = Math.floor(p[0] / cell);
    const by = Math.floor(p[1] / cell);
    const bz = Math.floor(p[2] / cell);
    const bKey = `${bx},${by},${bz}`;
    let list = buckets.get(bKey);
    if (!list) {
      list = [];
      buckets.set(bKey, list);
    }
    list.push(p);
  }

  const maxDist = 0.12;
  const maxDistSq = maxDist * maxDist;
  const weldedDist = new Float32Array(weldedCoords.length);

  for (let w = 0; w < weldedCoords.length; w++) {
    if (isBoundaryVertex[w]) {
      weldedDist[w] = 0.0;
      continue;
    }
    const p = weldedCoords[w];
    const bx0 = Math.floor(p[0] / cell);
    const by0 = Math.floor(p[1] / cell);
    const bz0 = Math.floor(p[2] / cell);
    let bestSq = maxDistSq;
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        for (let dz = -1; dz <= 1; dz++) {
          const list = buckets.get(`${bx0 + dx},${by0 + dy},${bz0 + dz}`);
          if (!list) continue;
          for (let k = 0; k < list.length; k++) {
            const q = list[k];
            const d2 = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2;
            if (d2 < bestSq) bestSq = d2;
          }
        }
      }
    }
    weldedDist[w] = Math.sqrt(bestSq);
  }

  for (let mIdx = 0; mIdx < meshes.length; mIdx++) {
    const node = meshes[mIdx];
    const pos = node.geometry.attributes.position;
    const vIds = meshVertexWeldedIds[mIdx];
    const distArr = new Float32Array(pos.count);
    for (let i = 0; i < pos.count; i++) {
      distArr[i] = weldedDist[vIds[i]];
    }
    node.geometry.setAttribute("aBoundaryDist", new THREE.BufferAttribute(distArr, 1));
  }

  let bVertCount = 0;
  for (let i = 0; i < isBoundaryVertex.length; i++) bVertCount += isBoundaryVertex[i];
  boundaryStats = { openEdges: openEdgeCount, boundaryVertices: bVertCount };
}

function createVdtMaterialForMesh(mesh) {
  const origMat = Array.isArray(mesh.material) ? mesh.material[0] : mesh.material;
  const baseMap = origMat && origMat.map ? origMat.map : null;
  if (baseMap) {
    baseMap.colorSpace = THREE.NoColorSpace;
    baseMap.needsUpdate = true;
  }

  const hasVertexColor = Boolean(mesh.geometry.attributes.color);
  const hasBoundaryDist = Boolean(mesh.geometry.attributes.aBoundaryDist);
  const isEstimatedPlane =
    mesh.name === "estimated_floor_plane" || (origMat && origMat.name === "estimated_floor_plane");

  const factorColor = origMat && origMat.color ? origMat.color.clone() : new THREE.Color(1, 1, 1);

  const defines = {};
  if (hasVertexColor) defines.USE_VERTEX_COLOR = "";
  if (hasBoundaryDist) defines.USE_BOUNDARY_DIST = "";
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
      uIsIndicatorPlane: { value: 0 },
      uIndicatorKind: { value: 0 },
      uIndicatorCalmColor: { value: new THREE.Vector3(0.32, 0.35, 0.39) },
      uIndicatorObsColor: { value: new THREE.Vector3(0.41, 0.44, 0.49) },
      uWallTangentWorldXY: { value: new THREE.Vector2(1.0, 0.0) },
      uBaseMap: { value: baseMap || dummyTex },
      uBaseFactorLinear: { value: new THREE.Vector3(factorColor.r, factorColor.g, factorColor.b) },
    },
  });
  allVdtMaterials.push(mat);
  if (!isEstimatedPlane) {
    tsdfMaterials.push(mat);
  }
  return mat;
}

function createQuadGeometryFromGlbCorners(cornersGlb, normalGlb) {
  // cornersGlb: [c0, c1, c2, c3] 4개 꼭짓점 -> 2개 삼각형 (0,1,2), (0,2,3)
  const positions = new Float32Array([
    ...cornersGlb[0],
    ...cornersGlb[1],
    ...cornersGlb[2],
    ...cornersGlb[3],
  ]);
  const normals = new Float32Array([
    ...normalGlb,
    ...normalGlb,
    ...normalGlb,
    ...normalGlb,
  ]);
  const uvs = new Float32Array([0, 0, 1, 0, 1, 1, 0, 1]);
  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geom.setAttribute("normal", new THREE.BufferAttribute(normals, 3));
  geom.setAttribute("uv", new THREE.BufferAttribute(uvs, 2));
  geom.setIndex([0, 1, 2, 0, 2, 3]);
  return geom;
}

function buildFloorAndWallIndicatorLayers(wallsDoc) {
  if (!wallsDoc) return;

  // 1) 2차 지시 #3: 바닥 미관측 표시 층 (World Z = -0.015m)
  const fp = wallsDoc.floor_plane;
  const floorCalm = fp.calm_indicator_srgb || [0.36, 0.38, 0.43];
  const floorObs = fp.observed_median_srgb || [0.41, 0.44, 0.49];
  // 관측된 바닥 평균 톤에 자연스럽게 맞춘 차분한 단색 (이중 감쇠 없이 calm_indicator_srgb 그대로 사용)
  const calmFloorVec = new THREE.Vector3(
    floorCalm[0] * 0.96,
    floorCalm[1] * 0.96,
    floorCalm[2] * 0.96
  );
  sharedUniforms.uFloorCalmColorShared.value.copy(calmFloorVec);

  const floorGeom = createQuadGeometryFromGlbCorners(fp.corners_glb, fp.normal_glb);
  const floorMat = new THREE.ShaderMaterial({
    name: "vdt_floor_indicator",
    defines: { USE_UV_ATTR: "" },
    vertexShader: vdtVertexShader,
    fragmentShader: vdtFragmentShader,
    side: THREE.DoubleSide,
    polygonOffset: true,
    polygonOffsetFactor: 2,
    polygonOffsetUnits: 2,
    uniforms: {
      ...sharedUniforms,
      uHasBaseMap: { value: 0 },
      uHasVertexColor: { value: 0 },
      uIsEstimatedPlane: { value: 0 },
      uIsIndicatorPlane: { value: 1 },
      uIndicatorKind: { value: 0 },
      uIndicatorCalmColor: { value: calmFloorVec },
      uIndicatorObsColor: { value: new THREE.Vector3(...floorObs) },
      uWallTangentWorldXY: { value: new THREE.Vector2(1.0, 0.0) },
      uBaseMap: { value: dummyTex },
      uBaseFactorLinear: { value: calmFloorVec.clone() },
    },
  });
  allVdtMaterials.push(floorMat);
  floorIndicatorMesh = new THREE.Mesh(floorGeom, floorMat);
  floorIndicatorMesh.name = "vdt_floor_indicator_plane";
  floorIndicatorMesh.renderOrder = -2;
  indicatorScene.add(floorIndicatorMesh);

  // 2) 2차 지시 #4: find_wall_planes 수용 경계 벽 평면 (3면)
  wallIndicatorGroup = new THREE.Group();
  wallIndicatorGroup.name = "vdt_wall_indicator_group";
  for (const wp of wallsDoc.wall_planes || []) {
    const wCalm = wp.calm_indicator_srgb || [0.50, 0.52, 0.55];
    const wObs = wp.observed_median_srgb || [0.60, 0.60, 0.60];
    const calmWallVec = new THREE.Vector3(
      wCalm[0] * 0.92,
      wCalm[1] * 0.92,
      wCalm[2] * 0.92
    );
    const wGeom = createQuadGeometryFromGlbCorners(wp.corners_glb, wp.normal_glb);
    const wMat = new THREE.ShaderMaterial({
      name: `vdt_${wp.wall_id}_indicator`,
      defines: { USE_UV_ATTR: "" },
      vertexShader: vdtVertexShader,
      fragmentShader: vdtFragmentShader,
      side: THREE.DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: 2,
      polygonOffsetUnits: 2,
      uniforms: {
        ...sharedUniforms,
        uHasBaseMap: { value: 0 },
        uHasVertexColor: { value: 0 },
        uIsEstimatedPlane: { value: 0 },
        uIsIndicatorPlane: { value: 1 },
        uIndicatorKind: { value: 1 },
        uIndicatorCalmColor: { value: calmWallVec },
        uIndicatorObsColor: { value: new THREE.Vector3(...wObs) },
        uWallTangentWorldXY: { value: new THREE.Vector2(wp.t_world_xy[0], wp.t_world_xy[1]) },
        uBaseMap: { value: dummyTex },
        uBaseFactorLinear: { value: calmWallVec.clone() },
      },
    });
    allVdtMaterials.push(wMat);
    const wMesh = new THREE.Mesh(wGeom, wMat);
    wMesh.name = `vdt_${wp.wall_id}`;
    wMesh.renderOrder = -1;
    wallIndicatorGroup.add(wMesh);
  }
  indicatorScene.add(wallIndicatorGroup);
}

function updateRightBadgeSubtitle() {
  if (!rightSubLabelEl) return;
  const isDiag = document.getElementById("chk-diag")?.checked;
  const isWeights = document.getElementById("chk-weights")?.checked;
  if (isDiag) {
    rightSubLabelEl.textContent = "1. 단색 진단 보기 (#b8c4d0 DoubleSide + 밝은 배경 → TSDF 형상 구멍 확인)";
    return;
  }
  if (isWeights) {
    rightSubLabelEl.textContent = "카메라별 투영 가중치 시각화 (A:빨강 B:초록 C:파랑 D:노랑)";
    return;
  }
  const activeNums = [];
  if (document.getElementById("chk-bg-tone")?.checked) activeNums.push("2:배경톤");
  if (document.getElementById("chk-floor-layer")?.checked) activeNums.push("3:바닥층");
  if (document.getElementById("chk-wall-layer")?.checked) activeNums.push("4:벽3면");
  if (document.getElementById("chk-edge-feather")?.checked) activeNums.push("5:경계섞기");
  rightSubLabelEl.textContent =
    activeNums.length > 0
      ? `실시간 카메라 투영 + [${activeNums.join(" + ")}]`
      : "4대 카메라 실시간 깊이 가림·각도 가중 투영 (기본 VDT)";
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

/**
 * 임의 사각형 뷰포트에 지정된 옵션으로 1회 렌더링한다.
 */
export function renderViewportWithConfig(x, y, w, h, cfg) {
  camera.aspect = w / Math.max(1, h);
  camera.updateProjectionMatrix();

  renderer.setViewport(x, y, w, h);
  renderer.setScissor(x, y, w, h);

  const mode = cfg.renderMode; // 0: 원래 맵, 1: VDT, 2: 가중치, 3: 단색 진단 보기
  sharedUniforms.uRenderMode.value = mode;
  sharedUniforms.uUseBgTone.value = cfg.bgTone ? 1 : 0;
  sharedUniforms.uShowFloorLayer.value = cfg.floorLayer ? 1 : 0;
  sharedUniforms.uShowWallLayer.value = cfg.wallLayer ? 1 : 0;
  sharedUniforms.uEdgeFeather.value = cfg.edgeFeather ? 1 : 0;
  sharedUniforms.uIndicatorDepthMatch.value = cfg.indicatorDepthMatch === false ? 0 : 1;
  sharedUniforms.uFeatherPass.value = 0;

  // 가시성 설정
  if (estimatedFloorNode) {
    // 원래 맵(mode 0)에서만 12차 GLB 내장 estimated_floor_plane 표시
    estimatedFloorNode.visible = mode === 0;
  }
  if (floorIndicatorMesh) {
    floorIndicatorMesh.visible = mode === 1 && Boolean(cfg.floorLayer);
  }
  if (wallIndicatorGroup) {
    wallIndicatorGroup.visible = mode === 1 && Boolean(cfg.wallLayer);
  }

  // 배경 클리어 (단색 진단 보기는 밝은 #e2e8f0, 배경 톤 켜짐 시 그라데이션 쿼드 렌더)
  if (mode === 3) {
    renderer.setClearColor(0xe2e8f0, 1.0);
    renderer.clear(true, true, false);
  } else {
    renderer.setClearColor(0x0b0f17, 1.0);
    renderer.clear(true, true, false);
    if (mode === 1 && cfg.bgTone) {
      renderer.render(bgScene, bgCam);
    }
  }

  // 바닥·벽 미관측 표시 층을 먼저 그린 뒤 깊이 버퍼만 비워(clearDepth),
  // 오목하게 들어간 창문이나 곡률이 있는 TSDF 벽·바닥 메시를 표시 층이 절대 가리지 않고
  // 오직 TSDF 기하가 비어 있는 구멍(hole) 픽셀에만 표시 층이 나타나도록 한다.
  if (mode === 1 && (cfg.floorLayer || cfg.wallLayer)) {
    renderer.render(indicatorScene, camera);
    renderer.clearDepth();
  }

  // TSDF 본 메시 렌더
  renderer.render(scene, camera);
}

export function getCurrentRightConfig() {
  const isDiag = Boolean(document.getElementById("chk-diag")?.checked);
  const isWeights = Boolean(document.getElementById("chk-weights")?.checked);
  let renderMode = 1;
  if (isDiag) renderMode = 3;
  else if (isWeights) renderMode = 2;

  return {
    renderMode,
    bgTone: Boolean(document.getElementById("chk-bg-tone")?.checked),
    floorLayer: Boolean(document.getElementById("chk-floor-layer")?.checked),
    wallLayer: Boolean(document.getElementById("chk-wall-layer")?.checked),
    edgeFeather: Boolean(document.getElementById("chk-edge-feather")?.checked),
    indicatorDepthMatch: true,
  };
}

let customLeftConfig = null;

export function renderSingleFrame() {
  const fullW = window.innerWidth;
  const fullH = window.innerHeight;
  const halfW = Math.floor(fullW * 0.5);

  renderer.setScissorTest(true);

  const leftCfg = customLeftConfig || {
    renderMode: 0,
    bgTone: false,
    floorLayer: false,
    wallLayer: false,
    edgeFeather: false,
    indicatorDepthMatch: true,
  };
  renderViewportWithConfig(0, 0, halfW, fullH, leftCfg);

  const rightCfg = getCurrentRightConfig();
  renderViewportWithConfig(halfW, 0, fullW - halfW, fullH, rightCfg);

  renderer.setScissorTest(false);
}

function describeConfigLabel(cfg) {
  if (cfg.renderMode === 0) {
    return `원래 맵 (12차 texrecon 구운 텍스처)  |  ${currentViewKey}`;
  }
  if (cfg.renderMode === 3) {
    return `1. 단색 진단 보기 (#b8c4d0 DoubleSide, 밝은 배경=TSDF 구멍)  |  ${currentViewKey}`;
  }
  if (cfg.renderMode === 2) {
    return `카메라별 투영 가중치 (A:빨강 B:초록 C:파랑 D:노랑)  |  ${currentViewKey}`;
  }
  const tags = [];
  if (cfg.bgTone) tags.push("2:배경톤");
  if (cfg.floorLayer) tags.push("3:바닥층");
  if (cfg.wallLayer) tags.push("4:벽3면");
  if (cfg.edgeFeather) tags.push("5:경계섞기");
  if (tags.length === 0) {
    return `시점 의존 텍스처 (기본 VDT, 토글 끔)  |  ${currentViewKey}`;
  }
  const prefix = cfg.indicatorDepthMatch === false ? "수정 전 ②③④⑤ (단순 가림)" : "수정 후 ②③④⑤ (깊이 5cm 일치)";
  if (cfg.bgTone && cfg.floorLayer && cfg.wallLayer && cfg.edgeFeather) {
    return `${prefix}  |  ${currentViewKey}`;
  }
  return `시점 의존 + [${tags.join(" + ")}]  |  ${currentViewKey}`;
}

export function captureComparisonDataUrl(
  mimeType = "image/jpeg",
  quality = 0.92,
  leftOverrideLabel = null,
  rightOverrideLabel = null
) {
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
  ctx.font = `600 ${Math.round(14 * scale)}px "Segoe UI", "Malgun Gothic", sans-serif`;

  const leftCfg = customLeftConfig || { renderMode: 0 };
  const rightCfg = getCurrentRightConfig();
  const leftText = leftOverrideLabel || describeConfigLabel(leftCfg);
  const rightText = rightOverrideLabel || describeConfigLabel(rightCfg);

  ctx.fillStyle = "rgba(15, 23, 42, 0.86)";
  ctx.fillRect(pad, pad, Math.min(w * 0.47, ctx.measureText(leftText).width + 24 * scale), boxH);
  ctx.fillRect(
    Math.floor(w * 0.5) + pad,
    pad,
    Math.min(w * 0.47, ctx.measureText(rightText).width + 24 * scale),
    boxH
  );

  ctx.fillStyle = "#e2e8f0";
  ctx.fillText(leftText, pad + 12 * scale, pad + 22 * scale);
  ctx.fillStyle = "#38bdf8";
  ctx.fillText(rightText, Math.floor(w * 0.5) + pad + 12 * scale, pad + 22 * scale);

  return outCanvas.toDataURL(mimeType, quality);
}

/**
 * 3차 지시 #3: 한 시점에 대해 [원래 | 시점의존 | 수정 전 ②③④⑤ | 수정 후 ②③④⑤] 4단계를
 * 2x2 격자 한 장에 담아 한눈에 비교할 수 있는 이미지 DataURL 을 생성한다.
 */
export function captureFourStageGridDataUrl(mimeType = "image/jpeg", quality = 0.93) {
  const fullW = window.innerWidth;
  const fullH = window.innerHeight;
  const halfW = Math.floor(fullW * 0.5);
  const halfH = Math.floor(fullH * 0.5);

  const stages = [
    {
      title: `① 원래 맵 (12차 구운 텍스처) | ${currentViewKey}`,
      color: "#e2e8f0",
      x: 0,
      y: halfH, // WebGL viewport Y=halfH 는 화면 상단
      cfg: {
        renderMode: 0,
        bgTone: false,
        floorLayer: false,
        wallLayer: false,
        edgeFeather: false,
        indicatorDepthMatch: true,
      },
    },
    {
      title: `② 시점 의존 텍스처 (기본 VDT) | ${currentViewKey}`,
      color: "#93c5fd",
      x: halfW,
      y: halfH,
      cfg: {
        renderMode: 1,
        bgTone: false,
        floorLayer: false,
        wallLayer: false,
        edgeFeather: false,
        indicatorDepthMatch: true,
      },
    },
    {
      title: `③ 수정 전 ②③④⑤ (단순 가림 → 구멍에 의자·탁자 비침) | ${currentViewKey}`,
      color: "#fbbf24",
      x: 0,
      y: 0, // WebGL viewport Y=0 은 화면 하단
      cfg: {
        renderMode: 1,
        bgTone: true,
        floorLayer: true,
        wallLayer: true,
        edgeFeather: true,
        indicatorDepthMatch: false,
      },
    },
    {
      title: `④ 수정 후 ②③④⑤ (깊이 5cm 일치만 투영, 빈 곳은 톤+격자) | ${currentViewKey}`,
      color: "#34d399",
      x: halfW,
      y: 0,
      cfg: {
        renderMode: 1,
        bgTone: true,
        floorLayer: true,
        wallLayer: true,
        edgeFeather: true,
        indicatorDepthMatch: true,
      },
    },
  ];

  renderer.setScissorTest(true);
  for (const st of stages) {
    renderViewportWithConfig(st.x, st.y, halfW, halfH, st.cfg);
  }
  renderer.setScissorTest(false);

  const w = canvas.width;
  const h = canvas.height;
  const outCanvas = document.createElement("canvas");
  outCanvas.width = w;
  outCanvas.height = h;
  const ctx = outCanvas.getContext("2d");
  ctx.drawImage(canvas, 0, 0);

  // 십자 구분선
  ctx.fillStyle = "rgba(255, 255, 255, 0.55)";
  ctx.fillRect(Math.floor(w * 0.5) - 1, 0, 2, h);
  ctx.fillRect(0, Math.floor(h * 0.5) - 1, w, 2);

  const scale = Math.max(1, w / 1600);
  const pad = Math.round(10 * scale);
  const boxH = Math.round(28 * scale);
  ctx.font = `600 ${Math.round(13 * scale)}px "Segoe UI", "Malgun Gothic", sans-serif`;

  const labelBoxes = [
    { text: stages[0].title, color: stages[0].color, cx: pad, cy: pad },
    { text: stages[1].title, color: stages[1].color, cx: Math.floor(w * 0.5) + pad, cy: pad },
    { text: stages[2].title, color: stages[2].color, cx: pad, cy: Math.floor(h * 0.5) + pad },
    { text: stages[3].title, color: stages[3].color, cx: Math.floor(w * 0.5) + pad, cy: Math.floor(h * 0.5) + pad },
  ];

  for (const lb of labelBoxes) {
    const tw = Math.min(w * 0.47, ctx.measureText(lb.text).width + 20 * scale);
    ctx.fillStyle = "rgba(15, 23, 42, 0.86)";
    ctx.fillRect(lb.cx, lb.cy, tw, boxH);
    ctx.fillStyle = lb.color;
    ctx.fillText(lb.text, lb.cx + 10 * scale, lb.cy + 19 * scale);
  }

  // 렌더링 후 원래 인터랙티브 프레임 복구
  renderSingleFrame();
  return outCanvas.toDataURL(mimeType, quality);
}

export function setToggles(opts = {}) {
  const map = {
    diag: "chk-diag",
    bgTone: "chk-bg-tone",
    floorLayer: "chk-floor-layer",
    wallLayer: "chk-wall-layer",
    edgeFeather: "chk-edge-feather",
    weights: "chk-weights",
  };
  for (const [k, id] of Object.entries(map)) {
    if (opts[k] !== undefined) {
      const el = document.getElementById(id);
      if (el) el.checked = Boolean(opts[k]);
    }
  }
  legendBox.classList.toggle("visible", Boolean(document.getElementById("chk-weights")?.checked));
  updateRightBadgeSubtitle();
  renderSingleFrame();
}

async function init() {
  const params = new URLSearchParams(window.location.search);
  const dataPrefix = params.get("data") || ".";

  const [camResp, wallResp] = await Promise.all([
    fetch(`${dataPrefix}/cameras.json`),
    fetch(`${dataPrefix}/walls.json`),
  ]);
  if (!camResp.ok) {
    throw new Error(`cameras.json 로드 실패: HTTP ${camResp.status}`);
  }
  camerasMeta = await camResp.json();
  if (wallResp.ok) {
    wallsMeta = await wallResp.json();
  }
  sharedUniforms.uFar.value = camerasMeta.cameras[0].far || 25.0;

  // 카메라 유니폼 설정
  camerasMeta.cameras.forEach((c, idx) => {
    const R = c.R;
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

    if (node.name === "estimated_floor_plane") {
      estimatedFloorNode = node;
    } else {
      shadowMeshes.push(node);
    }
    allMeshes.push(node);
  });

  computeWeldedSmoothNormals(shadowMeshes);
  computeOpenBoundaryDistances(shadowMeshes);

  for (const node of allMeshes) {
    node.material = createVdtMaterialForMesh(node);
  }

  scene.add(gltfRoot);

  // 바닥·벽 미관측 표시 층 생성
  if (wallsMeta) {
    buildFloorAndWallIndicatorLayers(wallsMeta);
  }

  frustumGroup = buildFrustumHelpers(camerasMeta.cameras);
  scene.add(frustumGroup);

  const initShadowRes = parseInt(document.getElementById("sel-shadow-res").value, 10) || 1536;
  renderShadowMaps(initShadowRes);
  updateRightBadgeSubtitle();

  const wallCount = wallsMeta ? wallsMeta.accepted_wall_count : 0;
  metaInfoEl.textContent = `삼각형 ${totalTriangles.toLocaleString()}개 | 열린경계 ${boundaryStats.openEdges.toLocaleString()}개 | 벽평면 ${wallCount}면 | 그림자맵 4x${initShadowRes}px`;

  const initialView = params.get("view") || "obl1";
  applyPresetView(initialView);

  // 전역 노출 (자동 캡처 및 디버깅용)
  window.__vdtApp = {
    ready: true,
    applyPresetView,
    renderSingleFrame,
    captureComparisonDataUrl,
    captureFourStageGridDataUrl,
    setToggles,
    setCustomLeftConfig(cfg) {
      customLeftConfig = cfg;
      renderSingleFrame();
    },
    setWeightVis(on) {
      setToggles({ weights: Boolean(on), diag: false });
    },
    getFps() {
      return currentFps;
    },
    getMeta() {
      return { camerasMeta, wallsMeta, boundaryStats };
    },
  };
}

// UI 이벤트 바인딩
document.querySelectorAll("#control-bar button[data-view]").forEach((btn) => {
  btn.addEventListener("click", () => {
    applyPresetView(btn.getAttribute("data-view"));
  });
});

["chk-diag", "chk-bg-tone", "chk-floor-layer", "chk-wall-layer", "chk-edge-feather"].forEach((id) => {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener("change", () => {
    if (id === "chk-diag" && el.checked) {
      const wEl = document.getElementById("chk-weights");
      if (wEl) wEl.checked = false;
      legendBox.classList.remove("visible");
    }
    updateRightBadgeSubtitle();
  });
});

document.getElementById("chk-weights").addEventListener("change", (e) => {
  if (e.target.checked) {
    const dEl = document.getElementById("chk-diag");
    if (dEl) dEl.checked = false;
  }
  legendBox.classList.toggle("visible", e.target.checked);
  updateRightBadgeSubtitle();
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
  const modeSuffix = document.getElementById("chk-diag")?.checked
    ? "_diag"
    : document.getElementById("chk-weights")?.checked
    ? "_weights"
    : "_vdt";
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

  await waitFrames(15);

  const views = ["cam_A", "cam_B", "cam_C", "cam_D", "top", "obl1", "obl2", "obl3"];
  const savedFiles = [];

  async function saveShot(filename, dataUrl) {
    await fetch("/__save_shot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename, dataUrl }),
    });
    savedFiles.push(filename);
  }

  // 1) 단색 진단 보기 (#1): [원래 맵 | 1. 단색 진단 보기 (#b8c4d0 + 밝은 배경)]
  window.__vdtApp.setCustomLeftConfig(null);
  setToggles({ diag: true, weights: false });
  for (const v of ["top", "obl1", "obl2", "obl3", "cam_A", "cam_C"]) {
    applyPresetView(v);
    await waitFrames(2);
    await saveShot(`compare_${v}_diag.jpg`, captureComparisonDataUrl("image/jpeg", 0.94));
  }

  // 2) [시점의존(기본) | 수정 후 시점의존+2(배경톤)+3(바닥층)+5(경계섞기)] 비교 (전 시점)
  window.__vdtApp.setCustomLeftConfig({
    renderMode: 1,
    bgTone: false,
    floorLayer: false,
    wallLayer: false,
    edgeFeather: false,
    indicatorDepthMatch: true,
  });
  setToggles({ diag: false, weights: false, bgTone: true, floorLayer: true, wallLayer: false, edgeFeather: true });
  for (const v of views) {
    applyPresetView(v);
    await waitFrames(2);
    await saveShot(`compare_${v}_vdt_235.jpg`, captureComparisonDataUrl("image/jpeg", 0.94));
  }

  // 3) [수정 전 ②③④⑤ (단순 가림) | 수정 후 ②③④⑤ (깊이 5cm 일치만 투영)] 비교 (전 시점)
  window.__vdtApp.setCustomLeftConfig({
    renderMode: 1,
    bgTone: true,
    floorLayer: true,
    wallLayer: true,
    edgeFeather: true,
    indicatorDepthMatch: false,
  });
  setToggles({ diag: false, weights: false, bgTone: true, floorLayer: true, wallLayer: true, edgeFeather: true });
  for (const v of views) {
    applyPresetView(v);
    await waitFrames(2);
    await saveShot(`compare_${v}_vdt_2345.jpg`, captureComparisonDataUrl("image/jpeg", 0.94));
  }

  // 4) [원래 맵 | 수정 후 ②③④⑤ (전체 개선 적용)] 기본 비교 이미지 갱신 (전 시점)
  window.__vdtApp.setCustomLeftConfig(null);
  setToggles({ diag: false, weights: false, bgTone: true, floorLayer: true, wallLayer: true, edgeFeather: true });
  for (const v of views) {
    applyPresetView(v);
    await waitFrames(2);
    await saveShot(`compare_${v}.jpg`, captureComparisonDataUrl("image/jpeg", 0.94));
  }

  // 5) 4단계(원래 / 시점의존 / 수정 전 ②③④⑤ / 수정 후 ②③④⑤) 2x2 한눈에 보기 그리드 (전 시점)
  for (const v of views) {
    applyPresetView(v);
    await waitFrames(2);
    await saveShot(`compare_${v}_stages.jpg`, captureFourStageGridDataUrl("image/jpeg", 0.94));
  }

  // 6) 카메라 가중치 시각화 캡처
  window.__vdtApp.setCustomLeftConfig(null);
  setToggles({ diag: false, weights: true });
  for (const v of ["top", "obl1", "obl2", "cam_A"]) {
    applyPresetView(v);
    await waitFrames(2);
    await saveShot(`compare_${v}_weights.jpg`, captureComparisonDataUrl("image/jpeg", 0.94));
  }

  // 7) 전체 토글(+2+3+4+5) 켠 상태에서 그림자 맵 해상도별(1024, 1536, 2048) FPS 및 렌더 시간 재측정
  setToggles({ diag: false, weights: false, bgTone: true, floorLayer: true, wallLayer: true, edgeFeather: true });
  const perfByShadowRes = {};
  const gl = renderer.getContext();
  const debugInfo = gl.getExtension("WEBGL_debug_renderer_info");
  const gpuRenderer = debugInfo ? gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);

  for (const res of [1024, 1536, 2048]) {
    const t0Shadow = performance.now();
    renderShadowMaps(res);
    gl.finish();
    const shadowBakeMs = Number((performance.now() - t0Shadow).toFixed(2));

    await waitFrames(8);
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
      boundary_stats: boundaryStats,
      accepted_wall_count: wallsMeta ? wallsMeta.accepted_wall_count : 0,
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
