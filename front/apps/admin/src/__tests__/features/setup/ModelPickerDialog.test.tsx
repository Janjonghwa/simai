import { ModelPickerDialog } from '@/features/setup/ModelPickerDialog';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(cleanup);

function renderDialog(reuseBlockedReason: string | null = null) {
  const onNew = vi.fn();
  const onReuse = vi.fn();
  const onClose = vi.fn();
  render(
    <ModelPickerDialog
      onNew={onNew}
      onReuse={onReuse}
      onClose={onClose}
      reuseBlockedReason={reuseBlockedReason}
    />,
  );
  const dialog = screen.getByRole('dialog', { name: '3D 공간' });
  return { onNew, onReuse, onClose, dialog, within: within(dialog) };
}

describe('방식 고르기 창', () => {
  it('새로 만들기와 이전 모델 쓰기 두 선택지를 보이고 새로 만들기에 예상 시간을 붙이며, 막는 이유가 없으면 이전 모델 쓰기를 눌러 onReuse 를 부른다', () => {
    const { onNew, onReuse, within: inDialog } = renderDialog();
    expect(inDialog.getByRole('heading', { name: '3D 공간' })).toBeTruthy();
    expect(inDialog.getByRole('button', { name: /^새로 3D 공간 만들기/ }).textContent).toContain(
      '예상 약 1분 30초',
    );
    const reuse = inDialog.getByRole('button', { name: /^이전 모델 쓰기/ }) as HTMLButtonElement;
    expect(reuse.disabled).toBe(false);
    expect(reuse.title).toBe('');
    expect(reuse.textContent).toBe('이전 모델 쓰기');
    fireEvent.click(reuse);
    expect(onReuse).toHaveBeenCalledTimes(1);
    expect(onNew).not.toHaveBeenCalled();
  });

  it('이전 모델 쓰기를 막는 이유가 있으면 꺼지고 이유를 글과 title 에 둔다', () => {
    const { onNew, onReuse, within: inDialog } = renderDialog('이전 모델이 없어요');
    const reuse = inDialog.getByRole('button', { name: /^이전 모델 쓰기/ }) as HTMLButtonElement;
    expect(reuse.disabled).toBe(true);
    expect(reuse.title).toBe('이전 모델이 없어요');
    expect(reuse.textContent).toContain('이전 모델이 없어요');
    fireEvent.click(reuse);
    expect(onReuse).not.toHaveBeenCalled();
    expect(onNew).not.toHaveBeenCalled();
  });

  it('처음 포커스는 새로 3D 공간 만들기에 있다', () => {
    const { within: inDialog } = renderDialog();
    expect(document.activeElement).toBe(
      inDialog.getByRole('button', { name: /^새로 3D 공간 만들기/ }),
    );
  });

  it('새로 3D 공간 만들기를 누르면 onNew 를 한 번 부른다', () => {
    const { onNew, within: inDialog } = renderDialog();
    fireEvent.click(inDialog.getByRole('button', { name: /^새로 3D 공간 만들기/ }));
    expect(onNew).toHaveBeenCalledTimes(1);
  });

  it('바깥(어두운 바탕 = 창 자체)을 누르거나 Esc 를 누르면 고르지 않고 onClose 를 부른다', () => {
    const { onNew, onReuse, onClose, dialog } = renderDialog();
    fireEvent.click(dialog);
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent(dialog, new Event('cancel', { cancelable: true }));
    expect(onClose).toHaveBeenCalledTimes(2);
    expect(onNew).not.toHaveBeenCalled();
    expect(onReuse).not.toHaveBeenCalled();
  });

  it('창 안의 빈 곳을 누르면 닫히지 않는다', () => {
    const { onClose, within: inDialog } = renderDialog();
    fireEvent.click(inDialog.getByRole('heading', { name: '3D 공간' }));
    expect(onClose).not.toHaveBeenCalled();
  });
});
