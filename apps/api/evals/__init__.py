"""기준선 평가 harness (Opus 5 전환 회귀 측정용).

`app/`이 아니라 별도 최상위 패키지에 둔다 — 운용 코드가 아니고, 서버는 이 폴더를 import하지
않는다. pytest의 testpaths는 `tests`뿐이라 여기 있는 모듈은 테스트로 수집되지 않는다
(지표 함수의 단위 테스트는 `tests/test_eval_metrics.py`에 따로 있다).

왜 필요한가: 프롬프트·모델을 바꾸면 원고가 달라진다. "좋아졌다"를 말하려면 바꾸기 전 숫자가
있어야 한다. 이 harness는 **품질을 정의하는 점수가 아니라 회귀를 발견하는 보조 지표**를
모은다 — 단일 "사람이 쓴 글 점수"는 만들지 않고, 문제를 항목별로 따로 표시한다.

사용:
    cd apps/api
    python -m evals                        # fixture 모드(실제 API 호출 없음, 무료)
    python -m evals --mode live-titles     # M2 제목 생성·평가만 실제 호출
    python -m evals --mode live-full       # M4 7단계까지 실제 호출(비싸다)
    python -m evals --mode live-full --limit 3
"""
