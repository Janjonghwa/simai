import type * as THREE from 'three';

export type VdtVec2 = readonly [number, number];
export type VdtVec3 = readonly [number, number, number];
export type VdtMatrix3 = readonly [VdtVec3, VdtVec3, VdtVec3];
export type VdtQuadCornersWorld = readonly [VdtVec3, VdtVec3, VdtVec3, VdtVec3];

/**
 * 카메라 1대의 시점 의존 텍스처(VDT) 입력 계약.
 * 모든 외부 파라미터(R, t)는 정본 World 좌표계(m, +Z 상방)에서 OpenCV 카메라 좌표계(+X 우, +Y 하, +Z 정면)로의 변환이다:
 *   p_cam_cv = R * p_world + t
 */
export interface VdtCameraInput {
  cameraId: string;
  imageUrl: string;
  imageExpiresAtMs?: number | null;
  imageSize: readonly [number, number];
  K: VdtMatrix3;
  R: VdtMatrix3;
  t: VdtVec3;
  colorGainSrgb?: VdtVec3;
}

/** 바닥 미관측 표시 층 평면 입력 (정본 World 좌표계 기준). */
export interface VdtFloorPlaneInput {
  zWorldM: number;
  normalWorld: VdtVec3;
  cornersWorld: VdtQuadCornersWorld;
  observedMedianSrgb: VdtVec3;
  calmIndicatorSrgb: VdtVec3;
}

/** 방 경계 벽 미관측 표시 층 평면 입력 (정본 World 좌표계 기준). */
export interface VdtWallPlaneInput {
  wallId: string;
  normalWorld: VdtVec3;
  tWorldXy: VdtVec2;
  dWorldM: number;
  dRenderWorldM: number;
  cornersWorld: VdtQuadCornersWorld;
  observedMedianSrgb: VdtVec3;
  calmIndicatorSrgb: VdtVec3;
}

/** 확인/캡처용 사전 정의 시점 (정본 World 좌표계 기준). */
export interface VdtFreeViewInput {
  name: string;
  positionWorld: VdtVec3;
  targetWorld: VdtVec3;
  hfovDeg: number;
}

/** 장면(`scene_version`)과 함께 전달되는 시점 의존 텍스처 전체 설정. */
export interface SceneVdtConfig {
  formatVersion: string;
  colorSigmaOklab: number;
  nearM: number;
  farM: number;
  cameras: readonly VdtCameraInput[];
  floorPlane: VdtFloorPlaneInput | null;
  wallPlanes: readonly VdtWallPlaneInput[];
  freeViews?: Readonly<Record<string, VdtFreeViewInput>>;
}

export type VdtRenderMode = 'vdt' | 'original' | 'weights' | 'diagnostic';

/**
 * 뷰어 시점 의존 텍스처(VDT) 실행 옵션.
 * 기능 플래그 기본값은 끔(`enabled: false`)이다.
 */
export interface ViewerVdtOptions {
  /** VDT 활성화 여부 (기본값: false) */
  enabled?: boolean;
  /** 깊이 그림자 맵 긴 변 해상도 px (기본값: 1536) */
  shadowMapSize?: number;
  /** 미관측 TSDF 면(`unobserved_neutral`)에도 투영 허용 여부 (4차 지시 기본값: false) */
  projectUnobserved?: boolean;
  /** 카메라 간 Oklab 메도이드 외톨이 제거 활성화 여부 (4차 지시 기본값: true) */
  outlierRemoval?: boolean;
  /** 열린 구멍 경계(11cm) 페더링 활성화 여부 (기본값: true) */
  edgeFeather?: boolean;
  /** 바닥 미관측 표시 층 표시 여부 (기본값: true) */
  floorLayer?: boolean;
  /** 벽 미관측 표시 층 표시 여부 (기본값: true) */
  wallLayer?: boolean;
  /** 배경 슬레이트-블루 톤 및 완만한 거리 안개 활성화 여부 (기본값: true) */
  bgTone?: boolean;
  /** 카메라별 노출/색 보정 게인 적용 여부 (기본값: true) */
  colorCorrection?: boolean;
  /** 상위 K대 카메라 블렌딩 개수 (2 또는 3, 기본값: 3) */
  topK?: number;
  /** 렌더 모드 (기본값: 'vdt') */
  renderMode?: VdtRenderMode;
}

/** `worldCoordinates.ts` 를 거쳐 Three.js 공간(`GLTF_Y_UP`)으로 변환된 카메라 파라미터. */
export interface VdtCameraThree {
  cameraId: string;
  imageUrl: string;
  imageSize: readonly [number, number];
  /** Three.js 월드 점 P_three -> OpenCV 카메라 좌표 p_cv 회전 행렬 (R_w2c * THREE_TO_WORLD_3x3) */
  rThreeToCv: THREE.Matrix3;
  /** Three.js 월드 점 P_three -> OpenCV 카메라 좌표 이동 벡터 (t_w2c 와 동일) */
  tThreeToCv: THREE.Vector3;
  /** 정규화 내부 파라미터 (fx/W, fy/H, cx/W, cy/H) */
  normalizedIntrinsics: THREE.Vector4;
  /** 정본 World 좌표계 카메라 중심 위치 C_world = -R_w2c^T * t_w2c */
  positionWorld: VdtVec3;
  /** Three.js 공간 카메라 중심 위치 */
  positionThree: THREE.Vector3;
  /** Three.js 공간 카메라 정면(+Z_cv) 단위벡터 */
  forwardThree: THREE.Vector3;
  /** Three.js 공간 카메라 상방(-Y_cv) 단위벡터 */
  upThree: THREE.Vector3;
  /** Three.js 공간 카메라 우측(+X_cv) 단위벡터 */
  rightThree: THREE.Vector3;
  /** Three.js 카메라 `matrixWorldInverse` (OpenGL view matrix) */
  threeViewMatrix: THREE.Matrix4;
  /** Three.js 카메라 `matrixWorld` */
  threeMatrixWorld: THREE.Matrix4;
  /** Three.js 카메라 `projectionMatrix` */
  threeProjectionMatrix: THREE.Matrix4;
  /** 수직 시야각 (deg) */
  vfovDeg: number;
  /** sRGB 노출/색 보정 게인 */
  colorGainSrgb: THREE.Vector3;
}
