"""대체 수집 경로: 국가법령정보센터 웹 "법령 본문" HTML → LawDoc.

Open API 인증키(OC)와 서버 IP 등록이 없을 때 쓴다. 국가법령정보센터 robots.txt 는 `Allow: /` 이고(2026-11-02 확인), 법령 본문은
저작권법 제7조 제1호로 보호받지 못하는 저작물이다. 그래도 요청 사이에 `min_interval_s`(기본 3초)를 두고 법령당 2번(현행 판 찾기,
본문)만 부른다.

1. `https://www.law.go.kr/법령/{이름에서 공백 뺀 것}` 의 iframe 주소에서 현행 lsiSeq(=법령일련번호 MST)·efYd(오늘 기준 시행일)를 읽는다.
2. `POST /LSW/lsInfoR.do`(lsiSeq, efYd, chrClsCd=010202) 가 본문 HTML 을 준다. efYd 를 오늘 기준 시행일로 줘야 그 판 안의
   나중 시행 조문(다른 법령 부칙 개정 포함)까지 반영된다(2025-09-01 과 2026-03-02 본문이 다르다, 설계 문서).
3. 본문에서 문단 클래스로 줄을 고른다: `gtit` 편·장·절, `pty1_p4` 조 머리(+첫 항), `pty1_de2_1`·`pty1_de2h`·`pty1_de3` 항·호·목,
   `pty1_de2` 조 꼬리 주석([전문개정 …], 버림), `pty3*` 부칙(여기서 조문 수집을 멈춘다). 별표는 `a.blu` 의 "[별표 N] 제목".
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser

from simai_ai.rag.graph.catalog import LawSpec
from simai_ai.rag.graph.ir import Appendix, ArticleBuilder, LawDoc, split_head

BASE = "https://www.law.go.kr"
USER_AGENT = "simai-legal-graph/0.1 (offline safety-law index; polite crawler)"
_TIMEOUT_S = 60
_IFRAME_RE = re.compile(r'src="/LSW/+lsInfoP\.do\?([^"]+)"')
_HEADER_RE = re.compile(
    r"\[시행\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\]\s*\[(\S+)\s+제(\d+)호,\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.,\s*([^\]]+)\]"
)
_APPENDIX_RE = re.compile(r"^\[별표\s*(\d+)(?:의(\d+))?\]\s*(.+)$")
_HEADING_LEVEL = {"편": 0, "장": 1, "절": 2, "관": 3}
_HEADING_RE = re.compile(r"^제\d+(?:의\d+)?(편|장|절|관)\s")


class Throttle:
    def __init__(self, min_interval_s: float = 3.0) -> None:
        self.min_interval_s = min_interval_s
        self._last = 0.0

    def wait(self) -> None:
        delay = self._last + self.min_interval_s - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()


@dataclass(frozen=True)
class Edition:
    lsi_seq: str
    ef_yd: str


def _request(url: str, data: bytes | None, throttle: Throttle) -> bytes:
    throttle.wait()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 (https 고정 주소)
        return resp.read()


def friendly_url(name: str) -> str:
    return f"{BASE}/{urllib.parse.quote('법령')}/{urllib.parse.quote(name.replace(' ', ''))}"


def parse_edition(page: str) -> Edition:
    m = _IFRAME_RE.search(page)
    if not m:
        raise ValueError("법령 페이지에서 본문 iframe(lsInfoP.do) 주소를 찾지 못했다")
    q = urllib.parse.parse_qs(m.group(1).replace("&amp;", "&"))
    return Edition(q["lsiSeq"][0], q["efYd"][0])


def current_edition(name: str, throttle: Throttle) -> Edition:
    return parse_edition(_request(friendly_url(name), None, throttle).decode("utf-8"))


def body_request(edition: Edition) -> tuple[str, bytes]:
    data = urllib.parse.urlencode({"lsiSeq": edition.lsi_seq, "chrClsCd": "010202", "efYd": edition.ef_yd, "ancYnChk": "0"})
    return f"{BASE}/LSW/lsInfoR.do", data.encode("ascii")


def fetch_body(edition: Edition, throttle: Throttle) -> tuple[str, bytes]:
    url, data = body_request(edition)
    return f"{url}?{data.decode('ascii')}", _request(url, data, throttle)


class _Blocks(HTMLParser):
    """본문 HTML → [(종류, 텍스트)]. 종류: meta:<id>, heading, head, line, appendix."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.blocks: list[tuple[str, str]] = []
        self._p_class: str | None = None
        self._label: list[str] | None = None
        self._in_label = False
        self._buf: list[str] = []
        self._blu: list[str] | None = None
        self._addenda = False
        self.header: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "input" and a.get("type") == "hidden" and a.get("id") and a.get("value") is not None:
            self.meta.setdefault(a["id"], a["value"] or "")
        elif tag == "p":
            self._p_class = a.get("class") or ""
            self._buf = []
            self._label = [] if self._p_class == "pty1_p4" else None
        elif tag == "label" and self._label is not None:
            self._in_label = True
        elif tag == "a" and a.get("class") == "blu":
            self._blu = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self._in_label = False
        elif tag == "a" and self._blu is not None:
            text = " ".join("".join(self._blu).split())
            if text.startswith("[별표"):
                self.blocks.append(("appendix", text))
            self._blu = None
        elif tag == "p" and self._p_class is not None:
            self._close_p()

    def _close_p(self) -> None:
        cls, text = self._p_class or "", "".join(self._buf).strip()
        self._p_class = None
        if cls.startswith("pty3"):
            self._addenda = True
        if self._addenda or not text:
            return
        if cls == "gtit":
            self.blocks.append(("heading", " ".join(text.split())))
        elif cls == "pty1_p4":
            label = " ".join("".join(self._label or []).split())
            rest = text
            if label and rest.startswith(label):
                rest = rest[len(label):]
            self.blocks.append(("head", label))
            self.blocks.append(("line", rest))
        elif cls in ("pty1_de2_1", "pty1_de2h", "pty1_de3"):
            self.blocks.append(("line", text))
        # pty1_de2(조 꼬리 주석)·그 밖의 문단은 버린다

    def handle_data(self, data: str) -> None:
        if self._p_class is not None:
            self._buf.append(data)
            if self._in_label and self._label is not None:
                self._label.append(data)
        if self._blu is not None:
            self._blu.append(data)
        if self.header is None and "[시행" in data and _HEADER_RE.search(" ".join(data.split())):
            self.header = " ".join(data.split())


def parse(html: bytes | str, spec: LawSpec, *, source_url: str, fetched_at: str | None = None) -> LawDoc:
    raw = html.encode("utf-8") if isinstance(html, str) else html
    p = _Blocks()
    p.feed(raw.decode("utf-8"))
    p.close()
    meta = p.meta
    if meta.get("lsNm") and meta["lsNm"].replace(" ", "") != spec.name.replace(" ", ""):
        raise ValueError(f"다른 법령이다: {meta['lsNm']} (기대 {spec.name})")
    hm = _HEADER_RE.search(p.header or "")
    chapters: list[str] = []
    articles = []
    appendices = []
    builder: ArticleBuilder | None = None

    def flush() -> None:
        nonlocal builder
        if builder is not None:
            articles.append(builder.finish())
            builder = None

    for kind, text in p.blocks:
        if kind == "heading":
            m = _HEADING_RE.match(text + " ")
            if m:
                level = _HEADING_LEVEL[m.group(1)]
                chapters = [c for c in chapters if _HEADING_LEVEL[_HEADING_RE.match(c + " ").group(1)] < level]  # type: ignore[union-attr]
                chapters.append(text)
        elif kind == "head":
            flush()
            label, _ = split_head(text)
            builder = ArticleBuilder(label, chapter=" > ".join(chapters) or None, effective=None)
        elif kind == "line" and builder is not None:
            builder.add_line(text)
        elif kind == "appendix":
            m = _APPENDIX_RE.match(text)
            if m:
                appendices.append(Appendix(int(m.group(1)), int(m.group(2) or 0), m.group(3).strip()))
    flush()
    return LawDoc(
        law_key=spec.key,
        law_id=meta.get("lsId", ""),
        law_name=spec.name,
        mst=meta.get("lsiSeq", ""),
        law_kind=hm.group(4) if hm else None,
        promulgation_date=meta.get("ancYd", ""),
        promulgation_no=meta.get("ancNo", ""),
        effective_date=meta.get("efYd", ""),
        source="web-html",
        source_url=source_url,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        fetched_at=fetched_at or datetime.now(UTC).isoformat(timespec="seconds"),
        articles=articles,
        appendices=appendices,
    )
