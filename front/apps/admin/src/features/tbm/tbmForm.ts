import { ATTENDEES_PER_ROW, FORM_ROWS, MIN_ATTENDEE_ROWS } from '@/features/tbm/constants';
import { evalText, itemHeight, taskPeak, taskPlace } from '@/features/tbm/tbm';
import { TBM_DATE, type TbmTask } from '@/features/tbm/tbmDemo';

// TBM 회의록 칸. 작업으로 칸을 채우고, 사람이 고친 칸을 받은 값 위에 덮어 관리한다(화면 없이).

/** 회의록 칸 값: 글자, 예·아니오, 비어 있음(null). */
export type TbmFormValue = string | boolean | null;
export type TbmForm = Record<string, TbmFormValue>;
/** 작업마다 고친 칸. */
export type TbmEditsByTask = Record<string, TbmForm>;

/** 참석자 서명 줄 수. 한 줄에 세 명씩, 적어도 네 줄이다. */
export const attendeeRows = (people: number) =>
  Math.max(MIN_ATTENDEE_ROWS, Math.ceil((people || 1) / ATTENDEES_PER_ROW));

/** 아무것도 채우지 않은 빈 양식. */
function emptyForm(): TbmForm {
  const form: TbmForm = {
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
    Object.assign(form, {
      [`r${n}`]: '',
      [`m${n}`]: '',
      [`c${n}`]: '',
      [`k${n}`]: null,
      [`f${n}`]: '',
    });
  return form;
}

/** 일시 · 작업명 · 장소 · 리더 같은 머리 칸. */
function fillHeader(form: TbmForm, task: TbmTask) {
  const items = task.items
    .map((item) => {
      const height = itemHeight(item);
      return item.label + (height ? ` ${height}m` : '');
    })
    .join(', ');
  Object.assign(form, {
    date: `${TBM_DATE} ${task.from} ~ ${task.to}`,
    same: true,
    name: task.name,
    content: `${taskPlace(task)} · ${task.people}명 · 반장 ${task.lead} · ${task.gear} · 배치 ${items || '없음'}`,
    place: taskPlace(task),
    riskDone: task.evals.length > 0,
    leadOrg: `반장 ${task.lead}`,
    leadRole: '반장',
  });
}

/** 평가를 잠재위험요인 · 대책 · 안전조치 줄과 중점위험 칸에 옮긴다. */
function fillRisks(form: TbmForm, task: TbmTask) {
  task.evals.slice(0, FORM_ROWS).forEach((e, n) => {
    form[`r${n}`] = evalText(e);
    form[`c${n}`] = evalText(e);
  });
  task.measures.slice(0, FORM_ROWS).forEach((measure, n) => {
    form[`m${n}`] = measure;
  });
  // 대책이 있는 위험요인은 조치함(예)으로 채운다.
  for (let n = 0; n < FORM_ROWS; n++)
    if (form[`r${n}`]) form[`k${n}`] = form[`m${n}`] ? true : null;
  const peak = taskPeak(task);
  if (!peak) return;
  const worst = task.evals.findIndex((e) => e.sev === peak);
  form.focusRisk = evalText(task.evals[worst]!);
  form.focusMeasure = task.measures[worst] ?? '';
}

/** 작업 → 회의록 칸(받은 내용). 작업이 없으면 빈 양식이다. */
export function tbmFill(task: TbmTask | null): TbmForm {
  const form = emptyForm();
  if (!task) return form;
  fillHeader(form, task);
  fillRisks(form, task);
  return form;
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
