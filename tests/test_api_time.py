"""P0-4: parse_api_time — ISO-8601 (Z / offset), unix s/ms, garbage."""

from __future__ import annotations

from datetime import UTC, datetime

from utils.helpers import format_money, parse_api_time


class TestParseApiTime:
    def test_iso_z(self):
        dt = parse_api_time("2026-09-29T12:45:15Z")
        assert dt == datetime(2026, 9, 29, 12, 45, 15, tzinfo=UTC)

    def test_iso_offset(self):
        dt = parse_api_time("2026-09-29T15:45:15+03:00")
        assert dt == datetime(2026, 9, 29, 12, 45, 15, tzinfo=UTC)

    def test_iso_naive_gets_utc(self):
        dt = parse_api_time("2026-09-29T12:45:15")
        assert dt == datetime(2026, 9, 29, 12, 45, 15, tzinfo=UTC)

    def test_epoch_seconds(self):
        dt = parse_api_time(1_700_000_000)
        assert dt == datetime.fromtimestamp(1_700_000_000, UTC)

    def test_epoch_milliseconds(self):
        dt = parse_api_time(1_700_000_000_000)
        assert dt == datetime.fromtimestamp(1_700_000_000, UTC)

    def test_numeric_string(self):
        dt = parse_api_time("1700000000")
        assert dt == datetime.fromtimestamp(1_700_000_000, UTC)

    def test_garbage_returns_none(self):
        for bad in (None, "", "abc", "2026-13-40", 0, -5, True, float("nan")):
            assert parse_api_time(bad) is None, bad


def test_format_money():
    assert format_money(1234.5) == "1 234,50 руб."
    assert format_money(700) == "700,00 руб."
