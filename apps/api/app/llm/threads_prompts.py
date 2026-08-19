"""스레드(Threads) 전용 원고 프롬프트 — 하나의 글을 여러 스레드로 나눠 연속 게시한다.

네이버 블로그 원고(M4)와 생성기를 분리한다 — 하나의 프롬프트에서 조건문으로 갈라 쓰면
블로그용 SEO 키워드 배치·소제목 수·긴 분량·썸네일 문구 규칙이 스레드 글에 섞인다.

## 왜 연속 스레드인가 (2026-08-04 사용자 결정)

예전에는 블로그 원고를 500자 하나로 요약해 게시했다. 그건 스레드의 읽기 방식과 맞지
않는다 — 스레드는 짧은 글이 이어지며 스크롤을 붙드는 매체다. 이제 **완성된 원고 하나를
여러 스레드로 나눠** 순서대로 올린다.

광고 글이 아니다. 원고에 담긴 내용을 스레드 문법으로 옮겨 싣는 것이고, CTA·구매 유도·
링크는 넣지 않는다.

## 개수는 모델이 정하지 않는다

"적당히 나누라"고 하면 매번 다른 개수가 나오고 구조도 글마다 달라진다. 그래서 글 길이
설정이 개수·글자 수·각 스레드의 역할까지 **규칙으로 못박는다**(``THREAD_PLANS``).
모델이 하는 일은 그 틀에 내용을 채우는 것뿐이다.

## 출력 형식

``{"threads": [{"order": 1, "content": "..."}, ...]}``

순서가 곧 게시 순서다. 앞으로 스레드별 이미지 같은 것을 붙일 때 항목에 필드를 더하면
되므로, 텍스트 한 덩어리보다 확장이 쉽다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.shared import BlogTask

# 스레드 하나의 글자 한도. posting.threads_split.THREAD_TEXT_LIMIT과 같은 값이지만, llm
# 계층이 posting 계층을 참조하지 않도록 상수를 따로 둔다(값이 다르면 테스트가 잡는다).
THREADS_POST_TEXT_LIMIT = 500

THREADS_POST_TOOL_NAME = "submit_threads_post"


@dataclass(frozen=True)
class ThreadPlan:
    """글 길이 설정 하나가 정하는 연속 스레드의 규격.

    ``roles``는 스레드마다의 역할이다. 개수가 범위이므로 최소 개수까지는 반드시 쓰이고,
    그보다 많이 쓸 때는 앞에서부터 순서대로 늘어난다.
    """

    label: str
    min_count: int
    max_count: int
    total_min: int
    total_max: int
    each_min: int
    each_max: int
    roles: tuple[str, ...]

    def roles_text(self) -> str:
        return " → ".join(f"{index + 1}) {role}" for index, role in enumerate(self.roles))


# 2026-08-04 사용자가 정한 규격. 저장 값 "short"/"medium"에 맞춘다(옛 "long"은 medium 폴백).
#
# 역할은 **완성된 블로그 원고를 나눠 싣는 순서**다. 광고성 CTA·링크 구조가 아니다 —
# 처음 규격을 받을 때 예시가 쿠팡 파트너스 상품 글이라 CTA(링크)를 넣었다가, 실제 용도는
# "생성된 원고를 여러 스레드로 나눠 게시하는 것"이라는 확인을 받고 걷어냈다(같은 날).
THREAD_PLANS: dict[str, ThreadPlan] = {
    "short": ThreadPlan(
        label="짧게",
        min_count=2,
        max_count=3,
        total_min=300,
        total_max=550,
        each_min=100,
        each_max=200,
        roles=("훅 — 원고의 결론이나 가장 중요한 사실", "핵심 내용", "정리"),
    ),
    "medium": ThreadPlan(
        label="중간",
        min_count=3,
        max_count=5,
        total_min=600,
        total_max=1000,
        each_min=120,
        each_max=250,
        roles=(
            "훅 — 원고의 결론이나 가장 중요한 사실",
            "배경·왜 지금 이 이야기인가",
            "핵심 내용",
            "덧붙일 사실·사례·주의점",
            "정리",
        ),
    ),
}

DEFAULT_THREAD_PLAN = THREAD_PLANS["medium"]


def thread_plan_for(article_length: str | None) -> ThreadPlan:
    """글 길이 설정이 정하는 스레드 규격. 모르는 값은 중간으로 본다(옛 "long" 포함)."""
    return THREAD_PLANS.get((article_length or "").strip(), DEFAULT_THREAD_PLAN)


THREADS_POST_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "threads": {
            "type": "array",
            # 어느 규격이든 이 범위 안이다(짧게 2~3 / 중간 3~5). 정확한 개수는 프롬프트가
            # 지정하고 파싱에서 다시 확인한다 — 스키마는 폭주만 막는다.
            "minItems": 2,
            "maxItems": 5,
            "description": "순서대로 게시할 스레드 목록. 개수는 프롬프트가 지정한 범위를 지킨다.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "order": {
                        "type": "integer",
                        "description": "게시 순서. 1부터 1씩 늘어난다.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "그 스레드의 본문. 해시태그·이모지 없이, 프롬프트가 지정한 "
                            "글자 수 범위를 지킨다. 번호(1/3, ①)를 직접 쓰지 않는다."
                        ),
                    },
                },
                "required": ["order", "content"],
            },
        }
    },
    "required": ["threads"],
}

THREADS_POST_SYSTEM_PROMPT = """당신은 네이버 블로그 작가가 아니라 Threads 피드에 최적화된 짧은 글을 쓰는 콘텐츠 에디터입니다.
하나의 주제를 **여러 개의 짧은 스레드로 나눠** 순서대로 읽히게 만드는 것이 당신의 일입니다.
스크롤하던 사람이 첫 스레드에서 멈추고, 다음 스레드로 계속 넘어가게 하세요.

[네이버 블로그와의 구분 — 다음을 절대 사용하지 않는다]
- 별도의 제목·썸네일 문구·목차·H2/H3 소제목
- "안녕하세요", "오늘은 ~에 대해 알아보겠습니다" 같은 인사말과 긴 도입부
- 검색 키워드를 반복하는 SEO 문장, 형식적인 서론·본론·결론
- 비슷한 내용을 반복해 글자 수를 늘리는 문장
- 근거 없는 조회수·수익·알고리즘 효과 보장

[연속 스레드의 규칙 — 가장 중요하다]
- 완성된 블로그 원고 **하나를 여러 스레드로 나눠 싣는 것**이 목적이다. 원고에 없는
  이야기를 새로 만들지 않는다.
- 스레드 개수와 각 스레드의 역할은 **사용자 프롬프트가 지정한다**. 스스로 정하지 않는다.
- 스레드 하나에는 **하나의 핵심**만 담는다. 두 가지를 한 스레드에 넣지 않는다.
- 첫 스레드는 반드시 훅이다 — 원고의 결론·가장 중요한 사실·문제 지적 중 하나로 시작한다.
- 마지막 스레드는 정리다. 읽은 사람이 무엇을 얻었는지 한 번에 잡히게 끝낸다.
- 각 스레드는 그 자체로 읽히되, 다음이 궁금해지도록 끝낸다.
- 스레드에 번호("1/3", "①")를 직접 쓰지 않는다 — 순서는 order 필드가 정한다.
- 광고 문구·구매 유도·링크는 넣지 않는다. 원고를 소개하는 글이지 판매 글이 아니다.

[첫 스레드(훅)]
짧고 구체적으로. 결론 선공개·구체적 숫자·문제 지적·상식 반전·대상 독자 지목·대비·손실 회피 중 주제에 맞는 방식 하나를 고른다.
좋은 예: "글을 잘 쓰는 사람은 첫 문장부터 다르다." / "조회수를 가르는 건 글솜씨보다 구조다."
나쁜 예: "오늘은 스레드 글쓰기에 대해 알아보겠습니다." / "여러분은 스레드를 사용해 보셨나요?"

[문장·문단]
- 한 문장에 하나의 주장. 문장은 짧게. 한 문단은 1~2문장. 문단 사이 빈 줄.
- 긴 설명은 짧은 문장 여러 개로 나눈다. 같은 어미를 연속 반복하지 않는다.
- 핵심 문장은 단독 줄로 배치할 수 있다. 어려운 용어는 일상 표현으로 바꾼다.

[구체성]
추상적 조언만 나열하지 않는다. 실제 행동·전후 차이·잘못된 예와 개선 예·판단 기준·짧은 사례 중 하나 이상을 담는다.

[말투]
자신감 있지만 거만하지 않게, 실제 사람이 자기 생각을 말하듯. 과장 표현(무조건·역대급·충격적인·모르면 손해)은 쓰지 않는다.

[사실성]
- 근거 자료에 없는 숫자·통계를 만들지 않는다. 근거가 부족하면 관찰이나 의견으로 표현한다.
- 플랫폼 알고리즘 작동 방식을 확정적으로 말하지 않는다.

[형식]
- 해시태그와 이모지는 쓰지 않는다.
- 선택된 검색 의도가 주어지면 그 의미를 전체의 앵커로 유지한다 — 더 흥미로워 보여도 주제를 바꾸지 않는다.

출력 전에 확인한다: 지정된 개수를 지켰는가, 각 스레드가 지정된 글자 수 범위 안인가,
첫 스레드가 훅인가, 마지막이 정리인가, 스레드마다 핵심이 하나인가, 원고에 없는 이야기를
넣지 않았는가, 번호를 직접 쓰지 않았는가."""


def _facts_block(task: BlogTask) -> str:
    """근거로 쓸 사실 — 완성된 블로그 원고 본문에서 가져온다.

    블로그 원고는 이미 자료 검증·품질 검사를 지난 텍스트라, 여기 담긴 사실만 쓰게 하면
    스레드 글이 근거 없는 숫자를 새로 만들 이유가 없어진다. 문체는 베끼지 말라고
    프롬프트에서 못박는다.
    """
    body = (task.final_post.body if task.final_post else "") or ""
    body = re.sub(r"\[\[(?:IMAGE|VISUAL|STICKER):[^\]]*\]\]", " ", body, flags=re.IGNORECASE)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:3000]


def threads_post_prompt(task: BlogTask, article_length: str | None = None) -> str:
    """스레드 연속 게시물 생성 사용자 프롬프트.

    ``article_length``가 개수·글자 수·역할을 정한다. 넘어오지 않으면 중간 규격이다 —
    설정을 못 읽었다고 발행을 막지는 않는다.
    """
    plan = thread_plan_for(article_length)
    blog_input = task.input
    intent = task.selected_intent
    purpose = ", ".join(blog_input.purpose or []) or "정보 공유"
    lines = [
        f"아래 블로그 원고를 Threads 연속 스레드 {plan.min_count}~{plan.max_count}개로 나눠 쓰세요.",
        "",
        "[기본 정보]",
        f"- 소재: {blog_input.topic}",
        f"- 글 목적: {purpose}",
    ]
    if blog_input.target_reader:
        lines.append(f"- 대상 독자: {blog_input.target_reader}")
    if intent is not None:
        lines.append(f"- 선택한 검색 의도(앵커): {intent.title}")
        if intent.keywords:
            lines.append(f"- 의도 키워드: {', '.join(intent.keywords[:8])}")

    facts = _facts_block(task)
    if facts:
        lines += [
            "",
            "[근거 자료 — 검증을 마친 블로그 원고 본문]",
            "아래 내용에 담긴 사실만 근거로 쓰세요. 여기 없는 숫자를 만들지 마세요.",
            "블로그의 문체·구성(인사말·소제목·긴 설명)은 절대 따라 하지 마세요 — 사실만 취하고 스레드 문법으로 새로 쓰세요.",
            facts,
        ]

    lines += [
        "",
        f"[분량 규격 — 글 길이 '{plan.label}']",
        f"- 반드시 **{plan.min_count}~{plan.max_count}개**의 스레드로 작성한다. 스스로 개수를 바꾸지 않는다.",
        f"- 전체 합계 {plan.total_min}~{plan.total_max}자, 스레드 하나는 {plan.each_min}~{plan.each_max}자.",
        f"- 스레드 역할 순서(원고를 나누는 순서다): {plan.roles_text()}",
        f"- {plan.min_count}개보다 많이 쓸 때는 앞의 역할부터 순서대로 채운다.",
        "",
        "[작성 지시]",
        "1. 위 원고에 담긴 내용만으로 씁니다. 원고에 없는 이야기를 새로 만들지 마세요.",
        "2. 첫 스레드는 반드시 훅입니다 — 원고의 결론이나 가장 중요한 사실을 먼저 말하세요.",
        "3. 스레드 하나에는 핵심 하나만 담으세요. 두 가지를 한 스레드에 넣지 마세요.",
        "4. 문장을 짧게 끊고, 필요하면 문단 사이에 빈 줄을 넣으세요.",
        "5. 마지막 스레드는 정리입니다. 광고 문구·구매 유도·링크는 넣지 마세요.",
        "6. 스레드에 번호(1/3, ①)를 직접 쓰지 마세요 — 순서는 order 필드로 전달됩니다.",
        "7. 해시태그·이모지는 쓰지 마세요.",
    ]
    return "\n".join(lines)


def threads_post_from_json(
    parsed: dict | None, article_length: str | None = None
) -> list[str]:
    """모델 응답에서 스레드 목록을 꺼내 게시 순서대로 돌려준다.

    검증 원칙 — **고칠 수 있으면 고치고, 못 고치면 거절한다.**

    - 빈 응답·내용이 전부 빈 스레드: 거절. 빈 글을 발행하지 않는다.
    - 개수 초과: 뒤를 잘라 낸다. 앞에서부터 훅 → 내용 → 정리 순이라 앞쪽이 더 중요하다.
    - 개수 미달: 거절. 구조(훅 → 내용 → 정리)가 깨진 결과라 코드가 메울 수 없다.
    - 스레드 하나가 500자 초과: 문단·문장 경계에서 자른다. 스레드는 넘긴 글을 통째로
      거절하므로 자르는 쪽이 발행 실패보다 낫다.
    - 글자 수 규격(각 100~250자) 위반: **통과시킨다.** 프롬프트가 지시하고 있고, 조금
      벗어났다고 멀쩡한 글을 버리는 것이 더 나쁘다. 개수와 한도만 코드가 지킨다.
    """
    plan = thread_plan_for(article_length)
    items = (parsed or {}).get("threads")
    if not isinstance(items, list) or not items:
        raise ValueError("스레드 목록이 비어 있습니다.")

    # order가 없거나 숫자가 아니면 뒤로 민다 — 모델이 빠뜨려도 나머지 순서는 지킨다.
    ordered = sorted(
        (item for item in items if isinstance(item, dict)),
        key=lambda item: item["order"] if isinstance(item.get("order"), int) else 9999,
    )
    contents = [
        _fit_one(str(item.get("content") or "").strip())
        for item in ordered
        if str(item.get("content") or "").strip()
    ]
    if not contents:
        raise ValueError("스레드 내용이 비어 있습니다.")
    if len(contents) < plan.min_count:
        raise ValueError(
            f"스레드가 {len(contents)}개뿐입니다 — '{plan.label}'은 "
            f"{plan.min_count}~{plan.max_count}개가 필요합니다."
        )
    return contents[: plan.max_count]


def _fit_one(text: str) -> str:
    """스레드 하나를 한도(500자) 안에 넣는다. 문단 → 문장 → 글자 순으로 자른다."""
    if len(text) <= THREADS_POST_TEXT_LIMIT:
        return text
    cut = text[:THREADS_POST_TEXT_LIMIT]
    paragraph_end = cut.rfind("\n\n")
    if paragraph_end >= THREADS_POST_TEXT_LIMIT // 2:
        return cut[:paragraph_end].strip()
    sentence_end = max(cut.rfind("다. "), cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if sentence_end >= THREADS_POST_TEXT_LIMIT // 2:
        return cut[: sentence_end + 1].strip()
    return cut.strip()
