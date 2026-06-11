#!/usr/bin/env python3
"""Generate Slack Canvas markdown for the weekly Help bug report dashboard."""

import argparse
import json
from datetime import timedelta
from pathlib import Path
from typing import Dict, Iterable, List

import generate_support_bug_report as report


def labels_for(ticket: Dict) -> Dict:
    return report.labels_for(ticket)


def parse_report_dt(value: str):
    return report.datetime.fromisoformat(value).astimezone(report.ET)


def week_label(start, end) -> str:
    if start.month == end.month:
        return f"{start.strftime('%b')} {start.day}-{end.day}"
    return f"{start.strftime('%b')} {start.day}-{end.strftime('%b')} {end.day}"


def trend_weeks(weekly_report: Dict, trend_data: Dict, lookback_weeks: int) -> List[Dict]:
    until = parse_report_dt(weekly_report["metadata"]["until"])
    current_start = parse_report_dt(weekly_report["metadata"]["since"])
    first_start = current_start - timedelta(days=7 * (lookback_weeks - 1))
    tickets = trend_data.get("tickets", [])
    rows = []
    for index in range(lookback_weeks):
        start = first_start + timedelta(days=7 * index)
        end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        if end > until:
            end = until
        week_tickets = [ticket for ticket in tickets if report.in_window(ticket, start, end)]
        bug_count = sum(1 for ticket in week_tickets if report.is_bug(ticket))
        task_count = len(week_tickets) - bug_count
        rows.append(
            {
                "label": week_label(start, end),
                "bug": bug_count,
                "task": task_count,
                "total": bug_count + task_count,
            }
        )
    return rows


def trend_bar(row: Dict) -> str:
    if row["total"] == 0:
        return "-"
    bug_blocks = ":large_red_square:" * row["bug"]
    task_blocks = ":large_blue_square:" * row["task"]
    return f"{bug_blocks}{task_blocks}"


def jira_link(ticket: Dict) -> str:
    key = ticket.get("key") or ticket.get("id") or "NO-KEY"
    url = ticket.get("source_url") or ""
    if not url:
        return key
    return f"[{key}]({url})"


def ticket_type(ticket: Dict) -> str:
    return "Bug" if report.is_bug(ticket) else "Task"


def ticket_rows(tickets: Iterable[Dict]) -> List[str]:
    rows = []
    for ticket in tickets:
        labels = labels_for(ticket)
        needs_engineering = labels.get("needs_engineering", "unknown")
        if not report.is_bug(ticket):
            needs_engineering = "no"
        rows.append(
            "| "
            + " | ".join(
                [
                    jira_link(ticket),
                    ticket_type(ticket),
                    report.display_name(labels.get("product_area", "unknown")),
                    report.display_name(labels.get("severity", "unknown")),
                    report.display_name(labels.get("root_cause", "unknown")),
                    report.display_name(needs_engineering),
                    str(ticket.get("title") or "Untitled ticket").replace("|", "\\|"),
                ]
            )
            + " |"
        )
    return rows


def build_canvas_markdown(weekly_report: Dict, trend_data: Dict, lookback_weeks: int) -> str:
    metadata = weekly_report["metadata"]
    totals = weekly_report["totals"]
    since = parse_report_dt(metadata["since"])
    until = parse_report_dt(metadata["until"])
    service_tasks = sum(int(count) for _label, count in weekly_report.get("service_ticket_counts", []))
    trend_rows = trend_weeks(weekly_report, trend_data, lookback_weeks)
    trend_total = sum(row["total"] for row in trend_rows)
    bug_total = sum(row["bug"] for row in trend_rows)
    task_total = sum(row["task"] for row in trend_rows)

    lines = [
        "# Summary",
        "",
        f"Weekly Help-board dashboard for {since.strftime('%b')} {since.day} through {until.strftime('%b')} {until.day}.",
        "",
        "| Metric | Count |",
        "|---|---:|",
        f"| Inbound tickets | {totals['inbound']} |",
        f"| Website bugs | {totals['bugs']} |",
        f"| SuperRare Services tasks | {service_tasks} |",
        f"| Needs engineering | {totals['needs_engineering']} |",
        f"| Low-confidence triage | {totals['low_confidence']} |",
        "",
        "# Weekly Trend",
        "",
        "| Week | Bug | Task | Total | Trend |",
        "|---|---:|---:|---:|---|",
    ]
    for row in trend_rows:
        lines.append(f"| {row['label']} | {row['bug']} | {row['task']} | {row['total']} | {trend_bar(row)} |")
    lines.extend(
        [
            "",
            f"Total across trend window: {trend_total} issues ({bug_total} bug, {task_total} task).",
            "",
            "# Website Bugs",
            "",
        ]
    )
    if weekly_report.get("bug_tickets"):
        lines.extend(
            [
                "| Ticket | Area | Severity | Root Cause | Summary |",
                "|---|---|---|---|---|",
            ]
        )
        for ticket in weekly_report["bug_tickets"]:
            labels = labels_for(ticket)
            lines.append(
                "| "
                + " | ".join(
                    [
                        jira_link(ticket),
                        report.display_name(labels.get("product_area", "unknown")),
                        report.display_name(labels.get("severity", "unknown")),
                        report.display_name(labels.get("root_cause", "unknown")),
                        report.ticket_summary(ticket).replace("|", "\\|"),
                    ]
                )
                + " |"
            )
    else:
        lines.append("No website bug tickets were found for this window.")

    lines.extend(
        [
            "",
            "# SUPERRARE SERVICES",
            "",
            "| Type | Count |",
            "|---|---:|",
        ]
    )
    services = weekly_report.get("service_ticket_counts") or []
    if services:
        for _label, count in services:
            lines.append(f"| Task | {count} |")
    else:
        lines.append("| Task | 0 |")

    lines.extend(
        [
            "",
            "# Ticket Detail",
            "",
            "| Ticket | Type | Area | Severity | Root Cause | Needs Eng | Summary |",
            "|---|---|---|---|---|---|---|",
            *ticket_rows(weekly_report.get("window_tickets", [])),
            "",
            "# Notes",
            "",
            "Non-website/service items are rolled up generically as Task under SUPERRARE SERVICES. Jira labels remain the source of truth for whether an inbound item counts as a website bug.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Slack Canvas markdown for the weekly Help dashboard.")
    parser.add_argument("--report-json", default="data/support_weekly_bug_report.json")
    parser.add_argument("--trend-input", default="data/support_trend_tickets.json")
    parser.add_argument("--output", default="data/support_weekly_bug_report_canvas.md")
    parser.add_argument("--lookback-weeks", type=int, default=5)
    args = parser.parse_args()

    weekly_report = json.loads(Path(args.report_json).read_text(encoding="utf-8"))
    trend_path = Path(args.trend_input)
    if trend_path.exists():
        trend_data = json.loads(trend_path.read_text(encoding="utf-8"))
    else:
        trend_data = {"tickets": weekly_report.get("window_tickets", [])}
    markdown = build_canvas_markdown(weekly_report, trend_data, args.lookback_weeks)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    print(f"Wrote Canvas markdown to {output_path}")


if __name__ == "__main__":
    main()
