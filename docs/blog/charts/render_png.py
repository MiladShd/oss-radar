"""Render each SVG in docs/blog/images to a 2x PNG with headless Chromium (Playwright)."""
from pathlib import Path

from playwright.sync_api import sync_playwright

images = Path(__file__).resolve().parents[1] / "images"
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1100, "height": 700}, device_scale_factor=2)
    for svg in sorted(images.glob("*.svg")):
        page.goto(svg.as_uri())
        h = page.evaluate("document.documentElement.getBoundingClientRect().height")
        page.set_viewport_size({"width": 1100, "height": int(h)})
        page.screenshot(path=str(svg.with_suffix(".png")), clip={"x": 0, "y": 0, "width": 1100, "height": h})
        print("rendered", svg.with_suffix(".png").name, int(h))
    browser.close()
