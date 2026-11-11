import { setupSessionResponseSchema } from '@/features/setup/types';
import { createSetupMock } from '@/mocks/setup';
import { setupServer } from 'msw/node';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

// 개발 서버용 세팅 mock 의 autoProgress(시간으로 단계 진행)와 보강 촬영 상태 싣기(S15P31A101-607).

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

const AUTH = { Authorization: 'Bearer mock-token.admin', 'Content-Type': 'application/json' };

describe('세팅 mock autoProgress', () => {
  it('시작 뒤 시간이 지나면 미리보기·정식 맵까지 단계가 앞으로만 넘어가고 supplement 를 싣는다', async () => {
    let clock = 1_000;
    const mock = createSetupMock({ now: () => clock, autoProgress: true });
    server.resetHandlers(...mock.handlers);
    const created = (await (
      await fetch('http://api.test/api/setup-sessions', {
        method: 'POST',
        headers: AUTH,
        body: JSON.stringify({ work_unit_id: 1, camera_heights_m: { cam1: 2.4 } }),
      })
    ).json()) as { setup_session_id: string };
    const get = async () =>
      setupSessionResponseSchema.parse(
        await (
          await fetch(`http://api.test/api/setup-sessions/${created.setup_session_id}`, {
            headers: AUTH,
          })
        ).json(),
      );

    const first = await get();
    expect(first.status).toBe('COLLECTING_BACKGROUND');
    expect(first.supplement).toMatchObject({
      can_upload: false,
      can_rebake: false,
      rebake_blocked_reason: 'SESSION_NOT_READY',
    });

    clock += 14_000;
    const ready = await get();
    expect(ready.status).toBe('READY_FOR_ZONES');
    expect(ready.preview_scene_version).toBeTruthy();
    expect(ready.supplement).toMatchObject({
      can_upload: true,
      rebake_blocked_reason: 'SETUP_JOB_RUNNING',
    });

    clock += 30_000;
    const done = await get();
    expect(done.status).toBe('COMPLETED');
    expect(done.scene_version).toBeTruthy();
    expect(done.supplement?.rebake_blocked_reason).toBe('NO_MEDIA');
  });
});
