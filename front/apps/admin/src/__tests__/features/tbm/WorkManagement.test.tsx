import { resetSetupFlowStore, useSetupFlowStore } from '@/features/setup/setup.store';
import type { SetupSessionResponse } from '@/features/setup/types';
import { TaskList } from '@/features/tbm/TaskList';
import { TBM_TASKS } from '@/features/tbm/tbmDemo';
import { TbmPage } from '@/features/tbm/TbmPage';
import { deriveWorkStages, type WorkStageInput } from '@/features/tbm/workStage';
import { WorkStageBar } from '@/features/tbm/WorkStageBar';
import { MOCK_STATIC_OBJECTS } from '@/mocks/staticObjects';
import { createTbmMock } from '@/mocks/tbm';
import { MOCK_ZONES } from '@/mocks/zones';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

// 작업 관리(/work) 단계 표시줄. API 주소는 존재하지 않는 도메인(.invalid)이라 새는 연결이 DNS 에서 끝난다.
vi.mock('@/lib/env', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/env')>();
  return {
    ...actual,
    env: {
      ...actual.env,
      VITE_API_BASE_URL: 'https://work-test.invalid',
      VITE_STOMP_URL: 'wss://work-test.invalid/ws',
    },
  };
});
vi.mock('@/features/viewer3d', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/viewer3d')>();
  const { forwardRef } = await import('react');
  return {
    ...actual,
    Viewer3D: forwardRef<unknown>(function Viewer3DStub(_props, ref) {
      void ref;
      return <div data-testid="viewer3d-stub" />;
    }),
  };
});

// ── 단계 판정(순수 함수) ─────────────────────────────────────────────────────

const SESSION = (patch: Partial<SetupSessionResponse>): SetupSessionResponse => ({
  setup_session_id: 'ss-1',
  site_id: 'site-1',
  work_unit_id: 1,
  retry_of: null,
  job_id: null,
  status: 'READY_FOR_ZONES',
  progress_pct: 100,
  camera_heights_m: { cam1: 2.2 },
  height_warnings: [],
  height_checks: null,
  calibration_version: null,
  preview_scene_version: null,
  scene_version: null,
  config_version: null,
  zones_committed_at: null,
  zone_drafts_ref: null,
  failure_code: null,
  failure_message: null,
  ...patch,
});

const BASE: WorkStageInput = {
  task: { status: 'PLANNED', actualStartedAt: null, actualEndedAt: null },
  setup: { kind: 'none' },
  zonesCommittedHere: false,
  report: null,
  reportKnown: true,
};

const states = (input: WorkStageInput) =>
  Object.fromEntries(deriveWorkStages(input).stages.map((s) => [s.key, s.state]));

describe('단계 판정', () => {
  it('세팅 세션이 없으면 세팅이 지금 단계이고 구역은 세팅 먼저다', () => {
    const result = deriveWorkStages(BASE);
    expect(result.current).toBe('setup');
    expect(states(BASE)).toMatchObject({
      setup: 'todo',
      zones: 'todo',
      tbm: 'todo',
      start: 'todo',
    });
  });

  it('세팅 진행 중·실패·끝남을 세션 status 로 가른다', () => {
    const run = deriveWorkStages({
      ...BASE,
      setup: {
        kind: 'ready',
        session: SESSION({ status: 'JOINT_BA_AND_VERIFY', progress_pct: 40 }),
      },
    });
    expect(run.stages[0]).toMatchObject({ state: 'running', detail: '세팅 진행 중 40%' });
    expect(run.current).toBe('setup');

    const failed = deriveWorkStages({
      ...BASE,
      setup: { kind: 'ready', session: SESSION({ status: 'FAILED' }) },
    });
    expect(failed.stages[0]!.state).toBe('failed');

    const done = deriveWorkStages({
      ...BASE,
      setup: { kind: 'ready', session: SESSION({ status: 'READY_FOR_ZONES' }) },
    });
    expect(done.stages[0]!.state).toBe('done');
  });

  it('세팅이 끝났는데 구역 발행(zones_committed_at)이 없으면 아직으로 본다(세션 config_version 은 근거가 아니다)', () => {
    const result = deriveWorkStages({
      ...BASE,
      setup: {
        kind: 'ready',
        session: SESSION({ status: 'COMPLETED', config_version: 'cfg-setup' }),
      },
    });
    expect(result.stages[1]).toMatchObject({ key: 'zones', state: 'todo' });
    expect(result.stages[1]!.detail).toContain('구역 발행 전');
    expect(result.current).toBe('zones');
  });

  it('세션의 zones_committed_at 이나 이 브라우저의 발행 기록이 있으면 구역을 끝남으로 본다', () => {
    const withConfig = states({
      ...BASE,
      setup: { kind: 'ready', session: SESSION({ zones_committed_at: 1_000 }) },
    });
    expect(withConfig.zones).toBe('done');
    const here = states({
      ...BASE,
      setup: { kind: 'ready', session: SESSION({}) },
      zonesCommittedHere: true,
    });
    expect(here.zones).toBe('done');
  });

  it('세션 조회가 실패하면 세팅·구역은 확인 불가다', () => {
    const s = states({ ...BASE, setup: { kind: 'error' } });
    expect(s).toMatchObject({ setup: 'unknown', zones: 'unknown' });
  });

  it('보고서가 있으면 TBM 이 끝남이고 지금 단계는 시작이다', () => {
    const result = deriveWorkStages({
      ...BASE,
      setup: { kind: 'ready', session: SESSION({ zones_committed_at: 1_000 }) },
      report: { status: 'SUCCEEDED', approved_at: null, signature_count: 0 },
    });
    expect(result.current).toBe('start');
    expect(result.stages[2]!.state).toBe('done');
  });

  it('보고서 생성 중·실패·조회 전을 가른다', () => {
    const base = {
      ...BASE,
      setup: { kind: 'ready', session: SESSION({ zones_committed_at: 1_000 }) } as const,
    };
    expect(states({ ...base, report: { status: 'GENERATING' } }).tbm).toBe('running');
    expect(states({ ...base, report: { status: 'FAILED' } }).tbm).toBe('failed');
    expect(states({ ...base, reportKnown: false }).tbm).toBe('unknown');
  });

  it('진행 중이면 종료가 지금 단계이고, 종료 뒤에는 결재 전까지 마무리·서명이다', () => {
    const report = { status: 'SUCCEEDED', approved_at: null, signature_count: 2 } as const;
    const running = deriveWorkStages({
      ...BASE,
      task: { status: 'IN_PROGRESS', actualStartedAt: 1, actualEndedAt: null },
      report,
    });
    expect(running.current).toBe('end');
    expect(running.stages[3]!.state).toBe('done');
    expect(running.stages[4]!.state).toBe('running');

    const ended = deriveWorkStages({
      ...BASE,
      task: { status: 'DONE', actualStartedAt: 1, actualEndedAt: 2 },
      report,
    });
    expect(ended.current).toBe('sign');
    expect(ended.stages[5]!.detail).toContain('참석자 서명 2명');

    const approved = deriveWorkStages({
      ...BASE,
      task: { status: 'DONE', actualStartedAt: 1, actualEndedAt: 2 },
      report: { ...report, approved_at: 3 },
    });
    expect(approved.stages[5]!.state).toBe('done');
  });

  it('작업 상태 값이 없으면 시작·종료는 확인 불가다', () => {
    const s = states({ ...BASE, task: { status: undefined } });
    expect(s).toMatchObject({ start: 'unknown', end: 'unknown' });
  });
});

describe('작업 종료 안내', () => {
  afterEach(cleanup);

  it('종료 작업은 최고 위험 배지 대신 종료 상태를 보이고 진행 작업은 등급을 유지한다', () => {
    const task = TBM_TASKS[0]!;
    const { rerender } = render(
      <TaskList
        tasks={[{ ...task, status: 'DONE' }]}
        selectedId=""
        sentAt={{}}
        onSelect={() => {}}
        onAdd={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: /작업 종료/ }).textContent).not.toContain('위험');
    expect(screen.getByRole('button', { name: /작업 종료/ }).textContent).not.toContain('▲');
    rerender(
      <TaskList
        tasks={[{ ...task, status: 'IN_PROGRESS' }]}
        selectedId=""
        sentAt={{}}
        onSelect={() => {}}
        onAdd={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: /CRITICAL/ }).textContent).toContain('▲');
  });

  it('종료 후 결재 전 단계에 서명과 결재 순서를 안내한다', () => {
    const result = deriveWorkStages({
      ...BASE,
      task: { status: 'DONE' },
      report: { status: 'SUCCEEDED', approved_at: null },
    });
    const noop = () => {};
    render(
      <WorkStageBar
        result={result}
        status="DONE"
        hasSetupSession={false}
        canOpenSetup
        generating={false}
        pending={false}
        onOpenSetup={noop}
        onOpenZones={noop}
        onGenerate={noop}
        onStart={noop}
        onEnd={noop}
        onSign={noop}
      />,
    );
    expect(screen.getByText(/참석자 서명을 확인한 뒤 관리자 결재까지/)).toBeTruthy();
  });
});

// ── 화면 ────────────────────────────────────────────────────────────────────

let tbmMock = createTbmMock();
const geometryHandlers = [
  http.get('*/api/zones', () => HttpResponse.json(MOCK_ZONES)),
  http.get('*/api/static-objects', () => HttpResponse.json(MOCK_STATIC_OBJECTS)),
  http.get('*/api/scenes/current', () =>
    HttpResponse.json({ status: 404, code: 'SCENE_NOT_FOUND', detail: '없음' }, { status: 404 }),
  ),
];
const server = setupServer(...tbmMock.handlers, ...geometryHandlers);

beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, 'innerText', {
    configurable: true,
    get() {
      return this.textContent;
    },
    set(value: string) {
      this.textContent = value;
    },
  });
  server.listen();
});
beforeEach(() => {
  resetSetupFlowStore();
  tbmMock = createTbmMock();
  server.resetHandlers(...tbmMock.handlers, ...geometryHandlers);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

/** 작업 한 건(등록 보고서 없음)과 그 작업의 세팅 세션 응답을 서버 값으로 준다. */
function serveWork(opts: { sessionId?: string | null; session?: SetupSessionResponse | 'error' }) {
  const work = {
    work_id: '77',
    status: 'PLANNED',
    date: '2026-10-16',
    name: '단계 시험 작업',
    from_ms: 1_760_572_800_000,
    to_ms: 1_760_583_600_000,
    zone_id: null,
    place: '교실',
    planned_people: 3,
    lead: 'A조',
    layout_id: null,
    work_type: null,
    active_setup_session_id: opts.sessionId ?? null,
  };
  server.use(
    http.get('*/api/work-units', () => HttpResponse.json({ works: [work] })),
    http.get('*/api/setup-sessions/:id', () =>
      opts.session === 'error' || opts.session === undefined
        ? HttpResponse.json({ code: 'SETUP_SESSION_NOT_FOUND' }, { status: 404 })
        : HttpResponse.json(opts.session),
    ),
  );
}

const stageOf = async (key: string) => {
  const bar = await screen.findByTestId('work-stage-bar');
  return bar.querySelector(`[data-stage="${key}"]`) as HTMLElement;
};

describe('작업 관리 화면', () => {
  it('제목이 작업 관리이고 세팅 전 작업은 세팅 단계가 강조되며 세팅 시작이 다음 행동이다', async () => {
    serveWork({});
    const onOpenSetup = vi.fn();
    render(<TbmPage onOpenSetup={onOpenSetup} />);
    expect(await screen.findByText('작업 관리')).toBeTruthy();
    const setup = await stageOf('setup');
    await waitFor(() => expect(setup.getAttribute('aria-current')).toBe('step'));
    expect(setup.getAttribute('data-state')).toBe('todo');
    fireEvent.click(screen.getByRole('button', { name: '세팅 시작' }));
    expect(onOpenSetup).toHaveBeenCalledWith(77);
  });

  it('세팅이 끝난 작업은 구역 단계가 아직으로 강조되고 구역 설정이 다음 행동이다', async () => {
    serveWork({
      sessionId: 'ss-77',
      session: SESSION({ setup_session_id: 'ss-77', work_unit_id: 77, status: 'READY_FOR_ZONES' }),
    });
    const onOpenZones = vi.fn();
    render(<TbmPage onOpenZones={onOpenZones} />);
    await waitFor(async () =>
      expect((await stageOf('setup')).getAttribute('data-state')).toBe('done'),
    );
    const zones = await stageOf('zones');
    expect(zones.getAttribute('data-state')).toBe('todo');
    expect(zones.getAttribute('aria-current')).toBe('step');
    fireEvent.click(screen.getByRole('button', { name: '구역 설정' }));
    expect(onOpenZones).toHaveBeenCalledTimes(1);
  });

  it('세션의 zones_committed_at 이 있으면 구역이 끝남이고 지금 단계는 TBM 보고서다', async () => {
    serveWork({
      sessionId: 'ss-77',
      session: SESSION({
        setup_session_id: 'ss-77',
        work_unit_id: 77,
        status: 'COMPLETED',
        zones_committed_at: 1_000,
      }),
    });
    render(<TbmPage />);
    await waitFor(async () =>
      expect((await stageOf('zones')).getAttribute('data-state')).toBe('done'),
    );
    expect((await stageOf('tbm')).getAttribute('aria-current')).toBe('step');
    expect(screen.getByRole('button', { name: 'TBM 보고서 만들기' })).toBeTruthy();
  });

  it('세션 조회가 실패하면 세팅·구역을 확인 불가로 보인다(지어내지 않는다)', async () => {
    serveWork({ sessionId: 'ss-gone', session: 'error' });
    render(<TbmPage />);
    await waitFor(async () =>
      expect((await stageOf('setup')).getAttribute('data-state')).toBe('unknown'),
    );
    expect((await stageOf('zones')).getAttribute('data-state')).toBe('unknown');
  });

  it('세팅·구역 전에 작업 시작을 누르면 확인 경고를 보이고, 취소하면 시작하지 않는다', async () => {
    serveWork({});
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    let started = 0;
    server.use(
      http.post('*/api/work-units/:id/start', () => {
        started += 1;
        return HttpResponse.json({ code: 'WORK_INVALID_STATE' }, { status: 409 });
      }),
    );
    render(<TbmPage />);
    fireEvent.click(await screen.findByRole('button', { name: '작업 시작' }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm.mock.calls[0]![0]).toContain('세팅');
    expect(confirm.mock.calls[0]![0]).toContain('구역');
    expect(started).toBe(0);

    // 계속하겠다고 하면 막지 않고 요청한다.
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole('button', { name: '작업 시작' }));
    await waitFor(() => expect(started).toBe(1));
  });

  it('세팅·구역이 끝난 작업은 경고 없이 시작한다', async () => {
    serveWork({
      sessionId: 'ss-77',
      session: SESSION({
        setup_session_id: 'ss-77',
        work_unit_id: 77,
        status: 'COMPLETED',
        zones_committed_at: 1_000,
      }),
    });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    let started = 0;
    server.use(
      http.post('*/api/work-units/:id/start', () => {
        started += 1;
        return HttpResponse.json({ code: 'WORK_INVALID_STATE' }, { status: 409 });
      }),
    );
    render(<TbmPage />);
    await waitFor(async () =>
      expect((await stageOf('zones')).getAttribute('data-state')).toBe('done'),
    );
    fireEvent.click(screen.getByRole('button', { name: '작업 시작' }));
    await waitFor(() => expect(started).toBe(1));
    expect(confirm).not.toHaveBeenCalled();
  });

  it('initialWorkId 로 열면 그 작업이 선택된다', async () => {
    tbmMock = createTbmMock();
    render(<TbmPage initialWorkId="2" />);
    expect(await screen.findByRole('region', { name: 'T2 가상 레이아웃' })).toBeTruthy();
  });

  it('이 브라우저에서 방금 구역을 발행했다면 구역을 끝남으로 본다', async () => {
    serveWork({
      sessionId: 'ss-77',
      session: SESSION({ setup_session_id: 'ss-77', work_unit_id: 77, status: 'READY_FOR_ZONES' }),
    });
    act(() => {
      useSetupFlowStore.setState({ activeSessionId: 'ss-77', zonesCommitted: true });
    });
    render(<TbmPage />);
    await waitFor(async () =>
      expect((await stageOf('zones')).getAttribute('data-state')).toBe('done'),
    );
  });
});

// ── 기본 선택·빈 상태·칩 정렬(S15P31A101-506) ────────────────────────────────

const serverWork = (id: string, status: string, name: string) => ({
  work_id: id,
  status,
  date: '2026-10-16',
  name,
  from_ms: 1_760_572_800_000,
  to_ms: 1_760_583_600_000,
  zone_id: null,
  place: '교실',
  planned_people: 3,
  lead: 'A조',
  layout_id: null,
  work_type: null,
  active_setup_session_id: null,
});

function serveWorks(works: ReturnType<typeof serverWork>[]) {
  server.use(
    http.get('*/api/work-units', () => HttpResponse.json({ works })),
    http.post('*/api/work-units', async ({ request }) => {
      const body = (await request.json()) as { name: string };
      const created = serverWork(String(works.length + 1), 'PLANNED', body.name);
      works.push(created);
      return HttpResponse.json(created, { status: 201 });
    }),
  );
}

const chipNames = () =>
  screen
    .getAllByRole('button', { name: /^T\d+ / })
    .map((b) => (b.getAttribute('aria-label') ?? '').split(' ')[0]);

describe('작업 칩 정렬', () => {
  afterEach(cleanup);
  it('진행 중 → 예정 → 끝난 작업 순으로 늘어놓는다', () => {
    const [a, b, c] = TBM_TASKS;
    render(
      <TaskList
        tasks={[
          { ...a!, id: 'T1', status: 'DONE' },
          { ...b!, id: 'T2', status: 'PLANNED' },
          { ...c!, id: 'T3', status: 'IN_PROGRESS' },
        ]}
        selectedId=""
        sentAt={{}}
        onSelect={() => {}}
        onAdd={() => {}}
      />,
    );
    expect(chipNames()).toEqual(['T3', 'T2', 'T1']);
  });
});

describe('작업 관리 기본 선택과 빈 상태', () => {
  it('진행 중 작업이 있으면 예정보다 먼저 선택한다', async () => {
    serveWorks([
      serverWork('1', 'DONE', '끝난 작업'),
      serverWork('2', 'PLANNED', '예정 작업'),
      serverWork('3', 'IN_PROGRESS', '진행 작업'),
    ]);
    render(<TbmPage />);
    expect(await screen.findByRole('region', { name: 'T3 가상 레이아웃' })).toBeTruthy();
    expect(screen.queryByTestId('tbm-empty-state')).toBeNull();
  });

  it('진행 중이 없으면 예정 작업을 선택한다(끝난 작업이 앞에 있어도)', async () => {
    serveWorks([serverWork('1', 'DONE', '끝난 작업'), serverWork('2', 'PLANNED', '예정 작업')]);
    render(<TbmPage />);
    expect(await screen.findByRole('region', { name: 'T2 가상 레이아웃' })).toBeTruthy();
  });

  it('끝난 작업만 있으면 상세 대신 빈 상태를 보이고, 새 작업 등록이 등록 대화상자를 연다', async () => {
    serveWorks([serverWork('1', 'DONE', '끝난 작업')]);
    render(<TbmPage />);
    await screen.findByText('진행 중인 작업이 없습니다');
    const empty = screen.getByTestId('tbm-empty-state');
    expect(empty.textContent).toContain('등록하면 1. 세팅부터 시작합니다');
    expect(screen.queryByTestId('work-stage-bar')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '새 작업 등록' }));
    expect(screen.getByRole('dialog', { name: '작업 등록' })).toBeTruthy();
  });

  it('빈 상태에서 끝난 작업 칩을 누르면 그 작업 상세가 열린다', async () => {
    serveWorks([serverWork('1', 'DONE', '끝난 작업')]);
    render(<TbmPage />);
    await screen.findByText('진행 중인 작업이 없습니다');
    fireEvent.click(screen.getByRole('button', { name: /^T1 끝난 작업/ }));
    expect(await screen.findByRole('region', { name: 'T1 가상 레이아웃' })).toBeTruthy();
    expect(screen.queryByTestId('tbm-empty-state')).toBeNull();
  });

  it('빈 상태에서 작업을 등록하면 새 작업이 선택되고 세팅 시작이 보인다', async () => {
    serveWorks([serverWork('1', 'DONE', '끝난 작업')]);
    render(<TbmPage />);
    await screen.findByText('진행 중인 작업이 없습니다');
    fireEvent.click(screen.getByRole('button', { name: '새 작업 등록' }));
    const dialog = screen.getByRole('dialog', { name: '작업 등록' });
    fireEvent.change(within(dialog).getByLabelText('작업명'), { target: { value: '새 작업' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '등록' }));
    expect(await screen.findByRole('button', { name: '세팅 시작' })).toBeTruthy();
    expect(screen.queryByTestId('tbm-empty-state')).toBeNull();
  });
});
