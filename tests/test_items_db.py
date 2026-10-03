"""P0-1: sync_items_db parses the real flat listing.json format."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api.client import StalcraftClient


class FakeResp:
    def __init__(self, data):
        self._data = data

    async def json(self, content_type=None):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, data):
        self._data = data

    def get(self, url):
        return FakeResp(self._data)


LISTING = [
    {
        "data": "data/weapon/y5vw.json",
        "name": {"type": "translation", "lines": {"ru": "Скорлупа"}},
    },
    {
        "data": "data/item/q1ab.json",
        "name": {"type": "translation", "lines": {"en": "Weight"}},
    },
    {
        "data": "data/ammo/51l0.json",
        "name": {"type": "translation", "lines": {"ru": "Харя", "en": "Kharja"}},
    },
    {"name": {"lines": {"ru": "без идентификатора"}}},  # no `data` -> skipped
]


async def test_sync_items_db_real_format(tmp_path):
    client = StalcraftClient(SimpleNamespace(), FakeSession(LISTING))
    dest = tmp_path / "items_db.json"
    count = await client.sync_items_db(dest)
    assert count == 3
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data == {
        "y5vw": {"name": "Скорлупа"},
        "q1ab": {"name": "Weight"},  # ru missing -> en fallback
        "51l0": {"name": "Харя"},
    }


async def test_sync_items_db_refuses_empty_result(tmp_path):
    client = StalcraftClient(SimpleNamespace(), FakeSession([]))
    dest = tmp_path / "items_db.json"
    dest.write_text('{"y5vw": {"name": "Скорлупа"}}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="0 items"):
        await client.sync_items_db(dest)
    # the working cache must survive a broken sync untouched
    assert json.loads(dest.read_text(encoding="utf-8")) == {"y5vw": {"name": "Скорлупа"}}
