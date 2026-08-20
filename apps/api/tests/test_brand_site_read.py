"""브랜드 사이트를 읽어 자료를 채운다(2026-08-20 사용자 결정).

> "지금처럼 직접 정보를 채워넣어서 저장해두는 형태가 아니고 브랜드 글 쓰기를 하면
>  너가 aiona 홈페이지 링크를 타고 들어가서 정보를 찾고 글을 쓰는 거지."

**저장하지는 않는다.** 읽어 온 것은 제안이고 사람이 확인하고 누른다. 사이트가 말하지
않는 기능 이름이 있기 때문이다 — aiona.kr 첫 화면은 큰 기능 여섯 개만 말하는데 기준표에는
스물여덟 줄이 있다. 통째로 덮으면 그 이름들이 사라지고, 모델은 없어진 이름 대신 지어낸다.
"""

import pytest

from app.errors import BlogTaskError
from app.llm import BrandDraft, FeatureBrief, SiteReadInput
from app.modules.brand import (
    DEFAULT_BRAND_ID,
    BrandService,
    InMemoryBrandRepository,
)
from app.modules.brand.validation import MAX_SITE_READ_URLS, validate_site_read_body
from app.shared import BrandLink, BrandProfile, BrandUseCase


class FakeReader:
    """읽어 온 척한다. 무엇을 받았는지 기록해 둔다."""

    def __init__(self, brand=None, feature=None, error=None):
        self.brand = brand or BrandDraft()
        self.feature = feature or FeatureBrief(name="앱스튜디오")
        self.error = error
        self.seen: list[SiteReadInput] = []

    async def read_brand(self, site_input: SiteReadInput) -> BrandDraft:
        self.seen.append(site_input)
        if self.error:
            raise self.error
        return self.brand

    async def read_feature(self, site_input: SiteReadInput) -> FeatureBrief:
        self.seen.append(site_input)
        if self.error:
            raise self.error
        return self.feature


async def service_with(reader) -> BrandService:
    repository = InMemoryBrandRepository()
    service = BrandService(repository, site_reader=reader)
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
        """조용히 빈 제안을 돌려주면 화면은 '사이트에 아무것도 없다'로 읽는다."""
        with pytest.raises(BlogTaskError):
            validate_site_read_body({}, profile())

    def test_too_many_urls_are_trimmed(self):
        urls, _ = validate_site_read_body(
            {"urls": [f"https://aiona.kr/{n}" for n in range(20)]}, profile()
        )

        assert len(urls) == MAX_SITE_READ_URLS


@pytest.mark.asyncio
class TestReadingTheSite:
    async def test_it_proposes_without_saving(self):
        """읽은 것이 곧바로 저장되면, 사이트에 없는 기능 이름이 조용히 사라진다."""
        reader = FakeReader(
            brand=BrandDraft(
                description="통합 AI 업무 플랫폼입니다.",
                features="AI 채팅, 앱스튜디오",
                use_cases=[
                    BrandUseCase(situation="앱을 만들고 싶을 때", feature="앱스튜디오", keywords=[])
                ],
                read_urls=["https://aiona.kr"],
            )
        )
        service = await service_with(reader)

        proposed = await service.read_site("user_1", DEFAULT_BRAND_ID, {})

        assert proposed["description"] == "통합 AI 업무 플랫폼입니다."
        assert proposed["useCases"][0]["feature"] == "앱스튜디오"
        # 저장은 일어나지 않았다 — 기준표는 기본값 그대로다.
        saved = await service.get_brand("user_1", DEFAULT_BRAND_ID)
        assert len(saved.use_cases) > 1
        assert saved.description != "통합 AI 업무 플랫폼입니다."

    async def test_it_reads_the_brands_registered_links(self):
        reader = FakeReader()
        service = await service_with(reader)

        await service.read_site("user_1", DEFAULT_BRAND_ID, {})

        assert "https://aiona.kr" in reader.seen[0].urls
        assert reader.seen[0].brand_name == "AIONA"

    async def test_a_failure_says_what_to_do_next(self):
        """provider 이름·상태코드를 화면에 띄우지 않는다. 다음에 할 일을 말한다."""
        service = await service_with(FakeReader(error=RuntimeError("gemini 503")))

        with pytest.raises(BlogTaskError) as caught:
            await service.read_site("user_1", DEFAULT_BRAND_ID, {})

        assert caught.value.code == "SITE_READ_FAILED"
        assert "gemini" not in caught.value.message
        assert "붙여넣어" in caught.value.message

    async def test_it_says_so_when_the_reader_is_off(self):
        """자격 증명이 없는 서버에서도 나머지는 그대로 돌아야 한다."""
        repository = InMemoryBrandRepository()
        service = BrandService(repository)
        await service.ensure_default_brands("user_1")

        with pytest.raises(BlogTaskError) as caught:
            await service.read_site("user_1", DEFAULT_BRAND_ID, {})

        assert caught.value.code == "SITE_READER_UNAVAILABLE"


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

    async def test_pasted_announcements_work_without_a_url(self):
        """AIONA 공지 목록은 공개 주소로 열어도 로그인 뒤에만 보인다."""
        reader = FakeReader(feature=FeatureBrief(name="앱스튜디오"))
        service = await service_with(reader)

        await service.read_feature(
            "user_1", DEFAULT_BRAND_ID, {"text": "이번 업데이트로 앱스튜디오가 열렸습니다."}
        )

        assert reader.seen[0].urls == []
        assert "앱스튜디오" in reader.seen[0].text
