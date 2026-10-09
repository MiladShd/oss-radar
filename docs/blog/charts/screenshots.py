"""Capture product screenshots of the live dashboard (2x) for the write-up."""
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "images"
URL = "https://radar.miladblog.com/"

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1360, "height": 860}, device_scale_factor=2)
    page.goto(URL, wait_until="networkidle")
    page.wait_for_selector("#movers .mvr")
    page.wait_for_timeout(800)
    page.screenshot(path=str(OUT / "06-dashboard-overview.png"))
    page.click('nav.tabs button[data-tab="packages"]')
    page.wait_for_selector("#pbody tr.row")
    page.wait_for_timeout(500)
    page.screenshot(path=str(OUT / "07-dashboard-packages.png"))
    page.click("#pbody tr.row >> nth=0")
    page.wait_for_selector("#drawer.on h2")
    page.wait_for_timeout(900)
    page.screenshot(path=str(OUT / "08-dashboard-package-detail.png"))
    b.close()
print("ok")
