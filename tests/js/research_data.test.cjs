const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = readFileSync(path.join(__dirname, "../../src/ai_quant_assistant/web/static/research-data.js"), "utf8").split('if ($("#research-data-panel"))')[0];
const report = {ready:false,complete:false,affected_symbols:["600519"],checks:[{symbol:"600519",dataset:"financial_metric",metric:"roe",status:"missing",reason:"无历史指标"}],blockers:[],warnings:[]};
const plan = {plan_id:"data-plan-fixture",version:1,input_sha256:"a".repeat(64),status:"draft",input:{resolved_symbols:["600519"]},report,items:[{symbol:"600519",dataset:"financial_metric",start_date:"2022-01-01",end_date:"2024-08-30",status:"pending",reason:"缺少 ROE"}],network_notice:"访问公网，不执行研究",task:null};

function harness(responder, confirmed = true) {
  const elements = new Map(), calls = [], timers = new Map();
  let timerId = 0;
  function element(selector) {
    if (!elements.has(selector)) elements.set(selector, {textContent:"",innerHTML:"",value:"",dataset:{},disabled:false,hidden:false,listeners:{},children:[],scrollIntoView(){},
      addEventListener(name, fn){this.listeners[name] = fn;}, appendChild(child){this.children.push(child);}, querySelectorAll(){return [];}});
    return elements.get(selector);
  }
  const context = vm.createContext({$:element,window:{confirm:() => confirmed},clearTimeout(id){timers.delete(id);},setTimeout(fn){timers.set(++timerId,fn);return timerId;},state:{},
    document:{createElement:() => element(`created-${elements.size}`)},
    formPayload:() => ({symbols:[]}),labPayload:() => ({symbols:[]}),screenPayload:() => ({symbols:[]}),
    escapeHtml:value => String(value ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;"),
    requestJson:async (url, options = {}) => {calls.push({url,options}); return responder(url, options);},
  });
  vm.runInContext(source, context);
  return {context,element,calls,timers,setPlan:(value=plan) => context.renderResearchDataPlan(JSON.parse(JSON.stringify(value)))};
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes,no) => {resolve=yes;reject=no;});
  return {promise,resolve,reject};
}

function assertDisabled(h) {
  for (const action of ["create","confirm","retry","refresh","recheck"]) assert.equal(h.element(`#research-data-${action}`).disabled,true, action);
}

test("stock navigation can invalidate a plan without scrolling away from the overview", () => {
  const h = harness(() => assert.fail("no network"));
  let scrolls = 0;
  h.element("#research-data-panel").scrollIntoView = () => {scrolls++;};
  h.context.beginResearchDataContext({scroll:false});
  assert.equal(scrolls,0);
  h.context.beginResearchDataContext();
  assert.equal(scrolls,1);
  assertDisabled(h);
});

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
  const gaps = h.context.researchReadinessHtml({...report,checks:[{symbol:"600036",dataset:"daily_bar",reason:"需核验",gap_details:[{date:"2015-01-01",classification:"unverified_gap",reason:"<未知>"}]}]});
  assert.match(gaps, /暂时无法核验/);
  assert.match(gaps, /&lt;未知&gt;/);
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

test("initial controls are disabled and real empty backtest click clears a previous single-stock report", async () => {
  const h = harness(() => assert.fail("empty manual range must not send a request"));
  const workbench = readFileSync(path.join(__dirname, "../../src/ai_quant_assistant/web/static/workbench.js"), "utf8");
  vm.runInContext(workbench.slice(workbench.indexOf("function selectedPool("),workbench.indexOf("function stockPoolOptions(")),h.context);
  vm.runInContext(workbench.slice(workbench.indexOf("function formPayload("),workbench.indexOf("function showTask(")),h.context);
  h.element("#research-stock-pool option:checked").value = "manual";
  h.context.bindResearchData();
  assertDisabled(h);
  h.setPlan({...plan,report:{...report,affected_symbols:["688111"]}});
  h.element("#symbols").value = " ，  ";
  await h.element("#backtest-form").children[0].listeners.click();
  assert.match(h.element("#research-data-error").textContent,/请选择一个非空股票池/);
  assert.equal(h.element("#research-data-report").innerHTML,"");
  assert.match(h.element("#research-data-plan").textContent,/尚未创建/);
  assertDisabled(h);
  await h.context.researchDataAction("create");
  await h.context.researchDataAction("confirm");
  assert.equal(h.calls.length,0);
});

test("form payload errors also clear the previous report and executable plan", async () => {
  const h = harness(() => assert.fail("no request"));
  h.setPlan();
  await h.context.openResearchReadiness("factor_backtest",() => {throw new Error("表单字段无效");});
  assert.equal(h.element("#research-data-error").textContent,"表单字段无效");
  assert.equal(h.element("#research-data-report").innerHTML,"");
  assertDisabled(h);
});

test("omitted default pool and explicit pool revision are preserved; empty range is not substituted", async () => {
  const h = harness(() => report);
  for (const parameters of [{as_of:"2024-08-30"},{stock_pool_id:"pool-a",stock_pool_revision:7}]) {
    await h.context.openResearchReadiness("screen",parameters);
    assert.deepEqual(JSON.parse(h.calls.at(-1).options.body).parameters,parameters);
    assert.equal(h.element("#research-data-create").disabled,false);
  }
  await h.context.openResearchReadiness("factor_lab",{symbols:["688111"]});
  assert.match(h.element("#research-data-error").textContent,/至少需要两只股票/);
  assert.equal(h.calls.length,2);
  assertDisabled(h);
});

for (const errorText of ["请求失败（422）：日期范围无效", "请求失败（500）", "Failed to fetch"]) {
  test(`new failed preflight (${errorText}) clears old report and cannot create a plan`, async () => {
    const pending = deferred();
    const h = harness(() => pending.promise);
    h.setPlan();
    const check = h.context.openResearchReadiness("factor_backtest",{symbols:["688111"]});
    assert.equal(h.element("#research-data-report").innerHTML,"");
    assertDisabled(h);
    await h.context.researchDataAction("create");
    assert.equal(h.calls.length,1);
    pending.reject(new Error(errorText));await check;
    assert.equal(h.element("#research-data-error").textContent,errorText);
    assert.equal(h.element("#research-data-report").innerHTML,"");
    assertDisabled(h);
    await h.context.researchDataAction("create");
    assert.equal(h.calls.length,1);
  });
}

test("a corrected single-stock preflight enables planning even when financial data is missing", async () => {
  const h = harness(url => url.endsWith("preflight") ? report : plan);
  await h.context.openResearchReadiness("factor_backtest",{symbols:[]});
  await h.context.openResearchReadiness("screen",{symbols:["688111"]});
  assert.equal(h.element("#research-data-error").textContent,"");
  assert.equal(h.element("#research-data-create").disabled,false);
  assert.equal(h.element("#research-data-confirm").disabled,true);
  await h.context.researchDataAction("create");
  assert.deepEqual(JSON.parse(h.calls[1].options.body).parameters.symbols,["688111"]);
  assert.equal(h.element("#research-data-confirm").disabled,false);
  assert.equal(h.element("#research-data-create").disabled,true);
});

for (const outcome of ["success","error"]) {
  test(`old plan creation ${outcome} cannot restore old data or buttons in a new failed scope`, async () => {
    const pending = deferred();
    const h = harness(url => url.endsWith("preflight") ? report : pending.promise);
    await h.context.openResearchReadiness("screen",{symbols:["600519"]});
    const create = h.context.researchDataAction("create");
    await h.context.openResearchReadiness("factor_backtest",{symbols:[]});
    const currentError = h.element("#research-data-error").textContent;
    if (outcome === "success") pending.resolve(plan); else pending.reject(new Error("old error"));
    await create;
    assert.equal(h.element("#research-data-error").textContent,currentError);
    assert.equal(h.element("#research-data-report").innerHTML,"");
    assertDisabled(h);
  });
}

test("late old action cannot clear the new scope busy guard or trigger a duplicate plan", async () => {
  const old = deferred(), current = deferred();let creations=0;
  const h = harness(url => url.endsWith("preflight") ? report : (++creations === 1 ? old.promise : current.promise));
  await h.context.openResearchReadiness("screen",{symbols:["600519"]});
  const first = h.context.researchDataAction("create");
  await h.context.openResearchReadiness("screen",{symbols:["688111"]});
  const second = h.context.researchDataAction("create");
  old.resolve(plan);await first;
  assertDisabled(h);
  await h.context.researchDataAction("create");assert.equal(creations,2);
  current.resolve({...plan,plan_id:"new-plan"});await second;
  assert.match(h.element("#research-data-plan").innerHTML,/new-plan/);
});

for (const action of ["recheck","refresh"]) {
  for (const outcome of ["success","error"]) {
    test(`late ${action} ${outcome} is ignored after switching research scope`, async () => {
      const pending = deferred();
      const h = harness(() => pending.promise);h.setPlan();
      const operation = action === "refresh" ? h.context.refreshResearchDataPlan() : h.context.researchDataAction(action);
      await h.context.openResearchReadiness("factor_backtest",{symbols:[]});
      const currentError = h.element("#research-data-error").textContent;
      if (outcome === "error") pending.reject(new Error("old failure"));else pending.resolve(action === "refresh" ? plan : report);
      await operation;
      assert.equal(h.element("#research-data-error").textContent,currentError);
      assert.equal(h.element("#research-data-report").innerHTML,"");
      assertDisabled(h);
    });
  }
}

test("an older poll cannot overwrite the newer recheck for the same plan", async () => {
  const old = deferred();
  const h = harness(url => url.endsWith("preflight") ? {...report,ready:true,complete:true} : old.promise);
  h.setPlan();const poll = h.context.refreshResearchDataPlan();
  await h.context.researchDataAction("recheck");
  old.resolve(plan);await poll;
  assert.match(h.element("#research-data-report").innerHTML,/所查数据完整/);
});

test("historical plan selection clears the previous request and failed load leaves no actions", async () => {
  const h = harness(url => url.endsWith("preflight") ? report : Promise.reject(new Error("计划不存在")));
  await h.context.openResearchReadiness("screen",{symbols:["600519"]});
  await h.context.openResearchDataPlan("missing-plan");
  assert.equal(h.element("#research-data-report").innerHTML,"");
  assert.equal(h.element("#research-data-error").textContent,"计划不存在");
  assertDisabled(h);
  await h.context.researchDataAction("create");assert.equal(h.calls.length,2);
});

for (const sourceKind of ["history","copilot"]) {
  test(`late ${sourceKind} plan response cannot replace a newer single-stock preflight`, async () => {
    const pending = deferred();
    const h = harness(url => url.endsWith("preflight") ? {...report,affected_symbols:["688111"]} : pending.promise);
    h.context.state.copilotPlan={plan_id:"copilot-old"};
    const operation = sourceKind === "history" ? h.context.openResearchDataPlan("old-plan") : h.context.copilotDataPlan();
    await h.context.openResearchReadiness("screen",{symbols:["688111"]});
    pending.resolve(plan);await operation;
    assert.match(h.element("#research-data-report").innerHTML,/受影响：688111/);
    assert.equal(h.element("#research-data-confirm").disabled,true);
    assert.equal(h.element("#research-data-create").disabled,false);
  });
}

test("confirmation already submitted stays bound to its plan but its reply cannot replace the new scope", async () => {
  const pending=deferred();const h=harness(() => pending.promise);h.setPlan();
  const operation=h.context.researchDataAction("confirm");
  await h.context.openResearchReadiness("factor_backtest",{symbols:[]});
  pending.resolve({task_id:"old-task"});await operation;
  assert.equal(h.calls.length,1);
  assert.deepEqual(JSON.parse(h.calls[0].options.body),{version:1,input_sha256:plan.input_sha256,confirm:true});
  assert.equal(h.element("#research-data-report").innerHTML,"");
  assertDisabled(h);
});

test("button permissions follow persisted plan state; empty plan cannot confirm or retry", async () => {
  const h=harness(() => assert.fail("no request"));
  h.setPlan({...plan,items:[]});assert.equal(h.element("#research-data-confirm").disabled,true);
  await h.context.researchDataAction("confirm");
  h.setPlan({...plan,status:"failed",task:{status:"failed",progress:95}});
  assert.equal(h.element("#research-data-retry").disabled,false);
  assert.equal(h.element("#research-data-confirm").disabled,true);
  h.setPlan({...plan,status:"confirmed",task:{status:"running",progress:20}});
  assert.equal(h.element("#research-data-retry").disabled,true);
  h.setPlan({...plan,status:"succeeded",task:{status:"succeeded",progress:100}});
  assert.equal(h.element("#research-data-confirm").disabled,true);
  assert.equal(h.element("#research-data-retry").disabled,true);
  assert.equal(h.element("#research-data-refresh").disabled,false);
});

test("completed gaps are shown as finished and cannot retry published items", async () => {
  const h=harness(() => assert.fail("no request"));
  h.setPlan({...plan,status:"failed",outcome:"completed_with_gaps",execution_summary:{total:1,published:1,verified:0,failed:0,pending:0,unknown:0,retryable:0},items:[{...plan.items[0],status:"published"}],task:{status:"failed",progress:95,error:"补齐已执行，但研究数据仍有缺口"}});
  assert.equal(h.element("#research-data-retry").disabled,true);
  assert.match(h.element("#research-data-plan").innerHTML,/同步已经完成，但最终检查仍有数据缺口/);
  assert.match(h.element("#research-data-plan").innerHTML,/执行结束（存在问题） · 100%/);
  assert.doesNotMatch(h.element("#research-data-plan").innerHTML,/95%/);
});

test("real failed item remains retryable while successful items are described as skipped", async () => {
  const h=harness(() => assert.fail("cancelled before request"),false);
  h.setPlan({...plan,status:"failed",outcome:"download_failed",execution_summary:{total:2,published:1,verified:0,failed:1,pending:0,unknown:0,retryable:1},items:[{...plan.items[0],status:"published"},{...plan.items[0],symbol:"000001",status:"failed"}],task:{status:"failed",progress:100,error:"provider timeout"}});
  assert.equal(h.element("#research-data-retry").disabled,false);
  assert.match(h.element("#research-data-plan").innerHTML,/存在真实同步失败/);
  assert.match(h.element("#research-data-plan").innerHTML,/已发布或已验证项目不会重新下载/);
  await h.context.researchDataAction("retry");
  assert.equal(h.calls.length,0);
});

test("failed frozen recheck clears the report and blocks confirmation until a successful recheck", async () => {
  let fail = true;
  const h=harness(() => {if(fail) throw new Error("无法读取当前数据");return report;});h.setPlan();
  await h.context.researchDataAction("recheck");
  assert.equal(h.element("#research-data-report").innerHTML,"");
  assert.equal(h.element("#research-data-confirm").disabled,true);
  assert.equal(h.element("#research-data-recheck").disabled,false);
  await h.context.researchDataAction("confirm");assert.equal(h.calls.length,1);
  fail=false;await h.context.researchDataAction("recheck");
  assert.equal(h.element("#research-data-error").textContent,"");
  assert.equal(h.element("#research-data-confirm").disabled,false);
});
