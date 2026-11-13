"""最小账号与会话：注册、登录、Cookie 会话和资料归属判断。

只解决演示需要的多用户隔离，不引入角色、组织或第三方登录。已有集合没有
归属记录时按共享资料处理，新建集合和新建对话会记录创建者。
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pagerag.models import (
    Collection,
    CollectionOwner,
    Conversation,
    ConversationOwner,
    User,
    UserSession,
)

SESSION_COOKIE = "pagerag_session"
SESSION_DAYS = 7
_USERNAME_MIN = 3
_USERNAME_MAX = 40
_PASSWORD_MIN = 8


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt.encode("ascii"),
        n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024)
    return f"scrypt${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt, digest_hex = encoded.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt.encode("ascii"),
        n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024)
    return secrets.compare_digest(digest.hex(), digest_hex)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_user(session: AsyncSession, username: str,
                      password: str) -> User:
    name = str(username or "").strip()
    if len(name) < _USERNAME_MIN or len(name) > _USERNAME_MAX:
        raise ValueError(f"用户名长度需要在 {_USERNAME_MIN} 到 {_USERNAME_MAX} 个字符之间")
    if len(str(password or "")) < _PASSWORD_MIN:
        raise ValueError(f"密码至少需要 {_PASSWORD_MIN} 个字符")
    existing = (await session.execute(
        select(User).where(User.username == name)
    )).scalar_one_or_none()
    if existing is not None:
        raise ValueError("用户名已存在")
    user = User(public_id=secrets.token_hex(16), username=name,
                password_hash=hash_password(password))
    session.add(user)
    await session.flush()
    return user


async def authenticate(session: AsyncSession, username: str,
                       password: str) -> User | None:
    user = (await session.execute(
        select(User).where(User.username == str(username or "").strip())
    )).scalar_one_or_none()
    if user is None or not verify_password(str(password or ""), user.password_hash):
        return None
    return user


async def issue_session(session: AsyncSession, user: User) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    session.add(UserSession(
        token_hash=_token_hash(token), user_id=user.id,
        created_at=now, expires_at=now + timedelta(days=SESSION_DAYS)))
    await session.flush()
    return token


async def resolve_session(session: AsyncSession, token: str) -> User | None:
    if not token:
        return None
    row = (await session.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == _token_hash(token))
    )).first()
    if row is None:
        return None
    expires = row.UserSession.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= datetime.now(UTC):
        await session.delete(row.UserSession)
        await session.commit()
        return None
    return row.User


async def revoke_session(session: AsyncSession, token: str) -> None:
    if not token:
        return
    row = (await session.execute(
        select(UserSession).where(UserSession.token_hash == _token_hash(token))
    )).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        await session.flush()


async def set_collection_owner(session: AsyncSession, collection: Collection,
                               user: User | None) -> None:
    if user is None:
        return
    existing = await session.get(CollectionOwner, collection.id)
    if existing is None:
        session.add(CollectionOwner(collection_id=collection.id, user_id=user.id))
        await session.flush()


async def set_conversation_owner(session: AsyncSession, conversation: Conversation,
                                 user: User | None) -> None:
    if user is None:
        return
    existing = await session.get(ConversationOwner, conversation.id)
    if existing is None:
        session.add(ConversationOwner(conversation_id=conversation.id,
                                      user_id=user.id))
        await session.flush()


async def can_access_collection(session: AsyncSession, collection: Collection,
                                user_id: int | None) -> bool:
    if user_id is None:
        return True
    owner = await session.get(CollectionOwner, collection.id)
    return owner is None or owner.user_id == user_id


async def can_access_conversation(session: AsyncSession,
                                  conversation: Conversation,
                                  user_id: int | None) -> bool:
    if user_id is None:
        return True
    owner = await session.get(ConversationOwner, conversation.id)
    if owner is not None:
        return owner.user_id == user_id
    collection = await session.get(Collection, conversation.collection_id)
    return collection is not None and await can_access_collection(
        session, collection, user_id)


def visible_collections_statement(user_id: int):
    """用户可见的集合：自己拥有的集合，加上没有归属记录的共享资料。"""
    owned = select(CollectionOwner.collection_id).where(
        CollectionOwner.user_id == user_id)
    recorded = select(CollectionOwner.collection_id)
    return select(Collection).where(
        (Collection.id.in_(owned)) | (~Collection.id.in_(recorded)))
