"""会话标题与删除行为。"""

import asyncio
from types import SimpleNamespace

from sqlalchemy.dialects import mysql

from pagerag.conversations import _title_from_question, delete_conversation


class FakeSession:
    def __init__(self):
        self.statements = []
        self.deleted = []

    async def execute(self, statement):
        self.statements.append(statement)

    async def delete(self, item):
        self.deleted.append(item)

    async def flush(self):
        pass


def test_title_uses_first_question_and_truncates():
    assert _title_from_question(None) == "新对话"
    assert _title_from_question("  营业收入\n是多少？ ") == "营业收入 是多少？"
    assert len(_title_from_question("问" * 100)) == 60


def test_delete_conversation_removes_owner_and_turns_first():
    session = FakeSession()
    conversation = SimpleNamespace(id=42)
    asyncio.run(delete_conversation(session, conversation))
    sql = [str(statement.compile(dialect=mysql.dialect()))
           for statement in session.statements]
    assert any("conversation_owners" in text for text in sql)
    assert any("conversation_turns" in text for text in sql)
    assert session.deleted == [conversation]
