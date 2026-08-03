"""Playwright rendering.

Two things matter here and both were learned the hard way against Naukri:

1. `headless=True` with Playwright's default bundled *headless shell* is
   trivially bot-detected - the page comes back as a 313-byte stub. Launching
   with `channel="chromium"` uses Chrome's **new headless mode**, which renders
   the real page and needs no display server, so it still works in a container.
2. Images, fonts and media are pure cost for a text scrape, so they are aborted
   at the network layer. That alone roughly halves page load time.

The renderer is shared across sources and bounded by a semaphore.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType

from app.config import settings
from app.http_client import DEFAULT_USER_AGENTS

logger = logging.getLogger(__name__)

BLOCKED_ASSETS = "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,otf,mp4,webm,avi}"

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
]

# Removes the most obvious `navigator.webdriver` tell before any page script runs.
STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


def playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


class BrowserRenderer:
    """One browser per pipeline run; pages are created and closed per render."""

    def __init__(self, concurrency: int | None = None) -> None:
        limit = concurrency or settings.browser_concurrency
        self._semaphore = asyncio.Semaphore(max(1, limit))
        self._playwright = None
        self._browser = None
        self._context = None
        self.enabled = False

    async def __aenter__(self) -> BrowserRenderer:
        if not settings.playwright_fallback:
            logger.info("Browser rendering disabled by configuration")
            return self
        if not playwright_available():
            logger.warning(
                "playwright is not installed - browser-backed sources will return nothing"
            )
            return self

        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._launch()
            self._context = await self._browser.new_context(
                user_agent=DEFAULT_USER_AGENTS[0],
                viewport={"width": 1440, "height": 900},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            await self._context.add_init_script(STEALTH_INIT)
            await self._context.route(BLOCKED_ASSETS, self._abort)
            self.enabled = True
            logger.info("Browser ready (channel=%s)", settings.browser_channel or "bundled")
        except Exception as exc:
            logger.warning("Could not start browser: %s", exc)
            await self._shutdown()
        return self

    async def _launch(self):
        """Prefer the requested channel; fall back to the bundled build."""
        channel = settings.browser_channel.strip()
        if channel:
            try:
                return await self._playwright.chromium.launch(
                    headless=settings.playwright_headless,
                    channel=channel,
                    args=LAUNCH_ARGS,
                )
            except Exception as exc:
                logger.warning(
                    "Channel %r unavailable (%s); falling back to the bundled browser, "
                    "which some sites will block. Run: playwright install chromium",
                    channel, exc,
                )
        return await self._playwright.chromium.launch(
            headless=settings.playwright_headless, args=LAUNCH_ARGS
        )

    @staticmethod
    async def _abort(route) -> None:
        try:
            await route.abort()
        except Exception:
            pass

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._shutdown()

    async def _shutdown(self) -> None:
        self.enabled = False
        for closable in (self._context, self._browser):
            try:
                if closable is not None:
                    await closable.close()
            except Exception:
                pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._context = self._browser = self._playwright = None

    async def render(
        self,
        url: str,
        *,
        wait_for: str | None = None,
        scrolls: int = 2,
        timeout_ms: int = 45000,
    ) -> str | None:
        """Load a URL and return settled HTML, or None on failure."""
        if not self.enabled or self._context is None:
            return None

        async with self._semaphore:
            page = await self._context.new_page()
            try:
                await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                if wait_for:
                    try:
                        await page.wait_for_selector(wait_for, timeout=15000)
                    except Exception:
                        logger.debug("Selector %r never appeared on %s", wait_for, url)
                        return None
                # Bounded scrolling for lazy-loaded cards, instead of the old
                # blanket sleep(8) that ran whether or not it was needed.
                for _ in range(scrolls):
                    await page.mouse.wheel(0, 3500)
                    await page.wait_for_timeout(500)
                return await page.content()
            except Exception as exc:
                logger.debug("Render failed for %s: %s", url, exc)
                return None
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
