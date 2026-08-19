"""소재와 브랜드가 **자연스럽게 닿는가**를 잰다(2026-08-19).

왜 이 판정이 필요한가. 소재와 브랜드를 함께 고를 수 있게 되면서, 모든 트렌드에 브랜드를
얹을 수 있게 됐다 — 그런데 얹을 수 있다는 것과 얹어야 한다는 것은 다르다. 브랜드를 쓸
이유가 없는 소재에 억지로 붙이면 남는 것은 광고 문장뿐이고, 그런 글이 쌓이면 블로그
전체가 홍보 채널로 읽힌다. 검색해서 들어온 독자를 브랜드로 데려오려던 목적과 정반대다.

그래서 세 등급으로 나눈다:

``A`` (DIRECT)      소재 자체가 브랜드 기준표의 상황과 곧바로 닿는다. 적극적으로 쓴다.
                    예: 소재 '다이어트' × 기준표 "무엇을 알아보고 싶을 때 → 자료 조사".
``B`` (SITUATIONAL) 소재는 직접 닿지 않지만, 그 소재를 다루다 보면 생기는 상황이 닿는다.
                    쓸 수 있다 — 다만 상황을 먼저 만들어야 자연스럽다.
                    예: 소재 '빼빼로' × "성분·칼로리를 확인할 때 → 자료 조사".
``C`` (FORCED)      닿는 곳이 없다. **이 조합으로는 쓰지 않는 편이 낫다.**

판정 근거는 브랜드 자료의 **기준표**(``BrandProfile.use_cases``)다. 기준표가 비어 있으면
서술 칸(소개·핵심 기능)으로 대신 재되 A는 주지 않는다 — 줄글에서 우연히 겹친 낱말은
"이 상황에서 이 기능을 쓴다"는 근거가 못 된다.

대조 방식은 트렌드 근거 집계와 같다(``llm/trends/text.mentions_keyword``): 공백·조사·
기호를 지워 붙인 뒤 낱말이 **모두** 들어 있는지 본다. 그래서 '다이어트'는 '다이어트
간식에서'에 닿고, '학교 준비물'은 '일본 여행 준비물'에 닿지 않는다 — 여러 낱말로 적은
검색어는 그 낱말이 다 있을 때만 닿는다. 형태소 분석기(kiwi)를
쓰지 않는 이유는 무게다 — 브랜드 모듈은 저장·조회 경로에 있고, 여기 하나 때문에 그 경로
전체가 분석기를 싣게 할 이유가 없다. 판정에 필요한 것은 어간 복원이 아니라 포함 여부다.
"""

from dataclasses import dataclass
import re

from app.shared import (
    # 등급 글자는 ``shared``에 있다 — 글 입력 검증도 같은 값을 보는데, 여기 두면 그쪽이
    # 이 모듈을 import해야 하고 그것은 순환이 된다. 판정 로직만 여기 있다.
    BRAND_FIT_DIRECT,
    BRAND_FIT_FORCED,
    BRAND_FIT_GRADES,
    BRAND_FIT_SITUATIONAL,
    BrandProfile,
    BrandUseCase,
)
from app.shared.format import with_particle

__all__ = [
    "BRAND_FIT_DIRECT",
    "BRAND_FIT_FORCED",
    "BRAND_FIT_GRADES",
    "BRAND_FIT_SITUATIONAL",
    "BrandFit",
    "BrandFitMatch",
    "brand_use_case_lines",
    "evaluate_brand_fit",
    "use_case_brief",
]

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")

#: 어디에나 나오는 낱말. 이것만 겹쳐서는 닿았다고 볼 수 없다.
#:
#: 짧은 목록으로 둔다. 길게 만들수록 진짜 신호까지 지우고, 브랜드마다 무엇이 흔한지가
#: 다르다 — 걸러야 할 것이 많다면 그것은 기준표의 검색어를 더 구체적으로 적을 일이다.
_TOO_COMMON = frozenset(
    {
        "정보",
        "내용",
        "사용",
        "이용",
        "확인",
        "방법",
        "서비스",
        "기능",
        "사람",
        "생각",
        "경우",
        "때문",
        "우리",
        "그것",
        "관련",
        "다양",
        "필요",
    }
)

#: 프롬프트에 싣는 기준표 줄 수. 전부 실으면 모델이 아무 줄이나 고르고, 브랜드 자료가
#: 커질수록 매 편이 비싸진다. 닿는 것부터 위에서 자른다.
MAX_PROMPT_USE_CASES = 4


def _compact(value: str) -> str:
    """공백·기호·조사 경계를 지워 한 덩어리로. 대조는 이 위에서만 한다."""
    return re.sub(r"[^0-9a-z가-힣]", "", (value or "").lower())


def _terms(value: str) -> list[str]:
    """대조에 쓸 낱말들. 너무 흔하거나 한 글자인 것은 버린다."""
    words: list[str] = []
    for word in _TOKEN.findall((value or "").lower()):
        term = _compact(word)
        if len(term) < 2 or term in _TOO_COMMON:
            continue
        if term.isascii() and len(term) < 3:
            # 영문 두 글자는 조사·약어 조각이 대부분이라 오탐만 만든다.
            continue
        if term not in words:
            words.append(term)
    return words


def _mentions(terms: list[str], haystack: str) -> bool:
    """그 낱말들이 **모두** 들어 있는가. 하나라도 빠지면 닿은 것이 아니다."""
    return bool(terms) and all(term in haystack for term in terms)


def _hit(needle: str, haystack_compact: str) -> bool:
    """``needle``(기준표의 검색어·상황·기능명)이 대상 글과 닿는가.

    규칙은 하나다: **낱말이 모두 들어 있으면 닿은 것이다.** 트렌드 근거 집계가 쓰는 규칙과
    같고(``llm/trends/text.mentions_keyword``), 대상 글은 공백·조사·기호를 지워 붙여 놓고
    보므로 '다이어트'는 '다이어트 간식에서'에도 닿는다.

    한때 반대 방향도 봤다 — 소재의 낱말 하나가 검색어 안에 들어 있으면 닿은 것으로.
    소재 '빼빼로'를 검색어 '빼빼로 신제품'에 닿게 하려던 것인데, 그 헐거움이 엉뚱한 곳을
    함께 열었다: '일본 여행 준비물'의 '준비물'이 '학교 준비물'에 들어맞아 여행 글에
    알림장 정리가 붙었다(실측). 낱말 하나로 판정하면 어디든 닿는다.

    그래서 여러 낱말로 적은 검색어는 **그 낱말이 다 있을 때만** 닿는다. 넓게 닿게 하고
    싶으면 표에 '빼빼로'라고 짧게 적으면 된다 — 어느 쪽을 원하는지는 표를 쓰는 사람이
    안다.
    """
    needle_terms = _terms(needle)
    return _mentions(needle_terms, haystack_compact)


@dataclass(frozen=True)
class BrandFitMatch:
    """닿은 기준표 한 줄과, 무엇이 닿았는지."""

    situation: str
    feature: str
    #: 어떤 낱말·상황이 닿았는가. 사용자에게 "왜 이 등급인지"를 보여 주는 값이다.
    matched: tuple[str, ...]
    #: 소재 자체에서 닿았는가(True) 아니면 넓은 맥락(분야·목적)에서만 닿았는가(False).
    direct: bool

    def to_wire(self) -> dict:
        return {
            "situation": self.situation,
            "feature": self.feature,
            "matched": list(self.matched),
            "direct": self.direct,
        }


@dataclass(frozen=True)
class BrandFit:
    """판정 결과. 등급과, 그렇게 본 이유와, 쓸 만한 기능들."""

    grade: str
    matches: tuple[BrandFitMatch, ...]
    reason: str

    @property
    def usable(self) -> bool:
        """이 조합으로 글을 써도 되는가. C는 억지 연결이다."""
        return self.grade != BRAND_FIT_FORCED

    @property
    def features(self) -> tuple[str, ...]:
        """닿은 기능 이름들 — 중복 없이, 닿은 순서대로."""
        names: list[str] = []
        for match in self.matches:
            if match.feature not in names:
                names.append(match.feature)
        return tuple(names)

    def to_wire(self) -> dict:
        return {
            "grade": self.grade,
            "reason": self.reason,
            "features": list(self.features),
            "matches": [match.to_wire() for match in self.matches],
        }


def _fallback_matches(profile: BrandProfile, direct_text: str) -> tuple[BrandFitMatch, ...]:
    """기준표가 없는 브랜드를, 서술 칸(소개·핵심 기능)으로 재 본다.

    여기서 나온 것은 **B까지만** 된다. 줄글에서 우연히 겹친 낱말은 "이 상황에서 이 기능을
    쓴다"는 근거가 못 되기 때문이다. 등급을 매기는 자리가 아니라, 아예 닿을 곳이 없는
    브랜드(C)와 가르는 자리다.
    """
    haystack = _compact(direct_text)
    haystack_terms = _terms(direct_text)
    if not haystack_terms:
        return ()

    matched: list[str] = []
    for line in re.split(r"[\n·,/]+", f"{profile.features or ''}\n{profile.description or ''}"):
        for term in _terms(line):
            if len(term) < 2 or term in matched:
                continue
            if term in haystack or any(word in term for word in haystack_terms):
                matched.append(term)
    if not matched:
        return ()
    return (
        BrandFitMatch(
            situation="브랜드 소개·핵심 기능에서 닿는 낱말",
            # 기능 이름을 지어내지 않는다. 기준표가 없으면 **어느 기능인지 모르는 것**이
            # 사실이고, 프롬프트가 그 자리를 브랜드 자료로 채운다.
            feature=profile.name,
            matched=tuple(matched[:5]),
            direct=False,
        ),
    )


def evaluate_brand_fit(
    profile: BrandProfile | None,
    topic: str,
    *,
    context: list[str] | None = None,
) -> BrandFit:
    """소재 × 브랜드의 결합 가능성을 A·B·C로 판정한다.

    ``topic``     사용자가 적은 소재. **A는 여기서만 나온다** — 소재가 곧 독자가 검색해서
                  들어온 말이고, 거기서 닿아야 "바로 연결 가능"이다.
    ``context``   소재 분야·트렌드 키워드·글 목적처럼 넓은 맥락. 여기서만 닿으면 B다 —
                  쓸 수 있지만 상황을 먼저 만들어야 자연스럽다.

    브랜드가 없으면 판정할 것도 없다(A로 둔다) — 브랜드를 안 쓰는 글은 이 규칙과 무관하고,
    호출부가 등급만 보고 막을 수 있어야 한다.
    """
    if profile is None:
        return BrandFit(BRAND_FIT_DIRECT, (), "브랜드를 쓰지 않는 글입니다.")

    direct_text = (topic or "").strip()
    context_text = " ".join(part for part in (context or []) if part)

    direct_compact = _compact(direct_text)
    context_compact = _compact(f"{direct_text} {context_text}")

    matches: list[BrandFitMatch] = []
    for case in profile.use_cases:
        needles = list(case.keywords) or [case.situation]
        # 기능 이름 자체도 닿을 수 있다 — 소재가 '번역'이면 번역 기능이 곧 그 소재다.
        needles = [*needles, case.feature]

        hit_direct = tuple(n for n in needles if _hit(n, direct_compact))
        if hit_direct:
            matches.append(
                BrandFitMatch(case.situation, case.feature, hit_direct[:5], direct=True)
            )
            continue
        hit_context = tuple(n for n in needles if _hit(n, context_compact))
        if hit_context:
            matches.append(
                BrandFitMatch(case.situation, case.feature, hit_context[:5], direct=False)
            )

    if not matches and not profile.use_cases:
        matches = list(_fallback_matches(profile, f"{direct_text} {context_text}"))

    if not matches:
        return BrandFit(
            BRAND_FIT_FORCED,
            (),
            f"'{direct_text or profile.name}'와(과) "
            + with_particle(profile.name, "을", "를")
            + " 이을 상황이 브랜드 자료에 없습니다."
            " 이 조합으로 쓰면 브랜드 문장이 겉돕니다.",
        )

    # 소재에서 닿은 줄을 앞에 둔다 — 프롬프트도, 화면도 위에서부터 읽는다.
    matches.sort(key=lambda match: not match.direct)
    if matches[0].direct:
        return BrandFit(
            BRAND_FIT_DIRECT,
            tuple(matches),
            f"소재가 '{matches[0].situation}' 상황과 곧바로 닿습니다 — "
            + with_particle(matches[0].feature, "을", "를")
            + " 쓰는 장면을 그대로 쓸 수 있습니다.",
        )
    return BrandFit(
        BRAND_FIT_SITUATIONAL,
        tuple(matches),
        f"소재 자체는 아니지만, 다루다 보면 '{matches[0].situation}' 상황이 생깁니다 — "
        + "그 장면을 먼저 만든 뒤 "
        + with_particle(matches[0].feature, "을", "를")
        + " 씁니다.",
    )


def brand_use_case_lines(profile: BrandProfile, fit: BrandFit) -> list[str]:
    """프롬프트에 실을 기준표 줄들. **닿은 것부터, 몇 줄만.**

    전부 실으면 모델이 아무 줄이나 고른다(실제로 자주 같은 기능만 반복됐다). 닿은 줄이
    하나도 없으면 빈 목록이다 — 그때는 억지로 기능을 정해 주지 않고, 프롬프트가 브랜드
    자료 안에서 확인되는 것만 쓰라고 말한다.
    """
    # 소재에서 곧바로 닿은 줄이 하나라도 있으면 **그것만** 싣는다. 넓은 맥락에서만 닿은
    # 줄(분야 이름이 겹친 정도)을 섞으면 소재와 상관없는 기능이 후보로 올라간다 —
    # 소재 '추석 선물 세트'에 분야가 '제품·쇼핑·리뷰'라는 이유로 리뷰 기능이 붙는 식이다.
    matches = [m for m in fit.matches if m.direct] or list(fit.matches)

    lines: list[str] = []
    for match in matches[:MAX_PROMPT_USE_CASES]:
        if match.feature == profile.name:
            # 기준표가 없어 이름으로 대신한 줄이다. 기능명을 만들어 주지 않는다.
            continue
        lines.append(f"- {match.situation} → {match.feature}")
    return lines


def use_case_brief(cases: list[BrandUseCase]) -> str:
    """기준표 전체를 브랜드 자료 문장으로. ``brand_brief``가 이어 붙인다."""
    if not cases:
        return ""
    return "이런 상황이면 이 기능:\n" + "\n".join(
        f"- {case.situation} → {case.feature}"
        + (f" (닿는 소재: {', '.join(case.keywords)})" if case.keywords else "")
        for case in cases
    )
