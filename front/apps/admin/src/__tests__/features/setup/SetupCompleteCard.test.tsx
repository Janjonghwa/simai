import {
  appliedZonesSummary,
  canCompleteSetup,
  SetupCompleteCard,
  setupCompleted,
} from '@/features/setup/SetupCompleteCard';
import {
  SETUP_COMPLETE_ERROR_TEXT,
  setupCompleteErrorMessage,
} from '@/features/setup/setupErrorMessage';
import { REBAKE_BLOCKED_LABEL, SUPPLEMENT_ERROR_TEXT } from '@/features/setup/supplement';
import type { SetupSessionResponse } from '@/features/setup/types';
import { ApiError } from '@/lib/http';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

// 세팅 완료(C17, S15P31A101-615): 완료할 수 있는 때, 적용 결과 요약, 완료 뒤 상태 카드, 거부 코드 안내.
// 완료를 부르는 일은 구역 단계 아래 오른쪽 단추가 하며 useCompleteSetup.test.tsx 와 SetupPage 시험이 본다.

afterEach(cleanup);

const COMPLETED_AT_MS = 1_800_000_000_000;
const sessionOf = (patch: Partial<SetupSessionResponse>) =>
  ({ setup_completed_at_ms: null, ...patch }) as SetupSessionResponse;

describe('setupCompleted·canCompleteSetup·appliedZonesSummary', () => {
  const base = sessionOf({});
  it('완료 시각이 있으면 완료한 세션이다', () => {
    expect(setupCompleted(base)).toBe(false);
    expect(setupCompleted({ ...base, setup_completed_at_ms: 1 })).toBe(true);
    expect(setupCompleted(null)).toBe(false);
  });

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

describe('세팅 완료 상태 카드', () => {
  it('완료 전 세션이면 아무것도 그리지 않는다(완료 단추·설명도 없다)', () => {
    render(<SetupCompleteCard session={sessionOf({ status: 'READY_FOR_ZONES' })} />);
    expect(screen.queryByTestId('setup-complete-card')).toBeNull();
    expect(screen.queryByRole('button', { name: '세팅 완료' })).toBeNull();
  });

  it('완료한 세션은 완료 시각과 안내를 보이고, 방금 받은 적용 결과가 없으면 개수 줄은 없다', () => {
    render(
      <SetupCompleteCard
        session={sessionOf({ status: 'COMPLETED', setup_completed_at_ms: COMPLETED_AT_MS })}
      />,
    );
    expect(screen.getByRole('heading', { name: '세팅 완료' })).toBeTruthy();
    expect(screen.getByRole('status').textContent).toMatch(/에 세팅을 완료했습니다$/);
    expect(screen.getByText(/구역 수정은 관제 화면의 구역 편집에서/)).toBeTruthy();
    expect(screen.queryByText(/구역 적용:/)).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('방금 완료한 화면은 적용 결과 개수를 함께 보인다', () => {
    render(
      <SetupCompleteCard
        session={sessionOf({ status: 'COMPLETED', setup_completed_at_ms: COMPLETED_AT_MS })}
        applied={{ created: ['a'], updated: [], kept: ['b', 'c'], archived: [] }}
      />,
    );
    expect(screen.getByText('구역 적용: 새로 1 · 고침 0 · 유지 2 · 보관 0')).toBeTruthy();
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
