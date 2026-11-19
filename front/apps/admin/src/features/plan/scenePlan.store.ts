import type { PlanBounds } from '@/features/plan/plan';
import { create } from 'zustand';

export interface ScenePlan {
  imageUrl: string;
  bounds: PlanBounds;
}
interface State {
  plan: ScenePlan | null;
  setPlan: (plan: ScenePlan | null) => void;
}
export const useScenePlanStore = create<State>((set) => ({
  plan: null,
  setPlan: (plan) => set({ plan }),
}));
