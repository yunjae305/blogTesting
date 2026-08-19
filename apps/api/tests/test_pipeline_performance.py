"""생성 파이프라인 성능 구조 검증.

빨라졌다는 주장을 코드 수준에서 고정한다:
- 원고는 마크다운 한 벌만 출력하고 HTML·텍스트는 코드가 유도한다(출력 3벌 제거)
- M3 요약은 출처를 다시 베끼지 않고 sourceIndex로 가리킨다
- 같은 입력의 수집·설계는 중복 API 호출 없이 캐시·in-flight 합류로 재사용된다
- 표·그래프 렌더링과 배경 장면 생성은 겹쳐 돈다(순차 대기 제거)
- provider 호출은 공유 keep-alive 클라이언트를 쓴다
- 성능 계측(perf)은 wall과 busy를 구분해 병렬 여부를 로그로 증명한다
"""

import asyncio
import json
import time

import httpx
import pytest
import respx

from app.llm.extractors import (
    extract_gemini_interaction_sources,
    extract_gemini_url_context_results,
)
from app.llm.http import shared_client
from app.llm.live_adapters import GeminiResearchAnalyzer
from app.llm.markdown_html import markdown_for_storage, markdown_to_html
from app.llm.parsing import final_post_from_json, sources_from_indexes
from app.llm.provider_config import LlmProvider, LlmRole, RoleConfig
from app.modules.blog_task.repository import InMemoryBlogTaskRepository
from app.modules.draft.service import PHOTO_SEARCH_GROUP_CONCURRENCY, DraftService
from app.shared import (
    ContentPlan,
    ContentPlanSection,
    DraftFormat,
    DraftGenerationInput,
    GeneratedPostImage,
    IntentAnchor,
    ReferenceMaterial,
    ReferenceMaterialType,
    SearchSource,
    SeoKeywordPlan,
    WebPhoto,
    WebSearchAnalysisInput,
    perf,
)
from app.shared import BlogTaskInput, SelectedIntentForDraft

from test_card_pipeline import (
    DRAFT,
    CardPlanningGenerator,
    brief,
    build_card_service,
    plan,
)
from test_draft_service import (
    DRAFT_RESULT,
    NOW,
    SHORT_RESULT,
    SequenceDraftGenerator,
    build_task,
)
from test_draft_content_design import BAR_VISUAL

# ---------------------------------------------------------------- 마크다운 변환


MARKDOWN_ARTICLE = "\n\n".join(
    [
        "# 제목입니다",
        "도입 문단입니다. **핵심 결론**과 ==주의사항==을 담습니다.",
        "## 첫 소제목",
        "첫 섹션 문단입니다.",
        "[[VISUAL: visual-1]]",
        "| 기준 | A | B |\n|---|---|---|\n| 요금 | 무료 | 유료 |",
        "### 세부 항목",
        "- 항목 하나\n- 항목 둘",
        "1. 첫 단계\n2. 둘째 단계",
        "## 둘째 소제목\n둘째 섹션 문단 <스크립트> 특수문자.",
    ]
)


class TestMarkdownToHtml:
    def test_structure_is_converted_one_to_one(self):
        html = markdown_to_html("제목입니다", MARKDOWN_ARTICLE)
        assert html.startswith("<article><h1>제목입니다</h1>")
        assert html.count("<h2>") == 2  # `## `와 1:1 — 카드 배치 순번의 전제
        assert "<h3>세부 항목</h3>" in html
        assert "<strong>핵심 결론</strong>" in html
        assert "<mark>주의사항</mark>" in html
        assert "<table><thead><tr><th>기준</th>" in html
        assert "<tbody><tr><td>요금</td>" in html
        assert "<ul><li>항목 하나</li>" in html
        assert "<ol><li>첫 단계</li>" in html

    def test_visual_marker_stays_bare_for_figure_substitution(self):
        html = markdown_to_html("제목입니다", MARKDOWN_ARTICLE)
        # <p>로 감싸면 치환된 <figure>가 <p> 안에 갇혀 유효하지 않은 HTML이 된다.
        assert "[[VISUAL: visual-1]]" in html
        assert "<p>[[VISUAL: visual-1]]</p>" not in html

    def test_text_is_html_escaped(self):
        html = markdown_to_html("제목입니다", MARKDOWN_ARTICLE)
        assert "<스크립트>" not in html
        assert "&lt;스크립트&gt;" in html

    def test_leading_title_heading_is_not_duplicated(self):
        html = markdown_to_html("제목입니다", MARKDOWN_ARTICLE)
        assert html.count("제목입니다") == 1

    def test_a_table_written_with_blank_lines_between_rows_still_becomes_a_table(self):
        """모델이 표의 행마다 빈 줄을 넣어 오면 행 하나하나가 별개 문단이 됐다.

        화면에 `| 구분 | 스탠딩석 | 지정석 |`이 글자 그대로 찍혔다. 문단 분리가 빈 줄이라
        생긴 일이고, 표는 모아 놓고 봐야 표다.
        """
        spaced = "\n\n".join(
            [
                "## 좌석 비교",
                "| 구분 | 스탠딩석 | 지정석 |",
                "|---|---|---|",
                "| 자리 결정 | 입장 번호 순 | 예매 시 좌석 확정 |",
                "| 체력 부담 | 오래 서 있음 | 앉아서 관람 |",
                "이어지는 문단입니다.",
            ]
        )

        html = markdown_to_html("제목입니다", spaced)

        assert "<table><thead><tr><th>구분</th>" in html
        assert "<td>자리 결정</td>" in html and "<td>체력 부담</td>" in html
        assert "<p>| 구분" not in html
        # 표 뒤의 문단은 표에 딸려 들어가지 않는다.
        assert "<p>이어지는 문단입니다.</p>" in html

    def test_a_lone_piped_line_is_not_turned_into_a_table(self):
        """파이프가 들었다고 표로 묶지 않는다 — 구분선까지 갖춘 진짜 표만 합친다."""
        html = markdown_to_html("제목입니다", "| 한 줄짜리 파이프 문단 |\n\n다음 문단입니다.")

        assert "<table>" not in html
        assert "<p>| 한 줄짜리 파이프 문단 |</p>" in html

    def test_storage_markdown_rejoins_a_blank_line_table(self):
        """저장 마크다운도 같은 규칙이다 — 편집기·복사·본문 검사가 같은 문자열을 본다."""
        spaced = "\n\n".join(
            [
                "| 구분 | 스탠딩석 |",
                "|---|---|",
                "| 자리 결정 | 입장 번호 순 |",
            ]
        )

        stored = markdown_for_storage("제목입니다", spaced)

        assert "| 구분 | 스탠딩석 |\n|---|---|\n| 자리 결정 | 입장 번호 순 |" in stored

    def test_storage_markdown_replaces_mark_and_keeps_title(self):
        stored = markdown_for_storage("제목입니다", MARKDOWN_ARTICLE)
        # 편집기 마크다운 경로에 ==가 새면 글자 그대로 보인다(프론트 MARK→** 규칙과 동일).
        assert "==" not in stored
        assert "**주의사항**" in stored
        assert stored.startswith("# 제목입니다\n\n")
        assert stored.count("# 제목입니다") == 1

    def test_storage_markdown_never_touches_base64_padding_inside_images(self):
        """base64 패딩(`==`)은 형광펜 기호가 아니다.

        이미지가 두 장이면 `==(.+?)==`가 첫 장의 패딩을 여는 기호로, 둘째 장의 패딩을
        닫는 기호로 오인해 둘 다 `**`로 바꿔 놓았다(2026-08-07 실측: 저장된 글의 썸네일
        src가 `…2Q**`로 끝나 미리보기가 깨지고 이미지 외부화도 빗나갔다).
        """
        first = "data:image/jpeg;base64,AAAA/2Q=="
        second = "data:image/jpeg;base64,BBBB/kUf=="
        markdown = "\n\n".join(
            [
                "# 제목입니다",
                f"![썸네일]({first})",
                "본문 문단에는 ==형광펜== 표시가 있습니다.",
                f"![본문 사진]({second})",
            ]
        )

        stored = markdown_for_storage("제목입니다", markdown)

        assert f"![썸네일]({first})" in stored
        assert f"![본문 사진]({second})" in stored
        # 형광펜 치환 자체는 그대로 동작한다.
        assert "**형광펜**" in stored
        assert "==" not in stored.replace(first, "").replace(second, "")


class TestSingleDraftOutput:
    def test_markdown_only_response_derives_html_and_body(self):
        post = final_post_from_json(
            {
                "title": "제목입니다",
                "markdownContent": MARKDOWN_ARTICLE,
                "hashtags": ["태그"],
                "thumbnailCopy": ["문구"],
            },
            "폴백 제목",
            5,
            ["키워드"],
        )
        assert post.html_content.startswith("<article><h1>제목입니다</h1>")
        assert "<h2>" in post.html_content  # 품질 검사(소제목)·카드 배치가 보는 구조
        assert "<mark>주의사항</mark>" in post.html_content
        assert post.markdown_content.startswith("# 제목입니다")
        assert "==" not in post.markdown_content
        # body는 순수 텍스트: 제목·마크업 기호가 섞이지 않는다(글자수 검사 대상).
        assert "제목입니다" not in post.body
        assert "**" not in post.body and "##" not in post.body
        assert "핵심 결론" in post.body

    def test_legacy_three_copy_response_still_parses(self):
        post = final_post_from_json(
            {
                "title": "T",
                "body": "본문",
                "htmlContent": "<article><p>본문</p></article>",
                "markdownContent": "# T\n\n본문",
                "hashtags": [],
                "thumbnailCopy": ["문구"],
            },
            "T",
            5,
            [],
        )
        assert post.body == "본문"
        assert post.html_content == "<article><p>본문</p></article>"


# ------------------------------------------------------------- M3 sourceIndex


COLLECTED = [
    SearchSource(title="출처1", url="https://a.example/1", snippet="요약1"),
    SearchSource(title="출처2", url="https://a.example/2", snippet="요약2"),
]


class TestSourceIndexJoin:
    def test_indexes_join_model_judgments_onto_collected_sources(self):
        joined = sources_from_indexes(
            [
                {
                    "sourceIndex": 2,
                    "sourceType": "OFFICIAL",
                    "relevanceScore": 91,
                    "dataPoints": [{"label": "이용률", "value": 42, "unit": "%"}],
                }
            ],
            COLLECTED,
        )
        assert len(joined) == 1
        assert joined[0].url == "https://a.example/2"  # 제목·URL은 수집본이 진실
        assert joined[0].title == "출처2"
        assert joined[0].source_type == "OFFICIAL"
        assert joined[0].relevance_score == 91
        assert joined[0].data_points[0].label == "이용률"

    def test_out_of_range_and_duplicate_indexes_are_dropped(self):
        joined = sources_from_indexes(
            [
                {"sourceIndex": 1, "sourceType": "NEWS", "relevanceScore": 80, "dataPoints": []},
                {"sourceIndex": 1, "sourceType": "NEWS", "relevanceScore": 70, "dataPoints": []},
                {"sourceIndex": 9, "sourceType": "NEWS", "relevanceScore": 60, "dataPoints": []},
                {"sourceIndex": 0, "sourceType": "NEWS", "relevanceScore": 50, "dataPoints": []},
            ],
            COLLECTED,
        )
        assert [s.url for s in joined] == ["https://a.example/1"]


# --------------------------------------------------------------- M3 수집 캐시


GEMINI_RESPONSE = {
    "output_text": "브리핑",
    "steps": [
        {
            "content": [
                {
                    "text": "브리핑",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "title": "출처1",
                            "url": "https://a.example/1",
                            "cited_text": "요약1",
                        }
                    ],
                }
            ]
        }
    ],
}

REFERENCE_URL = "https://reference.example.com/article"
REFERENCE_CITED_TEXT = "참고 문서에서 직접 확인한 핵심 내용"
REFERENCE_OUTPUT_TEXT = f"확인 결과: {REFERENCE_CITED_TEXT}"
REFERENCE_CITATION_START = len("확인 결과: ".encode("utf-8"))
REFERENCE_CITATION_END = REFERENCE_CITATION_START + len(REFERENCE_CITED_TEXT.encode("utf-8"))
GEMINI_URL_CONTEXT_RESPONSE = {
    "output_text": "사용자 URL 본문과 웹 검색을 함께 확인한 브리핑",
    "steps": [
        {
            "type": "url_context_call",
            "id": "url-call-1",
            "arguments": {"urls": [REFERENCE_URL]},
        },
        {
            "type": "url_context_result",
            "call_id": "url-call-1",
            "result": [
                {
                    "status": "success",
                    "title": "사용자 참고 문서",
                    "url": "https://reference.example.com/article/",
                }
            ],
        },
        {
            "type": "model_output",
            "content": [
                {
                    "type": "text",
                    "text": REFERENCE_OUTPUT_TEXT,
                    "annotations": [
                        {
                            "type": "url_citation",
                            "title": "사용자 참고 문서",
                            "url": "https://reference.example.com/article#details",
                            "start_index": REFERENCE_CITATION_START,
                            "end_index": REFERENCE_CITATION_END,
                        }
                    ],
                }
            ],
        },
    ],
}

_INTENTS_JSON = (
    '{"intentCandidates": ['
    '{"title": "의도1", "targetReader": "독자", "rationale": "근거", "keywords": ["k"],'
    ' "sources": [{"sourceIndex": 1, "sourceType": "NEWS", "relevanceScore": 80, "dataPoints": []}]},'
    '{"title": "의도2", "targetReader": "독자", "rationale": "근거", "keywords": ["k"], "sources": []},'
    '{"title": "의도3", "targetReader": "독자", "rationale": "근거", "keywords": ["k"], "sources": []}'
    "]}"
)

#: M3 정리 응답. 2026-08-07부터 이 절반도 Gemini다(generateContent + responseSchema).
GEMINI_INTENTS = {"candidates": [{"content": {"parts": [{"text": _INTENTS_JSON}]}}]}


def _role(role: LlmRole, provider: LlmProvider, model: str) -> RoleConfig:
    return RoleConfig(
        role=role,
        label=str(role),
        provider=provider,
        model=model,
        api_key_env="KEY",
        api_key="test-key",
        has_credentials=True,
    )


async def _no_sleep(attempt, retry_after):
    """재시도 백오프를 없앤다 — 테스트가 실제 초 단위로 자지 않게."""
    return None


def _analysis_input(
    topic: str = "블로그 자동화", materials: list[ReferenceMaterial] | None = None
) -> WebSearchAnalysisInput:
    return WebSearchAnalysisInput(
        post_id="post_1",
        user_id="user_1",
        input=BlogTaskInput(topic=topic, keywords=["AI"], reference_materials=materials or []),
        prompt_version="m3-intent@v1.0",
    )


def test_url_context_results_keep_requested_url_and_successful_source():
    """리디렉션이 있어도 어떤 사용자 URL을 읽었는지 보존하고 성공한 결과만 근거가 된다."""

    results = extract_gemini_url_context_results(GEMINI_URL_CONTEXT_RESPONSE)
    sources = extract_gemini_interaction_sources(GEMINI_URL_CONTEXT_RESPONSE)

    assert len(results) == 1
    assert results[0].requested_url == REFERENCE_URL
    assert results[0].url == "https://reference.example.com/article/"
    assert results[0].status == "success"
    assert sources[0].url == REFERENCE_URL
    assert sources[0].snippet == REFERENCE_CITED_TEXT
    assert len(sources) == 1


def test_failed_url_context_result_is_not_promoted_to_a_source():
    payload = {
        "steps": [
            {
                "type": "url_context_call",
                "id": "url-call-1",
                "arguments": {"urls": [REFERENCE_URL]},
            },
            {
                "type": "url_context_result",
                "call_id": "url-call-1",
                "is_error": True,
                "result": [{"status": "paywall", "url": REFERENCE_URL}],
            },
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": "검색 결과",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "검색 인용",
                                "url": REFERENCE_URL,
                                "cited_text": "본문처럼 보이는 검색 스니펫",
                            }
                        ],
                    }
                ],
            },
        ]
    }

    assert extract_gemini_url_context_results(payload)[0].status == "paywall"
    assert extract_gemini_interaction_sources(payload) == []


def test_a_url_context_call_without_a_result_cannot_be_revived_by_a_citation():
    payload = {
        "steps": [
            {
                "type": "url_context_call",
                "id": "url-call-1",
                "arguments": {"urls": [REFERENCE_URL]},
            },
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": "검색 결과",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "검색 인용",
                                "url": REFERENCE_URL,
                            }
                        ],
                    }
                ],
            },
        ]
    }

    assert extract_gemini_interaction_sources(payload) == []


def test_a_secret_bearing_gemini_citation_is_discarded():
    secret = "must-not-be-stored"
    payload = {
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": "검색 결과",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "서명 URL",
                                "url": f"https://example.com/report?access_token={secret}",
                            }
                        ],
                    }
                ],
            }
        ]
    }

    assert extract_gemini_interaction_sources(payload) == []


@pytest.fixture(autouse=True)
def _clear_model_cooldown():
    """모델 쿨다운은 프로세스 로컬 상태다 — 테스트 사이에 새지 않게 비운다.

    운영에서는 이 상태가 요청 사이에 남아 있어야 한다(그게 목적이다). 테스트에서만 비운다.
    """
    from app.llm.live_adapters import _research_model_cooldown

    _research_model_cooldown.clear()
    yield
    _research_model_cooldown.clear()


class TestResearchCache:
    @respx.mock
    async def test_same_input_reverification_reuses_collected_research(self):
        gemini = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions"
        ).mock(return_value=httpx.Response(200, json=GEMINI_RESPONSE))
        summarize = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-summary:generateContent"
        ).mock(return_value=httpx.Response(200, json=GEMINI_INTENTS))
        analyzer = GeminiResearchAnalyzer(
            _role(LlmRole.M3_COLLECT, LlmProvider.GEMINI, "gemini-test"),
            _role(LlmRole.M3_SUMMARY, LlmProvider.GEMINI, "gemini-summary"),
        )

        first = await analyzer.search_and_analyze(_analysis_input())
        second = await analyzer.search_and_analyze(_analysis_input())

        # 수집(가장 긴 구간)은 한 번, 요약은 매번 — 재검증은 새 의도 후보를 받아야 한다.
        assert gemini.call_count == 1
        assert summarize.call_count == 2
        request_body = json.loads(gemini.calls[0].request.content)
        assert request_body["tools"] == [{"type": "google_search"}]
        assert request_body["store"] is False
        assert "untrusted data" in request_body["system_instruction"]
        assert first.intent_candidates and second.intent_candidates
        # sourceIndex가 실제 수집 출처로 합쳐졌다.
        assert first.intent_candidates[0].sources[0].url == "https://a.example/1"
        assert first.intent_candidates[0].sources[0].source_type == "NEWS"

    @respx.mock
    async def test_user_reference_url_uses_url_context_and_records_real_success(self):
        gemini = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions"
        ).mock(return_value=httpx.Response(200, json=GEMINI_URL_CONTEXT_RESPONSE))
        summarize = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-summary:generateContent"
        ).mock(return_value=httpx.Response(200, json=GEMINI_INTENTS))
        analyzer = GeminiResearchAnalyzer(
            _role(LlmRole.M3_COLLECT, LlmProvider.GEMINI, "gemini-test"),
            _role(LlmRole.M3_SUMMARY, LlmProvider.GEMINI, "gemini-summary"),
        )
        material = ReferenceMaterial(type=ReferenceMaterialType.URL, value=REFERENCE_URL)

        result = await analyzer.search_and_analyze(
            _analysis_input("URL Context 확인", [material])
        )

        request_body = json.loads(gemini.calls[0].request.content)
        assert request_body["tools"] == [
            {"type": "url_context"},
            {"type": "google_search"},
        ]
        assert request_body["store"] is False
        assert "untrusted data" in request_body["system_instruction"]
        assert REFERENCE_URL in request_body["input"]
        summary_body = json.loads(summarize.calls[0].request.content)
        summary_prompt = summary_body["contents"][0]["parts"][0]["text"]
        assert f"[success] {REFERENCE_URL}" in summary_prompt
        assert "sourceIndex 1은 URL Context로 직접 조회에 성공" in summary_prompt
        assert result.intent_candidates[0].sources[0].url == REFERENCE_URL

    @respx.mock
    async def test_a_legacy_secret_url_never_reaches_gemini(self):
        gemini = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions"
        ).mock(return_value=httpx.Response(200, json=GEMINI_RESPONSE))
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-summary:generateContent"
        ).mock(return_value=httpx.Response(200, json=GEMINI_INTENTS))
        analyzer = GeminiResearchAnalyzer(
            _role(LlmRole.M3_COLLECT, LlmProvider.GEMINI, "gemini-test"),
            _role(LlmRole.M3_SUMMARY, LlmProvider.GEMINI, "gemini-summary"),
        )
        secret = "must-not-reach-provider"
        material = ReferenceMaterial(
            type=ReferenceMaterialType.URL,
            value=f"https://reference.example.com/report?access_token={secret}",
        )

        await analyzer.search_and_analyze(_analysis_input("legacy URL", [material]))

        request_body = json.loads(gemini.calls[0].request.content)
        assert request_body["tools"] == [{"type": "google_search"}]
        assert secret not in request_body["input"]

    @respx.mock
    async def test_failed_reference_url_is_retried_instead_of_cached(self):
        failed_response = {
            "output_text": "입력 URL은 paywall이었고 공개 검색 자료만 확인했습니다.",
            "steps": [
                {
                    "type": "url_context_call",
                    "id": "url-call-1",
                    "arguments": {"urls": [REFERENCE_URL]},
                },
                {
                    "type": "url_context_result",
                    "call_id": "url-call-1",
                    "result": [{"status": "paywall", "url": REFERENCE_URL}],
                },
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "text",
                            "text": "대체 공개 자료",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "title": "공개 자료",
                                    "url": "https://public.example.com/report",
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        gemini = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions"
        ).mock(return_value=httpx.Response(200, json=failed_response))
        summarize = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-summary:generateContent"
        ).mock(return_value=httpx.Response(200, json=GEMINI_INTENTS))
        analyzer = GeminiResearchAnalyzer(
            _role(LlmRole.M3_COLLECT, LlmProvider.GEMINI, "gemini-test"),
            _role(LlmRole.M3_SUMMARY, LlmProvider.GEMINI, "gemini-summary"),
        )
        material = ReferenceMaterial(type=ReferenceMaterialType.URL, value=REFERENCE_URL)
        analysis_input = _analysis_input("URL Context 재시도", [material])

        await analyzer.search_and_analyze(analysis_input)
        await analyzer.search_and_analyze(analysis_input)

        assert gemini.call_count == 2
        assert summarize.call_count == 2
        for call in summarize.calls:
            summary_body = json.loads(call.request.content)
            summary_prompt = summary_body["contents"][0]["parts"][0]["text"]
            assert "URL Context로 직접 조회에 성공" not in summary_prompt

    @respx.mock
    async def test_a_crowded_model_is_replaced_by_a_sibling(self, monkeypatch):
        """설정된 모델이 혼잡하면 형제 모델로 넘어가 검증을 끝낸다 (2026-07-30).

        재시도 예산을 6회로 늘려 봤지만 사용자가 본 것은 더 긴 대기 뒤의 같은 실패였다
        ("계속 재시도만 뜬다"). 한 모델이 혼잡할 때 필요한 것은 더 기다리는 것이 아니라
        다른 모델로 옮기는 것이다 — 실측에서 형제 모델들은 같은 순간에 5~9초로 응답했다.
        """
        from app.llm.live_adapters import RESEARCH_FALLBACK_MODELS

        monkeypatch.setattr("app.llm.live_adapters._sleep_before_retry", _no_sleep)
        used: list[str] = []

        def route(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            used.append(model)
            if model == "gemini-test":
                return httpx.Response(
                    500,
                    json={"error": {"message": "high demand", "code": "api_error"}},
                )
            return httpx.Response(200, json=GEMINI_RESPONSE)

        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions"
        ).mock(side_effect=route)
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-summary:generateContent"
        ).mock(return_value=httpx.Response(200, json=GEMINI_INTENTS))
        analyzer = GeminiResearchAnalyzer(
            _role(LlmRole.M3_COLLECT, LlmProvider.GEMINI, "gemini-test"),
            _role(LlmRole.M3_SUMMARY, LlmProvider.GEMINI, "gemini-summary"),
        )

        result = await analyzer.search_and_analyze(_analysis_input("혼잡한 주제"))

        # 검증이 끝났다 — 실패로 남지 않는다.
        assert result.intent_candidates
        # 설정값을 먼저 쓰고, 혼잡하면 폴백 목록의 첫 항목으로 넘어간다.
        assert used[0] == "gemini-test"
        assert used[-1] == RESEARCH_FALLBACK_MODELS[0]

    @respx.mock
    async def test_an_answer_without_a_body_moves_to_the_next_model(self, monkeypatch):
        """200인데 최종 본문이 없으면 다음 모델로 넘어간다(2026-08-12 사용자 신고).

            "방향 4가지 보여주는거 어디갔어"
            (화면: 자료를 모으는 중 오류가 났습니다 — Gemini interaction response did not
             contain text output)

        검색 단계(steps)는 다 돌았는데 모델이 마지막 답을 쓰지 않은 응답이다. 예전에는 본문
        추출을 폴백 루프 **밖에서** 해서, 폴백 모델을 하나도 시도하지 못한 채 검증이 통째로
        실패했다 — 화면에는 방향 후보 4개 대신 실패 안내 한 장만 남았다.
        """
        monkeypatch.setattr("app.llm.live_adapters._sleep_before_retry", _no_sleep)
        used: list[str] = []

        def route(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            used.append(model)
            if model == "gemini-test":
                # 검색은 돌았는데(steps) 본문이 없다.
                return httpx.Response(200, json={"steps": [{"content": [{}]}]})
            return httpx.Response(200, json=GEMINI_RESPONSE)

        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions"
        ).mock(side_effect=route)
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-summary:generateContent"
        ).mock(return_value=httpx.Response(200, json=GEMINI_INTENTS))
        analyzer = GeminiResearchAnalyzer(
            _role(LlmRole.M3_COLLECT, LlmProvider.GEMINI, "gemini-test"),
            _role(LlmRole.M3_SUMMARY, LlmProvider.GEMINI, "gemini-summary"),
        )

        result = await analyzer.search_and_analyze(_analysis_input("본문 없는 응답"))

        assert result.intent_candidates  # 실패로 남지 않는다
        assert len(used) > 1  # 폴백을 실제로 시도했다

    @respx.mock
    async def test_every_model_answering_without_a_body_says_so(self, monkeypatch):
        """전부 본문 없이 오면 그 사실로 올라온다 — 설정 오류와 구분해야 안내가 달라진다."""
        from app.llm.parsing import ProviderEmptyResponseError

        monkeypatch.setattr("app.llm.live_adapters._sleep_before_retry", _no_sleep)
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions"
        ).mock(return_value=httpx.Response(200, json={"steps": [{"content": [{}]}]}))
        analyzer = GeminiResearchAnalyzer(
            _role(LlmRole.M3_COLLECT, LlmProvider.GEMINI, "gemini-test"),
            _role(LlmRole.M3_SUMMARY, LlmProvider.GEMINI, "gemini-summary"),
        )

        with pytest.raises(ProviderEmptyResponseError):
            await analyzer.search_and_analyze(_analysis_input("전부 본문 없음"))

    @respx.mock
    async def test_every_model_being_crowded_reports_the_overload(self, monkeypatch):
        """모두 혼잡하면 혼잡으로 올라온다 — 화면이 안내할 말이 설정 오류와 다르다."""
        from app.llm.live_adapters import RESEARCH_FALLBACK_MODELS, VERIFY_REQUEST_ATTEMPTS
        from app.llm.parsing import ProviderOverloadedError

        monkeypatch.setattr("app.llm.live_adapters._sleep_before_retry", _no_sleep)
        gemini = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions"
        ).mock(
            return_value=httpx.Response(
                500, json={"error": {"message": "high demand", "code": "api_error"}}
            )
        )
        analyzer = GeminiResearchAnalyzer(
            _role(LlmRole.M3_COLLECT, LlmProvider.GEMINI, "gemini-test"),
            _role(LlmRole.M3_SUMMARY, LlmProvider.GEMINI, "gemini-summary"),
        )

        with pytest.raises(ProviderOverloadedError) as raised:
            await analyzer.search_and_analyze(_analysis_input("전부 혼잡"))

        # 모델마다 짧게 재시도한다 — 한 모델에 예산을 다 쓰지 않는다.
        assert gemini.call_count == (1 + len(RESEARCH_FALLBACK_MODELS)) * VERIFY_REQUEST_ATTEMPTS
        assert raised.value.status == 500
        assert raised.value.provider == "gemini"

    @respx.mock
    async def test_a_model_that_did_not_answer_is_skipped_next_time(self, monkeypatch):
        """혼잡으로 포기한 모델은 잠시 건너뛴다 — 매 검증이 45초를 먼저 버리지 않게.

        실측에서 혼잡한 모델은 오류를 빨리 주지 않고 95초를 붙잡고 있었다. 그 비용을 요청마다
        되풀이하면, 폴백이 있어도 사용자는 계속 오래 기다린다.
        """
        monkeypatch.setattr("app.llm.live_adapters._sleep_before_retry", _no_sleep)
        used: list[str] = []

        def route(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            used.append(model)
            if model == "gemini-test":
                return httpx.Response(
                    500, json={"error": {"message": "high demand", "code": "api_error"}}
                )
            return httpx.Response(200, json=GEMINI_RESPONSE)

        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions"
        ).mock(side_effect=route)
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-summary:generateContent"
        ).mock(return_value=httpx.Response(200, json=GEMINI_INTENTS))
        analyzer = GeminiResearchAnalyzer(
            _role(LlmRole.M3_COLLECT, LlmProvider.GEMINI, "gemini-test"),
            _role(LlmRole.M3_SUMMARY, LlmProvider.GEMINI, "gemini-summary"),
        )

        from app.llm.live_adapters import RESEARCH_FALLBACK_MODELS

        await analyzer.search_and_analyze(_analysis_input("첫 검증"))
        used.clear()
        # 다른 주제 — 수집 캐시를 타지 않는다.
        await analyzer.search_and_analyze(_analysis_input("두 번째 검증"))

        assert "gemini-test" not in used, "혼잡했던 모델을 곧바로 다시 불렀다"
        # 폴백 순서는 실측 응답 시간 순이다(RESEARCH_FALLBACK_MODELS).
        assert used and used[0] == RESEARCH_FALLBACK_MODELS[0]

    @respx.mock
    async def test_a_deterministic_error_is_not_masked_by_the_fallback(self, monkeypatch):
        """설정된 모델의 결정적 오류(잘못된 키·요청)는 폴백으로 가리지 않는다.

        가리면 사용자는 자료 품질이 왜 달라졌는지 모르고, 잘못된 설정이 조용히 남는다.
        """
        from app.llm.parsing import LiveAdapterError

        monkeypatch.setattr("app.llm.live_adapters._sleep_before_retry", _no_sleep)
        gemini = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions"
        ).mock(return_value=httpx.Response(401, json={"error": {"message": "bad key"}}))
        analyzer = GeminiResearchAnalyzer(
            _role(LlmRole.M3_COLLECT, LlmProvider.GEMINI, "gemini-test"),
            _role(LlmRole.M3_SUMMARY, LlmProvider.GEMINI, "gemini-summary"),
        )

        with pytest.raises(LiveAdapterError, match="401"):
            await analyzer.search_and_analyze(_analysis_input("잘못된 키"))

        assert gemini.call_count == 1, "401은 재시도도 폴백도 하지 않는다"

    @respx.mock
    async def test_a_different_topic_is_not_served_from_cache(self):
        gemini = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions"
        ).mock(return_value=httpx.Response(200, json=GEMINI_RESPONSE))
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-summary:generateContent"
        ).mock(return_value=httpx.Response(200, json=GEMINI_INTENTS))
        analyzer = GeminiResearchAnalyzer(
            _role(LlmRole.M3_COLLECT, LlmProvider.GEMINI, "gemini-test"),
            _role(LlmRole.M3_SUMMARY, LlmProvider.GEMINI, "gemini-summary"),
        )

        await analyzer.search_and_analyze(_analysis_input("주제 하나"))
        await analyzer.search_and_analyze(_analysis_input("완전히 다른 주제"))

        assert gemini.call_count == 2


# ------------------------------------------------------------ 콘텐츠 설계 캐시


PLAN = ContentPlan(
    target_reader="실무자",
    reader_problem="문제",
    reader_question="질문",
    article_promise="약속",
    content_angle="관점",
    article_type="INFORMATION",
    sections=[
        ContentPlanSection(
            section_id=f"section-{n}", heading=f"소제목{n}", question=f"질문{n}", purpose="근거 제시"
        )
        for n in (1, 2, 3)
    ],
)


class CountingPlanGenerator:
    def __init__(self, delay: float = 0.0):
        self.plan_calls = 0
        self._delay = delay

    async def generate_draft(self, draft_input):  # pragma: no cover - 여기선 안 쓴다
        raise AssertionError("not used")

    async def generate_content_plan(self, draft_input):
        self.plan_calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return PLAN


class CountingParallelPlanGenerator(CountingPlanGenerator):
    """콘텐츠/SEO가 동시에 들어왔는지와 중복 호출 여부를 함께 관찰한다."""

    def __init__(self, delay: float = 0.05):
        super().__init__(delay)
        self.seo_calls = 0
        self.in_flight = 0
        self.peak_in_flight = 0

    async def _stage(self):
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._delay)
        finally:
            self.in_flight -= 1

    async def generate_content_plan(self, draft_input):
        self.plan_calls += 1
        await self._stage()
        return PLAN

    async def generate_seo_keyword_plan(self, draft_input):
        self.seo_calls += 1
        await self._stage()
        return SeoKeywordPlan(primary="블로그 자동화", secondary=["AI"], avoid=[])


class CountingEarlySeoGenerator(CountingPlanGenerator):
    """편집 스타일과 SEO의 겹침, 콘텐츠 설계의 선후관계를 기록한다."""

    def __init__(self):
        super().__init__()
        self.editorial_calls = 0
        self.seo_calls = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.events: list[str] = []
        self.content_saw_editorial_style = False

    async def _stage(self, name: str, delay: float):
        self.events.append(f"{name}:start")
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(delay)
        finally:
            self.in_flight -= 1
            self.events.append(f"{name}:end")

    async def generate_editorial_style_plan(self, draft_input):
        self.editorial_calls += 1
        await self._stage("editorial", 0.03)
        # None이어도 서비스가 결정적인 코드 기본 계획으로 정규화한다.
        return None

    async def generate_seo_keyword_plan(self, draft_input):
        self.seo_calls += 1
        await self._stage("seo", 0.08)
        return SeoKeywordPlan(primary="블로그 자동화", secondary=["AI"], avoid=[])

    async def generate_content_plan(self, draft_input):
        self.plan_calls += 1
        self.content_saw_editorial_style = draft_input.editorial_style is not None
        await self._stage("content", 0.01)
        return PLAN


def _draft_input() -> DraftGenerationInput:
    return DraftGenerationInput(
        post_id="post_1",
        user_id="user_1",
        prompt_version="m4-draft@v1.1",
        format=DraftFormat.HTML,
        input=BlogTaskInput(topic="블로그 자동화", keywords=["AI"], reference_materials=[]),
        selected_intent=SelectedIntentForDraft(
            intent_id="intent_1", title="제목", target_reader="실무자", rationale="근거"
        ),
    )


def _plan_service(generator) -> DraftService:
    return DraftService(
        repository=InMemoryBlogTaskRepository(),
        draft_generator=generator,
    )


class TestContentPlanCache:
    async def test_concurrent_requests_share_one_api_call(self):
        generator = CountingPlanGenerator(delay=0.05)
        service = _plan_service(generator)
        results = await asyncio.gather(
            service._content_plan_with_cache(_draft_input()),
            service._content_plan_with_cache(_draft_input()),
        )
        assert generator.plan_calls == 1
        assert all(plan == PLAN for plan, _hit in results)

    async def test_second_request_is_a_cache_hit(self):
        generator = CountingPlanGenerator()
        service = _plan_service(generator)
        await service._content_plan_with_cache(_draft_input())
        plan, hit = await service._content_plan_with_cache(_draft_input())
        assert generator.plan_calls == 1
        assert plan == PLAN and hit

    async def test_content_and_seo_plans_run_together_and_share_inflight_calls(self):
        generator = CountingParallelPlanGenerator()
        repository = InMemoryBlogTaskRepository()
        service = DraftService(repository=repository, draft_generator=generator)
        task = build_task()
        await repository.create(task)
        draft_input = await service._build_draft_input(
            task, style=None, format_=DraftFormat.HTML
        )

        first, second = await asyncio.gather(
            service._with_content_and_seo_plans(draft_input, task),
            service._with_content_and_seo_plans(draft_input, task),
        )

        assert generator.peak_in_flight == 2
        assert generator.plan_calls == 1
        assert generator.seo_calls == 1
        assert first.content_plan == second.content_plan == PLAN
        assert first.seo_keyword_plan.primary == "블로그 자동화"

    async def test_seo_starts_with_editorial_but_content_waits_for_editorial(self):
        generator = CountingEarlySeoGenerator()
        service = _plan_service(generator)

        service.start_content_plan_prefetch(build_task())
        await service._jobs.drain()

        assert generator.editorial_calls == 1
        assert generator.seo_calls == 1
        assert generator.plan_calls == 1
        # SEO와 편집 스타일은 겹치되, 콘텐츠 설계는 편집 스타일의 시각 예산이 확정된 뒤다.
        assert generator.peak_in_flight == 2
        assert generator.events.index("seo:start") < generator.events.index("editorial:end")
        assert generator.events.index("editorial:end") < generator.events.index("content:start")
        assert generator.content_saw_editorial_style

    def test_prompt_version_changes_the_key_but_style_does_not(self):
        service = _plan_service(CountingPlanGenerator())
        base = _draft_input()
        assert service._plan_cache_key(base) != service._plan_cache_key(
            base.model_copy(update={"prompt_version": "m4-draft@v9.9"})
        )
        # style/format은 설계 프롬프트가 쓰지 않는다 — 키가 같아야 선행 생성(스타일을
        # 모르는 시점)이 실제 생성에 재사용된다.
        assert service._plan_cache_key(base) == service._plan_cache_key(
            base.model_copy(update={"style": "짧은 문장", "format": DraftFormat.MARKDOWN})
        )
        # 글의 방향(intent anchor)은 원고 프롬프트에만 실린다. 설계 프롬프트도 캐시 키도
        # 건드리지 않으므로 선행 생성이 그대로 재사용되어야 한다.
        assert service._plan_cache_key(base) == service._plan_cache_key(
            base.model_copy(
                update={"intent_anchor": IntentAnchor(intent="의도", keywords=["키워드"])}
            )
        )

    async def test_prefetch_fills_the_cache_for_generation(self):
        """선행 생성과 실제 생성이 **같은 키**를 만들어야 캐시가 의미를 갖는다.

        키에는 참고자료 근거와 편집 스타일도 들어가므로, 실제 생성이 그 둘을 준비하는 것과
        똑같은 순서로 준비해야 한다. 회차(generation_revision)를 GENERATING 전이로 세면 두
        경로가 갈려 캐시가 매번 빗나가므로, 완성(READY_TO_PUBLISH) 전이로 센다.
        """
        generator = CountingPlanGenerator()
        service = _plan_service(generator)

        service.start_content_plan_prefetch(build_task())
        await service._jobs.drain()
        assert generator.plan_calls == 1

        task = build_task()
        draft_input = await service._build_draft_input(
            task, style="짧은 문장", format_=DraftFormat.HTML
        )
        draft_input = await service._with_reference_evidence(draft_input)
        draft_input = await service._with_editorial_style(draft_input, task)
        plan, hit = await service._content_plan_with_cache(draft_input)
        assert generator.plan_calls == 1  # 추가 호출 없음
        assert plan == PLAN and hit


class TestPhotoSearchGroupConcurrency:
    async def test_groups_are_bounded_but_assignment_stays_in_input_order(self, monkeypatch):
        service = _plan_service(CountingPlanGenerator())
        task = build_task()
        keys = [
            (f"subject-{index}", "WEB_PHOTO", f"visual-{index}")
            for index in range(6)
        ]
        positions = {key: [index] for index, key in enumerate(keys)}
        subjects = {key: None for key in keys}
        calls = 0
        in_flight = 0
        peak_in_flight = 0
        second_group_finished = asyncio.Event()

        async def find_group(
            task,
            named_subject,
            count,
            image_source,
            entity,
            visual_subject="",
        ):
            nonlocal calls, in_flight, peak_in_flight
            calls += 1
            index = int(visual_subject.rsplit("-", 1)[1])
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
            try:
                # 같은 URL을 내는 두 번째 그룹을 반드시 먼저 끝낸다. 완료 순서로 배정하는
                # 구현이면 slot 1이 URL을 선점하므로 이 테스트가 결정적으로 실패한다.
                if index == 0:
                    await asyncio.wait_for(second_group_finished.wait(), timeout=0.2)
                elif index == 1:
                    await asyncio.sleep(0.005)
                    second_group_finished.set()
                else:
                    await asyncio.sleep(0.01)
            finally:
                in_flight -= 1
            source_url = (
                "https://shared.example/photo.jpg"
                if index in (0, 1)
                else f"https://host{index}.example/photo.jpg"
            )
            return [
                WebPhoto(
                    data_url="data:image/jpeg;base64,AA==",
                    source_url=source_url,
                    source_host="example",
                    width=1200,
                    height=800,
                    query=visual_subject,
                )
            ]

        monkeypatch.setattr(service, "_photos_for_source", find_group)

        photo_slots, reference_slots, _spares = await service._photo_slots_for_groups(
            task, positions, subjects, None, len(keys)
        )

        assert calls == len(keys)
        assert 1 < peak_in_flight <= PHOTO_SEARCH_GROUP_CONCURRENCY
        # group-1이 먼저 응답하지만 같은 URL은 먼저 입력된 group-0이 가진다.
        assert photo_slots[0].source_url == "https://shared.example/photo.jpg"
        assert photo_slots[1] is None
        assert [photo.source_url for photo in photo_slots[2:] if photo] == [
            f"https://host{index}.example/photo.jpg" for index in range(2, 6)
        ]
        assert reference_slots == [None] * len(keys)


class CountingDigestRepository(InMemoryBlogTaskRepository):
    def __init__(self):
        super().__init__()
        self.digest_calls = 0

    async def list_published_digests(
        self, user_id: str, limit: int = 30, exclude_post_id: str | None = None
    ):
        self.digest_calls += 1
        return await super().list_published_digests(user_id, limit, exclude_post_id)


class TestDraftRetryReadReuse:
    async def test_existing_digests_are_loaded_once_for_all_draft_attempts(self):
        generator = SequenceDraftGenerator([SHORT_RESULT, DRAFT_RESULT])
        repository = CountingDigestRepository()
        service = DraftService(repository=repository, draft_generator=generator)
        await repository.create(build_task())

        await service.generate_draft("post_1", {})

        assert generator.calls == 2
        assert repository.digest_calls == 1


# ------------------------------------------------- M5 렌더링·장면 생성의 겹침


class SlowSceneGenerator:
    def __init__(self, delay: float):
        self._delay = delay
        self.calls = 0

    async def generate_post_image(self, image_input):
        self.calls += 1
        await asyncio.sleep(self._delay)
        from test_card_pipeline import jpeg_data_url

        return GeneratedPostImage(
            data_url=jpeg_data_url(color=(90 + self.calls * 30, 130, 140)),
            alt_text="alt",
            prompt="p",
            provider="openai",
            model="gpt-image-2",
            generated_at=NOW,
            mime_type="image/jpeg",
            source="generated",
        )


class TestRenderAndSceneOverlap:
    async def test_chart_rendering_does_not_delay_image_generation(self, monkeypatch):
        """표·그래프 렌더링(스레드)과 배경 장면 생성(API 대기)이 겹쳐 돈다.
        순차라면 delay*2 이상 걸린다 — wall이 그보다 확실히 짧아야 한다."""
        delay = 0.25

        def slow_render(visual):
            time.sleep(delay)
            from app.modules.draft.visuals import render_planned_visual

            return render_planned_visual(visual)

        monkeypatch.setattr(
            "app.modules.draft.service.render_planned_visual", slow_render
        )

        draft = DRAFT.model_copy(update={"visuals": [BAR_VISUAL]})
        generator = CardPlanningGenerator(
            plan(
                brief("card-0", card_type="THUMBNAIL", section_id=None),
                brief("card-1"),
            ),
            result=draft,
        )
        images = SlowSceneGenerator(delay)
        service, repository = build_card_service(generator, images)
        # 차트는 생성 직후 선택 출처(source-1)의 실측값과 대조된다 — 대조 대상 출처를
        # 의도에 실어 준다(없으면 차트가 검증에서 빠져 렌더링 겹침을 관찰할 수 없다).
        from app.shared import SearchSource, SelectedIntent, SourceDataPoint

        task = build_task()
        task = task.model_copy(
            update={
                "selected_intent": SelectedIntent(
                    intent_id="intent_1",
                    title="AI 블로그 실무 가이드",
                    target_reader="실무자",
                    rationale="실무 적용 관점",
                    sources=[
                        SearchSource(
                            title="KB국민카드 데이터본부",
                            url="https://example.com/report",
                            snippet="생성형 AI 구독 이용 비교",
                            data_points=[
                                SourceDataPoint(label="2025년", value=42.0, unit="%"),
                                SourceDataPoint(label="2026년", value=67.0, unit="%"),
                            ],
                        )
                    ],
                )
            }
        )
        await repository.create(task)

        started = time.monotonic()
        task = await service.generate_draft("post_1", {})
        elapsed = time.monotonic() - started

        assert images.calls == 2  # 썸네일 + 본문 카드
        post = task.final_post
        assert post is not None
        assert any(image.source == "rendered" for image in post.images)
        # 순차(렌더 0.25 → 장면 0.25)면 0.5s+. 겹치면 ~0.25s + 오버헤드.
        assert elapsed < delay * 2 * 0.9, f"렌더링과 장면 생성이 겹치지 않았습니다: {elapsed:.2f}s"


# ------------------------------------------------------------- 공유 HTTP 클라이언트


class TestSharedClient:
    async def test_calls_reuse_one_keepalive_client(self):
        assert shared_client() is shared_client()


# ----------------------------------------------------------------- 성능 계측


class TestPerfTrace:
    async def test_spans_record_wall_and_busy_separately(self):
        trace = perf.start_trace("test-pipeline", "post_1")

        async def stage(name: str, seconds: float):
            with perf.span(name):
                await asyncio.sleep(seconds)

        # 두 구간을 병렬로 — busy(합)는 wall(체감)보다 커야 한다.
        await asyncio.gather(stage("a", 0.05), stage("b", 0.05))
        trace.finish()

        stages = {span.stage for span in trace.spans}
        assert {"a", "b"} <= stages
        assert trace.busy_seconds() >= trace.wall_seconds()

    async def test_provider_calls_are_nested_and_not_double_counted(self):
        trace = perf.start_trace("test-pipeline", "post_1")
        with perf.span("draft_llm_attempt_1"):
            perf.record_provider_call(
                provider="anthropic",
                model="m",
                start=time.monotonic() - 0.01,
                end=time.monotonic(),
                status=200,
                attempts=1,
                response_bytes=10,
            )
        nested = [span for span in trace.spans if span.meta.get("nested")]
        assert len(nested) == 1
        assert nested[0].stage == "draft_llm_attempt_1:call"
        # busy 합계는 상위 구간만 센다 — provider 호출을 이중으로 더하지 않는다.
        top = [span for span in trace.spans if not span.meta.get("nested")]
        assert abs(trace.busy_seconds() - sum(span.duration for span in top)) < 1e-6

    async def test_span_without_a_trace_is_a_safe_no_op(self):
        perf.current_trace.set(None)
        with perf.span("web_research"):
            pass  # 예외 없이 지나가면 된다
