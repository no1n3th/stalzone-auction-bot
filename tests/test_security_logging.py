"""utils/security + utils/logging_config: masking, compare, LOG_JSON switch."""

from __future__ import annotations

import logging
from typing import ClassVar

import structlog

from utils.logging_config import configure_logging
from utils.security import constant_time_compare, mask_sensitive, mask_text


class TestMasking:
    def test_mask_sensitive_dict(self):
        data = {"username": "alice", "password": "hunter2", "nested": {"token": "abc123456"}}
        masked = mask_sensitive(data)
        assert masked["password"] == "***"
        assert masked["nested"]["token"] == "***"
        assert masked["username"] == "alice"

    def test_mask_sensitive_list(self):
        out = mask_sensitive([{"api_key": "k"}, {"ok": 1}])
        assert out[0]["api_key"] == "***" and out[1]["ok"] == 1

    def test_mask_text(self):
        secrets = ["supersecretvalue"]
        assert mask_text("token=supersecretvalue ok", secrets) == "token=*** ok"
        assert mask_text("nothing here", secrets) == "nothing here"
        assert mask_text("", secrets) == ""

    def test_constant_time_compare(self):
        assert constant_time_compare("abc", "abc")
        assert not constant_time_compare("abc", "abd")
        assert not constant_time_compare("abc", 123)  # type: ignore[arg-type]


class _SettingsStub:
    log_json = True
    client_secret = ""
    client_id = ""
    discord_webhook_url = ""
    alerting_webhook_url = ""
    database_url = ""
    discord_webhooks: ClassVar[list[str]] = []


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records]


class TestLoggingConfig:
    def test_json_renderer_default(self, caplog):
        configure_logging(_SettingsStub())
        with caplog.at_level(logging.INFO):
            structlog.get_logger("t").info("hello_json", key="value")
        assert any('"event": "hello_json"' in m for m in _messages(caplog))

    def test_console_renderer_when_log_json_false(self, caplog):
        class S(_SettingsStub):
            log_json = False

        configure_logging(S())
        with caplog.at_level(logging.INFO):
            structlog.get_logger("t").info("hello_console")
        msgs = _messages(caplog)
        assert any("hello_console" in m for m in msgs)
        assert not any('"event"' in m for m in msgs)

    def test_secrets_masked_in_logs(self, caplog):
        class S(_SettingsStub):
            client_secret = "topsecretvalue123"

        configure_logging(S())
        with caplog.at_level(logging.INFO):
            structlog.get_logger("t").info("oops", leaked="topsecretvalue123")
        assert not any("topsecretvalue123" in m for m in _messages(caplog))
