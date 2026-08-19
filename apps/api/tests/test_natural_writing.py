"""M4 원고: 소유권 서열 · 자연스러운 원고 계약 · 기계적 패턴 검사 · 분량 수정 지시.

기준선 실측(6사례)이 가리킨 것만 검증한다:
- 근거 없는 수치 5/6 → 가장 큰 결함
- 분량 준수 4/6 → 수정 지시가 숫자와 자리를 지목해야 한다
- 도입부 상투구·문단 균일·소제목 균일·결론 중복은 0/6 → 이미 지켜지고 있으므로 규칙을
  넣되 검사는 경고로 둔다(멀쩡한 원고를 문체로 버리지 않는다)
"""

from app.llm import prompts
from app.modules.draft.quality import check_draft
from app.shared import DraftGenerationSettings, FinalPost


def post(markdown: str, *, title: str = "제습기 관리 순서", hashtags: int = 5) -> FinalPost:
    plain = "\n".join(
        line for line in markdown.splitlines() if not line.strip().startswith("#")
    )
    return FinalPost(
        title=title,
        body=markdown,
        hashtags=["태그"] * hashtags,
        html_content=f"<h1>{title}</h1><p>{plain}</p>",
        markdown_content=markdown,
    )


def report_for(markdown: str, **kwargs):
    return check_draft(
        post(markdown, **{k: v for k, v in kwargs.items() if k in {"title", "hashtags"}}),
        hashtag_count=5,
        min_body_chars=kwargs.get("min_body_chars", 1),
        max_body_chars=kwargs.get("max_body_chars", 100000),
        photo_count=0,
    )


def warnings_text(markdown: str, **kwargs) -> str:
    return " | ".join(report_for(markdown, **kwargs).warnings)


class TestOwnershipPriority:
    def test_the_order_appears_once_in_the_draft_prompt(self):
        text = "\n".join(prompts.OWNERSHIP_PRIORITY)
        assert text.count("글 목적이 글의 종류") == 1
        assert "페르소나는 말투·어조" in text
        assert "SEO 계획은 키워드가 들어갈 위치만" in text

    def test_persona_cannot_change_structure_or_facts(self):
        text = "\n".join(prompts.OWNERSHIP_PRIORITY)
        assert "페르소나는 글의 목적, 글 종류, 섹션 순서, 분량, SEO 규칙, 사실성 규칙을 바꿀 수 없다" in text

    def test_purpose_outranks_persona_in_the_numbered_list(self):
        joined = "\n".join(prompts.OWNERSHIP_PRIORITY)
        assert joined.index("글 목적이") < joined.index("페르소나는 말투")


class TestNaturalWritingContract:
    def test_it_is_not_a_style_template_for_every_persona(self):
        rules = "\n".join(prompts.NATURAL_EDITORIAL_RULES)
        # 문체를 통일하라는 규칙이 아니라 하한선이라는 것이 문장에 드러나야 한다.
        assert "오탈자" in rules and "유행어" in rules

    def test_evidence_bound_specificity_is_required(self):
        """검증할 수 있는 사실은 여전히 자료 안에서만 쓴다.

        2026-08-03 사용자 결정으로 '개인적인 사용 경험'은 이 목록에서 빠졌다 —
        체험 서술은 허용되고, 수치·발언 같은 확인 가능한 사실만 자료로 묶는다.
        """
        rules = "\n".join(prompts.NATURAL_EDITORIAL_RULES)
        assert "개인적인 사용 경험" not in rules
        assert "자료에 있을 때만 쓴다" in rules

    def test_the_anti_patterns_name_concrete_shapes(self):
        patterns = "\n".join(prompts.MECHANICAL_WRITING_ANTI_PATTERNS)
        for shape in (
            "제목을 첫 문장에서 그대로 반복",
            "모든 문단이 3문장으로 고정",
            "먼저 → 다음으로 → 마지막으로",
            "도움이 되셨길 바랍니다",
        ):
            assert shape in patterns

    def test_the_section_flow_rule_no_longer_contradicts_the_rhythm_rule(self):
        # 예전에는 "각 섹션은 핵심 주장 → 근거 → 사례 → 판단 기준으로 쓴다"와 "그 순서로
        # 기계적으로 맞추지 않는다"가 같은 프롬프트에 함께 실려 서로를 무효화했다.
        rhythm = "\n".join(prompts.HUMAN_RHYTHM_RULES)
        assert "기계적으로 맞추지 않는다" in rhythm


class TestMechanicalPatternChecks:
    def test_an_intro_cliche_is_caught_even_when_the_body_has_few_cliches(self):
        text = "오늘은 제습기 관리에 대해 알아보겠습니다.\n\n## 물통\n\n헹굽니다."
        assert "도입부 상투 표현" in warnings_text(text)

    def test_the_same_cliche_in_the_conclusion_is_not_an_intro_cliche(self):
        text = "장마가 시작되면 물통에서 냄새가 납니다.\n\n## 물통\n\n오늘은 여기까지입니다."
        assert "도입부 상투 표현" not in warnings_text(text)

    def test_a_connective_used_like_a_table_of_contents_is_reported(self):
        body = "\n\n".join(
            ["도입 문장입니다.", "## 하나", "또한 이렇습니다.", "또한 저렇습니다.", "또한 그렇습니다.", "또한 마지막입니다."]
        )
        assert "같은 연결어 반복" in warnings_text(body)
        assert "또한 4회" in warnings_text(body)

    def test_three_uses_are_not_yet_overuse(self):
        body = "\n\n".join(["도입.", "## 하나", "또한 하나.", "또한 둘.", "또한 셋."])
        assert "같은 연결어 반복" not in warnings_text(body)

    def test_the_word_inside_a_sentence_is_not_a_connective(self):
        # 2026-07-31 실측에서 발견한 오탐. 한 원고에 '먼저'가 12회 있었지만 문장 첫머리는
        # 0회였고, 전부 '무엇을 먼저 보는가'라는 내용이었다. 이걸 경고로 올리면 수정
        # 재시도가 모델에게 옳게 쓴 낱말을 빼라고 시킨다.
        body = "\n\n".join(
            [
                "도입 문장입니다.",
                "## 하나",
                "바닥재를 먼저 확인합니다.",
                "생활 패턴을 먼저 묻습니다.",
                "관리 간격을 먼저 봅니다.",
                "도크 자리가 먼저입니다.",
                "흡입 구조를 먼저 봅니다.",
            ]
        )
        assert "같은 연결어 반복" not in warnings_text(body)

    def test_a_list_marker_does_not_hide_the_connective(self):
        body = "\n\n".join(
            ["도입.", "## 하나", "- 먼저 하나.", "- 먼저 둘.", "* 먼저 셋.", "> 먼저 넷."]
        )
        assert "먼저 4회" in warnings_text(body)

    def test_repeating_the_whole_title_in_the_first_paragraph_is_reported(self):
        text = "제습기 관리 순서를 알려드립니다.\n\n## 물통\n\n헹굽니다."
        assert "제목을 첫 문단에서 그대로 반복" in warnings_text(text, title="제습기 관리 순서")

    def test_a_fresh_opening_is_not_reported(self):
        text = "장마가 시작되면 빨래가 마르지 않습니다.\n\n## 물통\n\n헹굽니다."
        assert "제목을 첫 문단에서 그대로 반복" not in warnings_text(text, title="제습기 관리 순서")

    def test_headings_that_all_end_the_same_way_are_reported(self):
        body = "\n\n".join(
            ["도입.", "## 관리하는 방법", "가.", "## 씻는 방법", "나.", "## 고르는 방법", "다."]
        )
        assert "소제목이 모두 같은 어미로 끝납니다" in warnings_text(body)

    def test_mixed_headings_are_not_reported(self):
        body = "\n\n".join(
            ["도입.", "## 관리하는 방법", "가.", "## 필터는 왜 막히나", "나.", "## 오늘 할 일", "다."]
        )
        assert "소제목이 모두 같은 어미" not in warnings_text(body)

    def test_none_of_these_signals_rejects_the_draft(self):
        # 문체 신호는 원고를 버리지 않는다 — 멀쩡한 사실을 문체 때문에 통째로 버리면
        # 사용자가 잃는 것이 더 크다.
        text = "오늘은 제습기 관리에 대해 알아보겠습니다.\n\n## 관리하는 방법\n\n또한 또한 또한 또한 헹굽니다."
        assert report_for(text).ok is True


class TestLengthRevisionInstruction:
    def _short(self):
        body = "\n\n".join(
            ["도입 문장입니다.", "## 짧은 섹션", "한 줄.", "## 긴 섹션", "설명이 조금 더 길게 이어집니다."]
        )
        return check_draft(
            post(body),
            hashtag_count=5,
            min_body_chars=2500,
            max_body_chars=3500,
            photo_count=0,
        )

    def _long(self):
        body = "\n\n".join(
            ["도입.", "## 짧은 섹션", "한 줄.", "## 긴 섹션", "아주 " * 400]
        )
        return check_draft(
            post(body),
            hashtag_count=5,
            min_body_chars=100,
            max_body_chars=300,
            photo_count=0,
        )

    def test_a_short_draft_gets_the_gap_and_the_target_range(self):
        problem = " ".join(self._short().problems)
        assert "목표 2500~3500자" in problem
        assert "자 이상 보강 필요" in problem

    def test_a_short_draft_is_told_which_section_is_shortest(self):
        assert "가장 짧은 섹션은 '짧은 섹션'" in " ".join(self._short().problems)

    def test_a_short_draft_is_told_not_to_invent_experience(self):
        assert "근거 없는 경험이나 수치를 추가하지 않는다" in " ".join(self._short().problems)

    def test_a_long_draft_is_told_which_section_is_longest(self):
        assert "가장 긴 섹션은 '긴 섹션'" in " ".join(self._long().warnings)

    def test_a_long_draft_gets_the_overflow_amount(self):
        assert "자 이상 줄여야 함" in " ".join(self._long().warnings)

    def test_a_long_draft_is_told_to_cut_duplication_not_facts(self):
        text = " ".join(self._long().warnings)
        assert "출처가 있는 수치" in text
        assert "새로운 사실이나 사례를 추가하지 않는다" in text


class TestTheLengthTargetIsGivenAsParagraphs:
    """실측(2026-08-03): 새 목표(1,800~2,300자)로 생성한 5편 중 3편이 상한을 넘었다
    (+136 ~ +365자, 평균 2,285자). 미달은 없었다.

    모델은 글자를 셀 수 없다. 같은 목표를 **셀 수 있는 단위**(문단 수)로도 준다 —
    실측상 문단 하나가 평균 76자였고, 문단이 많은 글이 그대로 긴 글이었다.
    """

    def test_the_paragraph_budget_follows_the_character_target(self):
        assert prompts.article_length_paragraphs(None) == (24, 30)  # 중간 1800~2300
        short = DraftGenerationSettings(hashtag_count=5, article_length="short")
        assert prompts.article_length_paragraphs(short) == (11, 16)  # 짧게 800~1200
