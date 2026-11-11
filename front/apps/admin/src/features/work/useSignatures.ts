import {
  deleteSignature,
  fetchSignatures,
  isWorkError,
  signReport,
  WORK_ERROR,
} from '@/features/work/api';
import { mutationResult } from '@/features/work/workQuery';
import { api } from '@/lib/api';
import { WORK_REPORTS_KEY, workSignaturesKey } from '@/lib/queryKeys';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

// 서명 훅.

/** 보고서의 서명 목록(참석자). reportId 가 null 이면 받지 않는다. */
export function useSignatures(reportId: number | null | undefined) {
  const enabled = reportId != null;
  const query = useQuery({
    queryKey: workSignaturesKey(reportId ?? 0),
    queryFn: ({ signal }) => fetchSignatures(api, reportId ?? 0, signal),
    enabled,
  });
  return {
    data: enabled ? query.data : undefined,
    pending: enabled && query.isPending,
    error: query.error,
    refetch: query.refetch,
  };
}

/** 서명 추가 변수: image 는 서명 PNG(256KiB 이하), reportVersion 은 서명하는 보고서 판. */
export interface AddSignatureVariables {
  reportId: number;
  reportVersion: number;
  signerName: string;
  image: Blob;
}

/**
 * 서명을 더한다(이미지 올리기 + 서명 추가). 성공하면 서명 목록과 보고서(서명 수)를 다시 받는다.
 * 실패: SignatureUploadError(이미지), 409 REPORT_VERSION_STALE·REPORT_LOCKED·SIGNATURE_LIMIT_REACHED.
 */
export function useAddSignature() {
  const client = useQueryClient();
  const mutation = useMutation({
    mutationFn: ({ reportId, reportVersion, signerName, image }: AddSignatureVariables) =>
      signReport(api, reportId, { image, signerName, reportVersion }),
    onSuccess: (_result, { reportId }) => {
      void client.invalidateQueries({ queryKey: workSignaturesKey(reportId) });
      void client.invalidateQueries({ queryKey: WORK_REPORTS_KEY });
    },
    onError: (error) => {
      if (isWorkError(error, WORK_ERROR.REPORT_VERSION_STALE)) {
        void client.invalidateQueries({ queryKey: WORK_REPORTS_KEY });
      }
    },
  });
  return mutationResult(mutation);
}

/** 서명 삭제 변수. */
export interface DeleteSignatureVariables {
  reportId: number;
  signatureId: string;
}

/** 서명을 지운다(작업 시작 전에만. 시작했거나 제출했으면 409 WORK_INVALID_STATE·REPORT_LOCKED). */
export function useDeleteSignature() {
  const client = useQueryClient();
  const mutation = useMutation({
    mutationFn: ({ reportId, signatureId }: DeleteSignatureVariables) =>
      deleteSignature(api, reportId, signatureId),
    onSuccess: (_result, { reportId }) => {
      void client.invalidateQueries({ queryKey: workSignaturesKey(reportId) });
      void client.invalidateQueries({ queryKey: WORK_REPORTS_KEY });
    },
  });
  return mutationResult(mutation);
}
