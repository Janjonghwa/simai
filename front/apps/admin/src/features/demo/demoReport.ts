import { useIsDemoAccount } from '@/features/demo/demoAccount';
import { taskNameOf } from '@/features/work/reportForm';
import type { ReportForm, ReportLeader, ReportRiskRow, WorkTask } from '@/features/work/types';
import { useEffect, useReducer } from 'react';

// 시연 계정(features/demo/demoAccount)의 TBM 보고서 초안. 서버 초안을 기다리지 않고, 보고서가 초안 단계가 되면(생성 중이거나 이미 끝남)
// DEMO_REPORT_REVEAL_MS 동안 「TBM 생성중..」으로 흐리게 보인 뒤 미리 적어 둔 내용으로 채워 보인다(화면·출력만, 서버에 저장하지 않는다).
// 어느 작업을 그렇게 보였는지 이 페이지에 남겨 단계를 옮겨 다시 그려도 또 흐리지 않는다(새로고침하면 처음부터 다시 보인다).

export const DEMO_REPORT_REVEAL_MS = 1_500;

const PLACE = '실습실 단상 및 개구부 주변';

const RISKS: readonly ReportRiskRow[] = [
  {
    risk: '단상 개구부로 발을 헛디뎌 추락',
    measure: '개구부 위험구역 지정, 덮개 설치 전까지 접근 통제',
  },
  {
    risk: '단상 위 작업 중 균형을 잃고 떨어짐',
    measure: '단상 가장자리 작업 금지, 2인 1조 작업',
  },
  {
    risk: '안전모·안전조끼 미착용 상태로 작업구역 진입',
    measure: '작업 전 보호구 착용 상호 확인, 미착용자 진입 금지',
  },
];

const FOCUS = {
  risk: '단상 개구부 추락',
  measure: '개구부 위험구역 진입 금지, 진입 경보가 울리면 즉시 작업 중지',
};

/** '아니오' 로 두고 조치 내용을 적는 점검 줄(개구부 덮개는 아직 없다). */
const OPENING_CHECK = {
  code: 'OPENING_GUARD',
  action: '덮개 설치 전까지 위험구역 지정·접근 통제',
};

const DAILY =
  '작업 전 보호구 착용 상태와 단상 개구부 위험구역 표시를 확인함. 개구부 덮개 설치 전까지 접근을 통제하고, 위험구역 진입 경보가 울리면 즉시 작업을 멈춘다.';

/** 작업을 끝낸 뒤 종료 미팅 칸(서버는 사건 집계만 적는다). */
const ENDING = [
  '• 작업 중 낙상사고 1건 발생(단상 개구부 인근)',
  '• 고소작업부 안전 난간 등 안전조치 필요',
  '• 작업자 안전교육 실행',
  '• 다음 작업 전 위험구역 표시와 보호구 착용 상태 재확인',
].join('\n');

/** 보고서 리더 칸(소속·직책·성명). 서버 값은 로그인한 계정이라 시연 이름으로 바꿔 보인다. */
const LEADER = { org: '싸피건설', title: '안전관리자', name: '김싸피' } as const;

/** 시연 보고서의 리더. 계정 번호만 서버 값을 따른다. */
export function demoReportLeader(leader: ReportLeader | null): ReportLeader {
  return { user_id: leader?.user_id ?? 0, ...LEADER };
}

/** 서버 양식에 미리 적어 둔 초안 칸(장소·위험성평가·잠재위험·중점·점검·일일 안전점검)을 채운다. 일시·작업내용·종료 미팅은 서버 값 그대로다. */
export function demoReportForm(
  form: ReportForm,
  catalog: readonly WorkTask[],
  workEnded: boolean,
): ReportForm {
  return {
    ...form,
    info: {
      ...form.info,
      task_name: form.info.task_name || taskNameOf(form.info.tasks, catalog),
      place: PLACE,
      risk_assessed: true,
    },
    risks: RISKS.map((row) => ({ ...row })),
    focus: { ...FOCUS },
    checks: form.checks.map((row) =>
      row.code === OPENING_CHECK.code
        ? { ...row, yes: false, action: OPENING_CHECK.action }
        : { ...row, yes: true, action: '' },
    ),
    review: { daily: DAILY, ending: workEnded ? ENDING : form.review.ending },
  };
}

/** 미리 적어 둔 초안을 보인 작업 id. */
const revealed = new Set<string>();

/** 시연 계정이 그 작업의 미리 적어 둔 초안을 이미 보였는가. */
export function useDemoReportShown(workId: string): boolean {
  const demo = useIsDemoAccount();
  return demo && revealed.has(workId);
}

/**
 * 보고서 종이를 초안 생성 중으로 가릴지와 미리 적어 둔 초안을 보일지. 시연 계정이 아니면 서버 상태(`drafting`) 그대로다.
 * 시연 계정은 초안 단계(`drafting` 또는 서버 초안이 이미 끝난 `drafted`)가 되면 DEMO_REPORT_REVEAL_MS 동안 가리고,
 * 그 뒤로는 서버 초안과 상관없이 미리 적어 둔 초안을 보인다. 오늘 작업 흐름의 종이(`today`)에서만 그렇고 지난 날짜 보고서는 서버 값 그대로다.
 */
export function useDemoReportDraft(
  workId: string,
  { drafting, drafted, today }: { drafting: boolean; drafted: boolean; today: boolean },
): { drafting: boolean; shown: boolean } {
  const demo = useIsDemoAccount() && today;
  const [, rerender] = useReducer((n: number) => n + 1, 0);
  const shown = demo && revealed.has(workId);
  const waiting = demo && (drafting || drafted) && !shown;

  useEffect(() => {
    if (!waiting) return undefined;
    const timer = window.setTimeout(() => {
      revealed.add(workId);
      rerender();
    }, DEMO_REPORT_REVEAL_MS);
    return () => window.clearTimeout(timer);
  }, [waiting, workId]);

  return { drafting: demo ? waiting : drafting, shown };
}
