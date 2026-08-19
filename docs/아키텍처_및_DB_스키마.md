# Blog-it 아키텍처 · DB 스키마

> 기준 코드: 2026-07-24 (main). 이 문서는 **지금 코드가 실제로 하는 일**을 적는다.
> 계획이나 이상적인 설계가 아니라, 파일을 열면 그대로 있는 것만 쓴다.
>
> 목표: 팀원 누구나 **화이트보드에 구조를 그리고 `blogTask`의 핵심 필드를 설명**할 수 있는 것.
> §7에 그 연습 문제를 뒀다. 답이 막히면 해당 절로 돌아가면 된다.

---

## 1. 한 장으로 보는 전체 그림

```
 [브라우저 React+TS]
        |  fetch (Authorization: Bearer, Idempotency-Key)
        v
 +--------------------------------------------------+
 |  FastAPI 프로세스 (apps/api)                       |
 |                                                  |
 |  http/routes.py      Route      인증·검증·상태코드  |
 |        |                                         |
 |        v                                         |
 |  modules/*/service.py  Service   업무 규칙·LLM 호출 |
 |        |                          ·백그라운드 잡    |
 |        v                                         |
 |  modules/*/repository.py Repository  Mongo 문서 R/W |
 +--------------------------------------------------+
        |                    |                  |
        v                    v                  v
   [MongoDB]            [Redis(선택)]      [외부 API]
   blogTask 등 5개        노출이력·멱등성      Gemini/Anthropic/OpenAI
                         ·작업 임차          SerpApi/YouTube
                                            네이버/인스타
                                                 |
                                                 v
                                        [Selenium → 네이버 블로그]
```

**한 문장 요약**: 글 하나 = `blogTask` 문서 하나. Route는 받아서 넘기고, Service가 판단하고,
Repository만 DB를 만진다. 오래 걸리는 일(M3 검증·M4 원고)은 202로 즉시 응답하고 뒤에서 돈다.

---

## 2. Route → Service → Repository

### 2.1 각 층이 하는 일과 **하지 않는 일**

| 층 | 파일 | 하는 일 | 하지 않는 일 |
|---|---|---|---|
| **Route** | `app/http/routes.py` (단일 파일) | 인증, 소유권 확인, 멱등성 키 요구, 본문 파싱, HTTP 상태코드·응답 봉투 | 업무 판단, DB 접근, LLM 호출 |
| **Service** | `app/modules/{blog_task,draft,trend,user_settings,auth,persona}/service.py` | 입력 검증(validation.py), 상태 전이 결정, LLM·이미지·발행 호출, 백그라운드 잡 시작 | HTTP를 앎 (Request/Response 타입이 등장하지 않는다), Mongo 쿼리 |
| **Repository** | 같은 모듈의 `repository.py` | Mongo 문서 읽기·쓰기, 버전 가드, 상태 전이 규칙 강제 | 업무 판단, LLM |

Route는 얇다. 실제로 대부분의 라우트가 세 줄이다:

```python
# routes.py:409-414
@router.post("/posts/{post_id}/intents/select")
async def select_intent(request: Request, post_id: str) -> JSONResponse:
    await _authorize_post(request, post_id)          # 인증 + 내 글인지
    body = await _json_body(request)
    task = await _services(request).blog_task_service.select_intent(post_id, body)
    return envelope(task)
```

### 2.2 조립은 한 곳에서만

`app/services.py`의 `_assemble()`이 리포지토리·서비스·LLM 프로바이더를 엮어
`ApiServices` 하나로 만든다. `main.py`의 lifespan이 시작할 때 한 번 호출하고
`app.state.services`에 얹는다. 라우트는 `_services(request)`로 꺼내 쓴다.

의존성 주입 프레임워크가 없다 — **생성자 인자로만** 넘긴다. 그래서 테스트는
`create_in_memory_services()`로 Mongo·Redis 없이 같은 서비스를 만든다.

```
create_runtime_services()          create_in_memory_services()
   Mongo 연결 성공 → MongoXxxRepository       InMemoryXxxRepository
   Mongo 연결 실패 → InMemoryXxxRepository    (테스트·CI)
```

**Repository는 Protocol이다.** `BlogTaskRepository`(`repository.py:46`)가 인터페이스이고
`MongoBlogTaskRepository` / `InMemoryBlogTaskRepository` 두 구현이 같은 메서드를 갖는다.
Service는 어느 쪽인지 모른다.

### 2.3 인증 경로

```
Authorization: Bearer <token>
   → routes._authenticate()  → auth_service.authenticate()
   → routes._authorize_post() → blog_task_service.get_user_blog_task(user_id, post_id)
                                 없으면 404 (남의 글은 "권한 없음"이 아니라 "없음")
```

예외 하나: `GET /posts/{id}/images/{index}`는 **의도적으로 무인증**이다(`routes.py:327-340`).
이 요청은 네이버 에디터 페이지에서 오는데, 에디터는 우리 액세스 토큰을 모른다 —
Bearer 헤더 뒤에 있는 이미지는 붙여넣을 수 없는 이미지다.
대신 `postId`가 uuid라 URL을 추측할 수 없다는 것이 사실상의 권한이다
(**주소를 아는 사람은 누구나 볼 수 있다** — 이 트레이드오프를 알고 둔 것이다).

---

## 3. DB 스키마

### 3.1 컬렉션 7개

| 컬렉션 | 문서 단위 | 소유(쓰는 코드) | 스키마 등록 |
|---|---|---|---|
| `users` | 사용자 1명 | `modules/auth/repository.py` | `scripts/init_mongo.py` |
| `userSettings` | 사용자 1명 | `modules/user_settings/repository.py` | `scripts/init_mongo.py` |
| `persona` | 프리셋 1개 (`p_1`…) | `modules/persona/repository.py` | `scripts/init_mongo.py` + 시작 시 upsert |
| `blogTask` | **글 1개 = 문서 1개** | `modules/blog_task/repository.py` | `scripts/init_mongo.py` |
| `trend_keywords` | 공용 트렌드 키워드 1개 | `llm/trends/cache.py` | `scripts/init_mongo.py` |
| `material_related_keywords` | 소재별 관련 키워드 1개 | `llm/trends/material_store.py` | `scripts/init_mongo.py` |
| `scheduled_batches` | '예약 시작' 한 번 = 문서 1개 | `modules/scheduled_posting/repository.py` | `scripts/init_mongo.py` |
| `scheduled_jobs` | 예약 소재 1개 = 문서 1개 | `modules/scheduled_posting/repository.py` | `scripts/init_mongo.py` |

> `material_context_profiles`(문맥 프로필 캐시)와 `auth_sessions`는 **더 이상 쓰지 않습니다.**
> `scripts/init_mongo.py`가 실행될 때 남아 있으면 drop합니다. 읽던 코드
> (`llm/trends/reference_context.py`)도 함께 사라졌습니다.

MongoDB인 이유: 사용자 입력·조사 자료 배열·제목 후보·편집 블록·생성 이미지·발행 결과가
**글마다 형태가 조금씩 다른 중첩 데이터**라 조인이 필요 없다.

**정규화 상태 한눈에**: 값이 여러 곳에서 재사용되는 데이터는 전부 참조로 분리돼 있다 —
페르소나 프리셋(사용자 문서는 id만), 공용 트렌드 풀(글과 무관), 소재별 키워드 풀(사용자·
글이 아니라 **소재**가 키라, 같은 소재를 쓰는 모든 글이 한 번 검증한 풀을 재사용), 문맥
프로필(같은 참고자료 조합이면 재사용). 반대로 **한 글에만 속하는 파생물**(검증 결과·원고·
이미지)은 일부러 `blogTask` 한 문서에 중첩한다 — 글의 생애가 문서 하나로 끝나고, 지우면
남는 게 없다. 남은 비정규화 항목은 §3.8에 적었다.

### 3.2 `users`

모델은 `app/shared/user.py`, 저장은 `modules/auth/repository.py`.

| 필드 | 타입 | 설명 |
|---|---|---|
| `userId` | string | 기본 키. 유니크 인덱스 |
| `nickname` | string | 표시 이름. 옛 문서엔 없어 기본값 `""` |
| `emailHash` | string | **블라인드 인덱스** — HMAC(정규화 이메일). 조회·유니크는 이 값으로 한다 |
| `emailEnc` | string | AES-GCM 암호문. 표시·발송 때만 복호화 |
| `passwordHash` | string | scrypt(N=16384/r=8/p=1) + 사용자별 16바이트 salt |
| `createdAt` / `updatedAt` | string(ISO) | |

**평문 `email` 필드는 새로 쓰지 않는다.** 마이그레이션 전 옛 문서는 `email`만 갖고 있어,
조회가 `emailHash` → `email` 순으로 두 번 시도한다(`repository.py:83-91`).
전환 스크립트는 `scripts/migrate_email_encryption.py`(기본 dry-run, `--apply`로 실행).

### 3.3 `userSettings`

모델은 `app/shared/user_settings.py`. 사용자당 정확히 한 문서(`userId` 유니크).

| 필드 | 값 | 설명 |
|---|---|---|
| `userId` | string | |
| `hashtagCount` | 1–10 | 생성할 해시태그 수 |
| `articleLength` | `short`\|`medium`\|`long` | 목표 분량. 프롬프트가 글자수로 옮긴다 |
| `blendMode` | `subject`\|`balanced`\|`trend` | 제목에서 소재와 트렌드 중 무엇이 중심인가. 기본 `trend` |
| `defaultPersona` | `p_1`…`p_9` 또는 `custom` | **ID 참조** |
| `customPersonaName` / `Description` / `customPersona` | string\|없음 | `defaultPersona == "custom"`일 때의 실제 내용 |
| `autoPostingEnabled` | bool | |

페르소나가 **혼합 구조**인 이유: 공용 프리셋 9종은 `persona` 컬렉션에 한 벌만 두고 사용자
문서는 id만 가리킨다(문구를 고쳐도 사용자 문서를 일괄 수정할 필요가 없다). 사용자가 직접
쓴 것만 사용자 문서 안에 산다(시작 시 시드가 사용자 데이터를 덮어쓰지 않는다).

`null`로 도착한 커스텀 필드는 `$set`에서 빠지는 게 아니라 `$unset`으로 **지운다**
(`user_settings/repository.py:51-58`). 안 그러면 지운 값이 되살아난다.

> 없어진 필드: `imageMode`. AI 이미지는 항상 생성하기로 해서 설정 자체를 없앴다.
> 옛 문서에 남은 값은 `CamelModel`의 `extra="ignore"`로 그냥 무시된다.

### 3.4 `blogTask` — **핵심**

모델은 `app/shared/blog_task.py`. 글 하나의 전 생애가 한 문서에 들어 있다.

#### 항상 있는 필드

| 필드 | 설명 |
|---|---|
| `postId` | 기본 키. 유니크 |
| `userId` | 소유자 |
| `status` | 상태 머신의 현재 위치 (§3.5) |
| `version` | **낙관적 동시성 카운터** (§3.6) |
| `createdAt` / `updatedAt` | |
| `statusHistory[]` | `{from, to, at, by}` — 누가 언제 어느 상태로 옮겼는지 |
| `input` | 사용자가 입력한 것 (아래) |
| `postingLogs[]` | 발행 시도 기록 |

`input`의 내용 (`modules/blog_task/validation.py:99-126`):

| 필드 | 필수? | 비고 |
|---|---|---|
| `topic` | **필수** | 소재 |
| `purpose[]` / `keywords[]` | **필수** (비어 있으면 안 됨) | **같은 값이 두 필드에 들어간다.** `keywords`는 `purpose`의 옛 이름이고, 호환을 위해 둘 다 쓴다 |
| `subject`, `tone`, `targetReader`, `readerAgeRange`, `readerKnowledgeLevel` | 선택 | API는 선택으로 받는다(대상 독자는 화면에서 요구) |
| `referenceMaterials[]` | 선택 | `{type: IMAGE\|PDF\|TEXT\|URL, value}` |

> `purpose`와 `keywords`가 같은 값인 것은 이름을 바꾸면서 남긴 호환 흔적이다.
> 새 코드에서는 `purpose`를 읽으면 된다.

#### 단계가 지나면서 붙는 필드 — **이게 blogTask를 이해하는 열쇠다**

| 필드 | 언제 생기나 | 무엇 |
|---|---|---|
| `trendSelection` | 사용자가 트렌드·제목 선택 | `{finalTopic, selectedTrendKeywordIds[], skipped, selectedAt}` |
| `intentValidationResult` | M3(검증) 완료 | 검색으로 모은 자료 + 의도 후보들. `collectedSourceCount`는 **검색이 실제로 찾아 온 총 개수**다 — 후보 하나에 붙는 자료는 상한이 있어 화면에 다 보이지 않으므로, 그 아래 '외 N개'를 적는 데 쓴다(2026-08-07 추가, 옛 문서에는 없어 기본값 0) |
| `selectedIntent` | 사용자가 후보 하나 선택 | `{intentId, title, targetReader, rationale, sources[]}` — **이 sources가 곧 원고의 자료 목록**이다 |
| `draftGenerationResult` | M4(원고) 완료 | `{promptVersion, provider, model, generatedAt, finalPost, contentPlan, visuals[]}` |
| `finalPost` | M4 완료 (위의 복사본) | `{title, body, hashtags[], htmlContent, images[], featuredImage, thumbnailCopy[]}` |
| `progress` | 백그라운드 작업 중에만 | 진행 라벨. 끝나면 지운다 |

**입력을 수정하면 파생 필드가 지워진다.** `replace_input()`이 `trendSelection`,
`intentValidationResult`, `selectedIntent`, `draftGenerationResult`를 `$unset` 한다
(`repository.py:38-43`). 트렌드는 옛 소재로 추천됐고 의도는 그 소재로 검증됐으므로,
남겨 두면 이 글을 설명하지 않는 데이터가 이 글을 설명하는 척한다.

**이미지는 문서 안에 base64 data URL로 인라인**된다(`finalPost.images[].dataUrl`).
GridFS·S3·로컬 파일 없다. 대표 썸네일은 항상 1장, 본문 이미지 수는 콘텐츠 설계가
글마다 정한다(고정값 아님, 폭주 방지 상한만 12장).

#### 상태 머신 (`app/shared/status.py`)

```
INPUT → REFERENCE_PROCESSING → SEARCH_ANALYZING → INTENT_SELECTED → GENERATING
                                                         ^                |
                                            복구 스위퍼가 되감음 ---------+
                                                                         v
                                                              READY_TO_PUBLISH
                                                                         |
                                                                      POSTING
                                                                    /    |    \
                                                              POSTED  NEEDS   FAILED
                                                                      _HUMAN
```

- 전이 규칙은 `ALLOWED_TRANSITIONS` 딕셔너리 하나에 있고, Repository가 강제한다.
- `GENERATING → INTENT_SELECTED` 역방향 간선은 **재시작 복구 전용**이다(§5.4).
- `FAILED`는 종착 상태다. 거기서 빠져나오는 것은 전이가 아니라 `rewind_status()`라는
  별도 메서드다 — 실패한 원고가 죽은 글은 아니기 때문(모델이 타임아웃했을 뿐).

### 3.5 `version`의 의미 — "글 개정 번호"가 아니다

**`version`은 낙관적 동시성(lost update 방지) 카운터다.** 스키마 버전도, 몇 번 고쳤는지도 아니다.

동작은 두 줄이다:

1. 모든 변경 쓰기가 `{"$inc": {"version": 1}}`을 건다.
2. 쓸 때 필터에 **읽었을 때의 버전**을 함께 넣는다:

```python
# repository.py:171-184  _apply()
document = await self._collection.find_one_and_update(
    {"postId": current.post_id, "version": current.version},   # ← 이 조건
    update,
    return_document=ReturnDocument.AFTER,
)
if document is None:
    raise BlogTaskError("INVALID_STATUS_TRANSITION", "... was updated concurrently")
```

그 사이에 다른 작업(예: 백그라운드 원고 생성)이 먼저 버전을 올렸으면 필터가 빗나가
`document is None`이 된다. **조용한 덮어쓰기 대신 충돌로 드러난다.**

예외 하나 — `progress`는 버전 가드를 건너뛴다(`repository.py:248-256`).
백그라운드 잡이 진행률을 쓰면서 **자기 자신의 실제 쓰기와 충돌하는 것**을 막기 위해서다.
진행률은 참고용이라 놓쳐도 라벨만 낡는다.

> 화이트보드에서 설명할 때: "읽고 → 생각하고 → 쓰는 사이에 남이 끼어들면 내 쓰기가 남의
> 결과를 지운다. 그걸 막으려고 읽을 때 본 번호를 쓸 때 조건으로 건다."

### 3.6 `persona` / `trend_keywords`

- `persona`: `_id`가 페르소나 id(`p_1`…), `{name, description, prompt}`.
  카탈로그 단일 정의는 `modules/persona/catalog.py`이고 시작 시 upsert된다.
- `trend_keywords`: **키워드 1개 = 문서 1개** — 최신순 탭이 쓰는 공용 풀이다. 소재와
  무관하며, 같은 시점이면 누구에게나 같다.
  `{_id: "key1", keyword, source, at, score, seq}`.
  `seq`가 따로 있는 이유는 `_id`가 문자열이라 사전순으로 `"key9" > "key10"`이 되어
  다음 순번을 못 뽑기 때문이다(`cache.py:326-347`). 소스당 200개 상한, 오래된 것부터 삭제.
  `score`는 소스 원시 값이 아니라 소스 내 상대 인기(40~100 정규화)다.

### 3.7 `material_related_keywords` / `material_context_profiles` — 소재 단위 재사용

**소재 관련순** 탭의 데이터다. 핵심 설계: 키가 사용자·글이 아니라 **소재(materialKey)** 다.
"배틀그라운드 감도 설정" 같은 키워드는 그 소재의 글에만 의미가 있으므로 공용 풀과 분리하고,
같은 소재로 글을 쓰는 모든 사용자·모든 글이 한 번 수집·채점한 풀을 재사용한다.

- `material_related_keywords`: 키워드 1개 = 문서 1개.
  `{materialKey, contextKey, contextMode, keyword, normalizedKeyword, relationType,
  subjectRelevance, purposeRelevance, personaRelevance, relevance, demandScore,
  source(s), collectedAt, verifiedAt, lastAccessedAt, promptVersion, …}`.
  `contextKey`는 동명이의어 분리용이다 — 같은 소재라도 참고자료가 가리키는 문맥이 다르면
  (게임 vs 배드민턴) 다른 풀이 된다. 유니크는 `(contextKey, normalizedKeyword)`.
  보관은 시간이 아니라 **관련도가 낮은 것부터** 버린다(상한 120, 미사용 소재 풀 180일 정리).
- `material_context_profiles`: (소재, 참고자료 지문)당 판별 결과 하나.
  참고자료 원문·개인 메모는 저장하지 않는다 — 판별 결과(개체·카테고리·허용/제외 주제)와
  근거 요약만. 유니크는 `(materialKey, referenceFingerprint)`.

### 3.8 남은 비정규화 항목 (알고 둔 트레이드오프)

| 항목 | 현재 상태 | 왜 그대로 두나 · 언제 바꾸나 |
|---|---|---|
| **이미지 base64 인라인** | `finalPost.images[].dataUrl`과 `htmlContent` 안에 base64로 중첩. 글 하나가 수 MB | 글 목록의 '복사' 기능이 `htmlContent`의 인라인 이미지를 그대로 쓰므로, 분리하려면 프론트(목록·복사 경로)를 함께 고쳐야 한다. **다음 단계 제안**: `post_images` 컬렉션(또는 GridFS)으로 분리하고 `htmlContent`에는 `GET /posts/{id}/images/{n}` URL만 남기기 — 목록 조회가 문서 전체(이미지 포함)를 읽는 현재 비용이 사라진다. Mongo 문서 상한(16MB)에 닿기 전에 하는 것이 안전하다 |
| `input.purpose` = `input.keywords` | 같은 값이 두 필드에 저장 | `keywords`는 옛 이름. 새 코드는 `purpose`만 읽는다. 호환 기간이 끝나면 `keywords` 제거 |
| `finalPost` = `draftGenerationResult.finalPost` 복사본 | 같은 원고가 두 자리에 | 읽기 경로(화면·발행)가 `finalPost`를 바로 읽는 편의용 복사. 쓰기는 항상 두 곳을 함께 갱신하므로 어긋나지 않는다 |

### 3.9 인덱스 (`scripts/init_mongo.py`)

| 컬렉션 | 인덱스 | 왜 |
|---|---|---|
| `users` | `userId` unique, `emailHash` unique | 이메일 중복 가입 거부는 **애플리케이션이 아니라 인덱스**가 보장한다 |
| `userSettings` | `userId` unique | 사용자당 한 문서 |
| `blogTask` | `postId` unique | |
| | `(userId, createdAt DESC)` | 내 글 목록 |
| | `(userId, postId)` unique | 소유권 확인 조회 |
| | `(status, updatedAt)` | 재시작 복구 스위퍼가 진행 중인 글을 훑는다 |
| `trend_keywords` | `(source, at)` | 소스별 풀 읽기·상한 정리(오래된 것부터) |
| | `(seq DESC)` | 다음 순번 발급(최댓값 하나만 읽는다) |
| `material_related_keywords` | `(contextKey, normalizedKeyword)` unique(partial) | 같은 문맥의 같은 키워드 중복 저장을 인덱스가 막는다 |
| | `(contextKey, referenceContextMatch DESC, subjectRelevance DESC)` | 소재 관련순 화면 조회 경로 |
| | `(materialKey, contextMode)` | 소재+모드(BROAD/문맥 한정) 조회 |
| | `lastAccessedAt` | 미사용 소재 풀 정리(180일) |
| `material_context_profiles` | `(materialKey, referenceFingerprint)` unique | 같은 소재·참고자료 조합은 프로필 하나 |
| | `contextKey`, `lastAccessedAt` | 프로필 되찾기 · 정리 |

`init_mongo.py`는 다시 실행해도 안전하다(검증기는 제자리 갱신, 인덱스는 ensure).

---

## 4. 비동기 구조

### 4.1 왜 비동기인가

M3(검증)은 모델 시간으로 1분쯤, M4(원고+이미지)도 비슷하다. 그동안 HTTP 요청을 붙잡고
있으면 클라이언트가 할 수 있는 건 스피너와 짐작뿐이고, 프록시 타임아웃에도 걸린다.

### 4.2 202 + 폴링

```
POST /posts/{id}/search/analyze   (Idempotency-Key 필수)
   → 상태를 SEARCH_ANALYZING으로 옮기고
   → 백그라운드 잡 시작
   → 202 Accepted + 현재 태스크 즉시 반환

클라이언트: GET /posts/{id} 를 폴링
   → task.progress 로 "지금 몇 단계"를 읽고
   → status 가 SEARCH_ANALYZING 을 벗어나면 완료
```

`POST /posts/{id}/draft/generate`도 같은 모양이다(`routes.py:417-429`).

### 4.3 진행 상황은 서버가 정한다

단계 라벨이 `app/shared/blog_task.py`의 `PHASE_STEPS`에 **서버 쪽에** 있다.

```python
PHASE_STEPS = {
    TaskPhase.SEARCH: ["자료 검색", "검증 후보 정리"],
    TaskPhase.DRAFT:  ["입력값 정리", "본문 원고 구성", "이미지 생성", "결과 표시"],
}
```

예전에는 클라이언트가 고정 목록을 타이머로 애니메이션했다 — 그건 사용자에게 아무 정보도
주지 못한다. 실제로 어느 단계인지 아는 쪽은 서버다.
`ProgressReporter`(`modules/blog_task/jobs.py:32`)가 각 단계 시작마다 `progress`를 쓰고,
로그에는 앞 단계가 몇 초 걸렸는지 함께 남긴다("왜 이렇게 느리지"의 답).

### 4.4 백그라운드 잡을 붙잡아 두는 이유

```python
# jobs.py:92  BackgroundJobs
self._running: set[asyncio.Task] = set()
```

asyncio는 실행 중인 태스크를 **약한 참조로만** 잡는다. 아무도 참조하지 않으면 await 도중
GC될 수 있고, 그러면 생성이 조용히 멈추고 글은 `GENERATING`에 남으며 에러는 어디에도 없다.
그래서 셋에 담아 두고 끝나면 뺀다.

### 4.5 중복 실행을 막는 3중 장치

| 장치 | 어디서 | 무엇을 막나 | 없으면 |
|---|---|---|---|
| **멱등성 키** | Route가 헤더 요구 → Service가 `{단계}:{postId}:{key}`로 스코프 | 재시도·더블클릭 | 같은 요청이 두 번 실행 |
| **작업 임차(lease)** | `modules/blog_task/locks.py` | 두 프로세스가 같은 글을 동시에 | LLM·이미지 호출이 두 벌, 결과 하나는 버려짐 |
| **버전 가드** | Repository `_apply()` | 동시 쓰기의 유실 | 늦게 끝난 쪽이 먼저 끝난 결과를 덮어씀 |

**임차가 락이 아니라 임차인 이유**: 워커가 죽으면 락을 풀 사람이 없다. 만료 시간을 반드시
걸고(기본 120초) 살아 있는 동안 1/3 주기로 갱신한다. 그래서 **"임차가 없다" = "잡고 있던
프로세스가 죽었다"**가 되고, 복구 스위퍼가 그걸 근거로 삼는다.

해제는 소유자 토큰을 확인한 뒤에만, **Lua 스크립트로 한 번에** 한다:

```lua
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
```

확인과 삭제 사이에 틈이 있으면, 만료된 뒤 남이 잡은 임차를 앞선 프로세스가 뒤늦게 풀어
버릴 수 있다. 그 순간부터 두 워커가 같은 작업을 돈다.

---

## 5. 캐시와 공유 상태

### 5.1 세 종류를 구분해야 한다

| | 무엇 | 어디 | 없으면 |
|---|---|---|---|
| **영속 데이터** | `blogTask` 등 | MongoDB | 글이 사라진다 |
| **누적 풀** | 수집한 트렌드 키워드 | MongoDB `trend_keywords` | 매번 다시 수집(과금) |
| **공유 상태** | 노출 이력·멱등성 키·작업 임차 | Redis (선택) | 재시작하면 초기화, 서버 여러 대면 서로 모름 |

### 5.2 트렌드 캐시 — 폴백 사슬

`llm/trends/cache.py`. 모두 `PoolCache` Protocol의 구현이라 갈아 끼울 수 있다.

```
MongoPoolCache      ← 추천어 공용 풀(키워드 1개 = 문서 1개). 만료 없음, 누적
    └ 그 외 키(소재 관련어·관련도 점수)는 DiskPoolCache 로
RedisPoolCache      ← 설정돼 있으면. 실패하면 DiskPoolCache 로 저하
DiskPoolCache       ← 저장소 루트 .trend-cache/ (gitignore). Redis 없는 기본값
InMemoryPoolCache   ← 테스트
```

핵심 수치:
- `POOL_TTL_SECONDS = 30일` — 이 안이면 소스 API를 부르지 않고 저장된 풀에서 서빙한다.
  자동 재수집은 한 달에 한 번. 더 최신이 필요하면 사용자가 '수집하기'로 즉시 부른다.
- `POOL_KEEP_SECONDS = 60일` — 신선하지 않아도 보관하는 안전망. SerpApi 할당량이 끊기거나
  네이버가 401을 줘도 패널이 비지 않고 마지막 수집분이 나온다.
- `POOL_PER_SOURCE = 20` — 1회 수집 때 소스당 상한. 누적되므로 한 번에 많이 가져올 필요가 없다.

**항목이 저장소 만료가 아니라 자기 타임스탬프를 지니는 이유**: 저장소는
"믿기엔 너무 오래됨"과 "보관하기엔 너무 오래됨"을 구분하지 못한다.

### 5.3 Redis 공유 상태 (P2-03)

`REDIS_URL`이 설정돼 있을 때만 쓴다. 없으면 전부 메모리 구현으로 돌아가고, 예전과 같이 동작한다.

| 키 | 자료구조 | TTL | 코드 |
|---|---|---|---|
| `blogit:trend:exposure:*` | Sorted Set (score=시각) | `TREND_EXPOSURE_TTL_SECONDS` (기본 24h) | `llm/trends/exposure.py` |
| `blogit:idempotency:*` | String, **SET NX** | `IDEMPOTENCY_TTL_SECONDS` (기본 24h) | `modules/blog_task/idempotency.py` |
| `blogit:job:lease:{postId}:{m3\|m4}` | String, SET NX EX | 120초 + 하트비트 갱신 | `modules/blog_task/locks.py` |

TTL·개수 상한은 전부 환경변수다(`app/config.py`). 잘못된 값이면 경고를 남기고 기본값으로
계속 간다 — TTL 오타 하나로 서버가 못 뜨면 안 된다.

**Redis가 죽었을 때의 방향이 저장소마다 반대다. 의도된 것이다.**

| 저장소 | 실패 시 | 왜 |
|---|---|---|
| 노출 이력 | 이력이 **비었다**고 본다 | 같은 키워드를 다시 보여주는 편이, 패널이 비는 것보다 낫다 |
| 멱등성 키 | 메모리 폴백 | 중복 방지 범위가 그 프로세스로 좁아질 뿐, 글 작성은 계속된다 |
| 작업 임차 | **잡은 것으로 친다** | 여기서 막으면 Redis가 흔들릴 때 원고 생성이 통째로 멈춘다 |
| 임차 상태 확인 | **살아 있다**고 본다 | 멀쩡히 도는 작업을 실패로 만드는 것보다, 죽은 작업이 한 번 더 남는 게 안전 |

지금 어느 쪽으로 돌고 있는지는 **`GET /health`의 `sharedStateStatus`**로 확인한다
(`"Redis"` / `"메모리(REDIS_URL 미설정)"` / `"메모리(Redis 연결 실패)"`).
시작 로그에도 `공유 상태: …` 한 줄로 찍는다. 접속 정보·비밀번호는 절대 남기지 않는다.

### 5.4 재시작 복구

배포·크래시로 프로세스가 사라지면 작업은 죽는데 글의 상태는 `SEARCH_ANALYZING` /
`GENERATING`에 남는다. 화면은 그걸 "지금 돌고 있음"으로 읽어 스피너가 영원히 돌고,
버튼은 이미 진행 중이라며 막혀 있어 사용자가 할 수 있는 일이 없다.

`modules/blog_task/recovery.py`가 시작할 때 한 번 훑는다:

```
(status, updatedAt) 인덱스로 SEARCH_ANALYZING·GENERATING 인 글을 찾는다
   → 그 글의 임차가 살아 있으면?  건드리지 않는다 (다른 서버가 지금 돌리는 중)
   → 임차가 없으면 (= 잡고 있던 프로세스가 죽었다)
        GENERATING       → INTENT_SELECTED 로 되감기 (사용자가 다시 누를 수 있는 자리)
        SEARCH_ANALYZING → 상태 유지 + '검증 실패' 결과 기록 (팝업에 '다시 검증'이 뜬다)
   → 둘 다 progress 를 지운다
```

**자동으로 재실행하지 않는 것은 의도적이다.** 재시작마다 원고·이미지 생성이 저절로 돌면
사용자 의사와 무관하게 과금된다.

---

## 6. 아직 프로세스 안에만 있는 것

정직하게 적어 둔다. API 서버를 여러 대로 늘리면 아래는 서버마다 따로 논다.

| 상태 | 위치 | 여러 대일 때 |
|---|---|---|
| `BackgroundJobs._running` | 프로세스 메모리 | 정상 — 각자 자기가 시작한 잡만 붙잡는다 |
| `AggregateTrendProvider._refreshing`, `_material_inflight` | 프로세스 메모리 | 백그라운드 재수집이 서버마다 한 번씩 돌 수 있다. 결과는 같은 DB로 합쳐지므로 **데이터는 틀리지 않고 API 호출만 더 나간다** |
| M3 수집 캐시 (`GeminiResearchAnalyzer._research_cache`, TTL 10분) | 프로세스 메모리 | 재검증 가속용. 서버가 다르면 그냥 다시 수집한다 — 데이터는 틀리지 않는다 |
| 콘텐츠 설계 캐시 (`DraftService._plan_cache`, TTL 30분) | 프로세스 메모리 | 의도 선택 직후 선행 생성이 채우고 원고 생성이 재사용. 서버가 다르면 설계를 다시 만든다. 캐시가 적중하면 저장된 같은 설계를 돌려주고, 없으면 다시 만들어 세부가 달라질 수 있다. 키에 모델·effort·thinking·프롬프트 버전이 들어가므로 그중 하나가 바뀌면 옛 설계를 재사용하지 않는다 |
| provider 공유 HTTP 클라이언트 (`llm/http.py`) | 프로세스 메모리 | keep-alive 풀. 서버마다 자기 풀을 갖는 것이 정상 |
| 별도 Worker 프로세스 / 영속 큐 | **없음** | 잡은 API 프로세스 안 asyncio 태스크다. 위 복구 스위퍼가 그 대가를 메운다 |

네이버 발행은 Selenium으로 **사용자 PC의 실제 Chrome**을 구동한다(로컬 PC = 서버가 같은 머신).
FastAPI 프로세스 안 워커 스레드에서 돈다.

---

## 7. 화이트보드 연습 문제

이 답이 나오면 P2-05 완료 기준을 만족한다.

1. **박스 5개로 요청 흐름을 그려라.** (브라우저 → Route → Service → Repository → Mongo)
   각 박스가 하지 **않는** 일을 하나씩 말하라. → §2.1
2. **`blogTask`에 항상 있는 필드 8개를 적어라.** → §3.4
3. **`selectedIntent`는 언제 생기고, 그 안의 `sources[]`는 무엇에 쓰이나?** → §3.4
4. **`version`은 무엇인가? "글을 세 번 고쳐서 3"이 맞나?** → §3.5 (아니다. 동시 쓰기 유실 방지)
5. **왜 `progress`만 버전을 올리지 않나?** → §3.5
6. **입력을 수정하면 왜 트렌드 선택이 지워지나?** → §3.4
7. **원고 생성 요청의 응답 코드는? 클라이언트는 완료를 어떻게 아나?** → §4.2
8. **중복 실행을 막는 장치 3개와 각각이 막는 상황.** → §4.5
9. **왜 락이 아니라 임차인가?** → §4.5
10. **Redis가 죽으면 노출 이력과 작업 임차는 각각 어느 쪽으로 기우나? 왜 반대인가?** → §5.3
11. **서버가 재시작하면 `GENERATING`이던 글은 어떻게 되나? 왜 자동 재실행하지 않나?** → §5.4

---

## 관련 문서

- `Blog-it_구조설명_리뷰대비.md` — 트렌드 추천 점수 공식·수집 소스별 역할·보안 요약
- `변경내역/` — 날짜별 변경 기록
- `README.md` — 실행 방법과 환경변수
