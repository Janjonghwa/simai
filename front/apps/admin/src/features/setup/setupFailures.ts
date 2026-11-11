import { pairLabel } from '@/features/setup/cameraLabels';

// 서버 세팅 세션이 실패한 이유(failure_code)마다 무엇이 문제이고 어디서 어떻게 풀지 보이는 안내.
// retry 가 false 면 같은 상태로 다시 해도 같은 곳에서 멈추므로 '다시 시도' 없이 '카메라 연결 관리로' 만 둔다
// (서버는 FAILED 세션이면 늘 다시 시도를 받는다). 표에 없는 코드는 FALLBACK 이다.

export interface FailureEntry {
  /** 원인 한 줄. 실패 메시지에서 뽑은 카메라 쌍('캠1–캠2, 캠3–캠4')이 있으면 받는다. */
  title: (pairs: string) => string;
  /** 해결 방법. */
  fix: string;
  /** 같은 상태로 다시 시도해 볼 만한지. */
  retry: boolean;
}

export interface FailureGuide {
  title: string;
  fix: string;
  retry: boolean;
}

export const SETUP_FAILURES: Readonly<Record<string, FailureEntry>> = {
  BACKGROUND_INCOMPLETE: {
    title: () => '사람 없는 배경을 다 모으지 못했어요',
    fix: '카메라 앞에서 사람과 장비가 비킨 상태로 다시 시도하세요.',
    retry: true,
  },
  FLOOR_FIT_FAILED: {
    title: () => '바닥을 찾지 못했어요',
    fix: '카메라 연결 관리에서 카메라 높이를 확인하고, 바닥이 영상에 넓게 보이게 맞춘 뒤 다시 시작하세요.',
    retry: false,
  },
  INSUFFICIENT_FEATURES: {
    title: (pairs) =>
      pairs
        ? `옆 카메라 ${pairs} 사이에 같이 보이는 곳이 적어요`
        : '옆 카메라끼리 같이 보이는 곳이 적어요',
    fix: '카메라 연결 관리로 돌아가 두 카메라를 서로 쪽으로 돌리거나, 겹치는 곳에 무늬 있는 물건을 두세요.',
    retry: false,
  },
  VALIDATION_REJECTED: {
    title: (pairs) =>
      pairs
        ? `보정한 뒤 오히려 어긋난 카메라 쌍이 있어요 (${pairs})`
        : '보정 결과가 검증을 통과하지 못했어요',
    fix: '카메라와 거치대를 단단히 고정하고 위치를 다시 맞춘 뒤 처음부터 다시 하세요.',
    retry: false,
  },
  TIMEOUT: {
    title: () => '시간이 너무 오래 걸렸어요 (15분)',
    fix: '잠시 뒤 다시 시도하세요. 계속되면 관리자에게 알려 주세요.',
    retry: true,
  },
  CANCELLED_BY_ADMIN: {
    title: () => '세팅을 취소했어요',
    fix: '다시 시작하려면 다시 시도하세요.',
    retry: true,
  },
  LEASE_EXPIRED: {
    title: () => '분석 서버와 연결이 끊겼어요',
    fix: '분석 서버가 켜져 있는지 확인한 뒤 다시 시도하세요.',
    retry: true,
  },
};

const FALLBACK: FailureEntry = {
  title: () => '서버에서 세팅을 끝내지 못했어요',
  fix: '잠시 뒤 다시 시도하세요. 계속되면 관리자에게 알려 주세요.',
  retry: true,
};

const WORSENED_PAIRS = /worsened_pairs=\[([^\]]*)\]/;

const escapeRegExp = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * 실패 메시지 안의 `cam_B-cam_C`·`cam1-cam2` 꼴에서 카메라 id 목록에 있는 쌍만 메시지에 나온 순서로 뽑는다.
 * id 에 '-' 가 있을 수 있어 글자를 쪼개지 않고 알려진 id 조합을 찾는다. `worsened_pairs=[...]` 가 있으면 그 안만 본다.
 */
function pairsInMessage(message: string, cameraIds: readonly string[]): [string, string][] {
  const text = WORSENED_PAIRS.exec(message)?.[1] ?? message;
  const found: { index: number; pair: [string, string] }[] = [];
  for (const a of cameraIds) {
    for (const b of cameraIds) {
      if (a === b) continue;
      const word = '[A-Za-z0-9_-]';
      const at = new RegExp(`(?<!${word})${escapeRegExp(`${a}-${b}`)}(?!${word})`).exec(text);
      if (at) found.push({ index: at.index, pair: [a, b] });
    }
  }
  return found.sort((x, y) => x.index - y.index).map((f) => f.pair);
}

/** 서버 failure_code·failure_message 를 사용자 안내(원인·해결·다시 시도 가능 여부)로 바꾼다. */
export function failureGuide(
  code: string | null,
  message: string | null,
  cameraIds: readonly string[],
): FailureGuide {
  const entry =
    (code !== null && Object.hasOwn(SETUP_FAILURES, code) ? SETUP_FAILURES[code] : undefined) ??
    FALLBACK;
  const pairs = pairsInMessage(message ?? '', cameraIds)
    .map(([a, b]) => pairLabel(cameraIds, a, b))
    .join(', ');
  return { title: entry.title(pairs), fix: entry.fix, retry: entry.retry };
}
