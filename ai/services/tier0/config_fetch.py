"""구성 묶음 내려받기(config.Fetcher 구현): EC2 가 알린 config_bundle 의 주소에서 바이트를 받는다 (명세 3.2, 11.1, 14).

서버의 묶음 주소 형식(임시 서명 HTTPS 주소 예상)은 아직 확정 전이다(contracts/config_bundle 의 uri 는 문자열). 여기서는
https 만 받는다: 인증서 검증을 끄는 경로가 없고, 리다이렉트를 따르지 않으며, 응답 크기에 상한을 둔다. 다른 형식(s3:// 등)은
FetchError 로 거부한다(ConfigSync 가 DOWNLOAD_FAILED 로 ACK 한다. 지어내지 않는다). 주소의 서명(query)은 로그·예외에
넣지 않는다.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request

from simai_ai.config import FetchError

MAX_BYTES = 16 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class HttpsFetcher:
    def __init__(self, *, timeout_s: float = 30.0, ca_file: str | None = None, max_bytes: int = MAX_BYTES) -> None:
        context = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirect())
        self._timeout_s = timeout_s
        self._max = max_bytes

    def fetch(self, uri: str) -> bytes:
        parts = urllib.parse.urlsplit(uri)
        if parts.scheme != "https" or not parts.hostname:
            raise FetchError("https 주소만 받는다")
        try:
            with self._opener.open(urllib.request.Request(uri, method="GET"), timeout=self._timeout_s) as resp:
                if resp.status != 200:
                    raise FetchError(f"응답 {resp.status}")
                data = resp.read(self._max + 1)
        except FetchError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise FetchError(f"내려받기 실패 ({type(e).__name__})") from None
        if len(data) > self._max:
            raise FetchError("묶음 구성 요소가 크기 상한을 넘었다")
        return data
