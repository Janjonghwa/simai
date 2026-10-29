import { CameraList, CameraPanel } from '@/features/dashboard/CameraPanel';
import {
  safetyNoteText,
  stageCounts,
  TICKET_STAGES,
  type TicketStage,
} from '@/features/dashboard/dashboard';
import { DEMO_PROCESS, DEMO_SAFETY, DEMO_SAFETY_LINK } from '@/features/dashboard/dashboardDemo';
import { DashModal, DashPanel } from '@/features/dashboard/DashPanel';
import { EventsList, EventsPanel } from '@/features/dashboard/EventsPanel';
import { ProcessPanel, ProcessTable } from '@/features/dashboard/ProcessPanel';
import { useEventsStore } from '@/features/events/events.store';
import { PageTitle } from '@/features/layout/PageTitle';
import { LivePlan } from '@/features/plan/LivePlan';
import { cn } from '@simai/ui/lib/utils';
import { useNavigate } from '@tanstack/react-router';
import { MapIcon, ShieldCheck, Wifi } from 'lucide-react';
import { useCallback, useState } from 'react';

// 대시보드(관제 데모 기준): 공정 현황 · 안전 현황 / 이벤트 목록 · 현장 도면 · 카메라 상태.
// 이벤트·도면·카메라 상태는 실시간 값, 공정·안전·카메라 지연은 받을 곳이 없어 데모 값(dashboardDemo)이다.

type ListModal = 'process' | 'events' | 'cameras';

/** 조치 단계 왼쪽 막대 색. */
const STAGE_TONE: Record<TicketStage, string> = {
  review: 'bg-primary',
  progress: 'bg-primary/50',
  verify: 'bg-point-soft',
  done: 'bg-faint',
};

/** 안전 현황: 위험 등급과 따로, 조치 업무 단계로 센다. */
function SafetyPanel() {
  const counts = stageCounts(DEMO_SAFETY);
  const { lost, lastAt } = DEMO_SAFETY_LINK;
  return (
    <DashPanel icon={ShieldCheck} title="안전 현황">
      <div className="flex min-h-0 flex-1 flex-col gap-3 px-4 py-3.5">
        <dl className="grid grid-cols-2 gap-3">
          {TICKET_STAGES.map(({ stage, label }) => (
            <div
              key={stage}
              className="relative flex min-h-[84px] flex-col gap-0.5 overflow-hidden rounded-lg bg-surface-2 px-3.5 py-3"
            >
              <span
                aria-hidden
                className={cn('absolute inset-y-0 left-0 w-[3px]', STAGE_TONE[stage])}
              />
              <dt className="text-[13px] text-muted-foreground">{label}</dt>
              <dd className="font-mono text-[30px] leading-[38px] font-semibold">
                {counts[stage]}
                <small className="ml-1 font-sans text-[13px] font-medium text-muted-foreground">
                  건
                </small>
              </dd>
            </div>
          ))}
        </dl>
        <p
          role="status"
          className={cn(
            'flex items-center gap-2 border-t border-border pt-3 text-[13px]',
            lost ? 'text-delay-soft' : 'text-muted-foreground',
          )}
        >
          <Wifi aria-hidden className={cn('size-4', lost ? 'text-delay' : 'text-ok')} />
          {safetyNoteText(lost, lastAt)}
        </p>
      </div>
    </DashPanel>
  );
}

/** 현장 도면: 누르면 실시간 관제로 간다. 여기서는 사람·구역을 고르지 않는다(버튼 안에 버튼을 두지 않는다). */
function PlanPanel({ onOpen }: { onOpen: () => void }) {
  return (
    <DashPanel icon={MapIcon} title="현장 도면">
      <button
        type="button"
        aria-label="실시간 관제로 이동"
        onClick={onOpen}
        className="group flex min-h-0 flex-1 cursor-pointer items-center justify-center p-2.5"
      >
        <div
          aria-hidden
          className="flex size-full items-center justify-center group-hover:brightness-97 dark:group-hover:brightness-112"
        >
          <LivePlan selectedKey={null} className="aspect-square h-full max-w-full" />
        </div>
      </button>
    </DashPanel>
  );
}

export function DashboardPage() {
  const [open, setOpen] = useState<ListModal | null>(null);
  const navigate = useNavigate();
  const close = useCallback(() => setOpen(null), []);
  const toMonitor = () => {
    void navigate({ to: '/' });
  };
  // 이벤트를 누르면 실시간 관제에서 그 사건 상세를 연다.
  const openEvent = (eventId: string) => {
    setOpen(null);
    useEventsStore.getState().openEvent(eventId);
    toMonitor();
  };

  return (
    <>
      <PageTitle>대시보드</PageTitle>
      <div className="grid min-h-0 flex-1 grid-rows-[minmax(0,0.95fr)_minmax(0,1fr)] gap-3.5">
        <div className="grid min-h-0 grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)] gap-3.5">
          <ProcessPanel onOpen={() => setOpen('process')} />
          <SafetyPanel />
        </div>
        <div className="grid min-h-0 grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)_minmax(0,1fr)] gap-3.5">
          <EventsPanel onOpenList={() => setOpen('events')} onOpen={openEvent} />
          <PlanPanel onOpen={toMonitor} />
          <CameraPanel onOpen={() => setOpen('cameras')} />
        </div>
      </div>
      {open === 'process' && (
        <DashModal label="공정 현황 전체" className="w-[min(900px,100%)]" onClose={close}>
          <ProcessTable rows={DEMO_PROCESS} className="overflow-auto" />
        </DashModal>
      )}
      {open === 'events' && (
        <DashModal label="이벤트 목록 전체" className="w-[min(900px,100%)]" onClose={close}>
          <EventsList onOpen={openEvent} />
        </DashModal>
      )}
      {open === 'cameras' && (
        <DashModal label="카메라 상태" className="w-[min(1040px,100%)]" onClose={close}>
          <CameraList />
        </DashModal>
      )}
    </>
  );
}
