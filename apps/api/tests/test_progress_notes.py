"""작업 현황에 지금 무슨 일이 벌어지는지 남긴다(2026-08-11 사용자 요청).

"터미널에서 뜨는 내용들을 보이게 해서 사용자들에게 좀 더 자세하게 실시간 작업 현황을
보이게 하고 싶어. 이것은 모든 작업 현황에 다 해당하는 말이야."

여기서 못박는 계약은 두 가지다.

1. **수집기가 흘려보낸 줄이 그대로 화면 통로로 간다.** 자료를 몇 건 보탰는지 같은 사실은
   서버 로거에만 남으면 안 된다.
2. **그 보고가 작업을 죽이지 않는다.** 진행 표시 하나 때문에 검증이 실패해서는 안 되고,
   콜백을 모르는 구형 수집기도 예전처럼 돌아야 한다.
"""

import pytest

from app.llm.live_adapters import _note, _say
from app.modules.blog_task.service import _accepts_note


class Recorder:
    def __init__(self, fail: bool = False):
        self.lines: list[str] = []
        self.fail = fail

    async def __call__(self, message: str) -> None:
        if self.fail:
            raise RuntimeError("보고 실패")
        self.lines.append(message)


class TestNotes:
    @pytest.mark.asyncio
    async def test_it_says_how_many_sources_each_step_added(self):
        note = Recorder()

        await _note(note, 3, "네이버 블로그 실사용 글")
        await _note(note, 4, "관련 최신 기사")

        assert note.lines == [
            "네이버 블로그 실사용 글 3건을 자료에 추가했습니다.",
            "관련 최신 기사 4건을 자료에 추가했습니다.",
        ]

    @pytest.mark.asyncio
    async def test_finding_nothing_is_said_too(self):
        """조용히 넘어가면 사용자는 그 단계가 돌았는지조차 알 수 없다."""
        note = Recorder()

        await _note(note, 0, "관련 최신 기사")

        assert note.lines == ["관련 최신 기사 — 찾지 못했습니다."]

    @pytest.mark.asyncio
    async def test_a_failing_report_does_not_break_collection(self):
        """진행 표시 하나 때문에 검증이 죽어서는 안 된다."""
        await _say(Recorder(fail=True), "무언가")  # 예외가 새어 나오지 않는다

    @pytest.mark.asyncio
    async def test_no_callback_is_fine(self):
        await _say(None, "무언가")
        await _note(None, 3, "무언가")


class TestOldAnalyzersStillWork:
    def test_an_analyzer_without_the_callback_is_detected(self):
        """구형 어댑터·테스트 스텁에 넘기면 TypeError로 검증이 통째로 죽는다."""

        class Old:
            async def search_and_analyze(self, analysis_input, on_collected=None):
                return None

        class New:
            async def search_and_analyze(self, analysis_input, on_collected=None, on_note=None):
                return None

        assert _accepts_note(Old()) is False
        assert _accepts_note(New()) is True

    def test_something_without_a_readable_signature_is_not_forced(self):
        class Weird:
            search_and_analyze = "not callable"

        assert _accepts_note(Weird()) is False
