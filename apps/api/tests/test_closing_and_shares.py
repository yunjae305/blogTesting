"""결말 방식 회전(5-4)과 목적별 분량 비중(5-6).

도입에는 축이 있었지만(articleRhythm → RHYTHM_OPENINGS) 결말에는 없어서, 같은 아키타입이면
마지막 문단이 늘 같게 읽혔다. 분량 비중은 실측에서 분량 준수가 4/6이었던 문제에 대응한다 —
짧게 나온 원고를 고칠 때 어디를 늘릴지 알 수 있어야 한다.
"""

from app.llm import prompts
from app.llm.title_variation import CLOSING_MODES, closing_mode
from tests.test_prompt_audit import draft_input


class TestClosingModeRotation:
    def test_the_same_post_and_revision_always_close_the_same_way(self):
        first = closing_mode(post_id="post_1", revision=0, purpose="문제 해결")
        assert closing_mode(post_id="post_1", revision=0, purpose="문제 해결") is first

    def test_regenerating_moves_to_another_closing(self):
        before = closing_mode(post_id="post_1", revision=0, purpose="문제 해결")
        after = closing_mode(post_id="post_1", revision=1, purpose="문제 해결")
        assert before != after

    def test_the_rotation_wraps_instead_of_running_out(self):
        size = len(CLOSING_MODES)
        assert closing_mode(post_id="p", revision=0, purpose="정보 전달") is closing_mode(
            post_id="p", revision=size, purpose="정보 전달"
        )

    def test_a_daily_life_post_never_closes_with_a_checklist_style(self):
        # 일상 기록에 '확인 기준'·'다음 행동'으로 닫으면 정보글로 바뀐다.
        picked = {
            closing_mode(post_id=f"p{index}", revision=0, purpose="일상·경험 공유").name
            for index in range(40)
        }
        assert "확인 기준" not in picked
        assert "다음 행동" not in picked

    def test_a_comparison_post_does_not_close_by_deferring(self):
        picked = {
            closing_mode(post_id=f"p{index}", revision=0, purpose="비교·추천").name
            for index in range(40)
        }
        assert "남은 확인" not in picked

    def test_an_unknown_purpose_can_use_every_closing(self):
        picked = {
            closing_mode(post_id=f"p{index}", revision=r, purpose="없는목적").name
            for index in range(20)
            for r in range(len(CLOSING_MODES))
        }
        assert picked == {mode.name for mode in CLOSING_MODES}


class TestClosingModeInThePrompt:
    def test_the_draft_prompt_carries_exactly_one_closing_mode(self):
        text = prompts.draft_prompt(draft_input())
        assert text.count("- 결말 방식(") == 1

    def test_the_whole_table_is_not_handed_to_the_model(self):
        # 목록을 주면 모델이 매번 같은 것을 고른다. 코드가 고른 하나만 준다.
        text = prompts.draft_prompt(draft_input())
        named = [mode.name for mode in CLOSING_MODES if f"결말 방식({mode.name})" in text]
        assert len(named) == 1

    def test_the_existing_conclusion_ban_is_still_there(self):
        text = prompts.draft_prompt(draft_input())
        assert "결론에서 본문을 그대로 요약하지 않는다" in text


class TestSectionLengthShares:
    def test_every_purpose_share_set_covers_intro_and_closing(self):
        for purpose, shares in prompts.SECTION_LENGTH_SHARES.items():
            joined = " ".join(shares)
            assert "도입" in joined, purpose
            assert "결말" in joined, purpose

    def test_the_shares_differ_by_purpose(self):
        assert prompts.section_length_shares("문제 해결") != prompts.section_length_shares(
            "비교·추천"
        )

    def test_introduction_spends_most_space_on_identity_features_and_first_use(self):
        shares = prompts.section_length_shares("입문·소개")

        assert "무엇인지·배경 20~25%" in shares
        assert "핵심 특징·구성 30~40%" in shares
        assert "쓰임·대상·첫 확인 20~30%" in shares

    def test_an_unknown_purpose_falls_back_instead_of_failing(self):
        assert prompts.section_length_shares("없는목적") == prompts.section_length_shares(
            "정보 전달"
        )

    def test_the_content_plan_prompt_states_it_is_a_direction_not_a_target(self):
        text = prompts.content_plan_prompt(draft_input())
        assert "섹션별 권장 분량 비중" in text
        assert "목표가 아니라 배분 방향이다" in text
        assert "문장을 끊거나 같은 내용을 반복하지 않는다" in text

    def test_the_plan_prompt_uses_the_purpose_of_this_post(self):
        text = prompts.content_plan_prompt(draft_input())
        # draft_input의 목적은 '문제 해결'이다.
        assert "원인·판단 25~35%" in text
        assert "비교 기준 20~30%" not in text
