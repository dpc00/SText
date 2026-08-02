import os
import tempfile
import unittest

from ai.terminal.profile_availability import (
    command_exists,
    menu_caption,
    profile_is_available,
    reset_update_from_text,
    usage_update_from_text,
)


class ProfileAvailabilityTests(unittest.TestCase):
    def test_existing_absolute_command_enables_any_configured_profile(self):
        profile = {"launch_command": [os.path.abspath(__file__)]}
        self.assertTrue(profile_is_available("Kimi", profile))

    def test_missing_command_disables_profile(self):
        profile = {"launch_command": ["definitely-not-an-installed-command-xyz"]}
        self.assertFalse(profile_is_available("Missing", profile, path=""))

    def test_relative_path_command_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = os.path.join(tmp, "agent.exe")
            with open(existing, "wb"):
                pass
            self.assertTrue(command_exists([existing]))
            self.assertFalse(command_exists([os.path.join(tmp, "absent.exe")]))

    def test_confirmed_exhaustion_disables_profile(self):
        self.assertEqual(
            usage_update_from_text("You have exhausted your daily quota on this model."),
            0.0,
        )

    def test_provider_quota_wording_is_detected(self):
        self.assertEqual(
            usage_update_from_text("You exceeded your current quota."),
            0.0,
        )

    def test_remaining_percentage_is_extracted(self):
        self.assertEqual(usage_update_from_text("Weekly usage: 64% remaining"), 64.0)

    def test_transient_rate_limit_does_not_mean_exhausted(self):
        self.assertIsNone(
            usage_update_from_text("Rate limit exceeded. Please wait a moment."),
        )

    def test_latest_usage_signal_wins(self):
        text = "You have no credits remaining.\nWeekly usage: 73% remaining"
        self.assertEqual(usage_update_from_text(text), 73.0)

    def test_percentage_used_is_treated_as_trustworthy(self):
        # Kimi CLI's own weekly-limit screen: "Weekly limit reached ... Weekly
        # usage 100% used ... Resets in 23 hours." Unlike a generic transient
        # rate-limit phrase, an explicit "N% used" figure is a measurement,
        # not a guess about whether the quota is actually empty.
        text = "Weekly limit reached\n\nWeekly usage\n\n100% used\n\nResets in 23 hours."
        self.assertEqual(usage_update_from_text(text), 0.0)
        self.assertEqual(reset_update_from_text(text), "23 hours")

    def test_reset_duration_is_extracted(self):
        self.assertEqual(reset_update_from_text("Usage resets in 3h 42m"), "3h 42m")

    def test_reset_timestamp_is_extracted(self):
        self.assertEqual(
            reset_update_from_text("Next reset: Aug 5, 2026 at 02:00 UTC"),
            "Aug 5, 2026 at 02:00 UTC",
        )

    def test_ansi_does_not_hide_usage_details(self):
        text = "\x1b[32m64% remaining\x1b[0m | resets in 2 days"
        self.assertEqual(usage_update_from_text(text), 64.0)
        self.assertEqual(reset_update_from_text(text), "2 days")

    def test_menu_caption_plain_when_nothing_observed(self):
        self.assertEqual(menu_caption("Claude"), "Claude")

    def test_menu_caption_shows_remaining_and_reset(self):
        self.assertEqual(
            menu_caption("Claude", remaining=64.0, reset="3h 42m"),
            "Claude — 64% left, resets 3h 42m",
        )

    def test_menu_caption_exhausted_with_reset(self):
        self.assertEqual(
            menu_caption("Gemini", remaining=0.0, reset="Aug 5, 02:00 UTC"),
            "Gemini — quota exhausted, resets Aug 5, 02:00 UTC",
        )

    def test_menu_caption_reset_only(self):
        self.assertEqual(
            menu_caption("Codex", reset="2 days"),
            "Codex — resets 2 days",
        )

    def test_menu_caption_missing_executable(self):
        self.assertEqual(
            menu_caption("Vibe", executable_ok=False),
            "Vibe — not installed",
        )


if __name__ == "__main__":
    unittest.main()
