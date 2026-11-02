import { H3, NOTE } from '@/features/tbm/styles';
import type {
  SubgraphNode,
  TbmReportCitation,
  TbmReportStep,
  TbmSubgraph,
} from '@/features/tbm/types';
import { cn } from '@simai/ui/lib/utils';
import { useState } from 'react';

// 법령 지식 그래프(subgraph: nodes·edges) SVG 시각화 + 생성·검증 단계(steps) 표시.
// 무거운 외부 그래프 라이브러리 없이 hop(0차 후보 vs 1-hop 확장) 2열 배치 SVG 로 그린다.
// 왼쪽 패널 영역(TbmLeftPanel)의 단추 개수 규칙을 깨지 않도록 SVG 노드는 <g> 클릭으로 처리한다.

const SVG_W = 560;
const NODE_W = 196;
const NODE_H = 44;

interface PositionedNode {
  node: SubgraphNode;
  cx: number;
  cy: number;
}

function layoutSubgraphNodes(nodes: readonly SubgraphNode[]): {
  placed: PositionedNode[];
  height: number;
} {
  const hop0 = nodes.filter((n) => n.hop === 0);
  const hop1 = nodes.filter((n) => n.hop !== 0);
  const maxRows = Math.max(hop0.length, hop1.length, 1);
  const rowGap = 58;
  const topPad = 38;
  const height = Math.max(180, topPad * 2 + (maxRows - 1) * rowGap + 24);

  const placed: PositionedNode[] = [];
  if (hop0.length > 0 && hop1.length > 0) {
    hop0.forEach((node, idx) => {
      placed.push({
        node,
        cx: 130,
        cy: topPad + idx * rowGap,
      });
    });
    hop1.forEach((node, idx) => {
      placed.push({
        node,
        cx: 430,
        cy: topPad + idx * rowGap,
      });
    });
  } else {
    nodes.forEach((node, idx) => {
      const col = idx % 2;
      const row = Math.floor(idx / 2);
      placed.push({
        node,
        cx: col === 0 ? 130 : 430,
        cy: topPad + row * rowGap,
      });
    });
  }
  return { placed, height };
}

export function TbmLegalGraph({
  subgraph,
  citations,
  steps,
}: {
  subgraph: TbmSubgraph;
  citations: readonly TbmReportCitation[];
  steps: readonly TbmReportStep[];
}) {
  const [clickedNodeId, setClickedNodeId] = useState<string | null>(null);

  const defaultNode = subgraph.nodes.find((n) => n.cited) ?? subgraph.nodes[0] ?? null;
  const selectedNode =
    (clickedNodeId ? subgraph.nodes.find((n) => n.node_id === clickedNodeId) : undefined) ??
    defaultNode;
  const selectedCitation = selectedNode
    ? citations.find((c) => c.node_id === selectedNode.node_id)
    : undefined;

  const { placed, height } = layoutSubgraphNodes(subgraph.nodes);
  const byId = new Map(placed.map((p) => [p.node.node_id, p]));

  return (
    <div className="flex flex-col gap-4">
      <section aria-label="법령 지식 그래프" className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className={H3}>법령 지식 그래프</h3>
          <div className="flex flex-wrap items-center gap-3 text-[11.5px] text-muted-foreground">
            <span className="inline-flex items-center gap-1">
              <span className="inline-block size-2.5 rounded-full bg-amber-400" />
              인용 조문 (cited)
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="inline-block size-2.5 rounded-sm border border-sky-400 bg-sky-500/20" />
              0차 후보 (hop 0)
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="inline-block size-2.5 rounded-sm border border-dashed border-muted-foreground" />
              1-hop 확장 (hop 1)
            </span>
          </div>
        </div>

        {subgraph.nodes.length === 0 ? (
          <p className={NOTE}>표시할 법령 서브그래프가 없습니다.</p>
        ) : (
          <>
            <div className="overflow-x-auto rounded-lg bg-surface-2 p-2 ring-1 ring-border ring-inset">
              <svg
                role="img"
                aria-label="법령 지식 그래프 노드·엣지 도식"
                viewBox={`0 0 ${SVG_W} ${height}`}
                className="h-auto w-full min-w-[440px] select-none"
              >
                <defs>
                  <marker
                    id="tbm-edge-arrow"
                    viewBox="0 0 10 10"
                    refX="8"
                    refY="5"
                    markerWidth="6"
                    markerHeight="6"
                    orient="auto-start-reverse"
                  >
                    <path d="M 0 1 L 8 5 L 0 9 z" className="fill-muted-foreground" />
                  </marker>
                </defs>

                {subgraph.edges.map((edge) => {
                  const src = byId.get(edge.src_node_id);
                  const dst = byId.get(edge.dst_node_id);
                  if (!src || !dst) return null;
                  const midX = (src.cx + dst.cx) / 2;
                  const midY = (src.cy + dst.cy) / 2 - 4;
                  return (
                    <g key={edge.edge_id} data-testid={`graph-edge-${edge.edge_id}`}>
                      <line
                        x1={src.cx}
                        y1={src.cy}
                        x2={dst.cx}
                        y2={dst.cy}
                        className="stroke-muted-foreground/60"
                        strokeWidth={1.5}
                        strokeDasharray={edge.edge_kind === 'DELEGATES' ? '4 3' : undefined}
                        markerEnd="url(#tbm-edge-arrow)"
                      />
                      <text
                        x={midX}
                        y={midY}
                        textAnchor="middle"
                        className="fill-muted-foreground text-[9.5px]"
                      >
                        {edge.relation_label ?? edge.edge_kind}
                      </text>
                    </g>
                  );
                })}

                {placed.map(({ node, cx, cy }) => {
                  const isSelected = selectedNode?.node_id === node.node_id;
                  const x = cx - NODE_W / 2;
                  const y = cy - NODE_H / 2;
                  const hopLabel = node.hop === 0 ? '0차 후보' : `${node.hop}-hop 확장`;
                  return (
                    <g
                      key={node.node_id}
                      data-testid={`graph-node-${node.node_id}`}
                      data-cited={node.cited ? 'true' : 'false'}
                      data-hop={String(node.hop)}
                      aria-label={`${node.source_name} ${node.article_ref ?? ''} ${node.title}`.trim()}
                      onClick={() => setClickedNodeId(node.node_id)}
                      className="cursor-pointer"
                    >
                      <rect
                        x={x}
                        y={y}
                        width={NODE_W}
                        height={NODE_H}
                        rx={8}
                        className={cn(
                          'transition-colors',
                          node.cited
                            ? 'fill-amber-500/15 stroke-amber-400'
                            : node.hop === 0
                              ? 'fill-sky-500/10 stroke-sky-400/70'
                              : 'fill-background stroke-muted-foreground/60',
                          isSelected && 'stroke-[2.5px]',
                        )}
                        strokeWidth={node.cited || isSelected ? 2.2 : 1.2}
                        strokeDasharray={node.hop > 0 && !node.cited ? '4 2' : undefined}
                      />
                      <text x={x + 10} y={y + 17} className="fill-foreground text-[11px] font-bold">
                        {node.article_ref ? `${node.article_ref} · ` : ''}
                        {node.title.length > 12 ? `${node.title.slice(0, 12)}…` : node.title}
                      </text>
                      <text x={x + 10} y={y + 33} className="fill-muted-foreground text-[10px]">
                        {hopLabel}
                        {node.cited ? ' · ★인용됨' : ''}
                        {node.rule_tags.length > 0 ? ` · [${node.rule_tags.join(',')}]` : ''}
                      </text>
                    </g>
                  );
                })}
              </svg>
            </div>

            {selectedNode && (
              <div
                data-testid="graph-node-detail"
                className="flex flex-col gap-1.5 rounded-lg bg-surface-2 px-3.5 py-2.5 text-[12.5px] ring-1 ring-border ring-inset"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-bold text-foreground">
                    {selectedNode.source_name}{' '}
                    {selectedNode.article_ref ? `${selectedNode.article_ref} ` : ''}(
                    {selectedNode.title})
                  </span>
                  <span
                    className={cn(
                      'rounded px-1.5 py-0.5 text-[11px] font-semibold',
                      selectedNode.cited
                        ? 'bg-amber-500/20 text-amber-300'
                        : 'bg-muted text-muted-foreground',
                    )}
                  >
                    {selectedNode.cited ? '인용됨 (cited)' : '미인용 후보'}
                  </span>
                  <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
                    {selectedNode.hop === 0 ? '0차 후보 (hop 0)' : `${selectedNode.hop}-hop 확장`}
                  </span>
                  {selectedNode.rule_tags.length > 0 && (
                    <span className="font-mono text-[11px] text-muted-foreground">
                      규칙: {selectedNode.rule_tags.join(', ')}
                    </span>
                  )}
                </div>
                <p className="leading-relaxed text-muted-foreground">
                  {selectedCitation?.excerpt ||
                    '보고서 본문에 직접 인용되지 않은 그래프 확장 참조 조문입니다.'}
                </p>
              </div>
            )}
          </>
        )}
      </section>

      <section aria-label="생성 · 검증 단계" className="flex flex-col gap-2">
        <h3 className={H3}>생성 · 검증 단계</h3>
        {steps.length === 0 ? (
          <p className={NOTE}>생성 단계 기록이 없습니다.</p>
        ) : (
          <div className="flex flex-col gap-1.5">
            {steps.map((s) => (
              <div
                key={s.step}
                data-testid={`report-step-${s.step}`}
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-surface-2 px-3 py-2 text-[12.5px] ring-1 ring-border ring-inset"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className={cn(
                      'rounded px-1.5 py-0.5 font-mono text-[11px] font-semibold',
                      s.status === 'DONE'
                        ? 'bg-emerald-500/20 text-emerald-300'
                        : s.status === 'FAILED'
                          ? 'bg-destructive/20 text-destructive'
                          : 'bg-amber-500/20 text-amber-300',
                    )}
                  >
                    {s.status}
                  </span>
                  <span className="font-semibold text-foreground">{s.label}</span>
                  <span className="font-mono text-[11px] text-muted-foreground">({s.step})</span>
                </div>
                <div className="flex flex-wrap items-center gap-3 text-[12px] text-muted-foreground tabular-nums">
                  <span>
                    입출력 {s.input_count} → {s.output_count}건
                  </span>
                  <span>{s.duration_ms}ms</span>
                </div>
                {s.detail && (
                  <p className="basis-full text-[12px] text-muted-foreground">{s.detail}</p>
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
