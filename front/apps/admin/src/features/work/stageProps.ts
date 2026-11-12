import type { WorkDayItem } from '@/features/work/types';

// 날짜 화면(WorkDayPage)이 단계 화면에 넘기는 공통 props. 단계마다 파일이 다르고(보고서·세팅·작업 전 확인·작업 종료),
// 날짜 화면은 이 모양으로만 부른다.

export interface WorkStageProps {
  /** 이 날짜의 작업(서버 `progress` 포함). */
  day: WorkDayItem;
  /** 날짜 키(YYYY-MM-DD). */
  date: string;
  /** 오늘 날짜 키. */
  todayKey: string;
  /** 다음 단계로 가 달라는 요청(보고서 → 세팅, 세팅 → 작업 전 확인). 이후 단계는 서버 `progress` 가 따라온다. */
  onNext: () => void;
}

/** 작업 종료 단계: 보고서를 보내면(제출·잠금) 달력으로 나가도록 날짜 화면에 알린다. */
export interface RunStageProps extends WorkStageProps {
  onSubmitted: () => void;
}

/** 지난 날짜 화면. */
export interface PastDayProps {
  /** 날짜 키(YYYY-MM-DD). */
  date: string;
}

/** TBM 보고서 종이 하나(`ReportPanel`)를 쓰는 화면들이 넘기는 props. 보고서는 작업의 최신 판을 스스로 읽고 고친다. */
export interface ReportPanelProps {
  /** 보고서를 읽을 작업 id. */
  workId: string;
  /** 사람이 고칠 수 있는지(제출 전·오늘 흐름). 꺼지면 읽기 전용이다. */
  editable: boolean;
  /** 출력 단추를 보일지. */
  withPrint?: boolean;
  /**
   * 초안이 아직 없거나(draft_status null) 실패했을 때 'AI 초안 만들기' 단추를 보일지(-624). 세팅이 끝난 뒤 화면(작업 전 확인)에서만 켠다.
   * 세팅 완료(-615) 커밋 뒤 서버가 초안을 시작하므로(작업내용을 골랐을 때), 시작된 뒤(GENERATING·SUCCEEDED)에는 단추가 보이지 않는다.
   */
  draftable?: boolean;
  className?: string;
}
