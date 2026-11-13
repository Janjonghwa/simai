import type { EventEntry } from '@/features/events/entries';
import {
  endWork,
  fetchReportsByWork,
  putDailyWorkUnit,
  signReport,
  startWork,
  submitReport,
  WORK_ERROR,
} from '@/features/work/api';
import { RunStage } from '@/features/work/run/RunStage';
import { useOpenDay } from '@/features/work/useWorkDays';
import { useWorkActionState, WorkActionProvider } from '@/features/work/WorkActionContext';
import { ZONES_KEY } from '@/features/zones/api';
import { api } from '@/lib/api';
import { createTbmMock } from '@/mocks/tbm';
import { createWorkMock, type WorkMock } from '@/mocks/work';
import { MOCK_ZONES } from '@/mocks/zones';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => 'mock-token.admin' }) };
});

// 보고서 종이(B 일꾼 몫)는 받은 props 만 보이는 대역으로 바꾼다.
vi.mock('@/features/work/report/ReportPanel', () => ({
  ReportPanel: ({
    workId,
    editable,
    withPrint,
  }: {
    workId: string;
    editable: boolean;
    withPrint?: boolean;
  }) => (
    <div
      data-testid="report-panel"
      data-work={workId}
      data-editable={String(editable)}
      data-print={String(withPrint ?? false)}
    />
  ),
}));

// 오른쪽 카드가 읽는 관제 데이터는 대역으로 고정한다. 시계도 고정한다.
const T0 = new Date(2026, 10, 11, 9, 0).getTime();
const MIN = 60_000;
/** 서버(mock)가 종료 뒤 종료 미팅을 채우기까지(mock 기본 초안 지연과 같다). */
const ENDING_DELAY_MS = 2_500;
const TODAY = '2026-11-11';
const NOW = vi.hoisted(() => ({ value: 0 }));
const live = vi.hoisted(() => ({ entries: [] as EventEntry[] }));

vi.mock('@/features/layout/useNow', () => ({ useNow: () => NOW.value }));
vi.mock('@/features/events/useLiveEvents', () => ({
  useLiveEvents: () => ({ entries: live.entries, loaded: true, failed: false }),
}));
vi.mock('@/features/layout/useCurrentScene', () => ({
  useCurrentScene: () => ({
    scene: { ok: true, raw: {} },
    sceneStatus: null,
    onSceneStatusChange: () => undefined,
  }),
}));
vi.mock('@/features/zones/useZones', () => ({
  useZones: () => ({ zones: [{}, {}, {}], raw: [], failed: false }),
}));
vi.mock('@/features/cctv/useCameraStates', () => ({
  useCameraStates: () => ({
    cameras: [{ id: 'cam1' }, { id: 'cam2' }, { id: 'cam3' }, { id: 'cam4' }],
    fresh: true,
    stateOf: (id: string) => (id === 'cam4' ? 'down' : 'live'),
  }),
}));

const eventAt = (
  id: string,
  raisedAt: number,
  severity: 'CRITICAL' | 'HIGH' | 'CAUTION',
  zone: string,
) =>
  ({
    event: {
      event_id: id,
      rule_id: 'R2',
      source: 'LIVE',
      lifecycle: 'RAISED',
      verification: 'VERIFIED',
      severity,
      confidence: 'HIGH',
      needs_check: false,
      world_ids: [],
      zone_id: zone,
      metrics: {},
      rationale: null,
      proposed_severity: null,
      raised_at: raisedAt,
    },
    stageStartedAt: null,
  }) satisfies EventEntry;

let clock = T0;
let mock: WorkMock;
const server = setupServer();
let requests: string[] = [];

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  clock = T0;
  NOW.value = T0 + 65_000;
  requests = [];
  live.entries = [];
  mock = createWorkMock({ now: () => clock });
  server.resetHandlers(...mock.handlers, ...createTbmMock().handlers);
  server.events.on('request:start', ({ request }) => {
    requests.push(`${request.method} ${new URL(request.url).pathname}`);
  });
});
afterEach(() => {
  cleanup();
  server.events.removeAllListeners();
});

const png = () => new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' });

type Stage = 'running' | 'ended' | 'submitted';

/**
 * 오늘 작업을 만들고 서명·시작(·종료·제출)까지 진행해 두고 작업 id 를 돌려준다.
 * 'ended' 는 종료 직후라 서버가 종료 미팅을 아직 채우는 중이다(시계를 ENDING_DELAY_MS 만큼 보내야 채워진다).
 */
async function prepare(upTo: Stage): Promise<string> {
  const day = await putDailyWorkUnit(api, TODAY);
  mock.markSetupDone(day.work_id); // 작업 시작은 세팅 완료 뒤다(-642)
  await signReport(api, day.tbm_report?.report_id ?? 0, {
    image: png(),
    signerName: '박반장',
    reportVersion: 1,
  });
  await startWork(api, day.work_id);
  if (upTo === 'running') return day.work_id;
  clock = T0 + 65 * MIN;
  await endWork(api, day.work_id);
  if (upTo === 'ended') return day.work_id;
  clock += ENDING_DELAY_MS;
  const { latest } = await fetchReportsByWork(api, day.work_id);
  await submitReport(api, latest?.report_id ?? 0);
  return day.work_id;
}

/** WorkActionLayer 대역: 단계 화면이 등록한 단추 줄의 주 단추. */
function ActionButton() {
  const action = useWorkActionState();
  if (!action) return null;
  return (
    <button type="button" disabled={!action.enabled} onClick={action.onClick}>
      {action.label}
    </button>
  );
}

/** 날짜 화면 대역: 하루 작업을 받아 서버 진행이 단계 화면을 정하게 한다(종료·제출 뒤 다시 받아 모양이 바뀐다). */
function Harness({ onSubmitted }: { onSubmitted: () => void }) {
  const { data: day } = useOpenDay(TODAY, TODAY);
  if (!day) return null;
  return (
    <WorkActionProvider>
      <RunStage
        day={day}
        date={TODAY}
        todayKey={TODAY}
        onNext={() => undefined}
        onSubmitted={onSubmitted}
      />
      <ActionButton />
    </WorkActionProvider>
  );
}

function renderStage(onSubmitted: () => void = () => undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(ZONES_KEY, MOCK_ZONES);
  render(
    <QueryClientProvider client={client}>
      <Harness onSubmitted={onSubmitted} />
    </QueryClientProvider>,
  );
}

const putForms = () => requests.filter((r) => /^PUT \/api\/reports\/tbm\/\d+\/form$/.test(r));
const reportPanel = () => screen.getByTestId('report-panel');
const doneButton = () => screen.getByRole('button', { name: '완료' }) as HTMLButtonElement;

describe('RunStage — 진행 중', () => {
  it('왼쪽에 고칠 수 있는 보고서, 오른쪽에 작업 현황·사건·관제 적용 상태를 보인다', async () => {
    const workId = await prepare('running');
    live.entries = [
      eventAt('before', T0 - MIN, 'CRITICAL', 'z-04'),
      eventAt('first', T0 + 10_000, 'CRITICAL', 'z-04'),
      eventAt('second', T0 + 20_000, 'HIGH', 'z-02'),
      eventAt('caution', T0 + 30_000, 'CAUTION', 'z-02'),
    ];
    renderStage();

    expect(await screen.findByRole('heading', { name: '작업 현황' })).toBeTruthy();
    expect(reportPanel().dataset).toMatchObject({
      work: workId,
      editable: 'true',
      print: 'true',
    });
    expect(screen.getByText('진행 중')).toBeTruthy();
    expect(screen.getByRole('timer', { name: '경과 시간' }).textContent).toBe('00:01:05');
    expect(screen.getByText('시작 시각 2026-11-11 09:00')).toBeTruthy();
    await waitFor(() => expect(screen.getByText('서명 1명')).toBeTruthy());

    const events = screen.getByRole('list', { name: '사건 목록' });
    expect(screen.getByText('2건')).toBeTruthy();
    expect(
      within(events)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual([expect.stringContaining('단상 구역'), expect.stringContaining('개구부 구역')]);

    expect(screen.getByRole('heading', { name: '관제 적용 상태' })).toBeTruthy();
    expect(screen.getByText('3D 모델 적용됨')).toBeTruthy();
    expect(screen.getByText('구역 3개 적용됨')).toBeTruthy();
    expect(screen.getByText('카메라 3/4 연결')).toBeTruthy();
    expect((screen.getByRole('button', { name: '다음' }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it('사건이 없으면 `사건 없음` 과 `0건` 을 보인다', async () => {
    await prepare('running');
    renderStage();
    expect(await screen.findByText('사건 없음')).toBeTruthy();
    expect(screen.getByText('0건')).toBeTruthy();
  });

  it('`다음` 은 종료 확인창을 열고, 취소하면 종료하지 않으며, 종료하면 종료 모양(`완료`)으로 바뀐다', async () => {
    await prepare('running');
    renderStage();
    fireEvent.click(await screen.findByRole('button', { name: '다음' }));

    const dialog = screen.getByRole('dialog', { name: '작업을 종료할까요?' });
    fireEvent.click(within(dialog).getByRole('button', { name: '취소' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(requests.some((r) => r.endsWith('/end'))).toBe(false);

    clock = T0 + 65 * MIN;
    fireEvent.click(screen.getByRole('button', { name: '다음' }));
    fireEvent.click(
      within(screen.getByRole('dialog', { name: '작업을 종료할까요?' })).getByRole('button', {
        name: '종료',
      }),
    );

    expect(await screen.findByRole('button', { name: '완료' })).toBeTruthy();
    expect(requests.filter((r) => r.endsWith('/end'))).toHaveLength(1);
    expect(screen.queryByRole('heading', { name: '작업 현황' })).toBeNull();
    expect(reportPanel().dataset.editable).toBe('true');
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('종료가 실패하면 확인창에 안내를 보이고 창은 그대로 둔다', async () => {
    await prepare('running');
    server.use(
      http.post('*/api/work-units/:id/end', () =>
        HttpResponse.json({ code: WORK_ERROR.INVALID_STATE, message: '안 됨' }, { status: 409 }),
      ),
    );
    renderStage();
    fireEvent.click(await screen.findByRole('button', { name: '다음' }));
    fireEvent.click(
      within(screen.getByRole('dialog', { name: '작업을 종료할까요?' })).getByRole('button', {
        name: '종료',
      }),
    );

    expect((await screen.findByRole('alert')).textContent).toBe(
      '작업을 종료하지 못했습니다. 다시 시도해 주세요.',
    );
    expect(screen.getByRole('dialog', { name: '작업을 종료할까요?' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: '작업 현황' })).toBeTruthy();
  });
});

describe('RunStage — 종료', () => {
  it('서버가 종료 미팅을 채우는 동안 `완료` 는 꺼 두고, 다 채우면 켜며, 화면은 양식을 저장하지 않는다', async () => {
    const workId = await prepare('ended');
    mock.setEventCounts(workId, { CRITICAL: 1, HIGH: 2 });
    renderStage();
    expect(await screen.findByRole('button', { name: '완료' })).toBeTruthy();
    expect(reportPanel().dataset).toMatchObject({ work: workId, editable: 'true', print: 'true' });
    expect(screen.queryByRole('heading', { name: '작업 현황' })).toBeNull();
    // 보고서를 받았지만 서버가 아직 채우는 중이다.
    await waitFor(() => expect(requests).toContain('GET /api/reports/tbm'));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(doneButton().disabled).toBe(true);

    // 서버가 다 채운 뒤 보고서를 다시 받으면(2초마다) 켜진다.
    clock += ENDING_DELAY_MS;
    await waitFor(() => expect(doneButton().disabled).toBe(false), { timeout: 5_000 });
    const { latest } = await fetchReportsByWork(api, workId);
    expect(latest?.wrap_up_status).toBe('SUCCEEDED');
    expect(latest?.form?.review.ending).toBe('작업 종료 · 총 작업 시간 01:05:00, 사건 3건');
    expect(putForms()).toEqual([]);
  });

  it.each(['SKIPPED', 'FAILED'])(
    '서버가 종료 미팅을 채우지 못했으면(%s) 비어 있어도 `완료` 가 켜지고 화면이 채워 저장하지 않는다',
    async (wrapUpStatus) => {
      const workId = await prepare('ended');
      const { latest } = await fetchReportsByWork(api, workId);
      server.use(
        http.get('*/api/reports/tbm', () =>
          HttpResponse.json({ latest: { ...latest, wrap_up_status: wrapUpStatus }, versions: [] }),
        ),
      );
      renderStage();
      await waitFor(() => expect(doneButton().disabled).toBe(false));
      expect(latest?.form?.review.ending).toBe('');
      await new Promise((resolve) => setTimeout(resolve, 100));
      expect(putForms()).toEqual([]);
    },
  );

  it('`완료` 는 보낼지 묻고, 취소하면 보내지 않으며, 보내면 제출하고 onSubmitted 를 부른다', async () => {
    await prepare('ended');
    clock += ENDING_DELAY_MS;
    const onSubmitted = vi.fn();
    renderStage(onSubmitted);
    await waitFor(() =>
      expect((screen.getByRole('button', { name: '완료' }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    fireEvent.click(screen.getByRole('button', { name: '완료' }));
    const dialog = screen.getByRole('dialog', { name: '보고서를 보낼까요?' });
    expect(within(dialog).getByText('보낸 뒤에는 고칠 수 없습니다.')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: '취소' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(requests.some((r) => r.endsWith('/submit'))).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: '완료' }));
    fireEvent.click(
      within(screen.getByRole('dialog', { name: '보고서를 보낼까요?' })).getByRole('button', {
        name: '보내기',
      }),
    );

    await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(1));
    expect(requests.filter((r) => r.endsWith('/submit'))).toHaveLength(1);
    // 서버 진행이 SUBMITTED 로 바뀌면 이 단계는 비고(날짜 화면이 다시 보기 화면을 보인다) `완료` 도 사라진다.
    await waitFor(() => expect(screen.queryByRole('button', { name: '완료' })).toBeNull());
  });

  it.each([
    [null, 'AI 초안을 만들지 않았습니다. 위험·대책·점검 칸이 비어 있을 수 있습니다.'],
    ['FAILED', 'AI 초안을 만들지 못했습니다. 위험·대책·점검 칸이 비어 있을 수 있습니다.'],
    ['SUCCEEDED', null],
  ] as const)(
    '보낼지 묻는 창은 AI 초안(%s)이 끝나지 않았으면 경고하되 보내기는 막지 않는다(-625)',
    async (draftStatus, warning) => {
      const workId = await prepare('ended');
      const { latest } = await fetchReportsByWork(api, workId);
      server.use(
        http.get('*/api/reports/tbm', () =>
          HttpResponse.json({
            latest: {
              ...latest,
              status: draftStatus ?? 'PENDING',
              draft_status: draftStatus,
              wrap_up_status: 'SUCCEEDED',
            },
            versions: [],
          }),
        ),
      );
      renderStage();
      await waitFor(() => expect(doneButton().disabled).toBe(false));
      fireEvent.click(doneButton());
      const dialog = screen.getByRole('dialog', { name: '보고서를 보낼까요?' });
      if (warning) {
        expect(within(dialog).getByText(warning)).toBeTruthy();
      } else {
        expect(within(dialog).queryByText(/AI 초안을/)).toBeNull();
      }
      expect(
        (within(dialog).getByRole('button', { name: '보내기' }) as HTMLButtonElement).disabled,
      ).toBe(false);
    },
  );

  it('AI 초안을 쓰는 중(status GENERATING)이면 종료 미팅이 끝났어도 `완료` 는 꺼 두고, 다 쓰면 켠다', async () => {
    const workId = await prepare('ended');
    clock += ENDING_DELAY_MS;
    const { latest } = await fetchReportsByWork(api, workId);
    expect(latest?.wrap_up_status).toBe('SUCCEEDED');
    let drafting = true;
    server.use(
      http.get('*/api/reports/tbm', () => {
        const status = drafting ? 'GENERATING' : 'SUCCEEDED';
        return HttpResponse.json({
          latest: { ...latest, status, draft_status: status },
          versions: [],
        });
      }),
    );
    const reportGets = () => requests.filter((r) => r === 'GET /api/reports/tbm').length;
    const before = reportGets();
    renderStage();
    await waitFor(() => expect(reportGets()).toBeGreaterThan(before));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(doneButton().disabled).toBe(true);

    // 서버가 초안을 다 쓴 뒤 보고서를 다시 받으면(2초마다) 켜진다.
    drafting = false;
    await waitFor(() => expect(doneButton().disabled).toBe(false), { timeout: 5_000 });
  });

  it.each([
    [WORK_ERROR.REPORT_NOT_READY, '보고서를 쓰는 중이에요. 잠시 뒤 다시 보내 주세요'],
    [WORK_ERROR.REPORT_LOCKED, '이미 제출한 보고서예요'],
    [WORK_ERROR.INVALID_STATE, '작업이 아직 끝나지 않았어요'],
    [WORK_ERROR.REPORT_VERSION_STALE, '보고서를 보내지 못했습니다. 다시 시도해 주세요.'],
  ])(
    '제출이 %s 로 실패하면 확인창에 그 안내를 보이고 onSubmitted 는 부르지 않는다',
    async (code, message) => {
      await prepare('ended');
      clock += ENDING_DELAY_MS;
      server.use(
        http.post('*/api/reports/tbm/:id/submit', () =>
          HttpResponse.json({ code, message: '안 됨' }, { status: 409 }),
        ),
      );
      const onSubmitted = vi.fn();
      renderStage(onSubmitted);
      await waitFor(() =>
        expect((screen.getByRole('button', { name: '완료' }) as HTMLButtonElement).disabled).toBe(
          false,
        ),
      );
      fireEvent.click(screen.getByRole('button', { name: '완료' }));
      fireEvent.click(
        within(screen.getByRole('dialog', { name: '보고서를 보낼까요?' })).getByRole('button', {
          name: '보내기',
        }),
      );

      expect((await screen.findByRole('alert')).textContent).toBe(message);
      expect(onSubmitted).not.toHaveBeenCalled();
    },
  );
});
