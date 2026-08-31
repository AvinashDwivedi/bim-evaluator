import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import server


class EvaluatorStateTests(unittest.TestCase):
    def setUp(self):
        self.state_path = Path(__file__).with_name(".state-test.json")
        self.state_patch = patch.object(server, "STATE", self.state_path)
        self.state_patch.start()
        self.reports_path = Path(__file__).with_name(".reports-test")
        self.reports_patch = patch.object(server, "REPORTS", self.reports_path)
        self.reports_patch.start()
        self.ledger_path = Path(__file__).with_name(".cost-ledger-test.jsonl")
        self.ledger_patch = patch.object(server, "COST_LEDGER", self.ledger_path)
        self.ledger_patch.start()
        server.run_state = server.empty_state()
        server.run_task = None

    def tearDown(self):
        self.state_patch.stop()
        self.reports_patch.stop()
        self.ledger_patch.stop()
        self.state_path.unlink(missing_ok=True)
        self.state_path.with_suffix(".json.tmp").unlink(missing_ok=True)
        self.ledger_path.unlink(missing_ok=True)
        report = self.reports_path / "eval-report-d9e02862-f664-4cf8-b22a-a5318512c17d.json"
        report.unlink(missing_ok=True)
        self.reports_path.rmdir() if self.reports_path.exists() else None

    def test_state_endpoint_preserves_saved_status(self):
        server.run_state.update({"status": "stopped", "results": [{"correct": True}]})
        server.persist_state()
        with TestClient(server.app) as client:
            value = client.get("/api/evaluation/state").json()
        self.assertEqual(value["status"], "stopped")
        self.assertEqual(len(value["results"]), 1)

    def test_each_case_timeout_is_ten_minutes(self):
        self.assertEqual(server.CASE_TIMEOUT_SECONDS, 600.0)

    def test_selected_cases_preserve_dataset_indices(self):
        all_cases = server.load_cases(server.CASES)
        requested = [1, len(all_cases)]
        selected, indices, dataset_total = server.selected_cases(
            server.EvaluationOptions(question_indices=requested)
        )
        self.assertEqual(indices, requested)
        self.assertEqual(dataset_total, len(all_cases))
        self.assertEqual(selected[0].question, all_cases[0].question)
        self.assertEqual(selected[1].question, all_cases[-1].question)

    def test_load_state_resets_selection_when_case_file_changes(self):
        server.persist_state()
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        saved.update({
            "status": "completed", "dataset_fingerprint": "stale",
            "dataset_total": 21, "total": 21,
            "selected_question_indices": list(range(1, 22)),
        })
        self.state_path.write_text(json.dumps(saved), encoding="utf-8")

        restored = server.load_state()

        self.assertEqual(restored["status"], "idle")
        self.assertEqual(restored["dataset_total"], len(server.load_cases(server.CASES)))
        self.assertIsNone(restored["selected_question_indices"])

    def test_each_run_has_an_independent_report_snapshot(self):
        server.run_state = {
            **server.empty_state(),
            "run_id": "d9e02862-f664-4cf8-b22a-a5318512c17d",
            "status": "stopped",
            "selected_question_indices": [13, 15],
            "results": [{"index": 13, "correct": False}],
        }
        server.persist_run_report()
        path = self.reports_path / "eval-report-d9e02862-f664-4cf8-b22a-a5318512c17d.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(value["run_id"], server.run_state["run_id"])
        self.assertEqual(value["selected_question_indices"], [13, 15])
        self.assertEqual(value["results"][0]["index"], 13)

    def test_legacy_score_report_is_normalized_to_binary_verdicts(self):
        value = server.normalize_report({
            "pass_threshold": 0.8,
            "results": [{"passed": True, "score": 0.91, "correctness": 0.9}],
            "summary": {"passed": 1, "failed": 0, "average_score": 0.91, "pass_rate": 1},
            "options": {"pass_threshold": 0.8},
        })
        self.assertEqual(value["results"], [{"correct": True}])
        self.assertEqual(value["summary"], {"correct": 1, "incorrect": 0})
        self.assertEqual(value["options"], {})
        self.assertNotIn("pass_threshold", value)

    def test_daily_cost_endpoint_totals_runs_in_requested_local_day(self):
        server.run_state = {
            **server.empty_state(),
            "run_id": "d9e02862-f664-4cf8-b22a-a5318512c17d",
            "status": "completed",
            "started_at": "2026-08-29T04:30:00+00:00",
            "finished_at": "2026-08-29T04:35:00+00:00",
            "results": [{
                "index": 1, "correct": True,
                "cost": {"estimated_cost_usd": 0.025, "is_complete": True,
                         "status": "calculated", "api_requests": 3, "tokens": {}},
            }],
            "summary": None,
        }
        server.persist_run_report()
        server.append_cost_entry({
            "recorded_at": "2026-08-29T10:00:00+05:30", "kind": "chat",
            "cost": {"estimated_cost_usd": 0.005, "is_complete": True,
                     "status": "calculated", "api_requests": 1, "tokens": {}},
        })

        with TestClient(server.app) as client:
            value = client.get("/api/evaluation/costs/daily", params={
                "start": "2026-08-29T00:00:00+05:30",
                "end": "2026-08-30T00:00:00+05:30",
            }).json()

        self.assertEqual(value["estimated_cost_usd"], 0.03)
        self.assertEqual(value["runs"], 1)
        self.assertEqual(value["questions"], 2)
        self.assertEqual(value["evaluation_questions"], 1)
        self.assertEqual(value["chat_questions"], 1)


if __name__ == "__main__":
    unittest.main()
