import { tbmReportDetailKey, tbmReportsByWorkKey } from '@/features/tbm/api';
import type { TbmReport } from '@/features/tbm/types';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState, type PointerEvent } from 'react';
import { z } from 'zod';

const signatureSchema = z.object({
  signature_id: z.union([z.string(), z.number()]).transform(String),
  signer_name: z.string(),
  signed_at: z.union([z.number(), z.string()]),
  report_version: z.number(),
  image_url: z.string(),
});
export type Signature = z.infer<typeof signatureSchema>;
const signatureKey = (report: TbmReport) =>
  ['tbm', 'signatures', report.report_id, report.report_version] as const;
export function requestSignatures(report: TbmReport, signal?: AbortSignal) {
  return api(`/api/reports/tbm/${report.report_id}/signatures?version=${report.report_version}`, {
    schema: z.object({ items: z.array(signatureSchema) }),
    signal,
  });
}
export function useSignatures(report: TbmReport | null) {
  const query = useQuery({
    queryKey: report ? signatureKey(report) : ['tbm', 'signatures'],
    enabled: !!report,
    retry: false,
    queryFn: ({ signal }) => requestSignatures(report!, signal),
  });
  return { items: query.data?.items ?? [], error: query.isError };
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
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState('');
  const client = useQueryClient();
  const point = (event: PointerEvent<HTMLCanvasElement>) => {
    const el = event.currentTarget;
    const rect = el.getBoundingClientRect();
    return [
      ((event.clientX - rect.left) * el.width) / rect.width,
      ((event.clientY - rect.top) * el.height) / rect.height,
    ] as const;
  };
  const start = (event: PointerEvent<HTMLCanvasElement>) => {
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
    if (!drawing.current) return;
    const ctx = event.currentTarget.getContext('2d');
    if (!ctx) return;
    const [x, y] = point(event);
    ctx.lineTo(x, y);
    ctx.stroke();
  };
  const submit = async () => {
    if (!canvas.current || !hasInk || !ack || !name.trim() || name.trim().length > 40 || pending)
      return;
    setPending(true);
    setMessage('');
    try {
      const blob = await new Promise<Blob>((resolve, reject) =>
        canvas.current!.toBlob(
          (value) => (value ? resolve(value) : reject(new Error('PNG'))),
          'image/png',
        ),
      );
      if (blob.size > 2 * 1024 * 1024) throw new Error('size');
      const hash = Array.from(
        new Uint8Array(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer())),
        (byte) => byte.toString(16).padStart(2, '0'),
      ).join('');
      const grant = await api('/api/uploads/sign', {
        method: 'POST',
        body: {
          purpose: 'REPORT_SIGNATURE',
          content_type: 'image/png',
          size_bytes: blob.size,
          sha256: hash,
        },
        schema: z.object({
          object_key: z.string(),
          upload_url: z.string().url(),
          headers: z.record(z.string(), z.string()),
        }),
      });
      const uploaded = await fetch(grant.upload_url, {
        method: 'PUT',
        headers: grant.headers,
        body: blob,
      });
      if (!uploaded.ok) throw new Error('upload');
      await api(`/api/reports/tbm/${report.report_id}/signatures`, {
        method: 'POST',
        body: {
          report_version: report.report_version,
          signer_name: name.trim(),
          image_key: grant.object_key,
          image_sha256: hash,
          notice_acknowledged: true,
        },
        schema: z.object({
          signature_id: z.union([z.string(), z.number()]),
          report_id: z.number(),
          report_version: z.number(),
          signer_name: z.string(),
          signed_at: z.union([z.number(), z.string()]),
          locked_at: z.union([z.number(), z.string()]),
          content_sha256: z.string(),
        }),
      });
      await Promise.all([
        client.invalidateQueries({ queryKey: signatureKey(report) }),
        client.invalidateQueries({ queryKey: tbmReportsByWorkKey(report.work_id) }),
        client.invalidateQueries({ queryKey: tbmReportDetailKey(report.report_id) }),
      ]);
      setName('');
      setAck(false);
      canvas.current
        ?.getContext('2d')
        ?.clearRect(0, 0, canvas.current.width, canvas.current.height);
      setHasInk(false);
      setMessage('서명을 등록했습니다.');
    } catch (error) {
      setMessage(
        error instanceof ApiError && error.code === 'REPORT_VERSION_STALE'
          ? '새 버전이 있어 다시 서명해야 합니다'
          : '서명을 등록하지 못했습니다. 다시 확인해 주세요.',
      );
    } finally {
      setPending(false);
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
        {signatures.items.map((item) => (
          <li key={item.signature_id}>
            {item.signer_name} · {new Date(item.signed_at).toLocaleString()}{' '}
            <img src={item.image_url} alt={`${item.signer_name} 서명`} className="h-12" />
          </li>
        ))}
      </ul>
      <label>
        서명자 실명{' '}
        <input
          className="text-black"
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
      <button
        type="button"
        onClick={() => {
          canvas.current?.getContext('2d')?.clearRect(0, 0, 400, 120);
          setHasInk(false);
        }}
      >
        서명 지우기
      </button>
      <label className="block">
        <input type="checkbox" checked={ack} onChange={(event) => setAck(event.target.checked)} />{' '}
        고지를 확인했습니다
      </label>
      <button
        type="button"
        disabled={pending || !hasInk || !ack || !name.trim() || report.status !== 'SUCCEEDED'}
        onClick={() => void submit()}
      >
        서명 등록
      </button>
      <p role="status">{message}</p>
    </section>
  );
}
