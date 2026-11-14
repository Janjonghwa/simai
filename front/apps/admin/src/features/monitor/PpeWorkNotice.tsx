import { useMonitoringPhase } from '@/features/monitor/useMonitoringPhase';
import { usePpeReceipt, useSustained } from '@/features/ppe/usePpeReceipt';

/** 안내 제목(S15P31A101-634, 문구 -692). */
export const PPE_RECEIPT_STALE_TEXT = '보호구 감시 멈춤';

/** 관제가 켜진 뒤 PPE 상태가 이만큼(ms) 계속 없어야 안내한다(PPE 첫 수신·순간 끊김은 알리지 않는다). */
export const PPE_NOTICE_SUSTAIN_MS = 15_000;

const NOTICE =
  'pointer-events-auto flex flex-col gap-0.5 rounded-[12px] bg-card/94 px-4 py-2 text-sm shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset';

/**
 * 관제 중인데 EC2 가 PPE 상태를 10초 넘게 받지 못하면(보호구 감시 프로세스가 안 떴거나 멈춤) 안전모·조끼 미착용 경보가 조용히 나지 않는다
 * (S15P31A101-634). 경보가 아니라 안내다. 무엇이 / 어떻게 두 줄로 알린다.
 * 이번 작업 묶음 적용 대기·거부와 종료 묶음 적용 대기는 관제 단계(WorkSessionBar)와 보호구 줄(WorkPpeStatus)이 바로 보이므로 여기서 다루지 않는다(-692).
 */
export function PpeWorkNotice() {
  const phase = useMonitoringPhase();
  const watching = phase.stage === 'on' && phase.problem === null;
  const receipt = usePpeReceipt(watching);
  const show = useSustained(watching && receipt === 'stale', PPE_NOTICE_SUSTAIN_MS);
  if (!show) return null;
  return (
    <div role="status" className={NOTICE}>
      <b>{PPE_RECEIPT_STALE_TEXT}</b>
      <span>보호구 상태가 들어오지 않아 안전모·조끼 미착용 경보가 나지 않습니다.</span>
      <span className="text-muted-foreground">
        현장 GPU 서버의 보호구 감시가 켜져 있는지 확인하세요.
      </span>
    </div>
  );
}
