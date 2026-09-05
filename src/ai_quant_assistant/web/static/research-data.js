// Three separate actions: read-only preflight, confirmed download, research execution.
const researchDataState = {request:null, plan:null, busy:false, preflightStatus:"idle", generation:0, readSequence:0, timer:null};

function researchDataError(error) {
  $("#research-data-error").textContent = error?.message || String(error || "");
}

function updateResearchDataControls() {
  const {request, plan, busy, preflightStatus} = researchDataState;
  const waiting = busy || preflightStatus === "checking";
  const failedCheck = preflightStatus === "failed";
  const hasPlan = Boolean(plan?.input_sha256 && Array.isArray(plan.items));
  $("#research-data-create").disabled = waiting || !request || preflightStatus !== "checked" || hasPlan;
  $("#research-data-confirm").disabled = waiting || failedCheck || !hasPlan || plan.status !== "draft" || !plan.items.length;
  $("#research-data-retry").disabled = waiting || failedCheck || !hasPlan || plan.task?.status !== "failed" || !plan.items.length;
  $("#research-data-refresh").disabled = waiting || !hasPlan;
  $("#research-data-recheck").disabled = waiting || !hasPlan;
}

function beginResearchDataContext({scroll = true} = {}) {
  const generation = ++researchDataState.generation;
  clearTimeout(researchDataState.timer);
  researchDataState.request = null;
  researchDataState.plan = null;
  researchDataState.busy = false;
  researchDataState.preflightStatus = "idle";
  researchDataState.readSequence++;
  $("#research-data-report").innerHTML = "";
  $("#research-data-plan").textContent = "尚未创建补齐计划；检查本地数据不会联网。";
  $("#research-data-input").textContent = "";
  $("#research-data-panel").hidden = false;
  if (scroll) $("#research-data-panel").scrollIntoView({behavior:"smooth"});
  researchDataError("");
  updateResearchDataControls();
  return generation;
}

function validateResearchDataSymbols(researchType, parameters) {
  // An omitted range still means the server's default pool. Explicit empty
  // manual input is not silently replaced with that pool or a previous stock.
  if (!Array.isArray(parameters?.symbols)) return;
  const count = parameters.symbols.filter(symbol => String(symbol).trim()).length;
  if (!count) throw new Error("尚未选择研究股票：请选择一个非空股票池，或手动输入六位股票代码后重新预检。");
  if (researchType === "factor_lab" && count < 2) throw new Error("因子实验至少需要两只股票，请调整股票范围后重新预检。");
}

function researchReadinessHtml(report) {
  const checks = report.checks || [];
  const rows = checks.map((check) => `<tr><td>${escapeHtml(check.symbol || "共享/范围")}</td><td>${escapeHtml(check.dataset)} ${escapeHtml(check.metric || check.adjustment || "")}</td><td>${escapeHtml(check.status)}</td><td>${escapeHtml(check.required_start || "—")} → ${escapeHtml(check.required_end || "—")}</td><td>${escapeHtml(check.reason || "")}<br>${escapeHtml(check.cannot_backfill_reason || "")}</td></tr>`).join("");
  return `<p>${report.ready ? "可按已列明缺失策略研究" : "研究预检有阻断项"}；${report.complete ? "所查数据完整" : "仍有缺口/未知项"}。受影响：${escapeHtml((report.affected_symbols || []).join("、") || "无")}</p><p>${escapeHtml((report.blockers || []).join("；"))}</p><p>${escapeHtml((report.warnings || []).join("；"))}</p><div class="table-shell"><table><thead><tr><th>股票</th><th>数据类型</th><th>状态</th><th>所需范围</th><th>诊断</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

async function openResearchReadiness(researchType, parameters) {
  const generation = beginResearchDataContext();
  try {
    // Read form fields inside this scope so even a payload-construction error
    // clears the previous report and cannot leave an old plan executable.
    if (typeof parameters === "function") parameters = parameters();
    const request = JSON.parse(JSON.stringify({research_type:researchType, parameters}));
    $("#research-data-input").textContent = JSON.stringify(request, null, 2);
    validateResearchDataSymbols(researchType, parameters);
    researchDataState.request = request;
    researchDataState.preflightStatus = "checking";
    updateResearchDataControls();
    const report = await requestJson("/api/v1/research-data/preflight", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(researchDataState.request)});
    if (generation !== researchDataState.generation) return;
    researchDataState.preflightStatus = "checked";
    $("#research-data-report").innerHTML = researchReadinessHtml(report);
  } catch (error) {
    if (generation === researchDataState.generation) {
      researchDataState.preflightStatus = "failed";
      researchDataError(error);
    }
  } finally {if (generation === researchDataState.generation) updateResearchDataControls();}
}

function renderResearchDataPlan(plan) {
  researchDataState.plan = plan;
  researchDataState.preflightStatus = "checked";
  const task = plan.task;
  $("#research-data-input").textContent = JSON.stringify(plan.input, null, 2);
  $("#research-data-plan").innerHTML = `<p>${escapeHtml(plan.plan_id)} · version ${escapeHtml(plan.version)} · ${escapeHtml(plan.status)}</p><p>${escapeHtml(plan.network_notice)}</p><p>${escapeHtml(task ? `${task.task_id} · ${task.status} · ${task.progress}% · ${task.error || task.message}` : "尚未确认，不会下载")}</p><ul>${plan.items.map((item) => `<li>${escapeHtml(item.symbol)} ${escapeHtml(item.dataset)} ${escapeHtml(item.adjustment || "")}：${escapeHtml(item.start_date)} → ${escapeHtml(item.end_date)}；约 ${escapeHtml(item.estimated_partitions ?? "未知")} 分区；${escapeHtml(item.status)}；${escapeHtml(item.reason)} ${escapeHtml(item.attempts?.at(-1)?.error || "")}</li>`).join("")}</ul>`;
  $("#research-data-report").innerHTML = researchReadinessHtml(plan.report);
  updateResearchDataControls();
}

async function researchDataAction(action) {
  if (researchDataState.busy) return;
  const generation = researchDataState.generation;
  researchDataState.busy = true;
  clearTimeout(researchDataState.timer);
  const sequence = ++researchDataState.readSequence;
  updateResearchDataControls();
  researchDataError("");
  try {
    let plan = researchDataState.plan;
    if (action === "create") {
      if (!researchDataState.request || researchDataState.preflightStatus !== "checked") throw new Error("请先完成当前参数的预检；参数错误或请求失败时不能创建补齐计划。");
      if (plan) throw new Error("当前已显示补齐计划；如需更换范围，请从研究表单重新预检。");
      plan = await requestJson("/api/v1/research-data/plans", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(researchDataState.request)});
      if (generation !== researchDataState.generation) return;
      renderResearchDataPlan(plan);
    } else {
      if (!plan) throw new Error("请先查看补齐计划");
      const root = `/api/v1/research-data/plans/${encodeURIComponent(plan.plan_id)}`;
      if (action === "confirm" || action === "retry") {
        if (researchDataState.preflightStatus !== "checked") throw new Error("当前预检未完成或失败，请重新检查冻结参数后再确认。");
        if (!plan.items?.length || (action === "confirm" ? plan.status !== "draft" : plan.task?.status !== "failed")) throw new Error("当前计划没有可执行的补齐项，或状态已变化，请重新查看计划。");
        if (!window.confirm(`确认${action === "retry" ? "重试" : "执行"}上述 ${plan.plan_id} version ${plan.version} 的补齐项？将访问公网；成功项先重新检查，失败项可能重新拉取。不执行研究，不下载 PDF。`)) return;
        await requestJson(`${root}/${action}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({version:plan.version,input_sha256:plan.input_sha256,confirm:true})});
        if (generation !== researchDataState.generation) return;
        await refreshResearchDataPlan();
      } else if (action === "recheck") {
        researchDataState.preflightStatus = "checking";
        $("#research-data-report").innerHTML = "";
        const report = await requestJson(`${root}/preflight`, {method:"POST"});
        if (generation !== researchDataState.generation || sequence !== researchDataState.readSequence) return;
        researchDataState.preflightStatus = "checked";
        $("#research-data-report").innerHTML = researchReadinessHtml(report);
      } else await refreshResearchDataPlan();
    }
  } catch (error) {
    if (generation === researchDataState.generation) {
      if (action === "recheck") researchDataState.preflightStatus = "failed";
      researchDataError(error);
    }
  } finally {
    if (generation === researchDataState.generation) {
      researchDataState.busy = false;
      updateResearchDataControls();
      scheduleResearchDataRefresh();
    }
  }
}

function scheduleResearchDataRefresh() {
  clearTimeout(researchDataState.timer);
  if (["queued","running"].includes(researchDataState.plan?.task?.status)) researchDataState.timer = setTimeout(refreshResearchDataPlan, 2000);
}

async function refreshResearchDataPlan() {
  clearTimeout(researchDataState.timer);
  const plan = researchDataState.plan, generation = researchDataState.generation;
  if (!plan) return;
  const sequence = ++researchDataState.readSequence;
  try {
    const updated = await requestJson(`/api/v1/research-data/plans/${encodeURIComponent(plan.plan_id)}`);
    if (generation !== researchDataState.generation || sequence !== researchDataState.readSequence || researchDataState.plan?.plan_id !== plan.plan_id) return;
    renderResearchDataPlan(updated);
    scheduleResearchDataRefresh();
  } catch (error) {if (generation === researchDataState.generation && sequence === researchDataState.readSequence) researchDataError(error);}
}

async function copilotDataPlan() {
  if (!state.copilotPlan || researchDataState.busy) return;
  const planId = state.copilotPlan.plan_id;
  const generation = beginResearchDataContext();
  researchDataState.busy = true;
  updateResearchDataControls();
  try {
    const plan = await requestJson(`/api/v1/research-data/copilot/${encodeURIComponent(planId)}/plan`, {method:"POST"});
    if (generation !== researchDataState.generation) return;
    renderResearchDataPlan(plan);
  } catch (error) {if (generation === researchDataState.generation) researchDataError(error);}
  finally {if (generation === researchDataState.generation) {researchDataState.busy = false;updateResearchDataControls();}}
}

async function openResearchDataPlan(planId) {
  const generation = beginResearchDataContext();
  researchDataState.busy = true;
  updateResearchDataControls();
  try {
    const plan = await requestJson(`/api/v1/research-data/plans/${encodeURIComponent(planId)}`);
    if (generation !== researchDataState.generation) return;
    renderResearchDataPlan(plan);
    scheduleResearchDataRefresh();
  } catch (error) {if (generation === researchDataState.generation) researchDataError(error);}
  finally {if (generation === researchDataState.generation) {researchDataState.busy = false;updateResearchDataControls();}}
}

function bindResearchData() {
  for (const [selector, kind, payload] of [["#backtest-form","factor_backtest",() => formPayload()],["#factor-lab-form","factor_lab",() => labPayload()],["#screen-form","screen",() => screenPayload()]]) {
    const form = $(selector);
    if (!form) continue;
    const button = document.createElement("button");
    button.type = "button"; button.className = "secondary-button"; button.textContent = "研究数据预检 / 查看补齐计划";
    button.addEventListener("click", () => openResearchReadiness(kind, payload));
    form.appendChild(button);
  }
  for (const action of ["create","confirm","retry","refresh","recheck"]) $("#research-data-"+action).addEventListener("click", () => researchDataAction(action));
  $("#copilot-data-plan")?.addEventListener("click", copilotDataPlan);
  $("#research-data-history").addEventListener("click", async () => {
    const generation = researchDataState.generation;
    try {
      const plans = await requestJson("/api/v1/research-data/plans?limit=50");
      if (generation !== researchDataState.generation) return;
      $("#research-data-history-list").innerHTML = plans.map((plan) => `<button type="button" data-data-plan="${escapeHtml(plan.plan_id)}">${escapeHtml(plan.plan_id)} · ${escapeHtml(plan.status)}</button>`).join(" ");
      $("#research-data-history-list").querySelectorAll("[data-data-plan]").forEach((button) => button.addEventListener("click", () => {
        openResearchDataPlan(button.dataset.dataPlan);
      }));
    } catch(error) {if (generation === researchDataState.generation) researchDataError(error);}
  });
  updateResearchDataControls();
}
if ($("#research-data-panel")) bindResearchData();
