# Blog-it

**Blog-it은 블로그 글 한 편을 쓰는 전 과정을 자동화하는 웹 앱입니다.**

무엇에 대해 쓸지만 정하면 — 지금 뜨는 트렌드 키워드를 찾아 제목을 만들고, 웹에서 자료를
모아 검증하고, 원고와 이미지를 생성해서, 네이버 블로그와 스레드에 발행까지 합니다.
소재를 여러 개 모아 두고 제목·방향까지 알아서 정하는 **자동 포스팅**도 있습니다.

네이버·Threads 로그인 식별자와 비밀번호는 서버 DB로 가지 않고 로컬에서 암호화되어
보관됩니다. Windows Server로 옮길 때는 DPAPI 파일을 그대로 복사하지 말고
[Windows Server 보안 배포 가이드](docs/Windows-Server-보안-배포.md)의 이관 순서를 따릅니다.

---

## 무엇을 할 수 있나

### 1. 글 한 편 쓰기 — 5단계

| 단계 | 하는 일 |
| --- | --- |
| **소재** | 무엇에 대해 쓸지 · 글 목적 · 대상 연령 · 카테고리(넷 다 필수). 브랜드와 참고 자료(URL·파일·메모)는 선택 |
| **제목** | 지금 뜨는 트렌드 키워드로 제목 후보를 만듭니다. 건너뛰어도 됩니다 |
| **검증** | 웹에서 모은 자료와 글의 방향 후보 4개를 확인하고, 쓸 자료를 고릅니다 |
| **원고** | 자료를 근거로 본문을 쓰고 이미지를 만듭니다. 문체는 설정한 페르소나를 따릅니다 |
| **발행** | 복사해서 쓰거나, 네이버·스레드에 바로 올립니다 |

제목을 고르면 자료 검증이 뒤에서 바로 시작되고, `작성 전 검증으로` 버튼으로 다음 단계로
넘어갑니다.

소재 칸 아래에서 **활용할 브랜드·서비스**를 고르면 등록해 둔 소개·핵심 기능·기준표가
참고 자료로 얹힙니다. **소재와 브랜드를 함께 고를 수 있고**, 그때 브랜드가 무엇을 맡을지
고릅니다(2026-08-19):

| 고른 역할 | 어떤 글이 나오나 |
| --- | --- |
| **활용한 도구로** | 소재·트렌드가 주인공이고, 브랜드는 그 상황에서 쓴 도구로 등장합니다. 분량은 트렌드 70 : 활용 20 : 정리 10 |
| **글의 주인공으로** | 브랜드 자체를 소개하는 글입니다. 소재를 비우면 자동으로 이쪽입니다 |

'활용한 도구로'를 고르면 그 소재에 브랜드를 얹는 것이 자연스러운지 **A·B·C**로 미리
보여 줍니다. 판단 기준은 브랜드 자료의 기준표("이런 상황이면 이 기능")이고, 억지
조합(C)은 한 번 막습니다. 자세한 운영 방법은
[AIONA 콘텐츠 운영](docs/AIONA-콘텐츠-운영.md)에 있습니다.

소재 단계 오른쪽에서 이 글을 **언제(지금/예약) · 몇 편(1~3) · 어디에(네이버/스레드)** 낼지
정합니다. 예약을 고르면 정한 시각에 자료를 새로 모아 원고를 만들고 그대로 발행합니다.

### 2. 자동 포스팅 — 모아 두고 맡기기

소재를 여러 개 넣어 두면 알아서 만들어 발행합니다. 글은 위의 5단계를 화면 없이 밟을
뿐이라, 예약으로 나간 글과 손으로 쓴 글은 품질 경로가 같습니다.

| 어디서 거나 | 무엇을 정하나 | 언제 올라가나 |
| --- | --- | --- |
| **자동 포스팅** 탭 | 소재 · 플랫폼 · 분야(선택) · 작업 시각(선택)은 줄마다, 활용할 브랜드(선택)는 배치에 하나 | 시각을 적은 줄은 그 시각에, 비운 줄은 앞 글이 발행된 뒤 |
| **새 글 작성** | 목적·연령대·제목·방향까지 직접 | `예약 발행`을 고르고 적은 시각에 (한 소재로 최대 3편) |

| 값 | 범위 |
| --- | --- |
| 소재 수 / 글의 개수 | 1 ~ 20개 |
| 한 소재로 만들 원고 수(새 글 작성) | 1 ~ 3편 |
| 시각을 적은 글 사이 간격 | 최소 12분 (발행이 사용자당 하나씩 돌기 때문) |

- 원고는 여러 편을 동시에 만듭니다(최대 3편). 발행은 사용자당 하나씩입니다.
- **예약 시각은 '발행을 시작하는 시각'**입니다. 게시 완료는 30초~2분 뒤이고, 발행
  내역에 두 시각이 함께 남습니다.
- 진행·결과는 **작업 관리** 탭에서 봅니다: 일시정지 ↔ 계속, 새 예약 시작, 작업 빼기,
  실패 재시도. 브라우저를 닫아도 예약은 계속 돕니다(돌리는 것은 백엔드입니다).

### 3. 발행 — 네이버와 스레드

- **네이버 블로그** — 크롬을 열어 사람이 하듯 스마트에디터에 붙여넣고 발행합니다(이미지 포함).
- **스레드** — 글 한 편을 여러 게시물로 나눠 연속으로 올립니다.
- 둘 다 고르면 네이버가 먼저 올라가고, 같은 원고가 스레드로 이어집니다.
- 로그인은 처음 한 번입니다. 세션이 남아 그다음부터는 로그인창 없이 발행됩니다.

### 화면 여섯 개

| 탭 | 하는 일 |
| --- | --- |
| **홈** | 진행 중인 글과 상태 요약 |
| **새 글 작성** | 5단계 마법사 (예약 발행도 가능) |
| **자동 포스팅** | 여러 소재를 한 번에 걸기 |
| **작업 관리** | 걸어 둔 작업의 큐·진행 상황·발행 내역 |
| **내 글 목록** | 만든 글 보기·복사·삭제 |
| **설정** | 페르소나, 글 길이, 해시태그 수, 네이버·스레드 계정 |

---

## 기술 스택

| 영역 | 기술 | 위치 |
| --- | --- | --- |
| 프론트엔드 | React 19 + TypeScript + Vite | `apps/web` |
| 백엔드 | Python + FastAPI | `apps/api` |
| DB | MongoDB (로컬 27017 또는 Atlas) | `.env`의 `MONGODB_URI` |
| 외부 AI | Anthropic / OpenAI / Gemini | `apps/api/app/llm` |
| 트렌드 수집 | SerpApi(구글) · 네이버 · 유튜브 · 인스타그램 | `apps/api/app/llm/trends` |
| 발행 자동화 | Selenium + undetected-chromedriver (Chrome) | `apps/api/app/posting` |
| 예약 실행 | FastAPI asyncio 백그라운드 워커 | `apps/api/app/modules/scheduled_posting` |

```text
React (5173)  ──HTTP──▶  FastAPI (3000)  ──▶  MongoDB (27017)
                                         ├──▶  외부 AI API (원고·이미지·자료)
                                         └──▶  Chrome (네이버·스레드 발행, 라이브 뷰 중계)
```

---

## 시작하기

### 필요한 것

- **Python 3.11 이상** — 백엔드
- **Node.js 22 이상** — 프론트엔드 빌드 도구
- **MongoDB** — 로컬(27017) 또는 Atlas
- **Google Chrome** — 네이버·스레드 발행

### 처음 한 번 설치

```bash
git clone https://github.com/g-rnd-winz/Blog-it.git
cd Blog-it

pip install -r apps/api/requirements.txt   # 백엔드
npm install                                # 프론트엔드

python apps/api/scripts/init_mongo.py      # MongoDB 컬렉션·인덱스 생성
cp .env.example .env                       # Windows: copy .env.example .env — AI 키 채우기
```

### 실행 — 개발

터미널 두 개, 둘 다 저장소 루트에서:

```bash
python apps/api/scripts/dev.py    # 백엔드 (3000, 자동 재시작)
npm run dev                       # 프론트엔드 (5173) — 여기로 접속
```

### 실행 — 운영(서버)

프론트를 빌드해 두면 백엔드 하나가 화면까지 서빙합니다:

```bash
npm run build
python apps/api/scripts/serve.py    # 포트는 .env의 PORT(기본 3000)를 따릅니다
```

`dev.py`/`serve.py`는 필요한 uvicorn 플래그를 모아 둔 실행기입니다(reload 범위 제한,
graceful shutdown, 콘솔 빠른편집 해제, 로그 큐 분리 등 — 파일 머리의 주석 참고).
서버 배포·반영 절차는 [docs/서버-반영-루틴.md](docs/서버-반영-루틴.md)에 있습니다.

### 로그인

미리 만들어 둔 계정은 없습니다 — 아무 이메일과 8자 이상 비밀번호로 가입합니다.
MongoDB가 꺼져 있으면 인메모리로 동작하며 **재시작 시 데이터가 사라집니다**
(`/health`의 `storageStatus`로 확인).

---

## 주요 명령어

| 명령어 | 설명 |
| --- | --- |
| `python apps/api/scripts/dev.py` | 백엔드 실행 (개발, 자동 재시작) |
| `python apps/api/scripts/serve.py` | 백엔드 실행 (운영, `.env`의 PORT) |
| `cd apps/api && python -m pytest` | 백엔드 테스트 |
| `python apps/api/scripts/init_mongo.py` | MongoDB 컬렉션·validator·인덱스 생성/갱신 |
| `python apps/api/scripts/check_mongo.py` | MongoDB 연결 진단 |
| `python apps/api/scripts/naver_login.py` | 네이버에 미리 로그인해 세션 만들기 |
| `npm run dev` | 프론트엔드 개발 서버 (5173) |
| `npm run build` | 프로덕션 빌드 (`apps/web/dist`) |
| `npm test` | 프론트엔드 + 백엔드 테스트 전체 |

> **저장 모델에서 필드를 지웠다면** `scripts/init_mongo.py`의 `required`도 함께 고치고
> 다시 실행해야 합니다. 안 하면 검증기가 저장을 거부합니다. (필드 추가는
> `additionalProperties: true`라 그대로 통과합니다.)

---

## 환경 변수

템플릿은 `.env.example`, 실제 키는 `.env`. **API 키는 절대 커밋하지 마세요.**

### 기본

| 변수 | 설명 | 기본값 |
| --- | --- | --- |
| `PORT` | 백엔드 포트 | `3000` |
| `APP_ENV` | 실행 환경 (`development` / `production`) | `development` |
| `MONGODB_URI` | MongoDB 접속 주소 | `mongodb://localhost:27017/blog_it` |
| `ALLOW_IN_MEMORY_STORAGE` | MongoDB 장애 시 메모리 저장소 허용 | 개발 `true`, 운영 `false` 권장 |
| `WEB_DIR` | 백엔드가 서빙할 빌드 결과 경로 | `apps/web/dist` |
| `REDIS_URL` | 트렌드 캐시 + 노출 이력 + 잡 임차 | 비어 있음 → 디스크·메모리 |
| `EMAIL_ENC_KEY` / `EMAIL_INDEX_KEY` | 계정 이메일 암호화·조회 색인 키 | 비어 있음 |
| `POSTING_CREDENTIALS_KEY` | 발행 계정 비밀번호 portable AES 키 | Windows 미설정 시 DPAPI |
| `AUTH_TOKEN_SECRET` | 로그인 세션 서명 키 | 운영에서는 필수 |
| `NAVER_PASTE_MODE` | 발행 입력 모드: `auto`(합성 시도 → 거부 시 그 발행만 클립보드) · `synthetic`(엄격, 클립보드 미사용) · `clipboard`(기존 경로) | `auto` |
| `KEEP_OPEN_BROWSER_TTL_MINUTES` | 발행 후 확인용 크롬을 닫기까지의 시간 | `15` |
| `SCHEDULED_MAX_CONCURRENT_PUBLISH` | 서버 전체 동시 발행 크롬 상한 | `10` |

**`EMAIL_INDEX_KEY`는 팀 전원이 같은 값을 쓰고 절대 바꾸지 않습니다.** 키 회전 절차는
[docs/Windows-Server-보안-배포.md](docs/Windows-Server-보안-배포.md) 참고.

### AI (필수)

| 변수 | 쓰이는 곳 |
| --- | --- |
| `ANTHROPIC_API_KEY` | 제목 추천, 원고 생성 |
| `GOOGLE_API_KEY` | 자료 수집, 글 방향 후보 |
| `OPENAI_API_KEY` | 2차 품질 검수, 이미지 생성 |

**mock은 없습니다.** 키가 빠지면 서버가 시작을 거부하고 빠진 키를 알려줍니다. 역할별
provider·모델은 `M2_TOPIC_PROVIDER`·`M4_DRAFT_MODEL`처럼 지정합니다(`.env.example` 참고).

### 트렌드 수집 (선택)

`SERPAPI_API_KEY`(구글) · `NAVER_CLIENT_ID/SECRET`(검색+데이터랩 둘 다 필요) ·
`YOUTUBE_API_KEY` · `FACEBOOK_USER_ACCESS_TOKEN`+`INSTAGRAM_BUSINESS_ACCOUNT_ID`.
서로 독립이라 일부만 설정해도 되고, 하나도 없으면 트렌드 단계 없이 소재만으로 진행합니다.

### 발행 (선택)

`NAVER_BLOG_ID`(관리 CLI용) · `NAVER_BROWSER_PROFILE_DIR` · `NAVER_CHROME_BINARY` ·
`THUMBNAIL_FONT_PATH`(리눅스에서 한글 폰트 지정). 네이버 로그인 정보는 `.env`가 아니라
**설정 화면**에서 저장합니다.

---

## 발행 설정

### 네이버 / 스레드

네이버는 2020년에 글쓰기 API를 종료해 **브라우저 자동화로만 발행할 수 있습니다.** 설정
화면의 **네이버 계정 / 스레드 계정**에 아이디·비밀번호를 저장하면 됩니다 — DB가 아니라
이 PC의 사용자 전용 영역에 암호화 보관됩니다(`.naver-profile-users/` 등, gitignore).

**캡차나 2단계 인증이 뜨면 사람이 처리합니다** — 로그인 중에는 화면에 뜨는 코드 입력창과
라이브 뷰(아래)로 원격에서도 처리할 수 있습니다.

### 여러 사용자의 동시 발행 — 서버 발행 + 라이브 뷰

서버 한 대에서 여러 사람이 발행해도 안전하게 돌도록 설계돼 있습니다
([docs/서버-발행-개선-계획.md](docs/서버-발행-개선-계획.md)):

- **합성 붙여넣기 우선(기본 `auto`)** — 발행은 OS 클립보드 대신 페이지 안 합성 paste로
  먼저 시도합니다(성공하면 발행끼리 완전히 독립). 다만 **현재 스마트에디터 ONE은 합성
  paste를 받지 않아**(2026-08-19 실발행 확인) 그 발행만 클립보드 경로로 자동 전환됩니다.
- **클립보드 보호** — 클립보드 경로에서는 붙여넣기 구간 잠금 + 직전 바이트 대조
  (fail-closed)로 발행끼리 글이 섞이지 않습니다. 같은 순간의 발행은 이 구간에서만
  차례를 기다립니다.
- **라이브 뷰** — 서버에서 뜨는 발행·로그인 크롬 화면을 웹으로 중계하고 클릭·입력을
  전달합니다. 외부 PC에서 로그인·2단계 인증·캡차를 직접 처리합니다.
- 예약 발행은 사용자별 병렬이고, 확인용으로 열어 둔 크롬은 일정 시간 뒤 자동 정리됩니다.

---

## 팀원과 데이터 공유하기

`mongodb://localhost:27017/blog_it`은 각자 자기 PC의 DB입니다. 한 DB를 같이 보려면
[MongoDB Atlas](https://www.mongodb.com/cloud/atlas/register) 무료 티어(M0)를 만들고
두 사람 모두 `.env`에 같은 주소를 넣습니다:

```bash
MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/blog_it
```

- 주소 끝에 `/blog_it`(DB 이름)을 꼭 붙입니다 — Atlas가 복사해 주는 주소에는 없습니다.
- 비밀번호의 `@ / ? #` 같은 문자는 URL 인코딩합니다(`@` → `%40`).
- 연결이 안 되면 `python apps/api/scripts/check_mongo.py`가 원인을 알려줍니다.

---

## 프로젝트 구조

```text
apps/web                    React 프론트엔드 (Vite)
  src/components/write      새 글 작성 (5단계)
  src/components/scheduled  자동 포스팅·작업 관리
  src/api                   API 클라이언트, 타입
apps/api                    FastAPI 백엔드
  app/http                  라우트, 에러 매핑
  app/modules               도메인 서비스 (auth, blog_task, draft, persona,
                            scheduled_posting, trend, user_settings)
  app/llm                   LLM provider 어댑터, 프롬프트, 트렌드 수집기
  app/posting               네이버·스레드 자동 발행 + 라이브 뷰 (Selenium + Chrome)
  app/shared                공용 모델 (Pydantic)
  tests                     pytest
  scripts/                  실행기(dev/serve), DB 초기화, 로그인, 진단
  evals/                    품질 측정 harness (실제 API 호출 — 과금됩니다)
docs/                       설계 배경, 배포 절차, 데이터 저장 위치
변경내역/                    날짜별 작업 기록 (증상·원인·변경·검증)
```

API 명세는 따로 관리하지 않습니다 — 서버 실행 중 `http://localhost:3000/openapi.json`이
현재 코드 그대로의 명세입니다. 더 깊은 내용은 [docs/개발-메모.md](docs/개발-메모.md).

---

## 자주 헷갈리는 부분

- 개발 중에는 **백엔드(3000)와 프론트엔드(5173)를 둘 다** 띄우고 5173으로 접속합니다.
  운영은 빌드 후 백엔드(serve.py) 하나면 됩니다.
- MongoDB가 꺼져 있으면 인메모리로 동작하며 재시작 시 데이터가 사라집니다 — `/health`의
  `storageStatus`로 확인하세요.
- **걸어 둔 예약은 브라우저를 닫아도 계속 돕니다.** 돌리는 것은 백엔드입니다. 백엔드를
  끄면 멈추고, 다시 켜면 남은 작업부터 이어집니다.
- 네이버·스레드 로그인은 처음 한 번입니다. 플랫폼이 세션을 끊으면(비밀번호 변경 등)
  그때만 다시 로그인합니다.
- 자료 검색은 1~2분, 원고 생성은 실측 약 7분입니다. 백그라운드로 돌기 때문에 화면의
  진행 표시가 실제 단계를 따라 움직입니다.
