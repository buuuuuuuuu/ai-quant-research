/* Local query and display are separate from explicit collection. No automatic provider calls. */
(() => {
  const numeric = value => value === null || value === undefined || value === "" ? null : Number(value);
  const money = value => numeric(value) === null ? "—" : (Number(value) / 1e8).toFixed(2);
  function parseFunds(text) {
    return text.trim().split(/\n/).filter(Boolean).map(line => {
      const parts = line.split(/[,，]/).map(x => x.trim());
      if (parts.length !== 3) throw Error("名单每行须为：基金代码,名称,指数分类");
      return {code: parts[0].toUpperCase(), name: parts[1], group: parts[2]};
    });
  }
  function countsText(counts) {
    if (!counts) return "旧任务未记录各类返回条数";
    return `本次接口返回：日线 ${counts.daily||0} 条、份额 ${counts.shares||0} 条、净值 ${counts.nav||0} 条、分钟 ${counts.minute||0} 条（含已存储的重复记录）`;
  }
  if (typeof module !== "undefined") module.exports = {money, numeric, parseFunds, countsText};
  if (typeof document === "undefined") return;
  const $ = id => document.getElementById(id), api = "/api/v1/national-team";
  const el = (tag, text, cls) => {const node = document.createElement(tag); if(text !== undefined) node.textContent = text; if(cls) node.className=cls; return node;};
  const charts = new Set();
  let dirty = false;
  let current = null, generation = 0, taskTimer = null, taskId = null, groups = [];
  async function request(path, body) {
    const response = await fetch(api + path, body ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)} : {});
    const data = await response.json(); if (!response.ok) throw Error(typeof data.detail === "string" ? data.detail : "参数无效，请检查日期、名单及分类"); return data;
  }
  function scope() {
    if (!$('nt-form').reportValidity()) throw Error("请填写完整参数");
    return {start:$("nt-start").value,end:$("nt-end").value,selected_day:$("nt-day").value,
      funds:parseFunds($("nt-funds").value), threshold:String(Number($("nt-threshold").value)*1e8),
      excluded:$("nt-excluded").value.split(/[,，\s]+/).filter(Boolean)};
  }
  function chart(id, title, points, series, unit="亿元") {
    const box=$(id); box.replaceChildren(el("p", title, "nt-chart-label"));
    if(!points.some(p=>series.some(s=>numeric(p[s.key])!==null))) {box.append(el("p","暂无完整数据，采集或导入后显示。","nt-empty"));return;}
    const legend = el("div", undefined, "nt-chart-legend");
    const lines = series.map(({key, name, color}) => {
      const label = el("span", name);
      label.style.color = color;
      legend.append(label);
      return {key, label: name + "（" + unit + "）", color};
    });
    const host = el("div", undefined, "workbench-time-chart");
    host.id = id + "-plot";
    box.append(legend, host);
    charts.add(host);
    const intraday = points.some(point => point.time != null);
    const rows = points.map(point => {
      const row = {...point};
      for (const {key} of lines) {
        const value = numeric(point[key]);
        row[key] = value === null ? null : unit === "亿元" ? value / 1e8 : value;
      }
      return row;
    });
    window.WorkbenchCharts.draw(host, rows, intraday ? "time" : "date", lines, {title, intraday});
  }
  function table(container, headers, rows) {const t=el("table"),head=el("thead"),hr=el("tr");headers.forEach(h=>hr.append(el("th",h)));head.append(hr);t.append(head);const body=el("tbody");rows.forEach(row=>{const tr=el("tr");row.forEach(v=>{const td=el("td",v?.text??v);if(v?.cls)td.className=v.cls;tr.append(td);});body.append(tr);});t.append(body);container.replaceChildren(t);}
  function render(data){charts.forEach(window.WorkbenchCharts.clear);charts.clear();current=data;$("nt-results").hidden=false;$("nt-banner").textContent=(data.demo?"合成示例：虚构价格、交易日和份额，不是市场数据。 ":"")+data.warning;
    const last=data.daily.at(-1),lastMinute=[...data.minute].reverse().find(p=>p.cumulative!==null&&p.cumulative!==undefined);
    $("nt-metrics").replaceChildren();for(const [label,value,note] of [["观察ETF",data.funds.length+"只","固定名单"],["最近日净申购估算",last?.flow==null?"待补齐":money(last.flow)+"亿元",last?.date||"暂无数据"],["日度完整覆盖",(last?.covered||0)+" / "+data.funds.length,"份额与净值同时可用"],["盘中累计成交",money(lastMinute?.cumulative)+"亿元",data.selected_day+" "+(lastMinute?.time||"待更新")]]){const card=el("div",undefined,"nt-metric");card.append(el("span",label),el("strong",value),el("small",note));$("nt-metrics").append(card);}
    const sourceErrors=data.latest_daily?.result?.errors||[];
    $("nt-coverage").textContent=last?`${last.date}：当日份额 ${(last.details||[]).filter(x=>x.shares!=null).length}/${data.funds.length}，单位净值 ${(last.details||[]).filter(x=>x.nav!=null).length}/${data.funds.length}，可计算申赎 ${last.covered}/${data.funds.length}。`+(last.covered<data.funds.length?" 缺失原因见逐日明细。收盘后净值或份额可能尚未披露；自动分钟采集不会补齐日度数据。":"")+(data.latest_daily?" 最近日度任务 "+data.latest_daily.start+" 至 "+data.latest_daily.end+"，结束于 "+data.latest_daily.completed_at+"；"+countsText(data.latest_daily.result?.received_counts)+"。":" 未找到与当前名单和日期匹配的已结束日度任务。")+(sourceErrors.length?" 该任务来源提示："+sourceErrors.slice(0,8).map(e=>e.partition+"："+(e.reason||e.error)+(e.http_status?" HTTP "+e.http_status:"")).join("；"):""):"暂无交易日数据。";
    const green="#216c53",blue="#4c73a1",gold="#af7d21";
    chart("nt-minute-chart","逐分钟合计成交额",data.minute,[{key:"amount",name:"成交额",color:green}]);
    chart("nt-cumulative-chart","累计成交额与同期比较",data.minute,[{key:"cumulative",name:"所选日",color:green},{key:"previous",name:"上一交易日",color:blue},{key:"mean20",name:"前20交易日均值",color:gold}]);
    chart("nt-index-minute","上证综指分钟走势",data.minute,[{key:"index",name:"上证综指",color:blue}],"点");
    $("nt-history").textContent="前20交易日同期有效样本："+(lastMinute?.history_count??data.minute[0]?.history_count??0)+" / 20；不足20日不输出20日均值。缺失分钟会使其后的累计值留空。";
    chart("nt-flow-chart","日度净申购估算",data.daily,[{key:"flow",name:"净申购估算",color:green}]);
    $("nt-samples").textContent=(lastMinute?.sample_percentile!=null?"截至 "+lastMinute.time+"，累计成交额不低于 "+Number(lastMinute.sample_percentile).toFixed(1)+"% 的历史大额申购日同期（有效样本 "+lastMinute.sample_count+" 日；非买入概率）。 ":"缺少同分钟历史样本，暂无成交额分位。 ")+"阈值 "+money(data.threshold)+"亿元；所选日之前符合条件的历史样本 "+data.sample_days.length+" 日。样本日："+(data.sample_days.join("、")||"暂无")+"。";
    for(const [key,label] of [["sample","大额净申购样本"],["normal","其余有效交易日"]]){chart("nt-"+key+"-profile",label+"：15分钟平均成交额",data.profiles[key],[{key:"amount",name:"平均成交额",color:green}]);chart("nt-"+key+"-index",label+"：指数相对前收盘涨跌幅",data.profiles[key],[{key:"index_return",name:"平均涨跌幅",color:blue}],"%");const counts=data.profiles[key].map(p=>p.count);$("nt-"+key+"-profile").append(el("p","各时段实际样本数："+counts.join(" / ")));}
    chart("nt-daily-chart","日度合计成交额",data.daily,[{key:"amount",name:"成交额",color:green}]);chart("nt-index-daily","上证综指日线",data.daily,[{key:"index",name:"指数",color:blue}],"点");
    const latest=data.daily.at(-1)?.date,cutoff=latest?new Date(latest+"T00:00:00Z"):null;if(cutoff)cutoff.setUTCDate(cutoff.getUTCDate()-30);
    const recent=data.daily.filter(p=>!cutoff||p.date>=cutoff.toISOString().slice(0,10));
    table($("nt-heatmap"),["分类（名单内只数）",...recent.map(p=>p.date.slice(5))],groups.map(g=>[g+"（"+data.funds.filter(f=>f.group===g).length+"）",...recent.map(p=>({text:money(p.groups[g]?.flow),cls:numeric(p.groups[g]?.flow)>0?"nt-positive":numeric(p.groups[g]?.flow)<0?"nt-negative":""}))]));
    $("nt-detail-day").replaceChildren();for(const p of data.daily) {const option=el("option",p.date);option.value=p.date;$("nt-detail-day").append(option);}if(latest)$("nt-detail-day").value=latest;details();
    $("nt-provenance").textContent=JSON.stringify({latest_daily:data.latest_daily,batches:data.batches,funds:data.funds,scope:data.scope,start:data.start,end:data.end,version:data.version,threshold:data.threshold,excluded:data.excluded,input_sha256:data.input_sha256,as_of:data.as_of},null,2);
  }
  function details(){const p=current?.daily.find(x=>x.date===$("nt-detail-day").value);table($("nt-details"),["基金","分类","份额（份）","单位净值","份额变化","估算净申购（亿元）","成交额（亿元）","缺失原因"],(p?.details||[]).map(x=>[x.name+" "+x.code,x.group,x.shares??"—",x.nav??"—",x.change??"—",x.corporate_action?"折算待核验":money(x.flow),money(x.amount),(x.missing||[]).join("、")||"完整"]));}
  async function query(){const own=++generation,body=scope();$("nt-status").textContent="正在读取本地数据…";try {const data=await request("/query",body);if(own!==generation)return;render(data);dirty=false;$("nt-status").textContent=data.daily.length?"已读取本地数据；编辑参数后须重新查询。":"本地无可用交易日历；可先体验合成示例或同步数据。";} catch(error) {if(own===generation) throw error;}}
  function guard(fn){return async()=>{try{await fn();}catch(e){$("nt-status").textContent=e.message;}};}
  async function poll(){clearTimeout(taskTimer);if(!taskId)return;try{const t=await request("/tasks/"+encodeURIComponent(taskId));$("nt-task").textContent=t.status+" · "+t.progress+"% · "+t.message+(t.result?" · "+(t.result.mode==="minute"?"分钟采集":"日度采集")+" "+(t.result.start||"")+" 至 "+(t.result.end||"")+" · "+countsText(t.result.received_counts):"")+(t.error?" · "+t.error:"")+(t.result?.errors?.length?" · "+t.result.errors.slice(0,5).map(e=>e.partition+":"+(e.reason||e.error)+(e.http_status?" HTTP "+e.http_status:"")).join("；"):"");if(["queued","running"].includes(t.status)){taskTimer=setTimeout(poll,2500);}else{$("nt-resume").hidden=true;sessionStorage.removeItem("nt-task");taskId=null;$("nt-task").textContent+="。请点击查看本地数据刷新结果。";}}catch(e){$("nt-task").textContent="进度查询失败："+e.message;$("nt-resume").hidden=false;}}
  function confirmed(){if(!$("nt-confirm").checked)throw Error("请勾选确认后再采集或更改计划");}
  async function sync(mode){confirmed();const t=await request("/sync",{...scope(),mode,confirm:true});taskId=t.task_id;sessionStorage.setItem("nt-task",taskId);poll();}
  async function schedule(enabled){confirmed();const result=await request("/schedule",{scope:scope(),enabled,confirm:true});$("nt-schedule").textContent=result.enabled?"交易时段自动采集已启用（服务器统一运行）；关闭页面后仍执行。":"自动采集已停止；已提交的任务会完成。";}
  $("nt-form").addEventListener("submit",e=>{e.preventDefault();guard(query)();});$("nt-form").addEventListener("input",()=>{generation++;dirty=true;$("nt-status").textContent="参数已修改；下方仍为上一次查询结果。";});
  $("nt-detail-day").addEventListener("change",details);$("nt-demo").onclick=guard(async()=>{const own=++generation;const data=await request("/demo");if(own!==generation)return;render(data);$("nt-status").textContent="当前为两只ETF的独立合成示例，未写入数据库。";});
  $("nt-daily").onclick=guard(()=>sync("daily"));$("nt-minute").onclick=guard(()=>sync("minute"));$("nt-auto").onclick=guard(()=>schedule(true));$("nt-stop").onclick=guard(()=>schedule(false));$("nt-resume").onclick=poll;
  $("nt-export").onclick=guard(async()=>{if(!current)throw Error("请先查询或体验示例");const url=URL.createObjectURL(new Blob([JSON.stringify(current,null,2)],{type:"application/json"}));const a=el("a");a.href=url;a.download=current.demo?"synthetic-etf-demo.json":"national-team.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  $("nt-import").onclick=guard(async()=>{const file=$("nt-file").files[0];if(!file)throw Error("请先选择JSON文件");if(file.size>16*1024*1024)throw Error("文件超过16 MiB");const body=JSON.parse(await file.text());body.confirm=true;const result=await request("/import",body);$("nt-status").textContent="已导入 "+result.records+" 条，来源未核验。请重新查询。";});
  setInterval(()=>{if(current&&!current.demo&&!dirty&&!document.hidden&&$("nt-refresh").checked)guard(query)();},60000);
  guard(async()=>{const today=new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Shanghai"}).format(new Date());$("nt-end").value=today;$("nt-day").value=today;
    const catalog=await request("/catalog");groups=catalog.groups;$("nt-funds").value=catalog.funds.map(f=>[f.code,f.name,f.group].join(",")).join("\n");$("nt-groups").textContent="支持分类："+groups.join("、");
    $("nt-status").textContent="名单已载入，点击查看本地数据或体验合成示例。";const plan=await request("/schedule");$("nt-schedule").textContent=plan.enabled?"服务器自动采集已启用；计划名单独立于当前编辑。"+(plan.error||""):"自动采集未启用。";
    taskId=sessionStorage.getItem("nt-task");if(taskId)poll();
  })();
})();
