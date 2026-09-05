const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const source = readFileSync("src/ai_quant_assistant/web/static/stock-research.js", "utf8");
function deferred() {let resolve,reject; const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {resolve,reject,promise};}
function harness(responder) {
  const elements = new Map(), calls=[];
  const element = id => {
    if(!elements.has(id)) elements.set(id,{value:"",innerHTML:"",textContent:"",disabled:false,listeners:{},querySelectorAll(){return [];},addEventListener(type,fn){this.listeners[type]=fn;}});
    return elements.get(id);
  };
  const context=vm.createContext({document:{querySelector:element,addEventListener(){}},window:{addEventListener(){}},URLSearchParams,Intl,Date,history:{pushState(){}},location:{pathname:"/stocks/600519",search:""},beginResearchDataContext(){element("#old-plan").textContent="";},
    fetch:async (url,options={})=>{calls.push({url,options}); const body=await responder(url,options);return {ok:true,json:async()=>body};}});
  vm.runInContext(source,context);
  element("#stock-symbol").value="600519";element("#stock-mode").value="current";element("#stock-start").value="2012-01-01";element("#stock-adjustment").value="qfq";
  return {context,element,calls,eval:code=>vm.runInContext(code,context)};
}
const overview = symbol=>({stock:{symbol,name:`name-${symbol}`,exchange:"SSE",listing_status:"active",management:{status:"active",availability:"ready"},coverage:{},pools:[],task:null},limitations:["unknown"]});
const empty={items:[],pagination:{page:1,pages:1,total:0}};
test("direct load is local GET only and generation stays disabled until overview succeeds",async()=>{
  const gate=deferred(); const h=harness(url=>url.includes("overview")?gate.promise:empty);
  const pending=h.context.loadStock(false);
  assert.equal(h.element("#stock-generate").disabled,true);
  gate.resolve(overview("600519")); await pending;
  assert.equal(h.element("#stock-generate").disabled,false);
  assert.ok(h.calls.every(c=>!c.options.method));
  assert.ok(h.calls.every(c=>c.url.startsWith("/api/v1/stocks/600519/")));
});
for(const lateError of [false,true]) test(`old stock ${lateError?"error":"success"} cannot overwrite new stock`,async()=>{
  const gate=deferred();const h=harness(url=>url.includes("600519/overview")?gate.promise:url.includes("overview")?overview("000001"):empty);
  const first=h.context.loadStock(false);h.element("#stock-symbol").value="000001";await h.context.loadStock(false);
  if(lateError)gate.reject(new Error("old error"));else gate.resolve(overview("600519"));
  await first;assert.equal(h.element("#stock-title").textContent,"000001 · name-000001");assert.equal(h.element("#stock-error").textContent,"");
});
test("empty symbol clears prior stock, summary and plan without sending a request",async()=>{
  const h=harness(()=>assert.fail("no network"));h.element("#stock-symbol").value="";h.element("#stock-summary").innerHTML="old";h.element("#old-plan").textContent="old-plan";
  await h.context.loadStock(false);assert.match(h.element("#stock-error").textContent,/空股票范围/);assert.equal(h.element("#stock-summary").innerHTML,"");assert.equal(h.element("#old-plan").textContent,"");assert.equal(h.element("#stock-generate").disabled,true);
});
test("switching as_of invalidates a pending summary without cancelling its server save",async()=>{
  const gate=deferred();const h=harness(()=>gate.promise);
  h.eval('stockState.ready=true;stockState.symbol="600519";stockState.options={mode:"historical",as_of:"2024-03-20"}');
  const first=h.context.generateStockSummary();await h.context.generateStockSummary();assert.equal(h.calls.length,1);
  h.context.invalidateStock();gate.resolve({summary_id:"old"});await first;
  assert.equal(h.element("#stock-summary").innerHTML,"");assert.equal(h.element("#stock-generate").disabled,true);
  assert.equal(h.calls[0].options.method,"POST");assert.equal(JSON.parse(h.calls[0].options.body).as_of,"2024-03-20");
});
test("backtest prefill top_n matches one frozen stock; no implicit execution",()=>{
  const h=harness(()=>assert.fail("no network"));h.eval('stockState.ready=true;stockState.symbol="600519";stockState.options={adjustment:"none"}');
  h.element("#stock-research-type").value="factor_backtest";h.element("#stock-research-end").value="2024-03-20";h.element("#stock-research-start").value="2024-01-01";h.element("#stock-metric").value="roe";h.element("#stock-benchmark").value="csi000300";
  const input=h.context.stockPrepareInput();assert.equal(input.parameters.top_n,1);assert.equal(input.parameters.symbols[0],"600519");assert.equal(input.parameters.adjustment,"none");assert.equal(h.calls.length,0);
});
test("untrusted records and quote are escaped and portfolio scope is preserved",()=>{
  const h=harness(()=>empty);const html=h.context.stockRecordHtml({title:'<img src=x onerror=alert(1)>',scope:"该股票参与的组合研究",resolved_symbols:["600519","000001"],evidence:[{quote:"<script>bad</script>",document_id:"safe",page_number:1}]},"events");
  assert.ok(!html.includes("<script>"));assert.ok(!html.includes("<img"));assert.match(html,/组合研究/);assert.match(html,/&lt;script&gt;/);
});
test("restoring URL restores historical mode and stock without submitting research",()=>{
  const h=harness(()=>assert.fail("no request"));h.eval('location.pathname="/stocks/000001";location.search="?mode=historical&as_of=2024-01-01&adjustment=none"');h.context.restoreStockLocation();assert.equal(h.element("#stock-symbol").value,"000001");assert.equal(h.element("#stock-as-of").value,"2024-01-01");assert.equal(h.element("#stock-adjustment").value,"none");assert.equal(h.calls.length,0);
});
