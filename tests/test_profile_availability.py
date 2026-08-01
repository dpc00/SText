import os
import tempfile
import unittest

from ai.terminal.profile_availability import command_exists, profile_is_available


class ProfileAvailabilityTests(unittest.TestCase):
    def test_allowlist_disables_installed_profile(self):
        profile = {"launch_command": [os.path.abspath(__file__)]}
        self.assertFalse(profile_is_available("Claude", profile, ["JCode"]))

    def test_allowlist_and_existing_absolute_command_enable_profile(self):
        profile = {"launch_command": [os.path.abspath(__file__)]}
        self.assertTrue(profile_is_available("Claude", profile, ["Claude"]))

    def test_missing_command_disables_profile(self):
        profile = {"launch_command": ["definitely-not-an-installed-command-xyz"]}
        self.assertFalse(profile_is_available("Missing", profile, ["Missing"], path=""))

    def test_relative_path_command_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = os.path.join(tmp, "agent.exe")
            with open(existing, "wb"):
                pass
            self.assertTrue(command_exists([existing]))
            self.assertFalse(command_exists([os.path.join(tmp, "absent.exe")]))


if __name__ == "__main__":
    unittest.main()
