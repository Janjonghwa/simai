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
  deriveWaitReasonView,
  draftGhostsOf,
  estimateRemainingSeconds,
  formatClock,
  formatGuideItem,
  formatMinSec,
  formatRemainingKorean,
  isPreviewSceneReady,
  MAX_CAMERA_HEIGHT_M,
  resolveHeightCameraIds,
  SETUP_TARGET_DURATION_SEC,
  STEPS,
  validateHeightFields,
  WAITING_STAGES,
  ZONE_STAGE,
  zoneMapTitle,
} from '@/features/setup/setup';
import {
  blockedLiveWorkId,
  isSetupBlockedByLiveWork,
  SETUP_LIVE_WORK_BLOCKED_TEXT,
  setupErrorMessage,
} from '@/features/setup/setupErrorMessage';
import {
  setupSessionResponseSchema,
  setupWaitReasonSchema,
  setupZoneDraftsResponseSchema,
  type SetupSessionResponse,
} from '@/features/setup/types';
import { ApiError } from '@/lib/http';
import { buildCoverageGuidePayload, buildSetupZoneDraftsResponse } from '@/mocks/setup';
import { describe, expect, it } from 'vitest';

describe('설치 · 보정 1단계 카메라 연결 상태·쌍 등급·방향 안내 계산', () => {
  it('보정 단계 순서에서 1단계는 카메라 · 높이 확인이고 마지막은 구역이며 걷기 보정 단계가 없다', () => {
    expect(STEPS).toEqual(['카메라 · 높이 확인', '자동 보정', '구역']);
    expect(STEPS[ZONE_STAGE]).toBe('구역');
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

  it('CONNECTED 의 선택 보강(OPTIONAL_IMPROVE)은 돌리라는 지시로 보이지 않는다', () => {
    const optional = (camera_id: string, pair: [string, string]) => ({
      camera_id,
      pair,
      direction: 'UP' as const,
      magnitude: 'MORE' as const,
      reason_code: 'OPTIONAL_IMPROVE' as const,
    });
    const latest = buildCoverageGuidePayload({
      connectivity: 'CONNECTED',
      guides: [
        optional('cam1', ['cam1', 'cam3']),
        optional('cam3', ['cam1', 'cam3']),
        optional('cam2', ['cam2', 'cam4']),
      ],
    });
    const view = deriveStep1Coverage(
      {
        calibration_version: 'cal-8',
        guides: latest.guides,
        site_id: 'lobby',
        status: 'OK',
        stale: false,
        received_at_ms: 1_793_690_000_100,
        age_ms: 100,
        latest,
      },
      false,
    );

    expect(view.canProceedCleanly).toBe(true);
    expect(view.cameras.map((c) => c.guideText)).toEqual([null, null, null, null]);
    expect(view.cameras.map((c) => c.angleLabel)).toEqual([
      '각도 양호',
      '각도 양호',
      '각도 양호',
      '각도 양호',
    ]);
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
    expect(view.cameras.find((c) => c.id === 'cam3')?.guideText).toBe(
      'cam3 카메라를 오른쪽으로 조금 돌려 주세요 (cam3–cam4)',
    );
    expect(view.canProceedCleanly).toBe(false);
    expect(view.submitLabel).toBe('그대로 진행');
  });

  it('한 카메라에 돌릴 안내가 둘이면 카메라 칸에 모두 이어 붙여 보인다', () => {
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
        {
          camera_id: 'cam3',
          pair: ['cam2', 'cam3'],
          direction: 'LEFT',
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
    expect(view.cameras.find((c) => c.id === 'cam3')?.angleLabel).toBe(
      'cam3 카메라를 오른쪽으로 조금 돌려 주세요 (cam3–cam4) · cam3 카메라를 왼쪽으로 조금 돌려 주세요 (cam2–cam3)',
    );
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

    // 작업 시작으로 정밀 굽기를 회수하고 끝난 세팅(S15P31A101-597): COMPLETED 여도 미리보기 맵으로 보인다
    const skipped = setupSessionResponseSchema.parse({
      ...previewSession,
      status: 'COMPLETED',
      stage: 'COMPLETED',
      progress_pct: 100,
      scene_version: null,
      bake_skipped: 'LIVE_WORK_STARTED',
    });
    expect(deriveSceneTierStatus(skipped)).toMatchObject({
      tier: 'PREVIEW',
      previewSceneVersion: 'scene-preview-1',
      finalSceneVersion: null,
      bakingTexture: false,
      label: '미리보기 맵',
    });
    expect(deriveSceneTierStatus(skipped).description).toContain('정밀 텍스처 굽기를 건너뜀');
    // 모르는 값은 null 로 보고 기존 판정(정식 Scene 없는 COMPLETED)을 따른다
    expect(
      setupSessionResponseSchema.parse({ ...previewSession, bake_skipped: 'SOMETHING_NEW' })
        .bake_skipped,
    ).toBeNull();
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

  it('수락·버리기 전 AI 초안만 3D 점선 윤곽(편집 초안과 겹치지 않는 id)으로 바꾼다', () => {
    const { drafts } = setupZoneDraftsResponseSchema.parse(
      buildSetupZoneDraftsResponse('019a0000-0000-7000-8000-000000000001'),
    );
    const [first, second] = drafts;
    expect(draftGhostsOf(drafts, [], [])).toEqual([
      { id: `ai-draft:${first!.candidate_id}`, box: boxOfDraftItem(first!) },
      { id: `ai-draft:${second!.candidate_id}`, box: boxOfDraftItem(second!) },
    ]);
    expect(draftGhostsOf(drafts, [first!.candidate_id], [])).toHaveLength(1);
    expect(draftGhostsOf(drafts, [first!.candidate_id], [second!.candidate_id])).toEqual([]);
  });

  it('구역 단계 3D 칸 제목은 세팅 맵 단계와 Scene 버전이고, 세팅 맵이 아직이면 3D 맵이다', () => {
    expect(zoneMapTitle(deriveSceneTierStatus(baseSession))).toBe('3D 맵');
    const preview: SetupSessionResponse = {
      ...baseSession,
      status: 'READY_FOR_ZONES',
      stage: 'READY_FOR_ZONES',
      preview_scene_version: 'scene-preview-1',
    };
    expect(zoneMapTitle(deriveSceneTierStatus(preview))).toBe('미리보기 맵 · scene-preview-1');
    const final: SetupSessionResponse = {
      ...preview,
      status: 'COMPLETED',
      stage: 'COMPLETED',
      scene_version: 'scene-final-1',
    };
    expect(zoneMapTitle(deriveSceneTierStatus(final))).toBe('정밀 3D 맵 · scene-final-1');
  });
});

describe('세팅 대기 이유 안내(S15P31A101-583)', () => {
  const reason = (over: Record<string, unknown>) =>
    setupWaitReasonSchema.parse({ code: 'WAITING_FOR_WORKER', since_ms: 1_000, ...over });

  it('없으면 null 이고, 실패한 세션에는 대기 이유를 보이지 않는다', () => {
    expect(deriveWaitReasonView(null)).toBeNull();
    expect(deriveWaitReasonView(undefined)).toBeNull();
    const session = setupSessionResponseSchema.parse({
      setup_session_id: 's1',
      site_id: 'classroom-1',
      work_unit_id: 1,
      status: 'FAILED',
      progress_pct: 0,
      camera_heights_m: { cam1: 2.4 },
      wait_reason: { code: 'WORKER_NOT_POLLING', since_ms: 1 },
    });
    expect(deriveWaitingView(session).waitReason).toBeNull();
    expect(
      deriveWaitingView({ ...session, status: 'COLLECTING_BACKGROUND' }).waitReason?.code,
    ).toBe('WORKER_NOT_POLLING');
  });

  it('Tier 0 우선 접수 중지는 근거(워커 알림/기록 추정)와 참고 지연을 덧붙인다', () => {
    const released = deriveWaitReasonView(
      reason({
        code: 'WORKER_PAUSED_TIER0',
        basis: 'WORKER_RELEASE',
        detail: 'Tier 0 우선으로 시작하지 않았다: 표시 파일이 120s 전 것이다',
        tier0_lag_ms: 1450.4,
      }),
    )!;
    expect(released.title).toBe(
      'GPU 서버가 관제(Tier 0) 우선 규칙으로 세팅을 아직 받지 않았습니다',
    );
    expect(released.notes).toEqual([
      'GPU 서버가 알려 옴: Tier 0 우선으로 시작하지 않았다: 표시 파일이 120s 전 것이다',
      '참고: 최근 관제 지연 1450 ms',
    ]);
    expect(released.hint).toContain('GPU 서버 상태를 확인');
    expect(released.tone).toBe('delay');

    const inferred = deriveWaitReasonView(
      reason({ code: 'WORKER_PAUSED_TIER0', basis: 'TIER0_HEALTH' }),
    )!;
    expect(inferred.notes).toEqual(['관제 지연을 알 수 없다는 기록으로 추정']);
  });

  it('배경 미완료는 덜 모인 카메라와 비켜 달라는 안내를, 재시도는 앞 실패를 보인다', () => {
    const bg = deriveWaitReasonView(
      reason({
        code: 'BACKGROUND_INCOMPLETE',
        cameras: [{ camera_id: 'cam_C', collection_pct: 15.4 }],
      }),
    )!;
    expect(bg.title).toBe('사람 없는 배경을 아직 다 모으지 못했습니다 (cam_C 15%)');
    expect(bg.hint).toBe('사람이 카메라 앞에서 잠시 비켜 주세요.');

    const retry = deriveWaitReasonView(reason({ previous_error_code: 'BACKGROUND_INCOMPLETE' }))!;
    expect(retry.tone).toBe('idle');
    expect(retry.notes[0]).toContain('앞 시도에서 배경을 다 모으지 못해');
    const other = deriveWaitReasonView(reason({ previous_error_code: 'BA_DIVERGED' }))!;
    expect(other.notes).toEqual(['앞 시도가 실패(BA_DIVERGED)해 다시 시도합니다.']);
  });

  it.each([
    ['NO_WORKER', 'GPU 서버(분석 워커)가 연결돼 있지 않습니다', 'delay'],
    ['WAITING_FOR_WORKER', 'GPU 서버가 세팅 작업을 가져가기를 기다리고 있습니다', 'idle'],
    ['WORKER_NOT_POLLING', 'GPU 서버가 세팅 작업을 가져가지 않고 있습니다', 'delay'],
    ['WORKER_STARTING', 'GPU 서버가 세팅 작업을 받아 시작을 준비하고 있습니다', 'idle'],
    ['SOMETHING_NEW', '세팅이 기다리는 중입니다 (SOMETHING_NEW)', 'idle'],
  ])('%s 는 짧은 안내와 할 일을 준다', (code, title, tone) => {
    const view = deriveWaitReasonView(reason({ code }))!;
    expect(view.title).toBe(title);
    expect(view.tone).toBe(tone);
    expect(view.hint.length).toBeGreaterThan(0);
    expect(view.showLastSeen).toBe(code === 'WORKER_NOT_POLLING');
  });

  it('다른 작업 처리 중이면 그 작업을 쉬운 이름으로 보인다', () => {
    expect(
      deriveWaitReasonView(
        reason({ code: 'WORKER_BUSY_OTHER_JOB', other_job_type: 'scene_regenerate' }),
      )!.title,
    ).toBe('GPU 서버가 다른 작업(3D 맵 다시 만들기)을 먼저 처리하고 있습니다');
    expect(formatMinSec(125)).toBe('2분 05초');
  });
});

describe('setupErrorMessage: 진행 중 작업 409 안내 매핑', () => {
  it('SETUP_BLOCKED_BY_LIVE_WORK(409)에 작업 이름이 없으면 일반 안내 문구를 돌려준다', () => {
    const error = new ApiError(409, {
      code: 'SETUP_BLOCKED_BY_LIVE_WORK',
      serverMessage:
        '같은 현장에 진행 중(IN_PROGRESS)인 작업이 있어 현장 세팅을 시작할 수 없습니다',
    });
    expect(isSetupBlockedByLiveWork(error)).toBe(true);
    expect(blockedLiveWorkId(error)).toBeNull();
    expect(setupErrorMessage(error)).toBe(SETUP_LIVE_WORK_BLOCKED_TEXT);
  });

  it('SETUP_BLOCKED_BY_LIVE_WORK(409)에 작업 이름·ID가 있으면 이름 포함 문구와 작업 ID를 돌려준다', () => {
    const error = new ApiError(409, {
      code: 'SETUP_BLOCKED_BY_LIVE_WORK',
      workId: 2,
      workName: '배관 점검',
      serverMessage:
        '같은 현장에 진행 중(IN_PROGRESS)인 작업이 있어 현장 세팅을 시작할 수 없습니다',
    });
    expect(isSetupBlockedByLiveWork(error)).toBe(true);
    expect(blockedLiveWorkId(error)).toBe(2);
    expect(setupErrorMessage(error)).toBe(
      '진행 중인 작업(배관 점검)이 있어 세팅을 시작할 수 없습니다. 작업을 종료한 뒤 다시 시도하세요',
    );
  });

  it('다른 409(SETUP_ALREADY_IN_PROGRESS)는 진행 중 작업 차단으로 보지 않고 서버 문구를 유지한다', () => {
    const error = new ApiError(409, {
      code: 'SETUP_ALREADY_IN_PROGRESS',
      serverMessage: '이미 진행 중인 현장 세팅 세션이 있습니다',
    });
    expect(isSetupBlockedByLiveWork(error)).toBe(false);
    expect(setupErrorMessage(error)).toBe('이미 진행 중인 현장 세팅 세션이 있습니다');
  });
});
