import json
import os
import tempfile
import unittest

from ai.terminal.usage_scan import (
    _claude_token_expired,
    _persist_claude_oauth,
    _read_claude_oauth,
    humanize_epoch,
    parse_claude_oauth_usage,
    parse_codex_rate_limits,
    parse_codex_wham_usage,
    provider_for_profile,
    scan_codex_usage,
    scan_local_usage,
    summarize_windows,
    window_label,
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


class LiveEndpointParseTests(unittest.TestCase):
    # Shape captured from a real chatgpt.com/backend-api/wham/usage response.
    def test_codex_wham_both_windows(self):
        payload = {
            "plan_type": "plus",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 53,
                    "limit_window_seconds": 604800,
                    "reset_at": 1786160271,
                },
                "secondary_window": {
                    "used_percent": 20,
                    "limit_window_seconds": 18000,
                    "reset_at": 1785640000,
                },
            },
        }
        now = 1785630000
        usage = parse_codex_wham_usage(payload, now=now)
        labels = [w["label"] for w in usage["windows"]]
        self.assertEqual(labels, ["5h", "weekly"])
        self.assertEqual(usage["windows"][0]["remaining"], 80.0)
        self.assertEqual(usage["windows"][1]["remaining"], 47.0)
        self.assertIn("5h 80% left", usage["summary"])
        self.assertIn("weekly 47% left", usage["summary"])
        # weekly (47%) is tighter than 5h (80%), so its reset is shown
        self.assertIn("resets in 6d", usage["summary"])

    def test_codex_wham_null_secondary_means_5h_untouched(self):
        payload = {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 53,
                    "limit_window_seconds": 604800,
                    "reset_at": 1786160271,
                },
                "secondary_window": None,
            }
        }
        usage = parse_codex_wham_usage(payload, now=1785630000)
        self.assertEqual(usage["windows"][0]["label"], "5h")
        self.assertEqual(usage["windows"][0]["remaining"], 100.0)

    def test_codex_wham_garbage_is_none(self):
        self.assertIsNone(parse_codex_wham_usage({}, now=0))
        self.assertIsNone(parse_codex_wham_usage({"rate_limit": "x"}, now=0))

    def test_claude_oauth_windows(self):
        payload = {
            "five_hour": {"utilization": 12, "resets_at": "2026-08-02T03:00:00Z"},
            "seven_day": {"utilization": 88.5, "resets_at": "2026-08-05T00:00:00Z"},
            "extra_field": "ignored",
        }
        usage = parse_claude_oauth_usage(payload, now=1785630000)
        by_label = {w["label"]: w for w in usage["windows"]}
        self.assertEqual(by_label["5h"]["remaining"], 88.0)
        self.assertEqual(by_label["weekly"]["remaining"], 11.5)
        self.assertEqual(usage["remaining"], 11.5)

    def test_claude_oauth_empty_is_none(self):
        self.assertIsNone(parse_claude_oauth_usage({}, now=0))


class WindowLabelTests(unittest.TestCase):
    def test_five_hours(self):
        self.assertEqual(window_label(18000), "5h")

    def test_weekly(self):
        self.assertEqual(window_label(604800), "weekly")

    def test_garbage(self):
        self.assertIsNone(window_label(None))
        self.assertIsNone(window_label(0))


class SummarizeTests(unittest.TestCase):
    def test_tightest_window_reset_wins(self):
        summary = summarize_windows([
            {"label": "5h", "remaining": 100.0, "reset": None},
            {"label": "weekly", "remaining": 47.0, "reset": "in 6d 3h"},
        ])
        self.assertEqual(
            summary, "5h 100% left \u00b7 weekly 47% left (resets in 6d 3h)"
        )

    def test_empty_is_none(self):
        self.assertIsNone(summarize_windows([]))


class ClaudeTokenHelperTests(unittest.TestCase):
    def test_expired_uses_ms_epoch_with_margin(self):
        now = 1785630000  # seconds
        oauth = {"expiresAt": (now - 10) * 1000}
        self.assertTrue(_claude_token_expired(oauth, now=now))
        # within the 60s safety margin still counts as expired
        oauth = {"expiresAt": (now + 30) * 1000}
        self.assertTrue(_claude_token_expired(oauth, now=now))
        oauth = {"expiresAt": (now + 3600) * 1000}
        self.assertFalse(_claude_token_expired(oauth, now=now))

    def test_missing_expiry_is_not_expired(self):
        # No expiry recorded: the endpoint gets to be the judge.
        self.assertFalse(_claude_token_expired({}, now=0))

    def test_persist_merges_and_preserves_other_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".credentials.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {"claudeAiOauth": {"accessToken": "old"}, "other": {"keep": 1}},
                    fh,
                )
            _persist_claude_oauth(path, {"accessToken": "new", "expiresAt": 5})
            with open(path, "r", encoding="utf-8") as fh:
                creds = json.load(fh)
            self.assertEqual(creds["claudeAiOauth"]["accessToken"], "new")
            self.assertEqual(creds["other"], {"keep": 1})

    def test_read_rejects_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".credentials.json")
            self.assertIsNone(_read_claude_oauth(path))  # missing
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("not json")
            self.assertIsNone(_read_claude_oauth(path))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"claudeAiOauth": "not a dict"}, fh)
            self.assertIsNone(_read_claude_oauth(path))


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
