import { reSetupCameras, reSetupTitle } from '@/features/layout/status';
import { useLinkAlive } from '@/hooks/useLinkAlive';
import { useRealtimeStore } from '@/store/realtime.store';
import { Button } from '@simai/ui/components/button';
import { useNavigate } from '@tanstack/react-router';

/**
 * 현장 세팅 다시 하기 배너(S15P31A101-480). 회전 자동 복구가 위치 이동·허용 각도 초과를 감지해 보정을 덮지 않고 알린 카메라를
 * EC2 가 viewer_health.re_setup 으로 내려주면, 관제 화면 상단 알림 자리에 띄운다. 세팅은 작업 관리(/work)에서 시작한다.
 * 새 보정이 활성화돼 EC2 가 빈 배열을 알리면 사라진다. 경보 등급색(빨강·주황·노랑)·지연 색과 겹치지 않게 주 색(파랑)을 쓰며,
 * 경보·System Health 판정과 무관한 안내라 장애 띠와 따로 둔다.
 */
export function ReSetupBanner() {
  const alive = useLinkAlive();
  const cameras = useRealtimeStore((s) => reSetupCameras(s.health, alive));
  const navigate = useNavigate();
  if (cameras.length === 0) return null;
  return (
    <div
      role="group"
      aria-label="현장 세팅 다시 하기"
      className="pointer-events-auto flex max-w-[680px] flex-col gap-1.5 rounded-[12px] bg-primary px-4 py-2.5 text-primary-foreground shadow-float"
    >
      <div className="flex items-center gap-3">
        <p className="min-w-0 text-[15px] leading-[22px] font-bold">
          <strong role="status">{reSetupTitle(cameras)}</strong>
        </p>
        <Button
          variant="secondary"
          size="sm"
          className="flex-none"
          onClick={() => void navigate({ to: '/work' })}
        >
          작업 관리로
        </Button>
      </div>
      <ul className="text-[13px] leading-[18px] opacity-90">
        {cameras.map((c) => (
          <li key={c.camera_id}>
            <span className="font-mono">{c.camera_id}</span> · {c.reason}
          </li>
        ))}
      </ul>
    </div>
  );
}
