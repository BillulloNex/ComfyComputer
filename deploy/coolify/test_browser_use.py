"""Test script to verify browser-use and Playwright connectivity and operations via CDP."""

import asyncio
import re
import sys
import httpx
from browser_use import Browser
from playwright.async_api import async_playwright


async def resolve_ws_url(cdp_url: str) -> str:
    """Resolve an HTTP/HTTPS CDP endpoint to a public WebSocket URL."""
    if cdp_url.startswith("ws://") or cdp_url.startswith("wss://"):
        return cdp_url

    version_url = cdp_url.rstrip("/") + "/json/version"
    print(f"Fetching CDP version metadata from {version_url}...")
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, verify=False) as client:
        resp = await client.get(version_url)
        resp.raise_for_status()
        data = resp.json()
        ws_url = data["webSocketDebuggerUrl"]
        print(f"Raw webSocketDebuggerUrl from Chrome: {ws_url}")

        # If Chrome returned an internal container IP, translate to public domain
        if "172." in ws_url or "10." in ws_url or "127.0.0.1" in ws_url:
            public_domain = cdp_url.split("://")[-1].split("/")[0]
            ws_url = re.sub(r"ws://[0-9a-zA-Z.:]+/(devtools/)", f"wss://{public_domain}/\\1", ws_url)
            print(f"Translated to public WebSocket URL: {ws_url}")
        return ws_url


async def test_browser_use_native(cdp_url: str):
    """Test using native browser-use Browser class."""
    print(f"\n--- Testing Native browser-use with CDP ({cdp_url}) ---")
    ws_url = await resolve_ws_url(cdp_url)
    browser = Browser(cdp_url=ws_url)
    await browser.connect()
    print("browser-use: Connected to browser successfully!")

    targets = browser.session_manager.get_all_page_targets()
    print(f"browser-use: Discovered {len(targets)} active browser target(s):")
    for t in targets:
        print(f"  - Target ID: {t.target_id[:8]}... | Title: {t.title} | URL: {t.url}")

    await browser.stop()
    print("browser-use: Native test completed successfully!\n")


async def test_playwright_cdp(cdp_url: str):
    """Test using Playwright over CDP to navigate and capture screenshots."""
    print(f"\n--- Testing Playwright over CDP ({cdp_url}) ---")
    ws_url = await resolve_ws_url(cdp_url)
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(ws_url)
        print("Playwright: Connected over CDP successfully!")

        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()
        page = await context.new_page()

        test_url = "https://cua.ai"
        print(f"Playwright: Navigating to {test_url}...")
        await page.goto(test_url, wait_until="networkidle")

        title = await page.title()
        print(f"Playwright: Page title: '{title}'")
        assert len(title) > 0, "Page title should not be empty"

        screenshot_path = "browser_use_test.png"
        await page.screenshot(path=screenshot_path)
        print(f"Playwright: Screenshot saved to {screenshot_path}")

        await page.close()
        print("Playwright: CDP navigation and screenshot test passed!\n")


async def main(url: str):
    print(f"Starting end-to-end CDP validation against: {url}")
    await test_browser_use_native(url)
    await test_playwright_cdp(url)
    print("ALL TESTS PASSED: browser-use and Playwright CDP are 100% operational!")


if __name__ == "__main__":
    target_url = sys.argv[1] if len(sys.argv) > 1 else "https://cdp-computer.beenex.cloud"
    asyncio.run(main(target_url))

