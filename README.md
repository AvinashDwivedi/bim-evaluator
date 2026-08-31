# Independent BIM Evaluator

This examiner is deliberately separate from `bim-multiagent`. It never imports application code,
opens Neo4j, or reads the application's internal artifacts. It treats the application as a black box
and invokes only its public CLI.

Each evaluation record must contain `client_id`, `project_id`, `question`, and `answer`. The examiner
passes both scope identifiers to the application CLI for that case; evaluator execution never relies on the
application's `.env` scope.

```powershell
python -m pip install -r requirements.txt
python bim_evaluator.py eval-cases.json `
  --sut-root ..\bim-multiagent `
  --output eval-report.json `
  --timeout 600
```

Use `--judge-mode exact` to avoid the semantic grader model.

## Web UI and live evaluation

Start `python -m bim_agents.webapp` in the BIM repository, then run `python -m server` here. Open
`http://127.0.0.1:8090` for chat or `http://127.0.0.1:8090/evaluation` for all cases, live responses,
individual verdicts, and the aggregate verdict summary.

Every comparison is binary: the independent judge returns `correct` or `incorrect` with a concise
reason. Reports and the dashboard do not assign numeric answer scores or use a passing threshold.

The web evaluator calls only the system's public HTTP API and remains a separate process and codebase.
Its default backend is `http://127.0.0.1:8000`; override it with `BIM_BACKEND_URL`. Dataset and report
paths can be changed with `BIM_EVALUATION_CASES` and `BIM_EVALUATION_REPORT`.

Evaluation progress is persisted after every case in `eval-run-state.json`. The dashboard restores the
last completed, stopped, failed, or active run after refresh. A run continues if the browser closes;
if the evaluator process itself exits mid-run, that run is marked failed on restart. Every BIM request
has a default ten-minute timeout. Override it with `BIM_EVALUATION_CASE_TIMEOUT_SECONDS`.
Transient connection failures and HTTP 408/409/429/5xx responses are retried up to four times by default;
override this with `EVAL_HTTP_MAX_ATTEMPTS`.

Select any subset of questions from the Evaluation page before starting. Subset results retain their
original dataset question numbers. The latest completed report remains in `eval-report.json`, while every
run receives an independent snapshot in `eval-reports/`, including partial stopped or interrupted runs.
Override the archive directory with `BIM_EVALUATION_REPORTS_DIR`. Browse saved run metadata at
`GET /api/evaluation/reports` or retrieve one report at `GET /api/evaluation/reports/{run_id}`.
Each case retains the system's limitations, failure categories, stages, artifact IDs, semantic checks,
and ordered investigation trace. The dashboard exposes these under “Investigation evidence,” so a generic
fallback can be diagnosed without access to the BIM service's terminal output.

## Cost tracking

The dashboard shows an estimated USD cost for every question, the current evaluation run, and the current
browser-local day. A question total combines the BIM backend's reported multi-call cost with the independent
semantic judge call. Exact judging and normalized exact matches add no judge API cost. Standalone questions
asked on the Chat page are written to `cost-ledger.jsonl`; evaluation runs remain in `eval-reports/`. The daily
total combines both sources and uses browser-supplied UTC boundaries so local calendar days and daylight-saving
changes are handled correctly.

Costs are estimates from API token usage, not invoices. A partial marker means usage, model pricing, or a
tool-specific fee was unavailable. The bundled GPT-5.6 Sol text-token prices are dated 2026-08-29 and can be
overridden with `BIM_EVALUATOR_PRICING_JSON`. Move the standalone ledger with `BIM_COST_LEDGER`. Daily totals are
also available from `GET /api/evaluation/costs/daily?start=<ISO-8601>&end=<ISO-8601>`.

For semantic grading, copy `.env.example` to `.env` inside this evaluator project and provide the
evaluator's own `OPENAI_API_KEY`. The system-under-test keeps its separate configuration. Child CLI
output is forced to UTF-8 so Hebrew and BIM units such as `m²` decode consistently on Windows.
