import { Button } from '@simai/ui/components/button';
import { useNavigate } from '@tanstack/react-router';

/** 이벤트 패널 아래의 「지난 이벤트」 링크: 작업 시작 전에 난 이벤트는 기본 목록에서 빠지고 작업 관리의 지난 날짜에서 본다. */
export function PastEventsLink({ hidden }: { hidden: number }) {
  const navigate = useNavigate();
  return (
    <div className="flex items-center justify-between gap-2 px-1 pt-1">
      <span className="text-sm text-muted-foreground">
        {hidden > 0 ? `작업 시작 전 이벤트 ${hidden}건` : '지난 이벤트'}
      </span>
      <Button variant="secondary" size="sm" onClick={() => void navigate({ to: '/work' })}>
        작업 관리로
      </Button>
    </div>
  );
}
