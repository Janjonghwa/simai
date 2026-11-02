import { requestRemoteStatus } from '@/features/cctv/api';
import { api } from '@/lib/api';
import { useQuery } from '@tanstack/react-query';

/** 원격 CCTV 월 송출 상태를 다시 받는 간격(ms). 서버가 1분마다 사용 기록을 남긴다. */
const RELOAD_MS = 60_000;

/** 관리자 CCTV 화면의 "원격 CCTV 월 송출 x / 80 GB · 차단 여부". 못 받으면 비운다(알림 아님). */
export function useRemoteStatus(enabled: boolean) {
  const query = useQuery({
    queryKey: ['cctv', 'remote', 'status'],
    queryFn: ({ signal }) => requestRemoteStatus(api, signal),
    enabled,
    refetchInterval: RELOAD_MS,
    retry: false,
  });
  return enabled ? query.data : undefined;
}
