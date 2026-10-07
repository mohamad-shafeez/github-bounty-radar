import unittest
from radar import detect_bounty, find_amounts


class RadarTests(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(find_amounts("Bounty: $175")[0]["amount"], 175)
        self.assertEqual(find_amounts("USD 1.5k")[0]["amount"], 1500)

    def test_confirmed_bounty(self):
        bounty, platforms = detect_bounty("[$175] Fix login", "", [])
        self.assertEqual(bounty["status"], "confirmed")
        self.assertEqual(bounty["amount"], 175)

    def test_label_bounty_is_uncertain_without_amount(self):
        bounty, _ = detect_bounty("Fix thing", "", ["💎 Bounty"])
        self.assertEqual(bounty["status"], "uncertain")


if __name__ == "__main__":
    unittest.main()
