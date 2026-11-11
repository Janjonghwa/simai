import {
  reportsByWorkSchema,
  SIGNATURE_MAX_BYTES,
  signatureResultSchema,
  workDayItemSchema,
  workDaysResponseSchema,
  workEndResponseSchema,
  workReportSchema,
  workSignaturesResponseSchema,
  workTasksResponseSchema,
  type ReportFormSaveBody,
  type ReportsByWork,
  type SignatureRequestBody,
  type SignatureResult,
  type WorkDayItem,
  type WorkDaysRange,
  type WorkEndResponse,
  type WorkReport,
  type WorkSignature,
  type WorkTask,
} from '@/features/work/types';
import { ApiError, type Http } from '@/lib/http';
import { z } from 'zod';

// 작업 관리 새 흐름 REST 요청. http 를 인자로 받는다(테스트가 env 없이 createHttp 로 부를 수 있게 한다).
// 쿼리 키는 여러 기능이 함께 무효화하므로 lib/queryKeys.ts 에 있다.

/** 보고서 초안·세팅 끝 기다리는 동안 보고서를 다시 받는 주기(ms). */
export const WORK_REPORT_POLL_MS = 2_000;
/** 세팅 중인 날 작업 항목(progress)을 다시 받는 주기(ms). 서버가 SETUP → SETUP_DONE 으로 바꾸는 때를 잡는다. */
export const WORK_DAY_POLL_MS = 3_000;

const enc = encodeURIComponent;

// ── 서버 오류 코드 ───────────────────────────────────────────────────────────

/**
 * 화면이 구분해서 안내해야 하는 서버 오류 코드(ProblemDetail 의 code).
 * 티켓에 이름이 없는 것은 임시다: DATE_IN_PAST·SIGNATURE_REQUIRED·STARTED 처럼 서버 구현 때 바뀔 수 있다.
 */
export const WORK_ERROR = {
  /** 날짜가 오늘이 아닌 작업은 시작할 수 없다(409, -610). */
  NOT_TODAY: 'WORK_NOT_TODAY',
  /** 오늘 이전 날짜는 하루 작업을 만들 수 없다(400). 임시 이름. */
  DATE_IN_PAST: 'WORK_DATE_IN_PAST',
  /** 지금 상태에서 할 수 없는 일(종료 안 한 작업 제출·시작한 작업의 서명 삭제 등, 409). */
  INVALID_STATE: 'WORK_INVALID_STATE',
  /** 같은 현장에 진행 중인 작업이 있다(409). `serverMessage` 에 그 작업 이름이 온다. */
  ALREADY_IN_PROGRESS: 'WORK_ALREADY_IN_PROGRESS',
  /** 서명이 한 명도 없어 작업을 시작할 수 없다(409). 임시 이름. */
  SIGNATURE_REQUIRED: 'SIGNATURE_REQUIRED',
  /** 제출해 잠긴 보고서(409). 양식 수정·서명·삭제가 막힌다. */
  REPORT_LOCKED: 'REPORT_LOCKED',
  /** 다른 곳에서 먼저 양식을 고쳤다(409). 다시 불러와야 한다. */
  REPORT_FORM_CONFLICT: 'REPORT_FORM_CONFLICT',
  /** 새 보고서 판이 있다(409). 종료 뒤 예전 판을 고치려 할 때. */
  REPORT_VERSION_STALE: 'REPORT_VERSION_STALE',
  /** 서명 인원 상한(50명) 도달(409). */
  SIGNATURE_LIMIT_REACHED: 'SIGNATURE_LIMIT_REACHED',
} as const;
export type WorkErrorCode = (typeof WORK_ERROR)[keyof typeof WORK_ERROR];

const KNOWN_WORK_ERRORS: readonly string[] = Object.values(WORK_ERROR);

/** 요청 실패에서 우리가 아는 서버 오류 코드를 읽는다. 연결 실패·모르는 코드면 null. */
export function workErrorOf(error: unknown): WorkErrorCode | null {
  if (!(error instanceof ApiError) || error.kind !== 'HTTP' || error.code === undefined)
    return null;
  return KNOWN_WORK_ERRORS.includes(error.code) ? (error.code as WorkErrorCode) : null;
}

/** 요청 실패가 이 서버 오류 코드인지. */
export const isWorkError = (error: unknown, code: WorkErrorCode): boolean =>
  workErrorOf(error) === code;

// ── 작업 (/api/work-units) ───────────────────────────────────────────────────

/**
 * 날짜를 열 때 그 날의 작업과 빈 보고서를 만들거나 가져온다(멱등, 오늘 이전은 400 WORK_DATE_IN_PAST). 응답은 작업 항목이고
 * `tbm_report.report_id` 가 같이 만들어진 보고서다.
 */
export function putDailyWorkUnit(http: Http, date: string, signal?: AbortSignal) {
  return http(`/api/work-units/daily/${enc(date)}`, {
    method: 'PUT',
    schema: workDayItemSchema,
    signal,
  });
}

/** 날짜 범위(양 끝 포함)의 작업 목록. 달력 월 칸과 지난 날짜 목록에 쓴다. 형식이 틀린 작업은 뺀다. */
export async function fetchWorkUnits(
  http: Http,
  { from, to }: WorkDaysRange,
  signal?: AbortSignal,
): Promise<WorkDayItem[]> {
  const query = new URLSearchParams({ from, to });
  const response = await http(`/api/work-units?${query}`, {
    schema: workDaysResponseSchema,
    signal,
  });
  return response.works;
}

/** 작업 하나. */
export function fetchWorkUnit(http: Http, workId: string, signal?: AbortSignal) {
  return http(`/api/work-units/${enc(workId)}`, { schema: workDayItemSchema, signal });
}

/** 작업 시작. 날짜가 오늘이 아니면 409 WORK_NOT_TODAY, 서명이 없으면 409 SIGNATURE_REQUIRED(임시). */
export function startWork(http: Http, workId: string) {
  return http(`/api/work-units/${enc(workId)}/start`, {
    method: 'POST',
    schema: workDayItemSchema,
  });
}

/** 작업 종료(202). 날짜 작업은 같은 보고서에서 서버가 종료 미팅을 채우므로(`wrap_up_status`) 보고서는 다시 받아 `latest` 를 쓴다. */
export function endWork(http: Http, workId: string): Promise<WorkEndResponse> {
  return http(`/api/work-units/${enc(workId)}/end`, {
    method: 'POST',
    schema: workEndResponseSchema,
  });
}

// ── 작업내용 항목 (/api/work-tasks) ──────────────────────────────────────────

export function fetchWorkTasks(http: Http, signal?: AbortSignal): Promise<WorkTask[]> {
  return http('/api/work-tasks', { schema: workTasksResponseSchema, signal });
}

// ── 보고서 (/api/reports/tbm) ────────────────────────────────────────────────

/** 작업의 보고서 판 목록과 최신 판(`latest`). 보고서가 없으면 latest 는 null. */
export function fetchReportsByWork(
  http: Http,
  workId: string,
  signal?: AbortSignal,
): Promise<ReportsByWork> {
  const query = new URLSearchParams({ work_id: workId });
  return http(`/api/reports/tbm?${query}`, { schema: reportsByWorkSchema, signal });
}

/**
 * 양식을 통째로 저장하고 보고서 전체를 돌려받는다. `info.tasks` 가 바뀌면 서버가 `info.task_name`·작업명을 다시 채운다.
 * 409: REPORT_FORM_CONFLICT(revision 다름)·REPORT_LOCKED(제출됨)·REPORT_VERSION_STALE(새 판 있음).
 */
export function saveReportForm(
  http: Http,
  reportId: number,
  body: ReportFormSaveBody,
): Promise<WorkReport> {
  return http(`/api/reports/tbm/${enc(String(reportId))}/form`, {
    method: 'PUT',
    body,
    schema: workReportSchema,
  });
}

/**
 * 보고서를 제출해 잠근다(작업이 끝난 뒤, 티켓 -611). 제출 뒤 양식 수정·서명은 409 REPORT_LOCKED.
 * 응답 모양이 티켓에 없어 본문은 읽지 않는다. 호출한 쪽이 보고서를 다시 받아 submitted_at 을 본다.
 */
export async function submitReport(http: Http, reportId: number): Promise<void> {
  await http(`/api/reports/tbm/${enc(String(reportId))}/submit`, {
    method: 'POST',
    schema: z.unknown(),
  });
}

// ── 서명 ─────────────────────────────────────────────────────────────────────

/** 서명 이미지를 올리지 못했다. reason: TOO_LARGE(256KiB 초과)·UPLOAD_FAILED(저장소가 거절). */
export class SignatureUploadError extends Error {
  readonly reason: 'TOO_LARGE' | 'UPLOAD_FAILED';
  constructor(reason: 'TOO_LARGE' | 'UPLOAD_FAILED') {
    super(
      reason === 'TOO_LARGE'
        ? `서명 이미지가 너무 크다 (최대 ${SIGNATURE_MAX_BYTES} 바이트)`
        : '서명 이미지를 올리지 못했다',
    );
    this.name = 'SignatureUploadError';
    this.reason = reason;
  }
}

const uploadGrantSchema = z.object({
  object_key: z.string(),
  upload_url: z.string().url(),
  headers: z.record(z.string(), z.string()),
});

async function sha256Hex(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/**
 * 서명 PNG 를 올린다: POST /api/uploads/sign 으로 받은 주소에 PUT. 돌려준 키·해시를 서명 추가 요청(`postSignature`)에 싣는다.
 * 256KiB 를 넘으면 요청 없이 SignatureUploadError(TOO_LARGE).
 */
export async function uploadSignatureImage(
  http: Http,
  image: Blob,
): Promise<{ image_key: string; image_sha256: string }> {
  if (image.size > SIGNATURE_MAX_BYTES) throw new SignatureUploadError('TOO_LARGE');
  const hash = await sha256Hex(image);
  const grant = await http('/api/uploads/sign', {
    method: 'POST',
    body: {
      purpose: 'REPORT_SIGNATURE',
      content_type: 'image/png',
      size_bytes: image.size,
      sha256: hash,
    },
    schema: uploadGrantSchema,
  });
  const uploaded = await fetch(grant.upload_url, {
    method: 'PUT',
    headers: grant.headers,
    body: image,
  });
  if (!uploaded.ok) throw new SignatureUploadError('UPLOAD_FAILED');
  return { image_key: grant.object_key, image_sha256: hash };
}

/**
 * 서명을 더한다(201). 보고서 판이 최신이 아니면 409 REPORT_VERSION_STALE, 제출됐으면 409 REPORT_LOCKED,
 * 50명이면 409 SIGNATURE_LIMIT_REACHED. 제출 전이면 작업 시작 뒤에도 더할 수 있다.
 */
export function postSignature(
  http: Http,
  reportId: number,
  body: SignatureRequestBody,
): Promise<SignatureResult> {
  return http(`/api/reports/tbm/${enc(String(reportId))}/signatures`, {
    method: 'POST',
    body,
    schema: signatureResultSchema,
  });
}

/** 서명을 지운다(작업 시작 전에만. 시작했거나 제출했으면 409). */
export async function deleteSignature(
  http: Http,
  reportId: number,
  signatureId: string,
): Promise<void> {
  await http(`/api/reports/tbm/${enc(String(reportId))}/signatures/${enc(signatureId)}`, {
    method: 'DELETE',
    schema: z.void(),
  });
}

/** 보고서의 서명 목록. */
export async function fetchSignatures(
  http: Http,
  reportId: number,
  signal?: AbortSignal,
): Promise<WorkSignature[]> {
  const response = await http(`/api/reports/tbm/${enc(String(reportId))}/signatures`, {
    schema: workSignaturesResponseSchema,
    signal,
  });
  return response.items;
}

/** 서명 한 번: 이미지를 올리고 서명을 더한다(참석자). */
export async function signReport(
  http: Http,
  reportId: number,
  { image, signerName, reportVersion }: { image: Blob; signerName: string; reportVersion: number },
): Promise<SignatureResult> {
  const uploaded = await uploadSignatureImage(http, image);
  return postSignature(http, reportId, {
    report_version: reportVersion,
    signer_name: signerName.trim(),
    signer_role: 'PARTICIPANT',
    image_key: uploaded.image_key,
    image_sha256: uploaded.image_sha256,
    notice_acknowledged: true,
  });
}
