"""LLM 프로바이더에 넘기는 입력 타입.

BlogTaskInput을 참조하기 때문에 intent.py / draft.py에서 분리했다. 그러지 않으면
blog_task.py <-> intent.py/draft.py가 순환한다.
"""

from .base import CamelModel
from .blog_task import BlogTaskInput
from .draft import (
    ContentPlan,
    DraftFormat,
    DraftGenerationResult,
    DraftGenerationSettings,
    EditorialStylePlan,
    FinalPost,
    IntentAnchor,
    ReferenceEvidenceProfile,
    SelectedIntentForDraft,
    SeoKeywordPlan,
    TitlePlan,
)


class WebSearchAnalysisInput(CamelModel):
    post_id: str
    user_id: str
    input: BlogTaskInput
    prompt_version: str
    # 사용자가 M2에서 고른 트렌드 검색 키워드 문자열(trendSelection.selectedKeywords).
    # BlogTaskInput.keywords는 이름과 달리 목적(purpose)의 옛 필드라 검색어가 아니다 —
    # 실제 검색어는 여기로 실어 날라야 M3 수집이 그 키워드로 검색한다.
    # 트렌드를 건너뛴 글·옛 호출에는 없으므로 기본값은 빈 목록이고, 그때는 예전과 같다.
    selected_keywords: list[str] = []
    #: 지금 **자료를 모을 것인가**(2026-08-12 사용자 결정).
    #:
    #:     "설정한 편수가 한편일때만 검증단계에서 자료수집해서 사용자에게 보여주고
    #:      2편 이상으로 설정한 경우에는 자료수집은 원고생성 단계 진입했을때"
    #:
    #: 검증 화면이 자료를 보여 주는 것은 **사용자가 그 자리에서 자료를 고를 때**만 뜻이
    #: 있다. 여러 편을 만들거나 시각을 정해 둔 글은 여기서 방향만 고르고 원고는 나중에
    #: 만드는데, 그때 자료를 새로 모으므로(refresh_selected_intent_sources) 지금 모은 것은
    #: 버려진다 — 1~2분과 검색 비용만 쓰는 셈이다.
    #:
    #: False여도 **방향 후보는 그대로 4개**를 만든다. 자료 없이 소재·목적·제목만 보고
    #: 각도를 나누고, 각 후보의 sources는 빈 목록이다.
    collect_sources: bool = True


class DraftGenerationInput(CamelModel):
    post_id: str
    user_id: str
    input: BlogTaskInput
    selected_intent: SelectedIntentForDraft
    prompt_version: str
    settings: DraftGenerationSettings | None = None
    style: str | None = None
    format: DraftFormat
    # 사용자가 M2에서 고른 트렌드 제목(trendSelection.finalTopic). 트렌드를 건너뛴 글에는
    # None이며, 값이 있을 때만 원고 프롬프트에 트렌드 연결 지침과 제목 앵커가 붙는다.
    trend_title: str | None = None
    # 사용자가 M2에서 고른 **원본 검색 키워드**. 검색 의도를 나타내는 검색어 조합이지,
    # 문장 속 명사가 아니다("창섭 전과자"). 글에 쓸 표현은 여기서 나오지 않고
    # reference_evidence.content_entity가 정한다 — 이 값은 무엇을 해석해야 하는지와
    # 무엇을 그대로 복사하면 안 되는지의 기준으로만 쓴다. 옛 문서·건너뛴 글에는 없다.
    raw_keywords: list[str] = []
    # 글의 방향(선택 의도 + 의도 키워드 + 제목 후킹). 원고 프롬프트에만 실린다 — 콘텐츠
    # 설계 프롬프트와 설계 캐시 키는 건드리지 않으므로 선행 생성(prefetch)의 캐시 적중이
    # 그대로 유지된다. 만들지 못한 경우(의도 없음)에는 None이고, 그러면 프롬프트는 예전과
    # 한 글자도 달라지지 않는다.
    intent_anchor: IntentAnchor | None = None
    # 원고보다 먼저 확정된 제목 계획(BlogTask.title_plan에서 실어 온다). 있으면 원고는
    # 제목을 짓지 않고 primary_title을 그대로 쓴다. 콘텐츠 설계 캐시 키에도 들어가므로,
    # 선행 생성과 실제 생성이 반드시 같은 계획을 봐야 한다 — 그래서 매번 새로 만들지 않고
    # 저장된 것을 읽는다.
    title_plan: TitlePlan | None = None
    # 원고보다 먼저 확정한 SEO 키워드 계획(BlogTask.seo_keyword_plan에서 실어 온다). 있으면
    # 원고 프롬프트에 SEO 규칙 블록이 붙고, 생성 후 SEO 검증이 primary의 제목·첫 문단 반영을
    # 확인한다. 없으면(생성 실패·구형 어댑터·옛 문서) 프롬프트와 검증은 예전과 동일하다.
    seo_keyword_plan: SeoKeywordPlan | None = None
    # 직전 생성이 품질 검사에 걸렸을 때, 그 실패 사유를 다음 시도 프롬프트에 넣기 위한 것.
    # 첫 시도에는 항상 None이라 프롬프트가 예전과 한 글자도 달라지지 않는다. 값이 있으면
    # "전체를 새로 쓰지 말고 이 문제만 고쳐라"는 수정 지시가 프롬프트 앞머리에 붙는다.
    revision_notes: list[str] | None = None
    # 품질 검사에서 반려된 직전 원고. 수정 재시도에서는 지적사항만 보내지 않고 실제 원고도
    # 함께 보내야 모델이 사실과 구성을 유지한 채 고칠 수 있다.
    previous_draft: FinalPost | None = None
    # 본문 생성 전에 만든 콘텐츠 설계. 있으면 원고 프롬프트가 이 설계(섹션 구조·시각자료
    # 계획)를 따르라고 지시한다. 설계 생성이 실패하면 None으로 두고 설계 없이 쓴다 —
    # 설계는 품질 장치이지, 원고 생성을 막을 이유가 아니다.
    content_plan: ContentPlan | None = None
    # 참고자료를 근거 정보로 정리한 것. 원고·사진 계획·이미지 프롬프트가 같은 사실을 본다.
    # 참고자료가 없으면 has_references=False인 빈 프로필이고, 그때 관련 블록은 프롬프트에서
    # 통째로 빠진다(예전과 같은 프롬프트).
    reference_evidence: ReferenceEvidenceProfile | None = None
    # 이 글의 편집·시각 스타일. 글의 형태(아키타입)·리듬·시각자료 예산이 여기서 온다.
    # 없으면 원고 프롬프트가 예전의 고정 구조 규칙을 쓴다.
    editorial_style: EditorialStylePlan | None = None


# 저장점 단계 표식. 지금은 본문 완성(이미지 이전) 하나뿐이지만, 값을 문서에 남기므로
# 단계가 늘어도 옛 저장점을 안전하게 구분할 수 있다.
DRAFT_CHECKPOINT_STAGE_DRAFT_READY = "draft_ready"


class DraftCheckpoint(CamelModel):
    """원고 생성 중간 저장점 — 본문(텍스트)까지 끝난 결과를 이미지 단계 전에 남긴다.

    이미지 생성이 실패해 글이 FAILED로 떨어져도 완성된 본문은 멀쩡하다. 재실행이 이
    저장점을 읽으면 참고자료 근거·제목·편집 스타일·설계·본문 생성을 건너뛰고 실패한
    이미지 단계부터 다시 시작한다. 입력 지문(fingerprint)이 다르면 무시한다 — 사용자가
    스타일·설정을 바꿨으면 본문도 다시 써야 한다. 생성이 끝까지 성공하면 지운다.

    BlogTask 모델에는 싣지 않는다: blog_task.py가 이 모듈을 참조하면 순환이고,
    무엇보다 클라이언트 조회 응답에 내려보낼 내용이 아니다(문서의 별도 필드로만 산다).
    """

    fingerprint: str
    stage: str
    draft_input: DraftGenerationInput
    result: DraftGenerationResult
    saved_at: str
