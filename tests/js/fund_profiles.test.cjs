const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.attributes = {}; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
}
const descendants = node => [node, ...node.children.flatMap(descendants)];
const texts = node => descendants(node).map(item => item.textContent || "").join("\n");
function setup() {
  const window = {};
  vm.runInNewContext(readFileSync(path.join(__dirname, "../../src/ai_quant_assistant/web/static/fund-profiles.js"), "utf8"), {
    window, document: {createElement: tag => new Element(tag)},
    fetch() { throw new Error("Profiles must not fetch or synchronize on read"); },
  });
  return {profiles: window.FundProfiles, root: new Element()};
}

test("age uses completed calendar months and a Beijing calendar date", () => {
  const {profiles: p} = setup();
  assert.equal(p.age("2012-05-04", "2026-11-03"), "已满14年5个月");
  assert.equal(p.age("2012-05-04", "2026-11-04"), "已满14年6个月");
  assert.equal(p.age("2024-09-20", "2024-09-20"), "成立不足1个月");
  assert.equal(p.age("2024-09-20", "2024-09-19"), "该日期尚未成立");
  assert.equal(p.age("2025-02-30", "2026-11-06"), "存续时间待核实");
  assert.equal(p.age("", "2026-11-06"), "存续时间待核实");
  assert.equal(p.beijingDay(new Date("2026-11-05T16:01:00Z")), "2026-11-06");
});

test("all eight indices and nineteen examples have distinct sourced profiles", () => {
  const {profiles: p, root} = setup();
  const allCodes = [];
  assert.equal(Object.keys(p.examples).length, 8);
  for (const [index, funds] of Object.entries(p.examples)) {
    p.render(root, index);
    assert.equal(root.children.length, funds.length);
    for (const [i, [code, name]] of funds.entries()) {
      allCodes.push(code);
      const card = root.children[i], content = texts(card);
      assert.match(content, /\d{4}-\d\d-\d\d 成立/);
      assert.ok(content.includes(name) && content.includes(code));
      assert.match(content, /2026-11-06/);
      assert.match(content, /本页尚未收录持仓报告/);
      assert.match(content, /已披露持仓不等于实时持仓/);
      assert.match(content, index === "000688" ? /每季度一次/ : /每半年一次/);
      const links = descendants(card).filter(node => node.tag === "a");
      assert.equal(links.length, 6);
      assert.ok(links.some(node => node.href.endsWith(`ccmx_${code.split(".")[1]}.html`)));
      assert.ok(links.every(node => node.href.startsWith("https://") && node.rel === "noopener noreferrer"));
    }
  }
  assert.equal(new Set(allCodes).size, 19);
});

test("manual codes stay unverified, do not inherit the chosen index, and unsafe text creates no link", () => {
  const {profiles: p, root} = setup();
  p.render(root, "000905", ["SSE.599999", "SSE.599999", '<img src=x onerror="bad()">']);
  assert.equal(root.children.length, 7);
  const unknown = root.children[5], unsafe = root.children[6];
  assert.match(texts(unknown), /手动填写不代表已核实/);
  assert.ok(!texts(unknown).includes("每半年一次"));
  assert.ok(!texts(unknown).includes("中证500的成分股"));
  assert.equal(descendants(unsafe).filter(node => node.tag === "a").length, 0);
  assert.equal(unsafe.children[0].textContent, '<img src=x onerror="bad()">（资料待核实）');
});

test("wrong-index selections show their own profile and an open card survives selection edits", () => {
  const {profiles: p, root} = setup();
  p.render(root, "000905", ["SSE.510300"]);
  const wrong = root.children.at(-1);
  assert.match(texts(wrong), /沪深300（000300）；与当前所选指数不同/);
  root.children[0].open = true;
  p.render(root, "000905", ["SSE.510500"]);
  assert.equal(root.children.length, 5);
  assert.equal(root.children[0].open, true);
  p.render(root, "000688");
  assert.equal(root.children.length, 2);
  assert.ok(!texts(root).includes("南方中证500"));
});
