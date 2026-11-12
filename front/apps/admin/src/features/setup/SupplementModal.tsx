import { SupplementCaptureBody } from '@/features/setup/SupplementCaptureBody';
import type { SetupSessionResponse } from '@/features/setup/types';
import { useSupplementCapture } from '@/features/setup/useSupplementCapture';
import { ModalDialog } from '@/features/tbm/ModalDialog';
import { ACTION, H2, SECONDARY } from '@/features/tbm/styles';
import { Button } from '@simai/ui/components/button';

// 보정·생성 단계에서 '다음'을 눌렀을 때 뜨는 「맵 보강 촬영(선택)」 창(S15P31A101-607). 닫으면(Esc·바깥 클릭 포함) 그 단계에
// 그대로 머물고, 아래 오른쪽 단추(올린 파일이 없으면 건너뛰기, 있으면 다음)를 누르면 구역 단계로 간다. 다시 굽는 중이어도 넘어갈 수 있다 —
// 굽기는 서버에서 계속되고, 끝나면 구역 단계의 3D 가 새 맵으로 바뀐다.

export function SupplementModal({
  session,
  onNext,
  onCancel,
}: {
  session: SetupSessionResponse;
  onNext: () => void;
  onCancel: () => void;
}) {
  const capture = useSupplementCapture(session, 'FILE_UPLOAD');
  // 이 창이 올리는 중인 파일(취소할 수 있는 줄)이 있으면 창을 닫을 때 올리기가 멈추므로, 끝나기 전에는 넘어가지 않는다.
  const uploading = capture.rows.some((row) => row.actions.includes('cancel'));
  const hasFiles = uploading || (capture.supplement?.media.length ?? 0) > 0;

  return (
    <ModalDialog
      label="맵 보강 촬영"
      onClose={onCancel}
      closeOnBackdrop
      className="w-[min(560px,calc(100vw-48px))]"
    >
      <div className="flex max-h-[calc(100dvh-48px)] flex-col">
        <div className="flex min-h-0 flex-col gap-3 overflow-y-auto px-5 pt-[18px] pb-4">
          <h2 className={H2}>맵 보강 촬영(선택)</h2>
          <SupplementCaptureBody sessionId={session.setup_session_id} capture={capture} />
        </div>
        <div className="flex flex-none items-center justify-between gap-2 border-t border-border px-5 py-3">
          <Button variant="secondary" onClick={onCancel} className={SECONDARY}>
            닫기
          </Button>
          <Button onClick={onNext} disabled={uploading} className={ACTION}>
            {hasFiles ? '다음' : '건너뛰기'}
          </Button>
        </div>
      </div>
    </ModalDialog>
  );
}
