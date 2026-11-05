import {
  boxOfDraftItem,
  CAMERA_CORNERS,
  CAMERA_HEIGHT_HELP,
  DEFAULT_CAMERA_IDS,
  deriveHeightFields,
  deriveHeightWarnings,
  deriveSceneTierStatus,
  deriveStep1Coverage,
  deriveWaitingView,
  estimateRemainingSeconds,
  formatClock,
  formatGuideItem,
  formatRemainingKorean,
  headErrorPass,
  isPreviewSceneReady,
  MAX_CAMERA_HEIGHT_M,
  resolveHeightCameraIds,
  SETUP_TARGET_DURATION_SEC,
  STEPS,
  validateHeightFields,
  WAITING_STAGES,
} from '@/features/setup/setup';
import { setupZoneDraftsResponseSchema, type SetupSessionResponse } from '@/features/setup/types';
import { buildCoverageGuidePayload, buildSetupZoneDraftsResponse } from '@/mocks/setup';
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

    // 받침 없는 방향(위·아래)은 '로', 받침 있는 방향(왼쪽·오른쪽)은 '으로'.
    const toward = (direction: 'LEFT' | 'RIGHT' | 'UP' | 'DOWN') =>
      formatGuideItem({
        camera_id: 'cam_A',
        pair: ['cam_A', 'cam_C'],
        direction,
        magnitude: 'MORE',
        reason_code: 'WEAK_PAIR_EDGE_CLUSTER',
      });
    expect(toward('UP')).toBe('cam_A 카메라를 위로 더 돌려 주세요 (cam_A–cam_C)');
    expect(toward('DOWN')).toBe('cam_A 카메라를 아래로 더 돌려 주세요 (cam_A–cam_C)');
    expect(toward('LEFT')).toBe('cam_A 카메라를 왼쪽으로 더 돌려 주세요 (cam_A–cam_C)');
    expect(toward('RIGHT')).toBe('cam_A 카메라를 오른쪽으로 더 돌려 주세요 (cam_A–cam_C)');

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

describe('설치 · 보정 2단계 이후: 대기 단계·진행률·높이 경고·Scene 단계·AI 초안 계산', () => {
  const baseSession: SetupSessionResponse = {
    setup_session_id: '019a0000-0000-7000-8000-000000000001',
    site_id: 'classroom-1',
    work_unit_id: 1,
    retry_of: null,
    job_id: 'job-setup-1',
    status: 'COLLECTING_BACKGROUND',
    stage: 'COLLECTING_BACKGROUND',
    progress_pct: 0,
    camera_heights_m: { cam1: 2.25, cam2: 2.3, cam3: 2.2, cam4: 2.35 },
    height_tolerance_cm: 5,
    background_collection_pct: { cam1: 50, cam2: 50, cam3: 40, cam4: 60 },
    height_checks: null,
    height_warnings: [],
    object_cross_checks: null,
    calibration_version: null,
    preview_scene_version: null,
    scene_version: null,
    config_version: null,
    zones_committed_at: null,
    zone_drafts_ref: null,
    failure_code: null,
    failure_message: null,
  };

  it('대기 4단계(배경 수집 → 3D 공간·좌표계 → 카메라 정밀 보정 → 미리보기 맵·구역 초안)와 목표 90초 기준 남은 시간을 계산한다', () => {
    expect(WAITING_STAGES.map((s) => s.label)).toEqual([
      '배경 수집',
      '3D 공간·좌표계',
      '카메라 정밀 보정',
      '미리보기 맵·구역 초안',
    ]);
    expect(SETUP_TARGET_DURATION_SEC).toBe(90);
    expect(estimateRemainingSeconds(0)).toBe(90);
    expect(estimateRemainingSeconds(50)).toBe(45);
    expect(estimateRemainingSeconds(100)).toBe(0);
    expect(formatRemainingKorean(90)).toBe('약 1분 30초');
    expect(formatRemainingKorean(45)).toBe('약 45초');
    expect(formatRemainingKorean(0)).toBe('곧 완료');

    const view = deriveWaitingView({
      ...baseSession,
      status: 'JOINT_BA_AND_VERIFY',
      stage: 'JOINT_BA_AND_VERIFY',
      progress_pct: 60,
    });
    expect(view.stageLabel).toBe('카메라 정밀 보정');
    expect(view.progressPct).toBe(60);
    expect(view.remainingSec).toBe(36);
    expect(view.remainingClock).toBe('0:36');
    expect(view.steps.map((s) => s.status)).toEqual(['done', 'done', 'current', 'upcoming']);
  });

  it('높이 불일치 경고(within_tolerance=false)와 미리보기/정밀 Scene 단계를 구분한다', () => {
    const warned = deriveHeightWarnings({
      ...baseSession,
      height_checks: {
        cam1: {
          input_height_m: 2.25,
          restored_height_m: 2.24,
          difference_cm: 1.0,
          within_tolerance: true,
          guidance: null,
        },
        cam2: {
          input_height_m: 2.3,
          restored_height_m: 2.15,
          difference_cm: 15.0,
          within_tolerance: false,
          guidance: 'cam2 입력 높이(2.30m)와 복원 높이(2.15m)가 15.0cm 차이납니다',
        },
      },
    });
    expect(warned).toHaveLength(1);
    expect(warned[0]?.cameraId).toBe('cam2');
    expect(warned[0]?.differenceCm).toBe(15.0);

    expect(isPreviewSceneReady(baseSession)).toBe(false);
    expect(deriveSceneTierStatus(baseSession).tier).toBe('NONE');

    const previewSession: SetupSessionResponse = {
      ...baseSession,
      status: 'BAKING_TEXTURE',
      stage: 'BAKING_TEXTURE',
      progress_pct: 85,
      preview_scene_version: 'scene-preview-1',
    };
    expect(isPreviewSceneReady(previewSession)).toBe(true);
    expect(deriveSceneTierStatus(previewSession)).toMatchObject({
      tier: 'PREVIEW',
      previewSceneVersion: 'scene-preview-1',
      finalSceneVersion: null,
      bakingTexture: true,
      label: '미리보기 맵',
    });

    const finalSession: SetupSessionResponse = {
      ...previewSession,
      status: 'COMPLETED',
      stage: 'COMPLETED',
      progress_pct: 100,
      scene_version: 'scene-final-1',
    };
    expect(deriveSceneTierStatus(finalSession)).toMatchObject({
      tier: 'FINAL',
      previewSceneVersion: 'scene-preview-1',
      finalSceneVersion: 'scene-final-1',
      bakingTexture: false,
      label: '정밀 3D 맵',
    });
  });

  it('VLM 구역 초안 응답 스키마(no_opinion 포함)를 검증하고 편집 상자(BoxBounds)로 변환한다', () => {
    const built = buildSetupZoneDraftsResponse('019a0000-0000-7000-8000-000000000001');
    const parsed = setupZoneDraftsResponseSchema.parse(built);
    expect(parsed.safety_use).toBe(false);
    expect(parsed.drafts).toHaveLength(2);
    expect(parsed.drafts[0]?.no_opinion).toBe(false);
    expect(parsed.drafts[1]?.vlm_status).toBe('NO_OPINION');
    expect(parsed.drafts[1]?.no_opinion).toBe(true);

    const box = boxOfDraftItem(parsed.drafts[0]!);
    expect(box).toEqual({
      min: [7.0, 8.0, 0],
      max: [8.6, 8.8, 2.0],
    });
  });
});
