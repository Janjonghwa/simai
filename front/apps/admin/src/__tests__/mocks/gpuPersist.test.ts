import { createGpuMock } from '@/mocks/gpu';
import {
  clearMockState,
  MOCK_STATE_PREFIX,
  type MockStateStorage,
  readMockState,
  writeMockState,
} from '@/mocks/mockPersist';
import type { RequestHandler } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';

// 연습 빌드 GPU mock(S15P31A101-806): 켜진 상태로 시작하고, 켜기·끄기를 저장소에 남겨 새로고침(새 mock) 뒤에도 잇는다.
const BASE = 'http://api.test';
const NOW = 1_800_000_000_000;

function memoryStorage(): MockStateStorage & { map: Map<string, string> } {
  const map = new Map<string, string>();
  return {
    map,
    get length() {
      return map.size;
    },
    key: (i) => [...map.keys()][i] ?? null,
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
}

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const use = (handlers: RequestHandler[]) => server.resetHandlers(...handlers);
const power = async () => {
  const res = await fetch(`${BASE}/api/gpu/sessions/current`);
  return ((await res.json()) as { power: string }).power;
};

describe('연습 빌드 GPU mock', () => {
  it('startReady 면 처음부터 연결됨이다', async () => {
    use(createGpuMock({ startReady: true, now: () => NOW }).handlers);
    expect(await power()).toBe('CONNECTED');
  });

  it('alwaysOn 이면 남긴 끈 상태도 무시하고 연결됨이며 끄기를 거절한다', async () => {
    const storage = memoryStorage();
    storage.setItem(`${MOCK_STATE_PREFIX}gpu`, '{"on":false}');
    use(createGpuMock({ alwaysOn: true, storage, now: () => NOW }).handlers);
    expect(await power()).toBe('CONNECTED');
    const res = await fetch(`${BASE}/api/gpu/stop`, { method: 'POST' });
    expect(res.status).toBe(409);
    expect(await res.text()).toContain('연습용 목업');
    expect(await power()).toBe('CONNECTED');
  });

  it('저장소가 없으면 기존처럼 꺼진 채 시작한다', async () => {
    use(createGpuMock({ now: () => NOW }).handlers);
    expect(await power()).toBe('OFF');
  });

  it('끈 상태를 남겨 다시 만든 mock(새로고침)도 꺼진 채 시작한다', async () => {
    const storage = memoryStorage();
    use(createGpuMock({ startReady: true, storage, now: () => NOW }).handlers);
    expect((await fetch(`${BASE}/api/gpu/stop`, { method: 'POST' })).status).toBe(204);
    use(createGpuMock({ startReady: true, storage, now: () => NOW }).handlers);
    expect(await power()).toBe('OFF');
  });

  it('켜는 중에 새로고침해도 켠 시각부터 이어 준비 완료가 된다', async () => {
    const storage = memoryStorage();
    let at = NOW;
    use(createGpuMock({ storage, now: () => at }).handlers);
    expect((await fetch(`${BASE}/api/gpu/launches`, { method: 'POST' })).status).toBe(201);
    expect(await power()).toBe('STARTING');
    at = NOW + 60_000;
    use(createGpuMock({ storage, now: () => at }).handlers);
    expect(await power()).toBe('CONNECTED');
  });

  it('남긴 값이 깨졌으면 무시하고 처음 상태로 둔다', async () => {
    const storage = memoryStorage();
    storage.setItem(`${MOCK_STATE_PREFIX}gpu`, '{"on":true,"launch":{"launch_id":1}}');
    use(createGpuMock({ storage, now: () => NOW }).handlers);
    expect(await power()).toBe('OFF');
    storage.setItem(`${MOCK_STATE_PREFIX}gpu`, 'not json');
    use(createGpuMock({ startReady: true, storage, now: () => NOW }).handlers);
    expect(await power()).toBe('CONNECTED');
  });
});

describe('mockPersist', () => {
  it('접두어 키만 지운다', () => {
    const storage = memoryStorage();
    writeMockState(storage, 'gpu', { on: false });
    storage.setItem('other', 'keep');
    expect(readMockState(storage, 'gpu')).toEqual({ on: false });
    clearMockState(storage);
    expect(readMockState(storage, 'gpu')).toBeUndefined();
    expect(storage.getItem('other')).toBe('keep');
  });

  it('저장소가 없으면 읽기·쓰기·지우기가 아무 일도 하지 않는다', () => {
    expect(readMockState(null, 'gpu')).toBeUndefined();
    expect(() => {
      writeMockState(null, 'gpu', 1);
      clearMockState(null);
    }).not.toThrow();
  });
});
