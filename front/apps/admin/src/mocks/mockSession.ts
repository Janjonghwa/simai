import {
  MOCK_STATE_PREFIX,
  type MockStateStorage,
  readMockState,
  writeMockState,
} from '@/mocks/mockPersist';

// 연습 빌드(S15P31A101-806) mock 상태 전체를 저장소 키 하나에 모아 남기고 부팅 때 되살린다. 각 mock 은 「지금 상태를 JSON 으로
// 옮길 수 있는 값으로 내보내기(exportState)」와 「저장분을 검사해 적용 함수 돌려주기(loadState)」를 내놓고, 여기서 모아 저장·복원한다.
// 모든 부분을 먼저 검사하고(하나라도 모양이 틀리면 키를 버리고 처음 상태), 다 맞을 때만 차례로 적용한다(부분끼리 어긋나지 않게).
// 저장소가 없으면(운영·개발 서버·시험: mockStateStorage() 가 null) 아무것도 하지 않아 지금처럼 메모리만 쓴다.

/** 저장 키(접두어 simai-mock: 뒤). 「연습 처음부터 다시」가 접두어 키를 모두 지우므로 함께 지워진다. */
export const MOCK_SESSION_KEY = 'session';
/** 저장 형식 판. 부분의 모양을 바꾸면 올린다(옛 판 저장분은 버리고 처음부터). */
export const MOCK_SESSION_VERSION = 1;
/** 상태가 바뀐 뒤 저장까지 기다리는 시간(ms). 그 사이 바뀐 것은 한 번에 저장한다. */
export const MOCK_SESSION_SAVE_DELAY_MS = 300;

/** 저장·복원에 참여하는 mock 한 부분. */
export interface MockSessionPart {
  /** 지금 상태를 JSON 으로 옮길 수 있는 값으로(Map·Set 은 배열로). */
  exportState(): unknown;
  /** 저장분을 검사해 모양이 맞으면 적용 함수를, 아니면 undefined. 검사하는 동안에는 상태를 바꾸지 않는다. */
  loadState(raw: unknown): (() => void) | undefined;
}

export type MockSessionRestoreResult = 'none' | 'restored' | 'discarded';

const SEOUL_DATE = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Seoul',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

/** Asia/Seoul 기준 날짜 키(YYYY-MM-DD). 어제 연습 상태가 오늘 날짜에 붙지 않게 저장분의 날짜와 견준다. */
export const seoulDateKey = (ms: number) => SEOUL_DATE.format(new Date(ms));

// ── 모양 검사 도우미(각 mock 의 loadState 가 쓴다) ─────────────────────────────

export const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
export const isNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);
export const isString = (value: unknown): value is string => typeof value === 'string';
export const isNumberOrNull = (value: unknown): value is number | null =>
  value === null || isNumber(value);

/** 배열이고 모든 원소가 guard 를 통과하면 그 배열, 아니면 undefined. */
export function arrayOf<T>(value: unknown, guard: (item: unknown) => item is T): T[] | undefined {
  return Array.isArray(value) && value.every(guard) ? value : undefined;
}

/** [키, 값] 쌍 배열(Map 을 내보낸 모양)이면 Map 으로, 아니면 undefined. */
export function mapOf<K, V>(
  value: unknown,
  isKey: (key: unknown) => key is K,
  isValue: (item: unknown) => item is V,
): Map<K, V> | undefined {
  if (!Array.isArray(value)) return undefined;
  const map = new Map<K, V>();
  for (const entry of value) {
    if (!Array.isArray(entry) || entry.length !== 2) return undefined;
    const [key, item] = entry as [unknown, unknown];
    if (!isKey(key) || !isValue(item)) return undefined;
    map.set(key, item);
  }
  return map;
}

/** 저장분은 JSON 에서 왔으므로 안쪽 값을 고쳐도 되지만, 적용한 뒤 저장분과 참조를 나누지 않게 깊은 사본을 쓴다. */
export const cloneJson = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

/** 바이트를 base64 로(서명 이미지처럼 작은 파일만 남긴다). */
export function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

/** base64 를 바이트로. 깨졌으면 undefined. */
export function base64ToBytes(text: string): Uint8Array | undefined {
  try {
    const binary = atob(text);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  } catch {
    return undefined;
  }
}

// ── 저장·복원 ────────────────────────────────────────────────────────────────

export interface MockSessionOptions {
  storage: MockStateStorage | null;
  parts: Record<string, MockSessionPart>;
  now?: () => number;
  saveDelayMs?: number;
}

export function createMockSession({
  storage,
  parts,
  now = Date.now,
  saveDelayMs = MOCK_SESSION_SAVE_DELAY_MS,
}: MockSessionOptions) {
  let timer: ReturnType<typeof setTimeout> | undefined;

  const discard = () => {
    if (!storage) return;
    try {
      storage.removeItem(MOCK_STATE_PREFIX + MOCK_SESSION_KEY);
    } catch {
      // 지우지 못하면 그대로 둔다(다음 저장이 덮어쓴다).
    }
  };

  /** 지금 상태를 바로 남긴다. */
  function save() {
    if (timer !== undefined) {
      clearTimeout(timer);
      timer = undefined;
    }
    if (!storage) return;
    const data: Record<string, unknown> = {};
    try {
      for (const [name, part] of Object.entries(parts)) data[name] = part.exportState();
    } catch {
      // 내보내다 실패하면 이번 저장은 건너뛴다(앞서 남긴 값은 그대로).
      return;
    }
    const t = now();
    writeMockState(storage, MOCK_SESSION_KEY, {
      version: MOCK_SESSION_VERSION,
      day: seoulDateKey(t),
      saved_at: t,
      parts: data,
    });
  }

  /** 짧게 기다렸다 남긴다. 기다리는 동안 다시 불러도 저장은 한 번이다. */
  function scheduleSave() {
    if (!storage || timer !== undefined) return;
    timer = setTimeout(save, saveDelayMs);
  }

  /**
   * 남긴 상태를 되살린다. 남긴 값이 없으면 'none', 판·날짜·모양이 맞지 않거나 JSON 이 깨졌으면 키를 지우고 'discarded'
   * (처음 상태 그대로), 모두 적용했으면 'restored'.
   */
  function restore(): MockSessionRestoreResult {
    if (!storage) return 'none';
    let hasRaw: boolean;
    try {
      hasRaw = storage.getItem(MOCK_STATE_PREFIX + MOCK_SESSION_KEY) !== null;
    } catch {
      return 'none';
    }
    if (!hasRaw) return 'none';
    const raw = readMockState(storage, MOCK_SESSION_KEY);
    if (
      !isRecord(raw) ||
      raw.version !== MOCK_SESSION_VERSION ||
      raw.day !== seoulDateKey(now()) ||
      !isRecord(raw.parts)
    ) {
      discard();
      return 'discarded';
    }
    const saved = raw.parts;
    const apply: (() => void)[] = [];
    for (const [name, part] of Object.entries(parts)) {
      let applier: (() => void) | undefined;
      try {
        applier = part.loadState(saved[name]);
      } catch {
        applier = undefined;
      }
      if (!applier) {
        discard();
        return 'discarded';
      }
      apply.push(applier);
    }
    try {
      for (const run of apply) run();
    } catch {
      // 검사를 통과했는데 적용이 실패하면 저장분을 버린다(다음 새로고침부터 처음 상태).
      discard();
      return 'discarded';
    }
    return 'restored';
  }

  return { save, scheduleSave, restore, discard };
}

export type MockSession = ReturnType<typeof createMockSession>;
