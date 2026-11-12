import {
  putSiteRuleProfile,
  RULE_PROFILES,
  type RuleProfile,
  type RuleProfileChange,
} from '@/features/layout/api';
import { useCurrentSite } from '@/features/layout/useCurrentSite';
import { BUNDLES_KEY } from '@/features/zones/api';
import { api } from '@/lib/api';
import {
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
} from '@simai/ui/components/dropdown-menu';
import { useMutation, useQueryClient } from '@tanstack/react-query';

/** 메뉴 항목 글. 고소 기준 높이는 명세 7.2(R2·R3 운영 2.0 m, 교실 시험 0.35 m). */
export const RULE_PROFILE_OPTION: Record<RuleProfile, string> = {
  OPERATION: '운영 (고소 2.0m)',
  CLASSROOM_TEST: '교실 시험 (고소 0.35m)',
};

const isRuleProfile = (value: unknown): value is RuleProfile =>
  RULE_PROFILES.includes(value as RuleProfile);

/** 바꾼 뒤 결과 글. 묶음을 만들지 못해도 설정은 저장됐고 다음 묶음(세팅 완료·작업 시작·구역 저장)부터 따른다. */
export function ruleProfileResultText(result: RuleProfileChange): string {
  if (result.bundle) return `새 구성 묶음 ${result.bundle.config_version} 발행`;
  return `기준은 저장했어요. 묶음은 만들지 못했어요(${result.publish_error ?? '알 수 없음'}) · 다음 묶음부터 적용`;
}

/**
 * 관리자 메뉴의 현장 규칙 기준(S15P31A101-654): 운영 / 교실 시험. 고르면 서버가 현장 설정을 바꾸고 그 기준으로 새 구성 묶음을 발행한다.
 * 이후 모든 묶음이 이 설정을 따른다. 고른 뒤에도 메뉴는 열려 있어 결과를 보인다(라디오 항목은 눌러도 닫히지 않는다).
 */
export function RuleProfileMenu() {
  const site = useCurrentSite();
  const client = useQueryClient();
  const mutation = useMutation({
    mutationFn: (profile: RuleProfile) => putSiteRuleProfile(api, profile),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['sites', 'current'] });
      void client.invalidateQueries({ queryKey: BUNDLES_KEY });
    },
  });
  const current = mutation.isPending ? mutation.variables : site?.rule_profile;
  return (
    <DropdownMenuGroup>
      <DropdownMenuLabel>규칙 기준</DropdownMenuLabel>
      <DropdownMenuRadioGroup
        value={current ?? null}
        onValueChange={(value: unknown) => {
          if (isRuleProfile(value) && value !== site?.rule_profile && !mutation.isPending) {
            mutation.mutate(value);
          }
        }}
      >
        {RULE_PROFILES.map((profile) => (
          <DropdownMenuRadioItem
            key={profile}
            value={profile}
            disabled={mutation.isPending || site === undefined}
          >
            {RULE_PROFILE_OPTION[profile]}
          </DropdownMenuRadioItem>
        ))}
      </DropdownMenuRadioGroup>
      {mutation.isError && (
        <p role="alert" className="px-1.5 py-1 text-xs text-destructive">
          규칙 기준을 바꾸지 못했어요
        </p>
      )}
      {mutation.data && (
        <p role="status" className="px-1.5 py-1 text-xs text-muted-foreground">
          {ruleProfileResultText(mutation.data)}
        </p>
      )}
    </DropdownMenuGroup>
  );
}
