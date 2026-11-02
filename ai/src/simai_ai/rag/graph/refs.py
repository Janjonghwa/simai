"""조문 참조 결정적 추출(정규식). 모델을 쓰지 않아 같은 입력이면 늘 같은 엣지가 나온다.

인식하는 꼴:
- 내부 위치: `제N조(의M)`, `제N조제M항`, `제N조제M항제K호(의L)`, `…가목`, `별표 N(의M)`. 띄어 쓴 `제42조 제2항` 도 한 덩어리.
- 상대 위치: `제M항`·`제K호`만 쓰면 지금 조·지금 항 기준(법령 문장 관례), `같은 조`·`같은 항`·`같은 호`는 이 조문에서 바로 앞에
  가리킨 조·항·호, `이 조`는 지금 조, `전항`은 앞 항.
- 다른 법령: `「법령명」 제N조…`, `같은 법 (시행령|시행규칙) 제N조`, 약칭 `법 제N조`·`영 제N조`·`규칙 제N조`(본문
  "(이하 “법”이라 한다)" 정의, 없으면 catalog 기본값), `이 법|영|규칙 제N조`. 위치 없는 `「법령명」` 은 법령 단위 참조(form=law).
- 이어 쓰기: `제5조, 제16조, 제37조부터 제40조까지 및 제123조`, `영 제42조제3항제1호 또는 제2호` 처럼 이음말(및·또는·과·와·
  쉼표·ㆍ·부터·까지·내지)로만 이어진 위치는 앞 위치의 법령·상위 위치를 물려받는다. `부터 … 까지`는 사이 번호도 낸다(form=range).
- 관계 단서: 이은 묶음 바로 뒤가 `…에도 불구하고` → 불구하고, `…을/를 (각각) 준용` → 준용, `…에 따른/따라/의한` → 따른.

오탐을 줄이는 규칙(시험: tests/rag/graph/test_refs.py):
- 날짜가 든 꼬리표(`<개정 2019. 12. 26.>`, `[본조신설 …]`, `[제610조에서 이동 …]`)는 같은 길이 공백으로 가린 뒤 찾는다.
- 공포번호(`고용노동부령 제450호`, `법률 제12345호`, `대통령령 제…호`, `고시 제…호`)와 서식(`별지 제1호서식`)은 호가 아니다.
- `제3자`·`제1종`·`제2편`·`제1장` 은 위치가 아니다(조·항·호·목만).
- `같은 조치`·`이 조항` 처럼 낱말 일부인 `같은 조`·`이 조`는 뒤 글자(조사·공백·제 등)로 거른다.
- `건축법 제2조`(「」 없이 쓴 다른 법령 이름) 의 `법` 은 약칭이 아니다(앞이 한글이면 약칭으로 보지 않는다).
- 기준이 없는 `같은 법`·정의 없는 약칭·앞에 가리킨 곳이 없는 `같은 조` 는 지어내지 않고 버린다.
- 자기 자신·자기 상위(이 조 안의 "이 조에서" 등) 로 가는 참조는 build 단계에서 버린다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

ArtNo = tuple[int, int]  # (번호, 가지번호)

# 날짜가 든 개정 꼬리표·이동 주석: 같은 길이 공백으로 가린다
_MASK_RE = re.compile(
    r"<[^<>]*\d{4}\s?\.\s?\d{1,2}\s?\.[^<>]*>"
    r"|\[[^\[\]]*\d{4}\s?\.\s?\d{1,2}\s?\.[^\[\]]*\]"
    r"|\[[^\[\]]*(?:이동|본조신설|전문개정|제목개정|종전)[^\[\]]*\]"
)
_FOLLOW = r"(?=$|[\s,.)ㆍ·]|에|의|를|은|는|와|과|부터|까지|제|각|및|또는)"
_SUBITEM = "가나다라마바사아자차카타파하"
_TOKEN_SRC = (
    r"(?P<lawname>「(?P<lawname_v>[^「」]{1,80})」)"
    r"|(?P<samelaw>같은\s?(?:법|규칙|영)(?P<samelaw_sfx>\s?시행령|\s?시행규칙)?)(?=\s?(?:제\s?\d|별표))"
    r"|(?<![가-힣])(?P<thislaw>이\s(?:법|영|규칙))(?=\s?(?:제\s?\d|별표))"
    r"|(?<![가-힣])(?P<named>{named})(?=\s?(?:제\s?\d|별표))"
    r"|(?<![가-힣])(?P<abbr>법|영|규칙)(?=\s?(?:제\s?\d|별표))"
    rf"|(?P<sameart>같은\s?조){_FOLLOW}"
    rf"|(?P<samepara>같은\s?항){_FOLLOW}"
    rf"|(?P<sameitem>같은\s?호){_FOLLOW}"
    rf"|(?<![가-힣])(?P<thisart>이\s?조){_FOLLOW}"
    r"|(?P<prevpara>전항)"
    r"|(?P<art>제\s?(?P<art_n>\d+)\s?조(?:의\s?(?P<art_b>\d+))?)(?!\d)"
    r"|(?P<para>제\s?(?P<para_n>\d+)\s?항)"
    r"|(?P<item>제\s?(?P<item_n>\d+)\s?호(?:의\s?(?P<item_b>\d+))?)(?!\s?서식|\d)"
    rf"|(?:(?<=호)|(?<![가-힣]))(?P<sub>(?P<sub_v>[{_SUBITEM}])목)"
    r"|(?P<appx>별표\s?(?P<appx_n>\d+)(?:의\s?(?P<appx_b>\d+))?)"
)
_KINDS = ("lawname", "samelaw", "thislaw", "named", "abbr", "sameart", "samepara", "sameitem", "thisart", "prevpara", "art", "para", "item", "sub", "appx")
_LAW_KINDS = ("lawname", "samelaw", "thislaw", "named", "abbr")
_SAME_KINDS = ("sameart", "samepara", "sameitem")
_RANK = {"lawname": 0, "samelaw": 0, "thislaw": 0, "named": 0, "abbr": 0, "art": 1, "appx": 1, "sameart": 1, "thisart": 1,
         "para": 2, "samepara": 2, "prevpara": 2, "item": 3, "sameitem": 3, "sub": 4}
_PROMULGATION_RE = re.compile(r"(?:령|법률|고시|훈령|예규|공고|조례|지침)\s*$")
_DEF_PAREN_RE = re.compile(r"^\s*\(\s*이하\s*[“\"'][^”\"']{1,10}[”\"']\s*(?:이)?라\s*한다\s*\)\s*$")
_OF_RE = re.compile(r"^\s?의\s?$")  # "제338조제1항의 별표 11": 별표는 앞 위치의 법령 것이다
_LAWLIKE_RE = re.compile(r"(?:법|령|규칙|률|지침|기준|규정)$")
_CONNECTOR_RE = re.compile(r"^(?:[\s,ㆍ·]|및|또는|과|와|이나|부터|까지|내지|그리고)*$")
_ABBR_DEF_RE = re.compile(r"「([^「」]+)」\s*\(\s*이하\s*[“\"']([^”\"']{1,10})[”\"']\s*(?:이)?라\s*한다\s*\)")
_CUE_PREFIX = r"^(?:까지)?(?:의\s?규정)?(?:\s?각\s?(?:호|목))?(?:\s?(?:본문|단서|전단|후단))?(?:의\s?규정)?\s?"
_CUE_OVERRIDE_RE = re.compile(_CUE_PREFIX + r"(?:에도|에)\s?불구하고")
_CUE_MUTATIS_RE = re.compile(_CUE_PREFIX + r"(?:을|를)\s?(?:각각\s?)?준용")
_CUE_FOLLOW_RE = re.compile(_CUE_PREFIX + r"에\s?(?:따른|따라|따르|의한|의하여|의해)")
# 「」 없이 쓴 다른 법령 이름 바로 뒤 위치(건축법 제2조, 산업안전보건법 시행령 제5조): 어느 법령인지 확정하지 못해 버린다
#   방법·공법·명령처럼 법·령으로 끝나는 보통 낱말은 법령 이름이 아니다
_BARE_LAWNAME_RE = re.compile(r"(?<![가-힣])(?!(?:방법|공법|기법|용법|수법|명령|요령|지령)\s?$)[가-힣]+(?:법|령|규칙|규정|지침|고시)\s?$")


@dataclass(frozen=True)
class Loc:
    article: ArtNo | None = None
    paragraph: int | None = None
    item: ArtNo | None = None
    subitem: str | None = None
    appendix: ArtNo | None = None

    def label(self) -> str:
        """노드 ID 의 위치 부분(예: 제42조제2항제1호가목, 별표3의2). 법령 단위면 빈 문자열."""
        if self.appendix:
            n, b = self.appendix
            return f"별표{n}" + (f"의{b}" if b else "")
        out = ""
        if self.article:
            n, b = self.article
            out += f"제{n}조" + (f"의{b}" if b else "")
        if self.paragraph is not None:
            out += f"제{self.paragraph}항"
        if self.item:
            n, b = self.item
            out += f"제{n}호" + (f"의{b}" if b else "")
        if self.subitem:
            out += f"{self.subitem}목"
        return out

    def depth(self) -> int:
        levels = ((1, self.article or self.appendix), (2, self.paragraph), (3, self.item), (4, self.subitem))
        return max([0] + [r for r, v in levels if v is not None])


@dataclass(frozen=True)
class Ref:
    law: str  # 법령 정식 이름(대상 목록 밖이면 그 이름 그대로)
    loc: Loc
    start: int
    end: int
    form: str  # explicit | inherited | relative | range | law
    cue: str | None = None  # 따른 | 준용 | 불구하고
    surface: str = ""


@dataclass
class Context:
    """조 하나를 읽는 동안의 상태. 같은 조의 본문·항·호·목을 문서 순서로 넣는다."""

    law_name: str
    abbreviations: dict[str, str]
    article: ArtNo | None = None
    paragraph: int | None = None
    item: ArtNo | None = None
    last_ref: Ref | None = None  # 같은 조·같은 항·같은 호의 기준
    last_lawname: str | None = None  # 같은 법의 기준

    def at(self, *, paragraph: int | None = None, item: ArtNo | None = None) -> Context:
        self.paragraph, self.item = paragraph, item
        return self


@lru_cache(maxsize=64)
def _token_re(named: tuple[str, ...]) -> re.Pattern[str]:
    alt = "|".join(re.escape(n) for n in named) if named else r"(?!x)x"
    return re.compile(_TOKEN_SRC.replace("{named}", alt))


def mask(text: str) -> str:
    return _MASK_RE.sub(lambda m: " " * len(m.group(0)), text)


def abbreviations(text: str, defaults: tuple[tuple[str, str], ...] = ()) -> dict[str, str]:
    """본문의 "「X」(이하 “Y”라 한다)" 정의 → {Y: X}(X 가 법령 이름일 때만, 예: “안전보건규칙”). 정의가 없는 약칭은 defaults."""
    out = dict(defaults)
    for m in _ABBR_DEF_RE.finditer(text):
        name, short = " ".join(m.group(1).split()), m.group(2).strip()
        if _LAWLIKE_RE.search(name) and (short in ("법", "영", "규칙") or _LAWLIKE_RE.search(short)):
            out[short] = name
    return out


@dataclass
class _Tok:
    kind: str
    rank: int
    start: int
    end: int
    m: re.Match[str]
    blocked: bool = False


@dataclass
class _Phrase:
    toks: list[_Tok] = field(default_factory=list)

    @property
    def start(self) -> int:
        return self.toks[0].start

    @property
    def end(self) -> int:
        return self.toks[-1].end

    def law_tok(self) -> _Tok | None:
        return next((t for t in self.toks if t.kind in _LAW_KINDS), None)

    def loc_toks(self) -> list[_Tok]:
        return [t for t in self.toks if t.kind not in _LAW_KINDS]


def _tokens(text: str, named: tuple[str, ...] = ()) -> list[_Tok]:
    out = []
    for m in _token_re(named).finditer(text):
        kind = next(k for k in _KINDS if m.group(k))
        if kind == "item" and _PROMULGATION_RE.search(text[max(0, m.start() - 8):m.start()]):
            continue
        blocked = kind in ("art", "appx", "para", "item") and bool(_BARE_LAWNAME_RE.search(text[max(0, m.start() - 30):m.start()]))
        out.append(_Tok(kind, _RANK[kind], m.start(), m.end(), m, blocked))
    return out


def _phrases(text: str, toks: list[_Tok]) -> list[_Phrase]:
    """띄어쓰기 한 칸 이하로 붙고 단계가 내려가는 토큰(법령 → 조 → 항 → 호 → 목)을 한 위치로 묶는다."""
    out: list[_Phrase] = []
    for t in toks:
        if out:
            last = out[-1].toks[-1]
            gap = text[last.end:t.start]
            # 「X」(이하 “Y”라 한다) 제11조: 정의 괄호를 사이에 둔 위치도 X 의 것이다
            defines = last.kind == "lawname" and _DEF_PAREN_RE.match(gap) is not None
            if ((len(gap) <= 2 and not gap.strip()) or defines) and t.rank > last.rank:
                out[-1].toks.append(t)
                continue
        out.append(_Phrase([t]))
    return out


def _num(m: re.Match[str], g: str) -> int:
    return int(m.group(g)) if m.group(g) else 0


def _resolve_law(tok: _Tok | None, ctx: Context) -> str | None:
    if tok is None:
        return None
    m = tok.m
    if tok.kind == "lawname":
        return " ".join(m.group("lawname_v").split())
    if tok.kind == "thislaw":
        return ctx.law_name
    if tok.kind in ("abbr", "named"):
        return ctx.abbreviations.get(m.group(tok.kind))
    if tok.kind == "samelaw":
        base = ctx.last_lawname
        if base is None:
            return None
        sfx = (m.group("samelaw_sfx") or "").strip()
        if sfx:
            base = re.sub(r"\s?(시행령|시행규칙)$", "", base) + " " + sfx
        return base
    return None


def _build_loc(toks: list[_Tok], base: Loc, rel: Ref | None, ctx: Context) -> tuple[Loc, str]:
    """위치 토큰 → Loc. 맨 위 토큰보다 높은 단계는 base(물려받은 위치나 지금 조·항)에서 채운다."""
    art, para, item, sub, appx = base.article, base.paragraph, base.item, base.subitem, base.appendix
    top = toks[0].rank
    if top <= 1:
        art = para = item = sub = appx = None
    elif top == 2:
        para = item = sub = appx = None
    elif top == 3:
        item = sub = None
    else:
        sub = None
    form = "explicit"
    for t in toks:
        m = t.m
        if t.kind == "art":
            art = (int(m.group("art_n")), _num(m, "art_b"))
        elif t.kind == "appx":
            appx = (int(m.group("appx_n")), _num(m, "appx_b"))
        elif t.kind == "para":
            para = int(m.group("para_n"))
        elif t.kind == "item":
            item = (int(m.group("item_n")), _num(m, "item_b"))
        elif t.kind == "sub":
            sub = m.group("sub_v")
        elif t.kind == "thisart":
            art, form = ctx.article, "relative"
        elif t.kind == "prevpara":
            art, form = ctx.article, "relative"
            para = ctx.paragraph - 1 if ctx.paragraph and ctx.paragraph > 1 else None
        elif t.kind in _SAME_KINDS and rel is not None:
            form = "relative"
            art = rel.loc.article
            if t.kind in ("samepara", "sameitem"):
                para = rel.loc.paragraph
            if t.kind == "sameitem":
                item = rel.loc.item
    return Loc(art, para, item, sub, appx), form


def extract(text: str, ctx: Context) -> list[Ref]:
    """본문 한 덩어리(조 본문·항·호·목 한 줄) → 참조 목록. ctx 의 같은 조·같은 법 기준을 갱신한다."""
    masked = mask(text)
    refs: list[Ref] = []
    chain: list[Ref] = []  # 이음말로 이어진 묶음(관계 단서를 함께 받는다)
    prev: tuple[_Phrase, Ref | None] | None = None  # Ref 가 None 이면 버린 위치(이어진 위치도 버린다)

    def close_chain(end: int) -> None:
        if not chain:
            return
        after = masked[end:end + 30]
        cue = None
        if _CUE_OVERRIDE_RE.match(after):
            cue = "불구하고"
        elif _CUE_MUTATIS_RE.match(after):
            cue = "준용"
        elif _CUE_FOLLOW_RE.match(after):
            cue = "따른"
        refs.extend(Ref(r.law, r.loc, r.start, r.end, r.form, cue, r.surface) for r in chain)
        chain.clear()

    named = tuple(sorted((k for k in ctx.abbreviations if k not in ("법", "영", "규칙")), key=len, reverse=True))
    for ph in _phrases(masked, _tokens(masked, named)):
        chained = prev is not None and _CONNECTOR_RE.match(masked[prev[0].end:ph.start]) is not None
        if prev is not None and not chained:
            close_chain(prev[0].end)
        if ph.toks[0].blocked or (chained and prev is not None and prev[1] is None):
            prev = (ph, None)
            continue
        law_tok, loc_toks = ph.law_tok(), ph.loc_toks()
        explicit_law = _resolve_law(law_tok, ctx)
        if law_tok is not None and law_tok.kind in ("lawname", "named", "abbr") and explicit_law:
            ctx.last_lawname = explicit_law
        if law_tok is not None and explicit_law is None:  # 기준 없는 같은 법·정의 없는 약칭
            prev = None
            continue
        if not loc_toks:
            if law_tok is not None and law_tok.kind == "lawname" and explicit_law:
                r = Ref(explicit_law, Loc(), ph.start, ph.end, "law", surface=masked[ph.start:ph.end])
                chain.append(r)
                prev = (ph, r)
            continue
        uses_same = any(t.kind in _SAME_KINDS for t in loc_toks)
        rel = ctx.last_ref if uses_same else None
        if uses_same and rel is None:  # 앞에 가리킨 곳이 없는 같은 조·항·호
            prev = None
            continue
        if explicit_law is not None:
            law, base, inherited = explicit_law, Loc(), False
        elif loc_toks[0].kind == "appx" and prev is not None and prev[1] is not None and _OF_RE.match(masked[prev[0].end:ph.start]):
            law, base, inherited = prev[1].law, Loc(), True
        elif chained and prev is not None and prev[1] is not None:
            law, base, inherited = prev[1].law, prev[1].loc, True
        else:
            law, base, inherited = ctx.law_name, Loc(ctx.article, ctx.paragraph, ctx.item), False
        if rel is not None:
            law = rel.law
        loc, form = _build_loc(loc_toks, base, rel, ctx)
        if form == "explicit" and inherited:
            form = "inherited"
        if loc.depth() == 0 or (loc.article is None and loc.appendix is None):  # 조 없는 항·호(별표 안 번호 등)는 확정할 수 없다
            prev = None
            continue
        r = Ref(law, loc, ph.start, ph.end, form, surface=masked[ph.start:ph.end])
        if chained and prev is not None and prev[1] is not None and "부터" in masked[prev[0].end:ph.start]:
            chain.extend(_range(prev[1], r))
        chain.append(r)
        ctx.last_ref = r
        prev = (ph, r)
    if prev is not None:
        close_chain(prev[0].end)
    return _dedupe(refs)


def _range(a: Ref, b: Ref) -> list[Ref]:
    """`A부터 B까지` 사이 번호(가지 번호 조문은 build 가 번호 범위로 따로 넣는다)."""
    if a.law != b.law:
        return []
    la, lb = a.loc, b.loc
    out = []
    if la.article and lb.article and la.paragraph is None and lb.paragraph is None and la.depth() == lb.depth() == 1:
        out = [Loc(article=(n, 0)) for n in range(la.article[0] + 1, lb.article[0])]
    elif la.item and lb.item and la.subitem is None and lb.subitem is None and (la.article, la.paragraph) == (lb.article, lb.paragraph):
        out = [Loc(la.article, la.paragraph, (n, 0)) for n in range(la.item[0] + 1, lb.item[0])]
    elif la.paragraph is not None and lb.paragraph is not None and la.item is None and lb.item is None and la.article == lb.article:
        out = [Loc(la.article, n) for n in range(la.paragraph + 1, lb.paragraph)]
    return [Ref(a.law, loc, a.start, b.end, "range") for loc in out[:50]]  # 잘못 읽은 범위가 수백 개 엣지를 만들지 않게


def _dedupe(refs: list[Ref]) -> list[Ref]:
    seen: set[tuple[str, Loc]] = set()
    out = []
    for r in refs:
        if (r.law, r.loc) not in seen:
            seen.add((r.law, r.loc))
            out.append(r)
    return out
