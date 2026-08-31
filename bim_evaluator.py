from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable, Literal

import httpx
from uuid import uuid4

from openai import OpenAI
from pydantic import BaseModel, Field, TypeAdapter
from dotenv import load_dotenv

from cost_tracking import combine_costs, response_cost, summarize_costs, zero_cost


load_dotenv(Path(__file__).with_name(".env"))


class EvaluationCase(BaseModel):
    client_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    evaluation_notes: str = ""


class EvaluationJudgment(BaseModel):
    correct: bool
    reason: str


class CaseResult(BaseModel):
    index: int
    question: str
    expected_answer: str
    actual_answer: str = ""
    pipeline_status: str = "error"
    elapsed_seconds: float = 0
    correct: bool = False
    reason: str = ""
    error: str | None = None
    limitations: list[str] = Field(default_factory=list)
    stages_used: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    investigation_trace: list[str] = Field(default_factory=list)
    failure_categories: list[str] = Field(default_factory=list)
    semantic_checks: list[dict] = Field(default_factory=list)
    cost: dict = Field(default_factory=dict)


class EvaluationSummary(BaseModel):
    total: int
    correct: int
    incorrect: int
    errors: int
    average_elapsed_seconds: float
    cost: dict = Field(default_factory=dict)


class EvaluationReport(BaseModel):
    judge_mode: Literal["semantic", "exact"]
    judge_model: str | None = None
    summary: EvaluationSummary
    results: list[CaseResult]


class SystemAnswer(BaseModel):
    answer: str
    verification_status: str
    limitations: list[str] = Field(default_factory=list)
    stages_used: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    investigation_trace: list[str] = Field(default_factory=list)
    failure_categories: list[str] = Field(default_factory=list)
    semantic_checks: list[dict] = Field(default_factory=list)
    cost: dict = Field(default_factory=dict)


Answerer = Callable[[EvaluationCase], Awaitable[object]]
Judge = Callable[[EvaluationCase, str, str], Awaitable[EvaluationJudgment | tuple[EvaluationJudgment, dict]]]
EventSink = Callable[[dict], Awaitable[None]]


def load_cases(path: Path) -> list[EvaluationCase]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict) and set(payload) >= {"question", "answer"}:
        records = [payload]
    elif isinstance(payload, dict) and isinstance(payload.get("cases"), list):
        records = payload["cases"]
    elif isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and all(isinstance(value, str) for value in payload.values()):
        records = [{"question": question, "answer": answer} for question, answer in payload.items()]
    else:
        raise ValueError("Unsupported evaluation JSON shape.")
    cases = TypeAdapter(list[EvaluationCase]).validate_python(records)
    if not cases:
        raise ValueError("Evaluation dataset cannot be empty.")
    return cases


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


async def run_system_cli(case: EvaluationCase, *, sut_root: Path, python: str) -> SystemAnswer:
    """Call the system only through its public CLI; never import its implementation."""
    child_env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    def call_system():
        return subprocess.run(
            [python, "-m", "bim_agents.cli", case.question,
             "--client-id", str(case.client_id), "--project-id", str(case.project_id), "--quiet"],
            cwd=str(sut_root.resolve()), env=child_env, capture_output=True, check=False,
        )

    process = await asyncio.to_thread(call_system)
    if process.returncode:
        raise RuntimeError("System-under-test CLI failed.")
    payload = json.loads(process.stdout.decode("utf-8-sig"))
    return SystemAnswer.model_validate(payload)


async def run_system_http(
    case: EvaluationCase, *, base_url: str, timeout: float | None = None,
    evaluation_run_id: str | None = None, evaluation_case_index: int | None = None,
) -> SystemAnswer:
    """Call the system under test only through its public, project-scoped HTTP API."""
    payload = {
        "question": case.question,
        "client_id": str(case.client_id),
        "project_id": str(case.project_id),
        "request_id": str(uuid4()),
        "evaluation_run_id": evaluation_run_id,
        "evaluation_case_index": evaluation_case_index,
    }
    max_attempts = max(1, int(os.getenv("EVAL_HTTP_MAX_ATTEMPTS", "4")))
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.post(f"{base_url.rstrip('/')}/api/chat", json=payload)
                response.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {408, 409, 429, 500, 502, 503, 504} or attempt == max_attempts:
                    raise
            except httpx.TransportError:
                if attempt == max_attempts:
                    raise
            await asyncio.sleep(min(4.0, float(2 ** (attempt - 1))))
        if response is None:
            raise RuntimeError("System-under-test HTTP request produced no response.")
    return SystemAnswer.model_validate(response.json())


async def semantic_judge(case, actual_answer, pipeline_status, *, model):
    if not os.getenv("OPENAI_API_KEY") and not os.getenv("OPENAI_ADMIN_KEY"):
        raise RuntimeError(
            "The independent evaluator needs OPENAI_API_KEY in its own environment or .env file."
        )
    prompt = f"""Evaluate a BIM system answer against the reference answer.

Question: {case.question}
Reference answer: {case.answer}
Reference terminology/acceptance notes: {case.evaluation_notes or "None supplied."}
System answer: {actual_answer}
Pipeline status: {pipeline_status}

Judge semantic equivalence, not wording. Treat the acceptance notes as authoritative clarifications of
reference terminology, not as extra facts the system must repeat. Penalize wrong numbers, units, scope, measurement basis,
unsupported compliance conclusions, contradictions, and claims that confuse missing data with zero.
Return correct=true only when the system answer is semantically correct. Otherwise return
correct=false. Do not assign a numeric score. Keep the reason concise.
"""

    def call():
        response = OpenAI().responses.parse(
            model=model,
            input=[{"role": "developer", "content": "You are a strict independent BIM examiner."},
                   {"role": "user", "content": prompt}],
            text_format=EvaluationJudgment,
        )
        if response.output_parsed is None:
            raise RuntimeError("Semantic judge returned no structured result.")
        return response.output_parsed, response_cost(
            response, configured_model=model, component="judge",
        )

    return await asyncio.to_thread(call)


async def evaluate_cases(cases, *, judge_mode="semantic", judge_model=None,
                         timeout_seconds=None, answerer=None, judge=None,
                         event_sink: EventSink | None = None,
                         case_indices: list[int] | None = None):
    if answerer is None:
        raise ValueError("An external system answerer is required.")
    if case_indices is not None and len(case_indices) != len(cases):
        raise ValueError("case_indices must contain one original index per evaluation case.")
    model = judge_model or os.getenv("BIM_EVALUATOR_MODEL", "gpt-5.6-sol")
    results = []
    indices = case_indices or list(range(1, len(cases) + 1))
    for position, (index, case) in enumerate(zip(indices, cases), 1):
        if event_sink:
            await event_sink({
                "type": "case_started", "index": index, "position": position,
                "total": len(cases),
            })
        started = time.monotonic()
        result = CaseResult(index=index, question=case.question, expected_answer=case.answer)
        try:
            report = await answerer(case) if timeout_seconds is None else await asyncio.wait_for(
                answerer(case), timeout_seconds)
            result.elapsed_seconds = round(time.monotonic() - started, 3)
            result.actual_answer = str(report.answer)
            result.pipeline_status = str(report.verification_status)
            for field in (
                "limitations", "stages_used", "artifact_ids", "investigation_trace",
                "failure_categories", "semantic_checks",
            ):
                setattr(result, field, list(getattr(report, field, []) or []))
            exact_match = _normalized(result.actual_answer) == _normalized(case.answer)
            if judge_mode == "exact" or exact_match:
                judgment = EvaluationJudgment(
                    correct=exact_match,
                    reason="Normalized exact match." if exact_match
                    else "Answers do not exactly match.")
                judge_cost = zero_cost(component="judge")
            else:
                judge_fn = judge or (lambda c, a, s: semantic_judge(c, a, s, model=model))
                judged = await judge_fn(case, result.actual_answer, result.pipeline_status)
                if isinstance(judged, tuple):
                    judgment, judge_cost = judged
                else:
                    judgment, judge_cost = judged, zero_cost(component="judge")
            for field in ("correct", "reason"):
                setattr(result, field, getattr(judgment, field))
            result.cost = combine_costs(getattr(report, "cost", None), judge_cost)
        except Exception as exc:
            result.elapsed_seconds = round(time.monotonic() - started, 3)
            result.error = type(exc).__name__
            result.reason = "Evaluation case could not complete."
        results.append(result)
        if event_sink:
            await event_sink({"type": "case_result", "result": result.model_dump(mode="json")})
    total = len(results)
    correct = sum(item.correct for item in results)
    errors = sum(item.error is not None for item in results)
    summary = EvaluationSummary(
        total=total, correct=correct, incorrect=total-correct, errors=errors,
        average_elapsed_seconds=round(sum(item.elapsed_seconds for item in results)/total, 3),
        cost=summarize_costs([item.cost or None for item in results]))
    report = EvaluationReport(judge_mode=judge_mode,
                              judge_model=model if judge_mode == "semantic" else None,
                              summary=summary, results=results)
    if event_sink:
        await event_sink({"type": "evaluation_complete", "report": report.model_dump(mode="json")})
    return report


def main():
    parser = argparse.ArgumentParser(description="Independent black-box BIM evaluator.")
    parser.add_argument("input", type=Path)
    parser.add_argument("--sut-root", type=Path, required=True,
                        help="Root of the BIM system under test; invoked only through its CLI.")
    parser.add_argument("--sut-python", default=sys.executable)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--judge-mode", choices=["semantic", "exact"], default="semantic")
    parser.add_argument("--judge-model")
    parser.add_argument("--timeout", type=float)
    args = parser.parse_args()
    answerer = lambda case: run_system_cli(
        case, sut_root=args.sut_root, python=args.sut_python)
    report = asyncio.run(evaluate_cases(
        load_cases(args.input), judge_mode=args.judge_mode, judge_model=args.judge_model,
        timeout_seconds=args.timeout, answerer=answerer))
    rendered = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
