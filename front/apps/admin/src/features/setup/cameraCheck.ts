import { cameraShortLabel, pairLabel } from '@/features/setup/cameraLabels';
import type { CameraCheck, QualityGate, SetupSessionResponse } from '@/features/setup/types';

// 세팅 「보정 검증 결과」 쌍별 표와 「카메라 확인」 카드(S15P31A101-646, docs/design/S15P31A101-646-setup-camera-check.md).
// 관제에서 뜨는 카메라 경고(쌍 에피폴라·잔차 실패, 한 카메라 제외 재투영 틀어짐)를 세팅에서 같은 계산으로 먼저 통과해야 다음 단계로
// 간다. 판정은 워커(보정 품질 게이트·카메라 확인)와 서버(camera_check_passed)가 하고, 화면은 그 판정을 그대로 보인다.

export type CameraCheckStatus = CameraCheck['status'] | 'NONE';
/** 표 한 행의 종합 판정: 통과 · 실패 · 아직 모름(재는 중·근거 부족) · 해당 없음(시야가 겹치지 않는 쌍). */
export type PairOutcome = 'pass' | 'fail' | 'pending' | 'na';

export interface CameraCheckPairRow {
  key: string;
  label: string;
  /** 세팅 보정의 배경 특징점 홀드아웃 재투영 오차(중앙값·p90, px). 그 쌍을 보정에 쓰지 않았으면 null. */
  holdoutMedianPx: number | null;
  holdoutP90Px: number | null;
  holdoutVerdict: QualityGate['pairs'][string]['verdict'] | null;
  /** 카메라 확인의 쌍 통과율(0~1). 근거가 없으면 null. */
  passRate: number | null;
  samples: number;
  checkVerdict: CameraCheck['pairs'][string]['verdict'] | null;
  outcome: PairOutcome;
}

export interface CameraCheckCameraRow {
  cameraId: string;
  label: string;
  observedViews: number;
  looMedianPx: number | null;
  verdict: CameraCheck['cameras'][string]['verdict'];
}

export interface CameraCheckView {
  status: CameraCheckStatus;
  progressPct: number;
  elapsedS: number;
  /**
   * 서버가 카메라 확인 관문을 아는지(camera_check_passed 를 싣는다). 모르는 이전 서버는 관문이 없어 화면도 막지 않고 카드를 숨긴다.
   */
  gated: boolean;
  /** 서버 관문 판정(구역 단계·세팅 완료를 받는지). 관문을 모르는 서버면 true(막지 않는다). */
  passed: boolean;
  qualityVerdict: QualityGate['verdict'] | null;
  pairs: CameraCheckPairRow[];
  cameras: CameraCheckCameraRow[];
  /** 관리자에게 보일 원인 문장(품질 게이트 실패·확인 실패). */
  problems: string[];
}

const sameIds = (ids: readonly string[], a: string, b: string) =>
  ids.length === 2 && ((ids[0] === a && ids[1] === b) || (ids[0] === b && ids[1] === a));

function pairOutcome(
  holdout: QualityGate['pairs'][string] | undefined,
  check: CameraCheck['pairs'][string] | undefined,
): PairOutcome {
  if (holdout?.verdict === 'FAIL' || check?.verdict === 'FAIL') return 'fail';
  if (check?.verdict === 'PASS') return 'pass';
  if (check?.verdict === 'NOT_OVERLAPPING' && holdout?.verdict !== 'NEEDS_CHECK') return 'na';
  return 'pending';
}

/** 세션 조회의 quality_gate·camera_check·camera_check_passed 를 표와 카드로 바꾼다. 카메라 쌍은 세션 카메라 순서로 모두 만든다. */
export function deriveCameraCheckView(
  session: SetupSessionResponse | null | undefined,
  cameraIds: readonly string[],
): CameraCheckView {
  const gate = session?.quality_gate ?? null;
  const check = session?.camera_check ?? null;
  const holdouts = Object.values(gate?.pairs ?? {});
  const checks = Object.values(check?.pairs ?? {});
  const pairs: CameraCheckPairRow[] = [];
  for (let i = 0; i < cameraIds.length; i++) {
    for (let j = i + 1; j < cameraIds.length; j++) {
      const a = cameraIds[i]!;
      const b = cameraIds[j]!;
      const holdout = holdouts.find((p) => sameIds(p.camera_ids, a, b));
      const pc = checks.find((p) => sameIds(p.camera_ids, a, b));
      pairs.push({
        key: `${a}|${b}`,
        label: pairLabel(cameraIds, a, b),
        holdoutMedianPx: holdout?.median_px ?? null,
        holdoutP90Px: holdout?.p90_px ?? null,
        holdoutVerdict: holdout?.verdict ?? null,
        passRate: pc?.pass_rate ?? null,
        samples: pc?.samples ?? 0,
        checkVerdict: pc?.verdict ?? null,
        outcome: pairOutcome(holdout, pc),
      });
    }
  }
  const cameras: CameraCheckCameraRow[] = Object.entries(check?.cameras ?? {})
    .map(([cameraId, c]) => ({
      cameraId,
      label: cameraShortLabel(cameraIds, cameraId),
      observedViews: c.observed_views,
      looMedianPx: c.loo_median_px,
      verdict: c.verdict,
    }))
    .sort((x, y) => cameraIds.indexOf(x.cameraId) - cameraIds.indexOf(y.cameraId));
  const problems = [
    ...(gate?.verdict === 'FAILED' ? gate.problems.map((p) => p.message) : []),
    ...(check && (check.status === 'FAILED' || check.status === 'INCOMPLETE')
      ? check.problems.map((p) => p.message)
      : []),
  ];
  return {
    status: check?.status ?? 'NONE',
    progressPct: check ? Math.max(0, Math.min(100, check.progress_pct)) : 0,
    elapsedS: check?.elapsed_s ?? 0,
    gated: typeof session?.camera_check_passed === 'boolean',
    passed: session?.camera_check_passed !== false,
    qualityVerdict: gate?.verdict ?? null,
    pairs,
    cameras,
    problems,
  };
}

/** 카메라 확인이 끝났는데 통과하지 못했다(다시 보정해야 한다). */
export function cameraCheckFailed(view: CameraCheckView): boolean {
  return view.status === 'FAILED' || view.status === 'INCOMPLETE';
}

/** 카메라 확인 안내 문구(단계별). 통과면 null. */
export function cameraCheckGuide(view: CameraCheckView): string | null {
  if (view.passed) return null;
  switch (view.status) {
    case 'NONE':
      return '보정이 끝나면 카메라 확인을 시작합니다.';
    case 'WAITING':
      return '한 사람이 방 안을 30~60초 천천히 걸어 주세요. 다른 사람은 카메라 밖에 있어 주세요.';
    case 'MEASURING':
      return '계속 걸어 주세요. 카메라 사이 겹치는 곳을 고루 지나가면 빨리 끝납니다.';
    case 'PASSED':
      return '카메라 확인 결과를 반영하는 중입니다.';
    default:
      return '카메라 확인을 통과하지 못했습니다. 카메라가 흔들리지 않았는지 확인하고 다시 보정해 주세요.';
  }
}

/** 2단계 '다음' 을 막는 이유(카메라 확인 미통과). 통과면 null. */
export function cameraCheckBlockReason(view: CameraCheckView): string | null {
  if (view.passed) return null;
  return cameraCheckFailed(view)
    ? '카메라 확인을 통과하지 못했어요. 다시 보정해 주세요'
    : '카메라 확인을 마치면 넘어갈 수 있어요';
}
