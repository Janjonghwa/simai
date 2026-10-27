import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { SurfacesPage } from '@/features/surfaces/SurfacesPage';
import { createFileRoute } from '@tanstack/react-router';

// 권한이 바뀌면 화면을 다시 띄워 초안을 버리고 3D 편집을 취소한다(관리자만 고친다).
function SurfacesRoute() {
  const role = useCurrentRole();
  return <SurfacesPage key={role ?? 'none'} />;
}

export const Route = createFileRoute('/_app/surfaces')({
  component: SurfacesRoute,
  staticData: { fullBleed: true, eventPanel: true },
});
