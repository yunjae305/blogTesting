"""블로그 태스크 상태 머신.

문자열 값은 통신·저장 형식이므로 바뀌면 안 된다.
"""

from enum import StrEnum


class BlogTaskStatus(StrEnum):
    INPUT = "INPUT"
    REFERENCE_PROCESSING = "REFERENCE_PROCESSING"
    SEARCH_ANALYZING = "SEARCH_ANALYZING"
    INTENT_SELECTED = "INTENT_SELECTED"
    GENERATING = "GENERATING"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    POSTING = "POSTING"
    POSTED = "POSTED"
    POSTING_NEEDS_HUMAN = "POSTING_NEEDS_HUMAN"
    FAILED = "FAILED"
    CONTENT_POLICY_VIOLATION = "CONTENT_POLICY_VIOLATION"


ALLOWED_TRANSITIONS: dict[BlogTaskStatus, list[BlogTaskStatus]] = {
    BlogTaskStatus.INPUT: [BlogTaskStatus.REFERENCE_PROCESSING, BlogTaskStatus.FAILED],
    BlogTaskStatus.REFERENCE_PROCESSING: [BlogTaskStatus.SEARCH_ANALYZING, BlogTaskStatus.FAILED],
    BlogTaskStatus.SEARCH_ANALYZING: [
        BlogTaskStatus.INTENT_SELECTED,
        BlogTaskStatus.FAILED,
        # 제목 다시 고르기. 제목을 고르는 순간 글은 여기로 오는데, 방향을 확정하기 전까지는
        # 제목을 바꿀 수 있어야 한다 — 검증 팝업의 '수정하기'로 제목 단계에 돌아와도 M2가
        # REFERENCE_PROCESSING만 받아 제목이 통째로 잠기던 것을 푼다. 같은 상태로 다시
        # 들어오는 자기 간선이며, 옛 제목으로 만든 검증 결과는 그때 함께 버린다
        # (modules/trend/service.py select_topic).
        BlogTaskStatus.SEARCH_ANALYZING,
    ],
    BlogTaskStatus.INTENT_SELECTED: [BlogTaskStatus.GENERATING, BlogTaskStatus.FAILED],
    BlogTaskStatus.GENERATING: [
        BlogTaskStatus.READY_TO_PUBLISH,
        BlogTaskStatus.CONTENT_POLICY_VIOLATION,
        BlogTaskStatus.FAILED,
        # 되돌리기. 원고를 만들던 프로세스가 죽으면 글이 GENERATING에 영영 남아 화면의
        # 스피너가 멈추지 않는다(버튼은 '진행 중'이라며 막혀 있다). 시작 시 복구 스위퍼가
        # 직전 상태로 되돌려 사용자가 다시 누를 수 있게 한다(modules/blog_task/recovery.py).
        BlogTaskStatus.INTENT_SELECTED,
    ],
    BlogTaskStatus.READY_TO_PUBLISH: [BlogTaskStatus.POSTING, BlogTaskStatus.FAILED],
    BlogTaskStatus.POSTING: [
        BlogTaskStatus.POSTED,
        BlogTaskStatus.POSTING_NEEDS_HUMAN,
        BlogTaskStatus.FAILED,
    ],
    BlogTaskStatus.POSTED: [],
    BlogTaskStatus.POSTING_NEEDS_HUMAN: [BlogTaskStatus.POSTING, BlogTaskStatus.FAILED],
    BlogTaskStatus.FAILED: [],
    BlogTaskStatus.CONTENT_POLICY_VIOLATION: [],
}


def can_transition(current: BlogTaskStatus, next_status: BlogTaskStatus) -> bool:
    return next_status in ALLOWED_TRANSITIONS.get(current, [])
