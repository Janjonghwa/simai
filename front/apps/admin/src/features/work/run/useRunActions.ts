import { SAVE_REPORT_FORM_KEY } from '@/features/work/useWorkReport';
import { useEndWork, useSubmitReport } from '@/features/work/useWorkRun';
import { useIsMutating, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useState } from 'react';

// 작업 종료 단계의 동작 훅: 종료 확인창, 보고서 보내기 확인창. 요청 결과는 화면이 pending 과 실패(failed·error)로만 본다.
// 종료 미팅(`review.ending`)은 서버가 종료 뒤 채운다(화면이 채워 저장하지 않는다).

const ignore = () => undefined;

/** 작업 종료: `작업 종료` 가 확인창을 열고(request), 확인하면 종료를 요청한다. 성공하면 창을 닫는다(진행이 ENDED 로 바뀐다). */
export function useEndRun(workId: string) {
  const { mutateAsync, pending, error, reset } = useEndWork();
  const [open, setOpen] = useState(false);
  const request = useCallback(() => {
    reset();
    setOpen(true);
  }, [reset]);
  const cancel = useCallback(() => setOpen(false), []);
  const confirm = useCallback(() => {
    mutateAsync(workId).then(() => setOpen(false), ignore);
  }, [mutateAsync, workId]);
  return { open, pending, failed: error !== null, request, cancel, confirm };
}

/** 미뤄 둔 제출이 기다리는 조건(-858). */
export interface SendHold {
  /** 서버가 종료 미팅을 쓰는 중이라 지금 보내면 409(REPORT_NOT_READY)인가. 그동안 확인한 제출은 기다렸다가 끝나면 보낸다. */
  waiting?: boolean;
  /** 종료 미팅 말고 제출을 막는 이유(AI 초안 작성 중 등). 기다림이 끝났는데 남아 있으면 보내지 않고 이 이유를 보인다. */
  blockedBy?: string | null;
}

/**
 * 보고서 제출: `보고서 제출` 이 확인창을 열고(request), 확인하면 제출한다. 성공하면 창을 닫고 onSubmitted 를 부른다.
 * 제출하면 진행이 SUBMITTED 로 바뀌어 이 화면이 사라질 수 있어 mutate 콜백이 아니라 약속(await)으로 이어 받는다.
 * 서버가 종료 미팅을 쓰는 중(hold.waiting)에 확인하면 바로 보내지 않고 기다리다가(pending) 끝나는 대로 이어서 보낸다(시연 계정, -858).
 * 그때 진행 중인 양식 저장(시연 양식 맞춤 등)이 있으면 그것도 끝난 뒤에 보낸다(저장보다 먼저 잠기지 않게).
 * 기다리는 동안 취소하면 대기도 버린다. 기다림이 끝났는데 다른 막는 이유(hold.blockedBy)가 있으면 보내지 않고 그 이유(held)를 보인다.
 */
export function useSendReport(
  onSubmitted: () => void,
  { waiting = false, blockedBy = null }: SendHold = {},
) {
  const client = useQueryClient();
  const { mutateAsync, pending, error, reset } = useSubmitReport();
  const saving = useIsMutating({ mutationKey: SAVE_REPORT_FORM_KEY }) > 0;
  const [open, setOpen] = useState(false);
  /** 확인했지만 아직 보내지 않은 보고서 id. */
  const [queued, setQueued] = useState<number | null>(null);
  const [held, setHeld] = useState<string | null>(null);
  const send = useCallback(
    (reportId: number) => {
      mutateAsync(reportId).then(() => {
        setOpen(false);
        onSubmitted();
      }, ignore);
    },
    [mutateAsync, onSubmitted],
  );
  const request = useCallback(() => {
    reset();
    setHeld(null);
    setOpen(true);
  }, [reset]);
  const cancel = useCallback(() => {
    setQueued(null);
    setOpen(false);
  }, []);
  const confirm = useCallback(
    (reportId: number) => {
      setHeld(null);
      if (waiting) setQueued(reportId);
      else send(reportId);
    },
    [waiting, send],
  );
  useEffect(() => {
    if (queued === null || waiting || saving) return;
    // 같은 커밋에서 자식(보고서 종이)이 막 시작한 양식 저장은 useIsMutating 에 아직 안 보이므로 지금 값을 한 번 더 본다
    if (client.isMutating({ mutationKey: SAVE_REPORT_FORM_KEY }) > 0) return;
    // 기다린 제출을 한 번만 꺼낸다(다음 그리기에서 queued 가 비어 다시 보내지 않는다)
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setQueued(null);
    if (blockedBy !== null) setHeld(blockedBy);
    else send(queued);
  }, [queued, waiting, saving, blockedBy, client, send]);
  return { open, pending: pending || queued !== null, error, held, request, cancel, confirm };
}
