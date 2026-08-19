"""네이버 SmartEditor ONE 발행 자동화 패키지.

네이버에는 글쓰기 API가 없어 사용자의 PC에서 실제 Chrome을 연다. 브라우저 프로필은
저장소 루트의 ``.naver-profile``에 유지하고, 로그인 정보는 세션이 없을 때만 쓴다. 캡차나
2단계 인증이 나오면 보이는 브라우저에서 사용자가 직접 처리한다.

기능별로 파일이 나뉘어 있다:
- ``constants``  : URL·셀렉터·타임아웃 상수
- ``browser``    : 크롬 실행/드라이버, 세션 쿠키 확인
- ``clipboard``  : 제목·본문 클립보드 쓰기 (Windows는 CF_HTML)
- ``login``      : 네이버 로그인
- ``editor``     : 스마트에디터 조작 (붙여넣기·태그·발행)
- ``publisher``  : 발행 오케스트레이션과 진입점

기존 ``app.posting.naver`` 로의 import가 그대로 동작하도록 공개 이름을 여기서 재노출한다.
"""

# 테스트가 app.posting.naver.time.sleep 을 patch하므로 time을 노출해 둔다.
import time  # noqa: F401

# 밑줄 이름은 패키지 밖에서 실제로 쓰는 것만 남긴다: _NeedsHuman(routes),
# _in_browser_thread·_type_input_value(테스트·threads_browser). 나머지 내부 이름은
# 각 모듈 경로에서 직접 import한다.
from .browser import (
    NaverConfig,
    _in_browser_thread,
    _NeedsHuman,
)
from .editor import SmartEditorOne, article_html
from .login import NaverLogin, _type_input_value
from .plan import (
    NaverImageAnchor,
    NaverPlanError,
    NaverPublishPlan,
    build_naver_publish_plan,
)
from .publisher import (
    NaverBrowserPublisher,
    fill_editor_for_preview,
    log_in_and_store_session,
)

__all__ = [
    "NaverConfig",
    "NaverBrowserPublisher",
    "NaverImageAnchor",
    "NaverLogin",
    "NaverPlanError",
    "NaverPublishPlan",
    "SmartEditorOne",
    "article_html",
    "build_naver_publish_plan",
    "log_in_and_store_session",
    "fill_editor_for_preview",
]
