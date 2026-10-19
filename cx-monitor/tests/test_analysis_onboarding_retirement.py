"""Regression tests for retiring the redundant onboarding feed and name-based pilot."""
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch


def load_analysis():
    package = types.ModuleType("_retirement_analysis")
    package.__path__ = []
    store = types.ModuleType("_retirement_analysis.store")
    store.query = Mock(return_value=[])
    store.connect = Mock(side_effect=AssertionError("Unexpected database write"))
    delivery = types.ModuleType("_retirement_analysis.slack_delivery")
    core = types.ModuleType("_retirement_analysis.core")
    core.scrub = lambda value: value
    core.candidate = lambda value: []
    dashboard = types.ModuleType("_retirement_analysis.dashboard")
    dashboard.sync_once = Mock(side_effect=AssertionError("Retired feed was polled"))
    dashboard.bundle_context = Mock(side_effect=AssertionError("Retired SLA was evaluated"))
    pilot = types.ModuleType("_retirement_analysis.case_pilot")
    pilot.send_once = Mock(side_effect=AssertionError("Retired pilot was sent"))
    psycopg = types.ModuleType("psycopg")
    psycopg_types = types.ModuleType("psycopg.types")
    psycopg_json = types.ModuleType("psycopg.types.json")
    psycopg_json.Jsonb = lambda value: value
    modules = {package.__name__: package, store.__name__: store,
        delivery.__name__: delivery, core.__name__: core,
        dashboard.__name__: dashboard, pilot.__name__: pilot,
        "psycopg": psycopg, "psycopg.types": psycopg_types,
        "psycopg.types.json": psycopg_json}
    spec = importlib.util.spec_from_file_location("_retirement_analysis.analysis",
        Path(__file__).resolve().parents[1] / "monitor" / "analysis.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module, store, dashboard, pilot


class StopAfterFirstCycle:
    def __init__(self):
        self.stopped = False
        self.delays = []
    def is_set(self):
        return self.stopped
    def wait(self, seconds):
        self.delays.append(seconds)
        self.stopped = True
        return True


class RetirementTests(unittest.TestCase):
    def test_source_sync_and_triage_continue_without_legacy_poll_or_send(self):
        analysis, _, dashboard, pilot = load_analysis()
        stop = StopAfterFirstCycle()
        with patch.object(analysis, "sync_sources") as sync, patch.object(analysis, "generate_once") as generate:
            analysis.run(stop)
        sync.assert_called_once_with(stop)
        generate.assert_called_once()
        dashboard.sync_once.assert_not_called()
        pilot.send_once.assert_not_called()
        self.assertEqual(stop.delays, [60])

    def test_bundle_excludes_legacy_sla_and_marks_official_source(self):
        analysis, store, dashboard, pilot = load_analysis()
        with patch.dict(os.environ, {"SLACK_MONITOR_SOURCE_CHANNELS": ""}):
            result = analysis.build_bundle(datetime(2026, 10, 19, 10, tzinfo=timezone.utc))
        self.assertEqual(result["version"], 4)
        self.assertFalse(result["published"])
        self.assertTrue(result["requires_review"])
        self.assertFalse(result["dashboard"]["loaded"])
        self.assertEqual(result["dashboard"]["status"], "retired_legacy_onboarding_context")
        self.assertEqual(result["dashboard"]["sla_findings"], [])
        self.assertEqual(result["sla_rules"]["source"], "official_dashboard_only")
        self.assertFalse(result["sla_rules"]["calculated_here"])
        dashboard.bundle_context.assert_not_called()
        pilot.send_once.assert_not_called()
        store.connect.assert_not_called()

    def test_retirement_does_not_remove_capture_or_source_limits(self):
        analysis, _, _, _ = load_analysis()
        self.assertIn("C0BNDFL2PC7", analysis.SOURCES)
        self.assertTrue(any("Áudios" in limit for limit in analysis.LIMITS))
        self.assertTrue(any("dashboard oficial" in limit for limit in analysis.LIMITS))

if __name__ == "__main__":
    unittest.main()
