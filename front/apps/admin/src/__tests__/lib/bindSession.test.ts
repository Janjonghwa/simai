import { bindSession } from '@/lib/bindSession';
import type { Session } from '@/store/session.store';
import { describe, expect, it } from 'vitest';
import { createStore } from 'zustand/vanilla';

const alice: Session = { accessToken: 'a-1', user: { id: 'alice', role: 'ADMIN' } };
const bob: Session = { accessToken: 'b-1', user: { id: 'bob', role: 'VIEWER' } };

function setup(initial?: Session) {
  const log: string[] = [];
  const sessionStore = createStore<{ session?: Session }>()(() => ({ session: initial }));
  let finishInvalidate: () => void = () => {};
  const unbind = bindSession({
    sessionStore,
    realtime: {
      start: ({ token, userId }) => log.push(`start ${userId} ${token}`),
      stop: async () => {
        log.push('stop');
      },
    },
    router: {
      invalidate: () => {
        log.push('invalidate');
        return new Promise<void>((resolve) => {
          finishInvalidate = () => resolve();
        });
      },
      clearCache: () => log.push('clearCache'),
    },
    queryClient: { clear: () => log.push('clear') },
  });
  const settle = async () => {
    finishInvalidate();
    await Promise.resolve();
    await Promise.resolve();
  };
  return { log, sessionStore, unbind, settle };
}

describe('bindSession', () => {
  it('로그인하면 그 토큰으로 연결하고 가드를 다시 돌린다(캐시는 지우지 않는다)', async () => {
    const { log, sessionStore, settle } = setup();
    sessionStore.setState({ session: alice });
    await settle();
    expect(log).toEqual(['start alice a-1', 'invalidate']);
  });

  it('로그아웃하면 연결을 끊고, 가드가 끝난 뒤에 캐시를 지운다', async () => {
    const { log, sessionStore, settle } = setup(alice);
    log.length = 0;
    sessionStore.setState({ session: undefined });
    expect(log).toEqual(['stop', 'clearCache', 'invalidate']);
    await settle();
    expect(log).toEqual(['stop', 'clearCache', 'invalidate', 'clear']);
  });

  it('다른 계정으로 바뀌면 끊고 새 토큰으로 연결한 뒤 캐시를 지운다', async () => {
    const { log, sessionStore, settle } = setup(alice);
    log.length = 0;
    sessionStore.setState({ session: bob });
    await settle();
    expect(log).toEqual(['stop', 'start bob b-1', 'clearCache', 'invalidate', 'clear']);
  });

  it('같은 사람의 토큰만 바뀌면 다시 연결만 하고 캐시는 둔다', async () => {
    const { log, sessionStore, settle } = setup(alice);
    log.length = 0;
    sessionStore.setState({ session: { ...alice, accessToken: 'a-2' } });
    await settle();
    expect(log).toEqual(['start alice a-2', 'invalidate']);
  });

  it('가드가 실패해도 로그아웃이면 캐시를 지운다', async () => {
    const log: string[] = [];
    const sessionStore = createStore<{ session?: Session }>()(() => ({ session: alice }));
    bindSession({
      sessionStore,
      realtime: { start: () => {}, stop: async () => {} },
      router: { invalidate: () => Promise.reject(new Error('loader')), clearCache: () => {} },
      queryClient: { clear: () => log.push('clear') },
    });
    sessionStore.setState({ session: undefined });
    await new Promise((r) => setTimeout(r, 0));
    expect(log).toEqual(['clear']);
  });

  it('이미 로그인된 상태로 묶으면 바로 연결하고, 해제하면 더 반응하지 않는다', () => {
    const { log, sessionStore, unbind } = setup(alice);
    expect(log).toEqual(['start alice a-1']);
    unbind();
    sessionStore.setState({ session: undefined });
    expect(log).toEqual(['start alice a-1']);
  });
});
