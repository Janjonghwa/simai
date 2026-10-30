"""아바타 운영 로그의 예외 요약. 인증값·서명 주소·줄바꿈은 남기지 않는다."""

import re


def exception_summary(error, *, secrets=()):
    message = str(error)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "<REDACTED>")
    message = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+", "<URL>", message)
    message = re.sub(r"(?i)bearer\s+[^\s,;\"']+", "Bearer <REDACTED>", message)
    message = re.sub(r"(?i)((?:[\w-]*(?:token|secret|password|signature)|authorization)[\"']?\s*[:=]\s*)"
                     r"(?:[\"'][^\"']*[\"']|[^\s,;]+)", r"\1<REDACTED>", message)
    message = " ".join(message.split())[:300]
    return f"{type(error).__name__}: {message}"
