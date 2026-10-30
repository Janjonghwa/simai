import { formatTime } from '@/features/events/events';
import { chooseStep, holdFor } from '@/features/history/replay';
import { WorkReplayPage } from '@/features/history/WorkReplayPage';
import { TBM_TASKS } from '@/features/tbm/tbmDemo';
import type { ReplaySpatialSource } from '@/features/viewer3d';
import { ApiError } from '@/lib/http';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

const navigate = vi.fn();
// 오늘 11시. 어제 작업은 모두 끝났다(mock). mock 은 작업마다 구간의 20% 지점부터 1분은 사람이 없고(world 행 없음, gaps 아님),
// 40% 지점부터 2분은 기록이 끊긴다(gaps). 작업 구간이 겹치면 mock 은 처음 맞는 작업(T1: 09:00–12:00)의 지점을 쓴다.
const NOW = vi.hoisted(() => new Date(2026, 9, 29, 11, 0).getTime());
// 화면이 만든 재생 Source. 떠날 때 멈췄는지 본다.
const engine = vi.hoisted(() => ({ replays: [] as ReplaySpatialSource[] }));
// 서버 흉내를 바꾸는 손잡이.
const server = vi.hoisted(() => ({
  /** 서버가 요청한 간격의 몇 배로 기록을 줄지(넓은 간격으로 주는 서버 흉내). */
  stepScale: 1,
  /** 받은 구간 기록 요청(창)들. */
  requests: [] as { from: number; to: number; step: number }[],
  /** 있으면 구간 기록 요청이 이 오류로 실패한다. */
  replayError: null as unknown,
  /** 있으면 구간 기록 응답을 이 함수가 고친 것으로 바꾼다. */
  reshape: null as ((body: Record<string, unknown>) => Record<string, unknown>) | null,
}));

vi.mock('@tanstack/react-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@tanstack/react-router')>()),
  useNavigate: () => navigate,
}));
vi.mock('@/features/zones/useZones', () => ({
  useZones: () => ({ zones: [], raw: undefined, failed: false }),
}));
// 작업 목록·구간 기록은 mock 합성 함수가 답한다(서버 실제 응답 모양). 요청은 화면이 창으로 나눠 보내고 합친다.
vi.mock('@/lib/api', async () => {
  const { historyReplay, historyWorks } = await import('@/mocks/history');
  const works = historyWorks(NOW);
  return {
    api: (path: string) => {
      const url = new URL(path, 'http://api.test');
      if (url.pathname === '/api/work-units')
        return Promise.resolve({ contract: 'DEMO_UNCONFIRMED', works });
      if (url.pathname === '/api/replay') {
        if (server.replayError !== null) return Promise.reject(server.replayError);
        const q = (key: string) => Number(url.searchParams.get(key));
        server.requests.push({ from: q('from'), to: q('to'), step: q('step_ms') });
        const step = q('step_ms') * server.stepScale;
        const body = historyReplay(works, q('from'), q('to'), step, NOW);
        return Promise.resolve(server.reshape ? server.reshape(body) : body);
      }
      return Promise.reject(new Error(path));
    },
  };
});
// 재생 엔진(parseReplayLog·createReplaySource)은 진짜를 쓰고, 만든 재생 Source 를 모아 둔다.
vi.mock('@/features/viewer3d', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/viewer3d')>();
  const createReplaySource: typeof actual.createReplaySource = (...args) => {
    const replay = actual.createReplaySource(...args);
    engine.replays.push(replay);
    return replay;
  };
  return { ...actual, createReplaySource };
});

const at = (day: number, hhmm: string, s = 0) => {
  const [h, m] = hhmm.split(':').map(Number);
  return new Date(2026, 9, day, h, m, s).getTime();
};

function renderPage(workId: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <WorkReplayPage workId={workId} />
    </QueryClientProvider>,
  );
}

interface Work {
  id: string;
  from: number;
  to: number;
  /** 슬라이더 한 칸(ms). 구간 길이로 정해진다. */
  step: number;
}
const work = (id: string, from: number, to: number): Work => ({
  id,
  from,
  to,
  step: chooseStep(from, to),
});
/** 어제 10:00–11:30. 90분이라 한 칸 1.9초이고 요청은 45분씩 두 창으로 나뉜다. */
const T2 = work('2026-10-28-T2', at(28, '10:00'), at(28, '11:30'));
/** 어제 09:00–12:00. 기록 끊김(10:12–10:14)과 사람 없음(09:36–09:37)이 이 작업 구간의 40%·20% 지점이다. */
const T1 = work('2026-10-28-T1', at(28, '09:00'), at(28, '12:00'));
const T2_ID = T2.id;
const T2_FROM = T2.from;
const T2_TO = T2.to;
const STEP = T2.step;
/** 슬라이더가 받는 시각으로 맞춘다(한 칸 단위). */
const snap = (t: number, w: Work = T2) => w.from + Math.round((t - w.from) / w.step) * w.step;

// 구간 기록(1.5시간, 2,800여 프레임)을 합성·검사·색인하느라 전체 시험을 함께 돌리면 1초를 넘긴다.
const LOAD = { timeout: 10_000 };
const SLOW_MS = 20_000;
/** CCTV 4화면이 뜨고 재생 상태·시계 구독(수동 Effect)까지 마칠 때까지. */
const ready = async () => {
  const feeds = await screen.findAllByRole('img', { name: /카메라 영상/ }, LOAD);
  await waitFor(() => expect(engine.replays.at(-1)?.subscriberCount).toBeGreaterThan(0), LOAD);
  await act(async () => {});
  return feeds;
};

const timeline = () => screen.getByRole('region', { name: '타임라인' });
const slider = () => screen.getByRole('slider', { name: '재생 시각' });
const cctv = () => screen.getByRole('region', { name: 'CCTV 다시 보기' });
/** CCTV 자리의 안내 글(작업을 찾고 구간 기록을 받은 뒤에 뜬다). */
const findNotice = async (text: string) =>
  within(await screen.findByRole('region', { name: 'CCTV 다시 보기' }, LOAD)).findByText(
    text,
    undefined,
    LOAD,
  );
const plan = () => screen.getByRole('region', { name: '2D 평면 다시 보기' });
const workerList = () => screen.getByRole('list', { name: '작업자 목록' });
const eventLog = () => screen.getByRole('list', { name: '이벤트 로그' });
/** 슬라이더로 옮기고 실제로 옮겨진 시각을 돌려준다. */
const seek = (t: number, w: Work = T2) => {
  fireEvent.change(slider(), { target: { value: String(snap(t, w)) } });
  return snap(t, w);
};
/** CCTV 4화면에 그린 사람 수(카메라마다 머리 하나). */
const heads = () =>
  within(cctv())
    .getAllByRole('img', { name: /카메라 영상/ })
    .reduce((n, feed) => n + feed.querySelectorAll('circle').length, 0);
/** 2D 평면에 그린 사람(<g role=button>, 이름은 World ID). */
const planPeople = () => within(plan()).queryAllByRole('button', { name: /^w-\d{4}/ });
/** 타임라인의 "데이터 없음 HH:MM–HH:MM" 칸(기록이 끊긴 2분)의 한가운데 시각. 옮겨 갈 자리를 mock 안의 지점에 묶지 않고 화면에서 읽는다. */
const outageMiddle = (day = 28) => {
  const gap = within(timeline()).getByRole('img', { name: /^데이터 없음 \d\d:\d\d–\d\d:\d\d$/ });
  const [, from] = /^데이터 없음 (\d\d:\d\d)–/.exec(gap.getAttribute('aria-label')!)!;
  return at(day, from!, 60);
};
const pressed = (list: HTMLElement, id: RegExp) =>
  within(list).getByRole('button', { name: id }).getAttribute('aria-pressed');

// 재생 엔진이 있는 3D 입구는 처음 받는 데 오래 걸린다. 미리 받아 두면 화면의 동적 import 가 바로 끝난다.
beforeAll(() => import('@/features/viewer3d'), 30_000);
afterEach(() => {
  cleanup();
  engine.replays.forEach((replay) => replay.pause());
  navigate.mockClear();
  engine.replays = [];
  server.stepScale = 1;
  server.requests = [];
  server.replayError = null;
  server.reshape = null;
});

describe('작업 다시 보기', () => {
  it(
    '작업 구간을 CCTV·작업자·이벤트 로그로 보이고(3D 창은 숨김), 재생·배속·끌기로 시각을 옮긴다',
    async () => {
      expect(STEP).toBe(1_900);
      renderPage(T2_ID);
      const task = TBM_TASKS.find((t) => t.id === 'T2')!;
      expect(await ready()).toHaveLength(4);
      // 90분 구간은 서버 한도(1시간)를 넘어 같은 길이의 두 창으로 나눠 받는다. 간격은 모두 같다.
      const windows = [...server.requests].sort((a, b) => a.from - b.from);
      expect(windows).toEqual([
        { from: T2_FROM, to: T2_FROM + 45 * 60_000, step: STEP },
        { from: T2_FROM + 45 * 60_000, to: T2_TO, step: STEP },
      ]);
      expect(screen.getByRole('heading', { name: task.name })).toBeTruthy();
      expect(screen.queryByRole('region', { name: '3D 다시 보기' })).toBeNull();

      const workers = within(workerList()).getAllByRole('button');
      expect(workers.map((w) => w.textContent?.slice(0, 6))).toEqual([
        'w-0012',
        'w-0015',
        'w-0021',
      ]);
      expect(within(eventLog()).getAllByRole('button').length).toBeGreaterThan(0);
      expect(screen.queryByText('위험·경고 없음')).toBeNull();

      const t = seek(at(28, '10:20'));
      expect(within(timeline()).getByText(formatTime(t))).toBeTruthy();
      expect(within(timeline()).queryByText('데이터 없음')).toBeNull();
      expect(screen.getAllByText('보임')).toHaveLength(3);
      expect(heads()).toBeGreaterThan(0);
      expect(within(cctv()).queryAllByText('데이터 없음')).toHaveLength(0);

      // 두 창의 경계(10:45)도 이어서 보인다(합쳐서 한 기록이다).
      seek(at(28, '10:45'));
      expect(within(timeline()).queryByText('데이터 없음')).toBeNull();
      expect(screen.getAllByText('보임')).toHaveLength(3);

      // 기록이 끊긴 2분: 데이터 없음, 아무도 보이지 않는다(보간하지 않는다, FR-28). 끊긴 시각은 타임라인 칸에서 읽는다.
      seek(outageMiddle());
      expect(within(timeline()).getByText('데이터 없음')).toBeTruthy();
      expect(screen.queryAllByText('보임')).toHaveLength(0);
      expect(heads()).toBe(0);
      expect(within(cctv()).getAllByText('데이터 없음')).toHaveLength(4);

      act(() => screen.getByRole('button', { name: '재생' }).click());
      expect(screen.getByRole('button', { name: '일시정지' })).toBeTruthy();
      act(() => screen.getByRole('button', { name: '일시정지' }).click());
      expect(screen.getByRole('button', { name: '재생' })).toBeTruthy();

      const speeds = within(screen.getByRole('group', { name: '배속' }));
      act(() => speeds.getByRole('button', { name: '2×' }).click());
      expect(speeds.getByRole('button', { name: '2×' }).getAttribute('aria-pressed')).toBe('true');
      expect(speeds.getByRole('button', { name: '1×' }).getAttribute('aria-pressed')).toBe('false');
    },
    SLOW_MS,
  );

  it(
    '오른쪽 아래 작은 창은 2D 평면이고(3D 창은 없다), 크게·작게 보기로 넓히고 줄인다',
    async () => {
      renderPage(T2_ID);
      await ready();
      expect(screen.queryByRole('region', { name: '3D 다시 보기' })).toBeNull();
      expect(cctv().contains(plan())).toBe(true);
      expect(within(plan()).getByRole('group', { name: '2D 평면' })).toBeTruthy();

      const small = within(plan()).getByRole('button', { name: '크게 보기' });
      expect(small.getAttribute('aria-expanded')).toBe('false');
      expect(plan().className).toContain('w-[320px]');

      fireEvent.click(small);
      const big = within(plan()).getByRole('button', { name: '작게 보기' });
      expect(big.getAttribute('aria-expanded')).toBe('true');
      expect(within(plan()).queryByRole('button', { name: '크게 보기' })).toBeNull();
      expect(plan().className).toContain('inset-0');

      fireEvent.click(big);
      expect(within(plan()).getByRole('button', { name: '크게 보기' })).toBeTruthy();
      expect(plan().className).toContain('w-[320px]');

      // 평면에는 그 시각에 보이는 사람이 그려진다.
      seek(at(28, '10:20'));
      expect(planPeople().map((p) => p.getAttribute('aria-label'))).toEqual([
        'w-0012',
        'w-0015',
        'w-0021',
      ]);
    },
    SLOW_MS,
  );

  it(
    '기록이 끊긴 구간으로 옮기면 타임라인·CCTV·평면이 데이터 없음이고, 지나면 돌아온다',
    async () => {
      renderPage(T1.id);
      await ready();
      // 끊긴 구간은 타임라인에 줄무늬 칸으로도 남아 있다(지나간 구간에서도 보인다).
      expect(within(timeline()).getByRole('img', { name: '데이터 없음 10:12–10:14' })).toBeTruthy();

      seek(at(28, '10:08'), T1);
      expect(planPeople()).toHaveLength(3);
      const group = () => within(plan()).getByRole('group', { name: '2D 평면' });
      expect(group().classList.contains('opacity-50')).toBe(false);

      seek(at(28, '10:13'), T1);
      expect(within(timeline()).getByText('데이터 없음')).toBeTruthy();
      expect(within(cctv()).getAllByText('데이터 없음')).toHaveLength(4);
      expect(screen.queryAllByText('보임')).toHaveLength(0);
      expect(heads()).toBe(0);
      expect(planPeople()).toHaveLength(0);
      expect(group().classList.contains('opacity-50')).toBe(true);

      // 끊긴 구간이 끝나면 다시 사람이 보인다.
      seek(at(28, '10:15'), T1);
      expect(within(timeline()).queryByText('데이터 없음')).toBeNull();
      expect(within(cctv()).queryAllByText('데이터 없음')).toHaveLength(0);
      expect(screen.getAllByText('보임')).toHaveLength(3);
      expect(planPeople()).toHaveLength(3);
      expect(group().classList.contains('opacity-50')).toBe(false);
    },
    SLOW_MS,
  );

  it(
    '사람이 없던 구간(기록은 없지만 기록 없는 구간이 아님)은 데이터 없음이 아니라 작업자 없음이다',
    async () => {
      renderPage(T1.id);
      await ready();
      // 09:36 부터 1분은 world 행이 없다. 서버 gaps 에도 들지 않는다(끊김 한 칸뿐이다).
      expect(within(timeline()).getAllByRole('img', { name: /^데이터 없음 / })).toHaveLength(1);

      seek(at(28, '09:36', 30), T1);
      expect(within(timeline()).queryByText('데이터 없음')).toBeNull();
      expect(within(cctv()).queryAllByText('데이터 없음')).toHaveLength(0);
      expect(within(cctv()).getAllByText('작업자 없음')).toHaveLength(4);
      expect(heads()).toBe(0);
      expect(planPeople()).toHaveLength(0);
      expect(within(plan()).getByRole('group', { name: '2D 평면' }).classList).not.toContain(
        'opacity-50',
      );
      // 작업자 목록에는 구간에 잡혔던 세 사람이 있지만 지금은 아무도 보임이 아니다.
      expect(within(workerList()).getAllByRole('button')).toHaveLength(3);
      expect(screen.queryAllByText('보임')).toHaveLength(0);

      // 지나면 다시 보인다.
      seek(at(28, '09:38'), T1);
      expect(within(cctv()).queryAllByText('작업자 없음')).toHaveLength(0);
      expect(screen.getAllByText('보임')).toHaveLength(3);
      expect(heads()).toBeGreaterThan(0);
      expect(planPeople()).toHaveLength(3);
    },
    SLOW_MS,
  );

  it(
    '3D 를 띄우지 않아도 재생하면 시각이 흐른다',
    async () => {
      renderPage(T2_ID);
      await ready();
      const t = seek(at(28, '10:20'));
      act(() =>
        within(screen.getByRole('group', { name: '배속' }))
          .getByRole('button', { name: '4×' })
          .click(),
      );
      act(() => screen.getByRole('button', { name: '재생' }).click());
      await waitFor(() => expect(within(timeline()).queryByText(formatTime(t))).toBeNull(), {
        timeout: 3_000,
      });
      act(() => screen.getByRole('button', { name: '일시정지' }).click());
    },
    SLOW_MS,
  );

  it(
    '이벤트를 누르면 그 시각으로 옮겨 강조하고 대상을 고른다. 작업자를 누르면 고른다',
    async () => {
      renderPage(T2_ID);
      await ready();
      const row = within(eventLog())
        .getAllByRole('button')
        .find((b) => b.textContent?.includes('w-0015'))!;
      const raisedAt = row.querySelector('span')!.textContent!;

      act(() => row.click());
      expect(within(timeline()).getByText(raisedAt)).toBeTruthy();
      expect(row.getAttribute('aria-current')).toBe('true');
      expect(pressed(workerList(), /w-0015/)).toBe('true');

      act(() =>
        within(workerList())
          .getByRole('button', { name: /w-0021/ })
          .click(),
      );
      expect(pressed(workerList(), /w-0021/)).toBe('true');
      expect(pressed(workerList(), /w-0015/)).toBe('false');
    },
    SLOW_MS,
  );

  it(
    '작업자를 고르면 목록과 평면에서 함께 강조하고, 평면의 사람을 눌러도 고른다',
    async () => {
      renderPage(T2_ID);
      await ready();
      seek(at(28, '10:20'));
      const person = (id: string) =>
        within(plan()).getByRole('button', { name: new RegExp(`^${id}`) });

      fireEvent.click(person('w-0021'));
      expect(pressed(workerList(), /w-0021/)).toBe('true');
      expect(pressed(workerList(), /w-0015/)).toBe('false');
      expect(person('w-0021').getAttribute('aria-pressed')).toBe('true');
      expect(person('w-0015').getAttribute('aria-pressed')).toBe('false');

      fireEvent.click(within(workerList()).getByRole('button', { name: /w-0015/ }));
      expect(pressed(workerList(), /w-0015/)).toBe('true');
      expect(pressed(workerList(), /w-0021/)).toBe('false');
      expect(person('w-0015').getAttribute('aria-pressed')).toBe('true');
      expect(person('w-0021').getAttribute('aria-pressed')).toBe('false');
    },
    SLOW_MS,
  );

  it(
    '타임라인의 이벤트 표시를 누르면 그 시각으로 옮기고 그 이벤트의 첫 작업자를 고른다',
    async () => {
      renderPage(T2_ID);
      await ready();
      const label = (b: HTMLElement) => b.getAttribute('aria-label') ?? '';
      const timeOf = (b: HTMLElement) => /(\d\d:\d\d:\d\d)/.exec(label(b))![1]!;
      // 작업 구간 안에서 일어난 개구부·가장자리 근접(R2, 대상 w-0015).
      const marker = within(timeline())
        .getAllByRole('button', { name: /^이벤트 / })
        .find(
          (b) =>
            label(b).endsWith('개구부·가장자리 근접') &&
            timeOf(b) >= formatTime(T2_FROM) &&
            timeOf(b) <= formatTime(T2_TO),
        )!;
      const time = timeOf(marker);
      expect(pressed(workerList(), /w-0015/)).toBe('false');

      fireEvent.click(marker);
      expect(within(timeline()).getByText(time)).toBeTruthy();
      expect(pressed(workerList(), /w-0015/)).toBe('true');
      const row = within(eventLog())
        .getAllByRole('button')
        .find((b) => b.textContent?.includes(time) && b.textContent.includes('개구부'))!;
      expect(row.getAttribute('aria-current')).toBe('true');
    },
    SLOW_MS,
  );

  it(
    '끝으로 옮기면 재생 버튼이 재생으로 돌아오고 엔진도 멈춘다. 다시 재생하면 처음부터 한다',
    async () => {
      renderPage(T2_ID);
      await ready();
      const replay = engine.replays.at(-1)!;
      const end = () => fireEvent.change(slider(), { target: { value: String(T2_TO) } });

      // 멈춘 채 끝으로
      end();
      expect(screen.getByRole('button', { name: '재생' })).toBeTruthy();
      expect(replay.getStatus()).toMatchObject({ playhead: T2_TO, ended: true, playing: false });
      expect(within(timeline()).getByText(formatTime(T2_TO))).toBeTruthy();

      // 끝에서 재생하면 처음부터 한다
      act(() => screen.getByRole('button', { name: '재생' }).click());
      expect(replay.getStatus()).toMatchObject({ playhead: T2_FROM, ended: false, playing: true });
      expect(screen.getByRole('button', { name: '일시정지' })).toBeTruthy();
      expect(within(timeline()).getByText(formatTime(T2_FROM))).toBeTruthy();

      // 재생 중에 끝으로 옮겨도 재생 중으로 남지 않는다
      end();
      expect(screen.getByRole('button', { name: '재생' })).toBeTruthy();
      expect(screen.queryByRole('button', { name: '일시정지' })).toBeNull();
      expect(replay.getStatus()).toMatchObject({ playhead: T2_TO, ended: true, playing: false });
    },
    SLOW_MS,
  );

  it(
    '서버가 요청보다 넓은 간격으로 기록을 주면 그 간격으로 보이는 시간(holdFor)을 잡는다',
    async () => {
      // 1.9초 간격을 청해 9.5초 간격으로 받는다. 요청 간격 기준(3.8초)이면 기록 사이가 비어 사람이 사라진다.
      server.stepScale = 5;
      const served = STEP * 5;
      expect(holdFor(STEP)).toBeLessThan(served);
      renderPage(T2_ID);
      await ready();
      // 기록(9.5초 배수 시각)에서 6초 지난 시각: 기록 사이이고, 요청 간격 기준의 보이는 시간은 이미 지났다.
      const tick = Math.ceil(T2_FROM / served) * served + 10 * served;
      const t = seek(tick + 6_000);
      expect(t - tick).toBeGreaterThan(holdFor(STEP));
      expect(t - tick).toBeLessThan(holdFor(served));
      expect(within(timeline()).getByText(formatTime(t))).toBeTruthy();
      expect(within(timeline()).queryByText('데이터 없음')).toBeNull();
      expect(screen.getAllByText('보임')).toHaveLength(3);
      expect(heads()).toBeGreaterThan(0);
      expect(planPeople()).toHaveLength(3);
    },
    SLOW_MS,
  );

  it(
    '화면을 떠나면 재생을 멈춘다',
    async () => {
      renderPage(T2_ID);
      await ready();
      act(() => screen.getByRole('button', { name: '재생' }).click());
      const replay = engine.replays.at(-1)!;
      expect(replay.getStatus().playing).toBe(true);

      cleanup();
      expect(replay.getStatus().playing).toBe(false);
    },
    SLOW_MS,
  );
});

describe('작업 다시 보기: 기록이 없을 때', () => {
  it(
    '기록이 없는 작업(구간 전체가 기록 없는 구간)은 데이터 없음이라고 하고 재생할 수 없다',
    async () => {
      const last = TBM_TASKS[TBM_TASKS.length - 1]!;
      renderPage(`2026-10-27-${last.id}`);
      // 글은 CCTV 자리 안내 한 곳에만 있다(타임라인은 쓸 수 없는 상태라 글을 쓰지 않는다).
      expect(await screen.findByText('데이터 없음', undefined, LOAD)).toBeTruthy();
      expect(screen.getAllByText('데이터 없음')).toHaveLength(1);
      expect(within(cctv()).getByText('데이터 없음')).toBeTruthy();
      expect(screen.queryAllByRole('img', { name: /카메라 영상/ })).toHaveLength(0);
      expect(screen.queryByRole('region', { name: '2D 평면 다시 보기' })).toBeNull();
      // 작업 전체가 기록 없는 구간 한 칸으로 남는다.
      expect(
        within(timeline()).getByRole('img', {
          name: `데이터 없음 ${last.from}–${last.to}`,
        }),
      ).toBeTruthy();

      expect((screen.getByRole('button', { name: '재생' }) as HTMLButtonElement).disabled).toBe(
        true,
      );
      expect((slider() as HTMLInputElement).disabled).toBe(true);
      // 이벤트가 없으면 로그는 위험·경고 없음 한 줄이다.
      expect(screen.getAllByText('위험·경고 없음')).toHaveLength(1);
      expect(within(workerList()).getByText('잡힌 작업자가 없습니다.')).toBeTruthy();
    },
    SLOW_MS,
  );

  it(
    '기록 없는 구간이 아닌데 잡힌 사람도 이벤트도 없으면 잡힌 작업자가 없다고 하고 재생할 수 없다',
    async () => {
      server.reshape = (body) => ({
        ...body,
        world_states: [],
        initial_events: [],
        events: [],
        gaps: [],
      });
      renderPage(T2_ID);
      expect(await findNotice('잡힌 작업자가 없습니다.')).toBeTruthy();
      expect(screen.queryByText('데이터 없음')).toBeNull();
      expect(screen.queryAllByRole('img', { name: /카메라 영상/ })).toHaveLength(0);
      expect((screen.getByRole('button', { name: '재생' }) as HTMLButtonElement).disabled).toBe(
        true,
      );
      expect((slider() as HTMLInputElement).disabled).toBe(true);
    },
    SLOW_MS,
  );

  it('없는 작업이면 찾을 수 없다고 하고, 작업 목록으로 돌아간다', async () => {
    renderPage('nope');
    expect(await screen.findByText('작업을 찾을 수 없습니다.')).toBeTruthy();
    act(() => screen.getByRole('button', { name: '작업 목록' }).click());
    expect(navigate).toHaveBeenCalledWith({ to: '/history' });
  });
});

describe('작업 다시 보기: 구간 기록을 못 받았을 때', () => {
  it.each([
    ['서버에 닿지 않으면', new ApiError(0, { kind: 'NETWORK' }), '서버에 연결하지 못했습니다.'],
    ['세션이 끝났으면(401)', new ApiError(401), '다시 로그인해 주세요.'],
    [
      '서버가 한도를 넘는다고 하면(더 나눌 수 없을 때까지 나눠도)',
      new ApiError(400, { code: 'REPLAY_RANGE_TOO_LARGE' }),
      '기록이 너무 많아 불러오지 못했습니다.',
    ],
    [
      '구간이 틀렸다고 하면',
      new ApiError(400, { code: 'INVALID_REPLAY_QUERY' }),
      '작업 구간이 올바르지 않습니다.',
    ],
    [
      '서버 응답이 계약과 다르면',
      new ApiError(200, { kind: 'INVALID_RESPONSE' }),
      '기록을 읽지 못했습니다.',
    ],
    ['그 밖의 오류면', new ApiError(500), '기록을 불러오지 못했습니다.'],
  ])(
    '%s CCTV 자리에 한 줄로 알리고 재생할 수 없다',
    async (_name, error, line) => {
      server.replayError = error;
      renderPage(T2_ID);
      expect(await findNotice(line)).toBeTruthy();
      expect(screen.queryAllByRole('img', { name: /카메라 영상/ })).toHaveLength(0);
      expect(screen.queryByRole('region', { name: '2D 평면 다시 보기' })).toBeNull();
      expect((screen.getByRole('button', { name: '재생' }) as HTMLButtonElement).disabled).toBe(
        true,
      );
      expect((slider() as HTMLInputElement).disabled).toBe(true);
    },
    SLOW_MS,
  );
});
