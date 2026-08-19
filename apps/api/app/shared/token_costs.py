"""모델별 토큰 단가와 호출 비용 계산.

터미널에 "이 호출이 토큰을 얼마나 쓰고 얼마였는지"를 보여 주기 위한 표다. 단가는 각
공급자 공시가(1M 토큰당 USD)이고 2026-08-11에 확인했다:

    claude-opus-5        입력 $5.00  / 출력 $25.00   (Anthropic 공시가)
    gemini-3.6-flash     입력 $1.50  / 출력 $7.50
    gemini-3.5-flash-lite 입력 $0.30 / 출력 $2.50
    gpt-5.6-sol          입력 $5.00  / 출력 $30.00

단가는 공급자가 바꿀 수 있으므로, 여기 값은 "청구서"가 아니라 **추정치**다. 표에 없는
모델(예: gpt-image-2 — 토큰이 아니라 장당 과금, gemini-2.5-flash — 폴백 체인에만 있고
쓰지 않기로 함)은 토큰량만 표시되고 비용은 "단가 미등록"으로 나온다 — 0원으로 속이는
것보다 모른다고 말하는 쪽이 맞다.
"""

from __future__ import annotations

_MTOK = 1_000_000

# 모델 -> (입력 단가, 출력 단가). 단위: USD / 1M 토큰.
MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gpt-5.6-sol": (5.00, 30.00),
}


def price_for(model: str) -> tuple[float, float] | None:
    """모델의 (입력, 출력) 단가. 없으면 None.

    날짜 고정 snapshot(`claude-opus-5-20260101` 같은 형식)은 기본 모델과 같은 단가로
    본다 — 공급자들이 snapshot에 별도 단가를 매기지 않는다.
    """
    normalized = (model or "").strip().lower()
    if normalized in MODEL_PRICES_USD_PER_MTOK:
        return MODEL_PRICES_USD_PER_MTOK[normalized]
    for name, price in MODEL_PRICES_USD_PER_MTOK.items():
        if normalized.startswith(name + "-"):
            return price
    return None


def estimate_cost_usd(
    model: str, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """한 호출의 추정 비용(USD). 단가 미등록 모델이거나 토큰 정보가 전혀 없으면 None."""
    price = price_for(model)
    if price is None:
        return None
    if input_tokens is None and output_tokens is None:
        return None
    input_price, output_price = price
    return ((input_tokens or 0) * input_price + (output_tokens or 0) * output_price) / _MTOK
