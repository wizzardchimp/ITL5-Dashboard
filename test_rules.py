import unittest

from itl5_dashboard.rules import (
    action_for,
    availability_percent,
    classify,
    next_incident_state,
    priority_for,
    sla_breached,
    synthetic_probe,
    validate_service_url,
)


class ClassifyTest(unittest.TestCase):
    def test_healthy_and_boundaries(self):
        self.assertEqual(classify(200, 50, 300, None), ("informational", "available"))
        self.assertEqual(classify(204, 300, 300, None), ("informational", "available"))
        self.assertEqual(classify(299, 1, 300, None), ("informational", "available"))
        self.assertEqual(classify(200, 301, 300, None), ("warning", "degraded"))
        self.assertEqual(classify(200, None, 300, None), ("warning", "degraded"))

    def test_failures_are_exceptions(self):
        self.assertEqual(classify(199, 10, 300, None), ("exception", "unavailable"))
        self.assertEqual(classify(300, 10, 300, None), ("exception", "unavailable"))
        self.assertEqual(classify(503, 20, 300, None), ("exception", "unavailable"))
        self.assertEqual(classify(None, None, 300, "timed out"), ("exception", "unavailable"))
        self.assertEqual(classify(200, 10, 300, "timed out"), ("exception", "unavailable"))


class IncidentTest(unittest.TestCase):
    def test_opens_on_second_consecutive_exception_only(self):
        self.assertEqual(next_incident_state(0, False, "exception", 2), (1, None))
        self.assertEqual(next_incident_state(1, False, "exception", 2), (2, "open"))
        self.assertEqual(next_incident_state(2, True, "exception", 2), (3, None))

    def test_warning_resets_streak_and_does_not_resolve(self):
        self.assertEqual(next_incident_state(1, False, "warning", 2), (0, None))
        self.assertEqual(next_incident_state(2, True, "warning", 2), (0, None))

    def test_available_resolves(self):
        self.assertEqual(next_incident_state(2, True, "informational", 2), (0, "resolve"))
        self.assertEqual(next_incident_state(0, False, "informational", 2), (0, None))


class MeasurementTest(unittest.TestCase):
    def test_availability_counts_degraded_as_up(self):
        self.assertIsNone(availability_percent([]))
        self.assertEqual(availability_percent(["available", "unavailable"]), 50)
        self.assertEqual(availability_percent(["degraded"]), 100)

    def test_sla(self):
        self.assertFalse(sla_breached(100, 99.9, "available"))
        self.assertTrue(sla_breached(99.0, 99.9, "available"))
        self.assertTrue(sla_breached(100, 99.9, "degraded"))
        self.assertTrue(sla_breached(100, 99.9, "unavailable"))
        self.assertFalse(sla_breached(None, 99.9, "pending"))

    def test_labels(self):
        self.assertEqual(action_for("exception"), "required")
        self.assertEqual(action_for("warning"), "review")
        self.assertEqual(action_for("informational"), "none")
        self.assertEqual(priority_for("high"), "P1")
        self.assertEqual(priority_for("low"), "P3")
        self.assertEqual(priority_for("other"), "P2")


class SyntheticTest(unittest.TestCase):
    def test_cycle(self):
        self.assertEqual(synthetic_probe(0, 300), (200, 48.0, None))
        self.assertEqual(synthetic_probe(19.9, 300)[0], 200)
        status, latency, error = synthetic_probe(25, 300)
        self.assertEqual((status, error), (200, None))
        self.assertGreater(latency, 300)
        self.assertEqual(synthetic_probe(32, 300), (None, None, "connection refused"))
        self.assertEqual(synthetic_probe(80, 300), (200, 48.0, None))


class UrlTest(unittest.TestCase):
    def test_blocks_schemes_credentials_and_metadata(self):
        self.assertIn("http", validate_service_url("file:///etc/passwd"))
        self.assertIn("credentials", validate_service_url("http://user:pass@example.com/"))
        self.assertIn("blocked", validate_service_url("http://169.254.169.254/latest/meta-data").lower())
        self.assertIn("blocked", validate_service_url("http://[fe80::1]/").lower())
        self.assertIn("blocked", validate_service_url("http://[::ffff:169.254.169.254]/").lower())
        self.assertIn("blocked", validate_service_url("http://metadata.google.internal/").lower())
        self.assertIn(
            "blocked",
            validate_service_url("https://evil.example/x", resolver=lambda host: ["169.254.169.254"]).lower(),
        )

    def test_allows_loopback_and_public_names(self):
        self.assertIsNone(validate_service_url("http://127.0.0.1:8787/health"))
        self.assertIsNone(validate_service_url("https://example.com/health", resolver=lambda host: ["93.184.216.34"]))


if __name__ == "__main__":
    unittest.main()
