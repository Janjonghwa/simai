import { endTbmWork, startTbmWork, TBM_WORKS_KEY, tbmReportsByWorkKey } from '@/features/tbm/api';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

export function useWorkSession(workId: string | undefined, onWrapUp: (id: number) => void) {
  const client = useQueryClient();
  const [message, setMessage] = useState('');
  const [pending, setPending] = useState(false);
  const run = async (action: 'start' | 'end') => {
    if (!workId || pending) return;
    setPending(true);
    setMessage('');
    try {
      if (action === 'start') await startTbmWork(api, workId);
      else {
        const result = await endTbmWork(api, workId);
        const { status, report_id: reportId, reason } = result.wrap_up;
        if (status === 'GENERATING' && reportId !== null) onWrapUp(reportId);
        setMessage(
          status === 'GENERATING'
            ? '작업 마무리 작성 중'
            : reason === 'REPORT_GENERATING'
              ? 'TBM 보고서를 아직 만드는 중이라 마무리를 만들지 않았습니다'
              : 'TBM 보고서가 없어 마무리를 만들지 않았습니다',
        );
      }
      await Promise.all([
        client.invalidateQueries({ queryKey: TBM_WORKS_KEY }),
        client.invalidateQueries({ queryKey: tbmReportsByWorkKey(workId) }),
      ]);
    } catch (error) {
      setMessage(
        error instanceof ApiError && error.code === 'WORK_ALREADY_IN_PROGRESS'
          ? `진행 중인 작업이 있습니다: ${error.serverMessage ?? '작업 목록을 확인하세요'}`
          : '작업 상태를 변경하지 못했습니다. 다시 확인해 주세요.',
      );
    } finally {
      setPending(false);
    }
  };
  return { message, pending, run };
}
