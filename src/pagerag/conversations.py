"""服务端会话：创建、列出、搜索、改名、删除与轮次关联。

会话只保存标题和顺序，问题与回答仍以 QueryRun 为准；删除会话不删除查询
记录。标题取第一次提问的前若干字符，用户可随后改名。
"""

import secrets

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pagerag.models import (
    Collection,
    Conversation,
    ConversationOwner,
    ConversationTurn,
    QueryRun,
    utcnow,
)

TITLE_MAX_CHARS = 60
# 追问改写只需要最近几轮；读取上限与 answering.HISTORY_MAX_TURNS 对齐。
HISTORY_TURNS = 6


def _title_from_question(question: str | None) -> str:
    text = " ".join(str(question or "").split())
    return text[:TITLE_MAX_CHARS] or "新对话"


async def create_conversation(session: AsyncSession, collection: Collection,
                              question: str | None = None) -> Conversation:
    conversation = Conversation(
        public_id=secrets.token_hex(16),
        collection_id=collection.id,
        title=_title_from_question(question),
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def get_conversation(session: AsyncSession,
                           public_id: str) -> Conversation | None:
    return (await session.execute(
        select(Conversation).where(Conversation.public_id == public_id)
    )).scalar_one_or_none()


async def list_conversations(
    session: AsyncSession, collection_id: int, query: str = "", limit: int = 40,
    user_id: int | None = None,
) -> list[dict]:
    stmt = select(Conversation).where(Conversation.collection_id == collection_id)
    if user_id is not None:
        owned = select(ConversationOwner.conversation_id).where(
            ConversationOwner.user_id == user_id)
        recorded = select(ConversationOwner.conversation_id)
        stmt = stmt.where(or_(Conversation.id.in_(owned),
                              ~Conversation.id.in_(recorded)))
    text = query.strip()
    if text:
        pattern = f"%{text}%"
        matching_runs = (select(ConversationTurn.conversation_id)
                         .join(QueryRun, QueryRun.id == ConversationTurn.query_run_id)
                         .where(QueryRun.question.like(pattern)))
        stmt = stmt.where(or_(Conversation.title.like(pattern),
                              Conversation.id.in_(matching_runs)))
    conversations = (await session.execute(
        stmt.order_by(Conversation.updated_at.desc()).limit(max(1, min(limit, 100)))
    )).scalars().all()
    if not conversations:
        return []
    ids = [item.id for item in conversations]
    rows = (await session.execute(
        select(ConversationTurn.conversation_id, ConversationTurn.position,
               QueryRun.question)
        .join(QueryRun, QueryRun.id == ConversationTurn.query_run_id)
        .where(ConversationTurn.conversation_id.in_(ids))
        .order_by(ConversationTurn.conversation_id, ConversationTurn.position)
    )).all()
    counts: dict[int, int] = {}
    last_question: dict[int, str] = {}
    for conversation_id, _position, question in rows:
        counts[conversation_id] = counts.get(conversation_id, 0) + 1
        last_question[conversation_id] = question or ""
    return [
        {
            "conversation_id": item.public_id,
            "title": item.title,
            "turn_count": counts.get(item.id, 0),
            "last_question": last_question.get(item.id, ""),
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        for item in conversations
    ]


async def recent_history(session: AsyncSession, conversation_id: int,
                         limit: int = HISTORY_TURNS) -> list[dict]:
    rows = (await session.execute(
        select(QueryRun.question, QueryRun.answer)
        .join(ConversationTurn, ConversationTurn.query_run_id == QueryRun.id)
        .where(ConversationTurn.conversation_id == conversation_id)
        .order_by(ConversationTurn.position.desc())
        .limit(max(1, limit))
    )).all()
    return [
        {"question": row.question or "", "answer": row.answer or ""}
        for row in reversed(rows)
    ]


async def append_turn(session: AsyncSession, conversation: Conversation,
                      run: QueryRun) -> ConversationTurn:
    next_position = (await session.execute(
        select(func.coalesce(func.max(ConversationTurn.position), 0))
        .where(ConversationTurn.conversation_id == conversation.id)
    )).scalar_one() + 1
    turn = ConversationTurn(conversation_id=conversation.id,
                            query_run_id=run.id, position=next_position)
    session.add(turn)
    if conversation.title in ("", "新对话"):
        conversation.title = _title_from_question(run.question)
    conversation.updated_at = utcnow()
    await session.flush()
    return turn


async def rename_conversation(session: AsyncSession, conversation: Conversation,
                              title: str) -> Conversation:
    text = " ".join(str(title or "").split())[:TITLE_MAX_CHARS]
    if not text:
        raise ValueError("会话标题不能为空")
    conversation.title = text
    conversation.updated_at = utcnow()
    await session.flush()
    return conversation


async def delete_conversation(session: AsyncSession,
                              conversation: Conversation) -> None:
    # 归属行先删，避免外键挡住会话本身的删除；查询记录仍保留。
    await session.execute(
        delete(ConversationOwner).where(
            ConversationOwner.conversation_id == conversation.id)
    )
    await session.execute(
        delete(ConversationTurn).where(
            ConversationTurn.conversation_id == conversation.id)
    )
    await session.delete(conversation)
    await session.flush()
