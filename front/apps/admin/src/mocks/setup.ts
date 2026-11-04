import type {
  CameraReceptionStatus,
  Connectivity,
  CoverageGuideItem,
  CoverageGuidePayload,
  CoveragePairItem,
  CoverageRelayStatus,
  CreateSetupSessionBody,
  SetupSessionResponse,
} from '@/features/setup/types';
import { problem, readLiveUser, unauthorized } from '@/mocks/problem';
import { http, HttpResponse } from 'msw/http';

// 현장 세팅 1단계(GET /api/cameras/coverage-guides, GET /api/setup-sessions/camera-height-defaults,
// POST /api/setup-sessions, GET /api/setup-sessions/:id) MSW 핸들러(S15P31A101-447·-452·-448).

const DEFAULT_SITE_ID = 'classroom-1';
const DEFAULT_TOLERANCE_CM = 5.0;
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
  const sessions = new Map<string, SetupSessionResponse>();
  let seq = 0;

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
        zone_drafts_ref: null,
        failure_code: null,
        failure_message: null,
        requested_by: 1,
        started_at_ms: t,
        ready_at_ms: null,
        completed_at_ms: null,
        updated_at_ms: t,
      };
      sessions.set(sessionId, session);
      return HttpResponse.json(session, { status: 202 });
    }),

    http.get('*/api/setup-sessions/:id', ({ request, params }) => {
      const t = now();
      if (!readLiveUser(request, t)) return unauthorized();
      const id = String(params.id ?? '');
      const found = sessions.get(id);
      if (!found) {
        return problem(404, 'SETUP_SESSION_NOT_FOUND', '세팅 세션을 찾을 수 없습니다');
      }
      return HttpResponse.json(found);
    }),
  ];

  return {
    handlers,
    state,
    submittedBodies,
    sessions,
  };
}
