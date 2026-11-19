import type { PlanBounds, PlanZone } from '@/features/plan/plan';
import { nextMinimapView, type MinimapView } from '@/features/plan/planFit';
import { nextHeadings, NO_HEADINGS, type HeadingMemory } from '@/features/plan/planHeading';
import type { WorldTrack } from '@simai/contracts';
import { useState } from 'react';

// world 가 올 때마다(10Hz) 앞 상태에 이어 붙는 2D 평면 표시 상태(-800): 사람 진행 방향 기억, 미니맵 범위.
// 계산은 planHeading·planFit 의 순수 함수가 하고, 여기서는 앞 상태를 들고만 있다.

/**
 * 입력이 바뀔 때만 앞 상태와 이번 입력으로 다음 상태를 만든다. ref 를 렌더 중에 읽지 않으려고
 * React 문서의 「이전 렌더의 정보 저장」 방식으로 바뀐 렌더에서 상태를 바로 고친다(같은 입력이면 다시 계산하지 않는다).
 * step 이 앞 상태 객체를 그대로 돌려주면 상태를 고치지 않는다. 입력 참조가 렌더마다 바뀌어도 다시 그리기가 끝없이 돌지 않는다.
 */
function useStepState<I, S>(
  input: I,
  same: (a: I, b: I) => boolean,
  step: (prev: S | null, input: I) => S,
): S {
  const [held, setHeld] = useState<{ input: I; state: S }>(() => ({
    input,
    state: step(null, input),
  }));
  if (!same(held.input, input)) {
    const state = step(held.state, input);
    if (state !== held.state) setHeld({ input, state });
    return state;
  }
  return held.state;
}

interface HeadingInput {
  tracks: readonly WorldTrack[];
  /** 이번 world 시각(t_latest_global, ms). world 를 받기 전이면 null. */
  t: number | null;
}

/** world 마다 시각(t_latest_global)이 바뀌므로 시각만 본다(같은 시각에 다시 계산하지 않는다). */
const sameFrame = (a: HeadingInput, b: HeadingInput) => a.t === b.t;

const stepHeadings = (prev: HeadingMemory | null, { tracks, t }: HeadingInput) =>
  t === null ? NO_HEADINGS : nextHeadings(prev ?? NO_HEADINGS, tracks, t);

/** 사람(world_id)마다 진행 방향 기억. 표시용이고 Tier 0 값을 바꾸지 않는다. */
export function usePlanHeadings(tracks: readonly WorldTrack[], t: number | null): HeadingMemory {
  return useStepState({ tracks, t }, sameFrame, stepHeadings);
}

interface ViewInput extends HeadingInput {
  enabled: boolean;
  zones: readonly PlanZone[];
  bounds: PlanBounds;
}

const sameViewInput = (a: ViewInput, b: ViewInput) =>
  (!a.enabled && !b.enabled) ||
  (a.enabled === b.enabled &&
    a.zones === b.zones &&
    a.bounds === b.bounds &&
    a.tracks === b.tracks &&
    a.t === b.t);

const stepView = (prev: MinimapView | null, input: ViewInput): MinimapView | null =>
  input.enabled ? nextMinimapView(prev, input, input.t ?? 0) : null;

/** 미니맵이 비출 World 범위(구역·사람에 맞춤, 흔들림 막기). enabled 가 아니면 없다(방 전체를 비춘다). */
export function useMinimapView(input: ViewInput): PlanBounds | undefined {
  return useStepState<ViewInput, MinimapView | null>(input, sameViewInput, stepView)?.view;
}
