"""test.ts."""

import pytest

from app.errors import BlogTaskError
from app.modules.blog_task.validation import validate_create_blog_task_request
from app.shared import BlogTask, BlogTaskStatus, can_transition

VALID_BODY = {
    "userId": "user_1",
    "topic": "제로트러스트 보안",
    "purpose": ["실무 적용", "의사결정 지원"],
    "referenceMaterials": [{"type": "URL", "value": "https://example.com/article"}],
}


def test_accepts_a_well_formed_request():
    result = validate_create_blog_task_request(VALID_BODY)

    assert result.user_id == "user_1"
    assert result.input.keywords == VALID_BODY["purpose"]
    assert len(result.input.reference_materials) == 1


def test_accepts_legacy_keywords_and_stores_them_as_purpose():
    body = {k: v for k, v in VALID_BODY.items() if k != "purpose"}

    result = validate_create_blog_task_request({**body, "keywords": ["ZTNA", "원격근무"]})

    assert result.input.purpose == ["ZTNA", "원격근무"]
    assert result.input.keywords == ["ZTNA", "원격근무"]


def test_accepts_reader_context_and_text_reference_materials():
    result = validate_create_blog_task_request(
        {
            **VALID_BODY,
            "subject": "IT·디지털",
            "targetReader": "마케터",
            "readerAgeRange": "30-39",
            "readerKnowledgeLevel": "practical",
            "referenceMaterials": [{"type": "TEXT", "value": "현업에서 자주 묻는 질문"}],
        }
    )

    assert result.input.subject == "IT·디지털"
    assert result.input.target_reader == "마케터"
    assert result.input.reader_age_range == "30-39"
    assert result.input.reader_knowledge_level == "practical"
    assert result.input.reference_materials[0].type == "TEXT"


def test_defaults_reference_materials_to_an_empty_list():
    body = {k: v for k, v in VALID_BODY.items() if k != "referenceMaterials"}

    result = validate_create_blog_task_request(body)

    assert result.input.reference_materials == []


def test_rejects_a_missing_topic():
    body = {k: v for k, v in VALID_BODY.items() if k != "topic"}

    with pytest.raises(BlogTaskError):
        validate_create_blog_task_request(body)


def test_rejects_an_empty_purpose_array():
    with pytest.raises(BlogTaskError):
        validate_create_blog_task_request({**VALID_BODY, "purpose": []})


def test_rejects_more_than_10_keywords():
    with pytest.raises(BlogTaskError):
        validate_create_blog_task_request(
            {**VALID_BODY, "purpose": [f"k{i}" for i in range(11)]}
        )


def test_rejects_an_unknown_reference_material_type():
    with pytest.raises(BlogTaskError):
        validate_create_blog_task_request(
            {**VALID_BODY, "referenceMaterials": [{"type": "VIDEO", "value": "x"}]}
        )


def test_rejects_a_url_reference_material_with_an_invalid_url():
    with pytest.raises(BlogTaskError):
        validate_create_blog_task_request(
            {**VALID_BODY, "referenceMaterials": [{"type": "URL", "value": "not-a-url"}]}
        )


def test_rejects_non_http_reference_urls():
    with pytest.raises(BlogTaskError):
        validate_create_blog_task_request(
            {
                **VALID_BODY,
                "referenceMaterials": [{"type": "URL", "value": "file:///etc/passwd"}],
            }
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.com/report",
        "https://127.0.0.1/report",
        "https://127.1/report",
        "https://0177.0.0.1/report",
        "https://0x7f.0.0.1/report",
        "https://[::1]/report",
        "https://service.internal/report",
        "https://example.com/report?access_token=secret",
        "https://example.com/report#code=temporary-secret",
        "https://example.com/report?X-Amz-Signature=signed",
    ],
)
def test_rejects_private_or_secret_bearing_reference_urls(url: str):
    with pytest.raises(BlogTaskError):
        validate_create_blog_task_request(
            {**VALID_BODY, "referenceMaterials": [{"type": "URL", "value": url}]}
        )


def test_rejects_oversized_text_material():
    with pytest.raises(BlogTaskError):
        validate_create_blog_task_request(
            {
                **VALID_BODY,
                "referenceMaterials": [{"type": "TEXT", "value": "x" * 16_001}],
            }
        )


def test_accepts_named_pdf_data_url_and_rejects_wrong_mime():
    import base64

    encoded = base64.b64encode(b"small pdf").decode()
    result = validate_create_blog_task_request(
        {
            **VALID_BODY,
            "referenceMaterials": [
                {"type": "PDF", "name": "report.pdf", "value": f"data:application/pdf;base64,{encoded}"}
            ],
        }
    )
    assert result.input.reference_materials[0].name == "report.pdf"

    # PDF 타입에 이미지 mime을 넣는 것처럼, 타입과 mime이 어긋나면 거부한다.
    with pytest.raises(BlogTaskError):
        validate_create_blog_task_request(
            {
                **VALID_BODY,
                "referenceMaterials": [
                    {"type": "PDF", "value": f"data:image/png;base64,{encoded}"}
                ],
            }
        )


def test_accepts_any_image_mime_and_rejects_non_image_mime_for_image():
    """참고자료 이미지는 형식을 가리지 않고 모두 받는다(Anthropic 비지원 형식은 전송 직전
    PNG로 변환). 다만 IMAGE 타입에 비이미지 mime은 거부한다."""
    import base64

    encoded = base64.b64encode(b"binary image bytes").decode()
    for mime in ("image/png", "image/jpeg", "image/webp", "image/gif", "image/heic", "image/svg+xml"):
        result = validate_create_blog_task_request(
            {
                **VALID_BODY,
                "referenceMaterials": [{"type": "IMAGE", "value": f"data:{mime};base64,{encoded}"}],
            }
        )
        assert result.input.reference_materials[0].type.value == "IMAGE"

    with pytest.raises(BlogTaskError):
        validate_create_blog_task_request(
            {
                **VALID_BODY,
                "referenceMaterials": [
                    {"type": "IMAGE", "value": f"data:application/zip;base64,{encoded}"}
                ],
            }
        )


def test_allows_a_defined_transition():
    assert can_transition(BlogTaskStatus.INPUT, BlogTaskStatus.REFERENCE_PROCESSING) is True
    assert can_transition(BlogTaskStatus.GENERATING, BlogTaskStatus.READY_TO_PUBLISH) is True


def test_rejects_skipping_states():
    assert can_transition(BlogTaskStatus.INPUT, BlogTaskStatus.POSTED) is False


def test_rejects_transitions_out_of_terminal_states():
    for terminal in (
        BlogTaskStatus.POSTED,
        BlogTaskStatus.FAILED,
        BlogTaskStatus.CONTENT_POLICY_VIOLATION,
    ):
        assert can_transition(terminal, BlogTaskStatus.POSTING) is False


# --- 옛 문서 읽기: 생성 기록에 원고가 빠져 있어도 글은 열려야 한다 ---

STORED_TASK = {
    "postId": "post_1",
    "userId": "user_1",
    "status": "READY_TO_PUBLISH",
    "version": 3,
    "createdAt": "2026-08-06T01:00:00.000Z",
    "updatedAt": "2026-08-06T02:00:00.000Z",
    "statusHistory": [],
    "input": {"topic": "제로트러스트", "keywords": [], "referenceMaterials": []},
    "postingLogs": [],
    "finalPost": {
        "title": "제로트러스트 입문",
        "body": "본문",
        "hashtags": ["#보안"],
        "htmlContent": "<p>본문</p>",
    },
}

BROKEN_RESULT = {
    "promptVersion": "m4-draft-v1",
    "provider": "anthropic",
    "model": "claude-opus-5",
    "generatedAt": "2026-08-06T02:00:00.000Z",
    "finalReview": {
        "reviewedAt": "2026-08-06T02:00:00.000Z",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "rounds": 1,
        "applied": 0,
        "removedImages": 0,
    },
}


def test_a_draft_record_without_its_manuscript_is_filled_from_the_post():
    """실제로 저장돼 있던 모양이다. 예전에는 이 문서를 읽다 500이 나서 글이 안 열렸다."""
    task = BlogTask.model_validate({**STORED_TASK, "draftGenerationResult": BROKEN_RESULT})

    assert task.draft_generation_result is not None
    assert task.draft_generation_result.final_post.title == "제로트러스트 입문"
    # 나머지는 그대로 읽힌다.
    assert task.draft_generation_result.prompt_version == "m4-draft-v1"


def test_a_draft_record_is_dropped_when_there_is_no_manuscript_anywhere():
    stored = {k: v for k, v in STORED_TASK.items() if k != "finalPost"}

    task = BlogTask.model_validate({**stored, "draftGenerationResult": BROKEN_RESULT})

    # 글 전체를 못 읽게 하지 않는다 — 설명할 원고가 없는 생성 기록만 버린다.
    assert task.draft_generation_result is None
    assert task.post_id == "post_1"


def test_a_complete_draft_record_is_left_alone():
    complete = {
        **BROKEN_RESULT,
        "finalPost": {
            "title": "생성이 저장한 제목",
            "body": "생성 본문",
            "hashtags": [],
            "htmlContent": "<p>생성 본문</p>",
        },
    }

    task = BlogTask.model_validate({**STORED_TASK, "draftGenerationResult": complete})

    assert task.draft_generation_result is not None
    assert task.draft_generation_result.final_post.title == "생성이 저장한 제목"


class TestUploadedImagesAreShrunkBeforeStoring:
    """올린 참고 이미지는 글 문서 안에 base64로 들어간다.

    실측(2026-08-06): 이미지 9장이 **2.11MB**였고, 그 글은 회선 0.09MB/s에서 20초 제한을
    넘겨 **다시 열리지 않았다.** 원고 이미지에 한 것과 같은 처리를 여기에도 한다.
    """

    @staticmethod
    def _jpeg(width: int, height: int) -> str:
        import io

        from PIL import Image

        from app.shared.image_bytes import to_data_url

        image = Image.new("RGB", (width, height))
        pixels = image.load()
        for x in range(width):
            for y in range(height):
                pixels[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 3) % 256)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=95)
        return to_data_url(buffer.getvalue(), "image/jpeg")

    def test_a_big_photo_is_stored_smaller(self):
        big = self._jpeg(2000, 1200)

        result = validate_create_blog_task_request(
            {
                **VALID_BODY,
                "referenceMaterials": [{"type": "IMAGE", "value": big}],
            }
        )

        stored = result.input.reference_materials[0].value
        assert len(stored) < len(big) / 2
        assert stored.startswith("data:image/jpeg;base64,")

    def test_the_picture_is_still_a_picture(self):
        """줄이는 것이지 버리는 것이 아니다 — 모델은 이 값을 그대로 첨부물로 받는다."""
        import io

        from PIL import Image

        from app.shared.image_bytes import MAX_IMAGE_WIDTH, data_url_parts

        result = validate_create_blog_task_request(
            {
                **VALID_BODY,
                "referenceMaterials": [{"type": "IMAGE", "value": self._jpeg(2000, 1200)}],
            }
        )

        raw, _ = data_url_parts(result.input.reference_materials[0].value)
        image = Image.open(io.BytesIO(raw))
        assert image.width == MAX_IMAGE_WIDTH
        # 비율이 틀어지면 첨부된 사진이 늘어나 보인다.
        assert image.height == round(1200 * MAX_IMAGE_WIDTH / 2000)

    def test_a_small_photo_is_left_alone(self):
        """다시 인코딩하면 화질만 잃고 용량은 안 준다."""
        small = self._jpeg(400, 300)

        result = validate_create_blog_task_request(
            {
                **VALID_BODY,
                "referenceMaterials": [{"type": "IMAGE", "value": small}],
            }
        )

        assert result.input.reference_materials[0].value == small

    def test_a_pdf_is_not_touched(self):
        """PDF는 서버가 텍스트를 뽑아 쓴다. 이미지로 다시 구우면 그 글자가 사라진다."""
        import base64

        encoded = base64.b64encode(b"%PDF-1.4 fake").decode()

        result = validate_create_blog_task_request(
            {
                **VALID_BODY,
                "referenceMaterials": [
                    {"type": "PDF", "value": f"data:application/pdf;base64,{encoded}"}
                ],
            }
        )

        assert result.input.reference_materials[0].value.endswith(encoded)


class TestATopicCanBeASentence:
    """소재는 단어만이 아니다.

    '스파이더맨 4편'처럼 짧을 수도, 한 문장일 수도 있다(2026-08-06 사용자 지적).
    화면의 `MAX_TOPIC_CHARS`(apps/web/src/constants.ts)가 이 값과 같아야 한다 —
    다르면 다 적고 나서 저장할 때 거절당하거나, 서버가 받아 주는 것을 못 적는다.
    """

    def test_a_sentence_is_accepted(self):
        topic = "스파이더맨 4편을 보고 느낀 좋았던 점과 아쉬웠던 점을 정리한다"

        result = validate_create_blog_task_request({**VALID_BODY, "topic": topic})

        assert result.input.topic == topic

    def test_the_limit_is_three_hundred(self):
        from app.modules.blog_task.validation import MAX_TOPIC_CHARS

        assert MAX_TOPIC_CHARS == 300

    def test_exactly_the_limit_fits(self):
        topic = "가" * 300

        result = validate_create_blog_task_request({**VALID_BODY, "topic": topic})

        assert len(result.input.topic) == 300

    def test_one_over_the_limit_is_refused(self):
        """넘치면 조용히 자르지 않는다 — 사용자가 적은 것이 사라지면 안 된다."""
        with pytest.raises(BlogTaskError, match="topic"):
            validate_create_blog_task_request({**VALID_BODY, "topic": "가" * 301})


class TestDraftsCarryTheirOwnDirection:
    """편마다 고른 방향을 함께 받는다(2026-08-12 사용자 신고).

        "3번째 편에 대해서 글 방향 선택하고 다음방향 가니까 순간에러가 났어"
        (화면 로그: POST /posts/.../schedule 400 Bad Request)

    ``intentId``는 자리번호다(``{postId}_intent_{n}``). 편마다 검증이 다시 도므로 서로 다른
    방향이 같은 번호를 달고 오는 것이 정상인데, 번호로 중복을 가리다가 멀쩡한 요청을 400으로
    돌려보냈다.
    """

    @staticmethod
    def _direction(intent_id: str, title: str) -> dict:
        return {
            "intentId": intent_id,
            "title": title,
            "targetReader": "20대 직장인",
            "rationale": "근거",
            "keywords": ["가"],
            "sources": [],
        }

    def test_no_body_is_still_a_single_draft(self):
        from app.modules.blog_task.validation import validate_additional_drafts

        assert validate_additional_drafts(None) == []

    def test_the_chosen_direction_comes_along(self):
        from app.modules.blog_task.validation import validate_additional_drafts

        drafts = validate_additional_drafts(
            [{"intentId": "post_1_intent_2", "title": "2편 제목",
              "intent": self._direction("post_1_intent_2", "2편이 고른 방향")}]
        )

        assert drafts[0]["intentId"] == "post_1_intent_2"
        assert drafts[0]["title"] == "2편 제목"
        assert drafts[0]["intent"].title == "2편이 고른 방향"

    def test_the_same_slot_number_with_different_directions_is_allowed(self):
        """이것이 400의 정체다 — 편마다 번호가 다시 매겨져 겹치는 것이 정상이다."""
        from app.modules.blog_task.validation import validate_additional_drafts

        drafts = validate_additional_drafts(
            [
                {"intentId": "post_1_intent_2", "intent": self._direction("post_1_intent_2", "방향 가")},
                {"intentId": "post_1_intent_2", "intent": self._direction("post_1_intent_2", "방향 나")},
            ]
        )

        assert len(drafts) == 2

    def test_the_same_direction_twice_is_still_refused(self):
        """말만 바꾼 중복 글을 막는 규칙 자체는 남는다 — 기준이 제목으로 옮겨졌을 뿐이다."""
        from app.modules.blog_task.validation import validate_additional_drafts

        with pytest.raises(BlogTaskError, match="repeat"):
            validate_additional_drafts(
                [
                    {"intentId": "a", "intent": self._direction("a", "같은 방향")},
                    {"intentId": "b", "intent": self._direction("b", "같은 방향")},
                ]
            )

    def test_an_old_request_without_directions_is_still_compared_by_number(self):
        from app.modules.blog_task.validation import validate_additional_drafts

        with pytest.raises(BlogTaskError, match="repeat"):
            validate_additional_drafts([{"intentId": "a"}, {"intentId": "a"}])

    def test_a_direction_that_is_not_an_object_is_refused(self):
        from app.modules.blog_task.validation import validate_additional_drafts

        with pytest.raises(BlogTaskError, match="intent"):
            validate_additional_drafts([{"intentId": "a", "intent": "방향"}])

    def test_a_direction_missing_its_title_is_refused(self):
        """제목이 곧 정체성이다 — 비어 있으면 중복도 가릴 수 없다."""
        from app.modules.blog_task.validation import validate_additional_drafts

        with pytest.raises(BlogTaskError, match="intent"):
            validate_additional_drafts(
                [{"intentId": "a", "intent": {**self._direction("a", ""), "title": "  "}}]
            )
