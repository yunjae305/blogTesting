"""두 번째·세 번째 라운드에 **같은 목록을 다시 보여 주지 않는다**(2026-08-12 사용자 요청).

한 소재로 여러 편을 만들 때 제목과 방향을 편마다 고르기로 했다(사용자 결정). 그 방식의
유일한 약점이 "두·세 번째에 거의 같은 목록이 나온다"는 것이라, 앞서 고른 것을 넘겨
다음 생성이 그것을 피하게 한다.
"""

import pytest

from app.llm.prompts import _already_taken_rule


class TestTheFirstRoundIsUntouched:
    @pytest.mark.parametrize("nothing", [None, [], ["", "   "]])
    def test_no_rule_is_added_when_nothing_was_taken(self, nothing):
        """첫 라운드의 프롬프트는 예전과 **한 글자도** 달라지지 않아야 한다."""
        assert _already_taken_rule(nothing) == []


class TestLaterRoundsAvoidWhatWasChosen:
    def test_each_taken_direction_is_listed(self):
        lines = _already_taken_rule(["가격대별 선택 기준", "입문자용 개념 정리"])

        assert any("가격대별 선택 기준" in line for line in lines)
        assert any("입문자용 개념 정리" in line for line in lines)

    def test_the_rule_says_that_rewording_still_counts_as_overlap(self):
        """'말만 바꾼 것'을 막지 않으면 같은 각도가 다른 문장으로 다시 온다."""
        lines = _already_taken_rule(["가격대별 선택 기준"])

        assert any("말만 바꾼" in line for line in lines)

    def test_blank_entries_are_dropped(self):
        lines = _already_taken_rule(["  ", "가격대별 선택 기준", ""])

        assert len([line for line in lines if line.startswith("  ·")]) == 1
