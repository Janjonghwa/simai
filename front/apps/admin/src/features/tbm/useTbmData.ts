import {
  requestCreateWorkUnit,
  requestLayoutDetail,
  requestLayouts,
  requestTbmReportsByWork,
  requestTbmWorks,
  requestUpdateWorkUnit,
  requestWorkTypes,
  TBM_LAYOUTS_KEY,
  TBM_WORK_TYPES_KEY,
  TBM_WORKS_KEY,
  tbmLayoutDetailKey,
  tbmReportsByWorkKey,
} from '@/features/tbm/api';
import { OUTSIDE_ZONE_TEXT } from '@/features/tbm/constants';
import {
  clampPeople,
  createTask,
  inputToWorkUnitBody,
  type TaskInput,
} from '@/features/tbm/taskInput';
import { TBM_TASKS, type TbmTask } from '@/features/tbm/tbmDemo';
import {
  isFallbackApiError,
  WORK_TYPE_OPTIONS,
  workUnitToTbmTask,
  type LayoutDetail,
  type LayoutSummary,
  type TbmReport,
  type TbmWorkUnit,
  type WorkTypeOption,
} from '@/features/tbm/types';
import { api } from '@/lib/api';
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useMemo, useState } from 'react';

export interface UseTbmDataResult {
  /** 화면에 표시할 작업 목록(서버 연결 시 서버 작업, API 실패 시 데모 폴백 작업). */
  tasks: readonly TbmTask[];
  /** 공종 선택 목록(/api/work-types 응답 또는 설계 5.4 기본 목록). */
  workTypes: readonly WorkTypeOption[];
  /** 가상 레이아웃 요약 목록(/api/layouts). */
  layouts: readonly LayoutSummary[];
  /** API 실패(네트워크 끊김·시간 초과·5xx)로 tbmDemo.ts 폴백 중인지 여부. */
  isFallback: boolean;
  /** 서버 데이터를 처음 불러오는 중인지 여부. */
  isLoading: boolean;
  /** 작업 등록(POST /api/work-units 또는 데모 상태 추가). */
  addTask: (input: TaskInput) => Promise<TbmTask>;
  /** 작업 수정(PUT /api/work-units/{id} 또는 데모 상태 수정). */
  editTask: (task: TbmTask, input: TaskInput) => Promise<TbmTask>;
}

export function useTbmData(): UseTbmDataResult {
  const queryClient = useQueryClient();
  const [demoTasks, setDemoTasks] = useState<readonly TbmTask[]>(TBM_TASKS);

  const worksQuery = useQuery({
    queryKey: TBM_WORKS_KEY,
    queryFn: ({ signal }) => requestTbmWorks(api, signal),
    retry: false,
  });

  const workTypesQuery = useQuery({
    queryKey: TBM_WORK_TYPES_KEY,
    queryFn: ({ signal }) => requestWorkTypes(api, signal),
    retry: false,
  });

  const layoutsQuery = useQuery({
    queryKey: TBM_LAYOUTS_KEY,
    queryFn: ({ signal }) => requestLayouts(api, signal),
    retry: false,
  });

  const serverWorks = worksQuery.data;
  const isFallback = worksQuery.isError && isFallbackApiError(worksQuery.error);

  const distinctLayoutIds = useMemo(() => {
    if (!serverWorks) return [];
    const set = new Set<number>();
    for (const w of serverWorks) {
      if (typeof w.layout_id === 'number') set.add(w.layout_id);
    }
    return [...set];
  }, [serverWorks]);

  const layoutDetailQueries = useQueries({
    queries: distinctLayoutIds.map((layoutId) => ({
      queryKey: tbmLayoutDetailKey(layoutId),
      queryFn: ({ signal }: { signal: AbortSignal }) => requestLayoutDetail(api, layoutId, signal),
      retry: false,
      staleTime: 60_000,
    })),
  });

  const reportByWorkQueries = useQueries({
    queries: (serverWorks ?? []).map((w) => ({
      queryKey: tbmReportsByWorkKey(w.work_id),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        requestTbmReportsByWork(api, w.work_id, signal),
      retry: false,
    })),
  });

  const layoutById = useMemo(() => {
    const map = new Map<number, LayoutDetail>();
    for (const q of layoutDetailQueries) {
      if (q.data) map.set(q.data.layout_id, q.data);
    }
    return map;
  }, [layoutDetailQueries]);

  const latestReportByWorkId = useMemo(() => {
    const map = new Map<string, TbmReport>();
    if (!serverWorks) return map;
    serverWorks.forEach((w, idx) => {
      const latest = reportByWorkQueries[idx]?.data?.latest;
      if (latest) map.set(w.work_id, latest);
    });
    return map;
  }, [serverWorks, reportByWorkQueries]);

  const tasks = useMemo<readonly TbmTask[]>(() => {
    if (isFallback || !serverWorks || serverWorks.length === 0) {
      return demoTasks;
    }
    return serverWorks.map((w) =>
      workUnitToTbmTask(
        w,
        w.layout_id !== null && w.layout_id !== undefined
          ? (layoutById.get(w.layout_id) ?? null)
          : null,
        latestReportByWorkId.get(w.work_id) ?? null,
      ),
    );
  }, [isFallback, serverWorks, demoTasks, layoutById, latestReportByWorkId]);

  const addTask = useCallback(
    async (input: TaskInput): Promise<TbmTask> => {
      const localCreated = createTask(tasks, input);
      setDemoTasks((prev) => [...prev, localCreated]);

      const body = inputToWorkUnitBody(input);
      const optimisticWorkId = localCreated.id.replace(/^T/, '');
      const optimisticWork: TbmWorkUnit = {
        work_id: optimisticWorkId,
        ...body,
        event_counts: { CRITICAL: 0, HIGH: 0 },
      };

      if (!isFallback && serverWorks !== undefined) {
        queryClient.setQueryData<TbmWorkUnit[]>(TBM_WORKS_KEY, (prev) =>
          prev ? [...prev, optimisticWork] : [optimisticWork],
        );
        try {
          const createdWork = await requestCreateWorkUnit(api, body);
          queryClient.setQueryData<TbmWorkUnit[]>(TBM_WORKS_KEY, (prev) =>
            prev
              ? prev.map((w) => (w.work_id === optimisticWorkId ? createdWork : w))
              : [createdWork],
          );
          await queryClient.invalidateQueries({ queryKey: TBM_WORKS_KEY });
          const layout =
            createdWork.layout_id !== null && createdWork.layout_id !== undefined
              ? (layoutById.get(createdWork.layout_id) ?? null)
              : null;
          return workUnitToTbmTask(createdWork, layout, null);
        } catch {
          return localCreated;
        }
      }
      return localCreated;
    },
    [tasks, isFallback, serverWorks, queryClient, layoutById],
  );

  const editTask = useCallback(
    async (task: TbmTask, input: TaskInput): Promise<TbmTask> => {
      const layoutNum =
        input.layoutId && input.layoutId.trim() !== '' ? Number(input.layoutId) : null;
      const updatedLocal: TbmTask = {
        ...task,
        name: input.name.trim(),
        from: input.from,
        to: input.to,
        zone: input.zone || null,
        ...(input.zone ? {} : { area: OUTSIDE_ZONE_TEXT }),
        people: clampPeople(input.people),
        lead: input.lead,
        layoutId: layoutNum !== null && Number.isFinite(layoutNum) ? layoutNum : null,
        workType: input.workType && input.workType.trim() !== '' ? input.workType.trim() : null,
      };
      setDemoTasks((prev) => prev.map((t) => (t.id === task.id ? updatedLocal : t)));

      if (!isFallback && task.workId) {
        const body = inputToWorkUnitBody(input, task.date);
        queryClient.setQueryData<TbmWorkUnit[]>(TBM_WORKS_KEY, (prev) =>
          prev ? prev.map((w) => (w.work_id === task.workId ? { ...w, ...body } : w)) : prev,
        );
        try {
          const updatedWork = await requestUpdateWorkUnit(api, task.workId, body);
          await queryClient.invalidateQueries({ queryKey: TBM_WORKS_KEY });
          const layout =
            updatedWork.layout_id !== null && updatedWork.layout_id !== undefined
              ? (layoutById.get(updatedWork.layout_id) ?? null)
              : null;
          const report = latestReportByWorkId.get(updatedWork.work_id) ?? null;
          return workUnitToTbmTask(updatedWork, layout, report);
        } catch {
          return updatedLocal;
        }
      }
      return updatedLocal;
    },
    [isFallback, queryClient, layoutById, latestReportByWorkId],
  );

  return {
    tasks,
    workTypes: workTypesQuery.data ?? WORK_TYPE_OPTIONS,
    layouts: layoutsQuery.data ?? [],
    isFallback,
    isLoading: worksQuery.isLoading,
    addTask,
    editTask,
  };
}
