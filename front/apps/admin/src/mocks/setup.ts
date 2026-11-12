import {
  setupModeSchema,
  setupZoneDraftsResponseSchema,
  type CameraReceptionStatus,
  type Connectivity,
  type CoverageGuideItem,
  type CoverageGuidePayload,
  type CoveragePairItem,
  type CoverageRelayStatus,
  type CreateSetupSessionBody,
  type RetrySetupSessionBody,
  type SetupMode,
  type SetupSessionResponse,
  type SetupZoneDraftsResponse,
  type SetupZoneDraftsResponseInput,
  type ZoneDraftItemInput,
} from '@/features/setup/types';
import { problem, readLiveUser, unauthorized } from '@/mocks/problem';
import { createSupplementMock } from '@/mocks/setupSupplement';
import { http, HttpResponse } from 'msw/http';

// 현장 세팅 1단계 및 2단계 이후(GET /api/cameras/coverage-guides, GET /api/setup-sessions/camera-height-defaults,
// POST /api/setup-sessions, GET /api/setup-sessions/:id, POST /api/setup-sessions/:id/retry,
// GET /api/setup-sessions/:id/zone-drafts) MSW 핸들러(S15P31A101-447·-452·-448·-455·-453).

const DEFAULT_SITE_ID = 'classroom-1';
/** previewReadyAfterMs 가 지난 세션이 되는 모양(미리보기 맵·보정 버전이 생겨 구역 단계로 갈 수 있다). */
const PREVIEW_READY_PATCH = {
  status: 'PREVIEW_AND_VLM_DRAFT',
  stage: 'PREVIEW_AND_VLM_DRAFT',
  progress_pct: 92,
  preview_scene_version: 'scene-preview-1',
  calibration_version: 'cal-8',
} as const satisfies Partial<SetupSessionResponse>;
const DEFAULT_TOLERANCE_CM = 5.0;
/** 세팅 완료(POST .../complete)를 받는 단계: 미리보기 맵 이후(서버 SetupCompletionService 와 같음). */
const COMPLETABLE_STAGES: ReadonlySet<string> = new Set([
  'READY_FOR_ZONES',
  'BAKING_TEXTURE',
  'COMPLETED',
]);
const STALE_AFTER_MS = 10_000;
const DEMAND_TTL_MS = 15_000;

export interface BuildCoverageGuideOptions {
  cameraIds?: readonly string[];
  connectivity?: Connectivity;
  cameraStatuses?: Readonly<Record<string, CameraReceptionStatus>>;
  pairs?: readonly CoveragePairItem[];
  guides?: readonly CoverageGuideItem[];
  computedAtMs?: number;
}

export function buildCoverageGuidePayload(
  options: BuildCoverageGuideOptions = {},
): CoverageGuidePayload {
  const cameraIds = options.cameraIds ?? ['cam1', 'cam2', 'cam3', 'cam4'];
  const computedAtMs = options.computedAtMs ?? 1_793_690_000_000;
  const connectivity = options.connectivity ?? 'CONNECTED';
  const cameraStatuses = options.cameraStatuses ?? {};

  const cameras = cameraIds.map((id, i) => {
    const status: CameraReceptionStatus = cameraStatuses[id] ?? 'OK';
    return {
      camera_id: id,
      status,
      frame_ts_ms: status === 'NO_VIDEO' ? null : computedAtMs - (i + 1) * 10,
    };
  });

  const [c0 = 'cam1', c1 = 'cam2', c2 = 'cam3', c3 = 'cam4'] = cameraIds;

  const defaultPairs: CoveragePairItem[] = [
    {
      a: c0,
      b: c1,
      raw_matches: 148,
      inliers: 64,
      inlier_ratio: 0.4324,
      grade: 'ENOUGH',
      centroid_a: [0.71, 0.52],
      centroid_b: [0.28, 0.5],
    },
    {
      a: c0,
      b: c2,
      raw_matches: 22,
      inliers: 4,
      inlier_ratio: 0.1818,
      grade: 'LACK',
      centroid_a: null,
      centroid_b: null,
    },
    {
      a: c0,
      b: c3,
      raw_matches: 120,
      inliers: 42,
      inlier_ratio: 0.35,
      grade: 'ENOUGH',
      centroid_a: [0.28, 0.51],
      centroid_b: [0.69, 0.49],
    },
    {
      a: c1,
      b: c2,
      raw_matches: 132,
      inliers: 51,
      inlier_ratio: 0.3864,
      grade: 'ENOUGH',
      centroid_a: [0.66, 0.55],
      centroid_b: [0.33, 0.54],
    },
    {
      a: c1,
      b: c3,
      raw_matches: 18,
      inliers: 2,
      inlier_ratio: 0.1111,
      grade: 'LACK',
      centroid_a: null,
      centroid_b: null,
    },
    {
      a: c2,
      b: c3,
      raw_matches: 80,
      inliers: 21,
      inlier_ratio: 0.2625,
      grade: 'WEAK',
      centroid_a: [0.79, 0.62],
      centroid_b: [0.16, 0.64],
    },
  ];

  const defaultGuides: CoverageGuideItem[] = [
    {
      camera_id: c2,
      pair: [c2, c3],
      direction: 'RIGHT',
      magnitude: 'SMALL',
      reason_code: 'WEAK_PAIR_EDGE_CLUSTER',
    },
  ];

  return {
    schema_version: '1.0.0',
    source_id: 'gw-lobby',
    computed_at_ms: computedAtMs,
    valid_until_ms: computedAtMs + STALE_AFTER_MS,
    capture_skew_ms: 18,
    params: {
      image_width_px: 1024,
      max_keypoints: 1024,
      ransac_threshold_px: 2.0,
      enough_min_inliers: 30,
      weak_min_inliers: 15,
      min_inlier_ratio: 0.1,
      intrinsics_source: 'STORED',
    },
    cameras,
    pairs: options.pairs ? [...options.pairs] : defaultPairs,
    connectivity,
    components: connectivity === 'CONNECTED' ? [[c0, c1, c2, c3]] : [[c0, c1, c2], [c3]],
    guides: options.guides ? [...options.guides] : defaultGuides,
  };
}

export function buildDefaultZoneDraftItems(): ZoneDraftItemInput[] {
  return [
    {
      draft_id: '019a0000-0000-7000-9000-000000000001',
      draft_index: 0,
      candidate_id: 'cand-opening-1',
      candidate_kind: 'OPENING',
      zone_kind: 'OPENING',
      eval_type: 'GROUND_FOOTPRINT',
      polygon_xy: [
        [7.0, 8.0],
        [8.6, 8.0],
        [8.6, 8.8],
        [7.0, 8.8],
      ],
      z_min_m: 0,
      z_max_m: 2.0,
      top_height_m: null,
      obb: null,
      recommended_name: '창가 개구부 추락 주의',
      recommended_severity: 'CRITICAL',
      rationale: '바닥 개구부 경계가 검출되어 추락 위험 구역으로 제안합니다.',
      vlm_status: 'VLM',
      evidence_cameras: ['cam1', 'cam2'],
      confidence: 0.86,
    },
    {
      draft_id: '019a0000-0000-7000-9000-000000000002',
      draft_index: 1,
      candidate_id: 'cand-surface-2',
      candidate_kind: 'ELEVATED_WORK_SURFACE',
      zone_kind: 'ELEVATED_WORK_SURFACE',
      eval_type: 'VOLUME_3D',
      polygon_xy: [
        [3.2, 0.3],
        [6.8, 0.3],
        [6.8, 1.3],
        [3.2, 1.3],
      ],
      z_min_m: 0,
      z_max_m: 2.2,
      top_height_m: 0.9,
      obb: {
        center: [5.0, 0.8, 0.45],
        size: [3.0, 0.6, 0.9],
        yaw_rad: 0,
      },
      recommended_name: '전면 단상 작업면 후보',
      recommended_severity: 'HIGH',
      rationale: '기하 단차 후보만 검출되었으며 AI 의견 없음(기본값)으로 표시합니다.',
      vlm_status: 'NO_OPINION',
      evidence_cameras: ['cam3', 'cam4'],
      confidence: null,
    },
  ];
}

export function buildSetupZoneDraftsResponse(
  setupSessionId: string,
  overrides: Partial<SetupZoneDraftsResponseInput> = {},
): SetupZoneDraftsResponse {
  return setupZoneDraftsResponseSchema.parse({
    draft_run_id: `draft-run-${setupSessionId}`,
    setup_session_id: setupSessionId,
    site_id: DEFAULT_SITE_ID,
    calibration_version: 'cal-8',
    preview_scene_version: 'scene-preview-1',
    safety_use: false,
    job_id: `job-vlm-${setupSessionId}`,
    status: 'SUCCEEDED',
    model_version: 'qwen2.5-vl-7b',
    prompt_version: 'zone-draft-v1',
    latency_ms: 1_840,
    error_code: null,
    error_message: null,
    created_at_ms: 1_793_690_010_000,
    completed_at_ms: 1_793_690_011_840,
    drafts: buildDefaultZoneDraftItems(),
    ...overrides,
  });
}

/**
 * 서버가 세팅을 실패로 끝내는 경우: 실패 코드마다 멈춘 시점의 진행률(서버는 stage 를 FAILED 로 덮어써서 마지막 진행률만 남는다)과
 * 서버 형식의 메시지(카메라 쌍은 DB 카메라 id). failSession 이 세션에 적용한다.
 */
export const SETUP_FAILURE_SCENARIOS = {
  INSUFFICIENT_FEATURES: {
    progress_pct: 40,
    failure_message: 'Insufficient raw feature matches on pair cam2-cam3: 7',
  },
  VALIDATION_REJECTED: {
    progress_pct: 40,
    failure_message: "Calibration validation rejected: worsened_pairs=['cam1-cam2', 'cam3-cam4']",
  },
  TIMEOUT: {
    progress_pct: 40,
    failure_message: 'Setup session timed out after 900 s',
  },
} as const;
export type SetupFailureScenario = keyof typeof SETUP_FAILURE_SCENARIOS;

export interface SetupMockState {
  siteId: string;
  heightToleranceCm: number;
  cameraHeightsM: Record<string, number | null>;
  guideStatus: CoverageRelayStatus;
  guideStale: boolean;
  guideError: boolean;
  latestGuide: CoverageGuidePayload | null;
}

export interface SetupMockOptions {
  now?: () => number;
  cameraHeightsM?: Record<string, number | null>;
  guideStatus?: CoverageRelayStatus;
  guideStale?: boolean;
  guideError?: boolean;
  latestGuide?: CoverageGuidePayload | null;
  /** GET /api/work-units/{id} 가 돌려줄 진행 중 세팅 세션 ID(새로고침 뒤 이어받기 시험). */
  workUnitSessionId?: string | null;
  /**
   * 개발 서버용: 세션을 시작한 지 이만큼(ms) 지나 처음 조회하면 미리보기 맵이 준비된 모양(PREVIEW_AND_VLM_DRAFT 92%)으로 바꿔 답한다.
   * 없으면 이 mock 은 세션을 저절로 진행시키지 않고 updateSession 으로만 바뀐다(시험).
   */
  previewReadyAfterMs?: number;
  /**
   * 조회할 때 시작 뒤 지난 시간으로 세션 단계를 넘긴다(개발 서버용, 시험은 끈다). 약 35초 뒤 COMPLETED 가 되어
   * 맵 보강 촬영 다시 굽기(S15P31A101-607)까지 mock 으로 해 볼 수 있다.
   */
  autoProgress?: boolean;
  /**
   * 새로 만든 세션을 새 모델 + World 이어짐(world_anchor.accepted)으로 둔다(S15P31A101-629). 구역 단계에 들어가면 지금 관제 구역
   * (bindLiveZones)을 확인 필요로 채운다. 없으면 World 끊김처럼 빈 목록에서 시작한다.
   */
  worldAnchorAccepted?: boolean;
}

/** 목록 채우기(seed)가 읽는 지금 관제 구역(zone_id, 레이아웃 항목이면 layout_item_id). */
export interface SetupMockLiveZone {
  zone_id: string;
  layout_item_id?: string | null;
}

/** autoProgress 단계표: 시작 뒤 이 시각(ms)부터 그 단계. */
const AUTO_STAGES: readonly { at: number; stage: SetupSessionResponse['status']; pct: number }[] = [
  { at: 0, stage: 'COLLECTING_BACKGROUND', pct: 10 },
  { at: 4_000, stage: 'INITIAL_POSE_AND_WORLD', pct: 30 },
  { at: 7_000, stage: 'JOINT_BA_AND_VERIFY', pct: 55 },
  { at: 10_000, stage: 'PREVIEW_AND_VLM_DRAFT', pct: 70 },
  { at: 13_000, stage: 'READY_FOR_ZONES', pct: 85 },
  { at: 25_000, stage: 'BAKING_TEXTURE', pct: 95 },
  { at: 35_000, stage: 'COMPLETED', pct: 100 },
];

export interface RetrySetupRecord {
  sessionId: string;
  retriedSessionId: string;
  body: RetrySetupSessionBody;
}

export function createSetupMock(options: SetupMockOptions = {}) {
  const { now = Date.now } = options;
  const state: SetupMockState = {
    siteId: DEFAULT_SITE_ID,
    heightToleranceCm: DEFAULT_TOLERANCE_CM,
    cameraHeightsM: options.cameraHeightsM
      ? { ...options.cameraHeightsM }
      : { cam1: 2.7, cam2: 2.7, cam3: 2.7, cam4: 2.7 },
    guideStatus: options.guideStatus ?? 'OK',
    guideStale: options.guideStale ?? options.guideStatus === 'STALE',
    guideError: options.guideError ?? false,
    latestGuide:
      options.latestGuide !== undefined ? options.latestGuide : buildCoverageGuidePayload(),
  };

  const submittedBodies: CreateSetupSessionBody[] = [];
  const retryRequests: RetrySetupRecord[] = [];
  const sessions = new Map<string, SetupSessionResponse>();
  const zoneDraftsBySession = new Map<string, SetupZoneDraftsResponse>();
  let seq = 0;

  function updateSession(
    sessionId: string,
    patch: Partial<SetupSessionResponse>,
  ): SetupSessionResponse {
    const current = sessions.get(sessionId);
    if (!current) {
      throw new Error(`Setup session not found in mock: ${sessionId}`);
    }
    const updated: SetupSessionResponse = {
      ...current,
      ...patch,
      updated_at_ms: now(),
    };
    sessions.set(sessionId, updated);
    return updated;
  }

  /** 세션을 서버가 실패시킨 것처럼 FAILED 로 바꾼다(실패 코드·메시지·멈춘 진행률은 SETUP_FAILURE_SCENARIOS). */
  function failSession(sessionId: string, scenario: SetupFailureScenario): SetupSessionResponse {
    const { progress_pct, failure_message } = SETUP_FAILURE_SCENARIOS[scenario];
    return updateSession(sessionId, {
      status: 'FAILED',
      stage: 'FAILED',
      progress_pct,
      failure_code: scenario,
      failure_message,
    });
  }

  /** previewReadyAfterMs 옵션이 있고 수집 중인 세션이 그 시간이 지났으면 미리보기 맵이 준비된 모양으로 바꾼다. */
  function advanceIfDue(session: SetupSessionResponse, t: number): SetupSessionResponse {
    const { previewReadyAfterMs } = options;
    if (previewReadyAfterMs === undefined || session.status !== 'COLLECTING_BACKGROUND') {
      return session;
    }
    if (t - (session.started_at_ms ?? t) < previewReadyAfterMs) return session;
    return updateSession(
      session.setup_session_id,
      session.mode === 'REUSE'
        ? { ...PREVIEW_READY_PATCH, preview_scene_version: null }
        : PREVIEW_READY_PATCH,
    );
  }

  /**
   * 세팅 세션의 구역을 발행했을 때(POST /api/zones/commit 의 setup_session_id) 세팅에 보이는 일: 아직 안 끝난 세션을 COMPLETED 로
   * 보내고 구역을 처음 발행한 시각(zones_committed_at)을 남긴다(개발 서버에서 굽기 끝을 기다리지 않게). 작업 관리의 '세팅 끝'
   * (SETUP_DONE)은 이것이 아니라 세팅 완료(POST .../complete, -615)의 setup_completed_at_ms 로 정한다(서버와 같음).
   * 없는 세션·실패한 세션은 그대로 둔다.
   */
  function completeAfterZonesCommit(sessionId: string, at: number = now()): void {
    const session = sessions.get(sessionId);
    if (!session || session.status === 'FAILED') return;
    updateSession(sessionId, {
      status: 'COMPLETED',
      stage: 'COMPLETED',
      progress_pct: 100,
      zones_committed_at: session.zones_committed_at ?? at,
      completed_at_ms: session.completed_at_ms ?? at,
    });
  }

  const supplement = createSupplementMock({ now, sessions, updateSession });

  /** 새 세션의 모드 칸. mode 는 실제로 따른 모드, requested_mode 는 요청한 모드다(새 모델로 바뀌는 경우는 흉내 내지 않는다). */
  const modePatch = (mode: SetupMode): Partial<SetupSessionResponse> => ({
    mode,
    requested_mode: mode,
  });
  /** 새 세션의 World 다시 맞추기 판정(옵션이 있을 때만 싣는다). */
  const anchorPatch = (): Partial<SetupSessionResponse> =>
    options.worldAnchorAccepted ? { world_anchor: { accepted: true } } : {};
  /** 목록 채우기가 읽는 지금 관제 구역. 개발 서버는 구역 mock 을 잇는다(bindLiveZones), 시험은 기본 빈 목록. */
  let liveZones: () => readonly SetupMockLiveZone[] = () => [];
  /** 확인 필요(needs_review)를 서버처럼 정리한다: 이어 쓰는 구역을 고치지 않은 항목만 true 를 남긴다. */
  const normalizeReview = (item: unknown): unknown => {
    const record = item as Record<string, unknown>;
    const review =
      record.needs_review === true &&
      record.zone_id != null &&
      record.zone == null &&
      record.surface == null;
    return { ...record, needs_review: review };
  };
  const pendingOf = (id: string) =>
    (zonePlans.get(id)?.items ?? []).filter(
      (item) => (item as Record<string, unknown>).needs_review === true,
    ).length;

  /** 세션별 구역 목록(-612). 저장한 적이 없으면 없다(빈 목록·revision 0). */
  const zonePlans = new Map<string, { items: unknown[]; revision: number; updatedAt: number }>();
  const planView = (id: string) => {
    const stored = zonePlans.get(id);
    return {
      setup_session_id: id,
      items: stored?.items ?? [],
      revision: stored?.revision ?? 0,
      updated_at: stored?.updatedAt ?? null,
    };
  };
  /** 완료가 적용한 것처럼 목록을 나눈다(개발 서버는 관제 구역 mock 을 바꾸지 않는다). 새 구역은 임시 zone_id 를 준다. */
  const appliedOf = (id: string) => {
    const items = zonePlans.get(id)?.items ?? [];
    const created: string[] = [];
    const updated: string[] = [];
    const kept: string[] = [];
    items.forEach((item, index) => {
      const zoneId = Reflect.get(item as object, 'zone_id');
      if (typeof zoneId !== 'string') created.push(`z-setup-${id.slice(-4)}-${index}`);
      else if (Reflect.get(item as object, 'zone') != null) updated.push(zoneId);
      else kept.push(zoneId);
    });
    return { created, updated, kept, archived: [] as string[] };
  };

  /** autoProgress: 시작 뒤 지난 시간만큼 단계를 앞으로만 넘긴다. 실패·취소한 세션은 두지 않는다. */
  function progressSession(session: SetupSessionResponse): SetupSessionResponse {
    if (
      !options.autoProgress ||
      session.status === 'FAILED' ||
      session.started_at_ms === undefined
    ) {
      return session;
    }
    const elapsed = now() - session.started_at_ms;
    const target = [...AUTO_STAGES].reverse().find((s) => elapsed >= s.at);
    const order = AUTO_STAGES.map((s) => s.stage);
    if (!target || order.indexOf(target.stage) <= order.indexOf(session.status)) return session;
    const idx = order.indexOf(target.stage);
    // 이전 모델 재사용은 새 맵을 만들지 않아 미리보기·정식 맵 버전이 없다(서버와 같다).
    const bakes = session.mode !== 'REUSE';
    const n = session.setup_session_id.slice(-4);
    const cams = Object.keys(session.camera_heights_m);
    return updateSession(session.setup_session_id, {
      status: target.stage,
      stage: target.stage,
      progress_pct: target.pct,
      background_collection_pct: Object.fromEntries(cams.map((c) => [c, idx > 0 ? 100 : 60])),
      calibration_version: idx >= 2 ? `cal-setup-${n}` : null,
      preview_scene_version: bakes && idx >= 4 ? `scene-preview-setup-${n}` : null,
      scene_version: bakes && idx >= 6 ? (session.scene_version ?? `scene-setup-${n}`) : null,
      height_checks:
        idx >= 4
          ? Object.fromEntries(
              Object.entries(session.camera_heights_m).map(([c, h]) => [
                c,
                {
                  input_height_m: h,
                  restored_height_m: h + 0.01,
                  difference_cm: 1,
                  within_tolerance: true,
                  guidance: null,
                },
              ]),
            )
          : null,
      ready_at_ms: idx >= 4 ? (session.ready_at_ms ?? now()) : null,
      completed_at_ms: idx >= 6 ? now() : null,
    });
  }

  function setZoneDrafts(
    sessionId: string,
    overrides: Partial<SetupZoneDraftsResponseInput>,
  ): SetupZoneDraftsResponse {
    const built = buildSetupZoneDraftsResponse(sessionId, overrides);
    zoneDraftsBySession.set(sessionId, built);
    return built;
  }

  const handlers = [
    http.get('*/api/cameras/coverage-guides', ({ request }) => {
      const t = now();
      if (!readLiveUser(request, t)) return unauthorized();
      if (state.guideError) {
        return problem(503, 'COVERAGE_GUIDE_UNAVAILABLE', '카메라 각도 안내를 계산하지 못했습니다');
      }
      const isEmpty = state.guideStatus === 'EMPTY' || state.latestGuide === null;
      const isStale = !isEmpty && (state.guideStale || state.guideStatus === 'STALE');
      const status: CoverageRelayStatus = isEmpty ? 'EMPTY' : isStale ? 'STALE' : 'OK';
      const ageMs = isEmpty ? null : isStale ? 12_500 : 1_200;
      const receivedAtMs = ageMs === null ? null : t - ageMs;

      return HttpResponse.json({
        calibration_version: 'cal-8',
        guides: [],
        site_id: state.siteId,
        status,
        stale: isStale,
        active_demand: true,
        demand_ttl_ms: DEMAND_TTL_MS,
        last_admin_query_at_ms: t,
        demand_until_ms: t + DEMAND_TTL_MS,
        stale_after_ms: STALE_AFTER_MS,
        received_at_ms: receivedAtMs,
        age_ms: ageMs,
        latest: isEmpty ? null : state.latestGuide,
      });
    }),

    http.get('*/api/setup-sessions/camera-height-defaults', ({ request }) => {
      const t = now();
      if (!readLiveUser(request, t)) return unauthorized();
      const entries = Object.entries(state.cameraHeightsM);
      const allPresent =
        entries.length > 0 &&
        entries.every(([, v]) => typeof v === 'number' && Number.isFinite(v) && v > 0);
      return HttpResponse.json({
        site_id: state.siteId,
        height_tolerance_cm: state.heightToleranceCm,
        camera_heights_m: { ...state.cameraHeightsM },
        all_present: allPresent,
      });
    }),

    http.post('*/api/setup-sessions', async ({ request }) => {
      const t = now();
      const user = readLiveUser(request, t);
      if (!user) return unauthorized();

      const raw: unknown = await request.json().catch(() => null);
      if (typeof raw !== 'object' || raw === null) {
        return problem(400, 'INVALID_SETUP_REQUEST', '세팅 시작 요청 본문이 올바르지 않습니다');
      }

      const workUnitId = Reflect.get(raw, 'work_unit_id');
      const rawHeights = Reflect.get(raw, 'camera_heights_m');
      if (typeof workUnitId !== 'number' || !Number.isInteger(workUnitId) || workUnitId < 1) {
        return problem(400, 'INVALID_SETUP_REQUEST', '유효한 작업 단위 ID가 필요합니다');
      }
      // 서버처럼(SetupReuseMode.parseRequested) 모드를 생략하면 새 모델이고 NEW·REUSE 밖의 값은 400 이다.
      const sentMode = Reflect.get(raw, 'mode');
      const parsedMode = setupModeSchema.safeParse(sentMode ?? 'NEW');
      if (!parsedMode.success) {
        return problem(400, 'INVALID_SETUP_REQUEST', '세팅 모드가 올바르지 않습니다');
      }
      const requestedMode = parsedMode.data;
      // 서버처럼(SetupService.create) 끝나지 않은 세션이 있으면 새로 시작할 수 없다.
      const running = [...sessions.values()].some(
        (session) => session.status !== 'COMPLETED' && session.status !== 'FAILED',
      );
      if (running) {
        return problem(409, 'SETUP_ALREADY_IN_PROGRESS', '이미 진행 중인 세팅 세션이 있습니다');
      }

      const submittedHeights: Record<string, number> = {};
      if (typeof rawHeights === 'object' && rawHeights !== null) {
        for (const [camId, val] of Object.entries(rawHeights)) {
          if (typeof val !== 'number' || !Number.isFinite(val) || val <= 0 || val > 20) {
            return problem(
              400,
              'INVALID_SETUP_REQUEST',
              `카메라 ${camId} 높이 값이 유효하지 않습니다`,
            );
          }
          submittedHeights[camId] = val;
        }
      }

      // 제출한 유효 높이는 다음 기본값으로 즉시 저장된다(SetupService.java 와 동일).
      for (const [camId, val] of Object.entries(submittedHeights)) {
        state.cameraHeightsM[camId] = val;
      }

      const effectiveHeights: Record<string, number> = {};
      for (const [camId, storedVal] of Object.entries(state.cameraHeightsM)) {
        const val = submittedHeights[camId] ?? storedVal;
        if (val === null || val === undefined || val <= 0 || val > 20) {
          return problem(400, 'CAMERA_HEIGHT_REQUIRED', `카메라 ${camId} 높이를 입력해야 합니다`);
        }
        effectiveHeights[camId] = val;
      }

      submittedBodies.push({
        work_unit_id: workUnitId,
        camera_heights_m: { ...submittedHeights },
        ...(sentMode === undefined ? {} : { mode: requestedMode }),
      });

      const sessionId = `019a0000-0000-7000-8000-${String(++seq).padStart(12, '0')}`;
      const collectionPct = Object.fromEntries(
        Object.keys(effectiveHeights).map((camId) => [camId, 0]),
      );
      const session: SetupSessionResponse = {
        setup_session_id: sessionId,
        site_id: state.siteId,
        work_unit_id: workUnitId,
        retry_of: null,
        job_id: `job-setup-${seq}`,
        status: 'COLLECTING_BACKGROUND',
        stage: 'COLLECTING_BACKGROUND',
        progress_pct: 0,
        camera_heights_m: effectiveHeights,
        height_tolerance_cm: state.heightToleranceCm,
        background_collection_pct: collectionPct,
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
        requested_by: 1,
        started_at_ms: t,
        ready_at_ms: null,
        completed_at_ms: null,
        updated_at_ms: t,
        ...modePatch(requestedMode),
        ...anchorPatch(),
      };
      sessions.set(sessionId, session);
      return HttpResponse.json(session, { status: 202 });
    }),

    http.get('*/api/work-units/:id', ({ request, params }) => {
      if (!readLiveUser(request, now())) return unauthorized();
      return HttpResponse.json({
        work_id: String(params.id ?? ''),
        active_setup_session_id: options.workUnitSessionId ?? null,
      });
    }),

    http.get('*/api/setup-sessions/:id', ({ request, params }) => {
      const t = now();
      if (!readLiveUser(request, t)) return unauthorized();
      const id = String(params.id ?? '');
      const found = sessions.get(id);
      if (!found) {
        return problem(404, 'SETUP_SESSION_NOT_FOUND', '세팅 세션을 찾을 수 없습니다');
      }
      return HttpResponse.json(supplement.withView(progressSession(advanceIfDue(found, t))));
    }),

    http.post('*/api/setup-sessions/:id/cancel', ({ request, params }) => {
      if (!readLiveUser(request, now())) return unauthorized();
      const id = String(params.id ?? '');
      const found = sessions.get(id);
      if (!found) return problem(404, 'SETUP_SESSION_NOT_FOUND', '세팅 세션을 찾을 수 없습니다');
      if (found.status === 'COMPLETED' || found.status === 'FAILED') {
        return problem(409, 'SETUP_CANCEL_NOT_ALLOWED', '이미 끝난 세팅 세션은 취소할 수 없습니다');
      }
      const cancelled = updateSession(id, {
        status: 'FAILED',
        stage: 'FAILED',
        failure_code: 'CANCELLED_BY_ADMIN',
        failure_message: '관리자가 세팅을 취소했습니다',
      });
      return HttpResponse.json(cancelled);
    }),

    http.post('*/api/setup-sessions/:id/retry', async ({ request, params }) => {
      const t = now();
      if (!readLiveUser(request, t)) return unauthorized();
      const prevId = String(params.id ?? '');
      const prevSession = sessions.get(prevId);
      if (!prevSession) {
        return problem(404, 'SETUP_SESSION_NOT_FOUND', '세팅 세션을 찾을 수 없습니다');
      }
      // 서버처럼(SetupService.retry) 실패한 세션만 재시도할 수 있다. 끝났거나 진행 중이면 새로 시작하거나 취소해야 한다.
      if (prevSession.status !== 'FAILED') {
        return problem(409, 'SETUP_RETRY_NOT_ALLOWED', '실패한 세팅 세션만 재시도할 수 있습니다');
      }

      const raw: unknown = await request.json().catch(() => ({}));
      const rawHeights =
        typeof raw === 'object' && raw !== null ? Reflect.get(raw, 'camera_heights_m') : undefined;

      const submittedHeights: Record<string, number> = {};
      if (typeof rawHeights === 'object' && rawHeights !== null) {
        for (const [camId, val] of Object.entries(rawHeights)) {
          if (typeof val !== 'number' || !Number.isFinite(val) || val <= 0 || val > 20) {
            return problem(
              400,
              'INVALID_SETUP_REQUEST',
              `카메라 ${camId} 높이 값이 유효하지 않습니다`,
            );
          }
          submittedHeights[camId] = val;
          state.cameraHeightsM[camId] = val;
        }
      }

      const effectiveHeights: Record<string, number> = {
        ...prevSession.camera_heights_m,
        ...submittedHeights,
      };

      const retriedSessionId = `019a0000-0000-7000-8000-${String(++seq).padStart(12, '0')}`;
      const collectionPct = Object.fromEntries(
        Object.keys(effectiveHeights).map((camId) => [camId, 0]),
      );
      const retried: SetupSessionResponse = {
        setup_session_id: retriedSessionId,
        site_id: prevSession.site_id,
        work_unit_id: prevSession.work_unit_id,
        retry_of: prevId,
        job_id: `job-setup-${seq}`,
        status: 'COLLECTING_BACKGROUND',
        stage: 'COLLECTING_BACKGROUND',
        progress_pct: 0,
        camera_heights_m: effectiveHeights,
        height_tolerance_cm: state.heightToleranceCm,
        background_collection_pct: collectionPct,
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
        requested_by: 1,
        started_at_ms: t,
        ready_at_ms: null,
        completed_at_ms: null,
        updated_at_ms: t,
        ...modePatch(prevSession.requested_mode ?? 'NEW'),
        ...anchorPatch(),
      };
      sessions.set(retriedSessionId, retried);
      retryRequests.push({
        sessionId: prevId,
        retriedSessionId,
        body:
          Object.keys(submittedHeights).length > 0 ? { camera_heights_m: submittedHeights } : {},
      });

      return HttpResponse.json(retried, { status: 202 });
    }),

    http.get('*/api/setup-sessions/:id/zone-drafts', ({ request, params }) => {
      const t = now();
      if (!readLiveUser(request, t)) return unauthorized();
      const id = String(params.id ?? '');
      const session = sessions.get(id);
      if (!session) {
        return problem(404, 'SETUP_SESSION_NOT_FOUND', '세팅 세션을 찾을 수 없습니다');
      }

      const existing = zoneDraftsBySession.get(id);
      if (existing) {
        return HttpResponse.json(existing);
      }

      const built = buildSetupZoneDraftsResponse(id, {
        site_id: session.site_id,
        calibration_version: session.calibration_version ?? 'cal-8',
        preview_scene_version: session.preview_scene_version ?? 'scene-preview-1',
      });
      zoneDraftsBySession.set(id, built);
      return HttpResponse.json(built);
    }),
    // 세팅 중 구역 목록(-612): 통째로 저장, revision 이 다르면 409 SETUP_ZONE_PLAN_CONFLICT, 완료 뒤면 409 SETUP_ZONES_CLOSED
    http.get('*/api/setup-sessions/:id/zones', ({ request, params }) => {
      if (!readLiveUser(request, now())) return unauthorized();
      const id = String(params.id ?? '');
      if (!sessions.has(id)) {
        return problem(404, 'SETUP_SESSION_NOT_FOUND', '세팅 세션을 찾을 수 없습니다');
      }
      return HttpResponse.json(planView(id));
    }),

    http.put('*/api/setup-sessions/:id/zones', async ({ request, params }) => {
      const t = now();
      const user = readLiveUser(request, t);
      if (!user) return unauthorized();
      if (user.role !== 'ADMIN') return problem(403, 'FORBIDDEN', '관리자만 저장할 수 있습니다');
      const id = String(params.id ?? '');
      const session = sessions.get(id);
      if (!session) {
        return problem(404, 'SETUP_SESSION_NOT_FOUND', '세팅 세션을 찾을 수 없습니다');
      }
      if (session.setup_completed_at_ms != null || session.status === 'FAILED') {
        return problem(409, 'SETUP_ZONES_CLOSED', '세팅이 끝나 구역 목록을 고칠 수 없습니다');
      }
      const raw: unknown = await request.json().catch(() => null);
      const items = typeof raw === 'object' && raw !== null ? Reflect.get(raw, 'items') : undefined;
      const revision =
        typeof raw === 'object' && raw !== null ? Reflect.get(raw, 'revision') : undefined;
      const valid =
        Array.isArray(items) &&
        items.length <= 100 &&
        items.every((item: unknown) => {
          if (typeof item !== 'object' || item === null) return false;
          const source = Reflect.get(item, 'source');
          const zoneId = Reflect.get(item, 'zone_id');
          return (
            (source === 'AI' || source === 'MANUAL') &&
            (zoneId != null || Reflect.get(item, 'zone') != null)
          );
        });
      if (!valid || typeof revision !== 'number') {
        return problem(400, 'INVALID_SETUP_ZONE_PLAN', '세팅 구역 목록이 올바르지 않습니다');
      }
      const stored = zonePlans.get(id);
      if ((stored?.revision ?? 0) !== revision) {
        return problem(
          409,
          'SETUP_ZONE_PLAN_CONFLICT',
          '다른 곳에서 먼저 고친 세팅 구역 목록입니다',
        );
      }
      zonePlans.set(id, {
        items: (items as unknown[]).map(normalizeReview),
        revision: revision + 1,
        updatedAt: t,
      });
      return HttpResponse.json(planView(id));
    }),

    // 처음 목록 채우기(-629): 저장한 적 없는 목록만. 재사용은 그대로, 새 모델 + World 이어짐은 확인 필요로, World 끊김은 채우지 않는다
    http.post('*/api/setup-sessions/:id/zones/seed', ({ request, params }) => {
      const t = now();
      const user = readLiveUser(request, t);
      if (!user) return unauthorized();
      if (user.role !== 'ADMIN') return problem(403, 'FORBIDDEN', '관리자만 저장할 수 있습니다');
      const id = String(params.id ?? '');
      const session = sessions.get(id);
      if (!session) {
        return problem(404, 'SETUP_SESSION_NOT_FOUND', '세팅 세션을 찾을 수 없습니다');
      }
      if (session.setup_completed_at_ms != null || session.status === 'FAILED') {
        return problem(409, 'SETUP_ZONES_CLOSED', '세팅이 끝나 구역 목록을 고칠 수 없습니다');
      }
      const reuse = session.mode === 'REUSE';
      if ((reuse || session.world_anchor?.accepted === true) && !zonePlans.has(id)) {
        const items = liveZones().map((zone) => ({
          zone_id: zone.zone_id,
          source: 'MANUAL',
          candidate_id: null,
          zone: null,
          surface: null,
          needs_review: !reuse && zone.layout_item_id == null,
        }));
        zonePlans.set(id, { items, revision: 1, updatedAt: t });
      }
      return HttpResponse.json(planView(id));
    }),

    // 세팅 완료(C17, -615): 미리보기 맵 이후·완료 전만 받는다. 완료 시각을 남기고 적용 결과(구역은 개발 서버에서 따로 저장)를 돌려준다
    http.post('*/api/setup-sessions/:id/complete', ({ request, params }) => {
      const t = now();
      const user = readLiveUser(request, t);
      if (!user) return unauthorized();
      if (user.role !== 'ADMIN')
        return problem(403, 'FORBIDDEN', '관리자만 세팅을 완료할 수 있습니다');
      const id = String(params.id ?? '');
      const found = sessions.get(id);
      if (!found) {
        return problem(404, 'SETUP_SESSION_NOT_FOUND', '세팅 세션을 찾을 수 없습니다');
      }
      const session = progressSession(advanceIfDue(found, t));
      if (session.setup_completed_at_ms != null) {
        return problem(409, 'SETUP_ALREADY_COMPLETED', '이 작업은 세팅을 이미 완료했습니다');
      }
      if (!COMPLETABLE_STAGES.has(session.status)) {
        return problem(
          409,
          'SETUP_NOT_READY_TO_COMPLETE',
          '미리보기 맵이 나온 뒤에 완료할 수 있습니다',
        );
      }
      const unconfirmed = pendingOf(id);
      if (unconfirmed > 0) {
        return HttpResponse.json(
          {
            status: 409,
            code: 'SETUP_ZONES_UNCONFIRMED',
            detail: `확인이 필요한 구역 ${unconfirmed}개가 남아 있습니다. 구역마다 확인·수정·삭제한 뒤 세팅을 완료하세요`,
            unconfirmed_count: unconfirmed,
          },
          { status: 409 },
        );
      }
      const done = updateSession(id, { setup_completed_at_ms: t });
      return HttpResponse.json({ ...supplement.withView(done), applied_zones: appliedOf(id) });
    }),
    ...supplement.handlers,
  ];

  return {
    handlers,
    state,
    submittedBodies,
    retryRequests,
    sessions,
    zoneDraftsBySession,
    updateSession,
    failSession,
    completeAfterZonesCommit,
    setZoneDrafts,
    zonePlans,
    /** 목록 채우기가 읽을 지금 관제 구역을 잇는다(개발 서버: 구역 mock). */
    bindLiveZones(read: () => readonly SetupMockLiveZone[]) {
      liveZones = read;
    },
  };
}
