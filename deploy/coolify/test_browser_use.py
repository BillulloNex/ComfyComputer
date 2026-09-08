"""Test script to verify browser-use connectivity and operations via CDP."""

import asyncio
import sys
from playwright.async_api import async_playwright


async def test_cdp_connection(cdp_url: str = "http://89.169.114.125:9222"):
    print(f"Connecting to Chrome over CDP at {cdp_url}...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            print("Connected to browser successfully!")

            contexts = browser.contexts
            if contexts:
                context = contexts[0]
            else:
                context = await browser.new_context()

            page = await context.new_page()
            print("Navigating to https://example.com...")
            await page.goto("https://example.com", wait_until="networkidle")

            title = await page.title()
            print(f"Page title: {title}")

            heading = await page.inner_text("h1")
            print(f"H1 content: {heading}")

            screenshot_path = "browser_use_test.png"
            await page.screenshot(path=screenshot_path)
            print(f"Screenshot saved to {screenshot_path}")

            assert "Example Domain" in title or "Example" in heading
            print("SUCCESS: browser-use CDP test passed!")
            await page.close()
    except Exception as e:
        print(f"ERROR: CDP connection test failed: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9222"
    asyncio.run(test_cdp_connection(url))
