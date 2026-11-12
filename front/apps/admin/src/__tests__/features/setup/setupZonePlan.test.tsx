import { createSetupSession, requestSetupZonePlan } from '@/features/setup/api';
import {
  applyCommitToPlan,
  liveZonesAsItems,
  planZonesView,
  SETUP_NEW_ZONE_PREFIX,
  SetupZonePlanEditError,
  type SetupZonePlanView,
} from '@/features/setup/setupZonePlan';
import { SetupZonePlanCard } from '@/features/setup/SetupZonePlanCard';
import type { SetupSessionResponse } from '@/features/setup/types';
import { useSetupSession } from '@/features/setup/useSetupStep1';
import { commitErrorText } from '@/features/zones/commitErrors';
import { useZoneCommitMutation } from '@/features/zones/useZoneCommitMutation';
import { useZones } from '@/features/zones/useZones';
import { bodyOf, formOf } from '@/features/zones/zoneForm';
import { readZones } from '@/features/zones/zones';
import { ZoneSourceContext } from '@/features/zones/zoneSource';
import { api } from '@/lib/api';
import { createSetupMock } from '@/mocks/setup';
import { createZonesMock, MOCK_ZONES } from '@/mocks/zones';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from '@testing-library/react';
import { setupServer } from 'msw/node';
import type { ReactNode } from 'react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

// 세팅 구역 단계의 구역 저장(S15P31A101-615): 관제 구역(zones/commit) 대신 세션 구역 목록(-612 PUT)에 저장하고, 화면 목록도
// 그 목록 기준이다. 이전 모델 재사용은 지금 관제 구역으로 채우고, 새 모델은 같은 World 일 때만 불러온다. 충돌(revision)은 다시 받는다.

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => 'mock-token.admin' }) };
});

const server = setupServer();
let setupMock: ReturnType<typeof createSetupMock>;
let requests: string[] = [];

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  setupMock = createSetupMock({ now: () => 1_800_000_000_000 });
  server.resetHandlers(...createZonesMock().handlers, ...setupMock.handlers);
  requests = [];
  server.events.on('request:start', ({ request }) => {
    requests.push(`${request.method} ${new URL(request.url).pathname}`);
  });
});
afterEach(() => {
  cleanup();
  server.events.removeAllListeners();
});

const z04 = readZones(MOCK_ZONES)[0]!;
const body = (name: string) => bodyOf(z04, { ...formOf(z04), name }, null);
const emptyPlan: SetupZonePlanView = { setup_session_id: 's', items: [], revision: 0 };

async function readySession(patch: Partial<SetupSessionResponse> = {}): Promise<string> {
  const created = await createSetupSession(api, {
    work_unit_id: 1,
    camera_heights_m: { cam1: 2.4 },
  });
  setupMock.updateSession(created.setup_session_id, {
    status: 'READY_FOR_ZONES',
    stage: 'READY_FOR_ZONES',
    preview_scene_version: 'scene-preview-1',
    calibration_version: 'cal-1',
    ...patch,
  });
  return created.setup_session_id;
}

function wrapperFor(sessionId: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ZoneSourceContext value={{ sessionId }}>{children}</ZoneSourceContext>
    </QueryClientProvider>
  );
}

describe('목록 계산', () => {
  it('만들기는 새 항목, 새 버전은 본문 바꾸기, 보관은 항목 빼기이고 화면 ID 는 이어 쓰는 zone_id 또는 임시 ID 다', () => {
    const created = applyCommitToPlan(
      emptyPlan,
      MOCK_ZONES,
      { zone: { op: 'CREATE', body: body('새 구역') } },
      {
        source: 'AI',
        candidateId: 'cand-1',
      },
    );
    expect(created.items).toEqual([
      { zone_id: null, source: 'AI', candidate_id: 'cand-1', zone: body('새 구역'), surface: null },
    ]);
    expect(created.result.zone_id).toBe(`${SETUP_NEW_ZONE_PREFIX}0`);
    expect(created.result.zone?.name).toBe('새 구역');

    const plan: SetupZonePlanView = {
      ...emptyPlan,
      items: [...liveZonesAsItems(MOCK_ZONES), ...created.items],
    };
    const updated = applyCommitToPlan(plan, MOCK_ZONES, {
      zone: { op: 'UPDATE', zone_id: 'z-04', body: body('바꾼 이름') },
    });
    expect(updated.items.find((i) => i.zone_id === 'z-04')?.zone?.name).toBe('바꾼 이름');
    const view = planZonesView({ ...plan, items: updated.items }, MOCK_ZONES);
    expect(view.find((z) => z.zone_id === 'z-04')).toMatchObject({
      name: '바꾼 이름',
      zone_version: 1,
    });
    expect(view).toHaveLength(plan.items.length);

    const newId = `${SETUP_NEW_ZONE_PREFIX}${plan.items.length - 1}`;
    const archived = applyCommitToPlan(plan, MOCK_ZONES, {
      zone: { op: 'ARCHIVE', zone_id: newId },
    });
    expect(archived.items).toHaveLength(plan.items.length - 1);
    expect(() =>
      applyCommitToPlan(emptyPlan, MOCK_ZONES, { zone: { op: 'ARCHIVE', zone_id: 'z-none' } }),
    ).toThrow(SetupZonePlanEditError);
  });

  it('지금 관제 구역을 가져올 때 이미 목록에 있는 구역은 빼고 본문 없이(그대로 두기) 넣는다', () => {
    const items = liveZonesAsItems(MOCK_ZONES, [
      { zone_id: 'z-04', source: 'MANUAL', candidate_id: null, zone: null, surface: null },
    ]);
    expect(items.some((i) => i.zone_id === 'z-04')).toBe(false);
    expect(items.every((i) => i.zone === null && i.source === 'MANUAL')).toBe(true);
  });
});

describe('세팅 구역 단계의 저장', () => {
  it('구역 저장은 세션 구역 목록 PUT 으로 가고 zones/commit 은 부르지 않으며 화면 목록은 그 목록을 따른다', async () => {
    const sessionId = await readySession();
    const { result } = renderHook(() => ({ commit: useZoneCommitMutation(), zones: useZones() }), {
      wrapper: wrapperFor(sessionId),
    });
    await waitFor(() => expect(result.current.zones.raw).toEqual([]));

    let done: Awaited<ReturnType<typeof result.current.commit.mutateAsync>> | undefined;
    await act(async () => {
      done = await result.current.commit.mutateAsync({
        request: { zone: { op: 'CREATE', body: body('세팅 구역') } },
        zone: null,
        linked: null,
      });
    });
    expect(done?.publishError).toBeNull();
    expect(done?.result.zone_id).toBe(`${SETUP_NEW_ZONE_PREFIX}0`);
    expect(requests).toContain(`PUT /api/setup-sessions/${sessionId}/zones`);
    expect(requests.some((r) => r.endsWith('/api/zones/commit'))).toBe(false);
    expect(setupMock.zonePlans.get(sessionId)).toMatchObject({ revision: 1 });
    await waitFor(() =>
      expect(result.current.zones.zones.map((z) => z.name)).toEqual(['세팅 구역']),
    );
  });

  it('다른 곳에서 먼저 고쳤으면(revision) 409 를 안내하고 목록을 다시 받아 다음 저장은 새 revision 으로 간다', async () => {
    const sessionId = await readySession();
    const { result } = renderHook(() => ({ commit: useZoneCommitMutation(), zones: useZones() }), {
      wrapper: wrapperFor(sessionId),
    });
    await waitFor(() => expect(result.current.zones.raw).toEqual([]));
    // 다른 PC 가 먼저 저장했다
    setupMock.zonePlans.set(sessionId, {
      items: [{ zone_id: 'z-04', source: 'MANUAL', candidate_id: null, zone: null, surface: null }],
      revision: 3,
      updatedAt: 0,
    });
    let failure: unknown;
    await act(async () => {
      await result.current.commit
        .mutateAsync({
          request: { zone: { op: 'CREATE', body: body('늦은 저장') } },
          zone: null,
          linked: null,
        })
        .catch((e: unknown) => (failure = e));
    });
    expect(commitErrorText(failure)).toContain('다른 곳에서 먼저 고친 세팅 구역 목록');
    await waitFor(() => expect(result.current.zones.zones.map((z) => z.zoneId)).toEqual(['z-04']));

    await act(async () => {
      await result.current.commit.mutateAsync({
        request: { zone: { op: 'CREATE', body: body('다시 저장') } },
        zone: null,
        linked: null,
      });
    });
    expect(setupMock.zonePlans.get(sessionId)?.revision).toBe(4);
  });

  it('완료 뒤 저장은 409 SETUP_ZONES_CLOSED 로 막힌다', async () => {
    const sessionId = await readySession({ setup_completed_at_ms: 1 });
    const { result } = renderHook(() => useZoneCommitMutation(), {
      wrapper: wrapperFor(sessionId),
    });
    let failure: unknown;
    await act(async () => {
      await result.current
        .mutateAsync({
          request: { zone: { op: 'CREATE', body: body('닫힘') } },
          zone: null,
          linked: null,
        })
        .catch((e: unknown) => (failure = e));
    });
    expect(commitErrorText(failure)).toContain('더 고칠 수 없습니다');
  });
});

describe('세팅 구역 목록 카드', () => {
  function showCard(sessionId: string) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    function Harness() {
      const session = useSetupSession(sessionId).data;
      return session ? <SetupZonePlanCard session={session} /> : null;
    }
    render(
      <QueryClientProvider client={client}>
        <Harness />
      </QueryClientProvider>,
    );
  }
  const liveIds = () => readZones(MOCK_ZONES).map((z) => z.zoneId);

  it('이전 모델 재사용은 빈 목록을 지금 관제 구역(zone_id 유지)으로 한 번 채운다', async () => {
    const sessionId = await readySession({ mode: 'REUSE' });
    showCard(sessionId);
    await waitFor(() => expect(setupMock.zonePlans.get(sessionId)?.revision).toBe(1));
    const plan = await requestSetupZonePlan(api, sessionId);
    expect(plan.items.map((i) => i.zone_id)).toEqual(liveIds());
    expect(plan.items.every((i) => i.zone === null)).toBe(true);
    expect(
      await screen.findByText(
        `구역 ${liveIds().length}개 · 세팅을 완료하면 이 목록이 관제 구역이 됩니다`,
      ),
    ).toBeTruthy();
    expect(screen.queryByRole('button', { name: '지금 구역 불러오기' })).toBeNull();
  });

  it('새 모델은 같은 World(world_anchor.accepted)일 때만 「지금 구역 불러오기」로 지금 구역을 더한다', async () => {
    const sessionId = await readySession({ mode: 'NEW', world_anchor: { accepted: true } });
    showCard(sessionId);
    expect(setupMock.zonePlans.get(sessionId)).toBeUndefined();
    const button = (await screen.findByRole('button', {
      name: '지금 구역 불러오기',
    })) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() => expect(setupMock.zonePlans.get(sessionId)?.revision).toBe(1));
    expect((await requestSetupZonePlan(api, sessionId)).items.map((i) => i.zone_id)).toEqual(
      liveIds(),
    );
  });

  it('새 모델인데 World 가 새로 정해졌으면 불러오기를 끄고 새로 그리라고 안내한다', async () => {
    const sessionId = await readySession({ mode: 'NEW', world_anchor: { accepted: false } });
    showCard(sessionId);
    expect(await screen.findByText(/지금 관제 구역을 불러오지 않습니다/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: '지금 구역 불러오기' })).toBeNull();
    expect(setupMock.zonePlans.get(sessionId)).toBeUndefined();
  });
});
