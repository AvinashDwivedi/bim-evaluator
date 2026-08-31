import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from bim_evaluator import EvaluationCase, EvaluationJudgment, evaluate_cases, load_cases, run_system_http

CLIENT_ID = "653fbe80-e4c5-11ed-95e8-fdb8a484b2c4"
PROJECT_ID = "858ef0f0-454a-11f1-8957-1fe1b101e373"


def evaluation_case(question, answer):
    return EvaluationCase(client_id=CLIENT_ID, project_id=PROJECT_ID,
                          question=question, answer=answer)


class EvaluatorTests(unittest.IsolatedAsyncioTestCase):
    def test_loads_supported_json_shapes(self):
        record = {"client_id": CLIENT_ID, "project_id": PROJECT_ID,
                  "question": "Q1", "answer": "A1"}
        for payload in (record, [record], {"cases": [record]}):
            with patch.object(Path, "read_text", return_value=json.dumps(payload)):
                self.assertEqual(load_cases(Path("cases.json"))[0].question, "Q1")

    def test_scope_identifiers_do_not_need_to_be_uuids(self):
        record = {"client_id": "client-alias", "project_id": "project-alias",
                  "question": "Q1", "answer": "A1"}
        with patch.object(Path, "read_text", return_value=json.dumps([record])):
            case = load_cases(Path("cases.json"))[0]
        self.assertEqual(case.client_id, "client-alias")
        self.assertEqual(case.project_id, "project-alias")

    def test_optional_evaluation_notes_are_loaded(self):
        record = {
            "client_id": CLIENT_ID,
            "project_id": PROJECT_ID,
            "question": "Which types?",
            "answer": "Five type values.",
            "evaluation_notes": "Four families contain five distinct family/type pairs.",
        }
        with patch.object(Path, "read_text", return_value=json.dumps([record])):
            case = load_cases(Path("cases.json"))[0]
        self.assertIn("five distinct", case.evaluation_notes.casefold())

    async def test_exact_evaluation(self):
        async def answerer(_question):
            return SimpleNamespace(answer="Five apartments.", verification_status="verified")
        report = await evaluate_cases(
            [evaluation_case("How many?", " five   apartments. ")],
            judge_mode="exact", answerer=answerer)
        self.assertEqual(report.summary.correct, 1)
        self.assertEqual(report.summary.incorrect, 0)
        self.assertNotIn("score", report.model_dump_json())

    async def test_injected_semantic_judge(self):
        async def answerer(_question):
            return SimpleNamespace(answer="There are five.", verification_status="verified")
        async def judge(*_args):
            return EvaluationJudgment(correct=True, reason="Equivalent.")
        report = await evaluate_cases([evaluation_case("Q", "5")],
                                      answerer=answerer, judge=judge)
        self.assertTrue(report.results[0].correct)

    async def test_question_and_evaluation_costs_combine_system_and_judge(self):
        async def answerer(_question):
            return SimpleNamespace(
                answer="There are five.", verification_status="verified",
                cost={
                    "currency": "USD", "status": "calculated",
                    "estimated_cost_usd": 0.012, "is_complete": True,
                    "api_requests": 2,
                    "tokens": {"input_tokens": 1000, "output_tokens": 100,
                               "total_tokens": 1100},
                },
            )

        async def judge(*_args):
            return EvaluationJudgment(correct=True, reason="Equivalent."), {
                "currency": "USD", "status": "calculated",
                "estimated_cost_usd": 0.003, "is_complete": True,
                "api_requests": 1,
                "tokens": {"input_tokens": 500, "output_tokens": 50,
                           "total_tokens": 550},
            }

        report = await evaluate_cases(
            [evaluation_case("Q", "5")], answerer=answerer, judge=judge,
        )

        self.assertEqual(report.results[0].cost["estimated_cost_usd"], 0.015)
        self.assertEqual(report.results[0].cost["system_estimated_cost_usd"], 0.012)
        self.assertEqual(report.results[0].cost["judge_estimated_cost_usd"], 0.003)
        self.assertEqual(report.summary.cost["estimated_cost_usd"], 0.015)
        self.assertTrue(report.summary.cost["is_complete"])

    async def test_missing_system_usage_is_marked_as_partial_cost(self):
        async def answerer(_question):
            return SimpleNamespace(answer="A", verification_status="verified")

        report = await evaluate_cases(
            [evaluation_case("Q", "A")], judge_mode="exact", answerer=answerer,
        )

        self.assertEqual(report.results[0].cost["status"], "partial")
        self.assertFalse(report.results[0].cost["is_complete"])

    async def test_errors_do_not_expose_details(self):
        async def answerer(_question):
            raise RuntimeError("password=secret")
        report = await evaluate_cases([evaluation_case("Q", "A")],
                                      judge_mode="exact", answerer=answerer)
        self.assertNotIn("secret", report.model_dump_json())

    async def test_subset_evaluation_preserves_original_question_indices(self):
        events = []

        async def answerer(_question):
            return SimpleNamespace(answer="A", verification_status="verified")

        async def event_sink(event):
            events.append(event)

        report = await evaluate_cases(
            [evaluation_case("Q13", "A"), evaluation_case("Q15", "A")],
            judge_mode="exact", answerer=answerer, event_sink=event_sink,
            case_indices=[13, 15],
        )
        self.assertEqual([item.index for item in report.results], [13, 15])
        started = [event for event in events if event["type"] == "case_started"]
        self.assertEqual(
            [(event["index"], event["position"]) for event in started],
            [(13, 1), (15, 2)],
        )

    async def test_subset_index_count_must_match_cases(self):
        async def answerer(_question):
            return SimpleNamespace(answer="A", verification_status="verified")

        with self.assertRaisesRegex(ValueError, "one original index"):
            await evaluate_cases(
                [evaluation_case("Q", "A")], judge_mode="exact",
                answerer=answerer, case_indices=[1, 2],
            )

    async def test_pipeline_trace_is_retained_in_case_result(self):
        async def answerer(_question):
            return SimpleNamespace(
                answer="A", verification_status="insufficient_evidence",
                limitations=["Missing exact classification."],
                stages_used=["Graph Inspector"], artifact_ids=["graph-discovery"],
                investigation_trace=["Graph Inspector: profiled live labels."],
                failure_categories=["agent_turn_limit"], semantic_checks=[],
            )

        report = await evaluate_cases(
            [evaluation_case("Q", "A")], judge_mode="exact", answerer=answerer,
        )

        result = report.results[0]
        self.assertEqual(result.investigation_trace, ["Graph Inspector: profiled live labels."])
        self.assertEqual(result.failure_categories, ["agent_turn_limit"])
        self.assertEqual(result.limitations, ["Missing exact classification."])

    async def test_http_answerer_retries_retryable_upstream_failure(self):
        request = httpx.Request("POST", "http://sut/api/chat")
        success = {
            "answer": "A", "verification_status": "verified", "limitations": [],
            "stages_used": [], "artifact_ids": [], "investigation_trace": [],
            "failure_categories": [], "semantic_checks": [],
        }

        class FakeClient:
            attempts = 0

            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, _url, **_kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    return httpx.Response(503, request=request, json={"detail": "temporary"})
                return httpx.Response(200, request=request, json=success)

        fake = FakeClient()
        with (
            patch("bim_evaluator.httpx.AsyncClient", return_value=fake),
            patch("bim_evaluator.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            answer = await run_system_http(evaluation_case("Q", "A"), base_url="http://sut")

        self.assertEqual(answer.answer, "A")
        self.assertEqual(fake.attempts, 2)
        sleep.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
