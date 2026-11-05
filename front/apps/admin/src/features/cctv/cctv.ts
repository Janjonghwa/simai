import type {
  CameraPhase,
  PauseReason,
  PreviewLink,
  RemoteView,
  ViewMode,
} from '@/features/cctv/cctv.store';
import type { HealthCamera } from '@/features/cctv/cctvCameras';

// CCTV 로컬 미리보기 화면 문구와 판단. 화면 컴포넌트 없이 시험한다.

/** 마지막 프레임이 이보다 오래되면 지연으로 보인다(키프레임 간격 2초보다 짧게). */
export const STALE_SEC = 1;

const PHASE: Record<CameraPhase, string> = {
  no_video: '영상 없음',
  waiting_keyframe: '키프레임 대기',
  playing: '표시 중',
  decode_error: '디코딩 오류',
};

/** 중계와 이어지지 않았을 때의 문구. */
const LINK_LABEL: Record<Exclude<PreviewLink, 'open'>, string> = {
  connecting: '연결 중',
  unavailable: '중계 끊김',
  unsupported: '중계 끊김',
  stopped: '원격 시청 멈춤',
};

/** 카메라 칸 상태 문구. 중계와 끊겼으면 카메라 단계보다 끊김을 먼저 보인다. */
export function tileLabel(phase: CameraPhase | undefined, link: PreviewLink): string {
  if (link !== 'open') return LINK_LABEL[link];
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

/**
 * 카메라 칸 상태(tier0_health.cameras 기준). live: 실시간, down: 끊김, frozen: 정지.
 * 끊김은 Tier 0 가 그 카메라의 신호 없음·키프레임 대기를 알린 것이고, 정지는 지금 값을 알 수 없는 것
 * (EC2 와 끊김·Tier 0 가 안 알림·카메라 목록이나 상태에 없음·World 가 멈춤)이다.
 */
export type CameraState = 'live' | 'down' | 'frozen';

export const STATE_LABEL: Record<CameraState, string> = {
  live: '실시간',
  down: '끊김',
  frozen: '정지',
};

/** 프레임이 들어오는 health status. SYNC_INVALID 는 시각 동기만 무효이므로 실시간으로 본다. */
const FRAMES_ARRIVING_STATUS: ReadonlySet<string> = new Set(['OK', 'SYNC_INVALID']);

/** fresh 는 최근 World 를 받았는지(useWorldFresh). */
export function cameraState(camera: HealthCamera | undefined, fresh: boolean): CameraState {
  if (camera?.status === undefined) return 'frozen';
  if (!FRAMES_ARRIVING_STATUS.has(camera.status)) return 'down';
  return fresh ? 'live' : 'frozen';
}

/** 칸에 보일 상태 글. 정지는 마지막으로 데이터를 받은 시각이 있으면 함께 보인다. */
export function stateText(state: CameraState, lastClock?: string): string {
  if (state === 'frozen' && lastClock) return `${STATE_LABEL[state]} · ${lastClock}`;
  return STATE_LABEL[state];
}

/**
 * LIVE 점은 영상 프레임이 실제로 들어올 때만 켠다(디코딩해 그리는 중이고 새 프레임이 끊기지 않음).
 * sinceFrameSec 는 브라우저가 마지막 프레임을 받은 뒤 지난 초다. 촬영 시각 기준 나이는 원격 시청에서 전송 지연과
 * 두 기계의 시계 차이가 섞여 기준(STALE_SEC) 근처를 오가므로 쓰지 않는다.
 */
export function framesArriving(
  phase: CameraPhase | undefined,
  link: PreviewLink,
  sinceFrameSec: number | undefined,
): boolean {
  return link === 'open' && phase === 'playing' && !isStale(sinceFrameSec);
}

/** 원격 시청 안내 한 줄과 "다시 보기"를 보일지. 로컬이거나 원격이 잘 보이는 중이면 null. */
export interface RemoteNotice {
  text: string;
  resumable: boolean;
}

const PAUSE_TEXT: Record<Exclude<PauseReason, 'hidden'>, string> = {
  idle: '10분 동안 입력이 없어 원격 시청을 멈췄습니다',
  watch_limit: '원격 시청 30분이 지나 멈췄습니다. 계속 보려면 다시 보기를 누르세요',
};

export function remoteNotice(
  mode: ViewMode,
  pause: PauseReason | null,
  remote: RemoteView | null,
): RemoteNotice | null {
  if (mode !== 'remote') return null;
  if (pause === 'idle' || pause === 'watch_limit')
    return { text: PAUSE_TEXT[pause], resumable: true };
  switch (remote?.state) {
    case 'viewer_limit':
      return {
        text: '동시 시청 인원이 다 찼습니다. 잠시 뒤 다시 보기를 누르세요',
        resumable: true,
      };
    case 'monthly_quota':
      return {
        text: '이번 달 원격 CCTV 송출 한도에 닿아 원격 시청이 막혔습니다. 현장 노트북에서는 그대로 볼 수 있습니다',
        resumable: true,
      };
    case 'waiting_source':
      return {
        text: '현장 노트북 영상 중계가 서버에 연결되지 않았습니다. 연결되면 바로 보입니다',
        resumable: false,
      };
    default:
      return null;
  }
}

const GB = 1_000_000_000;

/** 원격 CCTV 월 송출 표시(10^9 바이트 = 1 GB, 서버 상한과 같은 단위). */
export function usageText(sentBytes: number, quotaBytes: number, blocked: boolean): string {
  const sent = (sentBytes / GB).toFixed(sentBytes < 10 * GB ? 1 : 0);
  const quota = Math.round(quotaBytes / GB);
  return `원격 CCTV 월 송출 ${sent} / ${quota} GB · ${blocked ? '차단됨' : '정상'}`;
}
