import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { ApiError, NETWORK_ERROR } from '@/lib/http';

// 경보 확인(ACK) 권한과 실패 문구. import.meta.env 를 읽는 api 인스턴스(useAckEvent)와 나눠, 시험과 경보음 훅이 env 없이 부른다.

export function ackErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === NETWORK_ERROR) return '서버에 연결할 수 없습니다';
    // 4xx 서버 문구만 보여 준다. 5xx 문구에는 서버 내부 정보가 섞일 수 있다.
    if (error.status >= 400 && error.status < 500 && error.serverMessage)
      return error.serverMessage;
  }
  return '확인하지 못했습니다';
}

/** 경보 확인 권한: 반장·관리자(명세 12.6). 열람 계정에는 확인 버튼이 없고, 끌 수 없는 경보음도 내지 않는다. */
export function useCanAck() {
  const role = useCurrentRole();
  return role === 'ADMIN' || role === 'FOREMAN';
}
