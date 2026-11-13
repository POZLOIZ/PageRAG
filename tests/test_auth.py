"""账号密码派生与验证。"""

from pagerag.auth import hash_password, verify_password


def test_password_hash_roundtrip():
    encoded = hash_password("page-rag-2026")
    assert encoded.startswith("scrypt$")
    assert "page-rag-2026" not in encoded
    assert verify_password("page-rag-2026", encoded)
    assert not verify_password("wrong-password", encoded)


def test_password_hash_uses_fresh_salt():
    first = hash_password("same-password")
    second = hash_password("same-password")
    assert first != second
    assert verify_password("same-password", first)
    assert verify_password("same-password", second)
