import type { SceneRegenerateStatus, SceneRegenerateView } from '@/features/layout/api';
import { problem, readLiveUser, unauthorized } from '@/mocks/problem';
import { http, HttpResponse } from 'msw/http';

// 개발 하네스와 같은 자산만 재사용한다. 뷰어 내부 코드는 import 하지 않는다.
const demoSceneUrl = new URL(
  '../features/viewer3d/dev-harness/fixtures/scene-demo.gltf',
  import.meta.url,
).href;

// 서버가 주는 서명 URL 의 유효 시간.
const SIGNED_URL_TTL_MS = 5 * 60_000;

interface MockSceneState {
  currentSceneVersion: string;
  isDefault: boolean;
  committedAtMs: number | null;
  nextRequestId: number;
  requests: Map<number, SceneRegenerateView>;
  autoAdvanceOnPoll: boolean;
}

const state: MockSceneState = {
  currentSceneVersion: 'scene-demo',
  isDefault: true,
  committedAtMs: null,
  nextRequestId: 1,
  requests: new Map(),
  autoAdvanceOnPoll: false,
};

export function resetMockSceneState(options?: { autoAdvanceOnPoll?: boolean }): void {
  state.currentSceneVersion = 'scene-demo';
  state.isDefault = true;
  state.committedAtMs = null;
  state.nextRequestId = 1;
  state.requests.clear();
  state.autoAdvanceOnPoll = options?.autoAdvanceOnPoll ?? false;
}

export function setMockSceneRegenerateView(view: SceneRegenerateView): void {
  state.requests.set(view.request_id, { ...view });
  if (view.request_id >= state.nextRequestId) {
    state.nextRequestId = view.request_id + 1;
  }
}

function isInProgress(status: SceneRegenerateStatus): boolean {
  return status === 'REQUESTED' || status === 'CAPTURING' || status === 'RECONSTRUCTING';
}

function advanceIfEnabled(item: SceneRegenerateView): SceneRegenerateView {
  if (!state.autoAdvanceOnPoll) return item;
  const now = Date.now();
  let next: SceneRegenerateView = item;
  if (item.status === 'REQUESTED') {
    next = { ...item, status: 'CAPTURING', updated_at_ms: now };
  } else if (item.status === 'CAPTURING') {
    next = { ...item, status: 'RECONSTRUCTING', updated_at_ms: now };
  } else if (item.status === 'RECONSTRUCTING') {
    next = {
      ...item,
      status: 'CANDIDATE_READY',
      scene_version: `scene-cand-${item.request_id}`,
      updated_at_ms: now,
    };
  }
  state.requests.set(item.request_id, next);
  return next;
}

export const sceneHandlers = [
  http.get('*/api/scenes/current', ({ request }) => {
    const now = Date.now();
    if (!readLiveUser(request, now)) return unauthorized();
    return HttpResponse.json({
      scene_version: state.currentSceneVersion,
      state: state.isDefault ? 'DEFAULT' : 'COMMITTED',
      default: state.isDefault,
      calibration_version: state.isDefault ? null : 'cal-mock-1',
      committed_at_ms: state.committedAtMs,
      quality: null,
      gltf_url: demoSceneUrl,
      gltf_expires_at_ms: now + SIGNED_URL_TTL_MS,
    });
  }),

  http.post('*/api/scenes/regenerate', async ({ request }) => {
    const now = Date.now();
    const user = readLiveUser(request, now);
    if (!user) return unauthorized();
    if (user.role !== 'ADMIN') {
      return problem(403, 'FORBIDDEN', '관리자만 3D 배경 재생성을 요청할 수 있습니다.');
    }

    let reason: string | null = null;
    const rawText = await request.text();
    if (rawText.trim()) {
      try {
        const parsed: unknown = JSON.parse(rawText);
        if (typeof parsed !== 'object' || parsed === null) {
          return problem(400, 'INVALID_REGENERATE_REQUEST', '요청 형식이 올바르지 않습니다.');
        }
        const r: unknown = Reflect.get(parsed, 'reason');
        if (r !== undefined && r !== null) {
          if (typeof r !== 'string' || r.trim().length > 120) {
            return problem(
              400,
              'INVALID_REGENERATE_REQUEST',
              '재생성 사유는 120자 이하 문자열이어야 합니다.',
            );
          }
          reason = r.trim() || null;
        }
      } catch {
        return problem(400, 'INVALID_REGENERATE_REQUEST', '요청 형식이 올바르지 않습니다.');
      }
    }

    for (const existing of state.requests.values()) {
      if (isInProgress(existing.status)) {
        return HttpResponse.json(
          {
            status: 409,
            code: 'REGENERATE_IN_PROGRESS',
            detail: '이미 진행 중인 3D 배경 재생성 요청이 있습니다.',
            request_id: existing.request_id,
          },
          { status: 409 },
        );
      }
    }

    const requestId = state.nextRequestId++;
    const created: SceneRegenerateView = {
      request_id: requestId,
      status: 'REQUESTED',
      failure_reason: null,
      scene_version: null,
      reason,
      requested_at_ms: now,
      updated_at_ms: now,
    };
    state.requests.set(requestId, created);

    return HttpResponse.json(
      {
        request_id: requestId,
        status: 'REQUESTED',
      },
      { status: 202 },
    );
  }),

  http.get('*/api/scenes/regenerate/:requestId', ({ request, params }) => {
    const now = Date.now();
    if (!readLiveUser(request, now)) return unauthorized();
    const id = Number(params.requestId);
    const found = Number.isInteger(id) ? state.requests.get(id) : undefined;
    if (!found) {
      return problem(404, 'REGENERATE_NOT_FOUND', '3D 배경 재생성 요청을 찾을 수 없습니다.');
    }
    return HttpResponse.json(advanceIfEnabled(found));
  }),

  http.get('*/api/scenes/regenerate', ({ request }) => {
    const now = Date.now();
    if (!readLiveUser(request, now)) return unauthorized();
    let latest: SceneRegenerateView | null = null;
    for (const item of state.requests.values()) {
      if (!latest || item.request_id > latest.request_id) {
        latest = item;
      }
    }
    if (!latest) {
      return new HttpResponse(null, { status: 204 });
    }
    return HttpResponse.json(latest);
  }),

  http.post('*/api/scenes/:sceneVersion/commit', ({ request, params }) => {
    const now = Date.now();
    const user = readLiveUser(request, now);
    if (!user) return unauthorized();
    if (user.role !== 'ADMIN') {
      return problem(403, 'FORBIDDEN', '관리자만 Scene 을 활성화할 수 있습니다.');
    }
    const version = String(params.sceneVersion ?? '');
    if (!version || version === 'current' || version === 'regenerate') {
      return problem(400, 'INVALID_SCENE', '올바르지 않은 Scene 버전입니다.');
    }
    state.currentSceneVersion = version;
    state.isDefault = false;
    state.committedAtMs = now;
    return HttpResponse.json({
      scene_version: version,
      state: 'COMMITTED',
      committed_at_ms: now,
    });
  }),
];
