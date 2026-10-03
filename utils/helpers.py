"""Utility helpers."""


def format_price(val: float) -> str:
    """Human-readable price formatting."""
    return f"{int(val):_}".replace("_", " ") + " руб."


def safe_str(exc: BaseException) -> str:
    """str() that never raises.

    aiohttp >= 3.13 ClientResponseError.__str__ dereferences
    request_info.real_url; with request_info=None that raises
    AttributeError. This helper guards logging against that.
    """
    try:
        return str(exc)
    except Exception:
        return f"{type(exc).__name__}(status={getattr(exc, 'status', None)})"


from datetime import UTC, datetime  # noqa: E402  (parse_api_time)


def parse_api_time(value: object) -> datetime | None:
    """P0-4: API `time` as ISO-8601 ('Z' or offset) or unix seconds/ms.

    Returns an aware UTC datetime; None on garbage."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
    else:
        s = str(value).strip()
        try:
            ts = float(s)
        except ValueError:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    if ts != ts or ts in (float("inf"), float("-inf")):  # NaN/inf guard
        return None
    if ts > 1e12:  # milliseconds
        ts /= 1000.0
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, UTC)


def format_money(value: object) -> str:
    """P0-5: the single money format everywhere: '1 234,00 руб.'."""
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",") + " руб."  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
