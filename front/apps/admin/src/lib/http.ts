import type { ZodType } from 'zod';

/** 서버 응답을 받지 못했을 때(서버 꺼짐, 네트워크 끊김, 시간 초과)의 `ApiError.status`. */
export const NETWORK_ERROR = 0;

/**
 * 실패 종류.
 * - HTTP: 서버가 오류 상태(4xx·5xx)로 답했다.
 * - NETWORK: 연결이 안 됐다.
 * - TIMEOUT: 정한 시간 안에 답이 끝나지 않았다.
 * - INVALID_RESPONSE: 성공 상태인데 본문이 JSON 이 아니거나 스키마와 다르다.
 */
export type ApiErrorKind = 'HTTP' | 'NETWORK' | 'TIMEOUT' | 'INVALID_RESPONSE';

export interface ApiErrorOptions {
  kind?: ApiErrorKind;
  /** 서버가 보낸 오류 문구. 보여 줄지는 훅이 정한다(5xx 문구에는 서버 내부 정보가 섞일 수 있다). */
  serverMessage?: string;
  cause?: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly kind: ApiErrorKind;
  readonly serverMessage?: string;

  constructor(status: number, { kind, serverMessage, cause }: ApiErrorOptions = {}) {
    const resolved = kind ?? (status === NETWORK_ERROR ? 'NETWORK' : 'HTTP');
    // 서버 문구는 message 에 넣지 않는다. 로그·오류 화면으로 서버 내부 정보가 새지 않게 한다.
    super(`요청이 실패했다 (${resolved === 'HTTP' ? `HTTP ${status}` : resolved})`, { cause });
    this.name = 'ApiError';
    this.status = status;
    this.kind = resolved;
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
  /** 토큰을 실어 보낸 요청이 401 이면 그 토큰을 넘겨 부른다(토큰 만료·폐기). */
  onUnauthorized?: (token: string) => void;
  /** 요청 하나가 끝나기까지 기다리는 시간(ms). 서버가 멈춰도 화면이 "요청 중"에 머물지 않게 한다. */
  timeoutMs?: number;
}

const DEFAULT_TIMEOUT_MS = 10_000;

// import.meta.env 를 읽지 않는다. 주소와 토큰은 부르는 쪽(lib/api.ts)이 넘긴다.
export function createHttp({
  baseUrl,
  getToken,
  onUnauthorized,
  timeoutMs = DEFAULT_TIMEOUT_MS,
}: HttpOptions): Http {
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
    // 직렬화 오류(순환 참조 등)는 코드 문제라 연결 실패로 바꾸지 않는다.
    const payload = body === undefined ? undefined : JSON.stringify(body);
    const timeout = AbortSignal.timeout(timeoutMs);

    let response: Response;
    let text: string;
    try {
      response = await fetch(`${base}${path}`, {
        method,
        headers,
        body: payload,
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      });
      text = await response.text();
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new ApiError(NETWORK_ERROR, {
        kind: timeout.aborted ? 'TIMEOUT' : 'NETWORK',
        cause: error,
      });
    }

    if (!response.ok) {
      if (response.status === 401 && token) onUnauthorized?.(token);
      throw new ApiError(response.status, { serverMessage: readServerMessage(text) });
    }
    let data: unknown;
    try {
      data = text === '' ? undefined : JSON.parse(text);
    } catch (error) {
      throw new ApiError(response.status, { kind: 'INVALID_RESPONSE', cause: error });
    }
    const parsed = schema.safeParse(data);
    if (!parsed.success) {
      throw new ApiError(response.status, { kind: 'INVALID_RESPONSE', cause: parsed.error });
    }
    return parsed.data;
  };
}

// Spring 기본 오류 본문(message)과 ProblemDetail(detail)을 둘 다 읽는다.
function readServerMessage(text: string): string | undefined {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return undefined;
  }
  if (typeof data !== 'object' || data === null) return undefined;
  if ('message' in data && typeof data.message === 'string' && data.message) return data.message;
  if ('detail' in data && typeof data.detail === 'string' && data.detail) return data.detail;
  return undefined;
}
