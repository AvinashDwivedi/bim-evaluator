from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bim_evaluator import evaluate_cases, load_cases, run_system_http
from cost_tracking import summarize_costs

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
CASES = Path(os.getenv("BIM_EVALUATION_CASES", ROOT / "eval-cases.json"))
REPORT = Path(os.getenv("BIM_EVALUATION_REPORT", ROOT / "eval-report.json"))
STATE = Path(os.getenv("BIM_EVALUATION_STATE", ROOT / "eval-run-state.json"))
REPORTS = Path(os.getenv("BIM_EVALUATION_REPORTS_DIR", ROOT / "eval-reports"))
COST_LEDGER = Path(os.getenv("BIM_COST_LEDGER", ROOT / "cost-ledger.jsonl"))
BACKEND = os.getenv("BIM_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
CASE_TIMEOUT_SECONDS = float(os.getenv("BIM_EVALUATION_CASE_TIMEOUT_SECONDS", "600"))

app = FastAPI(title="Independent BIM Evaluator", version="1.4.0")
app.mount("/assets", StaticFiles(directory=WEB), name="assets")


class ChatPayload(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    client_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    request_id: str | None = None
    evaluation_run_id: str | None = None
    evaluation_case_index: int | None = None


class EvaluationOptions(BaseModel):
    judge_mode: Literal["semantic", "exact"] = "semantic"
    judge_model: str | None = None
    question_indices: list[int] | None = Field(default=None, min_length=1)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dataset_fingerprint() -> str:
    """Identify the exact case file backing the active dashboard state."""
    return hashlib.sha256(CASES.read_bytes()).hexdigest()


def empty_state() -> dict:
    dataset_total = len(load_cases(CASES))
    return {
        "run_id": None, "status": "idle", "total": dataset_total,
        "dataset_total": dataset_total, "dataset_fingerprint": dataset_fingerprint(),
        "selected_question_indices": None,
        "current_index": None, "results": [], "summary": None, "error": None,
        "started_at": None, "finished_at": None, "updated_at": now(),
        "case_timeout_seconds": int(CASE_TIMEOUT_SECONDS),
        "options": None, "report_name": None,
    }


def normalize_report(value: dict) -> dict:
    """Convert score-based saved reports to the binary verdict schema."""
    value = dict(value)
    results = []
    for saved_result in value.get("results", []):
        result = dict(saved_result)
        if "correct" not in result:
            result["correct"] = bool(result.get("passed", False))
        for field in (
            "passed", "score", "correctness", "completeness", "groundedness", "exact_match",
        ):
            result.pop(field, None)
        results.append(result)
    value["results"] = results

    if value.get("summary") is not None:
        summary = dict(value["summary"])
        summary.setdefault("correct", summary.get("passed", sum(item["correct"] for item in results)))
        summary.setdefault("incorrect", summary.get("failed", len(results) - summary["correct"]))
        for field in ("passed", "failed", "pass_rate", "average_score"):
            summary.pop(field, None)
        value["summary"] = summary

    if value.get("options") is not None:
        value["options"] = dict(value["options"])
        value["options"].pop("pass_threshold", None)
    value.pop("pass_threshold", None)
    return value


def _report_cost(value: dict) -> dict:
    summary = value.get("summary") or {}
    if isinstance(summary.get("cost"), dict):
        return summary["cost"]
    return summarize_costs([
        result.get("cost") if isinstance(result, dict) else None
        for result in value.get("results", [])
    ])


def load_state() -> dict:
    current = empty_state()
    if not STATE.exists():
        if REPORT.exists():
            try:
                report = normalize_report(json.loads(REPORT.read_text(encoding="utf-8-sig")))
                finished = datetime.fromtimestamp(REPORT.stat().st_mtime, timezone.utc).isoformat()
                if report["summary"]["total"] != current["dataset_total"]:
                    return current
                return {
                    **current, "run_id": "imported-report", "status": "completed",
                    "total": report["summary"]["total"], "results": report["results"],
                    "summary": report["summary"], "finished_at": finished, "updated_at": finished,
                }
            except (OSError, ValueError, KeyError, TypeError):
                pass
        return empty_state()
    try:
        saved = json.loads(STATE.read_text(encoding="utf-8-sig"))
        if saved.get("dataset_fingerprint") != current["dataset_fingerprint"]:
            return current
        value = normalize_report({**current, **saved})
    except (OSError, ValueError):
        return current
    if value["status"] == "running":
        value.update({
            "status": "failed", "error": "Evaluator process stopped during the run.",
            "finished_at": now(), "updated_at": now(),
        })
    return value


def refresh_state_for_dataset() -> None:
    """Reset non-running dashboard state when eval-cases.json changes on disk."""
    global run_state
    if run_state.get("status") == "running":
        return
    fingerprint = dataset_fingerprint()
    if run_state.get("dataset_fingerprint") != fingerprint:
        run_state = empty_state()
        persist_state()


run_state = load_state()
run_task: asyncio.Task | None = None


def persist_state() -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(STATE.suffix + ".tmp")
    temporary.write_text(json.dumps(run_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE)


def _run_report_path(run_id: str) -> Path:
    return REPORTS / f"eval-report-{run_id}.json"


def persist_run_report() -> None:
    """Persist an independent, resumable snapshot for the active run."""
    run_id = run_state.get("run_id")
    if not run_id or run_id == "imported-report":
        return
    REPORTS.mkdir(parents=True, exist_ok=True)
    destination = _run_report_path(run_id)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"schema_version": 3, **run_state}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def append_cost_entry(entry: dict) -> None:
    """Append one standalone chat question to the local cost ledger."""
    COST_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with COST_LEDGER.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def selected_cases(options: EvaluationOptions) -> tuple[list, list[int], int]:
    cases = load_cases(CASES)
    dataset_total = len(cases)
    indices = options.question_indices or list(range(1, dataset_total + 1))
    if len(indices) != len(set(indices)):
        raise ValueError("Question selections must not contain duplicates.")
    invalid = [index for index in indices if index < 1 or index > dataset_total]
    if invalid:
        raise ValueError(f"Question indices are outside the dataset: {invalid}")
    return [cases[index - 1] for index in indices], indices, dataset_total


# Persist a recovered report or mark a run interrupted by a prior evaluator process exit.
persist_state()
persist_run_report()


@app.get("/")
async def chat_page() -> FileResponse:
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/evaluation")
async def evaluation_page() -> FileResponse:
    return FileResponse(WEB / "evaluation.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health() -> dict:
    try:
        first_case = load_cases(CASES)[0]
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{BACKEND}/api/health",
                params={"client_id": str(first_case.client_id), "project_id": str(first_case.project_id)},
            )
            response.raise_for_status()
        return {**response.json(), "backend_url": BACKEND}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="BIM backend is unavailable.") from exc


@app.get("/api/config")
async def config() -> dict:
    scopes = sorted({(str(c.client_id), str(c.project_id)) for c in load_cases(CASES)})
    return {"backend_url": BACKEND, "case_timeout_seconds": int(CASE_TIMEOUT_SECONDS), "scopes": [
        {"client_id": client, "project_id": project} for client, project in scopes
    ]}


@app.post("/api/chat/stream")
async def proxy_chat(payload: ChatPayload, request: Request) -> StreamingResponse:
    async def stream():
        try:
            async with httpx.AsyncClient(timeout=CASE_TIMEOUT_SECONDS) as client:
                async with client.stream(
                    "POST", f"{BACKEND}/api/chat/stream", json=payload.model_dump()
                ) as response:
                    if response.status_code >= 400:
                        yield json.dumps({"type": "error", "message": "BIM backend rejected the request."}) + "\n"
                        return
                    async for line in response.aiter_lines():
                        if await request.is_disconnected():
                            return
                        if line:
                            try:
                                event = json.loads(line)
                                report = (
                                    event.get("report", {})
                                    if isinstance(event, dict) and event.get("type") == "result"
                                    else {}
                                )
                                if not payload.evaluation_run_id and isinstance(report.get("cost"), dict):
                                    append_cost_entry({
                                        "recorded_at": now(), "kind": "chat",
                                        "request_id": payload.request_id,
                                        "client_id": payload.client_id,
                                        "project_id": payload.project_id,
                                        "question": payload.question,
                                        "cost": report["cost"],
                                    })
                            except (OSError, ValueError, TypeError):
                                pass
                            yield line + "\n"
        except httpx.TimeoutException:
            minutes = int(CASE_TIMEOUT_SECONDS // 60)
            yield json.dumps({
                "type": "error",
                "message": f"BIM request exceeded the {minutes}-minute timeout.",
            }) + "\n"
        except Exception:
            yield json.dumps({"type": "error", "message": "BIM backend is unavailable."}) + "\n"
    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.get("/api/evaluation/cases")
async def evaluation_cases() -> dict:
    return {"cases": [case.model_dump(mode="json") for case in load_cases(CASES)]}


@app.get("/api/evaluation/state")
async def evaluation_state() -> dict:
    refresh_state_for_dataset()
    return run_state


@app.get("/api/evaluation/costs/daily")
async def daily_evaluation_cost(start: datetime, end: datetime) -> dict:
    """Aggregate evaluation runs and standalone chat inside a local-day window."""
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise HTTPException(status_code=422, detail="start and end must be timezone-aware and ordered.")
    if (end - start).total_seconds() > 26 * 60 * 60:
        raise HTTPException(status_code=422, detail="Daily cost windows cannot exceed 26 hours.")

    selected: list[dict] = []
    seen_run_ids: set[str] = set()
    if REPORTS.exists():
        for path in REPORTS.glob("eval-report-*.json"):
            try:
                value = normalize_report(json.loads(path.read_text(encoding="utf-8-sig")))
                started_at = datetime.fromisoformat(str(value.get("started_at")))
            except (OSError, ValueError, TypeError):
                continue
            run_id = str(value.get("run_id") or path.stem)
            if run_id in seen_run_ids or started_at.tzinfo is None or not (start <= started_at < end):
                continue
            seen_run_ids.add(run_id)
            selected.append(value)

    run_costs = [_report_cost(value) for value in selected]
    case_costs = [
        result.get("cost")
        for value in selected
        for result in value.get("results", [])
        if isinstance(result, dict)
    ]
    chat_costs: list[dict | None] = []
    if COST_LEDGER.exists():
        try:
            lines = COST_LEDGER.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                entry = json.loads(line)
                recorded_at = datetime.fromisoformat(str(entry.get("recorded_at")))
            except (ValueError, TypeError):
                continue
            if recorded_at.tzinfo is not None and start <= recorded_at < end:
                chat_costs.append(entry.get("cost"))

    total = summarize_costs([*run_costs, *chat_costs])
    total.update({
        "runs": len(selected),
        "questions": len(case_costs) + len(chat_costs),
        "evaluation_questions": len(case_costs),
        "chat_questions": len(chat_costs),
        "questions_with_cost": sum(
            isinstance(cost, dict) and cost.get("estimated_cost_usd") is not None
            for cost in [*case_costs, *chat_costs]
        ),
        "start": start.isoformat(),
        "end": end.isoformat(),
    })
    return total


@app.get("/api/evaluation/reports")
async def evaluation_reports() -> dict:
    reports = []
    if REPORTS.exists():
        paths = sorted(
            REPORTS.glob("eval-report-*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            reports.append({
                "run_id": value.get("run_id"),
                "status": value.get("status"),
                "started_at": value.get("started_at"),
                "finished_at": value.get("finished_at"),
                "total": value.get("total"),
                "summary": normalize_report(value).get("summary"),
                "report_name": path.name,
            })
    return {"reports": reports}


@app.get("/api/evaluation/reports/{run_id}")
async def evaluation_report(run_id: str) -> dict:
    try:
        normalized = str(UUID(run_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Evaluation report was not found.") from exc
    path = _run_report_path(normalized)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Evaluation report was not found.")
    return normalize_report(json.loads(path.read_text(encoding="utf-8-sig")))


async def execute_evaluation(options: EvaluationOptions) -> None:
    global run_state

    async def publish(event: dict) -> None:
        if event["type"] == "case_started":
            run_state["current_index"] = event["index"]
            run_state["current_position"] = event["position"]
        elif event["type"] == "case_result":
            run_state["results"].append(event["result"])
        elif event["type"] == "evaluation_complete":
            run_state.update({
                "status": "completed", "summary": event["report"]["summary"],
                "current_index": None, "current_position": None, "finished_at": now(),
            })
        run_state["updated_at"] = now()
        persist_state()
        persist_run_report()

    try:
        cases, indices, _ = selected_cases(options)
        index_by_case = {
            (str(case.client_id), str(case.project_id), case.question): index
            for case, index in zip(cases, indices)
        }
        answerer = lambda case: run_system_http(
            case, base_url=BACKEND, timeout=CASE_TIMEOUT_SECONDS,
            evaluation_run_id=run_state["run_id"],
            evaluation_case_index=index_by_case[
                (str(case.client_id), str(case.project_id), case.question)
            ],
        )
        report = await evaluate_cases(
            cases, judge_mode=options.judge_mode, judge_model=options.judge_model,
            timeout_seconds=CASE_TIMEOUT_SECONDS,
            answerer=answerer, event_sink=publish, case_indices=indices,
        )
        REPORT.write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except asyncio.CancelledError:
        if run_state["status"] == "running":
            run_state.update({
                "status": "stopped", "finished_at": now(),
                "current_index": None, "current_position": None,
            })
        run_state["updated_at"] = now()
        persist_state()
        persist_run_report()
        raise
    except Exception as exc:
        run_state.update({
            "status": "failed", "error": f"{type(exc).__name__}: evaluation stopped",
            "finished_at": now(), "current_index": None, "current_position": None,
            "updated_at": now(),
        })
        persist_state()
        persist_run_report()


@app.post("/api/evaluation/run", status_code=202)
async def start_evaluation(options: EvaluationOptions) -> dict:
    global run_state, run_task
    if run_task is not None and not run_task.done():
        raise HTTPException(status_code=409, detail="An evaluation is already running.")
    try:
        cases, indices, dataset_total = selected_cases(options)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run_id = str(uuid4())
    run_state = {
        "run_id": run_id, "status": "running", "total": len(cases),
        "dataset_total": dataset_total, "selected_question_indices": indices,
        "dataset_fingerprint": dataset_fingerprint(),
        "current_index": None, "current_position": None,
        "results": [], "summary": None, "error": None,
        "started_at": now(), "finished_at": None, "updated_at": now(),
        "case_timeout_seconds": int(CASE_TIMEOUT_SECONDS),
        "options": options.model_dump(mode="json"),
        "report_name": _run_report_path(run_id).name,
    }
    persist_state()
    persist_run_report()
    run_task = asyncio.create_task(execute_evaluation(options))
    return run_state


@app.post("/api/evaluation/stop")
async def stop_evaluation() -> dict:
    global run_task
    if run_task is None or run_task.done() or run_state["status"] != "running":
        raise HTTPException(status_code=409, detail="No evaluation is running.")
    run_state.update({
        "status": "stopped", "finished_at": now(), "current_index": None,
        "current_position": None, "updated_at": now(),
    })
    persist_state()
    persist_run_report()
    run_task.cancel()
    return run_state


def main() -> None:
    import uvicorn
    uvicorn.run("server:app", host=os.getenv("EVALUATOR_HOST", "127.0.0.1"),
                port=int(os.getenv("EVALUATOR_PORT", "8090")), reload=False)


if __name__ == "__main__":
    main()
