const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = readFileSync(path.join(__dirname, "../../src/ai_quant_assistant/web/static/research-data.js"), "utf8").split('if ($("#research-data-panel"))')[0];
const report = {ready:false,complete:false,affected_symbols:["600519"],checks:[{symbol:"600519",dataset:"financial_metric",metric:"roe",status:"missing",reason:"无历史指标"}],blockers:[],warnings:[]};
const plan = {plan_id:"data-plan-fixture",version:1,input_sha256:"a".repeat(64),status:"draft",input:{resolved_symbols:["600519"]},report,items:[],network_notice:"访问公网，不执行研究",task:null};

function harness(responder, confirmed = true) {
  const elements = new Map(), calls = [];
  function element(selector) {
    if (!elements.has(selector)) elements.set(selector, {textContent:"",innerHTML:"",disabled:false,hidden:false,scrollIntoView(){}});
    return elements.get(selector);
  }
  const context = vm.createContext({$:element,window:{confirm:() => confirmed},clearTimeout(){},setTimeout(){},state:{},
    escapeHtml:value => String(value ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;"),
    requestJson:async (url, options = {}) => {calls.push({url,options}); return responder(url, options);},
  });
  vm.runInContext(source, context);
  return {context,element,calls,setPlan:(value=plan) => vm.runInContext(`researchDataState.plan=${JSON.stringify(value)}`, context)};
}

test("preflight and plan preview never confirm or start research", async () => {
  const h = harness(url => url.endsWith("preflight") ? report : plan);
  await h.context.openResearchReadiness("screen", {symbols:["600519"],as_of:"2024-08-30"});
  await h.context.researchDataAction("create");
  assert.deepEqual(h.calls.map(c => c.url), ["/api/v1/research-data/preflight","/api/v1/research-data/plans"]);
  assert.match(h.element("#research-data-plan").innerHTML, /尚未确认，不会下载/);
});

test("cancelling confirmation sends nothing", async () => {
  const h = harness(() => assert.fail("no request"), false);
  h.setPlan();
  await h.context.researchDataAction("confirm");
  assert.equal(h.calls.length, 0);
});

test("confirmation binds server plan version and digest, not browser symbols", async () => {
  const h = harness(url => url.endsWith("confirm") ? {task_id:"task-one"} : {...plan,status:"confirmed",task:{task_id:"task-one",status:"queued",progress:0}});
  h.setPlan();
  await h.context.researchDataAction("confirm");
  assert.deepEqual(JSON.parse(h.calls[0].options.body), {version:1,input_sha256:plan.input_sha256,confirm:true});
  assert.equal(h.calls.length, 2);
  assert.match(h.element("#research-data-plan").innerHTML, /task-one/);
});

test("errors are visible and untrusted dynamic text is escaped", async () => {
  const h = harness(() => {throw new Error("管理 generation 已改变");});
  h.setPlan();
  await h.context.researchDataAction("confirm");
  assert.equal(h.element("#research-data-error").textContent, "管理 generation 已改变");
  const html = h.context.researchReadinessHtml({...report,checks:[{symbol:"<img onerror=bad>",dataset:"finance",reason:"<script>bad</script>"}]});
  assert.doesNotMatch(html, /<img|<script/);
  assert.match(html, /&lt;script/);
});

test("slow old preflight cannot replace newer parameters", async () => {
  let release;
  const h = harness((url, options) => JSON.parse(options.body).parameters.symbols[0] === "600519" ? new Promise(resolve => {release=resolve;}) : {...report,affected_symbols:["000001"]});
  const first = h.context.openResearchReadiness("screen",{symbols:["600519"]});
  await h.context.openResearchReadiness("screen",{symbols:["000001"]});
  release(report); await first;
  assert.match(h.element("#research-data-report").innerHTML, /受影响：000001/);
});

test("completed download only displays new preflight, never runs research", async () => {
  const h = harness(() => ({...plan,status:"succeeded",report:{...report,ready:true,complete:true},task:{task_id:"task-done",status:"succeeded",progress:100}}));
  h.setPlan();
  await h.context.refreshResearchDataPlan();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].options.method, undefined);
  assert.match(h.element("#research-data-report").innerHTML, /所查数据完整/);
});
