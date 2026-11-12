import { createSetupSession } from '@/features/setup/api';
import {
  appliedZonesSummary,
  canCompleteSetup,
  EMPTY_ZONE_PLAN_CONFIRM,
  SetupCompleteCard,
} from '@/features/setup/SetupCompleteCard';
import {
  SETUP_COMPLETE_ERROR_TEXT,
  setupCompleteErrorMessage,
} from '@/features/setup/setupErrorMessage';
import { REBAKE_BLOCKED_LABEL, SUPPLEMENT_ERROR_TEXT } from '@/features/setup/supplement';
import type { SetupSessionResponse } from '@/features/setup/types';
import { useSetupSession } from '@/features/setup/useSetupStep1';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { problem } from '@/mocks/problem';
import { createSetupMock } from '@/mocks/setup';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

// 세팅 완료(C17, S15P31A101-615) 카드: 보이는 조건, 빈 구역 목록 확인, 호출과 완료 뒤 비활성, 거부 코드 안내.

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => 'mock-token.admin' }) };
});

const server = setupServer();
let mock: ReturnType<typeof createSetupMock>;

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  mock = createSetupMock({ now: () => 1_800_000_000_000 });
  server.resetHandlers(...mock.handlers);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

async function sessionAt(status: SetupSessionResponse['status']): Promise<string> {
  const created = await createSetupSession(api, {
    work_unit_id: 1,
    camera_heights_m: { cam1: 2.4 },
  });
  mock.updateSession(created.setup_session_id, {
    status,
    stage: status,
    calibration_version: 'cal-1',
    preview_scene_version: 'scene-preview-1',
  });
  return created.setup_session_id;
}

function Harness({ id }: { id: string }) {
  const session = useSetupSession(id).data;
  return session ? <SetupCompleteCard session={session} /> : null;
}

function show(id: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Harness id={id} />
    </QueryClientProvider>,
  );
}

const completeButton = () => screen.findByRole('button', { name: '세팅 완료' });

describe('canCompleteSetup·appliedZonesSummary', () => {
  const base = { setup_completed_at_ms: null } as unknown as SetupSessionResponse;
  it('미리보기 맵 이후이고 완료 전일 때만 완료할 수 있다', () => {
    expect(canCompleteSetup({ ...base, status: 'PREVIEW_AND_VLM_DRAFT' })).toBe(false);
    expect(canCompleteSetup({ ...base, status: 'READY_FOR_ZONES' })).toBe(true);
    expect(canCompleteSetup({ ...base, status: 'BAKING_TEXTURE' })).toBe(true);
    expect(canCompleteSetup({ ...base, status: 'COMPLETED' })).toBe(true);
    expect(canCompleteSetup({ ...base, status: 'FAILED' })).toBe(false);
    expect(canCompleteSetup({ ...base, status: 'COMPLETED', setup_completed_at_ms: 1 })).toBe(
      false,
    );
    expect(canCompleteSetup(null)).toBe(false);
  });

  it('적용 결과는 개수만 짧게 보인다', () => {
    expect(
      appliedZonesSummary({ created: ['a', 'b'], updated: ['c'], kept: [], archived: ['d'] }),
    ).toBe('구역 적용: 새로 2 · 고침 1 · 유지 0 · 보관 1');
  });
});

describe('세팅 완료 카드', () => {
  it('미리보기 맵 전에는 단추가 꺼져 있다', async () => {
    show(await sessionAt('PREVIEW_AND_VLM_DRAFT'));
    expect(((await completeButton()) as HTMLButtonElement).disabled).toBe(true);
  });

  it('저장한 구역 목록이 없으면 보관 경고를 묻고, 취소하면 완료하지 않는다', async () => {
    const id = await sessionAt('READY_FOR_ZONES');
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    show(id);
    fireEvent.click(await completeButton());
    await waitFor(() => expect(confirm).toHaveBeenCalledWith(EMPTY_ZONE_PLAN_CONFIRM));
    await waitFor(() =>
      expect(
        (screen.getByRole('button', { name: '세팅 완료' }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    expect(mock.sessions.get(id)?.setup_completed_at_ms ?? null).toBeNull();
  });

  it('확인하면 완료를 부르고, 완료 시각·적용 개수를 보이며 단추가 사라진다', async () => {
    const id = await sessionAt('READY_FOR_ZONES');
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    show(id);
    fireEvent.click(await completeButton());
    expect(await screen.findByText(/세팅을 완료했습니다/)).toBeTruthy();
    expect(screen.getByText('구역 적용: 새로 0 · 고침 0 · 유지 0 · 보관 0')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '세팅 완료' })).toBeNull();
    expect(mock.sessions.get(id)?.setup_completed_at_ms).toBe(1_800_000_000_000);
  });

  it('구역 목록을 저장했으면 묻지 않고 완료한다', async () => {
    const id = await sessionAt('READY_FOR_ZONES');
    server.use(
      http.get('*/api/setup-sessions/:id/zones', ({ params }) =>
        HttpResponse.json({
          setup_session_id: String(params.id),
          items: [{ zone_id: 'z-1', source: 'MANUAL' }],
          revision: 2,
        }),
      ),
    );
    const confirm = vi.spyOn(window, 'confirm');
    show(id);
    fireEvent.click(await completeButton());
    expect(await screen.findByText(/세팅을 완료했습니다/)).toBeTruthy();
    expect(confirm).not.toHaveBeenCalled();
  });

  it('거부되면 코드별 안내를 보이고 다시 누를 수 있다', async () => {
    const id = await sessionAt('READY_FOR_ZONES');
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    server.use(
      http.post('*/api/setup-sessions/:id/complete', () =>
        problem(409, 'SETUP_ZONE_WORLD_MISMATCH', 'World 좌표가 바뀐 세팅'),
      ),
    );
    show(id);
    fireEvent.click(await completeButton());
    expect((await screen.findByRole('alert')).textContent).toBe(
      SETUP_COMPLETE_ERROR_TEXT.SETUP_ZONE_WORLD_MISMATCH,
    );
    expect(((await completeButton()) as HTMLButtonElement).disabled).toBe(false);
  });
});

describe('세팅 완료 거부 안내', () => {
  it.each([
    'SETUP_ALREADY_COMPLETED',
    'SETUP_NOT_READY_TO_COMPLETE',
    'SETUP_SESSION_NOT_ACTIVE',
    'WORK_NOT_TODAY',
    'WORK_INVALID_STATE',
    'SETUP_COMPLETE_BLOCKED_BY_LIVE_WORK',
    'SETUP_ZONE_WORLD_MISMATCH',
    'SUPPLEMENT_SETUP_COMPLETED',
  ])('%s 는 한국어 안내로 바꾼다', (code) => {
    const text = setupCompleteErrorMessage(new ApiError(409, { code, serverMessage: 'server' }));
    expect(text).toBe(SETUP_COMPLETE_ERROR_TEXT[code]);
    expect(text).not.toBe('server');
  });

  it('모르는 4xx 는 서버 문구, 그 밖에는 기본 문구', () => {
    expect(
      setupCompleteErrorMessage(new ApiError(409, { code: 'X', serverMessage: '서버 안내' })),
    ).toBe('서버 안내');
    expect(setupCompleteErrorMessage(new Error('x'))).toBe(
      '세팅을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요',
    );
  });

  it('완료 뒤 보강 거부 사유(SETUP_COMPLETED)와 코드도 안내한다', () => {
    expect(REBAKE_BLOCKED_LABEL.SETUP_COMPLETED).toBeTruthy();
    expect(SUPPLEMENT_ERROR_TEXT.SUPPLEMENT_SETUP_COMPLETED).toBeTruthy();
  });
});
