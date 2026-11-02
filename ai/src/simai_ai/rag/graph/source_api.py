"""국가법령정보 공동활용 Open API(DRF) 클라이언트와 XML/JSON → LawDoc 파서.

- 인증값 OC 는 환경변수 `LAW_GO_KR_OC` 로만 받는다. 등록한 서버 IP 에서만 응답한다. 주소·예외·manifest 에 OC 를 남기지 않는다
  (`redact`).
- 현행 본문: `lawSearch.do?target=eflaw` 에서 이름이 같고 `현행연혁코드=현행` 인 행의 법령일련번호(MST)·시행일자(efYd)를 고르고
  `lawService.do?target=eflaw&MST=…&efYd=…` 로 받는다. `target=law&MST=…` 는 그 MST 의 공포 시점 본문이라 그 뒤 시행된 개정
  (다른 MST 의 부칙 개정 포함)이 빠진다(2026-11-02 산업안전보건기준에 관한 규칙: law 는 제619조의2 "산소 및 유해가스 농도의 측정",
  eflaw 는 "… 측정 및 기록 등").
- 고시 등 행정규칙: `lawSearch.do?target=admrul`(현행) → `lawService.do?target=admrul&ID=행정규칙일련번호`.
- 관계 교차 확인: `lawSearch.do?target=lsRlt&ID=법령ID`(법령정보지식베이스 관련법령, 법령 단위).
- 받은 응답은 `cache_dir` 에 그대로 두고 다시 부르지 않는다(짧은 시간 과도한 호출 제한). 요청 사이 `min_interval_s`(기본 1초).

응답 모양(조문단위·항·호·목, 별표단위)은 공식 안내(open.law.go.kr 가이드 lawService)와 실제 응답(2026-11-02)을 따른다.
조 머리와 항·호·목 줄을 문서 순서로 펴서 `ArticleBuilder` 에 넣으므로 웹 본문 경로와 같은 LawDoc 이 나온다.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from simai_ai.rag.graph.catalog import LawSpec
from simai_ai.rag.graph.ir import Appendix, ArticleBuilder, LawDoc, split_head

API = "https://www.law.go.kr/DRF"
OC_ENV = "LAW_GO_KR_OC"
_TIMEOUT_S = 90
_HEADING_RE = re.compile(r"^제\s*\d+(?:의\d+)?\s*(편|장|절|관)(?:\s|$)")
_HEADING_LEVEL = {"편": 0, "장": 1, "절": 2, "관": 3}
_IMG_RE = re.compile(r"</?img[^>]*>")  # 본문 안 수식·표 그림 자리표(웹 본문에는 없다)


class LawApiError(RuntimeError):
    pass


def redact(text: str, oc: str | None) -> str:
    text = re.sub(r"(OC=)[^&\s]+", r"\1***", text)
    return text.replace(oc, "***") if oc else text


def scrub(raw: bytes, oc: str | None) -> bytes:
    return raw.replace(oc.encode("utf-8"), b"***") if oc else raw


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------- XML/JSON → dict


def xml_to_dict(elem: ET.Element) -> Any:
    """자식이 없으면 문자열, 있으면 {태그: 값}(같은 태그가 여럿이면 목록). 속성은 버린다."""
    children = list(elem)
    if not children:
        return (elem.text or "").strip()
    out: dict[str, Any] = {}
    for c in children:
        v = xml_to_dict(c)
        if c.tag in out:
            if not isinstance(out[c.tag], list):
                out[c.tag] = [out[c.tag]]
            out[c.tag].append(v)
        else:
            out[c.tag] = v
    return out


def _list(v: Any) -> list[Any]:
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def _str(v: Any) -> str:
    if isinstance(v, dict):  # JSON 은 속성이 있는 요소를 {"content": …} 로 준다
        v = v.get("content", "")
    return v.strip() if isinstance(v, str) else ""


def _lines(v: Any) -> list[str]:
    """문자열이나 줄 목록 → 줄(빈 줄 제외). 한 값 안의 개행도 줄로 나눈다."""
    out: list[str] = []
    for x in _list(v):
        if isinstance(x, str):
            x = html.unescape(_IMG_RE.sub("", x))
            out += [ln.strip() for ln in x.split("\n") if ln.strip()]
    return out


def check_error(raw: bytes) -> None:
    """인증 실패 등 오류 응답이면 LawApiError. 메시지에는 응답 본문만 쓴다(주소·OC 없음)."""
    head = raw[:600].decode("utf-8", "replace")
    if "<Response>" in head and "<result>" in head:
        m = re.search(r"<result>(.*?)</result>.*?<msg>(.*?)</msg>", raw.decode("utf-8", "replace"), re.S)
        raise LawApiError(f"{m.group(1).strip()} {m.group(2).strip()}" if m else "오류 응답")
    if head.lstrip().startswith("{"):
        try:
            doc = json.loads(raw)
        except ValueError:
            return
        if isinstance(doc, dict) and "result" in doc and "msg" in doc:
            raise LawApiError(f"{doc['result']} {doc['msg']}")


def parse_xml_root(raw: bytes) -> ET.Element:
    check_error(raw)
    return ET.fromstring(raw)  # noqa: S314 (법제처 고정 주소 응답, 외부 엔티티 없음)


# ---------------------------------------------------------------- 법령 본문 → LawDoc


class _Assembler:
    def __init__(self) -> None:
        self.chapters: list[str] = []
        self.articles: list[Any] = []
        self.builder: ArticleBuilder | None = None

    def heading(self, text: str) -> None:
        text = " ".join(text.split())
        m = _HEADING_RE.match(text)
        if not m:
            return
        level = _HEADING_LEVEL[m.group(1)]
        self.chapters = [c for c in self.chapters if _HEADING_LEVEL[_HEADING_RE.match(c).group(1)] < level]  # type: ignore[union-attr]
        self.chapters.append(text)

    def start(self, head_line: str, effective: str | None) -> None:
        self.flush()
        label, rest = split_head(" ".join(head_line.split()))
        self.builder = ArticleBuilder(label, chapter=" > ".join(self.chapters) or None, effective=effective or None)
        if rest:
            self.builder.add_line(rest)

    def line(self, text: str) -> None:
        if self.builder is not None:
            self.builder.add_line(text)

    def flush(self) -> None:
        if self.builder is not None:
            self.articles.append(self.builder.finish())
            self.builder = None


def law_from_dict(law: dict[str, Any], spec: LawSpec, *, source: str, source_url: str, raw: bytes, fetched_at: str | None = None) -> LawDoc:
    """lawService(target=law·eflaw) 응답의 `법령` 객체 → LawDoc."""
    info = law["기본정보"]
    name = _str(info.get("법령명_한글"))
    if name.replace(" ", "") != spec.name.replace(" ", ""):
        raise ValueError(f"다른 법령이다: {name} (기대 {spec.name})")
    asm = _Assembler()
    for unit in _list(law.get("조문", {}).get("조문단위") if isinstance(law.get("조문"), dict) else None):
        head = _lines(unit.get("조문내용"))
        if _str(unit.get("조문여부")) == "전문":
            if head:
                asm.heading(head[0])
            continue
        if not head:
            continue
        asm.start(head[0], _str(unit.get("조문시행일자")))
        for extra in head[1:]:
            asm.line(extra)
        for hang in _list(unit.get("항")):
            if not isinstance(hang, dict):
                continue
            for ln in _lines(hang.get("항내용")):
                asm.line(ln)
            for ho in _list(hang.get("호")):
                if not isinstance(ho, dict):
                    continue
                for ln in _lines(ho.get("호내용")):
                    asm.line(ln)
                for mok in _list(ho.get("목")):
                    if isinstance(mok, dict):
                        for ln in _lines(mok.get("목내용")):
                            asm.line(ln)
    asm.flush()
    appendices = []
    byl = law.get("별표")
    for b in _list(byl.get("별표단위") if isinstance(byl, dict) else None):
        if not isinstance(b, dict):
            continue
        title = html.unescape(_str(b.get("별표제목")))
        if _str(b.get("별표구분")) != "별표" or title.startswith("삭제"):
            continue
        text = "\n".join(_lines(b.get("별표내용"))) or None
        appendices.append(Appendix(int(_str(b.get("별표번호")) or 0), int(_str(b.get("별표가지번호")) or 0), title, text))
    return LawDoc(
        law_key=spec.key,
        law_id=_str(info.get("법령ID")),
        law_name=spec.name,
        mst=_mst_from(law, info),
        law_kind=_str(info.get("법종구분")) or None,
        promulgation_date=_str(info.get("공포일자")),
        promulgation_no=_str(info.get("공포번호")),
        effective_date=_str(info.get("시행일자")),
        source=source,
        source_url=source_url,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        fetched_at=fetched_at or _now(),
        articles=asm.articles,
        appendices=appendices,
    )


def _mst_from(law: dict[str, Any], info: dict[str, Any]) -> str:
    return _str(info.get("법령일련번호")) or _str(law.get("법령일련번호")) or ""


def parse_law_xml(raw: bytes, spec: LawSpec, *, source_url: str, mst: str = "", fetched_at: str | None = None) -> LawDoc:
    root = parse_xml_root(raw)
    if root.tag != "법령":
        raise ValueError(f"법령 본문 응답이 아니다: <{root.tag}>")
    doc = law_from_dict(xml_to_dict(root), spec, source="api-xml", source_url=source_url, raw=raw, fetched_at=fetched_at)
    if mst and not doc.mst:
        doc.mst = mst
    return doc


def parse_law_json(raw: bytes, spec: LawSpec, *, source_url: str, mst: str = "", fetched_at: str | None = None) -> LawDoc:
    check_error(raw)
    doc = law_from_dict(json.loads(raw)["법령"], spec, source="api-json", source_url=source_url, raw=raw, fetched_at=fetched_at)
    if mst and not doc.mst:
        doc.mst = mst
    return doc


def parse_admrul_xml(raw: bytes, spec: LawSpec, *, source_url: str, fetched_at: str | None = None) -> LawDoc:
    """행정규칙(고시) 본문. 조문내용 하나가 장·절 머리이거나 조 하나(첫 줄 머리 + 호 줄)다."""
    root = parse_xml_root(raw)
    d = xml_to_dict(root)
    info = d["행정규칙기본정보"]
    name = _str(info.get("행정규칙명"))
    if name.replace(" ", "") != spec.name.replace(" ", ""):
        raise ValueError(f"다른 행정규칙이다: {name} (기대 {spec.name})")
    asm = _Assembler()
    for blob in _list(d.get("조문내용")):
        lines = _lines(blob)
        if not lines:
            continue
        if _HEADING_RE.match(" ".join(lines[0].split())):
            asm.heading(lines[0])
            continue
        try:
            asm.start(lines[0], None)
        except ValueError:
            asm.line(lines[0])  # 조 머리가 아닌 덩어리는 앞 조의 이어지는 줄
        for ln in lines[1:]:
            if ln.startswith("<img") or ln.startswith("</img"):
                continue
            asm.line(ln)
    asm.flush()
    return LawDoc(
        law_key=spec.key,
        law_id=_str(info.get("행정규칙ID")),
        law_name=spec.name,
        mst=_str(info.get("행정규칙일련번호")),
        law_kind=_str(info.get("행정규칙종류")) or None,
        promulgation_date=_str(info.get("발령일자")),
        promulgation_no=_str(info.get("발령번호")),
        effective_date=_str(info.get("시행일자")),
        source="api-xml",
        source_url=source_url,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        fetched_at=fetched_at or _now(),
        articles=asm.articles,
        appendices=[],
    )


# ---------------------------------------------------------------- 클라이언트


@dataclass
class Fetch:
    url: str  # OC 를 가린 주소
    path: str
    sha256: str
    bytes: int
    fetched_at: str
    cached: bool


@dataclass
class Client:
    cache_dir: Path
    oc: str | None = None
    min_interval_s: float = 1.0
    log: list[Fetch] = field(default_factory=list)
    _last: float = 0.0

    def __post_init__(self) -> None:
        self.oc = self.oc or os.environ.get(OC_ENV) or None
        self.cache_dir = Path(self.cache_dir)

    def _oc(self) -> str:
        if not self.oc:
            raise LawApiError(f"{OC_ENV} 가 없다(사용자 환경변수). 키 없이 돌리려면 source_web 경로를 쓴다")
        return self.oc

    def get(self, path: str, params: dict[str, str], cache_name: str) -> bytes:
        cache = self.cache_dir / cache_name
        public = f"{API}/{path}?{urllib.parse.urlencode({'OC': '***', **params})}"
        if cache.exists():
            raw = scrub(cache.read_bytes(), self.oc)
            self.log.append(Fetch(public, str(cache), hashlib.sha256(raw).hexdigest(), len(raw), _mtime(cache), True))
            return raw
        delay = self._last + self.min_interval_s - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        url = f"{API}/{path}?{urllib.parse.urlencode({'OC': self._oc(), **params})}"
        try:
            with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:  # noqa: S310 (https 고정 주소)
                raw = resp.read()
        except OSError as e:  # 주소(OC 포함)가 예외 문자열에 섞이지 않게 가린다
            raise LawApiError(redact(f"{path} 요청 실패: {e}", self.oc)) from None
        finally:
            self._last = time.monotonic()
        check_error(raw)
        raw = scrub(raw, self._oc())  # 응답 안 상세 링크에도 OC 가 들어 있다(lawSearch·lsRlt). 캐시·해시는 가린 바이트로
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(raw)
        self.log.append(Fetch(public, str(cache), hashlib.sha256(raw).hexdigest(), len(raw), _now(), False))
        return raw

    # 법령 ---------------------------------------------------------------
    def current_law(self, spec: LawSpec) -> dict[str, str]:
        raw = self.get("lawSearch.do", {"target": "eflaw", "type": "XML", "query": spec.name, "nw": "3", "display": "100"},
                       f"search/eflaw_{spec.key}.xml")
        rows = [{c.tag: (c.text or "").strip() for c in law} for law in parse_xml_root(raw).findall("law")]
        hits = [r for r in rows if r.get("법령명한글", "").replace(" ", "") == spec.name.replace(" ", "") and r.get("현행연혁코드") == "현행"]
        if len(hits) != 1:
            raise LawApiError(f"현행 {spec.name} 를 하나로 찾지 못했다: {len(hits)}건")
        return hits[0]

    def law_xml(self, spec: LawSpec, mst: str, ef_yd: str) -> tuple[str, bytes]:
        params = {"target": "eflaw", "MST": mst, "efYd": ef_yd, "type": "XML"}
        raw = self.get("lawService.do", params, f"law/{spec.key}_{mst}_{ef_yd}.xml")
        return f"{API}/lawService.do?{urllib.parse.urlencode({'OC': '***', **params})}", raw

    def fetch_law(self, spec: LawSpec) -> LawDoc:
        row = self.current_law(spec)
        url, raw = self.law_xml(spec, row["법령일련번호"], row["시행일자"])
        doc = parse_law_xml(raw, spec, source_url=url, mst=row["법령일련번호"], fetched_at=self.log[-1].fetched_at)
        doc.mst = row["법령일련번호"]
        doc.effective_date = row["시행일자"]
        return doc

    # 행정규칙 -----------------------------------------------------------
    def current_admrul(self, spec: LawSpec) -> dict[str, str]:
        raw = self.get("lawSearch.do", {"target": "admrul", "type": "XML", "query": spec.name, "nw": "1", "display": "100"},
                       f"search/admrul_{spec.key}.xml")
        rows = [{c.tag: (c.text or "").strip() for c in r} for r in parse_xml_root(raw).findall("admrul")]
        hits = [r for r in rows if r.get("행정규칙명", "").replace(" ", "") == spec.name.replace(" ", "") and r.get("현행연혁구분") == "현행"]
        if len(hits) != 1:
            raise LawApiError(f"현행 {spec.name} 를 하나로 찾지 못했다: {len(hits)}건")
        return hits[0]

    def fetch_admrul(self, spec: LawSpec) -> LawDoc:
        row = self.current_admrul(spec)
        params = {"target": "admrul", "ID": row["행정규칙일련번호"], "type": "XML"}
        raw = self.get("lawService.do", params, f"admrul/{spec.key}_{row['행정규칙일련번호']}.xml")
        url = f"{API}/lawService.do?{urllib.parse.urlencode({'OC': '***', **params})}"
        return parse_admrul_xml(raw, spec, source_url=url, fetched_at=self.log[-1].fetched_at)

    def fetch(self, spec: LawSpec) -> LawDoc:
        return self.fetch_admrul(spec) if spec.level == "ADMIN_RULE" else self.fetch_law(spec)

    # 관련법령(법령 단위) -------------------------------------------------
    def related_laws(self, law_id: str) -> list[dict[str, str]]:
        raw = self.get("lawSearch.do", {"target": "lsRlt", "ID": law_id, "type": "XML"}, f"related/lsRlt_{law_id}.xml")
        return parse_related(raw)


def parse_related(raw: bytes) -> list[dict[str, str]]:
    """lsRlt 응답 → [{기준법령ID, 기준법령명, 관련법령ID, 관련법령명, 법령간관계}]. 행의 정확한 중첩은 판마다 달라 잎 태그로 모은다."""
    root = parse_xml_root(raw)
    out: list[dict[str, str]] = []
    for node in root.iter():
        kids = {c.tag: (c.text or "").strip() for c in node if len(c) == 0}
        if "관련법령명" in kids or "관련법령ID" in kids:
            out.append(kids)
    base = {c.tag: (c.text or "").strip() for c in root.iter() if c.tag in ("기준법령ID", "기준법령명")}
    for r in out:
        for k, v in base.items():
            r.setdefault(k, v)
    return out


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds")
