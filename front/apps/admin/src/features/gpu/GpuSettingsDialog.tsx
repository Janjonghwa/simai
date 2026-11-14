import { gpuErrorMessage } from '@/features/gpu/gpuStatus';
import type { GpuSettings, GpuSettingsBody } from '@/features/gpu/types';
import { useGpuSettings } from '@/features/gpu/useGpu';
import { ModalDialog } from '@/features/tbm/ModalDialog';
import { Button } from '@simai/ui/components/button';
import { Input } from '@simai/ui/components/input';
import { Label } from '@simai/ui/components/label';
import { Textarea } from '@simai/ui/components/textarea';
import { useState } from 'react';

// GPU 설정(S15P31A101-702, 관리자 메뉴 「GPU 설정」, 관리자만). 저장하면 서버에 새 버전으로 쌓인다(이력, 덮어쓰지 않음).
// 공급자·선호 GPU 목록(한 줄에 하나, 위가 먼저)·선호 지역 목록·클라우드 종류·온디맨드/스팟·시간당 가격 상한(비우면 없음)·
// 자동 정지 분(0 이면 끔)·RunPod 템플릿 ID. 비밀 값(API 키·부팅 비밀값)은 화면에 두지 않는다(EC2 .env·RunPod Secrets).

export interface GpuSettingsForm {
  provider: GpuSettings['provider'];
  gpuTypes: string;
  regions: string;
  cloudType: GpuSettings['cloud_type'];
  interruptible: boolean;
  maxPrice: string;
  autoStop: string;
  templateId: string;
}

export const toForm = (s: GpuSettings): GpuSettingsForm => ({
  provider: s.provider,
  gpuTypes: s.preferred_gpu_types.join('\n'),
  regions: s.preferred_regions.join('\n'),
  cloudType: s.cloud_type,
  interruptible: s.interruptible,
  maxPrice: s.max_price_per_hour_usd === null ? '' : String(s.max_price_per_hour_usd),
  autoStop: String(s.auto_stop_idle_minutes),
  templateId: s.runpod_template_id ?? '',
});

const lines = (text: string) =>
  text
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

/** 입력을 저장 본문으로. 틀린 칸이 있으면 그 까닭 */
export function toBody(form: GpuSettingsForm): GpuSettingsBody | string {
  const gpuTypes = lines(form.gpuTypes);
  const regions = lines(form.regions);
  if (gpuTypes.length === 0) return '선호 GPU 를 하나 이상 적으세요.';
  if (regions.length === 0) return '선호 지역을 하나 이상 적으세요.';
  const price = form.maxPrice.trim() === '' ? null : Number(form.maxPrice);
  if (price !== null && (!Number.isFinite(price) || price <= 0))
    return '가격 상한은 0 보다 큰 숫자이거나 비워 둡니다.';
  const autoStop = Number(form.autoStop);
  if (!Number.isInteger(autoStop) || autoStop < 0 || autoStop > 1440)
    return '자동 정지는 0~1440 분입니다.';
  return {
    provider: form.provider,
    preferred_gpu_types: gpuTypes,
    preferred_regions: regions,
    cloud_type: form.cloudType,
    interruptible: form.interruptible,
    max_price_per_hour_usd: price,
    auto_stop_idle_minutes: autoStop,
    runpod_template_id: form.templateId.trim() === '' ? null : form.templateId.trim(),
  };
}

export function GpuSettingsDialog({ onClose }: { onClose: () => void }) {
  const { data, loading, loadError, save, saving, saveError } = useGpuSettings(true);
  return (
    <ModalDialog label="GPU 설정" onClose={onClose} className="w-[min(560px,calc(100vw-32px))]">
      <div className="flex items-center justify-between border-b border-border px-5 py-3">
        <h2 className="text-base font-semibold">GPU 설정</h2>
        {data?.settings.version_id != null && (
          <span className="text-xs text-muted-foreground">버전 {data.settings.version_id}</span>
        )}
      </div>
      {loading && <p className="p-5 text-sm text-muted-foreground">불러오는 중…</p>}
      {loadError && (
        <p role="alert" className="p-5 text-sm">
          {gpuErrorMessage(loadError)}
        </p>
      )}
      {data && (
        <SettingsForm
          key={data.settings.version_id ?? 'default'}
          initial={toForm(data.settings)}
          providers={data.providers}
          saving={saving}
          error={saveError ? gpuErrorMessage(saveError) : null}
          onSave={(body) => save(body, { onSuccess: onClose })}
          onCancel={onClose}
        />
      )}
    </ModalDialog>
  );
}

function SettingsForm({
  initial,
  providers,
  saving,
  error,
  onSave,
  onCancel,
}: {
  initial: GpuSettingsForm;
  providers: { id: string; configured: boolean }[];
  saving: boolean;
  error: string | null;
  onSave: (body: GpuSettingsBody) => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState<GpuSettingsForm>(initial);
  const [invalid, setInvalid] = useState<string | null>(null);
  const set = <K extends keyof GpuSettingsForm>(key: K, value: GpuSettingsForm[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const submit = () => {
    const body = toBody(form);
    if (typeof body === 'string') {
      setInvalid(body);
      return;
    }
    setInvalid(null);
    onSave(body);
  };

  return (
    <form
      className="flex flex-col gap-4 p-5"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="gpu-provider">공급자</Label>
        <select
          id="gpu-provider"
          value={form.provider}
          onChange={(e) => set('provider', e.target.value === 'AWS' ? 'AWS' : 'RUNPOD')}
          className="h-8 rounded-lg border border-input bg-transparent px-2 text-sm"
        >
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.id === 'RUNPOD' ? 'RunPod' : 'AWS'}
              {p.configured ? '' : ' (설정되지 않음)'}
            </option>
          ))}
        </select>
      </div>
      <div className="grid gap-4 min-[480px]:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="gpu-types">선호 GPU (위가 먼저)</Label>
          <Textarea
            id="gpu-types"
            rows={4}
            value={form.gpuTypes}
            onChange={(e) => set('gpuTypes', e.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="gpu-regions">선호 지역 (위가 먼저)</Label>
          <Textarea
            id="gpu-regions"
            rows={4}
            value={form.regions}
            onChange={(e) => set('regions', e.target.value)}
          />
        </div>
      </div>
      <fieldset className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
        <legend className="sr-only">클라우드 종류</legend>
        <label className="flex items-center gap-1.5">
          <input
            type="radio"
            name="gpu-cloud"
            checked={form.cloudType === 'SECURE'}
            onChange={() => set('cloudType', 'SECURE')}
          />
          보안 클라우드
        </label>
        <label className="flex items-center gap-1.5">
          <input
            type="radio"
            name="gpu-cloud"
            checked={form.cloudType === 'COMMUNITY'}
            onChange={() => set('cloudType', 'COMMUNITY')}
          />
          커뮤니티 클라우드
        </label>
      </fieldset>
      <fieldset className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
        <legend className="sr-only">요금 방식</legend>
        <label className="flex items-center gap-1.5">
          <input
            type="radio"
            name="gpu-billing"
            checked={!form.interruptible}
            onChange={() => set('interruptible', false)}
          />
          온디맨드
        </label>
        <label className="flex items-center gap-1.5">
          <input
            type="radio"
            name="gpu-billing"
            checked={form.interruptible}
            onChange={() => set('interruptible', true)}
          />
          스팟(중단될 수 있음)
        </label>
      </fieldset>
      <div className="grid gap-4 min-[480px]:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="gpu-max-price">시간당 가격 상한 (USD)</Label>
          <Input
            id="gpu-max-price"
            inputMode="decimal"
            placeholder="없음"
            value={form.maxPrice}
            onChange={(e) => set('maxPrice', e.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="gpu-auto-stop">자동 정지 (유휴 분, 0 = 끔)</Label>
          <Input
            id="gpu-auto-stop"
            inputMode="numeric"
            value={form.autoStop}
            onChange={(e) => set('autoStop', e.target.value)}
          />
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="gpu-template">RunPod 템플릿 ID</Label>
        <Input
          id="gpu-template"
          value={form.templateId}
          onChange={(e) => set('templateId', e.target.value)}
        />
      </div>
      {(invalid ?? error) && (
        <p role="alert" className="text-sm">
          {invalid ?? error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="secondary" onClick={onCancel} disabled={saving}>
          취소
        </Button>
        <Button type="submit" disabled={saving}>
          {saving ? '저장 중…' : '저장'}
        </Button>
      </div>
    </form>
  );
}
