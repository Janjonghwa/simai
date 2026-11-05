// 여러 기능 폴더가 함께 쓰는 서버 상태의 쿼리 키. 한 기능이 다른 기능의 캐시를 무효화할 때 그 폴더를 가져오지 않게 여기 둔다.

/** 히스토리 작업 목록(GET /api/work-units?days=). 작업을 시작·종료하면 다시 받는다. */
export const HISTORY_WORKS_KEY = ['history', 'works'] as const;
