"""Stalzone Bot entry point (Windows-compatible: signal handlers have a fallback)."""

from __future__ import annotations

import asyncio
import signal
import sys

from config.settings import get_settings
from utils.container import AppContainer
from utils.logging_config import configure_logging


async def _run() -> None:
    _setup_event_loop_policy()
    settings = get_settings()
    configure_logging(settings, level=settings.log_level)
    container = AppContainer(settings)
    await container.start()

    stop = asyncio.Event()

    def _sig(*_args) -> None:  # type: ignore[no-untyped-def]
        stop.set()

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _sig)
    else:
        # Windows: signal handlers via loop are unsupported — rely on KeyboardInterrupt
        signal.signal(signal.SIGINT, lambda *_: _sig())
        signal.signal(signal.SIGTERM, lambda *_: _sig())

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await container.stop()


def _setup_event_loop_policy() -> None:
    """Bound the default executor: asyncio.to_thread otherwise spawns
    one thread per CPU core (dozens) which fragments memory on small hosts."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_event_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=4, thread_name_prefix="app-io"))


def main() -> None:
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
