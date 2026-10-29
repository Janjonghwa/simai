import { SeverityBadge } from '@/features/events/SeverityBadge';
import { PageTitle } from '@/features/layout/PageTitle';
import { TaskForm } from '@/features/tbm/TaskForm';
import {
  clockText,
  createTask,
  formOf,
  hasEdits,
  interferencesOf,
  overlapsTime,
  SEVERITY_TEXT,
  taskPeak,
  taskPlace,
  tbmFill,
  tbmInterferences,
  type Interference,
  type TaskInput,
} from '@/features/tbm/tbm';
import { useTbmFormStore } from '@/features/tbm/tbm.store';
import { TBM_DAY, TBM_TASKS, type TbmTask } from '@/features/tbm/tbmDemo';
import { TbmFormSheet } from '@/features/tbm/TbmFormSheet';
import { TbmPlan } from '@/features/tbm/TbmPlan';
import { Badge } from '@simai/ui/components/badge';
import { Button } from '@simai/ui/components/button';
import { Card } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { useEffect, useMemo, useState } from 'react';
import { createPortal, flushSync } from 'react-dom';

// 레이아웃 · TBM 보고서(계획 모드). 작업마다 가상 레이아웃 · 평가 · 동시 작업 간섭을 보이고 TBM 회의록을 채워 반장에게 보낸다.
// 작업·평가 API 가 아직 없어 가짜 자료(tbmDemo)로 그린다. 반장 PWA 보내기는 흉내만 내고 서버를 부르지 않는다.

/** 작업을 고르면 회의록을 받는 동안(흉내) 빈 양식을 흐리게 보인다. */
const RECEIVE_MS = 700;
const EMPTY_FORM = tbmFill(null);

const PANEL = 'flex-none gap-3 px-5 py-[18px]';
const H2 = 'text-[15px] leading-[22px] font-bold text-muted-foreground';
const NOTE = 'text-[13px] text-muted-foreground';
const PILL =
  'rounded-full px-2.5 py-0.5 text-xs whitespace-nowrap text-muted-foreground ring-1 ring-faint ring-inset';
const ACTION = 'h-[38px] px-3.5';
const SECONDARY = cn(
  ACTION,
  'bg-surface-3 aria-pressed:text-point-soft aria-pressed:ring-1 aria-pressed:ring-primary aria-pressed:ring-inset',
);
const CARD_BUTTON =
  'flex flex-col items-start gap-1 rounded-[10px] px-3.5 py-3 text-left ring-1 ring-inset outline-none transition-[background-color,box-shadow] duration-150 ease-out focus-visible:ring-3 focus-visible:ring-ring/50';
const EVAL_HEAD = 'px-1.5 py-2 text-left font-medium whitespace-nowrap text-faint';
const EVAL_CELL = 'border-t border-border px-1.5 py-2 text-left tabular-nums';

function TaskCard({
  task,
  pressed,
  interferences,
  sentAt,
  onSelect,
}: {
  task: TbmTask;
  pressed: boolean;
  interferences: number;
  sentAt?: string;
  onSelect: () => void;
}) {
  const peak = taskPeak(task);
  return (
    <button
      type="button"
      aria-label={`${task.id} ${task.name} · ${task.from}–${task.to} · ${peak ? SEVERITY_TEXT[peak] : '평가 없음'}`}
      aria-pressed={pressed}
      onClick={onSelect}
      className={cn(
        CARD_BUTTON,
        'bg-card ring-border hover:bg-surface-2 aria-pressed:bg-surface-3 aria-pressed:ring-2 aria-pressed:ring-primary',
      )}
    >
      <span className="flex w-full items-center gap-2">
        <b className="text-[15px]">
          {task.id} · {task.name}
        </b>
        <span className="ml-auto">
          {peak ? (
            <SeverityBadge severity={peak} />
          ) : (
            <Badge variant="secondary" className="rounded-[4px] bg-surface-3 text-muted-foreground">
              평가 없음
            </Badge>
          )}
        </span>
      </span>
      <span className="text-xs text-muted-foreground">
        {task.from}–{task.to} · {taskPlace(task)} · {task.people}명 · 반장 {task.lead} · {task.gear}
      </span>
      <span className={cn('text-xs', sentAt ? 'text-point-soft' : 'text-muted-foreground')}>
        {sentAt ? `보고서 보냄 ${sentAt} · ` : ''}동시 작업 간섭 {interferences}건
      </span>
    </button>
  );
}

function EvalTable({ task }: { task: TbmTask }) {
  if (!task.evals.length) return <p className={NOTE}>평가 항목 없음</p>;
  return (
    <table className="w-full border-collapse text-[13px]">
      <thead>
        <tr>
          {['항목', '규칙', '등급', '근거'].map((head) => (
            <th key={head} scope="col" className={EVAL_HEAD}>
              {head}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {task.evals.map((e, n) => (
          <tr key={n}>
            <td className={EVAL_CELL}>{e.item}</td>
            <td className={cn(EVAL_CELL, 'font-mono whitespace-nowrap')}>{e.rule}</td>
            <td className={cn(EVAL_CELL, 'whitespace-nowrap')}>
              <SeverityBadge severity={e.sev} />
            </td>
            <td className={EVAL_CELL}>{e.why}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function InterferenceSummary({
  task,
  tasks,
  mine,
}: {
  task: TbmTask;
  tasks: readonly TbmTask[];
  mine: readonly Interference[];
}) {
  return (
    <>
      {tasks
        .filter((other) => other.id !== task.id)
        .map((other) => {
          const hit = mine.find((x) => x.a === other.id || x.b === other.id);
          const pair = (
            <b>
              {task.id} ↔ {other.id}
            </b>
          );
          if (!hit)
            return (
              <div
                key={other.id}
                className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 rounded-lg px-3 py-2.5 text-[13px] text-muted-foreground ring-1 ring-border ring-inset"
              >
                {pair}
                <span>
                  {overlapsTime(other, task)
                    ? '같은 시간대 · 배치와 구역이 겹치지 않음'
                    : '시간대 겹침 없음'}
                </span>
              </div>
            );
          return (
            <div
              key={other.id}
              className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 rounded-lg bg-surface-2 px-3 py-2.5 text-[13px]"
            >
              {pair}
              <SeverityBadge severity={hit.sev} />
              <span className="text-muted-foreground">
                {hit.from}–{hit.to} 겹침
              </span>
              <span className="basis-full">{hit.why}</span>
            </div>
          );
        })}
    </>
  );
}

export function TbmPage() {
  const [tasks, setTasks] = useState<readonly TbmTask[]>(TBM_TASKS);
  const [selected, setSelected] = useState(TBM_TASKS[0]!.id);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(false);
  /** 회의록을 다 받은 작업. 지금 작업과 다르면 받는 중이다. */
  const [readyId, setReadyId] = useState<string | null>(null);
  /** 작업마다 반장 PWA 로 보낸 시각. */
  const [sentAt, setSentAt] = useState<Record<string, string>>({});
  const [message, setMessage] = useState('');
  const [printAll, setPrintAll] = useState(false);
  const edits = useTbmFormStore((s) => s.edits);
  const setField = useTbmFormStore((s) => s.setField);
  const undo = useTbmFormStore((s) => s.undo);

  const task = tasks.find((t) => t.id === selected) ?? tasks[0]!;
  const all = useMemo(() => tbmInterferences(tasks), [tasks]);
  const mine = interferencesOf(all, task.id);
  const sameTime = tasks.filter((t) => t.id !== task.id && overlapsTime(t, task));
  const base = useMemo(() => tbmFill(task), [task]);
  const receiving = readyId !== task.id;

  // 작업을 고르면 잠깐 받는 중을 보이고 그 작업 내용으로 양식을 채운다.
  useEffect(() => {
    const timer = window.setTimeout(() => setReadyId(task.id), RECEIVE_MS);
    return () => window.clearTimeout(timer);
  }, [task.id]);

  const select = (id: string) => {
    if (id !== task.id) {
      setSelected(id);
      setReadyId(null);
    }
    setEditing(false);
    setAdding(false);
    setMessage('');
  };

  const add = (input: TaskInput) => {
    const created = createTask(tasks, input);
    setTasks([...tasks, created]);
    select(created.id);
    setMessage(`${created.id} 등록됨 · 배치는 아직 없음`);
  };

  const send = () => {
    setSentAt({ ...sentAt, [task.id]: clockText(new Date()) });
    setMessage(`${task.id} 회의록을 반장 ${task.lead} PWA로 보냄`);
  };

  // 출력용 사본(하루치면 작업마다 한 장)을 먼저 그린 뒤 인쇄 창을 연다.
  const print = (whole: boolean) => {
    flushSync(() => {
      setPrintAll(whole);
      setEditing(false);
    });
    window.print();
    setPrintAll(false);
  };

  return (
    <>
      <PageTitle>레이아웃 · TBM 보고서</PageTitle>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className={cn(PILL, 'ml-auto')}>계획 모드</span>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => print(true)}
          className="h-[30px] bg-surface-2 px-3 text-[13px] text-muted-foreground"
        >
          하루치 묶음 PDF
        </Button>
      </div>
      <div
        role="group"
        aria-label={`내일 작업 ${TBM_DAY} · ${tasks.length}건`}
        className="grid flex-none grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-2.5"
      >
        {tasks.map((t) => (
          <TaskCard
            key={t.id}
            task={t}
            pressed={!adding && t.id === task.id}
            interferences={interferencesOf(all, t.id).length}
            sentAt={sentAt[t.id]}
            onSelect={() => select(t.id)}
          />
        ))}
        <button
          type="button"
          aria-pressed={adding}
          onClick={() => {
            setAdding(true);
            setMessage('');
          }}
          className={cn(
            CARD_BUTTON,
            'items-center justify-center text-point-soft ring-faint hover:bg-surface-2 aria-pressed:bg-surface-3 aria-pressed:ring-2 aria-pressed:ring-primary',
          )}
        >
          <b>+ 작업 등록</b>
        </button>
      </div>
      <p role="status" className="text-[13px] text-point-soft empty:hidden">
        {message}
      </p>
      {adding ? (
        <TaskForm onAdd={add} onCancel={() => setAdding(false)} />
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-[minmax(320px,0.8fr)_minmax(0,1.5fr)] gap-3.5">
          <div className="flex min-h-0 min-w-0 flex-col gap-3.5 overflow-auto">
            <Card role="region" aria-label={`${task.id} 가상 레이아웃`} className={PANEL}>
              <h2 className={H2}>{task.id} 가상 레이아웃</h2>
              <div className="rounded-xl bg-background p-3.5 ring-1 ring-faint ring-inset">
                <TbmPlan
                  task={task}
                  others={sameTime}
                  spots={mine.flatMap((x) => (x.spot ? [x.spot] : []))}
                />
              </div>
              {!task.items.length && <p className={NOTE}>배치 항목 없음</p>}
            </Card>
            <Card role="region" aria-label="계획 모드 평가 결과" className={PANEL}>
              <h2 className={H2}>계획 모드 평가 결과 · {task.id}</h2>
              <EvalTable task={task} />
            </Card>
            <Card role="region" aria-label="동시 작업 간섭 요약" className={PANEL}>
              <h2 className={H2}>동시 작업 간섭 요약</h2>
              <InterferenceSummary task={task} tasks={tasks} mine={mine} />
            </Card>
          </div>
          <Card
            role="region"
            aria-label={`${task.id} TBM 회의록`}
            className="min-h-0 min-w-0 gap-3 px-5 py-[18px]"
          >
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-2">
              <h2 className={H2}>TBM 회의록 · {task.id}</h2>
              {receiving && (
                <span role="status" className={PILL}>
                  받는 중
                </span>
              )}
              <span className="ml-auto" />
              <Button
                variant="secondary"
                aria-pressed={editing}
                disabled={receiving}
                onClick={() => setEditing(!editing)}
                className={SECONDARY}
              >
                {editing ? '편집 끝' : '편집'}
              </Button>
              <Button
                variant="secondary"
                disabled={receiving || !hasEdits(edits, task.id)}
                onClick={() => undo(task.id)}
                className={SECONDARY}
              >
                되돌리기
              </Button>
              <Button
                variant="secondary"
                disabled={receiving}
                onClick={() => print(false)}
                className={SECONDARY}
              >
                출력
              </Button>
              <Button disabled={receiving} onClick={send} className={cn(ACTION, 'font-bold')}>
                반장 {task.lead} PWA로 보내기
              </Button>
            </div>
            <div className="min-h-0 flex-1 overflow-auto rounded-lg bg-surface-2 p-[18px]">
              <TbmFormSheet
                form={receiving ? EMPTY_FORM : { ...base, ...edits[task.id] }}
                editable={editing && !receiving}
                onSave={(key, value) => setField(task.id, key, value, base[key] ?? null)}
                people={task.people}
                receiving={receiving}
              />
            </div>
          </Card>
        </div>
      )}
      {createPortal(
        <div className="hidden print:block print:[body:has(>&)]:bg-white print:[body:has(>&)>*:not(&)]:hidden!">
          {(printAll ? tasks : [task]).map((t) => (
            <TbmFormSheet key={t.id} form={formOf(t, edits)} editable={false} people={t.people} />
          ))}
        </div>,
        document.body,
      )}
    </>
  );
}
