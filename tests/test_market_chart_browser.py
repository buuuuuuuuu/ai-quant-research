"""Exercise real SVG coordinate transforms and date axes when Chromium is installed."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.config import Settings

CHROME = shutil.which("google-chrome") or shutil.which("chromium")
STATIC = Path(__file__).resolve().parents[1] / "src/ai_quant_assistant/web/static"


@pytest.mark.skipif(not CHROME, reason="Chromium browser unavailable")
@pytest.mark.parametrize("page", ["gold", "index-funds"])
def test_click_and_wheel_at_three_viewport_widths(tmp_path, page):
    settings = Settings(
        data_dir=tmp_path, cache_dir=tmp_path, artifact_dir=tmp_path / "artifacts",
        duckdb_path=tmp_path / "catalog.duckdb",
    )
    closes = {f"2026-11-{i:02}": 850 + i for i in range(1, 8)}
    (tmp_path / "sge-au9999-daily.json").write_text(json.dumps({
        "closes": closes, "updated_at": "2026-11-07T13:00:00+00:00",
    }))
    client = TestClient(create_app(settings))
    # Render the real overview shell without its data-loading scripts; footer geometry
    # must match the reading column even when the sidebar is hidden or the column caps.
    overview = client.get("/").text.replace('"/assets/', f'"{STATIC.as_uri()}/')
    overview = re.sub(r"<script\b[^>]*>.*?</script>", "", overview, flags=re.DOTALL)
    (tmp_path / "overview.html").write_text(overview)
    html = client.get(f"/market/{page}?days=7").text.replace('"/assets/', f'"{STATIC.as_uri()}/')
    snapshot = {"snapshot_id": "test", "spec": {"funds": ["SSE.510300"]}, "inputs": {},
                "result": {"points": [
                    {"trade_date": day, "numerator_cny": str(price * 100_000_000), "denominator_cny": "10000000000000",
                     "ratio_fraction": str(price / 100000)} for day, price in closes.items()
                ]}}
    mock = "<script>window.requests=[];window.fetch=async(url,options)=>{window.requests.push({url,options});return {ok:true,json:async()=>(" + json.dumps(
        {"snapshot": snapshot}
    ) + ")};};</script>"
    (tmp_path / "chart.html").write_text(html.replace("</head>", mock + "</head>"))
    harness = """<!doctype html><meta charset="utf-8"><body><output id="result">pending</output>
<script>
function check(condition, msg) { if (!condition) throw Error(msg); }
function contained(node, container, msg) {
  const rect = node.getBoundingClientRect(), bounds = container.getBoundingClientRect();
  check(rect.width > 0 && rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1, msg);
}
async function checkFooter(width) {
  const frame = document.createElement("iframe");
  frame.width = width; frame.height = 900;
  const ready = new Promise(resolve => frame.onload = resolve);
  frame.src = "overview.html"; document.body.append(frame); await ready;
  const doc = frame.contentDocument, win = frame.contentWindow;
  const footer = doc.querySelector("body > footer");
  const column = doc.querySelector("main > .page-heading").getBoundingClientRect();
  const bounds = footer.getBoundingClientRect(), style = win.getComputedStyle(footer);
  check(Math.abs(bounds.left + parseFloat(style.paddingLeft) - column.left) <= 1, "footer left alignment");
  check(Math.abs(bounds.right - parseFloat(style.paddingRight) - column.right) <= 1, "footer right alignment");
  frame.remove();
}
async function run(width) {
  const frame = document.createElement("iframe");
  frame.width = width;
  frame.height = 900;
  const ready = new Promise(resolve => frame.onload = resolve);
  frame.src = "chart.html";
  document.body.append(frame);
  await ready;
  const doc = frame.contentDocument, win = frame.contentWindow;
  const gold = !!doc.querySelector(".gold-chart");
  const charts = [...doc.querySelectorAll(gold ? ".gold-chart svg" : "#index-result svg")];
  check(charts.length === 1, "chart count");
  for (const svg of charts) {
    const position = svg.createSVGPoint();
    position.x = gold ? 500 : (Number(svg.querySelector("circle").getAttribute("cx")) +
      Number([...svg.querySelectorAll("circle")].at(-1).getAttribute("cx"))) / 2; position.y = 100;
    const screen = position.matrixTransform(svg.getScreenCTM());
    const coords = {clientX: screen.x, clientY: screen.y, bubbles: true};
    svg.dispatchEvent(new win.MouseEvent("click", coords));
    const selected = gold ? doc.querySelector("#gold-selected-day").textContent :
      doc.querySelector("#index-result select").value;
    check(selected.includes("2026-11-04"), "selected date " + selected);
    const range = () => gold ? doc.querySelector(".gold-chart figcaption").textContent :
      [...svg.querySelectorAll("text")].slice(-2).map(el => el.textContent).join("/");
    const original = range();
    function wheel(deltaY) {
      const event = new win.WheelEvent("wheel", {...coords, cancelable: true, deltaY});
      svg.dispatchEvent(event);
      check(event.defaultPrevented, "wheel ignored");
    }
    wheel(-100);
    check(range() !== original, "zoom in");
    wheel(100);
    check(range() === original, "zoom out");
    if (gold) {
      const ticks = [...doc.querySelectorAll(".gold-axis time")]
        .filter(el => win.getComputedStyle(el).display !== "none")
        .map(el => el.getBoundingClientRect());
      check(ticks.length >= 3, "date ticks missing");
      ticks.slice(1).forEach((tick, i) => check(ticks[i].right <= tick.left, "date labels overlap"));
    }
  }
  if (!gold) {
    const stats = [...doc.querySelectorAll(".market-stats > div")];
    stats.forEach(card => {
      const value = card.querySelector("dd");
      contained(value, card, "stat value clipped");
      if (width <= 600) {
        const range = doc.createRange(); range.selectNodeContents(value);
        check(range.getClientRects().length === 1, "mobile stat amount wraps");
      }
    });
    if (width <= 600) check(stats[0].getBoundingClientRect().bottom <= stats[1].getBoundingClientRect().top,
      "mobile stats need separate rows");
    const metric = doc.querySelector(".market-trends select");
    for (const name of ["numerator", "denominator", "ratio"]) {
      metric.value = name; metric.dispatchEvent(new win.Event("change"));
      await new Promise(resolve => win.setTimeout(resolve, 60));
      const svg = doc.querySelector(".market-trends svg");
      const labels = [...svg.querySelectorAll("text")];
      const dates = labels.slice(-2).map(node => node.getBoundingClientRect());
      check(dates[0].right < dates[1].left, name + " date labels overlap");
      const circles = [...svg.querySelectorAll("circle")];
      const price = labels[0].getBoundingClientRect();
      check(price.left >= svg.getBoundingClientRect().left, name + " value clipped at left edge");
      check(price.right < circles[0].getBoundingClientRect().left, name + " value overlaps plot");
      if (name !== "ratio") {
        check(labels[0].textContent.includes(name === "numerator" ? "亿元" : "万亿元"), "compact CNY unit");
        circles[0].dispatchEvent(new win.MouseEvent("click", {bubbles: true}));
        check(doc.querySelector(".market-trends p").textContent.includes(name === "numerator" ? "85,100,000,000.00" : "10,000,000,000,000.00"), "full CNY readout");
      }
    }
    check(!doc.querySelector("#market-chart").hidden, "chart is initial panel");
    check(doc.querySelector("#market-settings").hidden, "settings initially hidden");
    const disclosure = doc.querySelector("#market-list-disclosure");
    check(disclosure.open === (width > 700), "mobile watchlist starts compact");
    if (!disclosure.open) disclosure.querySelector("summary").click();
    const search = doc.querySelector("#market-search");
    search.value = "zz500"; search.dispatchEvent(new win.Event("input"));
    check([...doc.querySelectorAll(".market-watch-row")].filter(row => !row.hidden).length === 1, "index search");
    doc.querySelector('[data-market-pick="000905"]').click();
    check(doc.querySelector("#index-code").value === "000905", "watchlist switches index");
    check(doc.querySelector("#market-title").textContent === "中证500", "instrument heading");
    const star = doc.querySelector('[data-market-star="000905"]');
    star.click();
    const stored = JSON.parse(win.localStorage.getItem("market-favorites-v1"));
    check(stored.includes("000905") === (star.getAttribute("aria-pressed") === "true"), "favorite saved");
    // Leave deterministic storage for subsequent viewport fixtures.
    if (star.getAttribute("aria-pressed") === "true") star.click();
    search.value = ""; search.dispatchEvent(new win.Event("input"));
    doc.querySelector('[data-market-range="7d"]').click();
    check(doc.querySelector("#index-range").value === "7d", "quick date range");
    check(doc.querySelector('[data-market-range="7d"]').getAttribute("aria-pressed") === "true", "range state");
    doc.querySelector("#market-tab-settings").click();
    check(!doc.querySelector("#market-settings").hidden && doc.querySelector("#market-chart").hidden, "switch to settings");
    for (const field of doc.querySelectorAll("#market-settings .fund-field")) {
      const label = field.querySelector("label").getBoundingClientRect();
      const control = field.querySelector("input, select");
      contained(control, field, "form control clipped");
      check(control.getBoundingClientRect().left - label.right >= 12, "label touches control");
    }
    const start = doc.querySelector("#index-start").getBoundingClientRect();
    const end = doc.querySelector("#index-end").getBoundingClientRect();
    check(end.top >= start.bottom + 12 || end.left >= start.right + 12, "date controls crowded");
    const picker = doc.querySelector("#index-code");
    picker.value = "000905";
    picker.dispatchEvent(new win.Event("change"));
    await new Promise(resolve => win.setTimeout(resolve, 0));
    doc.querySelector("#index-add-examples").click();
    check(doc.querySelectorAll("#index-selected button").length === 5, "bulk selection");
    check(doc.querySelectorAll('#index-examples button[aria-pressed="true"]').length === 5,
      "selected state");
    doc.querySelector("#index-selected button").click();
    check(doc.querySelectorAll("#index-selected button").length === 4, "remove fund");
    const firstExample = doc.querySelector("#index-examples button");
    // Compare the settled selection cue, not an intermediate hover/color animation.
    firstExample.style.transition = "none";
    const unselectedColor = win.getComputedStyle(firstExample).backgroundColor;
    firstExample.focus();
    firstExample.click();
    check(doc.querySelectorAll("#index-selected button").length === 5, "toggle selection");
    check(win.getComputedStyle(firstExample).backgroundColor !== unselectedColor,
      "selected example has no visual feedback");
    check(doc.querySelector(".fund-snapshot-scope").textContent.includes("SSE.510300"),
      "actual snapshot group");
    doc.querySelector("#market-tab-profiles").focus();
    doc.querySelector("#market-tab-profiles").dispatchEvent(new win.KeyboardEvent("keydown", {key: "ArrowLeft", bubbles: true}));
    check(!doc.querySelector("#market-chart").hidden, "keyboard tab navigation");
    doc.querySelector("#market-tab-profiles").click();
    check(!doc.querySelector("#market-profiles").hidden, "profile tab");
    const cards = [...doc.querySelectorAll(".fund-profile")];
    check(cards.length === 5, "fund profiles count");
    const summary = cards[0].querySelector("summary");
    summary.focus(); summary.click();
    check(cards[0].open, "fund profile expansion");
    check(cards[0].textContent.includes("2013-02-06"), "fund inception");
    check(cards[0].textContent.includes("已披露持仓不等于实时持仓"), "holdings limitation");
    check(cards[0].querySelector('a[href$="ccmx_510500.html"]'), "holdings source");
    for (const node of cards[0].querySelectorAll("dt, dd, a")) contained(node, cards[0], "profile content clipped");
    const term = cards[0].querySelector("dt").getBoundingClientRect();
    const definition = cards[0].querySelector("dd").getBoundingClientRect();
    check(width <= 600 ? definition.top >= term.bottom : definition.left >= term.right + 12,
      "profile label and value layout");
    doc.querySelector("#index-selected button").click();
    check(doc.querySelector(".fund-profile").open, "profile remains open on selection change");
    check(doc.documentElement.scrollWidth <= width, "page overflow");
    doc.querySelector("#market-tab-settings").click();
    for (const control of doc.querySelectorAll("#index-examples button, #index-selected button")) {
      check(control.getBoundingClientRect().right <= width, "picker overflow");
    }
    doc.querySelector("#index-clear-funds").click();
    check(doc.querySelector("#index-funds").value === "", "clear selection");
    check(win.requests.every(request => !request.options?.method || request.options.method === "GET"), "navigation must not submit sync");
    for (const name of ["chart", "settings", "profiles"]) {
      doc.querySelector(`[data-market-tab="${name}"]`).click();
      check(doc.documentElement.scrollWidth <= width, name + " page overflow");
    }
  }
  frame.remove();
}
(async () => {
  try {
    for (const width of [390, 768, 1440]) await run(width);
    for (const width of [390, 768, 1440, 1920]) await checkFooter(width);
    document.querySelector("#result").textContent = "passed";
  } catch (error) { document.querySelector("#result").textContent = String(error); }
})();
</script>"""
    path = tmp_path / "test.html"
    path.write_text(harness)
    output = subprocess.run(
        [CHROME, "--headless", "--no-sandbox", "--disable-dev-shm-usage",
         "--allow-file-access-from-files", "--virtual-time-budget=8000",
         f"--user-data-dir={tmp_path / 'profile'}", "--dump-dom", path.as_uri()],
        capture_output=True, text=True, timeout=45, check=True,
    )
    # CI can retain real browser renders for review, without committing generated files.
    if directory := os.environ.get("AI_QUANT_UI_SCREENSHOT_DIR"):
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        renders = [(page, "chart.html")]
        if page == "index-funds":
            cap_script = """<script>window.addEventListener('load', () => {
              const metric = document.querySelector('.market-trends select');
              metric.value = 'denominator'; metric.dispatchEvent(new Event('change'));
            });</script>"""
            (tmp_path / "chart-cap.html").write_text((tmp_path / "chart.html").read_text() + cap_script)
            renders.append(("index-funds-cap", "chart-cap.html"))
            for panel in ("settings", "profiles"):
                panel_script = """<script>window.addEventListener('load', () => {
                  document.querySelector('#index-add-examples').click();
                  document.querySelector('#market-tab-PANEL').click();
                  if ('PANEL' === 'profiles') document.querySelector('.fund-profile summary').click();
                });</script>""".replace("PANEL", panel)
                filename = f"{panel}.html"
                (tmp_path / filename).write_text((tmp_path / "chart.html").read_text() + panel_script)
                renders.append((f"index-funds-{panel}", filename))
            renders.append(("overview-shell", "overview.html"))
        for name, filename in renders:
            for width in (390, 768, 1440):
                subprocess.run(
                    [CHROME, "--headless", "--no-sandbox", "--disable-dev-shm-usage",
                     "--allow-file-access-from-files", "--virtual-time-budget=1500",
                     f"--user-data-dir={tmp_path / 'screenshot-profile'}",
                     f"--window-size={width},1800", "--force-device-scale-factor=1",
                     f"--screenshot={destination / f'{name}-{width}.png'}",
                     (tmp_path / filename).as_uri()],
                    capture_output=True, text=True, timeout=30, check=True,
                )
    assert '<output id="result">passed</output>' in output.stdout, output.stdout[:2000]
