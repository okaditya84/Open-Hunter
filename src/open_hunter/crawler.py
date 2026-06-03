"""Headless-browser rendering for JS-heavy career pages.

Playwright's *sync* API must be used from a single thread. Since the pipeline
processes companies concurrently, we confine the browser to one dedicated
worker thread and talk to it through a queue. Any thread can call render();
the work is serialised onto the browser thread and the HTML comes back.

This is the *fallback* path. The ATS API clients handle the common, reliable
case; the browser is for the long tail of bespoke career sites.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .logging_util import get_logger

log = get_logger(__name__)


@dataclass
class _RenderRequest:
    url: str
    scroll: bool
    event: threading.Event
    result: Optional[str] = None
    error: Optional[str] = None


_STOP = object()


class BrowserCrawler:
    def __init__(self, config: Config):
        self.config = config
        self._queue: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._start_error: Optional[str] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def _ensure_started(self) -> bool:
        with self._lock:
            if self._started:
                return self._start_error is None
            self._started = True
            ready = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(ready,), daemon=True, name="browser"
            )
            self._thread.start()
            ready.wait(timeout=60)
            return self._start_error is None

    def _run(self, ready: threading.Event) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            self._start_error = f"playwright import failed: {exc}"
            ready.set()
            return

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=self.config.user_agent,
                    viewport={"width": 1366, "height": 900},
                )
                ready.set()
                self._serve(context)
                context.close()
                browser.close()
        except Exception as exc:  # noqa: BLE001
            self._start_error = f"browser launch failed: {exc}"
            log.warning("browser unavailable: %s", exc)
            ready.set()

    def _serve(self, context) -> None:
        while True:
            req = self._queue.get()
            if req is _STOP:
                self._queue.task_done()
                return
            try:
                req.result = self._render_one(context, req)
            except Exception as exc:  # noqa: BLE001
                req.error = str(exc)
                req.result = None
            finally:
                req.event.set()
                self._queue.task_done()

    def _render_one(self, context, req: _RenderRequest) -> Optional[str]:
        page = context.new_page()
        try:
            page.goto(
                req.url,
                wait_until="domcontentloaded",
                timeout=int(self.config.http_timeout * 1000),
            )
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=8000
                )
            except Exception:  # noqa: BLE001 - networkidle is best-effort
                pass

            if req.scroll:
                for _ in range(6):
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(600)

            return page.content()
        finally:
            page.close()

    # ------------------------------------------------------------------ #
    def render(self, url: str, scroll: bool = True) -> Optional[str]:
        if not self.config.enable_browser:
            return None
        if not self._ensure_started():
            return None
        req = _RenderRequest(url=url, scroll=scroll, event=threading.Event())
        self._queue.put(req)
        # Generous ceiling so a hung page can't wedge the caller forever.
        if not req.event.wait(timeout=self.config.http_timeout * 3 + 30):
            return None
        if req.error:
            log.debug("render error for %s: %s", url, req.error)
        return req.result

    def close(self) -> None:
        if self._started and self._start_error is None and self._thread:
            self._queue.put(_STOP)
            self._thread.join(timeout=30)
