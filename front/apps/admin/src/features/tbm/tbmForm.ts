import {
  ATTENDEES_PER_ROW,
  FORM_ROWS,
  MIN_ATTENDEE_ROWS,
  OUTSIDE_ZONE_TEXT,
} from '@/features/tbm/constants';
import { evalText, itemHeight, taskPeak, taskPlace } from '@/features/tbm/tbm';
import { TBM_DATE, TBM_ZONES, type TbmTask } from '@/features/tbm/tbmDemo';
import {
  extractReportMeasures,
  msToHhmm,
  riskItemToEval,
  type TbmReport,
} from '@/features/tbm/types';

// TBM 회의록 칸. 작업·보고서로 칸을 채우고, 사람이 고친 칸을 받은 값 위에 덮어 관리한다(화면 없이).

/** 회의록 칸 값: 글자, 예·아니오, 비어 있음(null). */
export type TbmFormValue = string | boolean | null;
export type TbmForm = Record<string, TbmFormValue>;
/** 작업마다 고친 칸. */
export type TbmEditsByTask = Record<string, TbmForm>;

/** 참석자 서명 줄 수. 한 줄에 세 명씩, 적어도 네 줄이다. */
export const attendeeRows = (people: number) =>
  Math.max(MIN_ATTENDEE_ROWS, Math.ceil((people || 1) / ATTENDEES_PER_ROW));

function formatKoreanDate(isoDate?: string | null): string {
  if (!isoDate) return TBM_DATE;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!m) return isoDate;
  return `${Number(m[1])}년 ${Number(m[2])}월 ${Number(m[3])}일`;
}

function headerPlaceText(zoneId: string | null, place?: string | null): string {
  if (zoneId) {
    const z = TBM_ZONES.find((zone) => zone.id === zoneId);
    return z ? `${z.name} (${z.id})` : zoneId;
  }
  return place || OUTSIDE_ZONE_TEXT;
}

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

/** 일시 · 작업명 · 장소 · 리더 같은 머리 칸. report.header 가 있으면 우선 반영한다. */
function fillHeader(form: TbmForm, task: TbmTask, report?: TbmReport | null) {
  const items = task.items
    .map((item) => {
      const height = itemHeight(item);
      return item.label + (height ? ` ${height}m` : '');
    })
    .join(', ');

  const hdr = report?.header;
  const dateLabel = formatKoreanDate(hdr?.date ?? task.date);
  const from = hdr ? msToHhmm(hdr.from_ms) : task.from;
  const to = hdr ? msToHhmm(hdr.to_ms) : task.to;
  const name = hdr?.name ?? task.name;
  const place = hdr ? headerPlaceText(hdr.zone_id, hdr.place) : taskPlace(task);
  const people = hdr?.planned_people ?? task.people;
  const lead = hdr?.lead ?? task.lead;
  const riskDone = report
    ? report.status === 'SUCCEEDED' && report.risk_items.length > 0
    : task.evals.length > 0;

  Object.assign(form, {
    date: `${dateLabel} ${from} ~ ${to}`,
    same: true,
    name,
    content: `${place} · ${people}명 · 반장 ${lead} · ${task.gear} · 배치 ${items || '없음'}`,
    place,
    riskDone,
    leadOrg: `반장 ${lead}`,
    leadRole: '반장',
  });
}

/** 평가·보고서(risk_items, sections, citations)를 잠재위험요인 · 대책 · 안전조치 줄과 중점위험 칸에 옮긴다. */
function fillRisks(form: TbmForm, task: TbmTask, report?: TbmReport | null) {
  // FAILED 또는 GENERATING 보고서는 지어낸 문장으로 대책·법령을 채우지 않는다.
  if (report && report.status !== 'SUCCEEDED') {
    const evals = report.risk_items.map(riskItemToEval);
    evals.slice(0, FORM_ROWS).forEach((e, n) => {
      form[`r${n}`] = evalText(e);
      form[`c${n}`] = evalText(e);
    });
    return;
  }

  const evals =
    report && report.risk_items.length > 0 ? report.risk_items.map(riskItemToEval) : task.evals;
  const reportMeasures = extractReportMeasures(report);
  const measures = reportMeasures.length > 0 ? reportMeasures : task.measures;

  evals.slice(0, FORM_ROWS).forEach((e, n) => {
    form[`r${n}`] = evalText(e);
    form[`c${n}`] = evalText(e);
  });
  measures.slice(0, FORM_ROWS).forEach((measure, n) => {
    form[`m${n}`] = measure;
  });
  // 대책이 있는 위험요인은 조치함(예)으로 채운다.
  for (let n = 0; n < FORM_ROWS; n++)
    if (form[`r${n}`]) form[`k${n}`] = form[`m${n}`] ? true : null;

  const syntheticTask: TbmTask = { ...task, evals, measures };
  const peak = taskPeak(syntheticTask);
  if (peak) {
    const worst = evals.findIndex((e) => e.sev === peak);
    form.focusRisk = evalText(evals[worst]!);
    const focusSec = report?.sections.find((s) => s.key === 'focus');
    form.focusMeasure = focusSec?.body.trim() || measures[worst] || measures[0] || '';
  }

  if (report && report.status === 'SUCCEEDED') {
    const legalSec = report.sections.find((s) => s.key === 'legal');
    const citationRefs = report.citations
      .map((c) => `${c.source_name} ${c.article_ref ?? ''} (${c.title})`.trim())
      .join(', ');
    if (legalSec?.body || citationRefs) {
      form.daily = [legalSec?.body?.trim(), citationRefs ? `[인용 조문] ${citationRefs}` : '']
        .filter(Boolean)
        .join('\n');
    }
  }
}

/** 작업을 끝낼 때 서버가 쓴 작업 마무리(wrap_up_summary)를 '작업 후 종료 미팅' 칸에 옮긴다. 아직 없으면 비워 둔다. */
function fillClosing(form: TbmForm, report?: TbmReport | null) {
  const summary = report?.wrap_up_summary?.trim();
  if (summary) form.closing = summary;
}

/** 작업(+최신 보고서) → 회의록 칸(받은 내용). 작업이 없으면 빈 양식이다. */
export function tbmFill(task: TbmTask | null, report?: TbmReport | null): TbmForm {
  const form = emptyForm();
  if (!task) return form;
  fillHeader(form, task, report);
  fillRisks(form, task, report);
  fillClosing(form, report);
  return form;
}

/** 받은 내용 위에 고친 칸을 덮는다. */
export const formOf = (
  task: TbmTask,
  edits: TbmEditsByTask,
  report?: TbmReport | null,
): TbmForm => ({
  ...tbmFill(task, report),
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
