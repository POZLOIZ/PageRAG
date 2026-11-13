"""上传入口的边界校验：坏编码、空文件、超大文件和不支持的扩展名。"""

import base64
from pathlib import Path

import pytest
from fastapi import HTTPException

from pagerag.app import _decode_upload
from pagerag.settings import Settings


def make_settings(**overrides) -> Settings:
    values = {
        "data_dir": Path("/tmp/pagerag-upload-test"),
        "max_upload_bytes": 8,
        "rerank_url": "http://127.0.0.1:18080/rerank",
        "rerank_model": "Qwen/Qwen3-Reranker-0.6B",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values,
                    database_url="", llm_base_url="", llm_model="")


def encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_valid_markdown_passes_through():
    assert _decode_upload(encode(b"# title"), "note.md", make_settings()) == b"# title"


def test_invalid_base64_is_400():
    with pytest.raises(HTTPException) as caught:
        _decode_upload("这不是 base64", "note.md", make_settings())
    assert caught.value.status_code == 400


def test_empty_content_is_400():
    with pytest.raises(HTTPException) as caught:
        _decode_upload(encode(b""), "note.md", make_settings())
    assert caught.value.status_code == 400


def test_oversized_content_is_413():
    settings = make_settings(max_upload_bytes=4)
    with pytest.raises(HTTPException) as caught:
        _decode_upload(encode(b"12345"), "note.md", settings)
    assert caught.value.status_code == 413


def test_unsupported_extension_is_400():
    with pytest.raises(HTTPException) as caught:
        _decode_upload(encode(b"data"), "scan.docx", make_settings())
    assert caught.value.status_code == 400
