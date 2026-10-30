import { requestRules } from '@/features/rules/api';
import { api } from '@/lib/api';
import { useQuery } from '@tanstack/react-query';

const RULES_KEY = ['rules'] as const;

/** 구성 묶음이 적용되면 규칙 판이 바뀌므로 화면이 열려 있는 동안 다시 받는 간격(ms). */
const RELOAD_MS = 10_000;

/** 규칙 현황. 받은 값은 그대로 두고, 다시 받기가 실패해도 이전 값을 유지한다(failed 만 알린다). */
export function useRules() {
  const query = useQuery({
    queryKey: RULES_KEY,
    queryFn: ({ signal }) => requestRules(api, signal),
    refetchInterval: RELOAD_MS,
  });
  return { rules: query.data, loading: query.isPending, failed: query.isError };
}
