import { todayIsoDate } from '@/features/tbm/taskInput';
import {
  dateHhmmToMs,
  type LayoutDetail,
  type SubgraphEdge,
  type SubgraphNode,
  type TbmReport,
  type TbmReportCitation,
  type TbmReportStep,
  type TbmWorkUnit,
  WORK_TYPE_OPTIONS,
  workTypeLabelOf,
  type WorkTypeOption,
} from '@/features/tbm/types';
import { readMockUser } from '@/mocks/mockUser';
import { http, HttpResponse } from 'msw/http';

// TBM 화면(작업·공종·가상 레이아웃·보고서) MSW 핸들러.
// 정본: .agy/tbm-interface.md B·C절, 설계 .agy/ref/tbm-legal-graph.md 5.1·5.2·5.4·5.5.

const INITIAL_LAYOUTS: readonly LayoutDetail[] = [
  {
    layout_id: 101,
    name: '개구부 옆 비계 배치 (z-04)',
    revision: 3,
    layout_version: 'lv-3',
    source: 'PLANNED',
    item_count: 1,
    created_at: 1760572800000,
    updated_at: 1760572800000,
    items: [
      {
        item_id: 'pos-01',
        kind: 'SCAFFOLD',
        center: [6.1, 2.4, 1.2],
        size: [1.0, 2.4, 2.4],
        yaw_rad: 0,
        label: '비계 S1',
      },
    ],
  },
  {
    layout_id: 102,
    name: '자재 반입 동선·주차 배치 (z-02)',
    revision: 1,
    layout_version: 'lv-1',
    source: 'PLANNED',
    item_count: 2,
    created_at: 1760572800000,
    updated_at: 1760572800000,
    items: [
      {
        item_id: 'eq-parking-p1',
        kind: 'EQUIPMENT',
        center: [7.9, 7.9, 0.5],
        size: [2.6, 2.6, 1.0],
        yaw_rad: 0,
        label: '장비 주차 P1',
      },
      {
        item_id: 'eq-forklift-path',
        kind: 'EQUIPMENT',
        center: [6.225, 3.0, 0.45],
        size: [2.75, 0.9, 0.9],
        yaw_rad: 0,
        label: '지게차 동선',
      },
    ],
  },
  {
    layout_id: 103,
    name: '북서쪽 바닥 마감 배치',
    revision: 1,
    layout_version: 'lv-1',
    source: 'PLANNED',
    item_count: 1,
    created_at: 1760572800000,
    updated_at: 1760572800000,
    items: [
      {
        item_id: 'area-m1',
        kind: 'OPENING',
        center: [2.4, 2.8, 0.05],
        size: [3.6, 4.4, 0.1],
        yaw_rad: 0,
        label: '마감 구역 M1',
      },
    ],
  },
];

interface LayoutRevisionRecord {
  layout_id: number;
  revision: number;
  layout_version: string;
  name: string;
  items: LayoutDetail['items'];
  created_at: number;
}

function validateLayoutPayload(body: Record<string, unknown> | null):
  | {
      ok: true;
      name: string;
      items: LayoutDetail['items'];
      revision: number | undefined;
    }
  | {
      ok: false;
      detail: string;
    } {
  if (!body || typeof body.name !== 'string') {
    return { ok: false, detail: 'name 은 필수입니다.' };
  }
  const name = body.name.trim();
  if (name.length === 0 || name.length > 100) {
    return { ok: false, detail: 'name 은 1~100 자여야 합니다.' };
  }
  if (!Array.isArray(body.items)) {
    return { ok: false, detail: 'items 배열이 필요합니다.' };
  }
  if (body.items.length > 200) {
    return { ok: false, detail: 'items 는 200 개 이하여야 합니다.' };
  }
  const ids = new Set<string>();
  const items: LayoutDetail['items'] = [];
  for (const raw of body.items) {
    if (!raw || typeof raw !== 'object') {
      return { ok: false, detail: 'item 형식이 올바르지 않습니다.' };
    }
    const item = raw as Record<string, unknown>;
    const itemId = typeof item.item_id === 'string' ? item.item_id.trim() : '';
    if (!itemId || itemId.length > 64) {
      return { ok: false, detail: 'item_id 가 필요합니다.' };
    }
    if (ids.has(itemId)) {
      return { ok: false, detail: `중복된 item_id: ${itemId}` };
    }
    ids.add(itemId);
    const kind = item.kind;
    if (kind !== 'OPENING' && kind !== 'SCAFFOLD' && kind !== 'EQUIPMENT') {
      return { ok: false, detail: `지원하지 않는 kind: ${String(kind)}` };
    }
    const center = item.center;
    if (
      !Array.isArray(center) ||
      center.length !== 3 ||
      !center.every((v) => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= 10000)
    ) {
      return { ok: false, detail: `center 는 3 개의 유한 숫자여야 합니다: ${itemId}` };
    }
    const size = item.size;
    if (
      !Array.isArray(size) ||
      size.length !== 3 ||
      !size.every((v) => typeof v === 'number' && Number.isFinite(v) && v > 0 && v <= 10000)
    ) {
      return { ok: false, detail: `size 는 3 개의 양수여야 합니다: ${itemId}` };
    }
    const yawRad = item.yaw_rad === undefined ? 0 : item.yaw_rad;
    if (typeof yawRad !== 'number' || !Number.isFinite(yawRad) || Math.abs(yawRad) > 3.1415927) {
      return { ok: false, detail: `yaw_rad 는 [-π, π] 범위여야 합니다: ${itemId}` };
    }
    let label: string | null = null;
    if (item.label !== undefined && item.label !== null) {
      if (typeof item.label !== 'string') {
        return { ok: false, detail: `label 형식이 올바르지 않습니다: ${itemId}` };
      }
      const trimmed = item.label.trim();
      if (trimmed.length > 100) {
        return { ok: false, detail: `label 은 100 자 이하여야 합니다: ${itemId}` };
      }
      label = trimmed.length > 0 ? trimmed : null;
    }
    items.push({
      item_id: itemId,
      kind,
      center: [center[0] as number, center[1] as number, center[2] as number],
      size: [size[0] as number, size[1] as number, size[2] as number],
      yaw_rad: yawRad,
      label,
    });
  }
  const revision = typeof body.revision === 'number' ? body.revision : undefined;
  return { ok: true, name, items, revision };
}

/** 처음에 있는 작업. 날짜는 오늘이라 개발 서버를 언제 열어도 오늘 작업 목록이 채워진다. */
const initialWorks = (date: string): TbmWorkUnit[] => [
  {
    work_id: '1',
    date,
    name: '개구부 옆 비계 설치',
    from_ms: dateHhmmToMs(date, '09:00'),
    to_ms: dateHhmmToMs(date, '12:00'),
    zone_id: 'z-04',
    place: null,
    planned_people: 4,
    lead: 'A조',
    layout_id: 101,
    work_type: 'SCAFFOLD_INSTALL',
    event_counts: { CRITICAL: 1, HIGH: 1 },
  },
  {
    work_id: '2',
    date,
    name: '자재 반입 · 정리',
    from_ms: dateHhmmToMs(date, '10:00'),
    to_ms: dateHhmmToMs(date, '11:30'),
    zone_id: 'z-02',
    place: null,
    planned_people: 3,
    lead: 'B조',
    layout_id: 102,
    work_type: 'MATERIAL_HANDLING',
    event_counts: { CRITICAL: 0, HIGH: 2 },
  },
  {
    work_id: '3',
    date,
    name: '바닥 마감',
    from_ms: dateHhmmToMs(date, '13:00'),
    to_ms: dateHhmmToMs(date, '17:00'),
    zone_id: null,
    place: '북서쪽 바닥',
    planned_people: 2,
    lead: 'A조',
    layout_id: 103,
    work_type: 'FLOOR_FINISHING',
    event_counts: { CRITICAL: 0, HIGH: 0 },
  },
];

const COMMON_CITATIONS: readonly TbmReportCitation[] = [
  {
    node_id: 'OSH-STD-A43',
    source_name: '산업안전보건기준에 관한 규칙',
    article_ref: '제43조',
    title: '개구부 등의 방호조치',
    excerpt:
      '사업주는 작업발판 및 통로의 끝이나 개구부로서 근로자가 추락할 위험이 있는 장소에는 안전난간, 울타리, 수직형 추락방망 또는 덮개 등 방호조치를 충분한 강도를 가진 구조로 튼튼하게 설치하여야 한다.',
  },
  {
    node_id: 'OSH-STD-A42',
    source_name: '산업안전보건기준에 관한 규칙',
    article_ref: '제42조',
    title: '추락의 방지',
    excerpt:
      '사업주는 근로자가 추락하거나 넘어질 위험이 있는 장소 또는 기계·설비·선박블록 등에서 작업을 할 때에 근로자가 위험해질 우려가 있는 경우 비계를 조립하는 등의 방법으로 작업발판을 설치하여야 한다.',
  },
  {
    node_id: 'OSH-STD-A44',
    source_name: '산업안전보건기준에 관한 규칙',
    article_ref: '제44조',
    title: '안전대의 부착설비 등',
    excerpt:
      '사업주는 추락할 위험이 있는 높이 2미터 이상의 장소에서 근로자에게 안전대를 착용시킨 경우 안전대를 안전하게 걸어 사용할 수 있는 설비 등을 설치하여야 한다.',
  },
  {
    node_id: 'OSH-STD-A56',
    source_name: '산업안전보건기준에 관한 규칙',
    article_ref: '제56조',
    title: '작업발판의 구조',
    excerpt:
      '사업주는 비계의 높이가 2미터 이상인 작업장소에는 발판재료 간의 틈이 3센티미터 이하이고 폭이 40센티미터 이상인 작업발판을 설치하여야 한다.',
  },
];

const COMMON_SUBGRAPH_NODES: readonly SubgraphNode[] = [
  {
    node_id: 'OSH-STD-A43',
    node_kind: 'ARTICLE',
    layer: 'OSH_STANDARD',
    source_name: '산업안전보건기준에 관한 규칙',
    article_ref: '제43조',
    title: '개구부 등의 방호조치',
    rule_tags: ['R1', 'R2'],
    hop: 0,
    cited: true,
  },
  {
    node_id: 'OSH-STD-A42',
    node_kind: 'ARTICLE',
    layer: 'OSH_STANDARD',
    source_name: '산업안전보건기준에 관한 규칙',
    article_ref: '제42조',
    title: '추락의 방지',
    rule_tags: ['R2', 'R3'],
    hop: 0,
    cited: true,
  },
  {
    node_id: 'OSH-STD-A44',
    node_kind: 'ARTICLE',
    layer: 'OSH_STANDARD',
    source_name: '산업안전보건기준에 관한 규칙',
    article_ref: '제44조',
    title: '안전대의 부착설비 등',
    rule_tags: ['R2', 'R3'],
    hop: 0,
    cited: true,
  },
  {
    node_id: 'OSH-STD-A56',
    node_kind: 'ARTICLE',
    layer: 'OSH_STANDARD',
    source_name: '산업안전보건기준에 관한 규칙',
    article_ref: '제56조',
    title: '작업발판의 구조',
    rule_tags: ['R2', 'R3'],
    hop: 0,
    cited: true,
  },
  {
    node_id: 'OSH-STD-A13',
    node_kind: 'ARTICLE',
    layer: 'OSH_STANDARD',
    source_name: '산업안전보건기준에 관한 규칙',
    article_ref: '제13조',
    title: '안전난간의 구조 및 설치요건',
    rule_tags: ['R2'],
    hop: 1,
    cited: false,
  },
  {
    node_id: 'OSH-ACT-A38',
    node_kind: 'ARTICLE',
    layer: 'LAW',
    source_name: '산업안전보건법',
    article_ref: '제38조',
    title: '안전조치',
    rule_tags: ['R1', 'R2', 'R3'],
    hop: 1,
    cited: false,
  },
];

const COMMON_SUBGRAPH_EDGES: readonly SubgraphEdge[] = [
  {
    edge_id: 'E-43-13',
    src_node_id: 'OSH-STD-A43',
    dst_node_id: 'OSH-STD-A13',
    edge_kind: 'REFERENCES',
    relation_label: '안전난간 구조 준용',
  },
  {
    edge_id: 'E-42-44',
    src_node_id: 'OSH-STD-A42',
    dst_node_id: 'OSH-STD-A44',
    edge_kind: 'REFERENCES',
    relation_label: '안전대 부착설비 연계',
  },
  {
    edge_id: 'E-42-56',
    src_node_id: 'OSH-STD-A42',
    dst_node_id: 'OSH-STD-A56',
    edge_kind: 'REFERENCES',
    relation_label: '비계 작업발판 기준',
  },
  {
    edge_id: 'E-38-42',
    src_node_id: 'OSH-ACT-A38',
    dst_node_id: 'OSH-STD-A42',
    edge_kind: 'DELEGATES',
    relation_label: '추락 위험 예방 위임',
  },
  {
    edge_id: 'E-38-43',
    src_node_id: 'OSH-ACT-A38',
    dst_node_id: 'OSH-STD-A43',
    edge_kind: 'DELEGATES',
    relation_label: '개구부 방호조치 위임',
  },
];

const COMMON_STEPS: readonly TbmReportStep[] = [
  {
    step: 'CANDIDATE_SEARCH',
    label: '법령 후보 검색 (1차)',
    status: 'DONE',
    input_count: 3,
    output_count: 4,
    duration_ms: 42,
    detail: 'rule_tags=[R1, R2, R3], work_type 매칭 조문 4건',
  },
  {
    step: 'GRAPH_EXPAND',
    label: '법령 지식 그래프 확장 (1-hop)',
    status: 'DONE',
    input_count: 4,
    output_count: 6,
    duration_ms: 18,
    detail: 'REFERENCES·DELEGATES 엣지 5개 확장 (노드 6개)',
  },
  {
    step: 'LLM_GENERATE',
    label: 'LLM 보고서 초안 생성',
    status: 'DONE',
    input_count: 6,
    output_count: 4,
    duration_ms: 1420,
    detail: 'gpt-4o-mini · sections 4개 · 인용 4건 생성',
  },
  {
    step: 'CITATION_VALIDATE',
    label: '인용·수치 결정론 검증',
    status: 'DONE',
    input_count: 4,
    output_count: 4,
    duration_ms: 7,
    detail: '화이트리스트 인용 4/4 통과 · 미인용 조문 제거 완료',
  },
];

function buildReportForWork(
  work: TbmWorkUnit,
  reportId: number,
  version: number,
  status: 'GENERATING' | 'SUCCEEDED' | 'FAILED' = 'SUCCEEDED',
  failureReason: string | null = null,
): TbmReport {
  const workTypeObj = work.work_type
    ? { code: work.work_type, label: workTypeLabelOf(work.work_type) ?? work.work_type }
    : null;
  const header = {
    date: work.date,
    name: work.name,
    from_ms: work.from_ms,
    to_ms: work.to_ms,
    zone_id: work.zone_id,
    place: work.place,
    planned_people: work.planned_people,
    lead: work.lead,
  };

  if (status === 'GENERATING') {
    return {
      report_id: reportId,
      work_id: work.work_id,
      report_version: version,
      status: 'GENERATING',
      failure_reason: null,
      layout_id: work.layout_id ?? null,
      layout_revision: 3,
      work_type: workTypeObj,
      header,
      rule_version: 3,
      risk_items: [],
      zone_history: [],
      sections: [],
      citations: [],
      subgraph: { nodes: [], edges: [] },
      steps: [
        {
          step: 'CANDIDATE_SEARCH',
          label: '법령 후보 검색 (1차)',
          status: 'RUNNING',
          input_count: 2,
          output_count: 0,
          duration_ms: 12,
          detail: '후보 조문 탐색 및 물리 위험 평가 대기 중',
        },
      ],
      llm_model: null,
      created_at_ms: 1760573100000,
      completed_at_ms: null,
    };
  }

  if (status === 'FAILED') {
    return {
      report_id: reportId,
      work_id: work.work_id,
      report_version: version,
      status: 'FAILED',
      failure_reason: failureReason ?? 'CITATION_VIOLATION',
      layout_id: work.layout_id ?? null,
      layout_revision: 3,
      work_type: workTypeObj,
      header,
      rule_version: 3,
      risk_items: [
        {
          rule_id: 'R2',
          rule_version: 3,
          severity: 'CRITICAL',
          position_ids: ['pos-01'],
          zone_id: work.zone_id ?? 'z-04',
          metrics: { edge_distance_m: 0.6, height_diff_m: 2.1 },
          reason_codes: ['edge_within_raise_distance', 'standing_surface_elevated'],
          confidence_reasons: ['high_confidence'],
          item: '비계 S1 ↔ 개구부',
          why: '수평 0.6m · 높이 차 2.1m',
        },
      ],
      zone_history: [],
      sections: [],
      citations: [],
      subgraph: {
        nodes: COMMON_SUBGRAPH_NODES.map((n) => ({ ...n, cited: false })),
        edges: [...COMMON_SUBGRAPH_EDGES],
      },
      steps: [
        COMMON_STEPS[0]!,
        COMMON_STEPS[1]!,
        COMMON_STEPS[2]!,
        {
          step: 'CITATION_VALIDATE',
          label: '인용·수치 결정론 검증',
          status: 'FAILED',
          input_count: 4,
          output_count: 0,
          duration_ms: 9,
          detail: `검증 실패: ${failureReason ?? 'CITATION_VIOLATION'}`,
        },
      ],
      llm_model: 'gpt-4o-mini',
      created_at_ms: 1760573100000,
      completed_at_ms: 1760573102000,
    };
  }

  if (work.work_id === '2') {
    return {
      report_id: reportId,
      work_id: work.work_id,
      report_version: version,
      status: 'SUCCEEDED',
      failure_reason: null,
      layout_id: work.layout_id ?? 102,
      layout_revision: 1,
      work_type: workTypeObj,
      header,
      rule_version: 3,
      risk_items: [
        {
          rule_id: 'R1',
          rule_version: 3,
          severity: 'HIGH',
          position_ids: ['eq-forklift-path'],
          zone_id: 'z-04',
          metrics: { zone_distance_m: 0.0 },
          reason_codes: ['xy_inside_polygon'],
          confidence_reasons: ['high_confidence'],
          item: '지게차 동선 ↔ z-04',
          why: '동선이 위험 구역을 2.8m 가로지름',
        },
        {
          rule_id: 'R1',
          rule_version: 3,
          severity: 'HIGH',
          position_ids: ['eq-parking-p1'],
          zone_id: 'z-02',
          metrics: { zone_distance_m: 0.9 },
          reason_codes: ['xy_uncertainty_overlap'],
          confidence_reasons: ['high_confidence'],
          item: '통로 ↔ 장비 P1',
          why: '통로 폭 0.9m 겹침',
        },
      ],
      zone_history: [
        {
          zone_id: 'z-02',
          days: 7,
          live_event_count: 3,
          rules: ['R1'],
          max_severity: 'HIGH',
          last_at_ms: 1760515200000,
        },
      ],
      sections: [
        {
          key: 'hazards',
          title: '잠재 위험요인',
          body: '지게차 이동 동선이 개구부 구역(z-04)을 가로지르며(R1 HIGH), 장비 주차 P1 구간이 보행 통로 폭 0.9m와 겹칩니다(R1 HIGH).',
          citations: ['OSH-STD-A43'],
        },
        {
          key: 'measures',
          title: '안전조치 대책',
          body: '반입 경로에 유도자 1명 배치\n장비 주차 P1에 표지와 차단봉 설치',
          citations: ['OSH-STD-A43', 'OSH-STD-A44'],
        },
        {
          key: 'focus',
          title: '중점 위험 및 작업 전 확인',
          body: '지게차 동선 ↔ z-04(R1 HIGH) 구간의 유도자 배치 및 개구부 방호울타리 간섭 여부를 작업 전 확인합니다.',
          citations: ['OSH-STD-A43'],
        },
        {
          key: 'legal',
          title: '관련 법령 근거 요약',
          body: '산업안전보건기준에 관한 규칙 제43조(개구부 등의 방호조치)에 따라 개구부 인접 자재 반입 동선에 울타리·표지 등 방호조치를 유지해야 합니다.',
          citations: ['OSH-STD-A43'],
        },
      ],
      citations: COMMON_CITATIONS.filter(
        (c) => c.node_id === 'OSH-STD-A43' || c.node_id === 'OSH-STD-A44',
      ),
      subgraph: {
        nodes: COMMON_SUBGRAPH_NODES.map((n) => ({
          ...n,
          cited: n.node_id === 'OSH-STD-A43' || n.node_id === 'OSH-STD-A44',
        })),
        edges: [...COMMON_SUBGRAPH_EDGES],
      },
      steps: [...COMMON_STEPS],
      llm_model: 'gpt-4o-mini',
      created_at_ms: 1760573100000,
      completed_at_ms: 1760573102000,
    };
  }

  if (work.work_id === '3') {
    return {
      report_id: reportId,
      work_id: work.work_id,
      report_version: version,
      status: 'SUCCEEDED',
      failure_reason: null,
      layout_id: work.layout_id ?? 103,
      layout_revision: 1,
      work_type: workTypeObj,
      header,
      rule_version: 3,
      risk_items: [
        {
          rule_id: 'R1',
          rule_version: 3,
          severity: 'CAUTION',
          position_ids: ['area-m1'],
          zone_id: null,
          metrics: { zone_distance_m: 0.2 },
          reason_codes: ['xy_uncertainty_overlap'],
          confidence_reasons: ['high_confidence'],
          item: '마감 구역 M1 ↔ 출입 동선',
          why: '젖은 바닥 · 출입문에서 0.2m',
        },
      ],
      zone_history: [],
      sections: [
        {
          key: 'hazards',
          title: '잠재 위험요인',
          body: '북서쪽 바닥 마감 구역(M1)이 출입문에서 0.2m 인접하여(R1 CAUTION) 통행 중 미끄러짐·전도 위험이 있습니다.',
          citations: ['OSH-STD-A42'],
        },
        {
          key: 'measures',
          title: '안전조치 대책',
          body: '출입문 쪽 우회 동선 표시\n미끄럼 주의 표지 설치',
          citations: ['OSH-STD-A42'],
        },
        {
          key: 'focus',
          title: '중점 위험 및 작업 전 확인',
          body: '마감 구역 M1 ↔ 출입 동선(R1 CAUTION) 구간에 우회 동선 안내와 미끄럼 주의 표지를 작업 시작 전 설치합니다.',
          citations: ['OSH-STD-A42'],
        },
        {
          key: 'legal',
          title: '관련 법령 근거 요약',
          body: '산업안전보건기준에 관한 규칙 제42조(추락의 방지)에 따라 근로자가 넘어질 위험이 있는 작업장 바닥에 안전 통로 및 방호 표지를 확보합니다.',
          citations: ['OSH-STD-A42'],
        },
      ],
      citations: COMMON_CITATIONS.filter((c) => c.node_id === 'OSH-STD-A42'),
      subgraph: {
        nodes: COMMON_SUBGRAPH_NODES.map((n) => ({
          ...n,
          cited: n.node_id === 'OSH-STD-A42',
        })),
        edges: [...COMMON_SUBGRAPH_EDGES],
      },
      steps: [...COMMON_STEPS],
      llm_model: 'gpt-4o-mini',
      created_at_ms: 1760573100000,
      completed_at_ms: 1760573102000,
    };
  }

  // 기본(work_id === '1' 및 새로 등록된 작업)
  return {
    report_id: reportId,
    work_id: work.work_id,
    report_version: version,
    status: 'SUCCEEDED',
    failure_reason: null,
    layout_id: work.layout_id ?? 101,
    layout_revision: 3,
    work_type: workTypeObj,
    header,
    rule_version: 3,
    risk_items: [
      {
        rule_id: 'R2',
        rule_version: 3,
        severity: 'CRITICAL',
        position_ids: ['pos-01'],
        zone_id: work.zone_id ?? 'z-04',
        metrics: { edge_distance_m: 0.6, height_diff_m: 2.1 },
        reason_codes: ['edge_within_raise_distance'],
        confidence_reasons: ['high_confidence'],
        item: '비계 S1 ↔ 개구부',
        why: '수평 0.6m · 높이 차 2.1m',
      },
      {
        rule_id: 'R3',
        rule_version: 3,
        severity: 'HIGH',
        position_ids: ['pos-01'],
        zone_id: work.zone_id ?? 'z-04',
        metrics: { surface_height_m: 2.4 },
        reason_codes: ['standing_on_surface', 'standing_surface_elevated'],
        confidence_reasons: ['high_confidence'],
        item: '비계 S1 상부',
        why: '작업면 2.4m · 난간 없음',
      },
    ],
    zone_history: [
      {
        zone_id: 'z-04',
        days: 7,
        live_event_count: 5,
        rules: ['R2', 'R3'],
        max_severity: 'CRITICAL',
        last_at_ms: 1760515200000,
      },
    ],
    sections: [
      {
        key: 'hazards',
        title: '잠재 위험요인',
        body: '비계 S1이 개구부 구역(z-04) 단부에서 수평 0.6m·높이 차 2.1m에 위치하여 추락 위험(R2 CRITICAL)이 있으며, 높이 2.4m 비계 상부에 안전난간이 없는 상태(R3 HIGH)입니다.',
        citations: ['OSH-STD-A42', 'OSH-STD-A43', 'OSH-STD-A56'],
      },
      {
        key: 'measures',
        title: '안전조치 대책',
        body: '개구부 덮개와 안전 난간을 비계보다 먼저 설치\n비계 위 작업 전 안전대 체결 확인',
        citations: ['OSH-STD-A42', 'OSH-STD-A43', 'OSH-STD-A44', 'OSH-STD-A56'],
      },
      {
        key: 'focus',
        title: '중점 위험 및 작업 전 확인',
        body: '비계 S1 ↔ 개구부(R2 CRITICAL, 수평 0.6m·높이 차 2.1m) 구간의 개구부 덮개·안전난간 선행 설치와 안전대 부착설비 체결을 최우선 확인합니다.',
        citations: ['OSH-STD-A43', 'OSH-STD-A44'],
      },
      {
        key: 'legal',
        title: '관련 법령 근거 요약',
        body: '산업안전보건기준에 관한 규칙 제42조(추락의 방지), 제43조(개구부 등의 방호조치), 제44조(안전대의 부착설비 등), 제56조(작업발판의 구조)에 따라 개구부 방호조치 및 2m 이상 비계 작업발판·안전대 설비를 확보해야 합니다.',
        citations: ['OSH-STD-A42', 'OSH-STD-A43', 'OSH-STD-A44', 'OSH-STD-A56'],
      },
    ],
    citations: [...COMMON_CITATIONS],
    subgraph: {
      nodes: [...COMMON_SUBGRAPH_NODES],
      edges: [...COMMON_SUBGRAPH_EDGES],
    },
    steps: [...COMMON_STEPS],
    llm_model: 'gpt-4o-mini',
    created_at_ms: 1760573100000,
    completed_at_ms: 1760573102000,
  };
}

export interface CreateTbmMockOptions {
  /**
   * true 이면 GET /api/work-units?days=... 요청을 다음 핸들러(createHistoryMock)에 넘긴다.
   */
  deferDaysToHistory?: boolean;
  /**
   * POST /api/reports/tbm 후 몇 번의 GET 폴링 뒤에 완료 상태로 바꿀지 설정한다(기본 1: 첫 폴링에서 완료).
   */
  pollsBeforeComplete?: number;
  /**
   * 다음에 생성되는 보고서의 최종 상태를 지정한다(기본 'SUCCEEDED').
   */
  nextReportOutcome?: {
    status: 'SUCCEEDED' | 'FAILED';
    failureReason?: string;
  };
}

export function createTbmMock(options: CreateTbmMockOptions = {}) {
  const workTypes: WorkTypeOption[] = WORK_TYPE_OPTIONS.map((w) => ({
    ...w,
    rule_tags: [...w.rule_tags],
  }));
  const layouts: LayoutDetail[] = INITIAL_LAYOUTS.map((l) => ({
    ...l,
    items: l.items.map((i) => ({ ...i })),
  }));
  const layoutRevisions = new Map<string, LayoutRevisionRecord>();
  for (const l of layouts) {
    layoutRevisions.set(`${l.layout_id}:${l.revision}`, {
      layout_id: l.layout_id,
      revision: l.revision,
      layout_version: `lv-${l.revision}`,
      name: l.name,
      items: l.items.map((i) => ({ ...i })),
      created_at: l.updated_at ?? l.created_at ?? 1760572800000,
    });
  }
  const today = todayIsoDate();
  const works: TbmWorkUnit[] = initialWorks(today).map((w) => ({ ...w, status: 'PLANNED' }));

  let nextLayoutSeq = 104;
  let nextWorkSeq = 4;
  let nextReportSeq = 300;
  const pollsBeforeComplete = options.pollsBeforeComplete ?? 1;
  let nextOutcome = options.nextReportOutcome ?? { status: 'SUCCEEDED' as const };

  // work_id 별 보고서 버전 목록(오래된 순 → 최신 순)
  const reportsByWork = new Map<string, TbmReport[]>();
  const pollCounts = new Map<number, number>();
  const uploadedSignatures = new Map<string, Uint8Array>();
  const signatures = new Map<
    number,
    {
      signature_id: string;
      signer_name: string;
      signer_role: 'PARTICIPANT' | 'APPROVER';
      signed_at: number;
      report_version: number;
      image_url: string;
    }[]
  >();
  const pendingOutcomeByReport = new Map<
    number,
    { status: 'SUCCEEDED' | 'FAILED'; failureReason?: string }
  >();

  for (const w of works) {
    const rep = buildReportForWork(w, ++nextReportSeq, 1, 'SUCCEEDED');
    reportsByWork.set(w.work_id, [rep]);
  }

  const findReportById = (reportId: number): TbmReport | undefined => {
    for (const list of reportsByWork.values()) {
      const found = list.find((r) => r.report_id === reportId);
      if (found) return found;
    }
    return undefined;
  };

  const handlers = [
    http.post('*/api/uploads/sign', async ({ request }) => {
      const body = (await request.json()) as { purpose?: string };
      if (body.purpose !== 'REPORT_SIGNATURE') return undefined;
      const key = crypto.randomUUID();
      return HttpResponse.json({
        object_key: `signatures/${key}.png`,
        upload_url: new URL(`/mock-signature-upload/${key}`, request.url).toString(),
        headers: { 'Content-Type': 'image/png' },
      });
    }),
    http.put('*/mock-signature-upload/:key', async ({ request, params }) => {
      uploadedSignatures.set(String(params.key), new Uint8Array(await request.arrayBuffer()));
      return new HttpResponse(null, { status: 200 });
    }),
    http.get(
      '*/mock-signature-image/:key',
      ({ params }) =>
        new HttpResponse(uploadedSignatures.get(String(params.key)) ?? null, {
          headers: { 'Content-Type': 'image/png' },
        }),
    ),
    http.get('*/api/reports/tbm/:reportId/signatures', ({ params, request }) => {
      const report = findReportById(Number(params.reportId));
      if (
        !report ||
        Number(new URL(request.url).searchParams.get('version')) !== report.report_version
      )
        return HttpResponse.json({ items: [] });
      return HttpResponse.json({ items: signatures.get(report.report_id) ?? [] });
    }),
    http.post('*/api/reports/tbm/:reportId/signatures', async ({ params, request }) => {
      const report = findReportById(Number(params.reportId));
      if (!report) return HttpResponse.json({ code: 'REPORT_NOT_FOUND' }, { status: 404 });
      if (report.approved_at != null)
        return HttpResponse.json({ code: 'REPORT_APPROVED' }, { status: 409 });
      const body = (await request.json()) as {
        report_version: number;
        signer_name: string;
        image_key: string;
        notice_acknowledged: boolean;
        signer_role?: 'PARTICIPANT' | 'APPROVER';
      };
      const role = body.signer_role ?? 'PARTICIPANT';
      const latest = reportsByWork.get(report.work_id)?.at(-1);
      if (latest?.report_id !== report.report_id || body.report_version !== report.report_version)
        return HttpResponse.json({ code: 'REPORT_VERSION_STALE' }, { status: 409 });
      if (report.status !== 'SUCCEEDED')
        return HttpResponse.json({ code: 'REPORT_NOT_READY' }, { status: 409 });
      if (
        !['PARTICIPANT', 'APPROVER'].includes(role) ||
        !body.notice_acknowledged ||
        !body.signer_name?.trim() ||
        body.signer_name.trim().length > 40
      )
        return HttpResponse.json({ code: 'INVALID_REQUEST' }, { status: 400 });
      const now = Date.now();
      report.locked_at ??= now;
      report.content_sha256 ??= 'a'.repeat(64);
      const entry = {
        signature_id: crypto.randomUUID(),
        signer_name: body.signer_name,
        signer_role: role,
        signed_at: now,
        report_version: report.report_version,
        image_url: new URL(
          `/mock-signature-image/${body.image_key
            .split('/')
            .at(-1)
            ?.replace(/\.png$/, '')}`,
          request.url,
        ).toString(),
      };
      const entries = signatures.get(report.report_id) ?? [];
      entries.push(entry);
      signatures.set(report.report_id, entries);
      report.signature_count = entries.length;
      if (role === 'APPROVER') {
        report.approved_at = now;
        report.approved_by_name = readMockUser(request.headers.get('Authorization'))?.id ?? 'admin';
      }
      return HttpResponse.json(
        {
          ...entry,
          report_id: report.report_id,
          locked_at: report.locked_at,
          content_sha256: report.content_sha256,
        },
        { status: 201 },
      );
    }),
    http.get('*/api/work-types', () => HttpResponse.json({ work_types: workTypes })),

    http.get('*/api/layouts', () =>
      HttpResponse.json(
        layouts.map(({ items, ...summary }) => ({
          ...summary,
          layout_version: summary.layout_version ?? `lv-${summary.revision}`,
          source: summary.source ?? 'PLANNED',
          item_count: items.length,
        })),
      ),
    ),

    http.get('*/api/layouts/:id/revisions/:revision', ({ params }) => {
      const id = Number(params.id);
      const rev = Number(params.revision);
      const record = layoutRevisions.get(`${id}:${rev}`);
      if (!record) {
        return HttpResponse.json(
          {
            status: 404,
            code: 'LAYOUT_NOT_FOUND',
            detail: `레이아웃 리비전이 없습니다: ${id}#${rev}`,
          },
          { status: 404 },
        );
      }
      return HttpResponse.json(record);
    }),

    http.get('*/api/layouts/:id', ({ params }) => {
      const id = Number(params.id);
      const layout = layouts.find((l) => l.layout_id === id);
      if (!layout) {
        return HttpResponse.json(
          { status: 404, code: 'LAYOUT_NOT_FOUND', detail: `레이아웃이 없습니다: ${id}` },
          { status: 404 },
        );
      }
      return HttpResponse.json(layout);
    }),

    http.post('*/api/layouts', async ({ request }) => {
      const body = (await request.json().catch(() => null)) as Record<string, unknown> | null;
      const validated = validateLayoutPayload(body);
      if (!validated.ok) {
        return HttpResponse.json(
          { status: 400, code: 'INVALID_LAYOUT', detail: validated.detail },
          { status: 400 },
        );
      }
      const now = Date.now();
      const created: LayoutDetail = {
        layout_id: nextLayoutSeq++,
        name: validated.name,
        revision: 1,
        layout_version: 'lv-1',
        source: 'PLANNED',
        item_count: validated.items.length,
        created_at: now,
        updated_at: now,
        items: validated.items,
      };
      layouts.unshift(created);
      layoutRevisions.set(`${created.layout_id}:1`, {
        layout_id: created.layout_id,
        revision: 1,
        layout_version: 'lv-1',
        name: created.name,
        items: created.items.map((i) => ({ ...i })),
        created_at: now,
      });
      return HttpResponse.json(created, { status: 201 });
    }),

    http.put('*/api/layouts/:id', async ({ params, request }) => {
      const id = Number(params.id);
      const body = (await request.json().catch(() => null)) as Record<string, unknown> | null;
      const validated = validateLayoutPayload(body);
      if (!validated.ok) {
        return HttpResponse.json(
          { status: 400, code: 'INVALID_LAYOUT', detail: validated.detail },
          { status: 400 },
        );
      }
      const idx = layouts.findIndex((l) => l.layout_id === id);
      if (idx < 0) {
        return HttpResponse.json(
          { status: 404, code: 'LAYOUT_NOT_FOUND', detail: `레이아웃이 없습니다: ${id}` },
          { status: 404 },
        );
      }
      const current = layouts[idx]!;
      if (validated.revision !== undefined && validated.revision !== current.revision) {
        return HttpResponse.json(
          {
            status: 409,
            code: 'LAYOUT_REVISION_CONFLICT',
            detail: `revision 충돌: 요청=${validated.revision}`,
          },
          { status: 409 },
        );
      }
      const nextRevision = current.revision + 1;
      const now = Date.now();
      const updated: LayoutDetail = {
        ...current,
        name: validated.name,
        revision: nextRevision,
        layout_version: `lv-${nextRevision}`,
        source: current.source ?? 'PLANNED',
        item_count: validated.items.length,
        updated_at: now,
        items: validated.items,
      };
      layouts[idx] = updated;
      layoutRevisions.set(`${updated.layout_id}:${nextRevision}`, {
        layout_id: updated.layout_id,
        revision: nextRevision,
        layout_version: `lv-${nextRevision}`,
        name: updated.name,
        items: updated.items.map((i) => ({ ...i })),
        created_at: now,
      });
      return HttpResponse.json(updated, { status: 200 });
    }),

    http.post('*/api/work-units/:id/start', ({ params }) => {
      const work = works.find((w) => w.work_id === String(params.id));
      if (!work) return HttpResponse.json({ code: 'WORK_NOT_FOUND' }, { status: 404 });
      const active = works.find((w) => w.status === 'IN_PROGRESS');
      if (active)
        return HttpResponse.json(
          { code: 'WORK_ALREADY_IN_PROGRESS', detail: active.name },
          { status: 409 },
        );
      if (work.status !== 'PLANNED')
        return HttpResponse.json({ code: 'WORK_INVALID_STATE' }, { status: 409 });
      work.status = 'IN_PROGRESS';
      work.actual_started_at = Date.now();
      return HttpResponse.json(work);
    }),
    http.post('*/api/work-units/:id/end', ({ params }) => {
      const work = works.find((w) => w.work_id === String(params.id));
      if (!work || work.status !== 'IN_PROGRESS')
        return HttpResponse.json({ code: 'WORK_INVALID_STATE' }, { status: 409 });
      work.status = 'DONE';
      work.actual_ended_at = Date.now();
      const list = reportsByWork.get(work.work_id) ?? [];
      const previous = list.at(-1);
      if (!previous)
        return HttpResponse.json(
          { work_unit: work, wrap_up: { status: 'SKIPPED', report_id: null, reason: 'NO_REPORT' } },
          { status: 202 },
        );
      const report = {
        ...previous,
        report_id: ++nextReportSeq,
        report_version: previous.report_version + 1,
        status: 'GENERATING' as const,
        wrap_up_status: 'GENERATING',
        wrap_up_summary: null,
        locked_at: null,
        content_sha256: null,
        approved_at: null,
        approved_by_name: null,
        signature_count: 0,
      };
      list.push(report);
      reportsByWork.set(work.work_id, list);
      return HttpResponse.json(
        {
          work_unit: work,
          wrap_up: { status: 'GENERATING', report_id: report.report_id, reason: null },
        },
        { status: 202 },
      );
    }),

    http.get('*/api/work-units', ({ request }) => {
      const url = new URL(request.url);
      if (options.deferDaysToHistory && url.searchParams.has('days')) {
        return undefined;
      }
      return HttpResponse.json({ works });
    }),

    http.post('*/api/work-units', async ({ request }) => {
      const body = (await request.json().catch(() => null)) as Record<string, unknown> | null;
      if (!body || typeof body.name !== 'string') {
        return HttpResponse.json(
          { status: 400, code: 'INVALID_WORK_UNIT', detail: '작업 이름이 필요합니다.' },
          { status: 400 },
        );
      }
      const created: TbmWorkUnit = {
        work_id: String(nextWorkSeq++),
        status: 'PLANNED',
        date: typeof body.date === 'string' ? body.date : today,
        name: body.name,
        from_ms: typeof body.from_ms === 'number' ? body.from_ms : dateHhmmToMs(today, '09:00'),
        to_ms: typeof body.to_ms === 'number' ? body.to_ms : dateHhmmToMs(today, '12:00'),
        zone_id: typeof body.zone_id === 'string' ? body.zone_id : null,
        place: typeof body.place === 'string' ? body.place : null,
        planned_people: typeof body.planned_people === 'number' ? body.planned_people : 1,
        // 서버처럼 조 이름을 받지 않으면 비워 둔다(지어내지 않는다).
        lead: typeof body.lead === 'string' && body.lead !== '' ? body.lead : null,
        layout_id: typeof body.layout_id === 'number' ? body.layout_id : null,
        work_type: typeof body.work_type === 'string' ? body.work_type : null,
        event_counts: { CRITICAL: 0, HIGH: 0 },
      };
      works.push(created);
      return HttpResponse.json(created, { status: 201 });
    }),

    http.put('*/api/work-units/:id', async ({ params, request }) => {
      const id = String(params.id);
      const idx = works.findIndex((w) => w.work_id === id);
      if (idx < 0) {
        return HttpResponse.json(
          { status: 404, code: 'WORK_NOT_FOUND', detail: '작업을 찾을 수 없습니다.' },
          { status: 404 },
        );
      }
      const body = (await request.json().catch(() => null)) as Record<string, unknown> | null;
      if (!body) {
        return HttpResponse.json(
          { status: 400, code: 'INVALID_WORK_UNIT', detail: '요청 본문이 올바르지 않습니다.' },
          { status: 400 },
        );
      }
      const prev = works[idx]!;
      const updated: TbmWorkUnit = {
        ...prev,
        date: typeof body.date === 'string' ? body.date : prev.date,
        name: typeof body.name === 'string' ? body.name : prev.name,
        from_ms: typeof body.from_ms === 'number' ? body.from_ms : prev.from_ms,
        to_ms: typeof body.to_ms === 'number' ? body.to_ms : prev.to_ms,
        zone_id:
          body.zone_id === null || typeof body.zone_id === 'string' ? body.zone_id : prev.zone_id,
        place: body.place === null || typeof body.place === 'string' ? body.place : prev.place,
        planned_people:
          typeof body.planned_people === 'number' ? body.planned_people : prev.planned_people,
        lead: body.lead === null || typeof body.lead === 'string' ? body.lead : prev.lead,
        layout_id:
          body.layout_id === null || typeof body.layout_id === 'number'
            ? body.layout_id
            : prev.layout_id,
        work_type:
          body.work_type === null || typeof body.work_type === 'string'
            ? body.work_type
            : prev.work_type,
      };
      works[idx] = updated;
      return HttpResponse.json(updated);
    }),

    http.delete('*/api/work-units/:id', ({ params }) => {
      const id = String(params.id);
      const idx = works.findIndex((w) => w.work_id === id);
      if (idx >= 0) works.splice(idx, 1);
      return new HttpResponse(null, { status: 204 });
    }),

    http.get('*/api/reports/tbm', ({ request }) => {
      const url = new URL(request.url);
      const workId = url.searchParams.get('work_id');
      if (!workId) {
        return HttpResponse.json(
          { status: 400, code: 'WORK_ID_REQUIRED', detail: 'work_id 쿼리가 필요합니다.' },
          { status: 400 },
        );
      }
      const list = reportsByWork.get(workId) ?? [];
      const latest = list.length > 0 ? list[list.length - 1]! : null;
      const versions = [...list].reverse().map((r) => ({
        report_id: r.report_id,
        report_version: r.report_version,
        status: r.status,
        created_at_ms: r.created_at_ms,
      }));
      return HttpResponse.json({ latest, versions });
    }),

    http.get('*/api/reports/tbm/:reportId', ({ params }) => {
      const reportId = Number(params.reportId);
      const report = findReportById(reportId);
      if (!report) {
        return HttpResponse.json(
          { status: 404, code: 'REPORT_NOT_FOUND', detail: '보고서를 찾을 수 없습니다.' },
          { status: 404 },
        );
      }
      if (report.status === 'GENERATING') {
        const count = (pollCounts.get(reportId) ?? 0) + 1;
        pollCounts.set(reportId, count);
        if (count >= pollsBeforeComplete) {
          const work = works.find((w) => w.work_id === report.work_id);
          const outcome = pendingOutcomeByReport.get(reportId) ?? { status: 'SUCCEEDED' };
          if (work) {
            const completed = buildReportForWork(
              work,
              report.report_id,
              report.report_version,
              outcome.status,
              outcome.failureReason ?? null,
            );
            Object.assign(
              report,
              completed,
              report.wrap_up_status === 'GENERATING'
                ? {
                    wrap_up_status: 'SUCCEEDED',
                    wrap_up_summary: '작업 구간의 위험 이벤트 집계를 확인했습니다.',
                  }
                : {},
            );
          }
        }
      }
      return HttpResponse.json(report);
    }),

    http.post('*/api/reports/tbm', async ({ request }) => {
      const body = (await request.json().catch(() => null)) as { work_id?: string | number } | null;
      const workId = body?.work_id !== undefined ? String(body.work_id) : '';
      const work = works.find((w) => w.work_id === workId);
      if (!work) {
        return HttpResponse.json(
          { status: 404, code: 'WORK_NOT_FOUND', detail: '작업을 찾을 수 없습니다.' },
          { status: 404 },
        );
      }
      if (work.layout_id === null || work.layout_id === undefined) {
        return HttpResponse.json(
          {
            status: 409,
            code: 'WORK_HAS_NO_LAYOUT',
            detail: '작업에 연결된 가상 레이아웃이 없습니다.',
          },
          { status: 409 },
        );
      }
      const list = reportsByWork.get(workId) ?? [];
      const latest = list.length > 0 ? list[list.length - 1] : undefined;
      if (latest?.status === 'GENERATING') {
        return HttpResponse.json(
          {
            status: 409,
            code: 'REPORT_IN_PROGRESS',
            detail: '이미 생성 중인 보고서가 있습니다.',
          },
          { status: 409 },
        );
      }
      const nextVersion = (latest?.report_version ?? 0) + 1;
      const reportId = ++nextReportSeq;
      const generating = buildReportForWork(work, reportId, nextVersion, 'GENERATING');
      list.push(generating);
      reportsByWork.set(workId, list);
      pollCounts.set(reportId, 0);
      pendingOutcomeByReport.set(reportId, { ...nextOutcome });

      return HttpResponse.json(
        {
          report_id: reportId,
          work_id: work.work_id,
          report_version: nextVersion,
          status: 'GENERATING',
        },
        { status: 202 },
      );
    }),
  ];

  return {
    handlers,
    works,
    layouts,
    workTypes,
    reportsByWork,
    setNextReportOutcome(outcome: { status: 'SUCCEEDED' | 'FAILED'; failureReason?: string }) {
      nextOutcome = outcome;
    },
  };
}
