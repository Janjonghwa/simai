import { ROLES } from '@/store/session.store';
import { z } from 'zod';

// 서버 OpenAPI(S15P31A101-61)가 나오기 전까지 쓰는 가정이다. 나오면 생성 타입으로 바꾼다.
export const loginResponseSchema = z.object({
  accessToken: z.string().min(1),
  user: z.object({
    id: z.string().min(1),
    role: z.enum(ROLES),
  }),
});

export const loginFormSchema = z.object({
  username: z.string().trim().min(1, '아이디를 입력하세요'),
  password: z.string().min(1, '비밀번호를 입력하세요'),
});

export type LoginForm = z.infer<typeof loginFormSchema>;
