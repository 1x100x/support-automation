#!/usr/bin/env python3
"""Run a Slack-only smoke test for report posting and native Canvas sharing."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from generate_support_bug_report import post_to_slack


ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def smoke_title(generated_at: datetime) -> str:
    return f"Support Automation Slack Canvas Smoke Test - {generated_at.strftime('%Y-%m-%d %H:%M %Z')}"


def smoke_report_markdown(generated_at: datetime) -> str:
    stamp = generated_at.strftime("%Y-%m-%d %H:%M:%S %Z")
    return (
        "--------------------------------------------------------\n"
        ":test_tube: *SUPPORT AUTOMATION SLACK SMOKE TEST*\n"
        "--------------------------------------------------------\n\n"
        f"Generated at `{stamp}`.\n\n"
        "This validates the weekly Help report Slack path without fetching Jira tickets "
        "or calling OpenAI.\n\n"
        "Expected result: this message should be updated with a Canvas dashboard link "
        "on the original post.\n"
    )


def smoke_canvas_markdown(generated_at: datetime) -> str:
    stamp = generated_at.strftime("%Y-%m-%d %H:%M:%S %Z")
    return (
        "# Slack Canvas Smoke Test\n\n"
        f"Generated at `{stamp}`.\n\n"
        "This Canvas confirms the support automation can create a native Slack Canvas, "
        "grant access to the report channel, and attach the Canvas link to the original "
        "Slack report post.\n\n"
        "| Check | Expected |\n"
        "|---|---|\n"
        "| Jira fetch | Skipped |\n"
        "| OpenAI summary calls | Skipped |\n"
        "| Slack report post | Visible in the configured channel |\n"
        "| Canvas link | Added to the original Slack post |\n"
        "| Canvas access | Channel members can open without requesting access |\n"
    )


def write_smoke_files(report_path: Path, canvas_path: Path, generated_at: datetime) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    canvas_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(smoke_report_markdown(generated_at), encoding="utf-8")
    canvas_path.write_text(smoke_canvas_markdown(generated_at), encoding="utf-8")


def run_canvas_post(
    *,
    canvas_path: Path,
    report_path: Path,
    slack_result_path: Path,
    canvas_result_path: Path,
    title: str,
) -> None:
    subprocess.run(
        [
            sys.executable,
            "scripts/post_slack_canvas_file.py",
            "--file",
            str(canvas_path),
            "--report-md",
            str(report_path),
            "--slack-result-json",
            str(slack_result_path),
            "--title",
            title,
            "--initial-comment",
            "Slack Canvas smoke test for the weekly Help bug report automation.",
            "--status-output",
            str(canvas_result_path),
        ],
        cwd=ROOT,
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Post a Slack-only Canvas smoke test.")
    parser.add_argument(
        "--report-md",
        default=os.getenv("SUPPORT_SLACK_SMOKE_REPORT_MD", "data/support_slack_smoke_report.md"),
    )
    parser.add_argument(
        "--canvas-md",
        default=os.getenv("SUPPORT_SLACK_SMOKE_CANVAS_MD", "data/support_slack_smoke_canvas.md"),
    )
    parser.add_argument(
        "--slack-result-output",
        default=os.getenv("SUPPORT_SLACK_SMOKE_RESULT_JSON", "data/support_slack_smoke_post_result.json"),
    )
    parser.add_argument(
        "--slack-canvas-result-output",
        default=os.getenv("SUPPORT_SLACK_SMOKE_CANVAS_RESULT_JSON", "data/support_slack_smoke_canvas_result.json"),
    )
    parser.add_argument("--slack-channel", default=os.getenv("SLACK_REPORT_CHANNEL_ID", ""))
    parser.add_argument("--dry-run", action="store_true", help="Write smoke files but do not call Slack.")
    args = parser.parse_args()

    generated_at = datetime.now(ET)
    report_path = Path(args.report_md)
    canvas_path = Path(args.canvas_md)
    slack_result_path = Path(args.slack_result_output)
    canvas_result_path = Path(args.slack_canvas_result_output)
    title = smoke_title(generated_at)

    write_smoke_files(report_path, canvas_path, generated_at)

    if args.dry_run:
        print("Slack Canvas smoke test dry run")
        print(f"- Report markdown: {report_path}")
        print(f"- Canvas markdown: {canvas_path}")
        print("- Slack calls: skipped")
        return

    channel = args.slack_channel or require_env("SLACK_REPORT_CHANNEL_ID")
    require_env("SLACK_BOT_TOKEN")

    result = post_to_slack(report_path.read_text(encoding="utf-8"), channel)
    write_json(slack_result_path, result)

    run_canvas_post(
        canvas_path=canvas_path,
        report_path=report_path,
        slack_result_path=slack_result_path,
        canvas_result_path=canvas_result_path,
        title=title,
    )

    print(f"Slack Canvas smoke test posted to {channel}.")
    print(f"Slack post result: {slack_result_path}")
    print(f"Slack Canvas result: {canvas_result_path}")


if __name__ == "__main__":
    main()
