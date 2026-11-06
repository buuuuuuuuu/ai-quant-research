"""Exercise real SVG coordinate transforms and date axes when Chromium is installed."""

import json
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
    html = client.get(f"/market/{page}?days=7").text.replace('"/assets/', f'"{STATIC.as_uri()}/')
    snapshot = {"snapshot_id": "test", "spec": {"funds": ["SSE.510300"]}, "inputs": {},
                "result": {"points": [
                    {"trade_date": day, "numerator_cny": str(price), "denominator_cny": "10000",
                     "ratio_fraction": str(price / 10000)} for day, price in closes.items()
                ]}}
    mock = "<script>window.fetch=async()=>({ok:true,json:async()=>(" + json.dumps(
        {"snapshot": snapshot}
    ) + ")});</script>"
    (tmp_path / "chart.html").write_text(html.replace("</head>", mock + "</head>"))
    harness = """<!doctype html><meta charset="utf-8"><body><output id="result">pending</output>
<script>
function check(condition, msg) { if (!condition) throw Error(msg); }
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
  check(charts.length === (gold ? 1 : 2), "chart count");
  for (const svg of charts) {
    const position = svg.createSVGPoint();
    position.x = gold ? 500 : 370; position.y = 100;
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
    firstExample.focus();
    firstExample.click();
    check(doc.querySelectorAll("#index-selected button").length === 5, "toggle selection");
    check(doc.querySelector(".fund-snapshot-scope").textContent.includes("SSE.510300"),
      "actual snapshot group");
    const cards = [...doc.querySelectorAll(".fund-profile")];
    check(cards.length === 5, "fund profiles count");
    const summary = cards[0].querySelector("summary");
    summary.focus(); summary.click();
    check(cards[0].open, "fund profile expansion");
    check(cards[0].textContent.includes("2013-02-06"), "fund inception");
    check(cards[0].textContent.includes("已披露持仓不等于实时持仓"), "holdings limitation");
    check(cards[0].querySelector('a[href$="ccmx_510500.html"]'), "holdings source");
    doc.querySelector("#index-selected button").click();
    check(doc.querySelector(".fund-profile").open, "profile remains open on selection change");
    check(doc.documentElement.scrollWidth <= width, "page overflow");
    for (const control of doc.querySelectorAll("#index-examples button, #index-selected button")) {
      check(control.getBoundingClientRect().right <= width, "picker overflow");
    }
    doc.querySelector("#index-clear-funds").click();
    check(doc.querySelector("#index-funds").value === "", "clear selection");
  }
  frame.remove();
}
(async () => {
  try {
    for (const width of [390, 768, 1440]) await run(width);
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
    assert '<output id="result">passed</output>' in output.stdout, output.stdout[:2000]
