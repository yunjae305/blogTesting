"""네이버 발행 자동화에서 쓰는 URL·셀렉터·타임아웃 상수 모음."""

WRITE_URL = "https://blog.naver.com/{blog_id}?Redirect=Write"
# 네이버가 **지금 로그인한 계정의** 블로그 글쓰기로 직접 보내 주는 주소. blog_id가 필요
# 없다. 설정에 저장된 blog_id가 지금 로그인한 계정의 블로그가 아니면 위 WRITE_URL은
# "게시물이 삭제되었거나 다른 페이지로 변경되었습니다" 안내창으로 막히는데, 이 주소는
# 계정을 보고 알아서 옮겨 준다. 블로그 홈의 '글쓰기' 버튼이 가리키는 곳이기도 하다.
WRITE_REDIRECT_URL = "https://blog.naver.com/GoBlogWrite.naver"
BLOG_URL = "https://blog.naver.com/{blog_id}"
# 설정에서 네이버 계정을 바꿨을 때 이전 계정 세션을 끊는 데 쓴다. 이 주소가 .naver.com
# 도메인의 로그인 쿠키를 지우므로 blog.naver.com 쪽 세션도 함께 끊긴다.
LOGOUT_URL = "https://nid.naver.com/nidlogin.logout"
LOGIN_URL = "https://nid.naver.com/nidlogin.login"
LOGIN_HOST = "nid.naver.com"
DEVICE_CONFIRM_PATH = "/login/ext/deviceConfirm"

SESSION_COOKIES = ("NID_AUT", "NID_SES")
EDITOR_FRAME = "#mainFrame"

TITLE_SELECTORS = (
    ".se-documentTitle [contenteditable='true']",
    ".se-title-text [contenteditable='true']",
    ".se-documentTitle .se-text-paragraph",
    ".se-title-text .se-text-paragraph",
    "[data-a11y-title='제목']",
)
BODY_SELECTORS = (
    ".se-component.se-text [contenteditable='true']",
    ".se-main-container [contenteditable='true']",
    ".se-component.se-text .se-text-paragraph",
    ".se-main-container .se-text-paragraph",
)
DRAFT_CANCEL_XPATHS = (
    "//button[normalize-space()='취소' and "
    "ancestor::*[contains(normalize-space(.), '작성 중인 글이 있습니다')]]",
    "//*[normalize-space()='작성 중인 글이 있습니다']"
    "/following::button[normalize-space()='취소'][1]",
)
# 고정 글쓰기 URL(WRITE_URL) 진입이 실패했을 때, 블로그 홈에서 글쓰기로 들어가는
# 대체 경로에서 쓰는 '글쓰기' 버튼/링크 셀렉터. 기본 실패 시 순서대로 시도한다.
WRITE_BUTTON_SELECTORS = (
    "a[href*='Redirect=Write']",
    "a[ng-href*='Redirect=Write']",
    "a[href*='PostWriteForm']",
    "a[href*='GoBlogWrite']",
    "a.btn_write",
    ".btn_write a",
    "[data-click-area='wtb.write']",
)
PUBLISH_OPEN_SELECTORS = (
    "button.publish_btn__m9KHH",
    "button[class*='publish_btn']",
    "[data-click-area='tpb.publish']",
)
PUBLISH_CONFIRM_SELECTORS = (
    "button.confirm_btn__WEaBq",
    ".layer_btn_area button",
    "button[data-testid='seOnePublishBtn']",
)
# 임시저장은 발행 버튼 바로 옆의 '저장' 버튼이다 — 같은 상단 바(tpb)에 있고, 하는 일은
# 클릭 대상만 다르다. 예전 선택자('tempsave'·'temp_save'·'draft')는 어느 것도 실제
# 에디터의 버튼과 맞지 않아 임시저장이 조용히 실패했다. 발행(PUBLISH_OPEN_SELECTORS)과
# 같은 형태로 맞춘다.
TEMP_SAVE_SELECTORS = (
    "button[class*='save_btn']",
    "[data-click-area='tpb.save']",
    "button[class*='tempsave']",
    "button[class*='temp_save']",
)

LOGIN_REJECTED_MARKERS = ("비밀번호가 잘못", "로그인 정보가 일치하지")
LOGIN_BUTTON_SELECTORS = (
    "#loginBtn_row",      # 2026-07 현재 기본 ID/PW 로그인 폼
    "#loginBtn_column",   # 좁은 화면/열 배치 변형
    "#log\\.login",      # 구형 네이버 로그인 폼
    "button.btn_done",    # 클래스 기반 최종 폴백
)
HUMAN_CHECK_TIMEOUT_SECONDS = 180
# 설정 화면에서 누른 로그인은 사람이 화면 앞에 있다. 휴대폰을 찾아 2단계 인증 알림을
# 승인하는 데 3분은 짧아서, 그 경로만 더 기다린다. 발행 중에는 180초 그대로다 -- 거기서
# 오래 붙잡으면 예약 발행이 통째로 밀린다.
SETTINGS_LOGIN_TIMEOUT_SECONDS = 420
# 2단계 인증 화면의 '이 브라우저는 2단계 인증 없이 로그인 합니다'를 눌러 볼 횟수.
# 화면이 늦게 그려질 수 있어 한 번으로는 놓친다. 켜는 데 성공하면 더 누르지 않는다 —
# 다시 누르면 방금 켠 체크가 도로 꺼진다.
TRUST_BROWSER_ATTEMPTS = 5
# 브라우저를 정상 종료(CDP Browser.close)한 뒤 크롬이 쿠키를 디스크에 쓰고 빠져나갈
# 때까지 기다리는 시간. 바로 quit()하면 그 사이에 프로세스가 죽어 쿠키가 사라진다.
CLOSE_FLUSH_SECONDS = 2.0
PAGE_LOAD_TIMEOUT_SECONDS = 60
ELEMENT_TIMEOUT_SECONDS = 15
# 본문 스캐폴드(전체 텍스트+앵커) 붙여넣기가 DOM에 반영되기까지의 대기. 긴 글은 네이버가
# 컴포넌트 변환에 시간이 걸린다. 성공 판정은 시간이 아니라 DOM 상태(WebDriverWait)로 한다.
SCAFFOLD_PASTE_TIMEOUT_SECONDS = 45
# 붙여넣기 재시도 횟수. OS 클립보드는 사용자와 공유된다 — 예약 발행이 뒤에서 도는 동안
# 사용자가 복사·화면 캡처를 하면 클립보드가 겹쳐 빈 붙여넣기가 된다(2026-08-04 실사용).
# 재시도마다 클립보드에 다시 넣으므로 한 번의 겹침은 스스로 낫는다.
TITLE_PASTE_ATTEMPTS = 3
# 본문은 하나도 안 붙었을 때만 다시 시도한다(일부 붙은 채 또 붙이면 중복된다).
SCAFFOLD_PASTE_ATTEMPTS = 2
# 앵커 문단 하나가 이미지 컴포넌트로 바뀌기까지의 대기(업로드·렌더 포함).
IMAGE_PASTE_TIMEOUT_SECONDS = 30
# 이미 넣은 이미지들이 네이버 서버에서 내려와 **로드를 마칠** 때까지의 대기. 이걸
# 기다리지 않고 다음 앵커를 클릭하면, 로딩이 끝나며 늘어난 높이만큼 문서가 밀려
# 클릭이 엉뚱한 곳에 떨어진다(실발행: 표 뒤 3번째 앵커가 다른 자리에 삽입됨).
IMAGE_LOAD_TIMEOUT_SECONDS = 20
# 앵커 문단의 화면 위치가 이 시간(초) 동안 변하지 않아야 '레이아웃이 멈췄다'로 본다.
LAYOUT_SETTLE_INTERVAL_SECONDS = 0.35
LAYOUT_SETTLE_TIMEOUT_SECONDS = 8
# 선택이 실제로 토큰을 잡을 때까지의 재시도 횟수(클릭이 빗나가는 경우 대비).
ANCHOR_SELECT_ATTEMPTS = 3
# 붙여넣기가 엉뚱한 곳에 들어갔을 때 되돌리고 다시 시도하는 횟수.
ANCHOR_PASTE_ATTEMPTS = 3
# 이미지를 클릭해 컴포넌트를 선택한 뒤 '사진 설명' 칸이 화면에 나타나기까지의 대기.
# 즉시 조회하면 아직 크기 0이라 못 찾는다 — 실발행에서 캡션이 조용히 빠졌던 원인 후보.
CAPTION_FIELD_TIMEOUT_SECONDS = 5
# 교체한 이미지 컴포넌트가 DOM에 자리를 잡기까지의 대기. 캡션 칸과 달리 이쪽은 기다리지
# 않고 한 번만 찾아, 아직 그려지지 않은 앞쪽 이미지들이 설명을 통째로 잃었다
# (2026-08-10 실발행: 1·2번째는 "찾지 못해", 3번째는 성공 — 그 사이에 시간이 흘렀다).
CAPTION_IMAGE_TIMEOUT_SECONDS = 5
# 캡션을 붙여넣은 뒤 실제로 칸에 반영됐는지 읽어서 확인하기까지의 대기.
CAPTION_VERIFY_TIMEOUT_SECONDS = 3
# 글자 사이 기본 지연. 실제 간격은 여기에 무작위 흔들림(최대 1.5배)이 더해져 글자마다
# 다르다 — 기계처럼 같은 간격으로 치면 봇으로 판정된다(2026-08-18 사용자 지적). 90ms
# 기준으로 90~225ms, 사람이 아이디·비밀번호를 치는 속도다.
LOGIN_EVENT_DELAY_MILLISECONDS = 90
LOGIN_KEYSTROKE_DELAY_SECONDS = 0.05
LOGIN_FIELD_PAUSE_SECONDS = 0.6
DRAFT_POPUP_TIMEOUT_SECONDS = 5
