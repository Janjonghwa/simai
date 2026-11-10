import { LoginHero } from '@/features/auth/LoginHero';
import { loginFormSchema } from '@/features/auth/types';
import { useLogin } from '@/features/auth/useLogin';
import { useMediaQuery } from '@/features/auth/useMediaQuery';
import { BrandMark, Wordmark } from '@/features/layout/BrandMark';
import { zodResolver } from '@hookform/resolvers/zod';
import { Button } from '@simai/ui/components/button';
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from '@simai/ui/components/input-group';
import { Label } from '@simai/ui/components/label';
import { Eye, EyeOff, LockKeyhole, UserRound } from 'lucide-react';
import { useState } from 'react';
import { useForm } from 'react-hook-form';

// 입력 칸은 높고 둥글게 하고 앞에 아이콘을 둔다. 아이콘을 눌러도 입력 칸으로 간다(InputGroupAddon).
const FIELD = 'h-12 rounded-xl bg-card/60 dark:bg-input/20';
const FIELD_INPUT = 'h-full text-[15px] md:text-[15px]';
const FIELD_ICON = 'pl-3.5 [&>svg:not([class*=size-])]:size-[18px]';
// 오류 문구는 칸 아래 빈자리에 띄운다(absolute). 문구가 생겨도 폼 높이가 그대로라 세로 가운데 정렬된 로고·제목이 밀리지 않는다(-591).
// 칸 사이 간격(gap-8)이 문구 한 줄을, 비밀번호 칸과 버튼 사이(gap-8 + mt-3 = 44px)는 서버 문구 두 줄(4 + 40px)까지 담는다.
const FIELD_ERROR = 'absolute top-full left-0 mt-1 text-sm leading-5 text-destructive';
// 로그인 버튼은 로고 오른쪽 파랑(#094EFC→#3539FA)으로 칠하고 흰 글자를 쓴다. 흰 글자 대비 6.0:1 이상.
// 다크 토큰 primary(#6886D5)에 검은 글자는 눌리지 않는 버튼처럼 흐려 보였다.
const SUBMIT =
  'mt-3 h-12 cursor-pointer rounded-full bg-linear-to-r from-[#094EFC] to-[#3539FA] text-[15px] font-semibold text-white shadow-lg shadow-[#094EFC]/30 transition-[filter,box-shadow] hover:brightness-110 hover:shadow-[#094EFC]/45 disabled:cursor-default';

export function LoginPage() {
  const { login, pending, error } = useLogin();
  const [showPassword, setShowPassword] = useState(false);
  // 오른쪽 카드는 넓은 화면(lg)에서만 그린다. 좁은 화면에서 숨긴 채 영상을 받지 않게 한다.
  const wide = useMediaQuery('(min-width: 64rem)');
  const {
    register,
    handleSubmit,
    resetField,
    setFocus,
    formState: { errors },
  } = useForm({
    resolver: zodResolver(loginFormSchema),
    defaultValues: { username: '', password: '' },
  });

  return (
    <main className="grid min-h-svh bg-background px-6 py-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,0.82fr)] lg:gap-4 lg:p-5">
      <section className="mx-auto flex w-full max-w-[420px] flex-col py-6 lg:py-8">
        <div className="flex flex-1 items-center py-12">
          <div className="flex w-full flex-col gap-10">
            {/* 로고는 폼 묶음 맨 위에 하나만 둔다(오른쪽 카드에는 넣지 않음). */}
            <div className="flex items-center gap-3">
              <BrandMark size={36} />
              <Wordmark size={36} />
            </div>
            <div className="flex flex-col gap-3">
              <h1 className="text-[32px] leading-tight font-bold tracking-[-0.8px]">
                현장 안전 관제
              </h1>
              <p className="text-[15px] break-keep text-muted-foreground">
                관리자 계정으로 로그인하세요.
              </p>
            </div>
            <form
              noValidate
              className="flex flex-col gap-8"
              onSubmit={(event) => {
                // 보낼 때는 다시 가린다. 비밀번호 관리자가 칸을 알아보고, 화면에 비밀번호가 남지 않게 한다.
                setShowPassword(false);
                void handleSubmit((values) => {
                  // 아이디는 남기고 비밀번호는 보내자마자 지운다. 실패하면 비밀번호 칸으로 돌아가 다시 받는다.
                  login(values, { onError: () => setFocus('password') });
                  resetField('password');
                })(event);
              }}
            >
              <div className="relative flex flex-col gap-2">
                <Label htmlFor="username" className="font-semibold">
                  아이디
                </Label>
                <InputGroup className={FIELD}>
                  <InputGroupAddon className={FIELD_ICON}>
                    <UserRound aria-hidden="true" />
                  </InputGroupAddon>
                  <InputGroupInput
                    id="username"
                    placeholder="아이디를 입력하세요"
                    autoComplete="username"
                    autoCapitalize="none"
                    spellCheck={false}
                    autoFocus
                    className={FIELD_INPUT}
                    aria-invalid={errors.username ? true : undefined}
                    aria-describedby={errors.username ? 'username-error' : undefined}
                    {...register('username')}
                  />
                </InputGroup>
                {errors.username && (
                  <p id="username-error" className={FIELD_ERROR}>
                    {errors.username.message}
                  </p>
                )}
              </div>
              <div className="relative flex flex-col gap-2">
                <Label htmlFor="password" className="font-semibold">
                  비밀번호
                </Label>
                <InputGroup className={FIELD}>
                  <InputGroupAddon className={FIELD_ICON}>
                    <LockKeyhole aria-hidden="true" />
                  </InputGroupAddon>
                  <InputGroupInput
                    id="password"
                    type={showPassword ? 'text' : 'password'}
                    placeholder="비밀번호를 입력하세요"
                    autoComplete="current-password"
                    className={FIELD_INPUT}
                    aria-invalid={errors.password ? true : undefined}
                    aria-describedby={errors.password ? 'password-error' : undefined}
                    {...register('password')}
                  />
                  <InputGroupAddon align="inline-end" className="pr-3">
                    <InputGroupButton
                      size="icon-sm"
                      className="text-muted-foreground hover:text-foreground [&>svg]:size-[18px]"
                      aria-label={showPassword ? '비밀번호 숨기기' : '비밀번호 보기'}
                      aria-pressed={showPassword}
                      onClick={() => setShowPassword((shown) => !shown)}
                    >
                      {showPassword ? <EyeOff /> : <Eye />}
                    </InputGroupButton>
                  </InputGroupAddon>
                </InputGroup>
                {errors.password ? (
                  <p id="password-error" className={FIELD_ERROR}>
                    {errors.password.message}
                  </p>
                ) : (
                  // 서버 오류(틀린 비밀번호 등)도 같은 자리에 띄워 폼이 밀리지 않게 한다.
                  error && (
                    <p role="alert" className={FIELD_ERROR}>
                      {error}
                    </p>
                  )
                )}
              </div>
              <Button type="submit" size="lg" disabled={pending} className={SUBMIT}>
                {pending ? '로그인 중…' : '로그인'}
              </Button>
            </form>
          </div>
        </div>
      </section>
      {wide && <LoginHero className="hidden lg:flex" />}
    </main>
  );
}
