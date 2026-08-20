"""신기능 페이지를 읽어 **글의 소재**로 만든다(2026-08-20 사용자 결정).

> "신기능이 나오면 그것에 대한 글을 생성하는거고."

이 칸이 있기 전에는 신기능 글을 쓰려면 기능 이름을 사람이 알고 정확히 적어야 했다.
브랜드 자료가 아직 그 기능을 모르면(새로 나왔으니 당연하다) 모델이 이름을 지어내거나
옛 기능으로 대신 썼다. 페이지를 읽어 **적힌 이름 그대로** 소재에 넣으면 그 둘이 함께
없어진다.

붙여넣기 통로가 함께 있는 이유: AIONA 업데이트 공지는 공개 주소로 열어도 목록이
로그인 뒤에만 보인다(`aiona.kr/announcements` HTML 안의 "전체 공지사항은 로그인 후
확인할 수 있습니다"). 그때는 공지 내용을 복사해 넣으면 같은 길로 흐른다.
"""

import pytest

from app.errors import BlogTaskError
from app.llm import FeatureBrief, SiteReadInput
from app.modules.brand import (
    DEFAULT_BRAND_ID,
    BrandService,
    InMemoryBrandRepository,
)
from app.modules.brand.validation import MAX_SITE_READ_URLS, validate_site_read_body
from app.shared import BrandLink, BrandProfile


class FakeReader:
    """읽어 온 척한다. 무엇을 받았는지 기록해 둔다."""

    def __init__(self, feature=None, error=None):
        self.feature = feature or FeatureBrief(name="앱스튜디오")
        self.error = error
        self.seen: list[SiteReadInput] = []

    async def read_feature(self, site_input: SiteReadInput) -> FeatureBrief:
        self.seen.append(site_input)
        if self.error:
            raise self.error
        return self.feature


async def service_with(reader) -> BrandService:
    service = BrandService(InMemoryBrandRepository(), site_reader=reader)
    await service.ensure_default_brands("user_1")
    return service


def profile(*links: str) -> BrandProfile:
    return BrandProfile(
        brand_id="brand_1",
        user_id="user_1",
        name="우리 회사",
        created_at="x",
        updated_at="x",
        links=[BrandLink(label="", url=url) for url in links],
    )


class TestWhatGetsRead:
    def test_the_brands_own_links_are_used_when_none_are_given(self):
        """자기 회사 주소를 매번 다시 적을 이유가 없다 — 이미 자료에 들어 있다."""
        urls, text = validate_site_read_body({}, profile("https://aiona.kr"))

        assert urls == ["https://aiona.kr"]
        assert text == ""

    def test_a_given_url_wins_over_the_registered_ones(self):
        urls, _ = validate_site_read_body(
            {"urls": ["https://aiona.kr/business"]}, profile("https://aiona.kr")
        )

        assert urls == ["https://aiona.kr/business"]

    def test_pasted_text_alone_is_enough(self):
        """로그인 뒤 공지는 서버가 못 연다 — 복사해 넣으면 같은 길로 흐른다."""
        urls, text = validate_site_read_body({"text": "  새 기능이 나왔습니다.  "}, profile())

        assert urls == []
        assert text == "새 기능이 나왔습니다."

    def test_unreadable_addresses_are_refused(self):
        with pytest.raises(BlogTaskError) as caught:
            validate_site_read_body({"urls": ["javascript:alert(1)"]}, profile())

        assert "읽을 수 없는 주소" in caught.value.message

    def test_nothing_to_read_is_refused(self):
        """조용히 빈 제안을 돌려주면 화면은 '페이지에 아무것도 없다'로 읽는다."""
        with pytest.raises(BlogTaskError):
            validate_site_read_body({}, profile())

    def test_too_many_urls_are_trimmed(self):
        urls, _ = validate_site_read_body(
            {"urls": [f"https://aiona.kr/{n}" for n in range(20)]}, profile()
        )

        assert len(urls) == MAX_SITE_READ_URLS


@pytest.mark.asyncio
class TestReadingOneNewFeature:
    async def test_the_feature_name_becomes_the_topic(self):
        """페이지에 적힌 이름 그대로여야 한다 — 이것이 글의 소재가 된다."""
        reader = FakeReader(
            feature=FeatureBrief(
                name="리서치 코파일럿",
                summary="논문을 찾고 비교합니다.",
                highlights=["선행연구 비교"],
                keywords=["논문", "학술"],
            )
        )
        service = await service_with(reader)

        brief = await service.read_feature(
            "user_1", DEFAULT_BRAND_ID, {"urls": ["https://aiona.kr/research"]}
        )

        assert brief["name"] == "리서치 코파일럿"
        assert brief["keywords"] == ["논문", "학술"]

    async def test_it_reads_as_this_brand(self):
        """'이 사이트가 무엇을 파는 곳인가'가 아니라 '이 브랜드에 대해 뭐라 하는가'다."""
        reader = FakeReader()
        service = await service_with(reader)

        await service.read_feature(
            "user_1", DEFAULT_BRAND_ID, {"urls": ["https://aiona.kr/research"]}
        )

        assert reader.seen[0].brand_name == "AIONA"

    async def test_pasted_announcements_work_without_a_url(self):
        """AIONA 공지 목록은 공개 주소로 열어도 로그인 뒤에만 보인다."""
        service = await service_with(FakeReader())

        await service.read_feature(
            "user_1", DEFAULT_BRAND_ID, {"text": "이번 업데이트로 앱스튜디오가 열렸습니다."}
        )

        assert "앱스튜디오" in service._site_reader.seen[0].text

    async def test_a_failure_says_what_to_do_next(self):
        """provider 이름·상태코드를 화면에 띄우지 않는다. 다음에 할 일을 말한다."""
        service = await service_with(FakeReader(error=RuntimeError("gemini 503")))

        with pytest.raises(BlogTaskError) as caught:
            await service.read_feature("user_1", DEFAULT_BRAND_ID, {"text": "공지"})

        assert caught.value.code == "SITE_READ_FAILED"
        assert "gemini" not in caught.value.message
        assert "붙여넣어" in caught.value.message

    async def test_it_says_so_when_the_reader_is_off(self):
        """자격 증명이 없는 서버에서도 나머지는 그대로 돌아야 한다."""
        service = BrandService(InMemoryBrandRepository())
        await service.ensure_default_brands("user_1")

        with pytest.raises(BlogTaskError) as caught:
            await service.read_feature("user_1", DEFAULT_BRAND_ID, {"text": "공지"})

        assert caught.value.code == "SITE_READER_UNAVAILABLE"
