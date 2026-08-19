"""자료 출처 금지 목록(2026-08-11 사용자 지시 — "나무위키나 디씨인사이드 같은 것은 안 돼").

프롬프트에도 적었지만 grounding이 무엇을 물어 올지는 정하지 못하므로 코드로 한 번 더
막는다. 여기서 못박는 것은 '무엇을 막고 무엇을 막지 않는가'다.
"""

from dataclasses import dataclass

import pytest

from app.llm.source_quality import drop_blocked_sources, is_blocked_source


@dataclass
class Source:
    url: str
    title: str = "제목"


class TestBlocked:
    @pytest.mark.parametrize(
        "url",
        [
            "https://namu.wiki/w/%EC%86%90%ED%9D%A5%EB%AF%BC",
            "https://gall.dcinside.com/board/lists/?id=stock",
            "https://m.dcinside.com/board/stock/123",  # 하위 도메인도 막힌다
            "https://www.fmkorea.com/1234567",
            "https://theqoo.net/square/1",
            "https://pann.nate.com/talk/1",
            "https://www.reddit.com/r/korea/comments/x",
        ],
    )
    def test_anonymous_communities_and_fandom_wikis_are_blocked(self, url):
        assert is_blocked_source(url)


class TestAllowed:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.yna.co.kr/view/AKR1",
            "https://n.news.naver.com/article/001/0000000001",
            "https://blog.naver.com/someone/223",
            "https://doi.org/10.1000/xyz",
            "https://arxiv.org/abs/2408.00001",
            # 위키백과는 막지 않는다 — 문장마다 출처를 요구하고 이력이 공개다.
            "https://ko.wikipedia.org/wiki/손흥민",
            "https://www.kostat.go.kr/board",
        ],
    )
    def test_verifiable_sources_pass(self, url):
        assert not is_blocked_source(url)

    def test_a_non_url_is_not_blocked(self):
        """주소가 아니면 판정하지 않는다 — 추측으로 자료를 지우지 않는다."""
        assert not is_blocked_source("")
        assert not is_blocked_source("손흥민 인터뷰")
        assert not is_blocked_source("user-upload://ref-1")


class TestDropping:
    def test_it_keeps_everything_else(self):
        sources = [
            Source("https://www.yna.co.kr/view/AKR1"),
            Source("https://namu.wiki/w/x"),
            Source("https://arxiv.org/abs/2408.00001"),
        ]

        kept = drop_blocked_sources(sources)

        assert [s.url for s in kept] == [
            "https://www.yna.co.kr/view/AKR1",
            "https://arxiv.org/abs/2408.00001",
        ]

    def test_nothing_to_drop_returns_the_same_list(self):
        sources = [Source("https://www.yna.co.kr/view/AKR1")]
        assert drop_blocked_sources(sources) == sources

    def test_it_says_what_it_removed(self, caplog):
        """자료가 적게 잡힐 때 원인이 검색인지 이 필터인지 구분할 수 있어야 한다."""
        import logging

        with caplog.at_level(logging.INFO, logger="app.llm.source_quality"):
            drop_blocked_sources([Source("https://namu.wiki/w/x")], where="수집")

        [record] = [r for r in caplog.records if "자료 출처 제외" in r.getMessage()]
        assert "namu.wiki" in record.getMessage()
