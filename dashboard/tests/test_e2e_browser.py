"""End-to-end browser tests: drive the real dashboard page in Chromium against a seeded warehouse.

Complements ``test_api_smoke.py`` (JSON contracts) by proving the *page* works: it loads without script
errors, every tab renders, filtering/sorting/drawer interactions behave, conformal intervals reach the UI,
the audit form handles input, and the layout holds up on a phone. The app runs on a real local port with a
temporary DuckDB file, never the developer's warehouse.

The page loads Chart.js / KaTeX from a CDN, so the module skips itself (rather than failing) when that CDN
is unreachable. Setup: ``pip install playwright && python -m playwright install chromium``.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import urllib.request

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from dashboard.tests.test_api_smoke import _seed  # noqa: E402

CDN_PROBE = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"
VISIBLE_TABS = ["overview", "system", "audit", "packages", "models", "accuracy", "validation", "agents"]


REQUIRED = os.environ.get("REQUIRE_BROWSER_TESTS") == "1"


def _skip_or_fail(reason: str):
    """Locally a missing browser/CDN skips; in CI (REQUIRE_BROWSER_TESTS=1) it must fail the build."""
    if REQUIRED:
        pytest.fail(f"browser tests are required here but cannot run: {reason}")
    pytest.skip(reason)


def _cdn_reachable() -> bool:
    try:
        with urllib.request.urlopen(CDN_PROBE, timeout=4) as resp:  # noqa: S310 - fixed https URL
            return resp.status == 200
    except OSError:
        return False


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    if not _cdn_reachable():
        _skip_or_fail("CDN unreachable: the page needs Chart.js/KaTeX from jsdelivr")
    from dashboard.app import main, queries

    wh = _seed(str(tmp_path_factory.mktemp("e2e") / "e2e.duckdb"))
    queries._wh_cache = wh
    main._response_cache.clear()
    main._audit_limiter.clear()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "dashboard server failed to start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
    wh.close()


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        try:
            launched = p.chromium.launch()
        except playwright_sync.Error as exc:  # browser binary not installed
            _skip_or_fail(f"Chromium not installed: {exc}")
        else:
            try:
                yield launched
            finally:
                launched.close()


@pytest.fixture()
def page(browser, base_url):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: pg.errors.append(f"console.error: {m.text}") if m.type == "error" else None)
    pg.on("response", lambda r: pg.errors.append(f"HTTP {r.status} {r.url}")
          if r.status >= 400 and r.url.startswith(base_url) else None)
    pg.goto(base_url, wait_until="networkidle")
    yield pg
    ctx.close()


def _ignore_external(errors):
    """Third-party font/CDN failures are not dashboard bugs."""
    return [e for e in errors if "fonts.g" not in e and "jsdelivr" not in e]


def test_page_loads_without_script_errors(page):
    assert "OSS Radar" in page.title()
    page.wait_for_selector("#kpis .kpi, #kpis > *")
    assert _ignore_external(page.errors) == []


def test_overview_kpis_movers_and_risks(page):
    page.wait_for_selector("#movers .mvr")
    assert "2" in page.inner_text("#kpis")  # two tracked packages
    assert page.locator("#movers .mvr").first.inner_text().lower().startswith("vllm")
    assert page.locator("#risks .mvr").first.inner_text().lower().startswith("langchain")
    assert "github" in page.inner_text("#sourceHealth").lower()


@pytest.mark.parametrize("tab", VISIBLE_TABS)
def test_every_tab_activates_and_renders_without_errors(page, tab):
    page.click(f'nav.tabs button[data-tab="{tab}"]')
    page.wait_for_selector(f'section[data-view="{tab}"]:not([hidden])')
    visible = page.locator("section[data-view]:not([hidden])")
    assert visible.count() == 1  # exactly one tab content visible
    assert page.locator(f'nav.tabs button[data-tab="{tab}"].on').count() == 1
    page.wait_for_load_state("networkidle")
    assert _ignore_external(page.errors) == []


def test_package_search_filters_rows(page):
    page.click('nav.tabs button[data-tab="packages"]')
    page.wait_for_selector("#pbody tr.row")
    assert page.locator("#pbody tr.row").count() == 2  # vllm + langchain have predictions
    page.fill("#search", "vllm")
    assert page.locator("#pbody tr.row").count() == 1
    assert "vllm" in page.inner_text("#pbody")
    page.fill("#search", "zzz-no-such-package")
    assert "no matches" in page.inner_text("#pbody")


def test_category_filter_and_sort_toggle(page):
    page.click('nav.tabs button[data-tab="packages"]')
    page.wait_for_selector("#pbody tr.row")
    page.select_option("#catf", "llm")
    assert page.locator("#pbody tr.row").count() == 1
    page.select_option("#catf", "")
    names_desc = page.locator("#pbody td.pkg").all_inner_texts()
    assert names_desc[0] == "vllm"  # default sort: momentum high -> low
    page.click('#ptable th[data-s="momentum_score"]')  # toggle ascending
    names_asc = page.locator("#pbody td.pkg").all_inner_texts()
    assert names_asc[0] != "vllm"


def test_table_tooltip_shows_conformal_range(page):
    page.click('nav.tabs button[data-tab="packages"]')
    page.wait_for_selector("#pbody tr.row")
    row = page.locator("#pbody tr.row", has_text="vllm")
    tip = row.locator('[data-testid="d70"]').get_attribute("title")
    # log-growth bounds -0.05 / 0.30 become percent changes via expm1: -4.9% / +35.0%
    assert tip.replace("\u2212", "-") == "80% range -4.9% to +35.0%"


def test_drawer_shows_interval_and_closes_with_escape(page):
    page.click('nav.tabs button[data-tab="packages"]')
    page.click("#pbody tr.row >> text=vllm")
    page.wait_for_selector("#drawer.on h2")
    assert page.inner_text("#drawer h2") == "vllm"
    assert "80% range" in page.inner_text('#drawer [data-testid="interval"]')
    assert "downloads accelerating" in page.inner_text("#drawer")
    page.keyboard.press("Escape")
    page.wait_for_selector("#drawer:not(.on)")


def test_drawer_shows_new_advisory_probability_or_nothing(page):
    page.click('nav.tabs button[data-tab="packages"]')
    page.click("#pbody tr.row >> text=vllm")
    page.wait_for_selector("#drawer.on h2")
    assert page.inner_text('#drawer [data-testid="advisory"]') == "new advisory: 12% in 14d · 25% in 30d"
    page.keyboard.press("Escape")
    page.click("#pbody tr.row >> text=langchain")
    page.wait_for_selector("#drawer.on h2")
    assert page.inner_text('#drawer [data-testid="advisory"]').strip() == ""  # not scored: no number


def test_drawer_degrades_gracefully_without_interval(page):
    page.click('nav.tabs button[data-tab="packages"]')
    page.click("#pbody tr.row >> text=langchain")
    page.wait_for_selector("#drawer.on h2")
    assert "not calibrated" in page.inner_text('#drawer [data-testid="interval"]')


def test_clicking_scrim_closes_drawer(page):
    page.click("#movers .mvr >> nth=0")
    page.wait_for_selector("#drawer.on")
    page.click("#scrim", position={"x": 5, "y": 5})
    page.wait_for_selector("#drawer:not(.on)")


def test_audit_requires_input_then_renders_results(page):
    page.click('nav.tabs button[data-tab="audit"]')
    page.click("#auditRun")
    assert "paste some packages first" in page.inner_text("#auditNote")

    canned = {
        "summary": {"total": 2, "audited": 2, "critical": 0, "high": 1, "watch": 0, "healthy": 1, "exposed": 0},
        "packages": [
            {"name": "vllm", "status": "ok", "verdict": "healthy", "version": "1.0", "vuln_count": 0,
             "trend_pct": 5, "risk_score": 20, "momentum_score": 80},
            {"name": "langchain", "status": "ok", "verdict": "high", "version": "0.1", "vuln_count": 2,
             "vuln_kind": "active", "max_severity": "HIGH", "trend_pct": -3, "risk_score": 70,
             "momentum_score": 40},
        ],
    }
    page.route("**/api/audit", lambda route: route.fulfill(json=canned))
    page.fill("#auditIn", "vllm==1.0\nlangchain==0.1")
    page.click("#auditRun")
    page.wait_for_selector("#auditResults tr.row")
    assert "2 of 2 audited" in page.inner_text("#auditSummary")
    assert page.locator("#auditResults tr.row").count() == 2
    assert page.locator("#auditRun").is_enabled()  # button re-enabled after the call


def test_self_audit_button_triggers_a_request_and_renders_its_result(page):
    canned = {
        "summary": {"total": 1, "audited": 1, "critical": 0, "high": 0, "watch": 0, "healthy": 1, "exposed": 0},
        "packages": [{"name": "vllm", "status": "ok", "verdict": "healthy", "version": "1.0",
                      "vuln_count": 0, "trend_pct": 1, "risk_score": 10, "momentum_score": 90}],
    }
    page.route("**/api/self-audit", lambda route: route.fulfill(json=canned))
    page.click('nav.tabs button[data-tab="audit"]')
    before = page.inner_text("#auditSummary")
    with page.expect_request("**/api/self-audit") as request_info:
        page.click("#auditSelf")
    assert request_info.value.method == "GET"
    page.wait_for_function(
        "(old) => document.querySelector('#auditSummary').innerText !== old", arg=before
    )
    assert "1 of 1 audited" in page.inner_text("#auditSummary")
    assert _ignore_external(page.errors) == []


def test_agent_activity_timeline_lists_seeded_agents(page):
    page.click('nav.tabs button[data-tab="agents"]')
    page.wait_for_selector("#timeline .act")
    text = page.inner_text("#timeline")
    assert "DataEngineer" in text and "MLOps" in text


def test_model_history_lists_champions(page):
    page.click('nav.tabs button[data-tab="models"]')
    page.wait_for_selector("#mbody tr")
    text = page.inner_text("#mbody").lower()
    assert "growth" in text and "risk" in text


def test_unknown_route_and_static_assets(page, base_url):
    assert page.request.get(f"{base_url}/health").json()["status"] == "ok"
    assert page.request.get(f"{base_url}/static/validation.json").ok
    assert page.request.get(f"{base_url}/api/does-not-exist").status == 404


def test_mobile_layout_has_no_horizontal_scroll(browser, base_url):
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True)
    pg = ctx.new_page()
    pg.goto(base_url, wait_until="networkidle")
    pg.wait_for_selector("#kpis > *")
    overflow = pg.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1, f"page overflows horizontally by {overflow}px on a 375px screen"
    ctx.close()
