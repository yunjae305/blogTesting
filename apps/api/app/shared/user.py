"""사용자 모델."""

from .base import CamelModel


class User(CamelModel):
    user_id: str
    email: str
    # 회원가입 때 정하는 표시 이름. 옛 문서에는 없으므로 기본값을 둔다(하위 호환).
    nickname: str = ""
    password_hash: str
    created_at: str
    updated_at: str


class PublicUser(CamelModel):
    user_id: str
    email: str
    nickname: str = ""
    created_at: str
    updated_at: str


class AuthSession(CamelModel):
    user: PublicUser
    access_token: str
    issued_at: str
    expires_at: str


def to_public_user(user: User) -> PublicUser:
    return PublicUser(
        user_id=user.user_id,
        email=user.email,
        nickname=user.nickname,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )
