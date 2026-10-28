import type { CameraPhase, PreviewLink } from '@/features/cctv/cctv.store';

// CCTV 로컬 미리보기 화면 문구와 판단. 화면 컴포넌트 없이 시험한다.

/** 마지막 프레임이 이보다 오래되면 지연으로 보인다(키프레임 간격 2초보다 짧게). */
export const STALE_SEC = 1;

const PHASE: Record<CameraPhase, string> = {
  no_video: '영상 없음',
  waiting_keyframe: '키프레임 대기',
  playing: '표시 중',
  decode_error: '디코딩 오류',
};

/** 카메라 칸 상태 문구. 중계와 끊겼으면 카메라 단계보다 끊김을 먼저 보인다. */
export function tileLabel(phase: CameraPhase | undefined, link: PreviewLink): string {
  if (link !== 'open') return link === 'connecting' ? '연결 중' : '중계 끊김';
  return PHASE[phase ?? 'no_video'];
}

/**
 * 안내만 보이고 영상 칸을 그리지 않는 경우: WebCodecs 가 없거나, 이번 화면에서 한 번도 중계에 붙지 못했다
 * (현장 노트북이 아니거나 중계 미리보기가 꺼짐). 한 번 붙은 뒤 끊기면 칸을 두고 끊김만 보인다.
 */
export function showsNotice(link: PreviewLink, everOpened: boolean): boolean {
  return link === 'unsupported' || (link === 'unavailable' && !everOpened);
}

export function noticeText(link: PreviewLink): string {
  return link === 'unsupported'
    ? '이 브라우저는 영상 디코딩(WebCodecs)을 지원하지 않는다. 현장 노트북의 Chrome·Edge 에서 연다.'
    : '실시간 영상은 현장 노트북에서만 표시된다. 노트북 영상 중계를 미리보기와 함께 켰는지 확인한다.';
}

export const isStale = (ageSec: number | undefined) => ageSec !== undefined && ageSec > STALE_SEC;
