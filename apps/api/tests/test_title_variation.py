"""제목 후보의 역할 배정과 재생성 방향 — 난수가 아니라 코드가 정한다.

실측 기준선에서 31사례 중 24건이 다섯 후보 중 최소 한 쌍이 같은 말로 시작했다. "다양하게
쓰라"는 열린 지시로는 갈리지 않는다는 뜻이고, 그래서 역할 배정과 금지 목록을 코드로 옮겼다.
여기서 검증하는 것은 그 장치가 실제로 프롬프트에 들어가는지다.
"""

from app.llm import prompts
from app.llm.contracts import ExcludedAngle, TitleEvaluationInput, TopicGenerationInput
from app.llm.title_variation import (
    CANDIDATE_ROLES,
    REGENERATION_DIRECTIONS,
    regeneration_direction,
    roles_for_purpose,
)
from app.shared import BlogTaskInput, DraftGenerationSettings, TrendKeyword, TrendSource


def keyword(keyword_id: str = "kw_1") -> TrendKeyword:
    return TrendKeyword(
        trend_keyword_id=keyword_id,
        keyword="장마 습도",
        source=TrendSource.NAVER_DATALAB,
        rank=1,
        score=90.0,
        collected_at="2026-07-30T00:00:00Z",
    )


def topic_input(
    *,
    purpose: str = "비교·추천",
    exclude_titles: list[str] | None = None,
    exclude_angles: list[ExcludedAngle] | None = None,
    regeneration_count: int = 0,
    keyword_id: str = "kw_1",
) -> TopicGenerationInput:
    return TopicGenerationInput(
        post_id="post_1",
        input=BlogTaskInput(
            topic="제습기 관리",
            purpose=[purpose],
            keywords=["제습기 청소"],
            target_reader="1인 가구",
        ),
        trend_keyword=keyword(keyword_id),
        settings=DraftGenerationSettings(hashtag_count=5, default_persona="블로거다."),
        exclude_titles=exclude_titles or [],
        exclude_angles=exclude_angles or [],
        regeneration_count=regeneration_count,
    )


class TestRoles:
    def test_a_comparison_role_is_only_offered_when_the_purpose_allows_comparison(self):
        with_comparison = roles_for_purpose(["NONE", "COMPARISON"], count=5)
        without = roles_for_purpose(["NONE", "STORY"], count=5)
        assert any(role.label == "비교 기준" for role in with_comparison)
        assert not any(role.label == "비교 기준" for role in without)

    def test_roles_without_a_hook_requirement_are_always_available(self):
        roles = roles_for_purpose(["NONE"], count=5)
        assert [role.label for role in roles] == ["독자 상황", "구체적 효익", "핵심 차이"]

    def test_the_count_caps_the_list(self):
        assert len(roles_for_purpose(["NONE", "COMPARISON", "REVERSAL"], count=2)) == 2

    def test_every_role_label_is_distinct(self):
        labels = [role.label for role in CANDIDATE_ROLES]
        assert len(labels) == len(set(labels))


class TestRegenerationDirection:
    def test_the_first_generation_has_no_direction(self):
        assert regeneration_direction(seed_key="kw_1", regeneration_count=0) is None

    def test_the_same_keyword_and_count_always_pick_the_same_direction(self):
        first = regeneration_direction(seed_key="kw_1", regeneration_count=3)
        again = regeneration_direction(seed_key="kw_1", regeneration_count=3)
        assert first is not None and first is again

    def test_the_next_press_moves_to_another_direction(self):
        one = regeneration_direction(seed_key="kw_1", regeneration_count=1)
        two = regeneration_direction(seed_key="kw_1", regeneration_count=2)
        assert one != two

    def test_two_keywords_do_not_have_to_share_the_rotation(self):
        picks = {
            regeneration_direction(seed_key=f"kw_{index}", regeneration_count=1).name
            for index in range(len(REGENERATION_DIRECTIONS) * 3)
        }
        # 해시 기반이라 여러 키워드에 걸쳐 여러 방향이 나온다(한 방향에 고정되지 않는다).
        assert len(picks) > 1

    def test_the_rotation_wraps_instead_of_running_out(self):
        total = len(REGENERATION_DIRECTIONS)
        assert regeneration_direction(
            seed_key="kw_1", regeneration_count=1
        ) is regeneration_direction(seed_key="kw_1", regeneration_count=1 + total)


class TestTopicPrompt:
    def test_the_duplicate_bans_name_the_opening_expression_first(self):
        text = prompts.topic_prompt(topic_input())
        assert "후보 사이에 다음이 겹치면 실패다" in text
        assert "같은 시작 표현" in text
        assert "같은 titleType" in text
        assert "조사·어미만 바꾼 같은 문장" in text
        assert "질문형과 평서형으로만" in text

    def test_the_hook_type_ban_does_not_contradict_the_positional_rule(self):
        # 1·2번은 배분 규칙이 둘 다 NONE으로 지정한다. hookType 중복 금지를 전체에 걸면
        # 프롬프트가 스스로 모순되므로 3·4·5번으로 한정한다는 것이 문장에 드러나야 한다.
        text = prompts.topic_prompt(topic_input())
        assert "3·4·5번 사이의 같은 hookType" in text
        assert "1번, 2번: 후킹 없는 기본 제목(hookType=NONE" in text

    def test_the_prompt_never_asks_for_creativity_in_the_abstract(self):
        text = prompts.topic_prompt(topic_input())
        for banned in ("창의적으로", "다양하게 만들", "매번 새롭게"):
            assert banned not in text

    def test_roles_are_listed_and_filtered_by_purpose(self):
        comparison = prompts.topic_prompt(topic_input(purpose="비교·추천"))
        story = prompts.topic_prompt(topic_input(purpose="일상·경험 공유"))
        assert "비교 기준:" in comparison
        assert "비교 기준:" not in story

    def test_no_direction_line_on_the_first_generation(self):
        assert "이번 재생성은" not in prompts.topic_prompt(topic_input())

    def test_the_direction_is_named_not_numbered(self):
        text = prompts.topic_prompt(topic_input(regeneration_count=1))
        direction = regeneration_direction(seed_key="kw_1", regeneration_count=1)
        assert f"이번 재생성은 '{direction.name}'" in text
        assert direction.meaning in text
        # seed 숫자는 프롬프트에 넣지 않는다 — 모델에게 숫자는 아무 뜻이 없다.
        assert "seed" not in text.lower()

    def test_excluded_titles_carry_their_hook_type_and_opening(self):
        text = prompts.topic_prompt(
            topic_input(
                exclude_titles=["제습기 물통 냄새를 잡는 관리 순서"],
                exclude_angles=[
                    ExcludedAngle(
                        title="제습기 물통 냄새를 잡는 관리 순서",
                        hook_type="CURIOSITY",
                        title_type="정보형",
                    )
                ],
            )
        )
        assert "후킹 CURIOSITY" in text
        assert "유형 정보형" in text
        assert "시작 '제습기 물통'" in text

    def test_an_old_client_that_sends_only_titles_still_works(self):
        text = prompts.topic_prompt(topic_input(exclude_titles=["옛 제목 하나"]))
        assert "- 옛 제목 하나" in text
        assert "후킹 " not in text.split("이미 사용한 관점")[1].split("\n\n")[0]

    def test_nothing_excluded_says_so(self):
        assert "이미 사용한 관점" in prompts.topic_prompt(topic_input())


class TestTitleEvaluationPrompt:
    def _prompt(self, exclude_titles: list[str] | None = None) -> str:
        return prompts.title_evaluation_prompt(
            TitleEvaluationInput(
                input=BlogTaskInput(topic="제습기 관리", purpose=["정보 전달"], keywords=["제습기"]),
                trend_keyword=keyword(),
                titles=["제습기 관리 순서를 정리했습니다"],
                exclude_titles=exclude_titles or [],
            )
        )

    def test_the_eight_steps_are_in_order(self):
        text = self._prompt()
        positions = [text.index(f"{step})") for step in range(1, 9)]
        assert positions == sorted(positions)

    def test_each_axis_has_a_numeric_anchor_instead_of_only_an_adjective(self):
        text = self._prompt()
        # '자연스럽게 담았는가' 같은 형용사만으로는 채점자가 매번 다른 기준을 쓴다.
        assert "80 이상" in text
        assert "50 이하" in text
        assert "제목이 소재를 얼마나 정확히·자연스럽게 담았는가" not in text

    def test_missing_information_is_not_guessed(self):
        assert "제공되지 않은 정보는 추정하지 않는다" in self._prompt()

    def test_previous_titles_are_shown_so_a_repeated_angle_scores_lower(self):
        text = self._prompt(["직전에 쓴 제목"])
        assert "이전 후보(관점이 겹치면" in text
        assert "- 직전에 쓴 제목" in text
