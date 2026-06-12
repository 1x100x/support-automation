#!/usr/bin/env python3
"""Generate a reviewable weekly bug report from normalized Support Dashboard JSON."""

import argparse
import json
import os
import re
import urllib.parse
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
ETH_ADDRESS_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
SUMMARY_STOP_RE = re.compile(
    r"\b(?:links?|attachments?|notes?|profile|contract|be cautious)\s*:",
    re.IGNORECASE,
)
FILLER_RE = re.compile(
    r"\b(?:hello|hi|hey|please|thank you|thanks|btw|i hope you can help me|could you please|can you help me understand what has happened)\b",
    re.IGNORECASE,
)
USERNAME_PREFIX_RE = re.compile(r"^@\S+\s*-\s*", re.IGNORECASE)
NON_CODE_PATH_RE = re.compile(r"(^|/)(references?|docs?|notes?|projects?)(/|$)|(^|/)\.env|\.md$", re.IGNORECASE)


def parse_dt(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def labels_for(ticket: Dict) -> Dict:
    labels = dict(ticket.get("suggested_labels") or {})
    labels.update(ticket.get("approved_labels") or {})
    return labels


def in_window(ticket: Dict, since: datetime, until: datetime) -> bool:
    created = parse_dt(ticket.get("created_at", ""))
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    created_et = created.astimezone(ET)
    return since <= created_et <= until


def is_bug(ticket: Dict) -> bool:
    labels = labels_for(ticket)
    if labels.get("issue_type") == "bug":
        return True
    if labels.get("issue_type") in {"user-error", "question", "account", "task", "ops", "content"}:
        return False
    flat = ticket.get("approved_flat_labels") or ticket.get("suggested_flat_labels") or []
    return "bug" in flat


def count_by(tickets: Iterable[Dict], key: str) -> Counter:
    counter = Counter()
    for ticket in tickets:
        counter[labels_for(ticket).get(key, "unknown")] += 1
    return counter


def display_name(value: str) -> str:
    words = str(value or "unknown").replace("-", " ").replace("_", " ").split()
    return " ".join(word.capitalize() for word in words) or "Unknown"


def ticket_line(ticket: Dict) -> str:
    labels = labels_for(ticket)
    key = ticket.get("key") or ticket.get("id") or "NO-KEY"
    severity = labels.get("severity", "unknown")
    area = labels.get("product_area", "unknown")
    cause = labels.get("root_cause", "unknown")
    confidence = ticket.get("confidence_band", "unknown")
    title = ticket.get("title", "").strip()
    return f"- `{key}` [{severity}] {title} | area: `{area}` | root cause: `{cause}` | confidence: `{confidence}`"


def top_items(counter: Counter, limit: int = 5) -> List[Tuple[str, int]]:
    return counter.most_common(limit)


def ordinal(value: int) -> str:
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def format_day(value: str) -> str:
    dt = datetime.fromisoformat(value).astimezone(ET)
    return f"{dt.strftime('%B')} {ordinal(dt.day)}"


def format_report_date(value: str) -> str:
    dt = datetime.fromisoformat(value).astimezone(ET)
    return f"{dt.month}/{dt.day}/{str(dt.year)[-2:]}"


def format_post_date(value: str) -> str:
    dt = datetime.fromisoformat(value).astimezone(ET)
    return f"{dt.month}/{dt.day}/{dt.year}"


def report_month(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(ET).strftime("%B")


def plural(count: int, singular: str, plural_value: str | None = None) -> str:
    return singular if count == 1 else plural_value or f"{singular}s"


def breakdown_phrase(counter_items: List[Tuple[str, int]]) -> str:
    parts = []
    for label, count in counter_items:
        name = display_name(label).lower()
        if label == "bug":
            name = "bug inbound"
        parts.append(f"{count} {name}")
    if not parts:
        return "0 inbound tickets"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"


def report_issue_type(ticket: Dict) -> str:
    issue_type = labels_for(ticket).get("issue_type", "unknown")
    if issue_type == "bug":
        return "bug"
    return "task"


def count_report_issue_types(tickets: Iterable[Dict]) -> Counter:
    counter = Counter()
    for ticket in tickets:
        counter[report_issue_type(ticket)] += 1
    return counter


def friday_to_thursday_window(now: datetime | None = None) -> Tuple[datetime, datetime]:
    now_et = (now or datetime.now(ET)).astimezone(ET)
    days_since_thursday = (now_et.weekday() - 3) % 7
    if days_since_thursday == 0 and now_et.time() < datetime.max.time().replace(microsecond=0):
        days_since_thursday = 7
    window_end_day = now_et - timedelta(days=days_since_thursday)
    window_end = window_end_day.replace(hour=23, minute=59, second=59, microsecond=0)
    window_start = (window_end - timedelta(days=6)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return window_start, window_end


def sanitize_slack_text(value: str) -> str:
    text = EMAIL_RE.sub("[redacted email]", value)
    text = ETH_ADDRESS_RE.sub("[redacted address]", text)
    return URL_RE.sub("", text)


def clean_summary_text(value: str) -> str:
    text = sanitize_slack_text(value)
    text = FILLER_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,:;-")


def issue_detail_text(ticket: Dict) -> str:
    text = " ".join(str(ticket.get("description") or "").split())
    if not text:
        return ""

    lower_text = text.lower()
    start = lower_text.find("issue details:")
    if start >= 0:
        text = text[start + len("issue details:") :]
    text = SUMMARY_STOP_RE.split(text, maxsplit=1)[0]
    return " ".join(text.split())


def title_context(ticket: Dict) -> str:
    title = str(ticket.get("title") or "").strip()
    title = re.sub(r"\s*-\s*\[AIRTABLE\]\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^\[[^\]]+\]\s*", "", title)
    return " ".join(title.split())


def concise_title(ticket: Dict) -> str:
    title = title_context(ticket)
    title = USERNAME_PREFIX_RE.sub("", title)
    return clean_summary_text(title)


def condensed_sentences(text: str) -> List[str]:
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    if not parts and text:
        parts = [text]
    cleaned = []
    for part in parts:
        normalized = clean_summary_text(part)
        if normalized:
            cleaned.append(normalized)
    return cleaned


def compress_detail_sentence(text: str) -> str:
    clauses = [clause.strip(" ,") for clause in re.split(r",\s+|;\s+", text) if clause.strip()]
    if not clauses:
        return text
    kept = clauses[:2]
    joined = "; ".join(kept)
    return clean_summary_text(joined)


def ticket_summary(ticket: Dict, limit: int = 260) -> str:
    llm_summary = clean_summary_text(str(ticket.get("llm_summary") or ""))
    if llm_summary:
        return llm_summary[: limit - 1].rstrip() + "..." if len(llm_summary) > limit else llm_summary

    details = issue_detail_text(ticket)
    title = concise_title(ticket)
    detail_sentences = condensed_sentences(details)
    summary_parts: List[str] = []
    if title:
        summary_parts.append(title)
    if detail_sentences:
        primary = compress_detail_sentence(detail_sentences[0])
        if not title or primary.lower() not in title.lower():
            summary_parts.append(primary)
        if len(detail_sentences) > 1 and len(summary_parts) < 2:
            follow_up = compress_detail_sentence(detail_sentences[1])
            joined = " ".join(summary_parts).lower()
            if follow_up and follow_up.lower() not in joined:
                summary_parts.append(follow_up)

    text = ": ".join(
        [summary_parts[0].rstrip(".")] + ["; ".join(part.rstrip(".") for part in summary_parts[1:])]
    ).strip() if summary_parts else ""
    if not text and title:
        text = clean_summary_text(title)
    if not text:
        text = "No summary provided."
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def llm_context_for_ticket(ticket: Dict) -> Dict:
    labels = labels_for(ticket)
    return {
        "key": ticket.get("key") or ticket.get("id") or "",
        "title": sanitize_slack_text(str(ticket.get("title") or "")),
        "description": sanitize_slack_text(issue_detail_text(ticket) or str(ticket.get("description") or "")),
        "status": sanitize_slack_text(str(ticket.get("status") or "")),
        "current_jira_labels": ticket.get("current_labels")
        or ticket.get("current_jira_labels")
        or ticket.get("jira_labels")
        or [],
        "suggested_labels": {
            "product_area": labels.get("product_area", "unknown"),
            "issue_type": labels.get("issue_type", "unknown"),
            "severity": labels.get("severity", "unknown"),
            "root_cause": labels.get("root_cause", "unknown"),
        },
    }


def extract_openai_text(result: Dict) -> str:
    if result.get("output_text"):
        return str(result["output_text"]).strip()
    for item in result.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                return str(content["text"]).strip()
    choices = result.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        if message.get("content"):
            return str(message["content"]).strip()
    return ""


def parse_json_object(text: str) -> Dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def http_error_message(exc: urllib.error.HTTPError) -> str:
    body = exc.read().decode("utf-8", errors="replace")
    if not body:
        return f"HTTP {exc.code}: {exc.reason}"
    return sanitize_slack_text(body)[:1000]


def llm_ticket_summaries(tickets: List[Dict], *, api_key: str, model: str, timeout: int = 45) -> Dict[str, str]:
    contexts = [llm_context_for_ticket(ticket) for ticket in tickets]
    prompt = (
        "Write one cohesive support-report summary per Jira Help ticket. "
        "Focus on the actual issue the customer is having and any useful observed behavior. "
        "Each summary must be 25 to 45 words. Do not include emails, wallet addresses, raw URLs, "
        "markdown, ticket keys, or root-cause claims unless the customer explicitly described the cause. "
        "Return only JSON shaped exactly like: "
        "{\"summaries\":[{\"key\":\"HELP-123\",\"summary\":\"...\"}]}"
    )
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "You summarize customer support tickets for a weekly product and engineering "
                    "bug report. Be specific, neutral, concise, and privacy preserving."
                ),
            },
            {
                "role": "user",
                "content": f"{prompt}\n\nTicket JSON array:\n{json.dumps(contexts, ensure_ascii=True)}",
            },
        ],
        "max_output_tokens": max(300, 120 * len(tickets)),
    }
    req = urllib.request.Request(
        url="https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    parsed = parse_json_object(extract_openai_text(result))
    summaries = {}
    for item in parsed.get("summaries", []):
        key = str(item.get("key") or "").strip()
        summary = clean_summary_text(str(item.get("summary") or ""))
        if key and summary:
            summaries[key] = summary
    return summaries


def llm_ticket_summary(ticket: Dict, *, api_key: str, model: str, timeout: int = 30) -> str:
    key = ticket.get("key") or ticket.get("id") or ""
    summaries = llm_ticket_summaries([ticket], api_key=api_key, model=model, timeout=timeout)
    return summaries.get(key) or ticket_summary({**ticket, "llm_summary": ""})


def add_llm_summaries(tickets: List[Dict]) -> Tuple[int, str, str]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY is not set; using heuristic ticket summaries.")
        return 0, "heuristic", "OPENAI_API_KEY is not set."

    model = os.getenv("OPENAI_SUMMARY_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    try:
        summaries = llm_ticket_summaries(tickets, api_key=api_key, model=model)
    except urllib.error.HTTPError as exc:
        message = http_error_message(exc)
        print(f"::warning title=LLM summaries failed::OpenAI request failed: {message}")
        for ticket in tickets:
            ticket["summary_source"] = "heuristic-fallback"
        return 0, "heuristic", message
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        message = sanitize_slack_text(str(exc))[:1000]
        print(f"::warning title=LLM summaries failed::OpenAI request failed: {message}")
        for ticket in tickets:
            ticket["summary_source"] = "heuristic-fallback"
        return 0, "heuristic", message

    generated = 0
    for ticket in tickets:
        key = ticket.get("key") or ticket.get("id") or ""
        summary = summaries.get(key)
        if summary:
            ticket["llm_summary"] = summary
            ticket["summary_source"] = "llm"
            generated += 1
        else:
            ticket["summary_source"] = "heuristic-fallback"
    if generated < len(tickets):
        missing = len(tickets) - generated
        message = f"LLM returned summaries for {generated} of {len(tickets)} tickets; {missing} used heuristic fallback."
        print(f"::warning title=Partial LLM summaries::{message}")
        return generated, "partial-llm", message
    return generated, "llm", ""


def likely_code_paths(ticket: Dict) -> List[str]:
    repo_signal = ticket.get("repo_signal") or {}
    paths = repo_signal.get("paths") or []
    filtered = [str(path) for path in paths if not NON_CODE_PATH_RE.search(str(path))]
    return filtered[:2]


def likely_code_area(ticket: Dict) -> str:
    return ", ".join(likely_code_paths(ticket))


def grouped_by_area(tickets: List[Dict]) -> Dict[str, List[Dict]]:
    grouped: Dict[str, List[Dict]] = {}
    for ticket in tickets:
        area = labels_for(ticket).get("product_area", "unknown")
        grouped.setdefault(area, []).append(ticket)
    return dict(sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])))


def issue_link(ticket: Dict) -> str:
    key = ticket.get("key") or ticket.get("id") or "NO-KEY"
    url = ticket.get("source_url")
    if url:
        return f"<{url}|{key}>"
    return f"`{key}`"


def jira_search_link(tickets: List[Dict], fallback_base_url: str = "https://superrare.atlassian.net") -> str:
    keys = [ticket.get("key") or ticket.get("id") for ticket in tickets if ticket.get("key") or ticket.get("id")]
    if not keys:
        return fallback_base_url.rstrip("/")
    base_url = fallback_base_url.rstrip("/")
    first_url = next((ticket.get("source_url") for ticket in tickets if ticket.get("source_url")), "")
    parsed = urllib.parse.urlparse(first_url)
    if parsed.scheme and parsed.netloc:
        base_url = f"{parsed.scheme}://{parsed.netloc}"
    jql = f"project IN (HELP) AND key in ({','.join(keys)})"
    return f"{base_url}/issues/?jql={urllib.parse.quote(jql)}"


def services_by_type(tickets: List[Dict]) -> Counter:
    counter = Counter()
    for ticket in tickets:
        counter["task"] += 1
    return counter


def markdown_report(payload: Dict) -> str:
    totals = payload["totals"]
    metadata = payload["metadata"]
    issue_breakdown = payload["breakdowns"]["issue_type_all"]
    window_text = f"{format_day(metadata['since'])} to {format_day(metadata['until'])}"
    date_text = format_post_date(metadata["generated_at"])
    month = report_month(metadata["until"])
    jira_month_url = metadata.get("dashboard_url") or "https://superrare.atlassian.net/jira/dashboards/10004?maximized=10234"
    jira_issue_url = jira_search_link(payload["window_tickets"], metadata.get("jira_base_url") or "https://superrare.atlassian.net")
    lines = [
        "--------------------------------------------------------",
        f":jira-intensifies: :bug: *WEEKLY BUG REPORT {date_text}* :bug::jira-intensifies:",
        "--------------------------------------------------------",
        "",
        f"Weekly bug and task breakdown for the month of <{jira_month_url}|{month}>.",
        "",
        ":mag: *Overview of Bug Trends* :mag:",
        "",
        ":sr-avatar: *SUPERRARE WEBSITE*",
        "",
        (
            f"For the week of {window_text}, we received {totals['inbound']} "
            f"*{plural(totals['inbound'], 'inbound ticket')}; {breakdown_phrase(issue_breakdown)}*"
        ),
        "",
    ]

    if payload["bug_tickets_by_area"]:
        for area, tickets in payload["bug_tickets_by_area"].items():
            lines.append(f":mag_right: *{display_name(area)} Issues ({len(tickets)} {plural(len(tickets), 'Ticket')})*")
            lines.append("")
            for ticket in tickets:
                labels = labels_for(ticket)
                reporter = ticket.get("reporter") or "Unknown"
                title = ticket.get("title") or "Untitled ticket"
                lines.extend(
                    [
                        f"• Reported By: {reporter}",
                        f"Ticket: {issue_link(ticket)} - {title}",
                        f"Summary: {ticket_summary(ticket)}",
                        f"Severity: `{labels.get('severity', 'unknown')}` | Possible root cause: `{labels.get('root_cause', 'unknown')}`",
                        *([f"Likely code area: `{likely_code_area(ticket)}`"] if likely_code_area(ticket) else []),
                        "",
                    ]
                )
    else:
        lines.extend(["No website bug tickets were found for this window.", ""])

    lines.extend(["", f":bellhop_bell: *<{jira_issue_url}|SUPERRARE SERVICES>*", ""])
    services = payload["service_ticket_counts"]
    if services:
        lines.extend([f"{display_name(label)} ({count})" for label, count in services])
    else:
        lines.append("- No account support, task, or service tickets were found for this window.")

    lines.extend(
        [
            "",
            "*Root-Cause Follow-Up*",
            "",
        ]
    )
    if payload["root_cause_review"]:
        lines.extend(ticket_line(ticket) for ticket in payload["root_cause_review"])
    else:
        lines.append("No low-confidence or unknown possible root causes found.")

    lines.extend(
        [
            "",
            "*Dashboard Signals*",
            "",
            f"- Needs engineering: {totals['needs_engineering']}",
            f"- Low-confidence triage: {totals['low_confidence']}",
            "- Top product areas: "
            + (", ".join(f"`{label}` ({count})" for label, count in payload["breakdowns"]["product_area"]) or "none"),
            "- Top root causes: "
            + (", ".join(f"`{label}` ({count})" for label, count in payload["breakdowns"]["root_cause"]) or "none"),
            "",
            "Feel free to share feedback or call out anything else you want tracked in the dashboard.",
            "",
        ]
    )
    return "\n".join(lines)


def legacy_markdown_report(payload: Dict) -> str:
    lines = [
        "# Weekly Help Board Bug Report",
        "",
        f"Generated: {payload['metadata']['generated_at']}",
        f"Window: {payload['metadata']['since']} through {payload['metadata']['until']}",
        "",
        "## Summary",
        "",
        f"- Total inbound tickets in window: {payload['totals']['inbound']}",
        f"- Bug tickets in window: {payload['totals']['bugs']}",
        f"- Needs engineering: {payload['totals']['needs_engineering']}",
        f"- Low-confidence triage: {payload['totals']['low_confidence']}",
        "",
        "## Top Product Areas",
        "",
    ]
    lines.extend([f"- `{label}`: {count}" for label, count in payload["breakdowns"]["product_area"]])
    lines.extend(["", "## Top Root Causes", ""])
    lines.extend([f"- `{label}`: {count}" for label, count in payload["breakdowns"]["root_cause"]])
    lines.extend(["", "## Severity Mix", ""])
    lines.extend([f"- `{label}`: {count}" for label, count in payload["breakdowns"]["severity"]])
    lines.extend(["", "## Bugs To Review", ""])
    if payload["bug_tickets"]:
        lines.extend(ticket_line(ticket) for ticket in payload["bug_tickets"])
    else:
        lines.append("- No bug tickets found in this window.")
    lines.extend(["", "## Root-Cause Follow-Up", ""])
    if payload["root_cause_review"]:
        lines.extend(ticket_line(ticket) for ticket in payload["root_cause_review"])
    else:
        lines.append("- No low-confidence or unknown-root-cause bugs found.")
    lines.append("")
    return "\n".join(lines)


def build_report(data: Dict, days: int, friday_window: bool, use_llm_summaries: bool = False) -> Dict:
    if friday_window:
        since, until = friday_to_thursday_window()
    else:
        until = datetime.now(ET)
        since = until - timedelta(days=days)
    tickets = data.get("tickets", [])
    window_tickets = [dict(ticket) for ticket in tickets if in_window(ticket, since, until)]
    llm_summary_count = 0
    summary_source = "heuristic"
    summary_error = ""
    if use_llm_summaries and window_tickets:
        llm_summary_count, summary_source, summary_error = add_llm_summaries(window_tickets)
    bug_tickets = [ticket for ticket in window_tickets if is_bug(ticket)]
    service_tickets = [ticket for ticket in window_tickets if not is_bug(ticket)]
    root_cause_review = [
        ticket
        for ticket in bug_tickets
        if labels_for(ticket).get("root_cause", "unknown") == "unknown"
        or ticket.get("confidence_band") == "low"
    ]
    needs_engineering = [
        ticket for ticket in bug_tickets if labels_for(ticket).get("needs_engineering") == "yes"
    ]
    source_metadata = data.get("metadata", {}).get("source_metadata", {})
    return {
        "metadata": {
            "generated_at": datetime.now(ET).isoformat(),
            "since": since.isoformat(),
            "until": until.isoformat(),
            "timezone": "America/New_York",
            "window": "previous Friday through Thursday 11:59 PM ET"
            if friday_window
            else f"last {days} days",
            "source_workflow": data.get("metadata", {}).get("workflow", "unknown"),
            "jira_base_url": source_metadata.get("base_url", ""),
            "dashboard_url": os.getenv("JIRA_HELP_DASHBOARD_URL", ""),
            "summary_source": summary_source,
            "llm_summary_count": llm_summary_count,
            "summary_error": summary_error,
            "write_policy": "No Jira or GitHub writes are performed by this script. Slack posting requires --post-slack.",
        },
        "totals": {
            "inbound": len(window_tickets),
            "bugs": len(bug_tickets),
            "needs_engineering": len(needs_engineering),
            "low_confidence": sum(1 for ticket in bug_tickets if ticket.get("confidence_band") == "low"),
        },
        "breakdowns": {
            "product_area": top_items(count_by(bug_tickets, "product_area")),
            "root_cause": top_items(count_by(bug_tickets, "root_cause")),
            "severity": top_items(count_by(bug_tickets, "severity")),
            "issue_type_all": top_items(count_report_issue_types(window_tickets), limit=8),
        },
        "bug_tickets": bug_tickets,
        "window_tickets": window_tickets,
        "bug_tickets_by_area": grouped_by_area(bug_tickets),
        "service_ticket_counts": top_items(services_by_type(service_tickets), limit=8),
        "root_cause_review": root_cause_review,
    }


def slack_text_from_report(markdown: str) -> str:
    text = markdown
    text = text.replace("# ", "*").replace("## ", "*").replace("### ", "*")
    lines = []
    for line in text.splitlines():
        if line.startswith("*") and not line.endswith("*"):
            line = f"{line}*"
        lines.append(line)
    text = "\n".join(lines)
    return text[:39000]


def post_to_slack(markdown: str, channel: str) -> Dict:
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Missing SLACK_BOT_TOKEN. Refusing to post.")
    if not channel:
        raise SystemExit("Missing Slack channel. Set SLACK_REPORT_CHANNEL_ID or pass --slack-channel.")
    payload = {
        "channel": channel,
        "text": slack_text_from_report(markdown),
        "unfurl_links": False,
        "unfurl_media": False,
    }
    req = urllib.request.Request(
        url="https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Slack post failed with HTTP {exc.code}: {body}") from exc
    if not result.get("ok"):
        raise SystemExit(f"Slack post failed: {result.get('error', 'unknown_error')}")
    return {"channel": result.get("channel"), "ts": result.get("ts")}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a weekly Help Board bug report.")
    parser.add_argument("--input", required=True, help="Normalized support dashboard JSON.")
    parser.add_argument("--output-json", default="data/support_weekly_bug_report.json")
    parser.add_argument("--output-md", default="data/support_weekly_bug_report.md")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days.")
    parser.add_argument(
        "--friday-window",
        action="store_true",
        help="Use previous Friday 12:00 AM ET through Thursday 11:59:59 PM ET.",
    )
    parser.add_argument(
        "--post-slack",
        action="store_true",
        help="Post the report to Slack using SLACK_BOT_TOKEN and a configured channel.",
    )
    parser.add_argument(
        "--slack-channel",
        default=os.getenv("SLACK_REPORT_CHANNEL_ID", ""),
        help="Slack channel ID for posting. Defaults to SLACK_REPORT_CHANNEL_ID.",
    )
    parser.add_argument(
        "--use-llm-summaries",
        action="store_true",
        help="Use OPENAI_API_KEY to generate cohesive ticket summaries before falling back to local heuristics.",
    )
    args = parser.parse_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    use_llm_summaries = args.use_llm_summaries or os.getenv("SUPPORT_USE_LLM_SUMMARIES", "").lower() in {
        "1",
        "true",
        "yes",
    }
    report = build_report(data, args.days, args.friday_window, use_llm_summaries=use_llm_summaries)
    json_path = Path(args.output_json)
    md_path = Path(args.output_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = markdown_report(report)
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    print(f"Wrote bug report JSON to {json_path}")
    print(f"Wrote bug report Markdown to {md_path}")
    if args.post_slack:
        result = post_to_slack(markdown, args.slack_channel.strip())
        print(f"Posted bug report to Slack channel {result['channel']} at {result['ts']}")


if __name__ == "__main__":
    main()
