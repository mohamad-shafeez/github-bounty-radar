import unittest
from datetime import datetime, timezone

import radar


class BountyGateRegressionTests(unittest.TestCase):

    def make_config(self):
        return {
            "alert_new": "bounty",
            "alert_bounty": "always",
            "alert_opportunity": "always",
            "alert_unassigned": "always",
            "alert_possible_bounty": False,
            "alert_comments_surge": False,
            "alert_any_new_comment": False,
            "opportunity_labels": ["External", "Help Wanted"],
            "high_requires_opportunity_label": False,
            "assignee_matters": False,
            "weight": 1.0,
        }

    def make_snapshot(self, status, amount, labels):
        return {
            "repo": "test/test",
            "number": 999999,
            "title": "Regression test bounty issue",
            "url": "https://github.com/test/test/issues/999999",
            "html_url": "https://github.com/test/test/issues/999999",
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z",
            "comments": 0,
            "assignee": None,
            "assignees": [],
            "labels": labels,
            "bounty": {"status": status, "amount": amount, "currency": "USD"},
            "tech": False,
        }

    def decide(self, status, amount, labels):
        return radar.decide(
            self.make_snapshot(status, amount, labels),
            None,
            self.make_config(),
            False,
            datetime(2026, 10, 4, tzinfo=timezone.utc),
        )

    def test_external_without_bounty_is_not_actionable(self):
        decision = self.decide("none", None, ["External", "Weekly"])
        self.assertEqual(decision.get("events", []), [])

    def test_bounty_below_50_is_not_actionable(self):
        decision = self.decide("confirmed", 49, ["External"])
        self.assertEqual(decision.get("events", []), [])

    def test_bounty_exactly_50_is_actionable(self):
        decision = self.decide("confirmed", 50, ["External"])
        self.assertTrue(decision.get("events", []))

    def test_bounty_above_50_is_actionable(self):
        decision = self.decide("confirmed", 175, ["External"])
        self.assertTrue(decision.get("events", []))

    def test_unconfirmed_bounty_is_not_actionable(self):
        decision = self.decide("possible", 175, ["External"])
        self.assertEqual(decision.get("events", []), [])


if __name__ == "__main__":
    unittest.main()
