import { PageTitle } from '@/features/layout/PageTitle';
import { PILL, SWITCH } from '@/features/tbm/styles';
import { TaskAddDialog } from '@/features/tbm/TaskForm';
import { createTask, type TaskInput } from '@/features/tbm/taskInput';
import { TaskList } from '@/features/tbm/TaskList';
import { clockText, tbmInterferences } from '@/features/tbm/tbm';
import { TBM_TASKS, type TbmTask } from '@/features/tbm/tbmDemo';
import { TbmPrint } from '@/features/tbm/TbmDocument';
import { TbmEditDialog } from '@/features/tbm/TbmEditDialog';
import { TbmFormSheet } from '@/features/tbm/TbmFormSheet';
import { TbmLeftPanel } from '@/features/tbm/TbmLeftPanel';
import { TbmMinutesPanel } from '@/features/tbm/TbmMinutesPanel';
import { useMinutesForm } from '@/features/tbm/useMinutesForm';
import { usePrint } from '@/features/tbm/usePrint';
import { useReceiving } from '@/features/tbm/useReceiving';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useMemo, useState } from 'react';

// 레이아웃 · TBM 보고서(계획 모드). 작업마다 가상 레이아웃 · 평가 · 동시 작업 간섭을 보이고 TBM 회의록을 채워 반장에게 보낸다.
// 왼쪽 패널은 레이아웃을 크게 보이는 것이 기본이고, 오른쪽 위 단추 하나로 평가 · 간섭 보기와 바꾼다(토글이 아니라 화면 전환).
// 회의록은 A4 쪽으로 나눠 미리 보이고, 편집은 모달 창에서 한다(고칠 때마다 저장).
// 작업·평가 API 가 아직 없어 가짜 자료(tbmDemo)로 그린다. 반장 PWA 보내기는 흉내만 내고 서버를 부르지 않는다.

export function TbmPage() {
  const [tasks, setTasks] = useState<readonly TbmTask[]>(TBM_TASKS);
  const [selected, setSelected] = useState(TBM_TASKS[0]!.id);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(false);
  /** 왼쪽 패널을 레이아웃 대신 평가 · 간섭으로 바꿔 보는지(기본은 레이아웃). */
  const [detail, setDetail] = useState(false);
  /** 작업마다 반장 PWA 로 보낸 시각. */
  const [sentAt, setSentAt] = useState<Record<string, string>>({});
  const [message, setMessage] = useState('');

  const task = tasks.find((t) => t.id === selected) ?? tasks[0]!;
  const interferences = useMemo(() => tbmInterferences(tasks), [tasks]);
  const { receiving, restart } = useReceiving(task.id);
  const { edits, filled, canUndo, save, undo } = useMinutesForm(task);
  const { printing, items, print } = usePrint(tasks, task, edits);

  const select = (id: string) => {
    if (id !== task.id) {
      setSelected(id);
      restart();
    }
    setEditing(false);
    setAdding(false);
    setMessage('');
  };

  const openAdd = () => {
    setAdding(true);
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

  const closeEditor = () => setEditing(false);

  return (
    <>
      <PageTitle>레이아웃 · TBM 보고서</PageTitle>
      <div className="flex flex-none flex-wrap items-center gap-2.5">
        <TaskList
          tasks={tasks}
          selectedId={task.id}
          sentAt={sentAt}
          onSelect={select}
          onAdd={openAdd}
        >
          <span className={cn(PILL, 'ml-auto')}>계획 모드</span>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => print(true, closeEditor)}
            className={SWITCH}
          >
            하루치 묶음 PDF
          </Button>
        </TaskList>
      </div>
      <p role="status" className="text-[13px] text-point-soft empty:hidden">
        {message}
      </p>
      <div className="[container-type:size] flex min-h-[560px] flex-1 gap-3.5">
        <TbmLeftPanel
          task={task}
          tasks={tasks}
          interferences={interferences}
          detail={detail}
          onSwitch={() => setDetail(!detail)}
        />
        <TbmMinutesPanel
          task={task}
          form={filled}
          receiving={receiving}
          canUndo={canUndo}
          onEdit={() => setEditing(true)}
          onUndo={undo}
          onPrint={() => print(false, closeEditor)}
          onSend={send}
        />
      </div>
      {adding && <TaskAddDialog onAdd={add} onCancel={() => setAdding(false)} />}
      {editing && (
        <TbmEditDialog onClose={closeEditor} onUndo={undo} canUndo={canUndo}>
          <TbmFormSheet form={filled} editable onSave={save} people={task.people} />
        </TbmEditDialog>
      )}
      {printing && <TbmPrint items={items} />}
    </>
  );
}
