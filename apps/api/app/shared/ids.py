"""읽을 수 있는 식별자.

로그에 계속 흐르는 것이 `post_e2f6c5fd-82bf-4ddb-91ad-eea1df2802e8` 이면 사람은 그것을
읽지 않는다. 언제 만들어진 글인지 앞부분만 보고 알 수 있어야 한다.

    post_20260715_133012_384_a3f1c9d2b4e6
         └날짜┘ └시각┘ └ms┘ └무작위 48비트┘

시각만으로는 동시 생성 충돌과 로그 추측이 쉬우므로 뒤에 무작위 48비트를 붙인다. 네이버가
가져가는 `/posts/{postId}/images/{n}`은 별도로 10분 만료 HMAC 서명을 검증한다. 즉 무작위
부분은 식별자 고유성, 서명은 이미지 접근 권한을 맡는다.
"""

import secrets
from datetime import datetime, timezone

# 48비트. 충돌 방지와 로그에서의 식별에 충분하고, 이미지 접근은 별도 서명이 지킨다.
RANDOM_HEX_CHARS = 12


def _stamp() -> str:
    """밀리초까지. 같은 초에 두 글이 만들어져도 순서가 남는다."""
    now = datetime.now(timezone.utc)
    return f"{now:%Y%m%d_%H%M%S}_{now.microsecond // 1000:03d}"


def new_post_id() -> str:
    return f"post_{_stamp()}_{secrets.token_hex(RANDOM_HEX_CHARS // 2)}"


def new_user_id() -> str:
    return f"user_{_stamp()}_{secrets.token_hex(RANDOM_HEX_CHARS // 2)}"


def new_log_id() -> str:
    return f"log_{_stamp()}_{secrets.token_hex(RANDOM_HEX_CHARS // 2)}"


def new_batch_id() -> str:
    return f"batch_{_stamp()}_{secrets.token_hex(RANDOM_HEX_CHARS // 2)}"


def new_job_id() -> str:
    return f"job_{_stamp()}_{secrets.token_hex(RANDOM_HEX_CHARS // 2)}"


def new_series_id() -> str:
    """**한 번에 건 묶음**의 id(2026-08-13). 화면의 '1편째·2편째'가 이 묶음 안에서 센다."""
    return f"series_{_stamp()}_{secrets.token_hex(RANDOM_HEX_CHARS // 2)}"


def short(identifier: str) -> str:
    """로그용 짧은 이름. `post_20260715_133012_a3f1…` 처럼 줄인다."""
    return identifier if len(identifier) <= 26 else f"{identifier[:26]}…"


def new_brand_id() -> str:
    return f"brand_{_stamp()}_{secrets.token_hex(RANDOM_HEX_CHARS // 2)}"
