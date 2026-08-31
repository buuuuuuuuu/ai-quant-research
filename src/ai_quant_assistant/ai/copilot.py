from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from ai_quant_assistant.ai.planner import Planner
from ai_quant_assistant.ai.preflight import CoveragePreflight, validate_tool_spec
from ai_quant_assistant.ai.repository import CopilotRepository
from ai_quant_assistant.ai.tool_registry import require_allowed_tool
from ai_quant_assistant.api.schemas import FactorBacktestRequest, FactorLabRequest
from ai_quant_assistant.research.factor_backtest import FactorBacktestService
from ai_quant_assistant.research.factor_lab import FactorLabService
from ai_quant_assistant.storage.operations import ExclusiveOperationLock
from ai_quant_assistant.storage.stock_pools import StockPoolService
from ai_quant_assistant.storage.task_repository import OperationTaskRepository
from ai_quant_assistant.storage.universe import UniverseRepository

logger = logging.getLogger(__name__)


class CopilotService:
    def __init__(
        self,
        *,
        repository: CopilotRepository,
        planner: Planner,
        preflight: CoveragePreflight,
        stock_pools: StockPoolService,
    ) -> None:
        self.repository = repository
        self.planner = planner
        self.preflight = preflight
        self.stock_pools = stock_pools

    def create_conversation(self, title: str = "新研究对话") -> dict[str, Any]:
        return self.repository.create_conversation(title)

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        conversation = self.repository.get_conversation(conversation_id)
        conversation["messages"] = self.repository.list_messages(conversation_id)
        return conversation

    def propose(self, conversation_id: str, question: str) -> dict[str, Any]:
        self.repository.get_conversation(conversation_id)
        self.repository.add_message(conversation_id, "user", question)
        draft = self.planner.plan(question)
        if draft.get("tool_name") and not draft.get("blocked"):
            try:
                proposed_spec = self._resolve_stock_scope(
                    draft["tool_name"], draft.get("spec", {})
                )
                frozen_context = proposed_spec.pop("_stock_pool_context", None)
                draft["spec"] = validate_tool_spec(draft["tool_name"], proposed_spec)
                if frozen_context is not None:
                    draft["spec"]["stock_pool"] = frozen_context
                draft["preflight"] = self.preflight.run(draft["tool_name"], draft["spec"])
                if not draft["preflight"]["ready"]:
                    draft["blocked"] = True
                    draft["warnings"] = [
                        *draft.get("warnings", []),
                        *draft["preflight"]["blockers"],
                    ]
            except (ValidationError, ValueError, PermissionError) as exc:
                draft["blocked"] = True
                draft["preflight"] = {
                    "status": "blocked",
                    "ready": False,
                    "checks": [],
                    "blockers": [str(exc)],
                    "warnings": [],
                }
                draft["warnings"] = [*draft.get("warnings", []), str(exc)]
        plan = self.repository.create_plan(conversation_id, question, draft)
        self.repository.add_message(
            conversation_id,
            "assistant",
            self._preview_text(plan),
            plan_id=plan["plan_id"],
            metadata={"kind": "plan_preview", "status": plan["status"]},
        )
        return self.repository.get_plan(plan["plan_id"])

    def _resolve_stock_scope(self, tool_name: str, spec: dict[str, Any]) -> dict[str, Any]:
        resolved = dict(spec)
        if tool_name not in {"run_factor_backtest", "run_factor_lab"}:
            return resolved
        symbols = resolved.get("symbols")
        if symbols:
            normalized = list(dict.fromkeys(str(item).strip() for item in symbols))
            canonical = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
            context = {
                "pool_id": None,
                "pool_revision": None,
                "resolved_symbols": normalized,
                "symbols_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "resolved_at": datetime.now(UTC).isoformat(),
                "source_type": "explicit",
                "as_of": None,
                "history_verified": False,
                "warning": "显式股票列表可能存在幸存者偏差。",
            }
        else:
            context = self.stock_pools.resolve(
                resolved.pop("stock_pool_id", None),
                revision=resolved.pop("stock_pool_revision", None),
            )
            normalized = list(context["resolved_symbols"])
        resolved["symbols"] = normalized
        resolved["_stock_pool_context"] = context
        return resolved

    def refresh_preflight(self, plan_id: str) -> dict[str, Any]:
        plan = self.repository.get_plan(plan_id)
        if not plan["tool_name"]:
            raise ValueError("blocked plan has no executable tool")
        preflight = self.preflight.run(plan["tool_name"], plan["spec"])
        self.repository.update_plan(plan_id, preflight_json=preflight)
        self.repository.add_event(plan_id, "preflighted", "system", preflight)
        return self.repository.get_plan(plan_id)

    def confirm(self, plan_id: str, *, revision: int, actor: str) -> dict[str, Any]:
        plan = self.repository.get_plan(plan_id)
        if plan["revision"] != revision:
            raise RuntimeError("计划版本已变化，请重新预览后确认。")
        if plan["status"] != "draft":
            raise RuntimeError(f"only a draft plan can be confirmed (current: {plan['status']})")
        refreshed = self.preflight.run(plan["tool_name"], plan["spec"])
        if not refreshed["ready"]:
            self.repository.update_plan(plan_id, status="blocked", preflight_json=refreshed)
            self.repository.add_event(plan_id, "blocked", actor, refreshed)
            raise RuntimeError("数据覆盖预检未通过，计划不能确认。")
        confirmed = self.repository.update_plan(
            plan_id,
            status="confirmed",
            preflight_json=refreshed,
            confirmed_at=datetime.now(UTC).isoformat(),
        )
        self.repository.add_event(plan_id, "confirmed", actor, {"revision": revision})
        return confirmed

    @staticmethod
    def _preview_text(plan: dict[str, Any]) -> str:
        if plan["status"] == "blocked":
            reasons = plan.get("preflight", {}).get("blockers") or plan.get("warnings")
            return "计划暂不可执行：" + "；".join(str(item) for item in reasons)
        return f"已生成《{plan['title']}》。请核对参数和数据覆盖，确认后才会执行。"


class CopilotTaskManager:
    def __init__(
        self,
        *,
        plans: CopilotRepository,
        tasks: OperationTaskRepository,
        factor_backtests: FactorBacktestService,
        factor_lab: FactorLabService,
        universe: UniverseRepository,
        stock_pools: StockPoolService,
        operation_lock: ExclusiveOperationLock,
    ) -> None:
        self.plans = plans
        self.tasks = tasks
        self.factor_backtests = factor_backtests
        self.factor_lab = factor_lab
        self.universe = universe
        self.stock_pools = stock_pools
        self.operation_lock = operation_lock
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="copilot")

    def submit(self, plan_id: str) -> dict[str, Any]:
        plan = self.plans.get_plan(plan_id)
        if plan["status"] != "confirmed":
            raise RuntimeError("research plan must be explicitly confirmed before execution")
        task = self.tasks.create(
            "copilot",
            spec={"research_type": "copilot", "plan_id": plan_id, "tool_name": plan["tool_name"]},
            message="等待执行已确认的研究计划",
        )
        self.plans.update_plan(plan_id, task_id=task["task_id"])
        self.plans.add_event(plan_id, "queued", "system", {"task_id": task["task_id"]})
        self.executor.submit(self._run, plan_id, task["task_id"])
        return self.tasks.get(task["task_id"])

    def _run(self, plan_id: str, task_id: str) -> None:
        started = datetime.now(UTC).isoformat()
        self.tasks.update(
            task_id, status="running", progress=1, message="研究任务已启动", started_at=started
        )
        self.plans.update_plan(plan_id, status="running", started_at=started)
        self.plans.add_event(plan_id, "started", "system", {"task_id": task_id})

        def progress(value: int, message: str) -> None:
            self.tasks.update(task_id, progress=value, message=message)

        try:
            plan = self.plans.get_plan(plan_id)
            preflight = CoveragePreflight(self.plans.database).run(plan["tool_name"], plan["spec"])
            if not preflight["ready"]:
                raise RuntimeError(
                    "execution-time coverage preflight failed: " + "; ".join(preflight["blockers"])
                )
            require_allowed_tool(plan["tool_name"])
            with self.operation_lock.hold():
                result = self._execute(plan["tool_name"], plan["spec"], progress)
            explanation = self._explain(plan, result, preflight)
            completed = datetime.now(UTC).isoformat()
            self.tasks.update(
                task_id,
                status="succeeded",
                progress=100,
                message="研究完成，可查看可追溯解释",
                completed_at=completed,
                run_id=result.get("run_id"),
                result_json={"plan_id": plan_id, "summary": explanation["conclusion"]},
            )
            self.plans.update_plan(
                plan_id,
                status="succeeded",
                run_id=result.get("run_id"),
                result_json=result,
                explanation_json=explanation,
                completed_at=completed,
            )
            self.plans.add_event(plan_id, "succeeded", "system", {"run_id": result.get("run_id")})
            self.plans.add_message(
                plan["conversation_id"],
                "tool",
                explanation["conclusion"],
                plan_id=plan_id,
                metadata={"kind": "grounded_explanation", "run_id": result.get("run_id")},
            )
        except Exception as exc:  # noqa: BLE001 - background failure is persisted
            logger.exception("copilot plan %s failed", plan_id)
            completed = datetime.now(UTC).isoformat()
            self.tasks.update(
                task_id,
                status="failed",
                message="研究失败",
                error=str(exc),
                completed_at=completed,
            )
            self.plans.update_plan(plan_id, status="failed", error=str(exc), completed_at=completed)
            self.plans.add_event(plan_id, "failed", "system", {"error": str(exc)})

    def _execute(self, tool_name: str, spec: dict[str, Any], progress: Any) -> dict[str, Any]:
        if tool_name == "list_stock_pools":
            return {
                "status": "succeeded",
                "run_id": None,
                "stock_pools": self.stock_pools.list(),
                "artifacts": {},
            }
        if tool_name == "run_factor_backtest":
            request = FactorBacktestRequest.model_validate(spec)
            return self.factor_backtests.run(
                request.to_spec(
                    resolved_symbols=spec["symbols"], stock_pool=spec.get("stock_pool")
                ),
                progress_callback=progress,
            )
        if tool_name == "run_factor_lab":
            request = FactorLabRequest.model_validate(spec)
            return self.factor_lab.run(
                request.to_spec(
                    resolved_symbols=spec["symbols"], stock_pool=spec.get("stock_pool")
                ),
                progress_callback=progress,
            )
        if tool_name == "build_point_in_time_universe":
            progress(30, "正在构建 Point-in-Time 股票池")
            result = self.universe.build(
                as_of=spec["as_of"],
                mode=spec["mode"],
                symbols=tuple(spec["symbols"]),
                benchmark_symbol=spec["benchmark_symbol"],
                classification_system=spec["classification_system"],
                require_history=spec["require_verified_industry"],
            )
            progress(95, "股票池与行业覆盖已生成")
            return {"status": "succeeded", "run_id": None, "universe": result, "artifacts": {}}
        raise RuntimeError(f"Copilot execution is not implemented for {tool_name}")

    @staticmethod
    def _explain(
        plan: dict[str, Any], result: dict[str, Any], preflight: dict[str, Any]
    ) -> dict[str, Any]:
        artifacts = result.get("artifacts", {})
        evidence: list[dict[str, Any]] = []
        conclusion: str
        if "metrics" in result:
            metrics = result["metrics"]
            conclusion = (
                f"回测累计收益 {metrics.get('total_return', 0):.2%}，"
                f"相对基准超额 {metrics.get('excess_return', 0):.2%}，"
                f"最大回撤 {metrics.get('max_drawdown', 0):.2%}。"
            )
            evidence.append({"kind": "metrics", "value": metrics})
        elif "summary" in result:
            summary = result["summary"]
            conclusion = (
                f"因子实验完成 {summary.get('strategy_count', 0)} 组策略，"
                f"验证集选择 {summary.get('selected_strategy', '—')}。"
            )
            evidence.append({"kind": "ic_summary", "value": result.get("ic_summary", [])})
        elif "stock_pools" in result:
            pools = result["stock_pools"]
            conclusion = f"当前有 {len(pools)} 个可用股票池。"
            evidence.append({"kind": "stock_pools", "value": pools})
        else:
            universe = result.get("universe", {})
            conclusion = (
                f"截至 {universe.get('as_of')} 构建 {universe.get('security_count', 0)} 只股票，"
                f"行业覆盖率 {universe.get('industry_coverage', 0):.2%}。"
            )
            evidence.append({"kind": "universe", "value": universe})
        if artifacts:
            evidence.append({"kind": "artifacts", "value": artifacts})
        return {
            "conclusion": conclusion,
            "evidence": evidence,
            "lineage": preflight.get("lineage", {}),
            "method": {
                "tool_name": plan["tool_name"],
                "plan_id": plan["plan_id"],
                "run_id": result.get("run_id"),
                "prompt_version": plan["prompt_version"],
            },
            "limitations": [
                *plan.get("warnings", []),
                *preflight.get("warnings", []),
                "结果仅用于研究，不构成投资建议；历史表现不代表未来收益。",
            ],
        }

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
