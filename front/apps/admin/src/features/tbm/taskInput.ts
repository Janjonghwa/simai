import {
  GEAR_UNSET_TEXT,
  MAX_PEOPLE,
  MIN_PEOPLE,
  OUTSIDE_ZONE_TEXT,
} from '@/features/tbm/constants';
import { toMin } from '@/features/tbm/tbm';
import type { TbmTask } from '@/features/tbm/tbmDemo';

// 작업 등록(수동 입력). 입력값 검사와 새 작업 만들기를 화면 없이 한다.

export interface TaskInput {
  name: string;
  from: string;
  to: string;
  /** 빈 글자면 구역 밖이다. */
  zone: string;
  people: string;
  lead: string;
}

/** 등록 창을 열 때 채워 두는 값. */
export const TASK_INPUT_DEFAULT: TaskInput = {
  name: '',
  from: '13:00',
  to: '15:00',
  zone: 'z-02',
  people: '2',
  lead: 'B조',
};

/** 이름이 있고 끝 시각이 시작보다 늦어야 등록할 수 있다. */
export const taskInputValid = (input: TaskInput) =>
  input.name.trim() !== '' &&
  input.from !== '' &&
  input.to !== '' &&
  toMin(input.from) < toMin(input.to);

/** 다음 작업 번호(T1, T2 … 중 가장 큰 번호 + 1). */
export function nextTaskId(tasks: readonly Pick<TbmTask, 'id'>[]): string {
  const max = tasks.reduce((n, t) => Math.max(n, Number(t.id.slice(1)) || 0), 0);
  return `T${max + 1}`;
}

/** 입력한 인원을 정수로 고르고 한도 안으로 맞춘다. 숫자가 아니면 최소 인원이다. */
function clampPeople(text: string): number {
  const people = Math.round(Number(text));
  return Number.isFinite(people) && people >= MIN_PEOPLE
    ? Math.min(people, MAX_PEOPLE)
    : MIN_PEOPLE;
}

/** 수동 입력으로 작업을 만든다. 배치·평가는 아직 없다. */
export function createTask(tasks: readonly TbmTask[], input: TaskInput): TbmTask {
  return {
    id: nextTaskId(tasks),
    name: input.name.trim(),
    from: input.from,
    to: input.to,
    zone: input.zone || null,
    ...(input.zone ? {} : { area: OUTSIDE_ZONE_TEXT }),
    people: clampPeople(input.people),
    lead: input.lead,
    gear: GEAR_UNSET_TEXT,
    items: [],
    evals: [],
    measures: [],
  };
}
