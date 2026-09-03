// Three separate actions: read-only preflight, confirmed download, research execution.
const researchDataState = {request:null, plan:null, busy:false, generation:0, timer:null};

function researchDataError(error) {
  $("#research-data-error").textContent = error?.message || String(error || "");
}

function researchReadinessHtml(report) {
  const checks = report.checks || [];
  const rows = checks.map((check) => `<tr><td>${escapeHtml(check.symbol || "共享/范围")}</td><td>${escapeHtml(check.dataset)} ${escapeHtml(check.metric || check.adjustment || "")}</td><td>${escapeHtml(check.status)}</td><td>${escapeHtml(check.required_start || "—")} → ${escapeHtml(check.required_end || "—")}</td><td>${escapeHtml(check.reason || "")}<br>${escapeHtml(check.cannot_backfill_reason || "")}</td></tr>`).join("");
  return `<p>${report.ready ? "可按已列明缺失策略研究" : "研究预检有阻断项"}；${report.complete ? "所查数据完整" : "仍有缺口/未知项"}。受影响：${escapeHtml((report.affected_symbols || []).join("、") || "无")}</p><p>${escapeHtml((report.blockers || []).join("；"))}</p><p>${escapeHtml((report.warnings || []).join("；"))}</p><div class="table-shell"><table><thead><tr><th>股票</th><th>数据类型</th><th>状态</th><th>所需范围</th><th>诊断</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

async function openResearchReadiness(researchType, parameters) {
  const generation = ++researchDataState.generation;
  clearTimeout(researchDataState.timer);
  researchDataState.request = JSON.parse(JSON.stringify({research_type:researchType, parameters}));
  researchDataState.plan = null;
  $("#research-data-plan").textContent = "尚未创建补齐计划；检查本地数据不会联网。";
  $("#research-data-input").textContent = JSON.stringify(researchDataState.request, null, 2);
  $("#research-data-panel").hidden = false;
  $("#research-data-panel").scrollIntoView({behavior:"smooth"});
  researchDataError("");
  try {
    const report = await requestJson("/api/v1/research-data/preflight", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(researchDataState.request)});
    if (generation !== researchDataState.generation) return;
    $("#research-data-report").innerHTML = researchReadinessHtml(report);
  } catch (error) {if (generation === researchDataState.generation) researchDataError(error);}
}

function renderResearchDataPlan(plan) {
  researchDataState.plan = plan;
  const task = plan.task;
  $("#research-data-input").textContent = JSON.stringify(plan.input, null, 2);
  $("#research-data-plan").innerHTML = `<p>${escapeHtml(plan.plan_id)} · version ${escapeHtml(plan.version)} · ${escapeHtml(plan.status)}</p><p>${escapeHtml(plan.network_notice)}</p><p>${escapeHtml(task ? `${task.task_id} · ${task.status} · ${task.progress}% · ${task.error || task.message}` : "尚未确认，不会下载")}</p><ul>${plan.items.map((item) => `<li>${escapeHtml(item.symbol)} ${escapeHtml(item.dataset)} ${escapeHtml(item.adjustment || "")}：${escapeHtml(item.start_date)} → ${escapeHtml(item.end_date)}；约 ${escapeHtml(item.estimated_partitions ?? "未知")} 分区；${escapeHtml(item.status)}；${escapeHtml(item.reason)} ${escapeHtml(item.attempts?.at(-1)?.error || "")}</li>`).join("")}</ul>`;
  $("#research-data-report").innerHTML = researchReadinessHtml(plan.report);
  $("#research-data-confirm").disabled = plan.status !== "draft";
  $("#research-data-retry").disabled = !task || task.status !== "failed";
}

async function researchDataAction(action) {
  if (researchDataState.busy) return;
  researchDataState.busy = true;
  researchDataError("");
  try {
    let plan = researchDataState.plan;
    if (action === "create") {
      if (!researchDataState.request) throw new Error("请先从研究表单检查当前参数");
      plan = await requestJson("/api/v1/research-data/plans", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(researchDataState.request)});
      renderResearchDataPlan(plan);
    } else {
      if (!plan) throw new Error("请先查看补齐计划");
      const root = `/api/v1/research-data/plans/${encodeURIComponent(plan.plan_id)}`;
      if (action === "confirm" || action === "retry") {
        if (!window.confirm(`确认${action === "retry" ? "重试" : "执行"}上述 ${plan.plan_id} version ${plan.version} 的补齐项？将访问公网；成功项先重新检查，失败项可能重新拉取。不执行研究，不下载 PDF。`)) return;
        await requestJson(`${root}/${action}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({version:plan.version,input_sha256:plan.input_sha256,confirm:true})});
        await refreshResearchDataPlan();
      } else if (action === "recheck") {
        const report = await requestJson(`${root}/preflight`, {method:"POST"});
        $("#research-data-report").innerHTML = researchReadinessHtml(report);
      } else await refreshResearchDataPlan();
    }
  } catch (error) {researchDataError(error);} finally {researchDataState.busy = false;}
}

async function refreshResearchDataPlan() {
  clearTimeout(researchDataState.timer);
  const plan = researchDataState.plan, generation = researchDataState.generation;
  if (!plan) return;
  try {
    const updated = await requestJson(`/api/v1/research-data/plans/${encodeURIComponent(plan.plan_id)}`);
    if (generation !== researchDataState.generation || researchDataState.plan?.plan_id !== plan.plan_id) return;
    renderResearchDataPlan(updated);
    if (["queued","running"].includes(updated.task?.status)) researchDataState.timer = setTimeout(refreshResearchDataPlan, 2000);
  } catch (error) {researchDataError(error);}
}

async function copilotDataPlan() {
  if (!state.copilotPlan || researchDataState.busy) return;
  researchDataState.busy = true;
  researchDataState.generation++;
  clearTimeout(researchDataState.timer);
  $("#research-data-panel").hidden = false;
  $("#research-data-panel").scrollIntoView({behavior:"smooth"});
  researchDataError("");
  try {
    const plan = await requestJson(`/api/v1/research-data/copilot/${encodeURIComponent(state.copilotPlan.plan_id)}/plan`, {method:"POST"});
    researchDataState.request = null;
    renderResearchDataPlan(plan);
  } catch (error) {researchDataError(error);} finally {researchDataState.busy = false;}
}

function bindResearchData() {
  for (const [selector, kind, payload] of [["#backtest-form","factor_backtest",formPayload],["#factor-lab-form","factor_lab",labPayload],["#screen-form","screen",screenPayload]]) {
    const form = $(selector);
    if (!form) continue;
    const button = document.createElement("button");
    button.type = "button"; button.className = "secondary-button"; button.textContent = "研究数据预检 / 查看补齐计划";
    button.addEventListener("click", () => {try {openResearchReadiness(kind, payload());} catch(error) {researchDataError(error);}});
    form.appendChild(button);
  }
  for (const action of ["create","confirm","retry","refresh","recheck"]) $("#research-data-"+action).addEventListener("click", () => researchDataAction(action));
  $("#copilot-data-plan").addEventListener("click", copilotDataPlan);
  $("#research-data-history").addEventListener("click", async () => {
    try {
      const plans = await requestJson("/api/v1/research-data/plans?limit=50");
      $("#research-data-history-list").innerHTML = plans.map((plan) => `<button type="button" data-data-plan="${escapeHtml(plan.plan_id)}">${escapeHtml(plan.plan_id)} · ${escapeHtml(plan.status)}</button>`).join(" ");
      $("#research-data-history-list").querySelectorAll("[data-data-plan]").forEach((button) => button.addEventListener("click", () => {
        researchDataState.generation++;researchDataState.plan={plan_id:button.dataset.dataPlan};refreshResearchDataPlan();
      }));
    } catch(error) {researchDataError(error);}
  });
}
if ($("#research-data-panel")) bindResearchData();
