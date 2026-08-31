import unittest

from fastapi.testclient import TestClient

import server


class EvaluatorServerTests(unittest.TestCase):
    def test_pages_and_scoped_cases_are_served(self):
        with TestClient(server.app) as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/evaluation").status_code, 200)
            payload = client.get("/api/evaluation/cases").json()
        self.assertTrue(payload["cases"])
        self.assertIn("client_id", payload["cases"][0])
        self.assertIn("project_id", payload["cases"][0])


if __name__ == "__main__":
    unittest.main()
