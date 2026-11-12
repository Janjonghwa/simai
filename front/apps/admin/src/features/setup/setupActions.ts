import {
  ACTION_LABEL,
  SECONDARY_ACTIONS,
  ZONE_STAGE,
  type ActionKey,
  type Stage,
} from '@/features/setup/setup';
import type { WorkActionConfig } from '@/features/work/WorkActionContext';

// 세팅 화면의 단추 설정을 한 곳에서 만든다. 단독 화면(/setup)은 머리줄에 그리고, 날짜 화면 안(embedded)은 같은 설정을 아래 오른쪽
// 단추 줄(useWorkAction)에 등록한다. 두 화면의 라벨·켜짐·순서가 어긋나지 않게 단추를 여기서만 정한다.

const STAGE_ACTIONS: Record<Stage, readonly ActionKey[]> = {
  0: ['start'],
  1: ['confirm'],
  2: ['restart'],
};

/** 정밀 3D 맵을 다 만들기 전에는 2단계 '다음' 이 꺼진다. */
const CONFIRM_BLOCKED_TITLE = '3D 맵을 다 만들면 넘어갈 수 있어요';
const CANCEL_LABEL = '세팅 취소';
const RECHECK_LABEL = '카메라 다시 맞추기';
/** 단독 화면의 구역 단계 마지막 단추: 세팅을 완료한 뒤 그 작업의 작업 관리로 돌아간다. */
const TO_WORK_LABEL = '작업 관리로';
/** 날짜 화면 안의 구역 단계 마지막 단추: 세팅을 완료한 뒤 다음 단계로 넘어간다. */
const EMBEDDED_DONE_LABEL = '다음';

/** 세팅 화면 단추 하나. 주 단추(다음 단계로 가는 것)는 맨 오른쪽에 하나, 나머지는 보조 단추다. */
export interface SetupActionButton {
  key: string;
  label: string;
  onClick: () => void;
  disabled: boolean;
  primary: boolean;
  /** 꺼진 이유(꺼진 단추에 마우스를 올렸을 때). */
  title?: string;
}

/** 단추를 정하는 화면 상태. */
export interface SetupActionState {
  stage: Stage;
  /** 날짜 화면 안인지. 구역 단계의 마지막 단추가 '다음' 이 된다. */
  embedded: boolean;
  /** 1단계 '다음' 이 꺼지는 때(카메라 높이가 틀렸거나 세션 요청 중). */
  startDisabled: boolean;
  /** 3D 맵이 완전히 만들어졌다(정밀 3D 맵까지 끝나야 2단계 '다음' 이 켜진다). */
  mapComplete: boolean;
  /** 세션이 아직 진행 중이라 취소할 수 있다. */
  sessionRunning: boolean;
  /** 세션 취소 요청 중. */
  cancelPending: boolean;
  /** 높이 불일치 안내가 떠서 '카메라 다시 맞추기' 가 있다. */
  showHeightNotice: boolean;
  /** 구역 단계 마지막 단추를 누를 수 있다(세팅을 이미 완료했거나 지금 완료할 수 있다). */
  canFinish: boolean;
  /** 마지막 단추가 세팅 완료를 요청하는 중. */
  finishPending: boolean;
  /** 세팅을 완료했다(하루 1회). 완료한 작업은 다시 보정할 수 없다. */
  setupCompleted: boolean;
}

export interface SetupActionHandlers extends Record<ActionKey, () => void> {
  cancelSession: () => void;
  recheckCameras: () => void;
  /** 구역 단계 마지막 단추(아직 완료 전이면 세팅을 완료하고 넘어간다). */
  finish: () => void;
}

/**
 * 지금 단계에서 보일 단추들을 왼쪽부터 순서대로 돌려준다(보조 단추 먼저, 주 단추가 맨 오른쪽이라 단계가 바뀌어도 자리가 같다).
 */
export function deriveSetupActions(
  state: SetupActionState,
  on: SetupActionHandlers,
): SetupActionButton[] {
  const disabledOf: Record<ActionKey, boolean> = {
    start: state.startDisabled,
    confirm: !state.mapComplete,
    restart: false,
  };
  const staged = STAGE_ACTIONS[state.stage]
    .filter((key) => !(key === 'restart' && state.setupCompleted))
    .map((key): SetupActionButton => ({
      key,
      label: ACTION_LABEL[key],
      onClick: on[key],
      disabled: disabledOf[key],
      primary: !SECONDARY_ACTIONS.has(key),
      title: key === 'confirm' && disabledOf[key] ? CONFIRM_BLOCKED_TITLE : undefined,
    }));

  const buttons = staged.filter((button) => !button.primary);
  if (state.stage === 1 && state.sessionRunning) {
    buttons.push({
      key: 'cancel',
      label: CANCEL_LABEL,
      onClick: on.cancelSession,
      disabled: state.cancelPending,
      primary: false,
    });
  }
  if (state.stage === 1 && state.showHeightNotice) {
    buttons.push({
      key: 'recheck',
      label: RECHECK_LABEL,
      onClick: on.recheckCameras,
      disabled: false,
      primary: false,
    });
  }
  buttons.push(...staged.filter((button) => button.primary));
  if (state.stage === ZONE_STAGE) {
    buttons.push({
      key: 'finish',
      label: state.embedded ? EMBEDDED_DONE_LABEL : TO_WORK_LABEL,
      onClick: on.finish,
      disabled: !state.canFinish || state.finishPending,
      primary: true,
    });
  }
  return buttons;
}

/** 날짜 화면 아래 오른쪽 단추 줄에 등록할 모양(주 단추 하나 + 보조 단추들). 주 단추가 없으면 null. */
export function toWorkAction(buttons: readonly SetupActionButton[]): WorkActionConfig | null {
  const main = buttons.find((button) => button.primary);
  if (!main) return null;
  return {
    label: main.label,
    enabled: !main.disabled,
    onClick: main.onClick,
    title: main.title,
    secondary: buttons
      .filter((button) => !button.primary)
      .map(({ label, onClick, disabled }) => ({ label, onClick, disabled })),
  };
}
