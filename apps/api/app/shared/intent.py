"""의도(intent) 모델.

WebSearchAnalysisInput은 llm_io.py에 있다 — BlogTaskInput에 의존하기 때문에,
여기서 빼두어야 모델 임포트가 순환하지 않는다.
"""

from .base import CamelModel

# 수집 자료의 성격. 원고가 뉴스·보도자료에만 기대지 않도록 자료를 이 다섯으로 분류하고
# 화면에서 타입별로 보여준다. OFFICIAL=공식자료, NEWS=뉴스, BLOG=블로그·후기,
# REPORT=통계·보고서, CASE=활용 사례.
SOURCE_TYPES: tuple[str, ...] = ("OFFICIAL", "NEWS", "BLOG", "REPORT", "CASE")


class SourceDataPoint(CamelModel):
    """자료에 실린 실측 수치 하나. 원고의 통계 문장과 그래프는 여기 담긴 숫자만 쓴다 —
    없는 수치를 지어내지 못하게 원천(M3 요약)에서 붙잡아 둔다."""

    label: str
    value: float
    unit: str | None = None


class SearchSource(CamelModel):
    title: str
    url: str
    snippet: str
    # 자료 분류(SOURCE_TYPES). 예전 문서·Gemini 원본 출처에는 없어 기본값을 둔다.
    source_type: str = ""
    # 소재·트렌드·독자 부합 관련도 0-100. 화면 기본 선택과 정렬에 쓴다.
    relevance_score: int = 0
    # 자료의 실측 수치 목록. 수치가 없는 자료는 None/빈 목록.
    data_points: list[SourceDataPoint] | None = None


class IntentCandidate(CamelModel):
    intent_id: str
    title: str
    target_reader: str
    rationale: str
    keywords: list[str]
    sources: list[SearchSource]


class IntentValidationResult(CamelModel):
    prompt_version: str
    provider: str
    model: str
    analyzed_at: str
    intent_candidates: list[IntentCandidate]
    #: 검색이 **실제로 찾아 온 자료의 총 개수**. 방향 하나에 붙는 자료는 상한이 있어서
    #: (INTENT_SOURCE_MAX) 화면에 다 보이지 않는데, 그 상한 때문에 사용자는 "자료를 이만큼
    #: 밖에 못 찾았나"로 읽었다(2026-08-07 신고). 화면이 '외 N개'를 적을 수 있게 남긴다.
    #:
    #: 기본값 0은 **옛 문서**다 — 이 필드가 생기기 전에 저장된 검증 결과에는 값이 없고,
    #: 그때 화면은 '외 N개'를 그리지 않는다(없는 숫자를 지어내지 않는다).
    collected_source_count: int = 0
