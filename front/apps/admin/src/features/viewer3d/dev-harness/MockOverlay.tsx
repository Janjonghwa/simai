import type { FixtureEvent, MenuKey } from '@/features/viewer3d/dev-harness/mockUi';
import {
  EVENTS,
  fixtureName,
  MENUS,
  OBJECTS,
  PEOPLE,
  ZONES,
} from '@/features/viewer3d/dev-harness/mockUi';
import type { ViewerStatus, ViewerTarget } from '@/features/viewer3d/types';
import { Button } from '@simai/ui/components/button';
import { Crosshair, X } from 'lucide-react';
import type { ReactNode } from 'react';

interface Commands {
  select: (target: ViewerTarget) => void;
  focus: (target: ViewerTarget) => void;
  clear: () => void;
}

interface MockOverlayProps extends Commands {
  menu: MenuKey;
  selected: ViewerTarget | null;
  status: ViewerStatus | null;
  onClose: () => void;
}

const same = (a: ViewerTarget | null, b: ViewerTarget) => a?.type === b.type && a.id === b.id;
const on = (value: boolean) => (value ? '' : undefined);

function Panel({
  title,
  wide,
  onClose,
  children,
}: {
  title: string;
  wide?: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <section
      aria-label={title}
      data-testid="mock-panel"
      data-wide={on(Boolean(wide))}
      className="smk-panel smk-float"
    >
      <header className="smk-panel-head">
        <h2>{title}</h2>
        <span className="smk-sample">예시</span>
        <Button
          variant="ghost"
          size="icon-sm"
          className="smk-close"
          aria-label="패널 닫기"
          onClick={onClose}
        >
          <X />
        </Button>
      </header>
      <div className="smk-panel-body">{children}</div>
    </section>
  );
}

function SeverityBadge({ severity }: { severity: FixtureEvent['severity'] }) {
  return (
    <span className="smk-sev" data-sev={severity}>
      {severity}
    </span>
  );
}

function Row({
  children,
  active,
  onClick,
}: {
  children: ReactNode;
  active?: boolean;
  onClick?: () => void;
}) {
  return (
    <li>
      <button type="button" onClick={onClick} className="smk-row" data-active={on(Boolean(active))}>
        {children}
      </button>
    </li>
  );
}

function MonitorBody({ selected, select, focus }: Commands & { selected: ViewerTarget | null }) {
  return (
    <>
      <p className="smk-hint">이벤트 목록 · 누르면 3D 대상을 선택하고 초점을 맞춘다</p>
      <ul className="smk-list">
        {EVENTS.map((event) => (
          <Row
            key={event.id}
            active={same(selected, event.target)}
            onClick={() => {
              select(event.target);
              focus(event.target);
            }}
          >
            <SeverityBadge severity={event.severity} />
            <span className="smk-grow">{event.title}</span>
            <span className="smk-id smk-mono">{event.target.id}</span>
          </Row>
        ))}
      </ul>
      <p className="smk-hint smk-hint-gap">추적 인원</p>
      <ul className="smk-list">
        {PEOPLE.map((person) => {
          const target: ViewerTarget = { type: 'person', id: person.id };
          return (
            <Row key={person.id} active={same(selected, target)} onClick={() => select(target)}>
              <span className="smk-grow">{person.name}</span>
              <span className="smk-id smk-mono">{person.id}</span>
            </Row>
          );
        })}
      </ul>
    </>
  );
}

function CctvBody() {
  return (
    <>
      <p className="smk-hint">카메라 4대 자리표시자 · 영상은 연결하지 않는다</p>
      <div className="smk-tiles">
        {['cam1', 'cam2', 'cam3', 'cam4'].map((camera) => (
          <div key={camera} className="smk-tile">
            {camera} 자리표시자
          </div>
        ))}
      </div>
    </>
  );
}

const EDITABLE: { target: ViewerTarget; name: string }[] = [
  ...ZONES.map((zone) => ({ target: { type: 'zone', id: zone.id } as const, name: zone.name })),
  ...OBJECTS.map((object) => ({
    target: { type: 'object', id: object.id } as const,
    name: object.name,
  })),
];

function ZonesBody({ selected, select, focus }: Commands & { selected: ViewerTarget | null }) {
  return (
    <>
      <p className="smk-hint">구역·작업면 목록 · 편집과 저장은 연결하지 않는다</p>
      <ul className="smk-list">
        {EDITABLE.map(({ target, name }) => (
          <li key={target.id} className="smk-item" data-active={on(same(selected, target))}>
            <span className="smk-grow">{name}</span>
            <Button variant="secondary" size="sm" onClick={() => select(target)}>
              선택
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`${name} 초점`}
              onClick={() => focus(target)}
            >
              <Crosshair />
            </Button>
          </li>
        ))}
      </ul>
      <dl className="smk-fields smk-box">
        <dt>이름</dt>
        <dd>{selected ? (fixtureName(selected) ?? '—') : '—'}</dd>
        <dt>높이 범위</dt>
        <dd>—</dd>
        <dt>적용 상태</dt>
        <dd>—</dd>
      </dl>
    </>
  );
}

function TbmBody() {
  return (
    <>
      <p className="smk-hint">PLANNED 모드로 전환된다 · 평가와 보고서는 연결하지 않는다</p>
      <ul className="smk-list">
        {['개구부 배치', '가설 비계 배치', '장비 주차 구역'].map((item) => (
          <li key={item} className="smk-plan">
            <span>{item}</span>
            <span className="smk-id">배치 전</span>
          </li>
        ))}
      </ul>
      <p className="smk-hint smk-box">보고서 초안 자리표시자</p>
    </>
  );
}

function SystemBody({ status }: { status: ViewerStatus | null }) {
  return (
    <>
      <p className="smk-hint">운영 상태는 연결하지 않는다</p>
      <dl className="smk-fields">
        <dt>서버 연결</dt>
        <dd>—</dd>
        <dt>카메라 4대</dt>
        <dd>—</dd>
        <dt>보정</dt>
        <dd>—</dd>
        <dt>Scene</dt>
        <dd>—</dd>
      </dl>
      <p className="smk-hint smk-hint-gap">3D 뷰어 자체 상태 (실제 값)</p>
      <dl className="smk-fields smk-mono">
        <dt>phase</dt>
        <dd>{status?.phase ?? '—'}</dd>
        <dt>mode</dt>
        <dd>{status?.mode ?? '—'}</dd>
        <dt>error</dt>
        <dd>{status?.error?.code ?? '없음'}</dd>
      </dl>
    </>
  );
}

/** 메뉴별 업무 오버레이. 3D 위 오른쪽에 뜨고 닫으면 같은 공간이 그대로 남는다. */
export function MockOverlay({ menu, selected, status, onClose, ...commands }: MockOverlayProps) {
  const title = MENUS.find((item) => item.key === menu)!.label;
  return (
    <Panel title={title} wide={menu === 'cctv'} onClose={onClose}>
      {menu === 'monitor' && <MonitorBody selected={selected} {...commands} />}
      {menu === 'cctv' && <CctvBody />}
      {menu === 'zones' && <ZonesBody selected={selected} {...commands} />}
      {menu === 'tbm' && <TbmBody />}
      {menu === 'system' && <SystemBody status={status} />}
    </Panel>
  );
}

/** 3D 에서 고른 대상을 보여주는 왼쪽 위 카드. */
export function SelectionCard({
  selected,
  focus,
  clear,
}: Pick<Commands, 'focus' | 'clear'> & { selected: ViewerTarget | null }) {
  return (
    <aside aria-label="선택 대상" data-testid="selection-card" className="smk-card smk-float">
      <p className="smk-hint">선택 대상</p>
      {selected ? (
        <>
          <p className="smk-card-title">{fixtureName(selected) ?? '알 수 없는 대상'}</p>
          <p className="smk-id smk-mono" data-testid="selected-id">
            {selected.type}:{selected.id}
          </p>
          <div className="smk-actions">
            <Button variant="secondary" size="sm" onClick={() => focus(selected)}>
              초점
            </Button>
            <Button variant="ghost" size="sm" onClick={clear}>
              해제
            </Button>
          </div>
        </>
      ) : (
        <p className="smk-id" data-testid="selected-id">
          없음 · 3D 에서 사람·구역·작업면을 누른다
        </p>
      )}
    </aside>
  );
}
