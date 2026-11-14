const test = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const vm = require("node:vm");

function element() {
  return {
    children: [], events: {},
    setAttribute(name, value) { this[name] = value; },
    append(...items) { this.children.push(...items); },
    replaceChildren() { this.children.length = 0; },
    addEventListener(type, handler) { this.events[type] = handler; },
  };
}

function harness(withLibrary = true) {
  const series = [], chart = {
    addSeries(type, options) {
      return {setData(points) { series.push({options, points}); }};
    },
    timeScale: () => ({fitContent() {}, getVisibleLogicalRange: () => null}),
    subscribeClick(fn) { this.click = fn; },
    subscribeCrosshairMove(fn) { this.hover = fn; },
    remove() { this.removed = true; },
  };
  const window = withLibrary ? {LightweightCharts: {
    createChart: () => chart, CrosshairMode: {Normal: 0}, LineSeries: {},
  }} : {};
  vm.runInNewContext(readFileSync(join(__dirname,
    "../../src/ai_quant_assistant/web/static/workbench-charts.js"), "utf8"),
    {window, document: {createElement: element}});
  return {host: element(), ui: window.WorkbenchCharts, chart, series};
}

const rows = [
  {date: "2026-11-03", portfolio_nav: 1.2, benchmark_nav: 1.1},
  {date: "2026-11-02", portfolio_nav: "1.1", benchmark_nav: null},
  {date: "2026-11-01", portfolio_nav: 1, benchmark_nav: 0},
];

test("net values retain independent gaps, zeroes, and date selection across input methods", () => {
  const {host, ui, series, chart} = harness();
  ui.drawNav(host, rows, true);
  const [, strategy, beforeGap, afterGap] = series;
  assert.equal(JSON.stringify(strategy.points.map(p => p.value)), "[1,1.1,1.2]");
  assert.equal(beforeGap.points.length, 1);
  assert.equal(beforeGap.points[0].value, 0);
  assert.equal(afterGap.points.length, 1);
  const [plot, , label, output] = host.children;
  const slider = label.children[1];
  chart.click({time: strategy.points[1].time});
  assert.equal(slider.value, "1");
  chart.hover({time: strategy.points[0].time});
  assert.match(output.textContent, /已选 2026-11-02 · 策略净值 1.1 · 基准净值 缺失/);
  plot.events.keydown({key: "ArrowRight", preventDefault() {}});
  assert.equal(slider.value, "2");
  plot.events.keydown({key: "Escape", preventDefault() {}});
  chart.hover({time: strategy.points[0].time});
  assert.match(output.textContent, /2026-11-01.*基准净值 0$/);
  ui.clear(host);
  assert.equal(chart.removed, true);
  assert.equal(host.children.length, 0);
});

test("without the renderer, the date slider still reads ambiguous and missing records", () => {
  const {host, ui} = harness(false);
  ui.drawNav(host, [...rows, {...rows[0], portfolio_nav: 2}]);
  const [plot, , label, output] = host.children;
  assert.equal(plot.hidden, true);
  assert.match(output.textContent, /多条记录有差异/);
  const slider = label.children[1];
  slider.value = "1";
  slider.events.input();
  assert.match(output.textContent, /基准净值 缺失/);
});
