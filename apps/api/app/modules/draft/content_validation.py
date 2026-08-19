"""원고 생성 후의 SEO·콘텐츠 품질 검증.

quality.check_draft가 '원고를 못 쓰게 만드는 치명 결함(짧은 본문·낚시·반복)'을 잡는다면,
이 모듈은 그 위에 **검색 노출과 약속 이행**을 확인하는 독립 검사들을 얹는다. 두 부류를
명확히 나눈다.

- **FAIL(SEO 필수)**: Primary Keyword가 확정 제목에 없음. 제목 계약 자체가 깨진 경우만
  draft 서비스가 기존 재생성 루프로 다시 만든다.
- **WARN(품질)**: Primary가 첫 문단에 없음·Secondary 미사용·H2 개수·Source 미활용·
  제목 약속 누락. 로그만 남기고 원고를 반려하지 않는다(§14). 키워드 한 번을 옮기기 위해
  완성 원고 전체를 다시 쓰는 비용과 내용 변화를 피한다.

각 검사는 하나의 책임만 지는 독립 함수이고, 서로의 결과에 영향을 주지 않는다. 검사 내부
오류는 콘텐츠 FAIL과 구분해 SKIPPED(VALIDATOR_ERROR)로 처리한다 — 검증이 원고 생성을
무너뜨리면 안 된다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .duplication import (
    COMPARE_LIMIT,
    PostDigest,
    compare,
    extract_headings,
    first_paragraph,
)
from app.shared import (
    WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL,
    FinalPost,
    PlannedVisual,
    ReferenceEvidenceProfile,
    ReferenceMaterial,
    ReferenceMaterialType,
    SearchSource,
    SeoKeywordPlan,
)
from app.shared.format import now_iso as _now

from app.llm.category_playbooks import required_facts_for
from app.llm.keyword_naturalization import (
    entity_aliases,
    is_entity_juxtaposition,
    keyword_meaning_covered,
    raw_keyword_misuse,
)

from .quality import TEMPLATE_PHRASES, normalize_for_match
from .visual_policy import (
    CHART_TYPES,
    is_redundant,
    is_vague_reason,
    purpose_policy,
)

# 로그·통계에서 어떤 규격으로 검사했는지 구분하는 버전. 검사 항목·판정 기준이 바뀌면 올린다.
# v3: 시각자료 목적 적합성·근거 충분성·중복, 참고자료 반영, 경험 주장 강화, 템플릿 반복.
# v4: 원본 검색어 문법 오용, 영상 콘텐츠의 핵심 포맷 반영·보조 장면 과대 강조, 시청 경험
#     조작, 공식 썸네일 사용. SEO Primary 판정도 연속 문자열에서 의미 기반으로 바뀌었다.
# v5: 카테고리 적합성, 실존 대상 이미지(영상 밖의 상품·인물·장소·작품), 제목의 체험 약속,
#     정확성이 중요한 주제의 단정.
CONTENT_VALIDATION_VERSION = "content-validation@v5"

# 검사 상태. 기존 프로젝트에는 per-check 상태 체계가 없어 새로 도입한다(내부/로그 전용 —
# API 계약은 건드리지 않는다).
PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIPPED = "SKIPPED"

# 권장 H2 개수 범위(§8). 벗어나도 반려하지 않고 WARN만 남긴다.
H2_MIN = 3
H2_MAX = 6

# Source 활용 판정 문턱(휴리스틱). 출처 제목의 의미 토큰 중 이 비율 이상이 본문에 등장하면
# '활용됨'으로 본다. WARN 전용이라 정밀 의미 판정 대신 저비용 휴리스틱을 쓴다.
_SOURCE_USED_TOKEN_RATIO = 0.4
# 이보다 짧은 메모·요약은 활용 여부를 판정할 근거가 부족해 검사 대상에서 제외한다.
_MIN_SOURCE_TEXT_CHARS = 10

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")


@dataclass
class CheckResult:
    """검사 하나의 결과. 공통 구조(check/status/message/details)로 통합한다(§16)."""

    check: str
    status: str
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        """이 검사가 원고를 반려시켰는가. FAIL만 반려로 이어진다(WARN은 절대 반려 안 함)."""
        return self.status == FAIL


@dataclass
class ValidationResult:
    """모든 검사 결과의 통합(§11). status는 기존 boolean이 아니라 요약 라벨이다."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def status(self) -> str:
        statuses = {c.status for c in self.checks}
        if FAIL in statuses:
            return FAIL
        if WARN in statuses:
            return "PASS_WITH_WARNINGS"
        if statuses and statuses <= {SKIPPED}:
            return SKIPPED
        return PASS

    @property
    def has_fail(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    def fail_messages(self) -> list[str]:
        """반려 사유 문자열. 재생성 프롬프트(revision_notes)에 실려 모델이 그 문제만 고치게
        하므로, 어떤 키워드가 문제인지 함께 담는다."""
        messages: list[str] = []
        for check in self.checks:
            if check.status != FAIL or not check.message:
                continue
            keyword = check.details.get("keyword")
            messages.append(
                f"{check.message} (Primary Keyword: {keyword})" if keyword else check.message
            )
        return messages

    def counts(self) -> dict[str, int]:
        counts = {PASS: 0, WARN: 0, FAIL: 0, SKIPPED: 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return counts


# --- 본문 파싱 헬퍼 ---------------------------------------------------------

_HEADING_LINE = re.compile(r"^(#{1,6})\s+(\S.*)$")
_MARKER_PREFIX = ("[[IMAGE", "[[VISUAL", "[[STICKER")


def count_h2(markdown: str) -> int:
    """마크다운 본문의 H2(`## 소제목`) 개수. H1·H3 이하, 코드블록 내부, 인용문 안의 `##`,
    빈 H2, 문장 중간의 `##`는 세지 않는다(§8)."""
    count = 0
    in_code = False
    for raw in (markdown or "").split("\n"):
        line = raw.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_code = not in_code
            continue
        if in_code:
            continue
        match = _HEADING_LINE.match(line)
        if match and len(match.group(1)) == 2:
            count += 1
    return count


def first_substantive_paragraph(markdown: str) -> str:
    """첫 번째 실질적인 본문 문단(§5). 제목(H1)·소제목·이미지/시각자료 마커·인용문만으로 된
    문단·코드블록·빈 문단은 건너뛰고, 처음 나오는 실제 설명 문단을 돌려준다."""
    text = (markdown or "").strip()
    if not text:
        return ""
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper.startswith(_MARKER_PREFIX):
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            continue
        lines = [line.strip() for line in stripped.split("\n") if line.strip()]
        if not lines:
            continue
        # 모든 줄이 소제목이면 문단이 아니다.
        if all(_HEADING_LINE.match(line) for line in lines):
            continue
        # 인용문만으로 된 문단 제외.
        if all(line.startswith(">") for line in lines):
            continue
        # 소제목 줄은 걷어내고 실제 설명만 남긴다(소제목 바로 뒤 첫 문단이 같은 블록일 때).
        prose = [line for line in lines if not _HEADING_LINE.match(line) and not line.startswith(">")]
        prose = [line for line in prose if not line.upper().startswith(_MARKER_PREFIX)]
        if prose:
            return " ".join(prose)
    return ""


def _contains_keyword(haystack: str, keyword: str) -> bool:
    """정규화(조사·띄어쓰기·대소문자 무시) 후 포함 여부. 동의어·유사표현은 인정하지 않는다."""
    keyword_norm = normalize_for_match(keyword)
    if not keyword_norm:
        return False
    return keyword_norm in normalize_for_match(haystack)


def _entity_alias_table(entity) -> dict[str, tuple[str, ...]]:
    """이 글에서 인정할 축약명 ↔ 공식 이름. 엔티티 정보가 없으면 빈 표."""
    if entity is None:
        return {}
    return entity_aliases([entity.canonical_name, *entity.person_names])


def _keyword_satisfied(text: str, keyword: str, entity) -> bool:
    """검색 의도가 이 문자열에 담겼는가.

    예전에는 정확한 연속 문자열만 인정했다. 그 규칙은 키워드가 하나의 고유명사일 때는
    맞지만, 검색어 조합('사람이름 프로그램명')에서는 **문법적으로 옳은 문장을 반려**한다:
    '이창섭이 대학 수업을 직접 듣는 유튜브 웹예능 전과자'에는 그 연속 문자열이 없지만
    검색 의도는 정확히 반영돼 있다. 그래서 한 문장 안에서 핵심 토큰이 모두 확인되면
    통과시킨다 — 토큰이 글 전체에 흩어져 있는 경우는 여전히 통과하지 못한다.
    """
    if _contains_keyword(text, keyword):
        return True
    return keyword_meaning_covered(text, keyword, _entity_alias_table(entity))


# --- SEO 검사(§6) -----------------------------------------------------------


def validate_primary_keyword_in_title(
    post: FinalPost,
    plan: SeoKeywordPlan | None,
    title_locked: bool = False,
    entity=None,
) -> CheckResult:
    """Primary Keyword가 제목에 있는가. 없으면 FAIL(§6.1).

    단, 제목이 잠긴 글(사용자가 M2에서 고른 제목·확정된 제목 계획)은 예외다. FAIL은
    '다시 생성하면 고쳐진다'는 뜻인데, 제목이 잠겨 있으면 원고를 몇 번 다시 써도 제목은
    같은 값이라 같은 이유로 계속 반려된다 — 사용자는 결과물 없이 막히고 매 시도마다 원고
    LLM이 통째로 한 번 더 돈다. 이 경우 키워드 쪽을 제목에 맞춰야 하고(생성 전
    parsing.keyword_inside_title이 그렇게 한다), 그래도 어긋난 것이 남으면 신호로만
    수집한다(§14).
    """
    check = "seo_primary_in_title"
    if plan is None or not plan.primary.strip():
        return CheckResult(check, SKIPPED, "SEO 키워드 계획이 없어 검사를 건너뜁니다.")
    if _keyword_satisfied(post.title, plan.primary, entity):
        return CheckResult(check, PASS, "", {"keyword": plan.primary})
    if title_locked:
        return CheckResult(
            check,
            WARN,
            "Primary Keyword가 제목에 없지만, 확정된 제목이라 원고 재생성으로 고칠 수 없습니다.",
            {"keyword": plan.primary, "titleLocked": True},
        )
    return CheckResult(
        check,
        FAIL,
        "Primary Keyword가 제목에 포함되지 않았습니다.",
        {"keyword": plan.primary},
    )


def validate_primary_keyword_in_first_paragraph(
    post: FinalPost, plan: SeoKeywordPlan | None, entity=None
) -> CheckResult:
    """Primary Keyword가 첫 실질 본문 문단에 있는가. 없으면 WARN(전체 재작성은 안 함).

    제목 검사와 같은 의미 기반 판정을 쓴다 — 연속 문자열이 아니어도 한 문장 안에서 핵심
    엔티티가 모두 확인되면 검색 의도가 담긴 것이다.
    """
    check = "seo_primary_in_first_paragraph"
    if plan is None or not plan.primary.strip():
        return CheckResult(check, SKIPPED, "SEO 키워드 계획이 없어 검사를 건너뜁니다.")
    paragraph = first_substantive_paragraph(post.markdown_content or post.body or "")
    if not paragraph:
        return CheckResult(
            check, SKIPPED, "첫 번째 실질적인 본문 문단을 찾지 못해 검사를 건너뜁니다."
        )
    if _keyword_satisfied(paragraph, plan.primary, entity):
        return CheckResult(check, PASS, "", {"keyword": plan.primary})
    return CheckResult(
        check,
        WARN,
        "Primary Keyword가 첫 번째 본문 문단에 포함되지 않았습니다.",
        {"keyword": plan.primary},
    )


def validate_secondary_keyword_usage(post: FinalPost, plan: SeoKeywordPlan | None) -> CheckResult:
    """Secondary Keyword 사용 여부만 확인한다(§6.3). 미사용이 있어도 WARN만 — 절대 반려하지
    않는다. Secondary가 없으면 SKIPPED."""
    check = "seo_secondary_usage"
    if plan is None or not plan.secondary:
        return CheckResult(check, SKIPPED, "Secondary Keyword가 없어 검사를 건너뜁니다.")
    body = f"{post.title}\n{post.body or ''}"
    unused = [kw for kw in plan.secondary if not _contains_keyword(body, kw)]
    if not unused:
        return CheckResult(check, PASS, "", {"total": len(plan.secondary)})
    return CheckResult(
        check,
        WARN,
        "일부 Secondary Keyword가 본문에서 사용되지 않았습니다.",
        {"unusedKeywords": unused, "total": len(plan.secondary)},
    )


# --- 품질 검사(§8~§10) ------------------------------------------------------


def validate_h2_count(post: FinalPost) -> CheckResult:
    """본문 H2 소제목 개수가 3~6개인지(§8). 벗어나면 WARN(반려하지 않는다)."""
    check = "h2_count"
    markdown = post.markdown_content or ""
    actual = count_h2(markdown)
    details = {"actualCount": actual, "recommendedRange": {"min": H2_MIN, "max": H2_MAX}}
    if H2_MIN <= actual <= H2_MAX:
        return CheckResult(check, PASS, "", details)
    return CheckResult(check, WARN, "H2 소제목 권장 개수를 벗어났습니다.", details)


def _significant_tokens(text: str, min_len: int = 2) -> list[str]:
    seen: dict[str, None] = {}
    for token in _TOKEN.findall((text or "").lower()):
        if len(token) >= min_len:
            seen.setdefault(token, None)
    return list(seen)


def _source_used(title: str, snippet: str, data_values: list[str], body_lower: str) -> bool:
    """출처가 본문에 의미 있게 반영됐는지 판정(휴리스틱, WARN 전용).

    ① 출처의 실측 수치가 본문에 등장하면 활용된 것으로 본다.
    ② 아니면 출처 제목(없으면 요약)의 의미 토큰 중 일정 비율 이상이 본문에 등장하는지 본다.
    URL 문자열만 넣거나 제목만 언급한 경우는 토큰 비율이 낮아 미활용으로 잡힌다."""
    for value in data_values:
        if value and value in body_lower:
            return True
    tokens = _significant_tokens(title) or _significant_tokens(snippet)
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in body_lower)
    return hits / len(tokens) >= _SOURCE_USED_TOKEN_RATIO


def validate_source_usage(
    post: FinalPost,
    sources: list[SearchSource] | None,
    materials: list[ReferenceMaterial] | None,
) -> CheckResult:
    """입력 Source가 본문에서 실제로 활용됐는지(§9). 미활용이 있으면 WARN, 없으면 PASS,
    검사할 유효 Source가 없으면 SKIPPED. 반려하지 않는다.

    검사 대상은 문맥(제목·요약·수치)이 있는 검색/검증 출처와 TEXT 메모다. 접근 실패 URL,
    본문 요약이 없는 참고 URL, 이미지·PDF 첨부는 활용 여부를 문자열로 판정할 근거가 없어
    제외하고, 제외 사유를 details에 남긴다."""
    check = "source_usage"
    body_lower = (post.body or "").lower()

    valid: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []

    for index, source in enumerate(sources or []):
        title = (source.title or "").strip()
        snippet = (source.snippet or "").strip()
        if not title and not snippet:
            excluded.append({"sourceId": f"source_{index + 1}", "reason": "제목·요약이 비어 있음"})
            continue
        data_values = [f"{p.value:g}" for p in (source.data_points or [])]
        valid.append(
            {
                "sourceId": f"source_{index + 1}",
                "type": "url",
                "title": title or source.url,
                "used": _source_used(title, snippet, data_values, body_lower),
            }
        )

    for index, material in enumerate(materials or []):
        source_id = f"material_{index + 1}"
        if material.type == ReferenceMaterialType.TEXT:
            content = (material.value or "").strip()
            if len(content) < _MIN_SOURCE_TEXT_CHARS:
                excluded.append({"sourceId": source_id, "reason": "메모 내용이 너무 짧음"})
                continue
            valid.append(
                {
                    "sourceId": source_id,
                    "type": "text",
                    "title": material.name or "사용자 메모",
                    "used": _source_used(content[:200], content, [], body_lower),
                }
            )
        else:
            # URL 본문 요약·이미지 분석 결과가 별도로 주어지지 않아 활용 여부를 문자열로
            # 판정할 수 없다. 실패가 아니라 검사 대상에서 제외한다(§9.1).
            excluded.append(
                {"sourceId": source_id, "reason": f"{material.type.value} 자료는 활용 여부 판정 대상이 아님"}
            )

    details: dict[str, Any] = {}
    if excluded:
        details["excludedSources"] = excluded

    if not valid:
        return CheckResult(check, SKIPPED, "검사할 Source가 없습니다.", details)

    unused = [
        {"sourceId": s["sourceId"], "type": s["type"], "title": s["title"]}
        for s in valid
        if not s["used"]
    ]
    if not unused:
        return CheckResult(check, PASS, "", {**details, "checked": len(valid)})
    details["unusedSources"] = unused
    details["checked"] = len(valid)
    return CheckResult(
        check, WARN, "입력된 Source 중 일부가 본문에서 활용되지 않았습니다.", details
    )


# 제목이 독자에게 하는 약속(§10). (라벨, 제목에 등장하는 트리거, 본문에서 이행을 뒷받침하는
# 표현). 단순 키워드 일치가 아니라 '약속한 종류의 내용이 본문에 있는가'로 본다.
_TITLE_PROMISES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # 본문 근거(evidence)는 되도록 두 글자 이상으로 둔다 — 한 글자('원','위','월')는 '지원',
    # '위해' 같은 다른 단어에 substring으로 걸려 실제로 없는 약속을 '이행됨'으로 오판한다.
    ("가격", ("가격", "비용", "요금", "얼마"), ("가격", "비용", "요금", "만원", "무료", "할인")),
    ("비교", ("비교", "대비"), ("비교", "차이", "대비", "반면")),
    ("차이", ("차이", "차이점"), ("차이", "다른", "다릅니다", "차이점", "반면")),
    ("추천", ("추천", "베스트"), ("추천", "추천합니다", "추천해", "권합니다", "적합")),
    ("순위", ("순위", "랭킹"), ("순위", "1위", "랭킹", "상위")),
    ("후기", ("후기", "리뷰", "사용기"), ("후기", "리뷰", "사용", "느낌", "경험")),
    ("장점", ("장점",), ("장점", "좋은 점", "이점", "강점")),
    ("단점", ("단점",), ("단점", "아쉬운", "한계", "약점")),
    ("장단점", ("장단점",), ("장점", "단점")),
    ("사용법", ("사용법", "사용 방법"), ("사용", "방법", "설정", "실행")),
    ("방법", ("방법", "하는 법", "how"), ("방법", "하려면", "단계", "절차")),
    ("과정", ("과정", "절차"), ("과정", "단계", "절차", "순서")),
    ("원인", ("원인",), ("원인", "때문", "이유")),
    ("이유", ("이유", "왜"), ("이유", "때문", "원인")),
    ("해결", ("해결", "해결법", "해결 방법"), ("해결", "방법", "조치", "대처")),
    ("주의사항", ("주의사항", "주의", "유의"), ("주의", "유의", "조심", "확인")),
    ("일정", ("일정", "언제", "날짜", "출시일"), ("일정", "날짜", "예정", "출시")),
    ("조건", ("조건", "자격"), ("조건", "자격", "해당", "기준")),
    ("기능", ("기능", "특징"), ("기능", "특징", "지원", "제공")),
)

# 개수 약속(N가지·N개·N선 등).
_NUMBER_PROMISE = re.compile(r"(\d+)\s*(가지|개|선|단계|곳|위|종류|택)")
_MAX_PROMISE_NUMBER = 30


def _count_enumerated_items(markdown: str) -> int:
    """본문에서 열거된 항목 수의 근사치. 목록(-, *, 1.)과 H3 소제목 중 가장 큰 값."""
    bullets = 0
    numbered = 0
    h3 = 0
    for raw in (markdown or "").split("\n"):
        line = raw.strip()
        if re.match(r"^[-*]\s+\S", line):
            bullets += 1
        elif re.match(r"^\d+[.)]\s+\S", line):
            numbered += 1
        elif re.match(r"^###\s+\S", line):
            h3 += 1
    return max(bullets, numbered, h3)


def validate_title_promise(post: FinalPost) -> CheckResult:
    """제목이 약속한 정보가 본문에 실제로 있는지(§10). 누락이 있으면 WARN, 모두 있으면 PASS,
    제목에 약속 표현이 없으면 SKIPPED. 반려하지 않는다."""
    check = "title_promise"
    title = post.title or ""
    title_norm = normalize_for_match(title)
    body = post.body or ""
    markdown = post.markdown_content or ""

    promises: list[dict[str, str]] = []

    for label, triggers, evidence in _TITLE_PROMISES:
        if not any(normalize_for_match(trigger) in title_norm for trigger in triggers):
            continue
        covered = any(_contains_keyword(body, term) for term in evidence)
        if covered:
            promises.append({"promise": label, "status": "COVERED"})
        else:
            promises.append(
                {
                    "promise": label,
                    "status": "MISSING",
                    "reason": f"본문에서 '{label}' 관련 설명을 확인할 수 없음",
                }
            )

    number_match = _NUMBER_PROMISE.search(title)
    if number_match:
        promised = int(number_match.group(1))
        if 1 < promised <= _MAX_PROMISE_NUMBER:
            found = _count_enumerated_items(markdown)
            if found >= promised:
                promises.append({"promise": f"{promised}{number_match.group(2)}", "status": "COVERED"})
            else:
                promises.append(
                    {
                        "promise": f"{promised}{number_match.group(2)}",
                        "status": "MISSING",
                        "reason": f"제목은 {promised}개를 약속했지만 본문에서 확인된 항목은 약 {found}개",
                    }
                )

    if not promises:
        return CheckResult(check, SKIPPED, "제목에 별도의 정보 약속이 없습니다.")

    missing = [p for p in promises if p["status"] == "MISSING"]
    if not missing:
        return CheckResult(check, PASS, "", {"promises": promises})
    return CheckResult(
        check,
        WARN,
        "제목에서 약속한 일부 내용이 본문에서 충분히 설명되지 않았습니다.",
        {"promises": promises},
    )


# --- 시각자료 검증(2026-07-28) ----------------------------------------------
#
# 여기 셋은 **원고를 반려하지 않는다**. 문제가 있는 자료 하나를 빼는 것이 완성된 원고
# 전체를 다시 쓰게 하는 것보다 낫고, 실제 제거는 생성 단계의 하드 게이트가 이미 했다.
# 이 검사들은 게이트를 빠져나온 것이 있는지 확인하는 두 번째 눈이다.


def validate_visual_purpose_fit(
    visuals: list[PlannedVisual] | None, purposes: list[str] | None
) -> CheckResult:
    """글 목적과 시각자료 유형이 맞는가.

    일상 공유 글의 인포그래픽, 가이드 글의 파이차트처럼 목적이 허용하지 않는 유형을 잡는다.
    출처 없는 그래프는 별도 검사(visual_evidence_sufficiency)가 FAIL로 다룬다.
    """
    check = "visual_purpose_fit"
    if not visuals:
        return CheckResult(check, SKIPPED, "시각자료가 없어 검사할 것이 없습니다.")
    policy = purpose_policy(purposes)
    allowed = set(policy.allowed_visual_types)
    offenders = [
        {"visualId": visual.visual_id, "type": visual.type}
        for visual in visuals
        if visual.type.upper() not in allowed
        and not (
            visual.type.upper() in policy.unlocked_by_verified_data
            and visual.data
            and visual.source
        )
    ]
    details = {"purpose": policy.purpose or "미지정", "allowed": sorted(allowed)}
    if not offenders:
        return CheckResult(check, PASS, "", details)
    return CheckResult(
        check,
        WARN,
        "글 목적에서 허용하지 않는 시각자료 유형이 남아 있습니다.",
        {**details, "offenders": offenders},
    )


def validate_visual_evidence_sufficiency(
    visuals: list[PlannedVisual] | None,
    sources: list[SearchSource] | None,
) -> CheckResult:
    """그래프에 실제 근거(수치·출처)가 있는가. 없으면 FAIL이다.

    근거 없는 그래프는 독자가 확인할 수 없는 주장을 그림으로 만든 것이라, 문구 하나를
    고치는 문제가 아니다. 다만 반려는 그 **자료**에 대한 것이고, 서비스는 원고를 살린 채
    해당 자료만 제거한다.
    """
    check = "visual_evidence_sufficiency"
    if not visuals:
        return CheckResult(check, SKIPPED, "시각자료가 없어 검사할 것이 없습니다.")
    charts = [visual for visual in visuals if visual.type.upper() in CHART_TYPES]
    if not charts:
        return CheckResult(check, PASS, "", {"charts": 0})
    unsupported = [
        {
            "visualId": visual.visual_id,
            "reason": "실측 수치 없음" if not visual.data else "출처 없음",
        }
        for visual in charts
        if not visual.data or not visual.source
    ]
    if not unsupported:
        return CheckResult(check, PASS, "", {"charts": len(charts)})
    return CheckResult(
        check,
        FAIL,
        "근거 없는 그래프가 있습니다. 해당 시각자료를 제거하거나 실제 출처 수치로 다시 만드세요.",
        {"unsupported": unsupported},
    )


def validate_visual_redundancy(
    post: FinalPost, visuals: list[PlannedVisual] | None
) -> CheckResult:
    """자료가 바로 앞 문단을 그대로 반복하는가. 반복이면 WARN이다.

    본문 문장을 박스 세 개로 다시 나눈 이미지는 정보를 더하지 않고 길이만 늘린다.
    """
    check = "visual_redundancy"
    if not visuals:
        return CheckResult(check, SKIPPED, "시각자료가 없어 검사할 것이 없습니다.")
    body = post.markdown_content or post.body or ""
    weak_reason = [
        visual.visual_id
        for visual in visuals
        if visual.visual_reason is not None and is_vague_reason(visual.visual_reason)
    ]
    repeated = [visual.visual_id for visual in visuals if is_redundant(visual, body)]
    if not repeated and not weak_reason:
        return CheckResult(check, PASS, "", {"checked": len(visuals)})
    details: dict[str, Any] = {"checked": len(visuals)}
    if repeated:
        details["repeatsPrecedingParagraph"] = repeated
    if weak_reason:
        details["vagueReason"] = weak_reason
    return CheckResult(
        check, WARN, "본문을 그대로 옮겼거나 필요 근거가 부실한 시각자료가 있습니다.", details
    )


# --- 참고자료 반영·경험 주장 ------------------------------------------------

# 실제 경험 근거 없이 쓰면 안 되는 표현. quality.EXPERIENCE_CLAIM_PHRASES(원고 반려용)보다
# 넓게 잡아 WARN/FAIL 신호를 모은다 — 구매·배송·기간·재구매처럼 '겪지 않으면 쓸 수 없는' 말들.
def validate_raw_keyword_grammar(
    post: FinalPost, raw_keywords: list[str] | None, entity
) -> CheckResult:
    """원본 검색어를 하나의 명사처럼 쓰지 않았는가. 썼으면 FAIL.

    사용자가 고른 것은 검색어 조합이다("사람이름 프로그램명"). 그대로 문장에 붙이고
    조사를 달면 한국어가 아니다. 다만 **모든** 검색어를 금지하지는 않는다 — '아이폰17'
    같은 정상 고유명사에는 조사가 붙는 것이 자연스럽다. 그래서 검색어가 서로 다른
    고유명사의 나열일 때만(검색으로 확인된 이름들과 대조한다) 검사한다.

    FAIL인 이유: 이것은 문체 취향이 아니라 비문이고, 다시 생성하면 고쳐지는 종류의
    문제다. 마지막 시도에서는 재생성 사유로 쓰이지 않으므로 원고가 막히지 않는다.
    """
    check = "raw_keyword_grammar"
    keywords = [k.strip() for k in (raw_keywords or []) if k and k.strip()]
    if not keywords:
        return CheckResult(check, SKIPPED, "원본 검색 키워드가 없어 검사를 건너뜁니다.")
    if entity is None:
        return CheckResult(check, SKIPPED, "소재 정체 정보가 없어 검사를 건너뜁니다.")

    names = [entity.canonical_name, *entity.person_names]
    names = [name for name in names if name]
    if not names:
        return CheckResult(check, SKIPPED, "확인된 고유명사가 없어 검사를 건너뜁니다.")

    text = f"{post.title}\n{post.markdown_content or post.body or ''}"
    found: list[str] = []
    checked: list[str] = []
    for keyword in keywords:
        if not is_entity_juxtaposition(keyword, names):
            continue
        checked.append(keyword)
        found.extend(raw_keyword_misuse(text, keyword))
    if not checked:
        return CheckResult(
            check, PASS, "", {"checkedKeywords": [], "reason": "자연스러운 고유명사"}
        )
    if not found:
        return CheckResult(check, PASS, "", {"checkedKeywords": checked})
    return CheckResult(
        check,
        FAIL,
        "검색어 조합을 하나의 명사처럼 사용했습니다: "
        + ", ".join(found)
        + ". 두 이름의 관계를 문장으로 풀어 쓰세요.",
        {"phrases": found, "checkedKeywords": checked},
    )


def validate_program_format_grounding(post: FinalPost, entity) -> CheckResult:
    """영상 콘텐츠 글의 도입부가 그 콘텐츠를 실제로 설명하는가. 아니면 WARN.

    정식 명칭·플랫폼(콘텐츠 종류)·주요 출연자·핵심 활동 가운데 확인된 것들이 제목이나
    도입부에 나와야 한다. 무엇을 하는 콘텐츠인지 말하지 않고 곁가지부터 시작하는 글은
    그 콘텐츠를 소개한 것이 아니다.
    """
    check = "program_format_grounding"
    if entity is None or not entity.is_media_content:
        return CheckResult(check, SKIPPED, "영상 콘텐츠 소재가 아니어서 검사하지 않습니다.")

    markdown = post.markdown_content or post.body or ""
    lead = "\n".join(
        [post.title, first_substantive_paragraph(markdown), _first_h2(markdown)]
    )
    expected: dict[str, list[str]] = {}
    if entity.canonical_name:
        expected["canonicalName"] = [entity.canonical_name]
    if entity.platform:
        expected["platform"] = [entity.platform]
    if entity.person_names:
        expected["person"] = entity.person_names
    activities = [a for a in entity.primary_activities if a]
    if entity.core_format:
        activities = [entity.core_format, *activities]
    if activities:
        expected["coreActivity"] = activities

    if not expected:
        return CheckResult(check, SKIPPED, "확인된 콘텐츠 정보가 없어 검사를 건너뜁니다.")

    missing = [
        label
        for label, candidates in expected.items()
        if not any(_lead_mentions(lead, candidate) for candidate in candidates)
    ]
    details = {"expected": sorted(expected), "missing": missing}
    # 무엇인지(정식 명칭)와 무엇을 하는지(핵심 활동)는 빠지면 안 된다. 플랫폼·출연자는
    # 그것을 받치는 정보라, 둘 다 빠졌을 때만 문제로 본다 — 플랫폼 표기는 'YouTube'와
    # '유튜브'처럼 언어가 갈려 하나만으로 판정하면 멀쩡한 글이 걸린다.
    essential = [label for label in ("canonicalName", "coreActivity") if label in missing]
    supporting = [label for label in ("platform", "person") if label in expected]
    supporting_missing = [label for label in supporting if label in missing]
    if not essential and (not supporting or len(supporting_missing) < len(supporting)):
        return CheckResult(check, PASS, "", details)
    return CheckResult(
        check,
        WARN,
        "영상 콘텐츠의 핵심 정보가 제목·도입부에 없습니다: " + ", ".join(missing),
        details,
    )


def _first_h2(markdown: str) -> str:
    """첫 번째 핵심 섹션의 소제목. 없으면 빈 문자열."""
    return next(
        (
            match.group(2)
            for match in (_HEADING_LINE.match(line.strip()) for line in (markdown or "").split("\n"))
            if match and len(match.group(1)) == 2
        ),
        "",
    )


def _lead_mentions(lead: str, phrase: str) -> bool:
    """도입부가 이 표현을 담고 있는가. 활동 설명은 한 문장 그대로 오지 않으므로,
    의미 토큰이 절반 이상 등장하면 담긴 것으로 본다."""
    if _contains_keyword(lead, phrase):
        return True
    tokens = _significant_tokens(phrase)
    if not tokens:
        return False
    lead_norm = normalize_for_match(lead)
    hits = sum(1 for token in tokens if normalize_for_match(token) in lead_norm)
    return hits * 2 >= len(tokens)


def validate_secondary_activity_emphasis(post: FinalPost, entity) -> CheckResult:
    """보조 장면이 글의 얼굴을 전부 차지하지 않았는가. 차지했으면 WARN.

    제목·도입부·첫 소제목이 **모두** 보조 활동만 말하고 핵심 포맷은 어디에도 없으면,
    그 글은 곁가지를 콘텐츠의 정체성으로 소개한 것이다. 셋 중 하나에 등장하는 것은
    정상이므로(보조 장면도 설명 대상이다) 세 자리를 모두 차지했을 때만 잡는다.
    """
    check = "secondary_activity_emphasis"
    if entity is None or not entity.is_media_content:
        return CheckResult(check, SKIPPED, "영상 콘텐츠 소재가 아니어서 검사하지 않습니다.")
    secondary = [a for a in (*entity.secondary_activities, *entity.background_scenes) if a]
    primary = [a for a in entity.primary_activities if a]
    if entity.core_format:
        primary = [entity.core_format, *primary]
    if not secondary or not primary:
        return CheckResult(check, SKIPPED, "핵심·보조 활동 구분 정보가 없습니다.")

    markdown = post.markdown_content or post.body or ""
    slots = {
        "title": post.title,
        "lead": first_substantive_paragraph(markdown),
        "firstHeading": _first_h2(markdown),
    }
    taken = {
        name: text
        for name, text in slots.items()
        if text
        and any(_lead_mentions(text, item) for item in secondary)
        and not any(_lead_mentions(text, item) for item in primary)
    }
    details = {"secondaryOnlySlots": sorted(taken), "slots": sorted(slots)}
    if len(taken) < len([name for name, text in slots.items() if text]):
        return CheckResult(check, PASS, "", details)
    return CheckResult(
        check,
        WARN,
        "제목·도입부·첫 소제목이 모두 보조 장면만 다루고 핵심 포맷이 빠졌습니다.",
        details,
    )


def validate_official_thumbnail_used(
    post: FinalPost, entity, thumbnail_photo=None
) -> CheckResult:
    """실제 영상 콘텐츠 글의 대표 이미지가 공식 영상 썸네일인가. 아니면 FAIL.

    이 검사는 이미지 단계 뒤에 돈다(원고 검증 묶음이 아니다). 실제 프로그램을 다루면서
    쓸 수 있는 공식 썸네일을 두고 다른 그림을 대표로 쓰는 것은 규격 위반이다.

    판정 기준이 '``source == "web"``'이면 안 된다. 지금은 모든 카드가 네이버 검색을 먼저
    타므로 일반 웹 사진도 ``source == "web"``이고, 그러면 공식 썸네일을 못 구한 글이
    조용히 통과한다 — 문제가 재발해도 로그에 아무것도 남지 않는다. 실제로 실린 사진의
    **출처 유형**을 본다.
    """
    check = "official_thumbnail_used"
    if entity is None or not entity.wants_official_youtube_thumbnail:
        return CheckResult(check, SKIPPED, "공식 썸네일 대상 소재가 아닙니다.")
    if post.featured_image is None:
        return CheckResult(check, SKIPPED, "대표 이미지가 없습니다.")

    source_type = getattr(thumbnail_photo, "source_type", None)
    details = {
        "source": post.featured_image.source,
        "photoSourceType": source_type or "NONE",
    }
    if source_type == WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL:
        return CheckResult(check, PASS, "", details)
    return CheckResult(
        check,
        FAIL,
        "실제 영상 콘텐츠 글인데 대표 이미지가 공식 영상 썸네일이 아닙니다.",
        details,
    )


def validate_real_entity_image_used(
    post: FinalPost, entity, thumbnail_photo=None
) -> CheckResult:
    """실존 대상 글의 대표 이미지가 실제로 찾아온 사진인가. 아니면 FAIL.

    ``validate_official_thumbnail_used``의 짝이다. 그쪽은 유튜브에 공식 영상이 있는
    콘텐츠만 보고, 여기는 나머지 실존 대상(상품·인물·장소·책·게임)을 본다. 두 검사가
    같은 글을 함께 잡지 않도록 대상이 겹치는 구간에서는 이 검사가 비켜선다.

    판정 기준은 '실제로 실린 사진이 웹에서 찾아온 것인가'다. 실존 상품·인물·장소 자리에
    생성 이미지가 들어가면, 글은 실제 대상을 말하는데 그림은 비슷한 다른 것을 보여 준다 —
    이 어긋남은 독자가 알아채고 신뢰를 잃는 종류의 결함이라 신호로 남겨야 한다.
    """
    check = "real_entity_image_used"
    if entity is None or not entity.wants_real_image:
        return CheckResult(check, SKIPPED, "실물 이미지가 필요한 소재가 아닙니다.")
    if entity.wants_official_youtube_thumbnail:
        return CheckResult(
            check, SKIPPED, "공식 영상 썸네일 검사(official_thumbnail_used)가 담당합니다."
        )
    if post.featured_image is None:
        return CheckResult(check, SKIPPED, "대표 이미지가 없습니다.")

    details = {
        "source": post.featured_image.source,
        "realImageType": entity.effective_real_image_type,
        "subject": entity.subject_label,
    }
    # 사용자가 직접 올린 이미지(reference)는 실물 근거로는 웹 검색보다 낫다.
    if post.featured_image.source in ("web", "reference"):
        return CheckResult(check, PASS, "", details)
    return CheckResult(
        check,
        FAIL,
        f"실존 대상('{entity.subject_label}') 글인데 대표 이미지가 실제 사진이 아닙니다.",
        details,
    )


def validate_category_fit(post: FinalPost, entity) -> CheckResult:
    """이 카테고리에서 빠지면 안 되는 정보가 실제로 들어 있는가. 없으면 WARN.

    두 가지를 본다.

    1) **판정 단계에서 확인됐는가** — 상품리뷰인데 브랜드가 비어 있으면, 어느 브랜드의
       무엇인지 모르는 채로 쓴 글이다. 카테고리마다 비어 있으면 안 되는 필드가 다르다
       (category_playbooks의 required_facts).
    2) **글에 실제로 등장하는가** — 확인은 됐는데 제목·도입부 어디에도 정식 명칭이
       없으면, 그 사실이 판정 단계에서 멈춘 것이다.

    반려하지 않는다. 표현을 달리 썼을 여지가 있고(브랜드가 본문 뒤쪽에만 나오는 글은
    정상일 수 있다), 신호를 모으는 것이 먼저다.
    """
    check = "category_fit"
    if entity is None or not entity.primary_category:
        return CheckResult(check, SKIPPED, "카테고리 판정이 없어 검사를 건너뜁니다.")

    required = required_facts_for(entity)
    label = {
        "canonicalName": "정식 명칭",
        "brand": "브랜드·제작 주체",
        "platform": "플랫폼·매체",
        "relatedPeople": "관련 인물",
    }
    values = {
        "canonicalName": entity.canonical_name.strip(),
        "brand": entity.brand.strip(),
        "platform": entity.platform.strip(),
        "relatedPeople": ", ".join(entity.person_names),
    }
    unconfirmed = [label.get(name, name) for name in required if not values.get(name)]

    lead = f"{post.title}\n{first_substantive_paragraph(post.markdown_content or post.body or '')}"
    name = entity.canonical_name.strip()
    name_missing = bool(name) and not _contains_keyword(lead, name)

    details = {
        "category": entity.primary_category,
        "secondaryCategory": entity.secondary_category,
        "unconfirmed": unconfirmed,
        "canonicalNameInLead": not name_missing,
    }
    if not unconfirmed and not name_missing:
        return CheckResult(check, PASS, "", details)

    reasons = []
    if unconfirmed:
        reasons.append(f"확인되지 않은 필수 정보: {', '.join(unconfirmed)}")
    if name_missing:
        reasons.append(f"정식 명칭 '{name}'이 제목·도입부에 없음")
    return CheckResult(
        check,
        WARN,
        f"'{entity.primary_category}' 카테고리에 필요한 정보가 부족합니다. " + " / ".join(reasons),
        details,
    )


# 정확성이 중요한 주제에서 쓰면 안 되는 단정 표현. 건강·의학, 금융·투자, 법률·제도처럼
# 틀린 단정이 독자의 손해로 이어지는 글에만 적용한다 — 일반 글에서는 같은 표현이
# 과장일 뿐이고, 그쪽은 기존 낚시·과장 검사가 본다.
HIGH_STAKES_CERTAINTY_PHRASES = (
    "반드시 낫",
    "완치됩니다",
    "완치된다",
    "부작용이 없",
    "치료 효과가 보장",
    "무조건 오르",
    "무조건 사야",
    "수익이 보장",
    "원금이 보장",
    "손실 없이",
    "확실히 이익",
    "100% 안전",
    "100% 보장",
)


def validate_high_stakes_certainty(post: FinalPost, entity) -> CheckResult:
    """정확성이 중요한 주제에서 단정하지 않았는가. 단정했으면 FAIL.

    건강·금융·법률·제도 글의 단정은 문체 문제가 아니다. 독자가 그 문장을 근거로 행동하면
    손해를 볼 수 있고, 그 문장은 우리가 확인하지 않은 것이다. 다시 생성하면 고쳐지는
    종류의 문제라 FAIL로 둔다.
    """
    check = "high_stakes_certainty"
    if entity is None or not entity.is_high_stakes:
        return CheckResult(check, SKIPPED, "정확성 위험이 높은 주제가 아닙니다.")
    text = f"{post.title}\n{post.markdown_content or post.body or ''}"
    found = [phrase for phrase in HIGH_STAKES_CERTAINTY_PHRASES if phrase in text]
    details = {"category": entity.primary_category, "entityType": entity.entity_type}
    if not found:
        return CheckResult(check, PASS, "", details)
    return CheckResult(
        check,
        FAIL,
        "확인되지 않은 효과·수익·안전을 단정했습니다: "
        + ", ".join(found)
        + ". 개인차와 확인이 필요한 지점을 밝히는 문장으로 바꾸세요.",
        {**details, "phrases": found},
    )


def validate_reference_anchor_usage(
    post: FinalPost, evidence: ReferenceEvidenceProfile | None
) -> CheckResult:
    """참고자료가 확인해 준 대상이 글에 실제로 등장하는가.

    참고자료가 있는데 일반론만 쓴 글은 '자료를 첨부한 의미가 없는 글'이다. 대상 이름이
    제목이나 첫 문단에 나오는지, 확인된 특징이 본문에 반영됐는지 본다. 반려하지 않는다 —
    표현을 달리 썼을 수 있어 오탐 여지가 있고, 신호를 모으는 것이 먼저다.
    """
    check = "reference_anchor_usage"
    if evidence is None or not evidence.has_references:
        return CheckResult(check, SKIPPED, "참고자료가 없어 검사할 것이 없습니다.")
    anchor = evidence.anchor
    if not anchor:
        return CheckResult(check, SKIPPED, "참고자료에서 확인된 대상이 없습니다.")

    lead = f"{post.title}\n{first_substantive_paragraph(post.markdown_content or post.body or '')}"
    body = post.body or ""
    in_lead = _contains_keyword(lead, anchor)
    attributes = [
        attribute
        for attribute in evidence.confirmed_attributes
        if _significant_tokens(attribute)
        and any(token in body.lower() for token in _significant_tokens(attribute))
    ]
    details: dict[str, Any] = {
        "entity": anchor,
        "inTitleOrLead": in_lead,
        "confirmedAttributes": len(evidence.confirmed_attributes),
        "reflectedAttributes": len(attributes),
    }
    if in_lead and (not evidence.confirmed_attributes or attributes):
        return CheckResult(check, PASS, "", details)
    return CheckResult(
        check,
        WARN,
        "참고자료가 확인해 준 대상이 제목·도입부나 본문에 충분히 반영되지 않았습니다.",
        details,
    )


def validate_template_repetition(post: FinalPost) -> CheckResult:
    """정형화된 글투가 반복되는가.

    한 항목씩은 자연스러울 수 있어 **모으기만 한다**(WARN). 실제 결과를 보고 임계값을
    조정할 수 있도록 무엇이 몇 번 걸렸는지 details에 남긴다.
    """
    check = "template_repetition"
    body = post.body or ""
    markdown = post.markdown_content or ""
    if not body.strip():
        return CheckResult(check, SKIPPED, "본문이 없어 검사할 것이 없습니다.")

    phrases = [phrase for phrase in TEMPLATE_PHRASES if phrase in body]
    signals: dict[str, Any] = {}
    if phrases:
        signals["templatePhrases"] = phrases

    counts = _paragraphs_per_section(markdown)
    if len(counts) >= 3 and len(set(counts)) == 1:
        signals["identicalParagraphsPerSection"] = counts[0]

    lengths = [len(paragraph) for paragraph in _prose_paragraphs(markdown)]
    if len(lengths) >= 6:
        average = sum(lengths) / len(lengths)
        spread = max(lengths) - min(lengths)
        # 모든 문단이 평균의 ±20% 안에 있으면 사람이 쓴 리듬이 아니다.
        if average and spread / average < 0.4:
            signals["uniformParagraphLength"] = round(average)

    endings = _max_consecutive_same_ending(body)
    if endings >= 4:
        signals["consecutiveSameEnding"] = endings

    if not signals:
        return CheckResult(check, PASS, "", {"paragraphs": len(lengths)})
    return CheckResult(
        check, WARN, "정형화된 구성·문투 신호가 감지되었습니다.", signals
    )


def _prose_paragraphs(markdown: str) -> list[str]:
    blocks = re.split(r"\n\s*\n", markdown or "")
    prose: list[str] = []
    for block in blocks:
        text = block.strip()
        if not text or text.upper().startswith(_MARKER_PREFIX):
            continue
        if _HEADING_LINE.match(text) or text.startswith(">") or text.startswith("```"):
            continue
        if re.match(r"^!?\[", text):
            continue
        prose.append(text)
    return prose


def _paragraphs_per_section(markdown: str) -> list[int]:
    """H2 소제목마다 문단이 몇 개인지. 전부 같으면 기계적으로 맞춘 구성이다."""
    counts: list[int] = []
    current: int | None = None
    for block in re.split(r"\n\s*\n", markdown or ""):
        text = block.strip()
        if not text:
            continue
        match = _HEADING_LINE.match(text.split("\n")[0])
        if match and len(match.group(1)) == 2:
            if current is not None:
                counts.append(current)
            current = 0
            continue
        if current is None:
            continue
        if text.upper().startswith(_MARKER_PREFIX) or re.match(r"^!?\[", text):
            continue
        current += 1
    if current is not None:
        counts.append(current)
    return [count for count in counts if count]


_SENTENCE_END = re.compile(r"[^.!?。\n]+[.!?。]")
# 한국어 종결 어미의 마지막 두 글자로 형태를 본다. '습니다/합니다'와 '이다/한다'는 다른 리듬이다.
_ENDING_TAIL = 3


def _max_consecutive_same_ending(body: str) -> int:
    endings: list[str] = []
    for raw in _SENTENCE_END.findall(body or ""):
        sentence = raw.strip().rstrip(".!?。").strip()
        if len(sentence) < 8:
            continue
        endings.append(sentence[-_ENDING_TAIL:])
    best = run = 0
    previous = None
    for ending in endings:
        run = run + 1 if ending == previous else 1
        previous = ending
        best = max(best, run)
    return best


# --- 통합·실행·로그 ---------------------------------------------------------


def aggregate_validation_results(checks: list[CheckResult]) -> ValidationResult:
    """검사 결과들을 공통 구조로 통합한다(§11)."""
    return ValidationResult(checks=checks)


def _safe_check(func, check_name: str, *args) -> CheckResult:
    """검사 하나를 실행하되, 내부 오류는 콘텐츠 FAIL과 구분해 SKIPPED로 흡수한다(§16).
    한 검사가 터져도 나머지 검사는 계속 돌아야 한다."""
    try:
        return func(*args)
    except Exception as error:  # noqa: BLE001 — 어떤 검사 오류도 파이프라인을 멈추면 안 된다.
        logger.warning("검증 오류(무시) | %s - %s", check_name, error)
        return CheckResult(
            check_name,
            SKIPPED,
            "검증 처리 중 오류가 발생하여 검사를 건너뛰었습니다.",
            {"errorType": "VALIDATOR_ERROR"},
        )


def validate_published_duplication(
    post: FinalPost, published: list[PostDigest] | None
) -> CheckResult:
    """이미 발행한 내 글과 새 원고가 얼마나 겹치는가(§중복).

    자동 생성의 위험은 한 편의 품질이 아니라 **쌓였을 때의 닮음**이다. 한 편씩 보면
    어느 것도 이상하지 않아 다른 검사는 아무것도 잡지 못한다.

    **WARN까지만 한다.** 임계값(0.70/0.85)은 검색엔진의 공식 기준이 아니라 우리 내부
    관리 기준이라, 그것을 근거로 완성된 원고를 반려하지 않는다(§14와 같은 규칙). 어디가
    닮았는지 축별로 남겨, 사람이 보고 판단하거나 나중에 부분 재작성의 근거로 쓴다.
    """
    check = "published_duplication"
    if not published:
        return CheckResult(check, SKIPPED, "비교할 기존 발행 글이 없어 검사를 건너뜁니다.")

    markdown = post.markdown_content or post.body or ""
    candidate = PostDigest(
        post_id="",
        title=post.title or "",
        headings=extract_headings(markdown),
        opening=first_paragraph(markdown),
    )
    verdict = compare(candidate, published)
    details = {
        "score": round(verdict.score, 3),
        "titleScore": round(verdict.title_score, 3),
        "headingsScore": round(verdict.headings_score, 3),
        "openingScore": round(verdict.opening_score, 3),
        "comparedWith": len(published[:COMPARE_LIMIT]),
        "closestPostId": verdict.closest.post_id if verdict.closest else None,
    }
    if verdict.near_duplicate:
        return CheckResult(
            check, WARN, "이미 발행한 글과 사실상 같은 글입니다.", details
        )
    if verdict.similar:
        return CheckResult(
            check, WARN, "이미 발행한 글과 도입부·소제목이 많이 겹칩니다.", details
        )
    return CheckResult(check, PASS, "", details)


def run_content_validations(
    post: FinalPost,
    seo_plan: SeoKeywordPlan | None,
    sources: list[SearchSource] | None,
    materials: list[ReferenceMaterial] | None,
    visuals: list[PlannedVisual] | None = None,
    purposes: list[str] | None = None,
    evidence: ReferenceEvidenceProfile | None = None,
    title_locked: bool = False,
    raw_keywords: list[str] | None = None,
    published: list[PostDigest] | None = None,
) -> ValidationResult:
    """SEO → 콘텐츠 품질 → 시각자료·근거 검증을 순서대로 독립 실행하고 결과를 통합한다.

    각 검사는 서로의 결과에 영향을 주지 않으며, 하나가 실패해도 나머지는 계속 실행된다.
    FAIL은 셋뿐이다: 제목의 SEO Primary, 근거 없는 그래프, 지어낸 사용 경험. 나머지는 모두
    WARN/PASS/SKIPPED이며 로그로만 쌓인다(§14: 품질 신호는 수집하되 생성을 막지 않는다).
    title_locked면 첫 번째 FAIL도 WARN이 된다 — 재생성이 바꿀 수 없는 제목을 이유로 원고를
    반려하지 않는다. 새 인자는 전부 기본값이 있어, 옛 호출부는 예전과 똑같이 동작한다."""
    entity = evidence.content_entity if evidence is not None else None
    checks = [
        _safe_check(
            validate_primary_keyword_in_title,
            "seo_primary_in_title",
            post,
            seo_plan,
            title_locked,
            entity,
        ),
        _safe_check(
            validate_primary_keyword_in_first_paragraph,
            "seo_primary_in_first_paragraph",
            post,
            seo_plan,
            entity,
        ),
        _safe_check(validate_secondary_keyword_usage, "seo_secondary_usage", post, seo_plan),
        _safe_check(validate_h2_count, "h2_count", post),
        _safe_check(validate_source_usage, "source_usage", post, sources, materials),
        _safe_check(validate_title_promise, "title_promise", post),
        _safe_check(validate_visual_purpose_fit, "visual_purpose_fit", visuals, purposes),
        _safe_check(
            validate_visual_evidence_sufficiency,
            "visual_evidence_sufficiency",
            visuals,
            sources,
        ),
        _safe_check(validate_visual_redundancy, "visual_redundancy", post, visuals),
        _safe_check(
            validate_reference_anchor_usage, "reference_anchor_usage", post, evidence
        ),
        _safe_check(validate_template_repetition, "template_repetition", post),
        # 한 원고 **안**의 반복은 위 검사가 본다. 이건 **이미 발행한 내 글들**과의 닮음이다.
        _safe_check(validate_published_duplication, "published_duplication", post, published),
        # 소재 정체를 아는 글에만 도는 검사들. 엔티티 정보가 없으면 전부 SKIPPED라
        # 옛 글·구형 어댑터의 결과는 예전과 같다.
        _safe_check(
            validate_raw_keyword_grammar,
            "raw_keyword_grammar",
            post,
            raw_keywords,
            entity,
        ),
        _safe_check(
            validate_program_format_grounding, "program_format_grounding", post, entity
        ),
        _safe_check(
            validate_secondary_activity_emphasis,
            "secondary_activity_emphasis",
            post,
            entity,
        ),
        # 카테고리를 판정한 글에만 도는 검사들. 카테고리가 없으면 SKIPPED다.
        _safe_check(validate_category_fit, "category_fit", post, entity),
        _safe_check(validate_high_stakes_certainty, "high_stakes_certainty", post, entity),
    ]
    return aggregate_validation_results(checks)


logger = logging.getLogger(__name__)

# details를 로그에 실을 때의 최대 길이. 본문 전체가 새어 들어가지 않게 넉넉히 자른다.
_MAX_DETAILS_CHARS = 800


def write_validation_log(
    result: ValidationResult,
    post_id: str,
    user_id: str,
    prompt_version: str,
) -> None:
    """검사 결과와 WARN을 기존 애플리케이션 로그(logging)에 남긴다(§12·§13).

    DB 구조를 새로 만들지 않고, 검사별 PASS/WARN/FAIL/SKIPPED와 반려 여부를 집계할 수 있는
    필드를 로그로 남긴다. 개인정보·본문 전체·인증정보는 담지 않는다 — details에는 키워드·
    개수·출처 id 같은 최소 정보만 들어간다."""
    counts = result.counts()
    logger.info(
        "[content-validation] post=%s status=%s pass=%d warn=%d fail=%d skipped=%d version=%s",
        post_id,
        result.status,
        counts[PASS],
        counts[WARN],
        counts[FAIL],
        counts[SKIPPED],
        CONTENT_VALIDATION_VERSION,
    )
    for check in result.checks:
        record = {
            "timestamp": _now(),
            "postId": post_id,
            "userId": user_id,
            "draftId": post_id,
            "validationVersion": CONTENT_VALIDATION_VERSION,
            "check": check.check,
            "status": check.status,
            "message": check.message,
            "details": check.details,
            "rejected": check.rejected,
        }
        try:
            serialized = json.dumps(record, ensure_ascii=False)[:_MAX_DETAILS_CHARS]
        except (TypeError, ValueError):
            serialized = f"{check.check}:{check.status}"
        if check.status in (WARN, FAIL):
            logger.warning("[content-validation] %s", serialized)
        else:
            logger.info("[content-validation] %s", serialized)
