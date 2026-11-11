import {
  deriveSetupActions,
  toWorkAction,
  type SetupActionHandlers,
  type SetupActionState,
} from '@/features/setup/setupActions';
import { describe, expect, it, vi } from 'vitest';

const handlers = (): SetupActionHandlers => ({
  start: vi.fn(),
  confirm: vi.fn(),
  restart: vi.fn(),
  cancelSession: vi.fn(),
  recheckCameras: vi.fn(),
  finish: vi.fn(),
});

const stateOf = (overrides: Partial<SetupActionState> = {}): SetupActionState => ({
  stage: 0,
  embedded: false,
  startDisabled: false,
  previewReady: false,
  sessionRunning: false,
  cancelPending: false,
  showHeightNotice: false,
  zonesPublished: false,
  ...overrides,
});

/** 왼쪽부터 `라벨` 또는 꺼진 단추는 `라벨(꺼짐)`. 주 단추는 `*` 를 붙인다. */
const summary = (state: SetupActionState) =>
  deriveSetupActions(state, handlers()).map(
    (button) => `${button.primary ? '*' : ''}${button.label}${button.disabled ? '(꺼짐)' : ''}`,
  );

describe('deriveSetupActions: 단계별 단추', () => {
  it('1단계는 주 단추 하나(다음)이고 높이가 틀렸거나 요청 중이면 꺼진다', () => {
    expect(summary(stateOf({ stage: 0 }))).toEqual(['*다음']);
    expect(summary(stateOf({ stage: 0, startDisabled: true }))).toEqual(['*다음(꺼짐)']);
  });

  it('2단계는 맵이 준비돼야 다음이 켜지고, 진행 중 세션은 세팅 취소가 먼저 있다', () => {
    expect(summary(stateOf({ stage: 1 }))).toEqual(['*다음(꺼짐)']);
    expect(summary(stateOf({ stage: 1, previewReady: true }))).toEqual(['*다음']);
    expect(summary(stateOf({ stage: 1, sessionRunning: true }))).toEqual([
      '세팅 취소',
      '*다음(꺼짐)',
    ]);
    expect(summary(stateOf({ stage: 1, sessionRunning: true, cancelPending: true }))).toEqual([
      '세팅 취소(꺼짐)',
      '*다음(꺼짐)',
    ]);
  });

  it('높이 불일치 안내가 있으면 세팅 취소 뒤에 카메라 다시 맞추기가 놓이고 주 단추는 맨 오른쪽이다', () => {
    expect(
      summary(
        stateOf({ stage: 1, sessionRunning: true, showHeightNotice: true, previewReady: true }),
      ),
    ).toEqual(['세팅 취소', '카메라 다시 맞추기', '*다음']);
  });

  it('세션 취소·카메라 다시 맞추기는 2단계에만 있다', () => {
    const extras = { sessionRunning: true, showHeightNotice: true };
    expect(summary(stateOf({ stage: 0, ...extras }))).toEqual(['*다음']);
    expect(summary(stateOf({ stage: 2, ...extras }))).toEqual(['다시 보정']);
  });
});

describe('deriveSetupActions: 구역 단계', () => {
  it('단독 화면은 구역을 발행하기 전에는 다시 보정만 있고, 발행하면 맨 오른쪽에 작업 관리로가 생긴다', () => {
    expect(summary(stateOf({ stage: 2 }))).toEqual(['다시 보정']);
    expect(summary(stateOf({ stage: 2, zonesPublished: true }))).toEqual([
      '다시 보정',
      '*작업 관리로',
    ]);
  });

  it('날짜 화면 안은 마지막 단추가 다음이고 구역을 발행하기 전에는 이유와 함께 꺼져 있다', () => {
    const before = deriveSetupActions(stateOf({ stage: 2, embedded: true }), handlers());
    expect(before.map((button) => button.label)).toEqual(['다시 보정', '다음']);
    expect(before[1]).toMatchObject({ primary: true, disabled: true });
    expect(before[1]?.title).toBeTruthy();

    const after = deriveSetupActions(
      stateOf({ stage: 2, embedded: true, zonesPublished: true }),
      handlers(),
    );
    expect(after[1]).toMatchObject({ label: '다음', primary: true, disabled: false });
    expect(after[1]?.title).toBeUndefined();
  });

  it('embedded 여부는 구역 단계 밖의 단추를 바꾸지 않는다', () => {
    for (const stage of [0, 1] as const) {
      const state = { stage, previewReady: true, sessionRunning: true, showHeightNotice: true };
      expect(summary(stateOf({ ...state, embedded: true }))).toEqual(summary(stateOf(state)));
    }
  });

  it('단추마다 알맞은 처리를 부른다', () => {
    const on = handlers();
    const run = (state: SetupActionState, label: string) =>
      deriveSetupActions(state, on)
        .find((button) => button.label === label)
        ?.onClick();

    run(stateOf({ stage: 0 }), '다음');
    expect(on.start).toHaveBeenCalledTimes(1);
    run(stateOf({ stage: 1 }), '다음');
    expect(on.confirm).toHaveBeenCalledTimes(1);
    run(stateOf({ stage: 1, sessionRunning: true }), '세팅 취소');
    expect(on.cancelSession).toHaveBeenCalledTimes(1);
    run(stateOf({ stage: 1, showHeightNotice: true }), '카메라 다시 맞추기');
    expect(on.recheckCameras).toHaveBeenCalledTimes(1);
    run(stateOf({ stage: 2 }), '다시 보정');
    expect(on.restart).toHaveBeenCalledTimes(1);
    run(stateOf({ stage: 2, embedded: true, zonesPublished: true }), '다음');
    run(stateOf({ stage: 2, zonesPublished: true }), '작업 관리로');
    expect(on.finish).toHaveBeenCalledTimes(2);
  });
});

describe('toWorkAction: 아래 오른쪽 단추 줄 설정', () => {
  it('주 단추는 label·enabled·onClick·title 로, 나머지는 왼쪽 보조 단추로 옮긴다', () => {
    const on = handlers();
    const config = toWorkAction(
      deriveSetupActions(
        stateOf({ stage: 1, sessionRunning: true, showHeightNotice: true, previewReady: true }),
        on,
      ),
    );
    expect(config).toMatchObject({ label: '다음', enabled: true });
    expect(config?.secondary?.map((item) => item.label)).toEqual([
      '세팅 취소',
      '카메라 다시 맞추기',
    ]);
    config?.onClick();
    config?.secondary?.[0]?.onClick();
    expect(on.confirm).toHaveBeenCalledTimes(1);
    expect(on.cancelSession).toHaveBeenCalledTimes(1);
  });

  it('보조 단추의 꺼짐(disabled)도 그대로 넘긴다', () => {
    const config = toWorkAction(
      deriveSetupActions(
        stateOf({
          stage: 1,
          sessionRunning: true,
          cancelPending: true,
          showHeightNotice: true,
          previewReady: true,
        }),
        handlers(),
      ),
    );
    expect(config?.secondary?.map((item) => [item.label, item.disabled])).toEqual([
      ['세팅 취소', true],
      ['카메라 다시 맞추기', false],
    ]);
  });

  it('꺼진 주 단추는 enabled false 와 이유를 그대로 넘긴다', () => {
    const config = toWorkAction(
      deriveSetupActions(stateOf({ stage: 2, embedded: true }), handlers()),
    );
    expect(config).toMatchObject({ label: '다음', enabled: false });
    expect(config?.title).toBeTruthy();
    expect(config?.secondary?.map((item) => item.label)).toEqual(['다시 보정']);
  });

  it('주 단추가 없으면 null 이다', () => {
    expect(toWorkAction(deriveSetupActions(stateOf({ stage: 2 }), handlers()))).toBeNull();
  });
});
