import { SEVERITY_RANK } from '@/features/events/events';
import type { Severity } from '@/features/events/types';
import {
  TBM_DATE,
  TBM_ZONES,
  type LayoutItem,
  type Rect,
  type TaskEval,
  type TbmTask,
} from '@/features/tbm/tbmDemo';

// 레이아웃 · TBM 보고서의 계산. 시간대 겹침, 동시 작업 간섭, 회의록 칸 채우기와 고친 칸 관리를 화면 없이 한다.

/** 'HH:MM' → 분. */
export function toMin(hhmm: string): number {
  const [h = 0, m = 0] = hhmm.split(':').map(Number);
  return h * 60 + m;
}

/** 분 → 'HH:MM'. */
export const fromMin = (n: number) =>
  `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}`;

type TimeSlot = Pick<TbmTask, 'from' | 'to'>;

/** 두 시간대가 겹친다. 한쪽이 끝나는 시각에 다른 쪽이 시작하면 겹치지 않는다. */
export const overlapsTime = (a: TimeSlot, b: TimeSlot) =>
  Math.max(toMin(a.from), toMin(b.from)) < Math.min(toMin(a.to), toMin(b.to));

/** 배치 항목이 차지하는 직사각형들. 동선은 구간마다 폭만큼 넓힌 직사각형이다. */
export function itemRects(item: LayoutItem): Rect[] {
  if (item.kind !== 'path') return [{ x: item.x, y: item.y, w: item.w, h: item.h }];
  const hw = item.w / 2;
  return item.pts.slice(1).map(([bx, by], i) => {
    const [ax, ay] = item.pts[i]!;
    return {
      x: Math.min(ax, bx) - hw,
      y: Math.min(ay, by) - hw,
      w: Math.abs(bx - ax) + item.w,
      h: Math.abs(by - ay) + item.w,
    };
  });
}

/** 두 직사각형이 넓이를 갖고 겹친다(변만 닿으면 겹치지 않는다). */
export const rectHit = (a: Rect, b: Rect) =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

/** 두 직사각형이 겹친 부분. */
export function rectAnd(a: Rect, b: Rect): Rect {
  const x = Math.max(a.x, b.x);
  const y = Math.max(a.y, b.y);
  return { x, y, w: Math.min(a.x + a.w, b.x + b.w) - x, h: Math.min(a.y + a.h, b.y + b.h) - y };
}

/** 동시 작업 간섭 하나. 배치가 겹치면 그 자리(spot)를 같이 둔다. */
export interface Interference {
  a: string;
  b: string;
  from: string;
  to: string;
  sev: Severity;
  why: string;
  spot: Rect | null;
}

/** 동시 작업 간섭: 시간대가 겹치고, 배치가 겹치면 HIGH, 배치는 안 겹치지만 같은 구역이면 CAUTION 이다. */
export function tbmInterferences(tasks: readonly TbmTask[]): Interference[] {
  const out: Interference[] = [];
  for (let i = 0; i < tasks.length; i++) {
    for (let j = i + 1; j < tasks.length; j++) {
      const a = tasks[i]!;
      const b = tasks[j]!;
      const from = Math.max(toMin(a.from), toMin(b.from));
      const to = Math.min(toMin(a.to), toMin(b.to));
      if (from >= to) continue;
      const slot = { a: a.id, b: b.id, from: fromMin(from), to: fromMin(to) };
      const hit = firstHit(a, b);
      if (hit) out.push({ ...slot, sev: 'HIGH', ...hit });
      else if (a.zone && a.zone === b.zone)
        out.push({ ...slot, sev: 'CAUTION', why: `같은 구역 ${a.zone}에서 동시 작업`, spot: null });
    }
  }
  return out;
}

function firstHit(a: TbmTask, b: TbmTask): { why: string; spot: Rect } | null {
  for (const ia of a.items)
    for (const ib of b.items)
      for (const ra of itemRects(ia))
        for (const rb of itemRects(ib))
          if (rectHit(ra, rb))
            return { why: `${ia.label} ↔ ${ib.label} 겹침`, spot: rectAnd(ra, rb) };
  return null;
}

/** 한 작업이 낀 간섭. */
export const interferencesOf = (all: readonly Interference[], taskId: string) =>
  all.filter((x) => x.a === taskId || x.b === taskId);

/** 작업 평가 중 가장 높은 등급. 평가가 없으면 null. */
export function taskPeak(task: Pick<TbmTask, 'evals'>): Severity | null {
  let peak: Severity | null = null;
  for (const e of task.evals) if (!peak || SEVERITY_RANK[e.sev] > SEVERITY_RANK[peak]) peak = e.sev;
  return peak;
}

export const zoneName = (id: string) => {
  const zone = TBM_ZONES.find((z) => z.id === id);
  return zone ? `${zone.name} (${zone.id})` : id;
};

export const taskPlace = (task: Pick<TbmTask, 'zone' | 'area'>) =>
  task.zone ? zoneName(task.zone) : (task.area ?? '구역 밖');

export const SEVERITY_TEXT: Record<Severity, string> = {
  CRITICAL: '▲▲ CRITICAL',
  HIGH: '▲ HIGH',
  CAUTION: '! CAUTION',
  INFO: 'i INFO',
};

export const evalText = (e: TaskEval) =>
  `${e.item} · ${e.rule} · ${SEVERITY_TEXT[e.sev]} · ${e.why}`;

// ---------- TBM 회의록 칸 ----------

/** 회의록 칸 값: 글자, 예·아니오, 비어 있음(null). */
export type TbmFormValue = string | boolean | null;
export type TbmForm = Record<string, TbmFormValue>;
/** 작업마다 고친 칸. */
export type TbmEditsByTask = Record<string, TbmForm>;

/** 잠재위험요인 · 대책 · 안전조치 확인 줄 수(① ~ ③). */
export const FORM_ROWS = 3;

/** 참석자 서명 줄 수. 한 줄에 세 명씩, 적어도 네 줄이다. */
export const attendeeRows = (people: number) => Math.max(4, Math.ceil((people || 1) / 3));

/** 작업 → 회의록 칸(받은 내용). 작업이 없으면 빈 양식이다. */
export function tbmFill(task: TbmTask | null): TbmForm {
  const f: TbmForm = {
    date: '',
    same: null,
    name: '',
    content: '',
    place: '',
    riskDone: null,
    focusRisk: '',
    focusMeasure: '',
    leadOrg: '',
    leadRole: '',
    leadName: '',
    daily: '',
    closing: '',
  };
  for (let n = 0; n < FORM_ROWS; n++)
    Object.assign(f, {
      [`r${n}`]: '',
      [`m${n}`]: '',
      [`c${n}`]: '',
      [`k${n}`]: null,
      [`f${n}`]: '',
    });
  if (!task) return f;
  const items = task.items
    .map((it) => it.label + (it.kind !== 'path' && it.hgt ? ` ${it.hgt}m` : ''))
    .join(', ');
  Object.assign(f, {
    date: `${TBM_DATE} ${task.from} ~ ${task.to}`,
    same: true,
    name: task.name,
    content: `${taskPlace(task)} · ${task.people}명 · 반장 ${task.lead} · ${task.gear} · 배치 ${items || '없음'}`,
    place: taskPlace(task),
    riskDone: task.evals.length > 0,
    leadOrg: `반장 ${task.lead}`,
    leadRole: '반장',
  });
  task.evals.slice(0, FORM_ROWS).forEach((e, n) => {
    f[`r${n}`] = evalText(e);
    f[`c${n}`] = evalText(e);
  });
  task.measures.slice(0, FORM_ROWS).forEach((s, n) => {
    f[`m${n}`] = s;
  });
  // 대책이 있는 위험요인은 조치함(예)으로 채운다.
  for (let n = 0; n < FORM_ROWS; n++) if (f[`r${n}`]) f[`k${n}`] = f[`m${n}`] ? true : null;
  const peak = taskPeak(task);
  if (peak) {
    const k = task.evals.findIndex((e) => e.sev === peak);
    f.focusRisk = evalText(task.evals[k]!);
    f.focusMeasure = task.measures[k] ?? '';
  }
  return f;
}

/** 받은 내용 위에 고친 칸을 덮는다. */
export const formOf = (task: TbmTask, edits: TbmEditsByTask): TbmForm => ({
  ...tbmFill(task),
  ...edits[task.id],
});

/** 칸 하나를 고친다. 받은 값과 같아지면 고친 칸에서 뺀다(고친 칸이 없으면 작업도 뺀다). */
export function withEdit(
  all: TbmEditsByTask,
  taskId: string,
  key: string,
  value: TbmFormValue,
  original: TbmFormValue,
): TbmEditsByTask {
  const task: TbmForm = { ...all[taskId] };
  delete task[key];
  if (value !== original) task[key] = value;
  return withTask(all, taskId, task);
}

/** 그 작업의 고친 칸을 모두 버린다(되돌리기). */
export const withoutEdits = (all: TbmEditsByTask, taskId: string) => withTask(all, taskId, {});

function withTask(all: TbmEditsByTask, taskId: string, task: TbmForm): TbmEditsByTask {
  const next = { ...all };
  if (Object.keys(task).length) next[taskId] = task;
  else delete next[taskId];
  return next;
}

export const hasEdits = (all: TbmEditsByTask, taskId: string) =>
  Object.keys(all[taskId] ?? {}).length > 0;

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

const isFormValue = (v: unknown): v is TbmFormValue =>
  v === null || typeof v === 'string' || typeof v === 'boolean';

/** 브라우저에 남은 고친 칸을 읽는다. 형식이 틀린 칸·작업은 버린다. */
export function parseEdits(value: unknown): TbmEditsByTask {
  if (!isRecord(value)) return {};
  const out: TbmEditsByTask = {};
  for (const [taskId, fields] of Object.entries(value)) {
    if (!isRecord(fields)) continue;
    const task: TbmForm = {};
    for (const [key, v] of Object.entries(fields)) if (isFormValue(v)) task[key] = v;
    if (Object.keys(task).length) out[taskId] = task;
  }
  return out;
}

// ---------- 작업 등록(수동 입력) ----------

export interface TaskInput {
  name: string;
  from: string;
  to: string;
  /** 빈 글자면 구역 밖이다. */
  zone: string;
  people: string;
  lead: string;
}

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

/** 수동 입력으로 작업을 만든다. 배치·평가는 아직 없다. */
export function createTask(tasks: readonly TbmTask[], input: TaskInput): TbmTask {
  const people = Math.round(Number(input.people));
  return {
    id: nextTaskId(tasks),
    name: input.name.trim(),
    from: input.from,
    to: input.to,
    zone: input.zone || null,
    ...(input.zone ? {} : { area: '구역 밖' }),
    people: Number.isFinite(people) && people >= 1 ? Math.min(people, 10) : 1,
    lead: input.lead,
    gear: '장비 미입력',
    items: [],
    evals: [],
    measures: [],
  };
}

/** 보낸 시각 표시('HH:MM'). */
export const clockText = (date: Date) => fromMin(date.getHours() * 60 + date.getMinutes());
