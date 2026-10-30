import { WorkReplayPage } from '@/features/history/WorkReplayPage';
import { createFileRoute } from '@tanstack/react-router';

// 다른 작업으로 옮기면 화면을 새로 띄워 재생·선택을 처음부터 한다.
function WorkReplayRoute() {
  const { workId } = Route.useParams();
  return <WorkReplayPage key={workId} workId={workId} />;
}

export const Route = createFileRoute('/_app/history/$workId')({
  component: WorkReplayRoute,
});
