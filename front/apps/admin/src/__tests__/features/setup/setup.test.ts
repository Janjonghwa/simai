import {
  CAMERA_CORNERS,
  CAMERA_HEIGHT_HELP,
  DEFAULT_CAMERA_IDS,
  deriveHeightFields,
  deriveStep1Coverage,
  formatClock,
  formatGuideItem,
  headErrorPass,
  MAX_CAMERA_HEIGHT_M,
  resolveHeightCameraIds,
  STEPS,
  validateHeightFields,
} from '@/features/setup/setup';
import { buildCoverageGuidePayload } from '@/mocks/setup';
import { describe, expect, it } from 'vitest';

describe('설치 · 보정 1단계 카메라 연결 상태·쌍 등급·방향 안내 계산', () => {
  it('보정 단계 순서에서 1단계는 카메라 · 높이 확인이고 걷기 보정 단계가 없다', () => {
    expect(STEPS).toEqual(['카메라 · 높이 확인', '보정 검증', '완료']);
    expect(DEFAULT_CAMERA_IDS).toEqual(['cam1', 'cam2', 'cam3', 'cam4']);
    expect(CAMERA_CORNERS).toHaveLength(4);
  });

  it('CONNECTED 이고 정상 수신이면 쌍 등급(충분·약함·부족)을 정리하고 다음 버튼을 낸다', () => {
    const latest = buildCoverageGuidePayload({
      connectivity: 'CONNECTED',
      guides: [],
    });
    const view = deriveStep1Coverage(
      {
        calibration_version: 'cal-8',
        guides: [],
        site_id: 'classroom-1',
        status: 'OK',
        stale: false,
        received_at_ms: 1_793_690_000_100,
        age_ms: 100,
        latest,
      },
      false,
    );

    expect(view.connectivity).toBe('CONNECTED');
    expect(view.connectivityLabel).toBe('4대 시야 연결됨');
    expect(view.canProceedCleanly).toBe(true);
    expect(view.submitLabel).toBe('다음');
    expect(view.directionLine).toBe('4대 카메라 시야가 하나로 연결되었습니다');
    expect(view.pairs.map((p) => p.gradeLabel)).toEqual([
      '충분',
      '부족',
      '충분',
      '충분',
      '부족',
      '약함',
    ]);
    expect(view.cameras.every((c) => c.statusLabel === '정상')).toBe(true);
  });

  it('DISCONNECTED 와 한 줄 방향 안내 문구를 만들고 그대로 진행 버튼을 낸다', () => {
    const latest = buildCoverageGuidePayload({
      connectivity: 'DISCONNECTED',
      guides: [
        {
          camera_id: 'cam3',
          pair: ['cam3', 'cam4'],
          direction: 'RIGHT',
          magnitude: 'SMALL',
          reason_code: 'WEAK_PAIR_EDGE_CLUSTER',
        },
      ],
    });
    const view = deriveStep1Coverage(
      {
        calibration_version: null,
        guides: latest.guides,
        site_id: 'classroom-1',
        status: 'OK',
        stale: false,
        received_at_ms: 1_793_690_000_100,
        age_ms: 100,
        latest,
      },
      false,
    );

    expect(view.connectivity).toBe('DISCONNECTED');
    expect(view.connectivityLabel).toBe('시야 분리됨');
    expect(view.directionLine).toBe('cam3 카메라를 오른쪽으로 조금 돌려 주세요 (cam3–cam4)');
    expect(view.cameras.find((c) => c.id === 'cam3')?.guideText).toBe(
      'cam3 카메라를 오른쪽으로 조금 돌려 주세요 (cam3–cam4)',
    );
    expect(view.canProceedCleanly).toBe(false);
    expect(view.submitLabel).toBe('그대로 진행');
  });

  it('NO_VIDEO·STALE·계산 실패(UNKNOWN) 상태를 구분하고 진행을 막지 않는다', () => {
    const latest = buildCoverageGuidePayload({
      connectivity: 'UNKNOWN',
      cameraStatuses: { cam2: 'NO_VIDEO', cam4: 'STALE' },
      guides: [
        {
          camera_id: 'cam2',
          pair: ['cam2', 'cam3'],
          direction: 'NONE',
          magnitude: 'NONE',
          reason_code: 'NO_OVERLAP_VISIBLE',
        },
      ],
    });

    const staleView = deriveStep1Coverage(
      {
        calibration_version: null,
        guides: latest.guides,
        site_id: 'classroom-1',
        status: 'STALE',
        stale: true,
        received_at_ms: 1_793_690_000_000,
        age_ms: 12_500,
        latest,
      },
      false,
    );

    expect(staleView.connectivity).toBe('UNKNOWN');
    expect(staleView.connectivityLabel).toBe('연결 확인 불가');
    expect(staleView.stale).toBe(true);
    expect(staleView.calculationFailed).toBe(true);
    expect(staleView.noVideoCameras).toEqual(['cam2']);
    expect(staleView.staleCameras).toEqual(['cam4']);
    expect(staleView.cameras.find((c) => c.id === 'cam2')?.statusLabel).toBe('영상 없음');
    expect(staleView.cameras.find((c) => c.id === 'cam4')?.statusLabel).toBe('오래된 결과(STALE)');
    expect(staleView.statusNotices).toEqual([
      '오래된 결과(STALE) · 최근 수신된 각도 안내입니다',
      '영상 없음: cam2',
      '계산 실패 · 연결 상태를 확인할 수 없습니다',
    ]);
    expect(staleView.submitLabel).toBe('그대로 진행');

    expect(
      formatGuideItem({
        camera_id: 'cam2',
        pair: ['cam2', 'cam3'],
        direction: 'NONE',
        magnitude: 'NONE',
        reason_code: 'NO_OVERLAP_VISIBLE',
      }),
    ).toBe('cam2 · cam2–cam3 공통 시야 없음 — 각도를 조정해 주세요');

    const errorView = deriveStep1Coverage(undefined, true);
    expect(errorView.calculationFailed).toBe(true);
    expect(errorView.connectivity).toBe('UNKNOWN');
    expect(errorView.submitLabel).toBe('그대로 진행');
  });
});

describe('카메라 높이 기본값 채움·검증', () => {
  it('마지막 제출 높이를 기본값으로 채우고 값 없는 카메라만 필수 입력으로 표시한다', () => {
    const defaultsResponse = {
      site_id: 'classroom-1',
      height_tolerance_cm: 5,
      camera_heights_m: {
        cam1: 2.25,
        cam2: 2.3,
        cam3: 2.2,
        cam4: null,
      },
      all_present: false,
    };

    const cameraIds = resolveHeightCameraIds(defaultsResponse);
    expect(cameraIds).toEqual(['cam1', 'cam2', 'cam3', 'cam4']);

    const fields = deriveHeightFields(cameraIds, defaultsResponse.camera_heights_m, {});
    expect(fields).toEqual([
      {
        cameraId: 'cam1',
        defaultHeightM: 2.25,
        rawValue: '2.25',
        required: false,
        missing: false,
        invalid: false,
        valueM: 2.25,
      },
      {
        cameraId: 'cam2',
        defaultHeightM: 2.3,
        rawValue: '2.3',
        required: false,
        missing: false,
        invalid: false,
        valueM: 2.3,
      },
      {
        cameraId: 'cam3',
        defaultHeightM: 2.2,
        rawValue: '2.2',
        required: false,
        missing: false,
        invalid: false,
        valueM: 2.2,
      },
      {
        cameraId: 'cam4',
        defaultHeightM: null,
        rawValue: '',
        required: true,
        missing: true,
        invalid: false,
        valueM: null,
      },
    ]);

    expect(validateHeightFields(fields)).toEqual({
      valid: false,
      missingIds: ['cam4'],
      invalidIds: [],
      cameraHeightsM: {
        cam1: 2.25,
        cam2: 2.3,
        cam3: 2.2,
      },
    });

    const filled = deriveHeightFields(cameraIds, defaultsResponse.camera_heights_m, {
      cam4: '2.35',
    });
    expect(validateHeightFields(filled)).toEqual({
      valid: true,
      missingIds: [],
      invalidIds: [],
      cameraHeightsM: {
        cam1: 2.25,
        cam2: 2.3,
        cam3: 2.2,
        cam4: 2.35,
      },
    });
  });

  it(`0 이하이거나 ${MAX_CAMERA_HEIGHT_M}m 초과인 값은 거부한다`, () => {
    const fields = deriveHeightFields(
      DEFAULT_CAMERA_IDS,
      { cam1: 2.2, cam2: 2.2, cam3: 2.2, cam4: 2.2 },
      { cam1: '0', cam2: '25' },
    );
    expect(validateHeightFields(fields)).toEqual({
      valid: false,
      missingIds: [],
      invalidIds: ['cam1', 'cam2'],
      cameraHeightsM: {
        cam3: 2.2,
        cam4: 2.2,
      },
    });
  });

  it('높이 입력 도움말과 보조 유틸 함수를 유지한다', () => {
    expect(CAMERA_HEIGHT_HELP).toBe(
      '거치대 기둥을 늘 같은 길이로 펴고(테이프 표시 권장), 바꿨을 때만 다시 재세요',
    );
    expect(formatClock(80)).toBe('1:20');
    expect(headErrorPass(15)).toBe(true);
    expect(headErrorPass(16)).toBe(false);
  });
});
