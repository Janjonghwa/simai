import { requestTbmReportsByWork } from '@/features/tbm/api';
import { requestSignatures } from '@/features/tbm/SignaturePanel';
import type { TbmTask } from '@/features/tbm/tbmDemo';
import type { PrintItem } from '@/features/tbm/TbmDocument';
import { formOf, type TbmEditsByTask } from '@/features/tbm/tbmForm';
import type { TbmReport } from '@/features/tbm/types';
import { api } from '@/lib/api';
import { useState } from 'react';
import { flushSync } from 'react-dom';

export function usePrint(
  tasks: readonly TbmTask[],
  task: TbmTask,
  edits: TbmEditsByTask,
  report?: TbmReport | null,
) {
  const [items, setItems] = useState<PrintItem[] | null>(null);
  const [error, setError] = useState('');
  const print = async (whole: boolean, beforePrint: () => void) => {
    setError('');
    try {
      const targets = whole ? tasks : [task];
      const prepared = await Promise.all(
        targets.map(async (t) => {
          const selected =
            t.id === task.id
              ? report
              : t.workId
                ? (await requestTbmReportsByWork(api, t.workId)).latest
                : null;
          const signers = selected ? (await requestSignatures(selected)).items : [];
          return {
            id: t.id,
            form: formOf(t, edits, selected),
            people: t.people,
            signatures: signers,
            approvedAt: selected?.approved_at,
            approvedByName: selected?.approved_by_name,
            reportVersion: selected?.report_version,
          };
        }),
      );
      flushSync(() => {
        setItems(prepared);
        beforePrint();
      });
      window.print();
      setItems(null);
    } catch {
      setError('인쇄할 보고서와 서명자 목록을 불러오지 못했습니다.');
    }
  };
  return { printing: items !== null, items: items ?? [], print, error };
}
