"""실제 영상 콘텐츠 글의 사실 고정과 이미지 경로(2026-08-03).

재현 사례: 소재 '전과자' + 검색어 '창섭 전과자'로 쓴 글이

- '창섭 전과자는 …'처럼 검색어 조합을 문장 속 명사로 썼고,
- 학식·배식 줄 같은 보조 장면을 프로그램의 핵심 포맷처럼 설명했고,
- 시청 경험 자료가 없는데 재생 시점·소리·분위기를 1인칭으로 지어냈고,
- 실제 프로그램인데 일반 강의실 생성 이미지를 대표로 실었다.

여기서는 그 넷을 각각 잡는 검사와, 공식 유튜브 썸네일이 생성 이미지보다 먼저 쓰이는
파이프라인을 확인한다.
"""


from app.shared import BlogTaskInput, TrendSelection

from app.modules.draft.content_validation import (
    run_content_validations,
    validate_official_thumbnail_used,
    validate_program_format_grounding,
    validate_raw_keyword_grammar,
    validate_secondary_activity_emphasis,
)
from app.shared import (
    ContentEntityProfile,
    FinalPost,
    GeneratedPostImage,
    ReferenceEvidenceProfile,
    RelatedPerson,
    SeoKeywordPlan,
    WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL,
    WebPhoto,
)

NOW = "2026-08-03T00:00:00.000Z"

PROGRAM = ContentEntityProfile(
    entity_type="YOUTUBE_PROGRAM",
    canonical_name="전과자",
    raw_keyword="창섭 전과자",
    platform="YouTube",
    official_channel="전과자 공식 채널",
    related_people=[RelatedPerson(name="이창섭", relation="출연자")],
    core_format="여러 대학의 학과를 찾아가 강의와 실습에 참여하는 학과 체험형 웹예능",
    primary_activities=["학과 방문", "강의 참여", "실습 참여"],
    secondary_activities=["학식 체험", "캠퍼스 이동"],
    background_scenes=["배식 줄"],
    natural_phrases=["유튜브 웹예능 전과자"],
    forbidden_phrases=["창섭 전과자"],
    confidence=0.9,
)

GOOD_LEAD = (
    "유튜브 웹예능 전과자는 이창섭이 여러 대학의 학과를 찾아가 실제 강의와 실습에 "
    "참여하며 전공의 특징을 보여주는 프로그램입니다. 학식이나 캠퍼스 일상도 회차에 따라 "
    "등장하지만, 중심은 해당 학과의 수업과 전공 체험입니다."
)
GOOD_TITLE = "이창섭이 대학 수업을 직접 듣는 유튜브 웹예능 전과자"
FILLER = "\n\n".join(
    f"{n}번 문단입니다. 회차마다 다른 학과의 수업 방식과 실습 내용을 살펴봅니다."
    for n in range(1, 6)
)


def post(title=GOOD_TITLE, lead=GOOD_LEAD, heading="회차마다 반복되는 학과 체험 방식"):
    markdown = f"# {title}\n\n{lead}\n\n## {heading}\n\n{FILLER}"
    body = f"{lead}\n\n{FILLER}"
    return FinalPost(
        title=title,
        body=body,
        hashtags=["전과자"],
        html_content=f"<article><h1>{title}</h1><p>{lead}</p></article>",
        markdown_content=markdown,
    )


def evidence(entity=PROGRAM, experience=False) -> ReferenceEvidenceProfile:
    return ReferenceEvidenceProfile(
        has_references=True,
        has_user_experience_evidence=experience,
        content_entity=entity,
    )


class TestRawKeywordGrammar:
    """A. '창섭 전과자는' 같은 문장이 나오지 않는다."""

    def test_a_combination_used_as_a_noun_fails(self):
        bad = post(
            title="창섭 전과자 완벽 정리",
            lead="창섭 전과자는 출연자가 대학 강의를 직접 듣는 현장형 웹예능입니다.",
        )
        result = validate_raw_keyword_grammar(bad, ["창섭 전과자"], PROGRAM)
        assert result.status == "FAIL"
        assert "창섭 전과자는" in result.details["phrases"]

    def test_a_natural_relationship_sentence_passes(self):
        result = validate_raw_keyword_grammar(post(), ["창섭 전과자"], PROGRAM)
        assert result.status == "PASS"

    def test_a_normal_proper_noun_keyword_is_not_checked(self):
        """C. 이번 변경 때문에 멀쩡한 키워드가 막히면 안 된다."""
        phone = ContentEntityProfile(
            entity_type="PRODUCT_OR_SERVICE", canonical_name="아이폰17"
        )
        result = validate_raw_keyword_grammar(
            post(title="아이폰17 출시일", lead="아이폰17은 9월에 공개될 예정입니다."),
            ["아이폰17"],
            phone,
        )
        assert result.status == "PASS"
        assert result.details["checkedKeywords"] == []

    def test_without_the_raw_keyword_the_check_is_skipped(self):
        """옛 문서(선택 키워드 문자열 없음)는 예전과 같이 동작한다."""
        assert validate_raw_keyword_grammar(post(), [], PROGRAM).status == "SKIPPED"

    def test_without_the_entity_the_check_is_skipped(self):
        assert (
            validate_raw_keyword_grammar(post(), ["창섭 전과자"], None).status == "SKIPPED"
        )


class TestProgramFormatGrounding:
    """첫 문단에서 프로그램의 핵심 포맷이 설명된다."""

    def test_a_grounded_lead_passes(self):
        assert validate_program_format_grounding(post(), PROGRAM).status == "PASS"

    def test_a_lead_without_the_format_warns(self):
        vague = post(
            title="요즘 화제인 콘텐츠 한 편",
            lead="요즘 사람들이 많이 찾아보는 콘텐츠가 하나 있습니다. 반응이 좋습니다.",
        )
        result = validate_program_format_grounding(vague, PROGRAM)
        assert result.status == "WARN"
        assert "canonicalName" in result.details["missing"]

    def test_a_non_media_topic_is_skipped(self):
        phone = ContentEntityProfile(
            entity_type="PRODUCT_OR_SERVICE", canonical_name="아이폰17"
        )
        assert validate_program_format_grounding(post(), phone).status == "SKIPPED"
        assert validate_program_format_grounding(post(), None).status == "SKIPPED"


class TestSecondaryActivityEmphasis:
    """학식이 프로그램의 핵심 포맷처럼 설명되면 안 된다."""

    def test_side_scenes_taking_every_slot_warns(self):
        bad = post(
            title="창섭 전과자 학식 먹방",
            lead="학식 배식 줄에 선 모습이 이 콘텐츠의 얼굴입니다. 캠퍼스 이동도 자주 나옵니다.",
            heading="학식은 어떻게 나올까",
        )
        result = validate_secondary_activity_emphasis(bad, PROGRAM)
        assert result.status == "WARN"
        assert set(result.details["secondaryOnlySlots"]) == {
            "title",
            "lead",
            "firstHeading",
        }

    def test_mentioning_a_side_scene_alongside_the_core_format_passes(self):
        assert validate_secondary_activity_emphasis(post(), PROGRAM).status == "PASS"

    def test_without_a_core_secondary_split_it_is_skipped(self):
        bare = PROGRAM.model_copy(
            update={"secondary_activities": [], "background_scenes": []}
        )
        assert validate_secondary_activity_emphasis(post(), bare).status == "SKIPPED"


class TestSemanticSeoKeyword:
    """SEO Primary 검증이 연속 문자열이 아니라 의미로 판정한다."""

    def test_a_natural_title_and_lead_pass(self):
        result = run_content_validations(
            post(),
            SeoKeywordPlan(primary="창섭 전과자"),
            [],
            [],
            evidence=evidence(),
            raw_keywords=["창섭 전과자"],
        )
        by_check = {c.check: c for c in result.checks}
        assert by_check["seo_primary_in_title"].status == "PASS"
        assert by_check["seo_primary_in_first_paragraph"].status == "PASS"
        assert by_check["raw_keyword_grammar"].status == "PASS"
        assert result.has_fail is False

    def test_the_bad_article_is_rejected_as_a_whole(self):
        bad = post(
            title="창섭 전과자 학식 먹방",
            lead="창섭 전과자는 학식까지 따라가는 현장형 웹예능입니다. 밤늦게 재생 버튼을 눌렀는데 웅성거림이 들렸습니다.",
            heading="학식은 어떻게 나올까",
        )
        result = run_content_validations(
            bad,
            SeoKeywordPlan(primary="창섭 전과자"),
            [],
            [],
            evidence=evidence(),
            raw_keywords=["창섭 전과자"],
        )
        by_check = {c.check: c for c in result.checks}
        assert by_check["raw_keyword_grammar"].status == "FAIL"
        assert by_check["secondary_activity_emphasis"].status == "WARN"
        assert result.has_fail is True

    def test_an_article_without_entity_info_behaves_exactly_as_before(self):
        """옛 문서·구형 어댑터: 새 검사는 전부 SKIPPED이고 반려도 없다."""
        result = run_content_validations(
            post(), SeoKeywordPlan(primary="전과자"), [], []
        )
        by_check = {c.check: c for c in result.checks}
        assert by_check["raw_keyword_grammar"].status == "SKIPPED"
        assert by_check["program_format_grounding"].status == "SKIPPED"
        assert by_check["secondary_activity_emphasis"].status == "SKIPPED"


class TestOfficialThumbnailUsed:
    """대표 이미지가 **공식 영상 썸네일인가**를 사진의 출처 유형으로 판정한다.

    'source == web'으로 판정하면 안 된다: 병합 후에는 모든 카드가 네이버 검색을 먼저
    타므로 일반 웹 사진도 web이고, 그러면 공식 썸네일을 못 구한 글이 조용히 통과한다.
    """

    def image(self, source: str) -> GeneratedPostImage:
        return GeneratedPostImage(
            data_url="data:image/jpeg;base64,AAAA",
            alt_text="대표",
            prompt="p",
            provider="web-photo" if source == "web" else "openai",
            model="youtube.com" if source == "web" else "gpt-image-2",
            generated_at=NOW,
            mime_type="image/jpeg",
            source=source,
        )

    def photo(self, source_type: str) -> WebPhoto:
        return WebPhoto(
            data_url="data:image/jpeg;base64,AAAA",
            source_url="https://www.youtube.com/watch?v=vid_0",
            source_host="youtube.com",
            source_type=source_type,
        )

    def test_the_official_thumbnail_passes(self):
        final = post().model_copy(update={"featured_image": self.image("web")})
        result = validate_official_thumbnail_used(
            final, PROGRAM, self.photo(WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL)
        )
        assert result.status == "PASS"

    def test_a_generated_cover_fails(self):
        final = post().model_copy(update={"featured_image": self.image("generated")})
        assert validate_official_thumbnail_used(final, PROGRAM, None).status == "FAIL"

    def test_a_plain_web_photo_also_fails(self):
        """네이버에서 온 사진도 source=='web'이다 — 그것만으로 통과시키면 신호가 죽는다."""
        final = post().model_copy(update={"featured_image": self.image("web")})
        result = validate_official_thumbnail_used(final, PROGRAM, self.photo("WEB_IMAGE"))
        assert result.status == "FAIL"
        assert result.details["photoSourceType"] == "WEB_IMAGE"

    def test_non_program_topics_are_skipped(self):
        final = post().model_copy(update={"featured_image": self.image("generated")})
        assert validate_official_thumbnail_used(final, None, None).status == "SKIPPED"


# --- 파이프라인: 공식 썸네일이 생성 이미지보다 먼저다 -------------------------
#
# 카드 파이프라인 스텁(원고·이미지 생성기·서비스 빌더)은 이미 있는 것을 그대로 쓴다 —
# 같은 규격을 두 벌 만들면 한쪽만 고쳐질 때 조용히 갈린다.

from test_card_pipeline import (  # noqa: E402
    CLAIM_1,
    CLAIM_2,
    DRAFT,
    SceneImageGenerator,
    brief,
    build_card_service,
    plan,
)
from test_draft_service import build_task  # noqa: E402


class FakeThumbnailSearch:
    """공식 영상 썸네일 검색 스텁.

    계약은 팀원의 ``YouTubeThumbnailSearch``와 같다: ``PhotoSearch``의
    ``find_photos(query, limit)``에 공식 회차 채점용 키워드 인자가 더해진 형태다.
    무엇을 어떤 근거로 물었는지가 이 테스트들의 관심사다.
    """

    def __init__(self, available: int = 1):
        self._available = available
        self.calls: list[dict] = []

    async def find_photos(
        self,
        query: str,
        limit: int = 1,
        *,
        program_name: str = "",
        person_names=None,
        official_channel: str = "",
    ):
        self.calls.append(
            {
                "query": query,
                "limit": limit,
                "program_name": program_name,
                "person_names": list(person_names or []),
                "official_channel": official_channel,
            }
        )
        return [
            WebPhoto(
                data_url=_jpeg_data_url(index),
                # 팀원 규약: 출처는 썸네일 파일이 아니라 영상 페이지다.
                source_url=f"https://www.youtube.com/watch?v=vid_{index}",
                source_host="youtube.com",
                title=f"{program_name or query} EP.{index}",
                width=1280,
                height=720,
                query=query,
                meets_spec=True,
                source_type=WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL,
                channel_title=f"{program_name or query} 공식 채널",
                video_id=f"vid_{index}",
            )
            for index in range(min(self._available, limit))
        ]


def _jpeg_data_url(index: int) -> str:
    import base64
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (128, 72), (20 + index * 40, 90, 140)).save(buffer, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class EntityAwareGenerator:
    """소재 정체까지 돌려주는 M4 스텁. 실제 어댑터와 같은 자리에서 프로필을 채운다."""

    def __init__(self, card_plan, entity=PROGRAM):
        self._plan = card_plan
        self._entity = entity
        self.result = DRAFT
        self.evidence_calls: list = []

    async def generate_draft(self, draft_input):
        return self.result

    async def generate_reference_evidence(self, draft_input):
        self.evidence_calls.append(draft_input)
        return ReferenceEvidenceProfile(
            has_references=True,
            has_user_experience_evidence=False,
            content_entity=self._entity,
        )

    async def generate_visual_card_plan(
        self, draft_input, final_post, rendered_visual_count, reference_image_count
    ):
        return self._plan


def program_task():
    """유튜브 프로그램 소재의 글. 사용자가 고른 원본 검색어까지 함께 저장돼 있다."""
    return build_task(
        input=BlogTaskInput(topic="전과자", keywords=["창섭 전과자"]),
        trend_selection=TrendSelection(
            topic_candidate_id="topic_1",
            final_topic=DRAFT.final_post.title,
            selected_trend_keyword_ids=["trend_1"],
            selected_keywords=["창섭 전과자"],
            skipped=False,
            selected_at=NOW,
        ),
    )


class TestOfficialThumbnailPipeline:
    async def test_the_cover_is_the_official_thumbnail_not_a_generated_image(self):
        thumbnails = FakeThumbnailSearch(available=1)
        images = SceneImageGenerator()
        service, repository = build_card_service(
            EntityAwareGenerator(plan(brief("cover", card_type="THUMBNAIL", section_id=None))),
            images,
            youtube_search=thumbnails,
        )
        await repository.create(program_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        # 정식 명칭 + 출연자로 묻고, 채점 근거(정식명·출연자·공식 채널)를 함께 넘긴다.
        first = thumbnails.calls[0]
        assert first["query"] == "전과자 이창섭"
        assert first["program_name"] == "전과자"
        # 짧은 이름(원본 검색어)도 제목 앵커로 함께 넘긴다(2026-08-10) — 정식 명칭이
        # 길어 제목에 통째로 안 담기는 콘텐츠에서 후보 전멸을 막는다.
        assert first["person_names"] == ["이창섭", "창섭 전과자"]
        assert first["official_channel"] == "전과자 공식 채널"
        cover_call = images.calls[0]
        assert cover_call.web_photo is not None
        assert cover_call.web_photo.source_type == WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL
        assert cover_call.web_photo.source_url == "https://www.youtube.com/watch?v=vid_0"

    async def test_no_title_box_is_composited_on_an_official_thumbnail(self):
        thumbnails = FakeThumbnailSearch(available=1)
        images = SceneImageGenerator()
        service, repository = build_card_service(
            EntityAwareGenerator(plan(brief("cover", card_type="THUMBNAIL", section_id=None))),
            images,
            youtube_search=thumbnails,
        )
        await repository.create(program_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.draft_generation_result.thumbnail_layout_plan.show_copy is False
        assert updated.draft_generation_result.thumbnail_layout_plan.copy_lines == []
        assert updated.final_post.thumbnail_copy == []
        assert images.calls[0].thumbnail_copy == []

    async def test_body_cards_get_different_episodes(self):
        """같은 영상의 썸네일을 반복해서 쓰지 않는다."""
        thumbnails = FakeThumbnailSearch(available=3)
        images = SceneImageGenerator()
        service, repository = build_card_service(
            EntityAwareGenerator(
                plan(
                    brief("cover", card_type="THUMBNAIL", section_id=None),
                    brief("photo-1", section_id="section-1", claim=CLAIM_1),
                    brief("photo-2", section_id="section-2", claim=CLAIM_2),
                )
            ),
            images,
            youtube_search=thumbnails,
        )
        await repository.create(program_task())

        await service.generate_draft("post_1", {})

        video_ids = [call.web_photo.video_id for call in images.calls if call.web_photo]
        assert video_ids == ["vid_0", "vid_1", "vid_2"]

    async def test_no_official_thumbnail_falls_back_to_generation(self):
        """공식 썸네일을 못 구하면 기존 이미지 생성 경로가 그대로 돈다."""
        thumbnails = FakeThumbnailSearch(available=0)
        images = SceneImageGenerator()
        service, repository = build_card_service(
            EntityAwareGenerator(plan(brief("cover", card_type="THUMBNAIL", section_id=None))),
            images,
            youtube_search=thumbnails,
        )
        await repository.create(program_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert images.calls[0].web_photo is None
        # 생성 썸네일에는 예전 규격대로 제목 박스가 얹힌다.
        assert updated.draft_generation_result.thumbnail_layout_plan.show_copy is True
        assert updated.final_post.thumbnail_copy

    async def test_a_non_program_topic_searches_without_official_grounding(self):
        general = ContentEntityProfile(entity_type="GENERAL_TOPIC", canonical_name="캠핑")
        thumbnails = FakeThumbnailSearch(available=1)
        images = SceneImageGenerator()
        service, repository = build_card_service(
            EntityAwareGenerator(
                plan(brief("cover", card_type="THUMBNAIL", section_id=None)), entity=general
            ),
            images,
            youtube_search=thumbnails,
        )
        await repository.create(program_task())

        await service.generate_draft("post_1", {})

        # 일반 소재도 검색은 탄다(2026-08-03 사용자 결정: 모든 이미지는 검색이 먼저).
        # 다만 대조할 정식 명칭이 없으므로 공식 회차 채점 근거는 넘기지 않는다 —
        # 그래야 멀쩡한 영상이 제외 목록에 걸려 떨어지지 않는다.
        assert thumbnails.calls
        assert all(call["program_name"] == "" for call in thumbnails.calls)

    async def test_a_search_failure_does_not_break_the_article(self):
        class _Broken:
            async def find_photos(self, *args, **kwargs):
                raise RuntimeError("quota exceeded")

        images = SceneImageGenerator()
        service, repository = build_card_service(
            EntityAwareGenerator(plan(brief("cover", card_type="THUMBNAIL", section_id=None))),
            images,
            youtube_search=_Broken(),
        )
        await repository.create(program_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert images.calls[0].web_photo is None


