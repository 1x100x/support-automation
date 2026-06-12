#!/usr/bin/env python3
"""Build a Data Analytics dashboard payload from a weekly Help report JSON."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from generate_support_bug_report import likely_code_paths, ticket_summary


def display_name(value: str) -> str:
    words = str(value or "unknown").replace("-", " ").replace("_", " ").split()
    return " ".join(word.capitalize() for word in words) or "Unknown"


def iso_now() -> str:
    return datetime.now().astimezone().isoformat()


def rows_from_pairs(items: List[List], key_name: str) -> List[Dict]:
    return [{key_name: str(label), "label": display_name(str(label)), "count": int(count)} for label, count in items]


def ticket_labels(ticket: Dict) -> Dict:
    labels = dict(ticket.get("suggested_labels") or {})
    labels.update(ticket.get("approved_labels") or {})
    return labels


def build_ticket_detail(report: Dict) -> List[Dict]:
    rows = []
    for ticket in report.get("window_tickets", []):
        labels = ticket_labels(ticket)
        issue_type = labels.get("issue_type", "unknown")
        needs_engineering = labels.get("needs_engineering", "unknown")
        likely_code_area = ", ".join(likely_code_paths(ticket))
        if issue_type != "bug":
            needs_engineering = "no"
        rows.append(
            {
                "key": ticket.get("key") or ticket.get("id") or "NO-KEY",
                "issue_type": issue_type,
                "product_area": labels.get("product_area", "unknown"),
                "severity": labels.get("severity", "unknown"),
                "root_cause": labels.get("root_cause", "unknown"),
                "needs_engineering": needs_engineering,
                "summary": ticket_summary(ticket),
                "likely_code_area": likely_code_area,
                "source_url": ticket.get("source_url") or "",
            }
        )
    return rows


def source_for_dataset(source: Dict, source_path: str, dataset: str, sql: str) -> Dict:
    return {
        "label": source["label"],
        "path": source_path,
        "query": {
            "description": f"Dashboard view over {dataset} from the generated weekly Help-board report.",
            "language": "sql",
            "sql": sql,
            "filters": source["query"]["filters"],
            "tables_used": [dataset],
            "metric_definitions": source["query"]["metric_definitions"],
        },
    }


def build_dashboard_payload(report: Dict, *, source_path: str, status: str = "ready") -> Dict:
    metadata = report.get("metadata", {})
    totals = report.get("totals", {})
    breakdowns = report.get("breakdowns", {})
    services = report.get("service_ticket_counts", [])
    service_tasks = sum(int(count) for _label, count in services)
    generated_at = metadata.get("generated_at") or iso_now()
    since = metadata.get("since", "")
    until = metadata.get("until", "")

    summary = [
        {
            "inbound": int(totals.get("inbound", 0)),
            "bugs": int(totals.get("bugs", 0)),
            "service_tasks": service_tasks,
            "needs_engineering": int(totals.get("needs_engineering", 0)),
            "low_confidence": int(totals.get("low_confidence", 0)),
        }
    ]
    issue_type_mix = rows_from_pairs(breakdowns.get("issue_type_all", []), "issue_type")
    product_areas = rows_from_pairs(breakdowns.get("product_area", []), "product_area")
    root_causes = rows_from_pairs(breakdowns.get("root_cause", []), "root_cause")
    ticket_detail = build_ticket_detail(report)

    source = {
        "id": "weekly-help-report-json",
        "label": "Generated weekly Help-board report JSON",
        "path": source_path,
        "query": {
            "description": "Generated weekly Help-board report pipeline output.",
            "language": "local_json",
            "filters": ["project = HELP", f"since = {since}", f"until = {until}"],
            "tables_used": [
                "Jira project HELP via scripts/fetch_jira_help_tickets.py",
                "scripts/support_triage.py",
                "scripts/generate_support_bug_report.py",
            ],
            "metric_definitions": [
                "Inbound tickets: all Help tickets created in the Friday-through-Thursday ET window.",
                "Website bugs: tickets whose final issue_type is bug after Jira label precedence rules.",
                "SuperRare services/tasks: non-bug support tickets grouped under the generic task bucket.",
                "Needs engineering: website bug tickets with needs_engineering = yes.",
            ],
        },
    }
    issue_mix_source = source_for_dataset(
        source,
        source_path,
        "issue_type_mix",
        "SELECT issue_type, label, count FROM issue_type_mix ORDER BY count DESC;",
    )
    product_area_source = source_for_dataset(
        source,
        source_path,
        "product_areas",
        "SELECT product_area, label, count FROM product_areas ORDER BY count DESC;",
    )
    root_cause_source = source_for_dataset(
        source,
        source_path,
        "root_causes",
        "SELECT root_cause, label, count FROM root_causes ORDER BY count DESC;",
    )
    ticket_detail_source = source_for_dataset(
        source,
        source_path,
        "ticket_detail",
        (
            "SELECT key, issue_type, product_area, severity, root_cause, needs_engineering, summary, likely_code_area "
            "FROM ticket_detail ORDER BY key;"
        ),
    )

    manifest = {
        "version": 1,
        "surface": "dashboard",
        "title": "Weekly Help Bug Report Dashboard",
        "description": "Visual companion to the Friday Help-board Slack report.",
        "generatedAt": generated_at,
        "sources": [source],
        "cards": [
            {
                "id": "card-inbound",
                "dataset": "summary",
                "description": "All Help tickets in the weekly reporting window.",
                "metrics": [{"label": "Inbound tickets", "field": "inbound", "format": "number"}],
            },
            {
                "id": "card-bugs",
                "dataset": "summary",
                "description": "Website bugs after Jira label overrides and support exclusions.",
                "metrics": [{"label": "Website bugs", "field": "bugs", "format": "number"}],
            },
            {
                "id": "card-services",
                "dataset": "summary",
                "description": "Non-website items grouped under SuperRare Services as generic tasks.",
                "metrics": [{"label": "Service tasks", "field": "service_tasks", "format": "number"}],
            },
            {
                "id": "card-needs-eng",
                "dataset": "summary",
                "description": "Website bug tickets requiring engineering follow-up.",
                "metrics": [{"label": "Needs engineering", "field": "needs_engineering", "format": "number"}],
            },
        ],
        "charts": [
            {
                "id": "chart-issue-mix",
                "title": "Inbound Ticket Mix",
                "subtitle": "Non-website service items are grouped as tasks.",
                "type": "bar",
                "dataset": "issue_type_mix",
                "source": issue_mix_source,
                "encodings": {
                    "x": {"field": "label", "type": "nominal", "label": "Ticket type"},
                    "y": {"field": "count", "type": "quantitative", "label": "Tickets"},
                },
                "yAxisTitle": "Tickets",
            },
            {
                "id": "chart-product-area",
                "title": "Website Bugs by Product Area",
                "subtitle": "Product-area grouping uses final Jira label precedence.",
                "type": "bar",
                "dataset": "product_areas",
                "source": product_area_source,
                "encodings": {
                    "x": {"field": "label", "type": "nominal", "label": "Product area"},
                    "y": {"field": "count", "type": "quantitative", "label": "Website bugs"},
                },
                "yAxisTitle": "Website bugs",
            },
            {
                "id": "chart-root-cause",
                "title": "Website Bugs by Root Cause",
                "subtitle": "Root-cause buckets help decide follow-up ownership.",
                "type": "bar",
                "dataset": "root_causes",
                "source": root_cause_source,
                "encodings": {
                    "x": {"field": "label", "type": "nominal", "label": "Root cause"},
                    "y": {"field": "count", "type": "quantitative", "label": "Website bugs"},
                },
                "yAxisTitle": "Website bugs",
            },
        ],
        "tables": [
            {
                "id": "table-ticket-detail",
                "title": "Weekly Ticket Detail",
                "subtitle": "Operational lookup table for the Friday Slack post.",
                "dataset": "ticket_detail",
                "density": "dense",
                "source": ticket_detail_source,
                "columns": [
                    {"field": "key", "label": "Ticket", "type": "text"},
                    {"field": "issue_type", "label": "Type", "type": "text"},
                    {"field": "product_area", "label": "Area", "type": "text"},
                    {"field": "severity", "label": "Severity", "type": "text"},
                    {"field": "root_cause", "label": "Root Cause", "type": "text"},
                    {"field": "needs_engineering", "label": "Needs Eng", "type": "text"},
                    {"field": "summary", "label": "Summary", "type": "text"},
                    {"field": "likely_code_area", "label": "Likely Code Area", "type": "text"},
                ],
            }
        ],
        "blocks": [
            {
                "id": "intro",
                "type": "markdown",
                "body": (
                    "Visual companion to the Friday Slack bug report. "
                    "The dashboard uses the generated weekly Help-board report JSON and applies the same Jira label precedence rules."
                ),
            },
            {"id": "kpis", "type": "metric-strip", "cardIds": ["card-inbound", "card-bugs", "card-services", "card-needs-eng"], "layout": "full"},
            {"id": "issue-mix", "type": "chart", "chartId": "chart-issue-mix", "layout": "half"},
            {"id": "product-area", "type": "chart", "chartId": "chart-product-area", "layout": "half"},
            {"id": "root-cause", "type": "chart", "chartId": "chart-root-cause", "layout": "half"},
            {"id": "ticket-detail", "type": "table", "tableId": "table-ticket-detail", "layout": "full"},
        ],
    }
    snapshot = {
        "version": 1,
        "generatedAt": generated_at,
        "status": status,
        "datasets": {
            "summary": summary,
            "issue_type_mix": issue_type_mix,
            "product_areas": product_areas,
            "root_causes": root_causes,
            "ticket_detail": ticket_detail,
        },
    }
    return {"manifest": manifest, "snapshot": snapshot}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a dashboard payload from a weekly Help report JSON.")
    parser.add_argument("--input", default="data/support_weekly_bug_report.json")
    parser.add_argument("--output", default="data/support_weekly_bug_report_dashboard.json")
    parser.add_argument("--status", choices=["ready", "fixture"], default="ready")
    args = parser.parse_args()

    input_path = Path(args.input)
    report = json.loads(input_path.read_text(encoding="utf-8"))
    payload = build_dashboard_payload(report, source_path=str(input_path), status=args.status)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote dashboard payload to {output_path}")


if __name__ == "__main__":
    main()
