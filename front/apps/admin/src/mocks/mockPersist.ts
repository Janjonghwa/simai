// 연습 빌드(S15P31A101-806)의 mock 상태를 브라우저 localStorage 에 남긴다. msw 상태는 메모리에만 있어 새로고침·새 탭마다
// 처음으로 돌아갔다. 키는 모두 PREFIX 로 시작하고, 「연습 처음부터 다시」는 이 접두어 키를 모두 지운다.
// 저장소가 없거나(비공개 창·차단) 값이 깨졌으면 저장하지 않은 것처럼 처음 상태로 둔다.

export const MOCK_STATE_PREFIX = 'simai-mock:';

export type MockStateStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> & {
  readonly length: number;
  key(index: number): string | null;
};

/** 연습 빌드면 localStorage, 아니면(개발 서버·시험) 저장하지 않는다. */
export function mockStateStorage(): MockStateStorage | null {
  if (import.meta.env.VITE_MOCK_BUILD !== 'true') return null;
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function readMockState(storage: MockStateStorage | null, key: string): unknown {
  if (!storage) return undefined;
  try {
    const raw = storage.getItem(MOCK_STATE_PREFIX + key);
    return raw === null ? undefined : (JSON.parse(raw) as unknown);
  } catch {
    return undefined;
  }
}

export function writeMockState(storage: MockStateStorage | null, key: string, value: unknown) {
  if (!storage) return;
  try {
    storage.setItem(MOCK_STATE_PREFIX + key, JSON.stringify(value));
  } catch {
    // 저장 공간이 차거나 막혔으면 이번 값은 남기지 않는다(새로고침하면 처음부터).
  }
}

/** 연습 상태를 모두 지운다(「연습 처음부터 다시」). */
export function clearMockState(storage: MockStateStorage | null) {
  if (!storage) return;
  try {
    const keys: string[] = [];
    for (let i = 0; i < storage.length; i += 1) {
      const key = storage.key(i);
      if (key?.startsWith(MOCK_STATE_PREFIX)) keys.push(key);
    }
    for (const key of keys) storage.removeItem(key);
  } catch {
    // 지우지 못하면 그대로 둔다.
  }
}
