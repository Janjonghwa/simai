import { DEFAULT_SUPPLEMENT_LIMITS, phoneSupplementUrl } from '@/features/setup/supplement';
import { SupplementModal } from '@/features/setup/SupplementModal';
import {
  setupSessionResponseSchema,
  supplementMediaSchema,
  supplementRebakeSchema,
  type SetupSessionResponse,
  type SupplementState,
} from '@/features/setup/types';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(cleanup);

const SESSION_ID = 'sess-1';

const uploadedFile = supplementMediaSchema.parse({
  media_id: 'sup-1',
  kind: 'VIDEO',
  content_type: 'video/mp4',
  file_name: 'walk.mp4',
  size_bytes: 1024,
  status: 'UPLOADED',
  created_at_ms: 1,
  uploaded_at_ms: 2,
});

const runningRebake = supplementRebakeSchema.parse({
  rebake_id: 'rb-1',
  status: 'RUNNING',
  stage: 'BAKING_TEXTURE',
  progress_pct: 40,
  media_ids: ['sup-1'],
  requested_at_ms: 1,
});

function session(patch: Partial<SupplementState> = {}): SetupSessionResponse {
  return setupSessionResponseSchema.parse({
    setup_session_id: SESSION_ID,
    site_id: 'classroom-1',
    work_unit_id: 3,
    status: 'COMPLETED',
    progress_pct: 100,
    camera_heights_m: { cam1: 2.4 },
    preview_scene_version: 'scene-preview-1',
    supplement: {
      media: [],
      limits: DEFAULT_SUPPLEMENT_LIMITS,
      can_upload: true,
      can_rebake: false,
      rebake_blocked_reason: 'NO_MEDIA',
      rebake: null,
      ...patch,
    },
  });
}

function renderModal(patch: Partial<SupplementState> = {}) {
  const onNext = vi.fn();
  const onCancel = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SupplementModal session={session(patch)} onNext={onNext} onCancel={onCancel} />
    </QueryClientProvider>,
  );
  const dialog = screen.getByRole('dialog', { name: '맵 보강 촬영' });
  return { onNext, onCancel, dialog, within: within(dialog) };
}

describe('맵 보강 촬영 창', () => {
  it('제목과 맵이 비기 쉬운 곳 목록을 보인다', () => {
    const { within: inDialog } = renderModal();
    expect(inDialog.getByRole('heading', { name: '맵 보강 촬영(선택)' })).toBeTruthy();
    const spots = within(inDialog.getByRole('list', { name: '맵이 비기 쉬운 곳' }));
    expect(spots.getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      '책상·기둥·사물함 뒤',
      '카메라 바로 아래 벽 쪽',
      '문 옆·창가 구석',
      '바닥이 가려진 통로',
    ]);
  });

  it('휴대폰으로 열 주소와 QR, 파일 고르기 입력을 보인다', () => {
    const { within: inDialog } = renderModal();
    const url = phoneSupplementUrl(window.location.origin, SESSION_ID);
    expect(inDialog.getByRole('img', { name: '휴대폰으로 열 주소 QR' })).toBeTruthy();
    expect(inDialog.getByRole('link', { name: url }).getAttribute('href')).toBe(url);
    expect(inDialog.getByRole('button', { name: '주소 복사' })).toBeTruthy();
    const input = inDialog.getByLabelText('보강 촬영 파일 고르기') as HTMLInputElement;
    expect(input.type).toBe('file');
    expect(input.multiple).toBe(true);
    expect(inDialog.getByRole('button', { name: '파일 고르기' })).toBeTruthy();
  });

  it('올릴 수 없는 세션이면 파일 고르기 대신 이유를 보인다', () => {
    const { within: inDialog } = renderModal({
      can_upload: false,
      rebake_blocked_reason: 'SETUP_COMPLETED',
    });
    expect(inDialog.queryByLabelText('보강 촬영 파일 고르기')).toBeNull();
    expect(inDialog.getByText('세팅을 완료해 더 보강 촬영을 올리지 않습니다')).toBeTruthy();
  });

  it('올린 파일이 없으면 아래 오른쪽 단추는 건너뛰기이고 누르면 onNext 를 부른다', () => {
    const { onNext, onCancel, within: inDialog } = renderModal();
    expect(inDialog.getByText('아직 올린 파일이 없습니다')).toBeTruthy();
    expect(inDialog.queryByRole('button', { name: '다음' })).toBeNull();
    fireEvent.click(inDialog.getByRole('button', { name: '건너뛰기' }));
    expect(onNext).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('올린 파일이 있으면 그 파일을 목록에 보이고 단추는 다음이며 누르면 onNext 를 부른다', () => {
    const {
      onNext,
      onCancel,
      within: inDialog,
    } = renderModal({
      media: [uploadedFile],
      can_rebake: true,
      rebake_blocked_reason: null,
    });
    const files = within(inDialog.getByRole('list', { name: '보강 촬영 파일' }));
    expect(files.getByText('walk.mp4')).toBeTruthy();
    expect(inDialog.queryByRole('button', { name: '건너뛰기' })).toBeNull();
    fireEvent.click(inDialog.getByRole('button', { name: '다음' }));
    expect(onNext).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('다시 굽는 중이어도 다음으로 넘어갈 수 있다', () => {
    const { onNext, within: inDialog } = renderModal({
      media: [uploadedFile],
      rebake: runningRebake,
      rebake_blocked_reason: 'REBAKE_RUNNING',
    });
    expect(inDialog.getByRole('status', { name: '다시 굽기 상태' }).textContent).toContain(
      '다시 굽는 중 40%',
    );
    const next = inDialog.getByRole('button', { name: '다음' }) as HTMLButtonElement;
    expect(next.disabled).toBe(false);
    fireEvent.click(next);
    expect(onNext).toHaveBeenCalledTimes(1);
  });

  it('닫기 단추는 onCancel 만 부른다', () => {
    const { onNext, onCancel, within: inDialog } = renderModal();
    fireEvent.click(inDialog.getByRole('button', { name: '닫기' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onNext).not.toHaveBeenCalled();
  });

  it('Esc(cancel)와 바깥(창 자체) 클릭도 onCancel 만 부른다', () => {
    const { onNext, onCancel, dialog } = renderModal();
    // false = preventDefault 가 불렸다(브라우저는 이때 창을 닫지 않고 onCancel 이 닫는다).
    expect(fireEvent(dialog, new Event('cancel', { cancelable: true }))).toBe(false);
    expect(onCancel).toHaveBeenCalledTimes(1);
    fireEvent.click(dialog);
    expect(onCancel).toHaveBeenCalledTimes(2);
    expect(onNext).not.toHaveBeenCalled();
  });

  it('창 안쪽을 누르면 닫지 않는다', () => {
    const { onCancel, within: inDialog } = renderModal();
    fireEvent.click(inDialog.getByRole('heading', { name: '맵 보강 촬영(선택)' }));
    expect(onCancel).not.toHaveBeenCalled();
  });
});
