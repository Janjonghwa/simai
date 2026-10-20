import type { ZodType } from 'zod';

/** 연결 자체가 안 됐을 때(서버 꺼짐, 네트워크 끊김)의 `ApiError.status`. */
export const NETWORK_ERROR = 0;

export class ApiError extends Error {
  readonly status: number;
  /** 서버가 보낸 오류 문구. 없으면 훅이 든 한국어 기본 문구를 쓴다. */
  readonly serverMessage?: string;

  constructor(status: number, serverMessage?: string) {
    super(serverMessage ?? `요청이 실패했다 (HTTP ${status})`);
    this.name = 'ApiError';
    this.status = status;
    this.serverMessage = serverMessage;
  }
}

type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

export interface RequestOptions<T> {
  method?: Method;
  body?: unknown;
  /** 응답 본문 검사. 통과한 값만 돌려준다. */
  schema: ZodType<T>;
  signal?: AbortSignal;
}

export type Http = <T>(path: string, options: RequestOptions<T>) => Promise<T>;

export interface HttpOptions {
  baseUrl: string;
  getToken?: () => string | undefined;
  /** 토큰을 실어 보낸 요청이 401 이면 부른다(토큰 만료·폐기). */
  onUnauthorized?: () => void;
}

// import.meta.env 를 읽지 않는다. 주소와 토큰은 부르는 쪽(lib/api.ts)이 넘긴다.
export function createHttp({ baseUrl, getToken, onUnauthorized }: HttpOptions): Http {
  const base = baseUrl.replace(/\/+$/, '');

  return async (path, { method = 'GET', body, schema, signal }) => {
    // base 뒤에 붙이므로 / 로 시작해야 한다. '@host/…' 같은 값은 http://base@host/… 가 되어 토큰이 다른 서버로 간다.
    if (!path.startsWith('/') || path.startsWith('//')) {
      throw new Error(`REST 경로는 / 로 시작해야 한다: ${path}`);
    }
    const token = getToken?.();
    const headers = new Headers({ Accept: 'application/json' });
    if (body !== undefined) headers.set('Content-Type', 'application/json');
    if (token) headers.set('Authorization', `Bearer ${token}`);

    let response: Response;
    try {
      response = await fetch(`${base}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal,
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new ApiError(NETWORK_ERROR);
    }

    if (!response.ok) {
      // 그사이 다시 로그인해 토큰이 바뀌었으면 새 세션은 끝내지 않는다.
      if (response.status === 401 && token && getToken?.() === token) onUnauthorized?.();
      throw new ApiError(response.status, await readServerMessage(response));
    }
    return schema.parse(response.status === 204 ? undefined : await response.json());
  };
}

// Spring 기본 오류 본문(message)과 ProblemDetail(detail)을 둘 다 읽는다.
async function readServerMessage(response: Response): Promise<string | undefined> {
  const data: unknown = await response.json().catch(() => undefined);
  if (typeof data !== 'object' || data === null) return undefined;
  if ('message' in data && typeof data.message === 'string' && data.message) return data.message;
  if ('detail' in data && typeof data.detail === 'string' && data.detail) return data.detail;
  return undefined;
}
