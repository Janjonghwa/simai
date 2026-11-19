import { ROOM_BOUNDS, type PlanBackdrop, type PlanBounds } from '@/features/plan/plan';
import { useScenePlanStore } from '@/features/plan/scenePlan.store';

export function usePlanBackdrop(): { bounds: PlanBounds; backdrop: PlanBackdrop | undefined } {
  const plan = useScenePlanStore((s) => s.plan);
  return { bounds: plan?.bounds ?? ROOM_BOUNDS, backdrop: plan ?? undefined };
}
