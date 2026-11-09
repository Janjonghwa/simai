import { TbmPage } from '@/features/tbm/TbmPage';
import { createFileRoute, useNavigate } from '@tanstack/react-router';

// 작업 관리. ?workUnitId=<작업 id> 로 들어오면 그 작업이 선택된 채로 열린다(세팅 화면에서 돌아올 때).
function WorkRoute() {
  const { workUnitId } = Route.useSearch();
  const navigate = useNavigate();
  return (
    <TbmPage
      key={workUnitId ?? ''}
      initialWorkId={workUnitId}
      onOpenSetup={(id) => void navigate({ to: '/setup', search: { workUnitId: id } })}
      onOpenMonitor={() => void navigate({ to: '/' })}
    />
  );
}

export const Route = createFileRoute('/_app/work')({
  validateSearch: (search: Record<string, unknown>): { workUnitId?: string } => {
    const raw = search.workUnitId;
    const text = typeof raw === 'number' ? String(raw) : typeof raw === 'string' ? raw.trim() : '';
    return { workUnitId: text !== '' ? text : undefined };
  },
  component: WorkRoute,
  // 하단 메뉴가 블록 위에 반투명하게 떠서 본문이 화면 아래까지 내려온다(스크롤 없이 한 화면에 들어가게).
  staticData: { dockOverlay: true },
});
