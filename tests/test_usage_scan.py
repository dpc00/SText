import json
import os
import tempfile
import unittest

from ai.terminal.usage_scan import (
    humanize_epoch,
    parse_codex_rate_limits,
    provider_for_profile,
    scan_codex_usage,
    scan_local_usage,
)


_CODEX_LINE = json.dumps({
    "timestamp": "2026-07-31T21:21:21.722Z",
    "type": "event_msg",
    "payload": {
        "type": "token_count",
        "info": {"total_token_usage": {"total_tokens": 18889}},
        "rate_limits": {
            "limit_id": "codex",
            "primary": {
                "used_percent": 35.0,
                "window_minutes": 10080,
                "resets_at": 1786099466,
            },
            "credits": {"has_credits": False},
            "plan_type": "plus",
        },
    },
})


class ProviderDetectionTests(unittest.TestCase):
    def test_plain_cli(self):
        self.assertEqual(
            provider_for_profile({"launch_command": ["codex"]}), "codex"
        )

    def test_windows_shim_extension_stripped(self):
        self.assertEqual(
            provider_for_profile({"launch_command": ["gemini.cmd"]}), "gemini"
        )

    def test_wrapper_wins_over_wrapped_cli(self):
        # "ollama launch codex" bills the Ollama account, not Codex.
        self.assertEqual(
            provider_for_profile(
                {"launch_command": ["ollama", "launch", "codex"]}
            ),
            "ollama",
        )

    def test_shell_profile_has_no_provider(self):
        self.assertIsNone(provider_for_profile({"launch_command": ["cmd.exe"]}))

    def test_absolute_path_resolves_by_basename(self):
        self.assertEqual(
            provider_for_profile(
                {"launch_command": ["C:\\Users\\donal\\.local\\bin\\claude.exe"]}
            ),
            "claude",
        )


class CodexParseTests(unittest.TestCase):
    def test_real_shape_snapshot(self):
        remaining, resets_at = parse_codex_rate_limits(_CODEX_LINE)
        self.assertEqual(remaining, 65.0)
        self.assertEqual(resets_at, 1786099466)

    def test_irrelevant_line_is_ignored(self):
        self.assertIsNone(parse_codex_rate_limits('{"type":"event_msg"}'))
        self.assertIsNone(parse_codex_rate_limits("not json rate_limits"))

    def test_scan_reads_newest_rollout_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = os.path.join(tmp, "sessions", "2026", "07", "31")
            os.makedirs(day)
            path = os.path.join(day, "rollout-x.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"type":"other"}\n')
                handle.write(_CODEX_LINE + "\n")
            result = scan_codex_usage(tmp, now=1786099466 - 3 * 86400)
            self.assertEqual(result["remaining"], 65.0)
            self.assertEqual(result["reset"], "in 3d 0h")

    def test_scan_empty_home_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(scan_codex_usage(tmp))

    def test_scan_local_usage_aggregates_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = os.path.join(tmp, ".codex", "sessions", "2026", "07", "31")
            os.makedirs(day)
            with open(os.path.join(day, "r.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(_CODEX_LINE + "\n")
            results = scan_local_usage(home=tmp)
            self.assertIn("codex", results)
            self.assertEqual(results["codex"]["remaining"], 65.0)


class HumanizeTests(unittest.TestCase):
    def test_minutes(self):
        self.assertEqual(humanize_epoch(1000 + 25 * 60, now=1000), "in 25m")

    def test_hours(self):
        self.assertEqual(humanize_epoch(1000 + 3 * 3600 + 120, now=1000), "in 3h 2m")

    def test_past_is_now(self):
        self.assertEqual(humanize_epoch(500, now=1000), "now")

    def test_garbage_is_none(self):
        self.assertIsNone(humanize_epoch("soon", now=1000))


if __name__ == "__main__":
    unittest.main()
