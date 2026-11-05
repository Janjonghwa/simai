import { requestReportSignatures, signatureKey } from '@/features/tbm/api';
import type { SignerRole, TbmReport } from '@/features/tbm/types';
import { useSignatureSubmission } from '@/features/tbm/useSignatureSubmission';
import { api } from '@/lib/api';
import { Button } from '@simai/ui/components/button';
import { useQuery } from '@tanstack/react-query';
import { useRef, useState, type PointerEvent } from 'react';

export function requestSignatures(report: TbmReport, signal?: AbortSignal) {
  return requestReportSignatures(api, report, signal);
}
export function useSignatures(report: TbmReport | null) {
  const query = useQuery({
    queryKey: report ? signatureKey(report) : ['tbm', 'signatures'],
    enabled: !!report,
    retry: false,
    queryFn: ({ signal }) => requestSignatures(report!, signal),
  });
  return { items: query.data?.items ?? [], error: query.isError, loading: query.isPending };
}

export function SignaturePanel({
  report,
  signatures,
}: {
  report: TbmReport;
  signatures: ReturnType<typeof useSignatures>;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const drawing = useRef(false);
  const [hasInk, setHasInk] = useState(false);
  const [name, setName] = useState('');
  const [ack, setAck] = useState(false);
  const { submit, pending, message } = useSignatureSubmission(report);
  const participants = signatures.items.filter((item) => item.signer_role === 'PARTICIPANT');
  const approver = signatures.items.find((item) => item.signer_role === 'APPROVER');
  const approved = report.approved_at != null || !!approver;
  const disabled = pending || !hasInk || !ack || !name.trim() || report.status !== 'SUCCEEDED';
  const missing = [!name.trim() && '실명', !hasInk && '서명', !ack && '고지 확인'].filter(Boolean);
  const point = (event: PointerEvent<HTMLCanvasElement>) => {
    const el = event.currentTarget;
    const rect = el.getBoundingClientRect();
    return [
      ((event.clientX - rect.left) * el.width) / rect.width,
      ((event.clientY - rect.top) * el.height) / rect.height,
    ] as const;
  };
  const start = (event: PointerEvent<HTMLCanvasElement>) => {
    if (pending) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    drawing.current = true;
    const ctx = event.currentTarget.getContext('2d');
    if (!ctx) return;
    const [x, y] = point(event);
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x + 0.1, y + 0.1);
    ctx.lineWidth = 2;
    ctx.lineCap = 'round';
    ctx.stroke();
    setHasInk(true);
  };
  const move = (event: PointerEvent<HTMLCanvasElement>) => {
    if (pending || !drawing.current) return;
    const ctx = event.currentTarget.getContext('2d');
    if (!ctx) return;
    const [x, y] = point(event);
    ctx.lineTo(x, y);
    ctx.stroke();
  };
  const clear = () => {
    canvas.current?.getContext('2d')?.clearRect(0, 0, 400, 120);
    setHasInk(false);
  };
  const register = async (role: SignerRole) => {
    if (!canvas.current || disabled || approved) return;
    if (
      role === 'APPROVER' &&
      !window.confirm(
        `${participants.length === 0 ? '참석자 서명 0명입니다. ' : ''}현재 로그인한 관리자 계정으로 결재합니다. 결재 후 이 버전에는 서명을 추가할 수 없습니다. 결재하시겠습니까?`,
      )
    )
      return;
    if (await submit(canvas.current, name, role)) {
      setName('');
      setAck(false);
      clear();
    }
  };
  return (
    <section
      className="space-y-2 rounded-lg border border-border p-4 text-sm"
      aria-label="참석자 서명"
    >
      <h2>참석자 서명 · v{report.report_version}</h2>
      <p>공인 전자서명 아님, 내부 기록용 전자 서명</p>
      <p>
        잠금 시각: {report.locked_at != null ? new Date(report.locked_at).toLocaleString() : '—'} ·
        내용 해시: {report.content_sha256?.slice(0, 12) ?? '—'}
      </p>
      {signatures.error && <p role="alert">서명자 목록을 불러오지 못했습니다.</p>}
      <ul>
        {participants.map((item) => (
          <li key={item.signature_id}>
            {item.signer_name} · {new Date(item.signed_at).toLocaleString()}{' '}
            <img src={item.image_url} alt={`${item.signer_name} 서명`} className="h-12" />
          </li>
        ))}
      </ul>
      <h2>관리자 결재</h2>
      {approved ? (
        <div>
          <span className="rounded bg-ok/15 px-2 py-1 text-ok">결재 완료</span>{' '}
          {report.approved_by_name ?? '관리자'} ·{' '}
          {new Date(report.approved_at ?? approver!.signed_at).toLocaleString()}
          {approver && (
            <img
              src={approver.image_url}
              alt={`${approver.signer_name} 결재 서명`}
              className="h-12"
            />
          )}
        </div>
      ) : (
        <>
          {!signatures.loading && !signatures.error && participants.length === 0 && (
            <p role="alert">참석자 서명 0명</p>
          )}
          <fieldset disabled={pending} className="space-y-2">
            <label>
              서명자 실명{' '}
              <input
                className="bg-background text-foreground"
                value={name}
                maxLength={40}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <canvas
              ref={canvas}
              width={400}
              height={120}
              aria-label="서명 패드"
              className="block max-w-full touch-none bg-white"
              onPointerDown={start}
              onPointerMove={move}
              onPointerUp={() => {
                drawing.current = false;
              }}
              onPointerCancel={() => {
                drawing.current = false;
              }}
            />
            <Button type="button" variant="outline" onClick={clear}>
              서명 지우기
            </Button>
            <label className="block">
              <input
                type="checkbox"
                checked={ack}
                onChange={(event) => setAck(event.target.checked)}
              />{' '}
              고지를 확인했습니다
            </label>
            {report.status !== 'SUCCEEDED' && <p>보고서 생성 완료 후 서명할 수 있습니다.</p>}
            {missing.length > 0 && <p>서명 등록을 위해 필요: {missing.join(' · ')}</p>}
            <Button type="button" disabled={disabled} onClick={() => void register('PARTICIPANT')}>
              서명 등록
            </Button>{' '}
            <Button
              type="button"
              disabled={disabled || signatures.loading || signatures.error}
              onClick={() => void register('APPROVER')}
            >
              관리자 결재
            </Button>
          </fieldset>
        </>
      )}
      <p role="status">{message}</p>
    </section>
  );
}
