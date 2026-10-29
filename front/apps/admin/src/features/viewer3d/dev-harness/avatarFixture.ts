import { Quaternion, Vector3 } from 'three';

// 개발 화면 EXP-02 아바타(-65) 시험 자료. 자산은 fixtures/synthetic-mhr.glb(합성 127관절 리그, make_synthetic_mhr.py 가
// AI -88 내보내기로 만든 것)다. 실제 MHR 체형·관절 순서가 아니다. 포즈는 걷기 흉내이고 HMR 추정이 아니다.

/** fixtures/synthetic-mhr.glb 의 값. 파일을 다시 만들면 함께 고친다(avatarMesh 시험이 파일과 대조한다). */
export const SYNTHETIC_MHR = {
  meshSha256: 'sha256:9559301e4745dd758d1735c838a3a7b51f72eb41c7c6ec9596c7e931ef12fcb3',
  jointParentsSha256: 'sha256:d22c16506c652947a7d06e166145f6ee40f7b2e648b853ccdffd1eb733274bb2',
  jointCount: 127,
  pelvisJointIndex: 1,
} as const;

/**
 * fixtures/synthetic-mhr-dense.glb(make_synthetic_mhr.py --dense). 관절은 같고 메시만 실제 MHR 크기(정점 21,168·삼각형 36,504,
 * 관절 근처 2개 가중치)다. 다인원 성능 시험(avatar-crowd-*)용. 실제 체형·외형이 아니다.
 */
export const SYNTHETIC_MHR_DENSE = {
  ...SYNTHETIC_MHR,
  meshSha256: 'sha256:4b8b9d405753d7113e7cf999e83f96e7fe4e9055ad95271ba488efb4748216fc',
} as const;
/** MSW 가 dense 자산으로 주는 자산 버전(0 은 기본 합성 리그, 그 밖은 404). */
export const DENSE_ASSET_VERSION = 1;

/** 합성 리그 관절 번호(make_synthetic_mhr.py 의 BODY 순서). */
const J = {
  lShoulder: 8,
  lElbow: 9,
  rShoulder: 12,
  rElbow: 13,
  lHip: 15,
  lKnee: 16,
  rHip: 19,
  rKnee: 20,
} as const;

/** MHR 손가락 범위처럼 body 모드에서 null 로 오는 관절. */
const isFinger = (i: number) => (i >= 43 && i <= 64) || (i >= 79 && i <= 100);

export const avatarMeshUrl = (worldId: string, epoch: number, version: number) =>
  `/api/avatars/${worldId}/epochs/${epoch}/versions/${version}/mesh.glb`;

const r4 = (v: number) => Math.round(v * 10000) / 10000;
const toAxisAngle = (input: Quaternion): [number, number, number] => {
  // 같은 회전의 두 표현 중 회전각 ≤ π 인 쪽(w ≥ 0)을 쓴다.
  const q = input.w < 0 ? new Quaternion(-input.x, -input.y, -input.z, -input.w) : input;
  const w = Math.min(1, Math.max(-1, q.w));
  const angle = 2 * Math.acos(w);
  const s = Math.sqrt(1 - w * w);
  if (s < 1e-9) return [0, 0, 0];
  return [r4((q.x / s) * angle), r4((q.y / s) * angle), r4((q.z / s) * angle)];
};

const X = new Vector3(1, 0, 0);
const Z = new Vector3(0, 0, 1);

/**
 * 리그 → World 회전. 리그는 Y-up 이고 +Z 를 본다. Rx(+π/2) 로 세우면 앞이 World −Y 라,
 * World 진행 방향 heading(rad, +X 기준)을 보려면 Rz(heading + π/2) 를 더 돌린다.
 */
export function rootRotationFor(heading: number): [number, number, number] {
  const stand = new Quaternion().setFromAxisAngle(X, Math.PI / 2);
  const turn = new Quaternion().setFromAxisAngle(Z, heading + Math.PI / 2);
  return toAxisAngle(turn.multiply(stand));
}

/** 걷기 흉내 관절 회전(127개). stride 0 이면 선 자세다. 다리·팔은 리그 X 축으로 흔든다. */
export function walkRotations(phase: number, stride: number): ([number, number, number] | null)[] {
  const s = Math.sin(phase);
  const swing: Record<number, number> = {
    [J.lHip]: -0.45 * stride * s,
    [J.rHip]: 0.45 * stride * s,
    [J.lKnee]: 0.7 * stride * Math.max(0, -Math.sin(phase - 0.6)),
    [J.rKnee]: 0.7 * stride * Math.max(0, Math.sin(phase - 0.6)),
    [J.lShoulder]: 0.35 * stride * s,
    [J.rShoulder]: -0.35 * stride * s,
    [J.lElbow]: -0.25 - 0.1 * stride,
    [J.rElbow]: -0.25 - 0.1 * stride,
  };
  return Array.from({ length: SYNTHETIC_MHR.jointCount }, (_, i) => {
    if (isFinger(i)) return null;
    const angle = swing[i];
    return angle === undefined ? [0, 0, 0] : [r4(angle), 0, 0];
  });
}
