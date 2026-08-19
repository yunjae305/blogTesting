# 기준선 평가 harness

Opus 5 전환 전후의 **회귀를 발견하기 위한** 측정 도구다. 품질을 정의하는 점수가 아니다 —
단일 "사람이 쓴 글 점수"는 만들지 않고, 문제를 항목별로 따로 보고한다. 외부 AI 탐지 서비스는
쓰지 않는다.

`app/`이 아니라 최상위 별도 패키지다. 서버는 이 폴더를 import하지 않고, pytest의 testpaths도
`tests`뿐이라 여기 모듈은 테스트로 수집되지 않는다.

## 실행

```bash
cd apps/api
python -m evals                       # fixture 모드(기본). 실제 API를 부르지 않는다
python -m evals --mode live-titles    # M2 제목 생성+평가만 실제 호출
python -m evals --mode live-regen     # 제목을 두 번 뽑아 재생성 다양성까지
python -m evals --mode live-full      # M4 7단계까지 실제 호출(비싸고 느리다)

python -m evals --mode live-full --cases "ev-battery__review__p_5,extra__control-no-conflict"
python -m evals --mode live-titles --tags conflict
python -m evals --mode live-full --limit 3 --model claude-opus-5
```

`--out`은 최종 JSON 경로다(기본 `evals/baseline-<mode>.json`). **사례가 하나 끝날 때마다 같은
이름의 `.jsonl`에 append**하므로 중간에 끊겨도 거기까지는 남는다 — 한 호출이 매달려 1시간 반을
버리고 아무것도 남기지 못한 일이 있어서 넣었다. 한 사례는 `CASE_TIMEOUT_SECONDS`(600초)를
넘기면 그 사례만 오류로 기록하고 다음으로 간다.

`ANTHROPIC_API_KEY`는 서버와 같은 방식으로 `.env`에서 읽는다.

## 저장된 측정값

전환 전후를 **같은 사례·같은 지표**로 잰 결과가 폴더에 함께 있다. 모델은 `.env`가 가리키는
것을 그대로 쓰므로, 파일 안의 `"model"` 값이 그 측정이 어느 모델이었는지를 말한다.

| 파일 | 모델 | 범위 |
|---|---|---|
| `baseline-live-titles.json` | claude-opus-4-6 | 제목 31사례 |
| `after-live-titles.json` | claude-opus-5 | 제목 31사례 |
| `baseline-live-full.json` | claude-opus-4-6 | 원고 6사례 |
| `after-live-full.json` | claude-opus-5 | 원고 6사례 |

`baseline-fixture.json`은 비교 대상이 아니다(fixture 본문에 결함을 심어 뒀다).

전후 비교에서 **모델 교체 효과와 프롬프트 변경 효과는 분리되지 않는다.** 둘을 함께 바꾼 뒤
한 번 쟀기 때문이다. 분리하려면 같은 프롬프트로 `--model`을 바꿔 가며 두 번 돌려야 한다.

## 모드가 뜻하는 것

| 모드 | 실제 호출 | 무엇을 알 수 있나 |
|---|---|---|
| `fixture` | 없음 | 배선(프롬프트 조립 → 파싱 → 지표)이 도는지, 지표가 결함을 잡는지. **여기 숫자는 기준선이 아니다** — fixture 본문에 결함을 일부러 심어 뒀다 |
| `live-titles` | 사례당 2회 | 제목 품질 기준선 |
| `live-regen` | 사례당 4회 | + '제목 추천 다시'가 관점을 정말 바꾸는지 |
| `live-full` | 사례당 8회 | + 원고·시각자료 계획 기준선 |

## 평가 입력 (`cases.py`)

27조합 격자 = 소재 3 × 목적 3 × 페르소나 3, 여기에 격자로 담을 수 없는 4사례를 더해 **31개**.

- 소재: 일반 생활(참고자료 없음) · 비교·분석(자료 없이 비교 요구) · 전문(URL + 실측 수치 메모)
- 목적: 문제 해결 · 비교·추천 · 후기·리뷰 작성
- 페르소나: 일상 기록 블로거 · 실무 코치 · 브랜드 스토리텔러

목적·페르소나를 이렇게 고른 이유는 그 격자 안에 목적↔페르소나 충돌 세 쌍이 모두 들어오기
때문이다(소재 3개마다 생기므로 충돌 셀은 9개).

| 충돌 쌍 | 왜 충돌인가 |
|---|---|
| 일상 기록 블로거 × 문제 해결 | 시간순 일기 습관 vs 문제→해결 구조 |
| 브랜드 스토리텔러 × 비교·추천 | 브랜드 서사 vs 기준별 비교표 |
| 실무 코치 × 후기·리뷰 | 체크리스트 습관 vs 경험 서술 |

추가 4사례:

| case_id | 무엇을 재나 |
|---|---|
| `extra__custom-persona-lookalike` | 커스텀 이름 `"실무 코치처럼 쓰는 사람"`이 프리셋으로 오인되는지 |
| `extra__injection-probe` | 커스텀 페르소나 안의 명령문이 상위 규칙(해시태그 수)을 덮는지 |
| `extra__locked-trend-title` | 트렌드 제목이 고정된 글(원고가 제목을 짓지 않는다) |
| `extra__control-no-conflict` | 충돌 없는 대조군 |

## 지표 (`metrics.py`)

전부 순수 함수이고 `tests/test_eval_metrics.py`가 검증한다. 검증되지 않은 자로 재면 그 숫자로는
아무것도 주장할 수 없다.

기존 검사를 다시 구현하지 않고 그대로 쓴다: `check_draft`, `body_char_count`,
`_repeated_ngram_rate`, `_cliche_hits`, `run_content_validations`. 새로 계산하는 것은 **지금
코드에 없는 지표**뿐이다.

| 새 지표 | 왜 필요한가 |
|---|---|
| `intro_cliche_hits` | `_cliche_hits`는 위치를 보지 않는다. 도입부 상투구만 따로 센다 |
| `connective_counts` | 같은 연결어 반복. **문장을 그 낱말로 시작한 횟수**만 센다(`quality.connective_openings`) — 본문 전체에서 낱말을 세면 '바닥재를 먼저 확인한다' 같은 정상 문장이 목차형 연결어로 잡힌다 |
| `title_first_paragraph_overlap` | 제목을 첫 문장에서 되풀이하는지 |
| `uniform_h2_grammar` | 소제목은 개수만 검사된다. 어미 패턴이 전부 같은지 본다 |
| `conclusion_body_overlap` | 결론이 소제목을 다시 늘어놓는지 |
| `seo_primary_per_1000_chars` / `seo_avoid_violations` | SEO 검사는 '존재 여부'만 본다. 과밀과 avoid 위반은 검사가 없다 |
| `unsupported_numeric_claims` | 본문 수치를 자료와 대조하는 검사가 없다. `3가지` 같은 구조 표기는 제외한다 |
| `max_same_sentence_ending_run` | 같은 종결어미 연속 |
| `paragraph_length_cv` / `uniform_paragraph_length` | 문단 길이 균일성 |

## provider 호출 기록

모든 모드에서 `live_adapters._post_json`을 실행 중에만 감싸(끝나면 되돌린다) 단계별로 기록한다:
모델 · `max_tokens` · **`stop_reason`** · 입출력 토큰 · 지연 · 잘림 여부.

`stop_reason`을 여기서 재는 이유: 운용 코드에는 `stop_reason`을 읽는 곳이 한 곳도 없어서
`max_tokens`로 잘린 응답이 부분 dict로 파서에 넘어가고, `final_post_from_json`은 예외를 던지지
않고 폴백한다. 즉 잘림은 오류가 아니라 **조용한 품질 저하**로 나가고, 이 harness가 유일한
관측 수단이다.

## 알려진 한계

- `live-full`은 품질검사 실패 시의 **재시도 루프를 돌지 않는다.** 그래서 `quality_ok`는
  "1차 시도 품질"이고, 운용에서는 실패 사례에 재시도가 한 번 더 붙는다(`DRAFT_ATTEMPTS=2`).
  `quality_ok=False`인 비율이 곧 "재시도가 걸리는 비율"이다.
- 호출 순서는 `draft/service.py`의 순서를 이 파일이 따라 적은 것이다. 서비스가 순서를 바꾸면
  여기도 함께 바꿔야 한다(DB·큐를 쓰지 않으려고 감수한 중복이다).
- 이미지 **생성**은 부르지 않는다. 2-4가 요구한 것은 시각자료 **계획**이다.
