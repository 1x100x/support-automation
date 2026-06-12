import sys
import json
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime
from argparse import Namespace
from io import BytesIO, StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_support_bug_report as report
import fetch_jira_help_tickets as fetch_jira
import generate_support_canvas as canvas
import generate_help_report_dashboard as dashboard
import run_weekly_help_bug_report as runner
import support_triage


class WeeklyHelpBugReportTest(unittest.TestCase):
    def test_ticket_summary_prefers_llm_summary(self):
        summary = report.ticket_summary(
            {
                "title": "Raw title",
                "description": "issue details: raw description",
                "llm_summary": "Collector cannot see newly minted artwork on their profile even though the artwork page is available.",
            }
        )

        self.assertEqual(
            summary,
            "Collector cannot see newly minted artwork on their profile even though the artwork page is available",
        )

    def test_llm_ticket_summary_redacts_request_and_response(self):
        old_urlopen = report.urllib.request.urlopen
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "output_text": json.dumps(
                            {
                                "summaries": [
                                    {
                                        "key": "HELP-1",
                                        "summary": (
                                            "Customer cannot settle an imported auction despite having enough ETH; "
                                            "the flow should avoid leaking collector@example.com or "
                                            "0x39e2d6ed53f4866cbf9e12a21d05caa84d2d4dc2."
                                        ),
                                    }
                                ]
                            }
                        )
                    }
                ).encode("utf-8")

        def fake_urlopen(req, timeout=30):
            captured["body"] = req.data.decode("utf-8")
            captured["timeout"] = timeout
            return FakeResponse()

        try:
            report.urllib.request.urlopen = fake_urlopen
            summary = report.llm_ticket_summary(
                {
                    "key": "HELP-1",
                    "title": "[SETTLE ISSUE] collector@example.com cannot settle",
                    "description": (
                        "issue details: User collector@example.com with wallet "
                        "0x39e2d6ed53f4866cbf9e12a21d05caa84d2d4dc2 cannot settle. "
                        "links: https://example.com/raw"
                    ),
                    "suggested_labels": {"product_area": "wallet", "root_cause": "permissions"},
                },
                api_key="test-key",
                model="test-model",
            )
        finally:
            report.urllib.request.urlopen = old_urlopen

        self.assertNotIn("collector@example.com", captured["body"])
        self.assertNotIn("0x39e2d6ed53f4866cbf9e12a21d05caa84d2d4dc2", captured["body"])
        self.assertNotIn("https://example.com/raw", captured["body"])
        self.assertIn("[redacted email]", captured["body"])
        self.assertIn("[redacted address]", captured["body"])
        self.assertNotIn("collector@example.com", summary)
        self.assertNotIn("0x39e2d6ed53f4866cbf9e12a21d05caa84d2d4dc2", summary)
        self.assertIn("cannot settle an imported auction", summary)

    def test_llm_summary_http_error_is_recorded_in_metadata(self):
        ticket = {
            "key": "HELP-1",
            "title": "Artwork missing from profile",
            "description": "issue details: A collector cannot see their artwork on their profile.",
            "created_at": report.datetime.now(report.ET).isoformat(),
            "suggested_labels": {"issue_type": "bug", "product_area": "profile"},
        }
        old_urlopen = report.urllib.request.urlopen
        old_api_key = report.os.environ.get("OPENAI_API_KEY")

        def fake_urlopen(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                "https://api.openai.com/v1/responses",
                429,
                "Too Many Requests",
                {},
                BytesIO(b'{"error":{"message":"Rate limit reached for requests"}}'),
            )

        try:
            report.os.environ["OPENAI_API_KEY"] = "test-key"
            report.urllib.request.urlopen = fake_urlopen
            with redirect_stdout(StringIO()):
                payload = report.build_report(
                    {"metadata": {"workflow": "test"}, "tickets": [ticket]},
                    days=7,
                    friday_window=False,
                    use_llm_summaries=True,
                )
        finally:
            report.urllib.request.urlopen = old_urlopen
            if old_api_key is None:
                report.os.environ.pop("OPENAI_API_KEY", None)
            else:
                report.os.environ["OPENAI_API_KEY"] = old_api_key

        self.assertEqual(payload["metadata"]["summary_source"], "heuristic")
        self.assertEqual(payload["metadata"]["llm_summary_count"], 0)
        self.assertIn("Rate limit", payload["metadata"]["summary_error"])
        self.assertEqual(payload["window_tickets"][0]["summary_source"], "heuristic-fallback")

    def test_runner_uses_previous_friday_through_thursday_window(self):
        now = datetime(2026, 6, 3, 15, 0, tzinfo=runner.ET)

        since, until_exclusive, report_date = runner.friday_window(now)

        self.assertEqual(since.strftime("%Y-%m-%d"), "2026-05-22")
        self.assertEqual(until_exclusive.strftime("%Y-%m-%d"), "2026-05-29")
        self.assertEqual(report_date.strftime("%Y-%m-%d"), "2026-05-28")
        self.assertEqual(
            runner.default_help_jql(since, until_exclusive),
            'project = HELP AND created >= "2026-05-22" AND created < "2026-05-29" ORDER BY created DESC',
        )

    def test_schedule_gate_allows_only_friday_7am_et(self):
        self.assertTrue(runner.is_friday_7am_et(datetime(2026, 6, 5, 7, 0, tzinfo=runner.ET)))
        self.assertFalse(runner.is_friday_7am_et(datetime(2026, 6, 5, 8, 0, tzinfo=runner.ET)))
        self.assertFalse(runner.is_friday_7am_et(datetime(2026, 6, 4, 7, 0, tzinfo=runner.ET)))

    def test_jira_fetcher_uses_new_search_jql_payload(self):
        payload = fetch_jira.search_payload("project = HELP", 25, "next-page-token")

        self.assertEqual(payload["jql"], "project = HELP")
        self.assertEqual(payload["maxResults"], 25)
        self.assertEqual(payload["nextPageToken"], "next-page-token")
        self.assertIn("summary", payload["fields"])

    def test_jira_fetcher_falls_back_to_curl_on_urlopen_network_error(self):
        old_urlopen = fetch_jira.urllib.request.urlopen
        old_curl = fetch_jira.fetch_page_with_curl
        calls = []

        def fake_urlopen(*args, **kwargs):
            raise urllib.error.URLError("dns unavailable")

        def fake_curl(url, headers, body_text):
            calls.append((url, headers, body_text))
            return {"issues": [{"key": "HELP-1"}], "isLast": True}

        try:
            fetch_jira.urllib.request.urlopen = fake_urlopen
            fetch_jira.fetch_page_with_curl = fake_curl
            page = fetch_jira.fetch_page(
                "https://example.atlassian.net",
                {"Authorization": "Basic token", "Accept": "application/json"},
                "project = HELP",
                50,
            )
        finally:
            fetch_jira.urllib.request.urlopen = old_urlopen
            fetch_jira.fetch_page_with_curl = old_curl

        self.assertEqual(page["issues"][0]["key"], "HELP-1")
        self.assertEqual(calls[0][0], "https://example.atlassian.net/rest/api/3/search/jql")
        self.assertIn("project = HELP", calls[0][2])

    def test_report_uses_previous_friday_through_thursday_window(self):
        now = datetime(2026, 6, 3, 15, 0, tzinfo=report.ET)

        since, until = report.friday_to_thursday_window(now)

        self.assertEqual(since.strftime("%Y-%m-%d %H:%M:%S"), "2026-05-22 00:00:00")
        self.assertEqual(until.strftime("%Y-%m-%d %H:%M:%S"), "2026-05-28 23:59:59")

    def test_slack_summary_redacts_email_and_wallet_address(self):
        summary = report.ticket_summary(
            {
                "description": (
                    "issue details: User email collector@example.com and wallet "
                    "0x39e2d6ed53f4866cbf9e12a21d05caa84d2d4dc2 need help. "
                    "links: https://example.com"
                )
            }
        )

        self.assertIn("[redacted email]", summary)
        self.assertIn("[redacted address]", summary)
        self.assertNotIn("collector@example.com", summary)
        self.assertNotIn("0x39e2d6ed53f4866cbf9e12a21d05caa84d2d4dc2", summary)
        self.assertNotIn("https://example.com", summary)

    def test_slack_summary_uses_issue_details_not_raw_intake_boilerplate(self):
        summary = report.ticket_summary(
            {
                "title": "[SPACES ISSUE] @artist - Can't mint new NFTs on my contract",
                "description": (
                    "user info: @artist artist@example.com issue details: "
                    "The artist can see their existing collection, but the contract is missing from the create page "
                    "when they try to mint a new work. links: https://superrare.com/artist "
                    "attachment: https://airtable.example/attachment notes: internal handling note"
                ),
            }
        )

        self.assertIn("Can't mint new NFTs on my contract", summary)
        self.assertIn("contract is missing from the create page", summary)
        self.assertNotIn("user info", summary.lower())
        self.assertNotIn("links:", summary.lower())
        self.assertNotIn("attachment", summary.lower())

    def test_account_support_label_absorbs_user_error(self):
        labels = support_triage.labels_from_jira_labels(["bug", "user-error", "account-support"])

        self.assertEqual(labels["issue_type"], "account")
        self.assertFalse(report.is_bug({"suggested_labels": labels}))

    def test_user_error_without_account_support_is_non_bug(self):
        labels = support_triage.labels_from_jira_labels(["bug", "user-error"])

        self.assertEqual(labels["issue_type"], "user-error")
        self.assertFalse(report.is_bug({"suggested_labels": labels}))

    def test_plain_bug_label_counts_as_bug(self):
        labels = support_triage.labels_from_jira_labels(["bug", "profile"])

        self.assertEqual(labels["issue_type"], "bug")
        self.assertEqual(labels["product_area"], "profile")
        self.assertTrue(report.is_bug({"suggested_labels": labels}))

    def test_env_status_flags_placeholders_without_revealing_values(self):
        old_value = runner.os.environ.get("JIRA_API_TOKEN")
        try:
            runner.os.environ["JIRA_API_TOKEN"] = "replace-with-local-token"
            self.assertEqual(
                runner.env_status("JIRA_API_TOKEN", placeholder_values={"replace-with-local-token"}),
                "placeholder",
            )
            runner.os.environ["JIRA_API_TOKEN"] = "real-looking-token"
            self.assertEqual(runner.env_status("JIRA_API_TOKEN"), "set")
        finally:
            if old_value is None:
                runner.os.environ.pop("JIRA_API_TOKEN", None)
            else:
                runner.os.environ["JIRA_API_TOKEN"] = old_value

    def test_preflight_fails_without_jira_values(self):
        args = Namespace(
            env_file="/private/tmp/missing-weekly-help-env",
            post_slack=True,
            raw_output="data/help_inbound_raw.json",
            normalized_output="data/support_dashboard_tickets.json",
            report_md="data/support_weekly_bug_report.md",
            report_json="data/support_weekly_bug_report.json",
            dashboard_output="data/support_weekly_bug_report_dashboard.json",
        )
        since, until_exclusive, report_date = runner.friday_window(
            datetime(2026, 6, 3, 15, 0, tzinfo=runner.ET)
        )
        old_values = {key: runner.os.environ.get(key) for key in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")}

        try:
            for key in old_values:
                runner.os.environ.pop(key, None)
            with redirect_stdout(StringIO()):
                result = runner.print_preflight(
                    args,
                    jql=runner.default_help_jql(since, until_exclusive),
                    since=since,
                    report_date=report_date,
                )
            self.assertEqual(result, 1)
        finally:
            for key, value in old_values.items():
                if value is None:
                    runner.os.environ.pop(key, None)
                else:
                    runner.os.environ[key] = value

    def test_preflight_accepts_process_environment_without_local_env_file(self):
        args = Namespace(
            env_file="/private/tmp/missing-weekly-help-env",
            post_slack=False,
            raw_output="data/help_inbound_raw.json",
            normalized_output="data/support_dashboard_tickets.json",
            report_md="data/support_weekly_bug_report.md",
            report_json="data/support_weekly_bug_report.json",
            dashboard_output="data/support_weekly_bug_report_dashboard.json",
        )
        since, until_exclusive, report_date = runner.friday_window(
            datetime(2026, 6, 3, 15, 0, tzinfo=runner.ET)
        )
        old_values = {key: runner.os.environ.get(key) for key in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")}

        try:
            runner.os.environ["JIRA_BASE_URL"] = "https://example.atlassian.net"
            runner.os.environ["JIRA_EMAIL"] = "ops@example.com"
            runner.os.environ["JIRA_API_TOKEN"] = "real-looking-token"
            with redirect_stdout(StringIO()):
                result = runner.print_preflight(
                    args,
                    jql=runner.default_help_jql(since, until_exclusive),
                    since=since,
                    report_date=report_date,
                )
            self.assertEqual(result, 0)
        finally:
            for key, value in old_values.items():
                if value is None:
                    runner.os.environ.pop(key, None)
                else:
                    runner.os.environ[key] = value

    def test_dashboard_payload_maps_report_counts_and_ticket_detail(self):
        weekly_report = {
            "metadata": {"generated_at": "2026-05-29T09:00:00-04:00", "since": "2026-05-22", "until": "2026-05-28"},
            "totals": {"inbound": 2, "bugs": 1, "needs_engineering": 1, "low_confidence": 0},
            "breakdowns": {
                "issue_type_all": [["bug", 1], ["task", 1]],
                "product_area": [["checkout", 1]],
                "root_cause": [["payment-provider", 1]],
            },
            "service_ticket_counts": [["task", 1]],
            "window_tickets": [
                {
                    "key": "HELP-1",
                    "title": "Checkout failure",
                    "suggested_labels": {
                        "issue_type": "bug",
                        "product_area": "checkout",
                        "severity": "high",
                        "root_cause": "payment-provider",
                        "needs_engineering": "yes",
                    },
                },
                {
                    "key": "HELP-2",
                    "title": "Account support question",
                    "suggested_labels": {
                        "issue_type": "account",
                        "product_area": "releases",
                        "severity": "low",
                        "root_cause": "contract-indexing",
                        "needs_engineering": "unknown",
                    },
                },
            ],
        }

        payload = dashboard.build_dashboard_payload(weekly_report, source_path="data/support_weekly_bug_report.json")

        self.assertEqual(payload["snapshot"]["datasets"]["summary"][0]["inbound"], 2)
        self.assertEqual(payload["snapshot"]["datasets"]["summary"][0]["service_tasks"], 1)
        self.assertEqual(payload["snapshot"]["datasets"]["issue_type_mix"][1]["issue_type"], "task")
        self.assertEqual(payload["snapshot"]["datasets"]["ticket_detail"][0]["key"], "HELP-1")
        self.assertEqual(payload["snapshot"]["datasets"]["ticket_detail"][1]["needs_engineering"], "no")
        self.assertEqual(payload["manifest"]["surface"], "dashboard")
        self.assertIn("chart-product-area", [chart["id"] for chart in payload["manifest"]["charts"]])

    def test_report_groups_service_items_as_generic_tasks(self):
        ticket = {
            "created_at": report.datetime.now(report.ET).isoformat(),
            "suggested_labels": {"issue_type": "account"},
        }
        weekly_report = {
            "metadata": {"workflow": "test"},
            "tickets": [ticket],
        }

        payload = report.build_report(weekly_report, days=7, friday_window=False)

        self.assertEqual(payload["breakdowns"]["issue_type_all"], [("task", 1)])
        self.assertEqual(payload["service_ticket_counts"], [("task", 1)])

    def test_slack_report_header_uses_generated_post_date(self):
        payload = {
            "metadata": {
                "generated_at": "2026-06-12T09:00:00-04:00",
                "since": "2026-06-05T00:00:00-04:00",
                "until": "2026-06-11T23:59:59-04:00",
                "dashboard_url": "",
                "jira_base_url": "https://example.atlassian.net",
            },
            "totals": {"inbound": 0, "bugs": 0, "needs_engineering": 0, "low_confidence": 0},
            "breakdowns": {"issue_type_all": [], "product_area": [], "root_cause": []},
            "bug_tickets_by_area": {},
            "service_ticket_counts": [],
            "root_cause_review": [],
            "window_tickets": [],
        }

        markdown = report.markdown_report(payload)

        self.assertIn("*WEEKLY BUG REPORT 6/12/2026*", markdown)
        self.assertNotIn("*WEEKLY BUG REPORT 6/11/26*", markdown)

    def test_slack_ticket_layout_uses_possible_root_cause_wording(self):
        payload = {
            "metadata": {
                "generated_at": "2026-06-12T09:00:00-04:00",
                "since": "2026-06-05T00:00:00-04:00",
                "until": "2026-06-11T23:59:59-04:00",
                "dashboard_url": "",
                "jira_base_url": "https://example.atlassian.net",
            },
            "totals": {"inbound": 1, "bugs": 1, "needs_engineering": 1, "low_confidence": 0},
            "breakdowns": {
                "issue_type_all": [("bug", 1)],
                "product_area": [("profile", 1)],
                "root_cause": [("data-mismatch", 1)],
            },
            "bug_tickets_by_area": {
                "profile": [
                    {
                        "key": "HELP-1",
                        "title": "Profile missing artwork",
                        "description": "issue details: Artwork is missing from profile. links: https://example.com",
                        "reporter": "Support",
                        "repo_signal": {"paths": ["src/profile/gallery.ts", "docs/profile-runbook.md"]},
                        "suggested_labels": {
                            "severity": "medium",
                            "root_cause": "data-mismatch",
                            "product_area": "profile",
                            "issue_type": "bug",
                        },
                    }
                ]
            },
            "service_ticket_counts": [("task", 1)],
            "root_cause_review": [],
            "window_tickets": [],
        }

        markdown = report.markdown_report(payload)

        self.assertIn("\n• Reported By: Support\n", markdown)
        self.assertIn("\nTicket: `HELP-1` - Profile missing artwork\n", markdown)
        self.assertIn("\nSummary: Profile missing artwork: Artwork is missing from profile\n", markdown)
        self.assertIn("Severity: `medium` | Possible root cause: `data-mismatch`", markdown)
        self.assertIn("Likely code area: `src/profile/gallery.ts`", markdown)
        self.assertIn("\n\n:bellhop_bell:", markdown)
        self.assertIn("\nTask (1)\n", markdown)
        self.assertNotIn("Root cause:", markdown)
        self.assertNotIn("docs/profile-runbook.md", markdown)

    def test_dashboard_and_canvas_share_summary_and_likely_code_area(self):
        weekly_report = {
            "metadata": {
                "generated_at": "2026-06-12T09:00:00-04:00",
                "since": "2026-06-05T00:00:00-04:00",
                "until": "2026-06-11T23:59:59-04:00",
            },
            "totals": {"inbound": 1, "bugs": 1, "needs_engineering": 1, "low_confidence": 0},
            "breakdowns": {
                "issue_type_all": [("bug", 1)],
                "product_area": [("profile", 1)],
                "root_cause": [("data-mismatch", 1)],
            },
            "service_ticket_counts": [],
            "window_tickets": [
                {
                    "key": "HELP-1",
                    "title": "[INDEXING ISSUE] @artist - Artwork missing from profile",
                    "description": "hello issue details: The artwork is missing from the collector profile, but appears on the artwork page. links: https://example.com",
                    "source_url": "https://example.atlassian.net/browse/HELP-1",
                    "created_at": "2026-06-06T12:00:00-04:00",
                    "repo_signal": {"paths": ["src/profile/gallery.ts", "references/profile-note.md", ".env.example"]},
                    "suggested_labels": {
                        "issue_type": "bug",
                        "product_area": "profile",
                        "severity": "medium",
                        "root_cause": "data-mismatch",
                        "needs_engineering": "yes",
                    },
                }
            ],
        }
        weekly_report["bug_tickets"] = weekly_report["window_tickets"]

        payload = dashboard.build_dashboard_payload(weekly_report, source_path="data/support_weekly_bug_report.json")
        canvas_markdown = canvas.build_canvas_markdown(weekly_report, {"tickets": weekly_report["window_tickets"]}, 1)

        detail = payload["snapshot"]["datasets"]["ticket_detail"][0]
        self.assertEqual(detail["summary"], "Artwork missing from profile: The artwork is missing from the collector profile; but appears on the artwork page")
        self.assertEqual(detail["likely_code_area"], "src/profile/gallery.ts")
        self.assertIn("Likely Code Area", canvas_markdown)
        self.assertIn("src/profile/gallery.ts", canvas_markdown)
        self.assertNotIn("references/profile-note.md", canvas_markdown)

    def test_canvas_markdown_includes_weekly_bug_task_trend(self):
        weekly_report = {
            "metadata": {
                "since": "2026-05-29T00:00:00-04:00",
                "until": "2026-06-04T23:59:59-04:00",
            },
            "totals": {"inbound": 1, "bugs": 0, "needs_engineering": 0, "low_confidence": 0},
            "service_ticket_counts": [("task", 1)],
            "bug_tickets": [],
            "window_tickets": [
                {
                    "key": "HELP-2",
                    "title": "Account task",
                    "source_url": "https://example.atlassian.net/browse/HELP-2",
                    "created_at": "2026-05-30T12:00:00-04:00",
                    "suggested_labels": {"issue_type": "account", "product_area": "checkout"},
                }
            ],
        }
        trend_data = {
            "tickets": [
                {
                    "created_at": "2026-05-22T12:00:00-04:00",
                    "suggested_labels": {"issue_type": "bug"},
                },
                {
                    "created_at": "2026-05-30T12:00:00-04:00",
                    "suggested_labels": {"issue_type": "account"},
                },
            ]
        }

        markdown = canvas.build_canvas_markdown(weekly_report, trend_data, 2)

        self.assertIn("# Weekly Trend", markdown)
        self.assertIn("| May 22-28 | 1 | 0 | 1 |", markdown)
        self.assertIn("| May 29-Jun 4 | 0 | 1 | 1 |", markdown)
        self.assertIn(":large_red_square:", markdown)
        self.assertIn(":large_blue_square:", markdown)


if __name__ == "__main__":
    unittest.main()
