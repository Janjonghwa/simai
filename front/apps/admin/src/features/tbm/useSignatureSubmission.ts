import { signatureKey, tbmReportDetailKey, tbmReportsByWorkKey } from '@/features/tbm/api';
import { signatureResultSchema, type SignerRole, type TbmReport } from '@/features/tbm/types';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { z } from 'zod';

export function useSignatureSubmission(report: TbmReport) {
  const client = useQueryClient();
  const submitting = useRef(false);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState('');
  const refresh = () =>
    Promise.all([
      client.invalidateQueries({ queryKey: signatureKey(report) }),
      client.invalidateQueries({ queryKey: tbmReportsByWorkKey(report.work_id) }),
      client.invalidateQueries({ queryKey: tbmReportDetailKey(report.report_id) }),
    ]);
  const submit = async (canvas: HTMLCanvasElement, name: string, role: SignerRole) => {
    if (submitting.current || report.approved_at != null) return false;
    submitting.current = true;
    setPending(true);
    setMessage('');
    try {
      const blob = await new Promise<Blob>((resolve, reject) =>
        canvas.toBlob((value) => (value ? resolve(value) : reject(new Error('PNG'))), 'image/png'),
      );
      if (blob.size > 256 * 1024) throw new Error('size');
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
          signer_role: role,
          image_key: grant.object_key,
          image_sha256: hash,
          notice_acknowledged: true,
        },
        schema: signatureResultSchema,
      });
      await refresh();
      setMessage(role === 'APPROVER' ? '관리자 결재를 완료했습니다.' : '서명을 등록했습니다.');
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.code === 'REPORT_APPROVED') {
        setMessage('이미 관리자 결재가 완료된 보고서입니다.');
        await refresh();
      } else {
        setMessage(
          error instanceof ApiError && error.code === 'REPORT_VERSION_STALE'
            ? '새 버전이 있어 다시 서명해야 합니다'
            : '서명을 등록하지 못했습니다. 다시 확인해 주세요.',
        );
      }
      return false;
    } finally {
      submitting.current = false;
      setPending(false);
    }
  };
  return { submit, pending, message };
}
