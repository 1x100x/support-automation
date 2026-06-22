#!/usr/bin/env python3
"""Run the end-to-end Friday Help-board bug report workflow."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
SCHEDULED_POST_HOUR_ET = 3
SCHEDULED_POST_MINUTE_ET = 30


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def env_status(name: str, *, placeholder_values: set[str] | None = None) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        return "missing"
    if placeholder_values and value in placeholder_values:
        return "placeholder"
    return "set"


def friday_window(now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    now_et = (now or datetime.now(ET)).astimezone(ET)
    days_since_thursday = (now_et.weekday() - 3) % 7
    if days_since_thursday == 0:
        days_since_thursday = 7
    end_day = now_et - timedelta(days=days_since_thursday)
    until_exclusive = (end_day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    since = (until_exclusive - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    report_date = until_exclusive - timedelta(days=1)
    return since, until_exclusive, report_date


def is_friday_7am_et(now: datetime | None = None) -> bool:
    now_et = (now or datetime.now(ET)).astimezone(ET)
    return now_et.weekday() == 4 and now_et.hour == 7


def is_friday_scheduled_post_time_et(now: datetime | None = None) -> bool:
    now_et = (now or datetime.now(ET)).astimezone(ET)
    return (
        now_et.weekday() == 4
        and now_et.hour == SCHEDULED_POST_HOUR_ET
        and now_et.minute >= SCHEDULED_POST_MINUTE_ET
    )


def expected_friday_scheduled_post_utc_cron(now: datetime | None = None) -> str:
    now_et = (now or datetime.now(ET)).astimezone(ET)
    utc_hour = (SCHEDULED_POST_HOUR_ET - int(now_et.utcoffset().total_seconds() // 3600)) % 24
    return f"{SCHEDULED_POST_MINUTE_ET} {utc_hour} * * 5"


def github_event_schedule(event_path: str | None = None) -> str:
    raw_path = event_path if event_path is not None else os.getenv("GITHUB_EVENT_PATH", "")
    if not raw_path:
        return ""
    path = Path(raw_path)
    if not path.exists():
        return ""
    try:
        event = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(event.get("schedule", "")).strip()


def should_run_scheduled_post(now: datetime | None = None, event_schedule: str | None = None) -> bool:
    now_et = (now or datetime.now(ET)).astimezone(ET)
    schedule = github_event_schedule() if event_schedule is None else event_schedule.strip()
    if now_et.weekday() != 4:
        return False

    if schedule:
        return schedule == expected_friday_scheduled_post_utc_cron(now_et)

    return is_friday_scheduled_post_time_et(now_et)


def shell_quote_jql_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d")


def default_help_jql(since: datetime, until_exclusive: datetime) -> str:
    return (
        "project = HELP "
        f'AND created >= "{shell_quote_jql_datetime(since)}" '
        f'AND created < "{shell_quote_jql_datetime(until_exclusive)}" '
        "ORDER BY created DESC"
    )


def run_step(command: list[str], *, dry_run: bool) -> None:
    print("+ " + " ".join(command))
    if dry_run:
        return
    subprocess.run(command, cwd=ROOT, check=True)


def print_preflight(args: argparse.Namespace, *, jql: str, since: datetime, report_date: datetime) -> int:
    env_file = ROOT / args.env_file
    checks = {
        "env_file": "present" if env_file.exists() else "missing",
        "JIRA_BASE_URL": env_status("JIRA_BASE_URL", placeholder_values={"https://your-domain.atlassian.net"}),
        "JIRA_EMAIL": env_status("JIRA_EMAIL", placeholder_values={"you@company.com"}),
        "JIRA_API_TOKEN": env_status("JIRA_API_TOKEN", placeholder_values={"replace-with-local-token"}),
    }
    if args.post_slack:
        checks["SLACK_REPORT_CHANNEL_ID"] = env_status(
            "SLACK_REPORT_CHANNEL_ID",
            placeholder_values={"C0123456789"},
        )
        checks["SLACK_BOT_TOKEN"] = env_status(
            "SLACK_BOT_TOKEN",
            placeholder_values={"xoxb-replace-with-local-token", "replace-with-local-token"},
        )

    print("Weekly Help bug report preflight")
    print(f"- Env file: {args.env_file} ({checks['env_file']})")
    print(f"- Window: {since.strftime('%Y-%m-%d')} through {report_date.strftime('%Y-%m-%d')} ET")
    print(f"- JQL: {jql}")
    print(f"- Raw output: {args.raw_output}")
    print(f"- Normalized output: {args.normalized_output}")
    print(f"- Markdown output: {args.report_md}")
    print(f"- JSON output: {args.report_json}")
    print(f"- Dashboard output: {args.dashboard_output}")
    print(f"- Canvas output: {getattr(args, 'canvas_output', 'data/support_weekly_bug_report_canvas.md')}")
    print(f"- Slack result output: {getattr(args, 'slack_result_output', 'data/support_slack_post_result.json')}")
    print(f"- Slack Canvas result output: {getattr(args, 'slack_canvas_result_output', 'data/support_slack_canvas_result.json')}")
    if os.getenv("SUPPORT_USE_LLM_SUMMARIES", "").lower() in {"1", "true", "yes"}:
        print(f"- LLM summaries: enabled ({env_status('OPENAI_API_KEY')})")
    else:
        print("- LLM summaries: disabled")
    print("- Required values:")
    for key, status in checks.items():
        if key == "env_file":
            continue
        print(f"  - {key}: {status}")

    failing = [
        key
        for key, status in checks.items()
        if key != "env_file" and status in {"missing", "placeholder"}
    ]
    if failing:
        print("Preflight failed: " + ", ".join(failing))
        return 1
    if checks["env_file"] == "missing":
        print(
            f"Preflight passed using process environment values. "
            f"Create {args.env_file} if you want local persisted defaults."
        )
        return 0
    print("Preflight passed.")
    return 0


def main() -> None:
    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument("--env-file", default=".env.support")
    env_args, _ = env_parser.parse_known_args()
    load_env_file(ROOT / env_args.env_file)

    parser = argparse.ArgumentParser(
        description="Fetch Jira HELP tickets, normalize labels, and generate the Friday Slack bug report."
    )
    parser.add_argument("--env-file", default=".env.support", help="Local env file to load before running.")
    parser.add_argument("--raw-output", default=os.getenv("SUPPORT_DASHBOARD_RAW", "data/help_inbound_raw.json"))
    parser.add_argument(
        "--normalized-output",
        default=os.getenv("SUPPORT_DASHBOARD_NORMALIZED", "data/support_dashboard_tickets.json"),
    )
    parser.add_argument(
        "--report-json",
        default=os.getenv("SUPPORT_BUG_REPORT_JSON", "data/support_weekly_bug_report.json"),
    )
    parser.add_argument(
        "--report-md",
        default=os.getenv("SUPPORT_BUG_REPORT_MD", "data/support_weekly_bug_report.md"),
    )
    parser.add_argument(
        "--dashboard-output",
        default=os.getenv("SUPPORT_BUG_REPORT_DASHBOARD", "data/support_weekly_bug_report_dashboard.json"),
    )
    parser.add_argument(
        "--canvas-output",
        default=os.getenv("SUPPORT_BUG_REPORT_CANVAS", "data/support_weekly_bug_report_canvas.md"),
    )
    parser.add_argument(
        "--slack-result-output",
        default=os.getenv("SUPPORT_SLACK_RESULT_JSON", "data/support_slack_post_result.json"),
    )
    parser.add_argument(
        "--slack-canvas-result-output",
        default=os.getenv("SUPPORT_SLACK_CANVAS_RESULT_JSON", "data/support_slack_canvas_result.json"),
    )
    parser.add_argument("--limit", type=int, default=int(os.getenv("SUPPORT_DASHBOARD_LIMIT", "100")))
    parser.add_argument("--jql", default="", help="Override the generated Help-board Friday-window JQL.")
    parser.add_argument("--post-slack", action="store_true", help="Post to Slack after report generation.")
    parser.add_argument("--slack-channel", default=os.getenv("SLACK_REPORT_CHANNEL_ID", ""))
    parser.add_argument("--skip-fetch", action="store_true", help="Use an existing raw Jira JSON file.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    parser.add_argument("--preflight", action="store_true", help="Check non-secret local setup and exit.")
    parser.add_argument(
        "--schedule-gate",
        action="store_true",
        help="Exit without work unless this is the active scheduled America/New_York Friday post.",
    )
    args = parser.parse_args()

    if args.schedule_gate and not should_run_scheduled_post():
        schedule = github_event_schedule()
        expected = expected_friday_scheduled_post_utc_cron()
        suffix = f" schedule={schedule or 'none'} expected={expected}"
        print("Schedule gate skipped: this is not the active Friday New York post cron." + suffix)
        return

    since, until_exclusive, report_date = friday_window()
    jql = args.jql or default_help_jql(since, until_exclusive)

    if args.preflight:
        raise SystemExit(print_preflight(args, jql=jql, since=since, report_date=report_date))

    print(
        "Weekly Help bug report window: "
        f"{since.strftime('%Y-%m-%d')} through {report_date.strftime('%Y-%m-%d')} ET"
    )
    print(f"JQL: {jql}")

    if not args.skip_fetch:
        if not args.dry_run:
            require_env("JIRA_BASE_URL")
            require_env("JIRA_EMAIL")
            require_env("JIRA_API_TOKEN")
        run_step(
            [
                sys.executable,
                "scripts/fetch_jira_help_tickets.py",
                "--jql",
                jql,
                "--output",
                args.raw_output,
                "--limit",
                str(args.limit),
            ],
            dry_run=args.dry_run,
        )

    history = [
        "data/jira_created_results.json",
        "data/jira_created_results_sb1326_core_ux_next16_regressions_20260407.json",
    ]
    run_step(
        [
            sys.executable,
            "scripts/support_triage.py",
            "--input",
            args.raw_output,
            "--output",
            args.normalized_output,
            "--repo-root",
            ".",
            "--history",
            *history,
        ],
        dry_run=args.dry_run,
    )

    report_command = [
        sys.executable,
        "scripts/generate_support_bug_report.py",
        "--input",
        args.normalized_output,
        "--output-json",
        args.report_json,
        "--output-md",
        args.report_md,
        "--friday-window",
    ]
    if os.getenv("SUPPORT_USE_LLM_SUMMARIES", "").lower() in {"1", "true", "yes"}:
        report_command.append("--use-llm-summaries")
    if args.post_slack:
        channel = args.slack_channel or ("" if args.dry_run else require_env("SLACK_REPORT_CHANNEL_ID"))
        if not args.dry_run:
            require_env("SLACK_BOT_TOKEN")
        report_command.extend(
            [
                "--post-slack",
                "--slack-channel",
                channel,
                "--slack-result-output",
                args.slack_result_output,
            ]
        )

    run_step(report_command, dry_run=args.dry_run)
    run_step(
        [
            sys.executable,
            "scripts/generate_help_report_dashboard.py",
            "--input",
            args.report_json,
            "--output",
            args.dashboard_output,
        ],
        dry_run=args.dry_run,
    )
    run_step(
        [
            sys.executable,
            "scripts/generate_support_canvas.py",
            "--report-json",
            args.report_json,
            "--trend-input",
            args.normalized_output,
            "--output",
            args.canvas_output,
        ],
        dry_run=args.dry_run,
    )
    if args.post_slack:
        run_step(
            [
                sys.executable,
                "scripts/post_slack_canvas_file.py",
                "--file",
                args.canvas_output,
                "--report-md",
                args.report_md,
                "--slack-result-json",
                args.slack_result_output,
                "--title",
                f"Weekly Help Bug Report Dashboard - {report_date.strftime('%B')} {report_date.day}, {report_date.year}",
                "--initial-comment",
                "Canvas dashboard for this weekly Help bug report.",
                "--status-output",
                args.slack_canvas_result_output,
            ],
            dry_run=args.dry_run,
        )
    if not args.dry_run:
        print(f"Ready to review/share: {args.report_md}")
        print(f"Ready to render dashboard: {args.dashboard_output}")
        print(f"Ready to review Canvas markdown: {args.canvas_output}")


if __name__ == "__main__":
    main()
