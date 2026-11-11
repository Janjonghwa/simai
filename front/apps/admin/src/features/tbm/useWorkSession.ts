import { STATIC_OBJECTS_KEY } from '@/features/surfaces/api';
import { endTbmWork, startTbmWork, TBM_WORKS_KEY, tbmReportsByWorkKey } from '@/features/tbm/api';
import { WRAP_UP_GENERATING_TEXT } from '@/features/tbm/constants';
import { BUNDLES_KEY, ZONES_KEY } from '@/features/zones/api';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { HISTORY_WORKS_KEY } from '@/lib/queryKeys';
import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

/**
 * 작업의 시작·종료 요청. run 은 요청이 성공했는지를 돌려주고(실패하면 message 에 문구), 끝나면 작업 목록·보고서·
 * 히스토리 목록을 다시 받게 한다. 대상은 workId 이고 요청은 한 번에 하나다(pending 이 막는다). onWrapUp 은 종료 때
 * 서버가 작업 마무리 보고서(GENERATING)를 만들었을 때 그 보고서 id 를 받는다.
 */
export function useWorkSession(workId?: string, onWrapUp?: (id: number) => void) {
  const client = useQueryClient();
  const [message, setMessage] = useState('');
  const [pending, setPending] = useState(false);
  const run = async (action: 'start' | 'end'): Promise<boolean> => {
    if (!workId || pending) return false;
    setPending(true);
    setMessage('');
    try {
      if (action === 'start') await startTbmWork(api, workId);
      else {
        const result = await endTbmWork(api, workId);
        const { status, report_id: reportId, reason } = result.wrap_up;
        if (status === 'GENERATING' && reportId !== null) onWrapUp?.(reportId);
        setMessage(
          status === 'GENERATING'
            ? WRAP_UP_GENERATING_TEXT
            : reason === 'REPORT_GENERATING'
              ? 'TBM 보고서를 아직 만드는 중이라 마무리를 만들지 않았습니다'
              : 'TBM 보고서가 없어 마무리를 만들지 않았습니다',
        );
      }
      await Promise.all([
        client.invalidateQueries({ queryKey: TBM_WORKS_KEY }),
        client.invalidateQueries({ queryKey: tbmReportsByWorkKey(workId) }),
        client.invalidateQueries({ queryKey: HISTORY_WORKS_KEY }),
        // 서버는 작업 시작·종료 때 안전모 요구(ppe_policy)를 실은 새 구성 묶음을 발행한다(-518). 적용 상태를 다시 받는다.
        client.invalidateQueries({ queryKey: BUNDLES_KEY }),
        client.invalidateQueries({ queryKey: ZONES_KEY }),
        client.invalidateQueries({ queryKey: STATIC_OBJECTS_KEY }),
      ]);
      return true;
    } catch (error) {
      setMessage(
        error instanceof ApiError && error.code === 'WORK_ALREADY_IN_PROGRESS'
          ? `진행 중인 작업이 있습니다: ${error.serverMessage ?? '작업 목록을 확인하세요'}`
          : '작업 상태를 변경하지 못했습니다. 다시 확인해 주세요.',
      );
      return false;
    } finally {
      setPending(false);
    }
  };
  return { message, pending, run };
}
