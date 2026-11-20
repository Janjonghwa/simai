"""Tier 0 표시 파일에서 외형 주기용 작업 상태를 읽는다."""

import json
from pathlib import Path


def read_work_state(marker, now_s, max_age_s):
    try:
        path = Path(marker)
        if abs(now_s - path.stat().st_mtime) > max_age_s:
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("started") is not True:
            return None
        value = doc.get("work_in_progress")
        return value if type(value) is bool else None
    except (OSError, ValueError, UnicodeError):
        return None
