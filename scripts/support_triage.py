#!/usr/bin/env python3
"""Normalize inbound Help/Jira tickets and add review-first triage suggestions."""

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse


TAXONOMY = {
    "product_area": [
        "auth",
        "checkout",
        "creator-tools",
        "marketplace",
        "profile",
        "releases",
        "search",
        "wallet",
        "unknown",
    ],
    "issue_type": ["bug", "user-error", "question", "feature-request", "ops", "content", "account", "task"],
    "root_cause": [
        "auth-session",
        "cache-state",
        "contract-indexing",
        "data-mismatch",
        "payment-provider",
        "permissions",
        "third-party",
        "ui-copy",
        "unknown",
    ],
    "severity": ["critical", "high", "medium", "low"],
    "customer_impact": ["blocked", "degraded", "confusing", "cosmetic", "unknown"],
    "source": ["jira-help", "slack", "manual", "unknown"],
    "needs_engineering": ["yes", "no", "unknown"],
}

LABEL_ALIASES = {
    "issue_type": {
        "bug": "bug",
        "bugs": "bug",
        "user-error": "user-error",
        "user_error": "user-error",
        "not-a-bug": "user-error",
        "not_bug": "user-error",
        "account-support": "account",
        "account_support": "account",
        "account": "account",
        "question": "question",
        "support-question": "question",
        "task": "task",
        "tasks": "task",
        "ops": "ops",
        "content": "content",
        "feature-request": "feature-request",
    },
    "needs_engineering": {
        "needs-eng-yes": "yes",
        "needs-engineering": "yes",
        "engineering": "yes",
        "needs-eng-no": "no",
        "no-eng": "no",
    },
}


RULES = [
    {
        "terms": ["login", "sign in", "signin", "privy", "session", "email code", "otp"],
        "labels": {
            "product_area": "auth",
            "issue_type": "bug",
            "root_cause": "auth-session",
            "needs_engineering": "yes",
        },
        "repo_terms": ["auth", "privy", "session", "login"],
    },
    {
        "terms": ["credit card", "coinflow", "checkout", "payment", "usdc", "buy", "purchase"],
        "labels": {
            "product_area": "checkout",
            "issue_type": "bug",
            "root_cause": "payment-provider",
            "needs_engineering": "yes",
        },
        "repo_terms": ["checkout", "payment", "coinflow", "purchase"],
    },
    {
        "terms": ["profile", "avatar", "creator page", "favorites", "owned", "collected"],
        "labels": {
            "product_area": "profile",
            "issue_type": "bug",
            "root_cause": "data-mismatch",
            "needs_engineering": "yes",
        },
        "repo_terms": ["profile", "user", "favorites"],
    },
    {
        "terms": ["search", "typesense", "filter", "results", "trending"],
        "labels": {
            "product_area": "search",
            "issue_type": "bug",
            "root_cause": "data-mismatch",
            "needs_engineering": "yes",
        },
        "repo_terms": ["search", "typesense", "index"],
    },
    {
        "terms": ["mint", "release", "edition", "claim", "drop"],
        "labels": {
            "product_area": "releases",
            "issue_type": "bug",
            "root_cause": "contract-indexing",
            "needs_engineering": "yes",
        },
        "repo_terms": ["mint", "release", "edition", "drop"],
    },
    {
        "terms": ["wallet", "metamask", "ledger", "connect wallet", "address"],
        "labels": {
            "product_area": "wallet",
            "issue_type": "bug",
            "root_cause": "permissions",
            "needs_engineering": "yes",
        },
        "repo_terms": ["wallet", "address", "provider"],
    },
    {
        "terms": ["copy", "wording", "confusing", "button", "label", "text"],
        "labels": {
            "product_area": "marketplace",
            "issue_type": "content",
            "root_cause": "ui-copy",
            "customer_impact": "confusing",
            "needs_engineering": "no",
        },
        "repo_terms": ["copy", "label", "button"],
    },
]

SEVERITY_RULES = [
    ("critical", ["outage", "down", "all users", "cannot access", "security", "exploit"]),
    ("high", ["blocked", "cannot", "failed", "error", "broken", "stuck", "urgent"]),
    ("medium", ["wrong", "missing", "slow", "inconsistent", "confusing"]),
]

IMPACT_RULES = [
    ("blocked", ["blocked", "cannot", "unable", "failed", "stuck"]),
    ("degraded", ["slow", "intermittent", "wrong", "missing", "broken"]),
    ("confusing", ["confusing", "unclear", "copy", "wording", "expected"]),
    ("cosmetic", ["spacing", "style", "visual", "alignment", "color"]),
]


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_label(label: str) -> str:
    cleaned = normalize_space(label).lower().replace(" ", "-")
    return "".join(ch for ch in cleaned if ch.isalnum() or ch in "-_")[:255]


def dedupe(items: Iterable[str]) -> List[str]:
    result = []
    seen = set()
    for item in items:
        normalized = normalize_label(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def plain_from_adf(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(plain_from_adf(item) for item in value)
    if not isinstance(value, dict):
        return ""
    parts = []
    text = value.get("text")
    if text:
        parts.append(str(text))
    for child in value.get("content", []) or []:
        child_text = plain_from_adf(child)
        if child_text:
            parts.append(child_text)
    return normalize_space(" ".join(parts))


def parse_date(value: str) -> str:
    value = normalize_space(value)
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


def extract_reporter(fields: Dict) -> str:
    for key in ("reporter", "creator", "requestType"):
        value = fields.get(key)
        if isinstance(value, dict):
            return normalize_space(
                value.get("displayName")
                or value.get("emailAddress")
                or value.get("name")
                or value.get("key")
            )
        if isinstance(value, str):
            return normalize_space(value)
    return ""


def browser_url(issue: Dict, metadata: Dict) -> str:
    key = normalize_space(issue.get("key"))
    if not key:
        return normalize_space(issue.get("self") or issue.get("source_url"))
    base_url = normalize_space(metadata.get("base_url"))
    if not base_url:
        parsed = urlparse(normalize_space(issue.get("self")))
        if parsed.scheme and parsed.netloc:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
    if base_url:
        return f"{base_url.rstrip('/')}/browse/{key}"
    return normalize_space(issue.get("self") or issue.get("source_url"))


def jira_issue_to_ticket(issue: Dict, metadata: Dict | None = None) -> Dict:
    metadata = metadata or {}
    fields = issue.get("fields", {}) if isinstance(issue.get("fields"), dict) else {}
    key = normalize_space(issue.get("key") or fields.get("key"))
    description = plain_from_adf(fields.get("description") or issue.get("description"))
    labels = fields.get("labels") if isinstance(fields.get("labels"), list) else issue.get("labels", [])
    status = fields.get("status") or issue.get("status") or {}
    status_name = status.get("name") if isinstance(status, dict) else status
    return {
        "id": key or normalize_space(issue.get("id")),
        "key": key,
        "title": normalize_space(fields.get("summary") or issue.get("title") or issue.get("summary")),
        "description": description,
        "status": normalize_space(status_name),
        "created_at": parse_date(fields.get("created") or issue.get("created_at") or issue.get("created")),
        "reporter": extract_reporter(fields) or normalize_space(issue.get("reporter")),
        "current_labels": dedupe(labels or []),
        "source_url": browser_url(issue, metadata),
        "source": "jira-help",
    }


def load_tickets(path: Path) -> Tuple[List[Dict], Dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [jira_issue_to_ticket(item) for item in data], {}
    if isinstance(data, dict) and isinstance(data.get("issues"), list):
        metadata = data.get("metadata", {}) if isinstance(data.get("metadata"), dict) else {}
        return [jira_issue_to_ticket(item, metadata) for item in data["issues"]], {
            "jira_total": data.get("total"),
            **metadata,
        }
    if isinstance(data, dict) and isinstance(data.get("tickets"), list):
        metadata = data.get("metadata", {}) if isinstance(data.get("metadata"), dict) else {}
        return [jira_issue_to_ticket(item, metadata) for item in data["tickets"]], metadata
    raise SystemExit("Expected Jira search JSON {issues:[...]}, {tickets:[...]}, or a list of issues.")


def tokenize(text: str) -> Set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower())
        if word not in {"the", "and", "for", "with", "from", "that", "this", "into"}
    }


def choose_by_rules(text: str, rules: Sequence[Tuple[str, Sequence[str]]], default: str) -> str:
    lower = text.lower()
    for label, terms in rules:
        if any(term in lower for term in terms):
            return label
    return default


def repo_index(repo_root: Optional[Path]) -> List[Dict]:
    if not repo_root or not repo_root.exists():
        return []
    ignored_parts = {"node_modules", ".git", "__pycache__", "lib", "dist", "build"}
    entries = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root)
        if any(part in ignored_parts for part in rel.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".pyc"}:
            continue
        label = str(rel)
        entries.append({"path": label, "tokens": tokenize(label.replace("/", " "))})
    return entries[:4000]


def match_repo_area(text: str, entries: List[Dict], repo_terms: Sequence[str]) -> Dict:
    if not entries:
        return {"area": "unknown", "paths": [], "reason": "No repo index was provided."}
    text_tokens = tokenize(text).union(tokenize(" ".join(repo_terms)))
    scored = []
    for entry in entries:
        overlap = text_tokens.intersection(entry["tokens"])
        if overlap:
            scored.append((len(overlap), entry["path"], sorted(overlap)))
    scored.sort(reverse=True)
    paths = [path for _, path, _ in scored[:3]]
    if not paths:
        return {"area": "unknown", "paths": [], "reason": "No repo path had a strong token match."}
    top = paths[0].lower()
    area = "unknown"
    for candidate in TAXONOMY["product_area"]:
        if candidate != "unknown" and candidate.replace("-", "") in top.replace("-", "").replace("_", ""):
            area = candidate
            break
    return {"area": area, "paths": paths, "reason": "Matched ticket language against local repo paths."}


def labels_from_jira_labels(labels: Sequence[str]) -> Dict:
    structured: Dict[str, str] = {}
    normalized = dedupe(labels)
    for label in normalized:
        for key in ("product_area", "root_cause", "severity", "customer_impact", "source"):
            if label in TAXONOMY[key] and label != "unknown" and key not in structured:
                structured[key] = label
        for key, aliases in LABEL_ALIASES.items():
            if label in aliases and key not in structured:
                structured[key] = aliases[label]
    if any(label in {"account-support", "account_support", "account"} for label in normalized):
        structured["issue_type"] = "account"
    elif any(label in {"user-error", "user_error", "not-a-bug", "not_bug"} for label in normalized):
        structured["issue_type"] = "user-error"
    return structured


def load_historical(paths: Sequence[Path]) -> List[Dict]:
    history = []
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        items = data.get("tickets") or data.get("tasks") or data.get("results") or []
        if isinstance(items, list):
            history.extend(item for item in items if isinstance(item, dict))
    return history


def similar_items(ticket: Dict, history: List[Dict], limit: int = 3) -> List[Dict]:
    source_tokens = tokenize(f"{ticket.get('title', '')} {ticket.get('description', '')}")
    matches = []
    for item in history:
        text = f"{item.get('title', '')} {item.get('summary', '')} {item.get('description', '')}"
        target_tokens = tokenize(text)
        if not target_tokens:
            continue
        score = len(source_tokens.intersection(target_tokens)) / max(len(source_tokens.union(target_tokens)), 1)
        if score >= 0.08:
            matches.append(
                {
                    "id": item.get("key") or item.get("jira_key") or item.get("id") or item.get("task_id"),
                    "title": normalize_space(item.get("title") or item.get("summary")),
                    "score": round(score, 2),
                    "labels": dedupe(item.get("labels", [])) if isinstance(item.get("labels"), list) else [],
                }
            )
    matches.sort(key=lambda item: item["score"], reverse=True)
    return matches[:limit]


def suggest_ticket(ticket: Dict, entries: List[Dict], history: List[Dict]) -> Dict:
    text = normalize_space(f"{ticket.get('title', '')} {ticket.get('description', '')}")
    lower = text.lower()
    labels = {
        "product_area": "unknown",
        "issue_type": "question",
        "root_cause": "unknown",
        "severity": choose_by_rules(lower, SEVERITY_RULES, "low"),
        "customer_impact": choose_by_rules(lower, IMPACT_RULES, "unknown"),
        "source": ticket.get("source") or "jira-help",
        "needs_engineering": "unknown",
    }
    evidence = []
    repo_terms: List[str] = []
    matched_rules = 0
    for rule in RULES:
        hits = [term for term in rule["terms"] if term in lower]
        if not hits:
            continue
        matched_rules += 1
        evidence.append(f"Matched terms: {', '.join(hits[:4])}")
        repo_terms.extend(rule.get("repo_terms", []))
        for key, value in rule["labels"].items():
            current = labels.get(key, "unknown")
            if current in {"unknown", "question"}:
                labels[key] = value

    if any(word in lower for word in ("how do i", "can i", "question", "help me")) and matched_rules == 0:
        labels["issue_type"] = "question"
        labels["needs_engineering"] = "no"

    repo_match = match_repo_area(text, entries, repo_terms)
    if labels["product_area"] == "unknown" and repo_match["area"] != "unknown":
        labels["product_area"] = repo_match["area"]

    similar = similar_items(ticket, history)
    if similar:
        evidence.append(f"Found {len(similar)} similar historical item(s).")

    confidence = 0.25 + (matched_rules * 0.2)
    if labels["severity"] in {"critical", "high"}:
        confidence += 0.1
    if repo_match["paths"]:
        confidence += 0.15
    if similar:
        confidence += 0.1
    confidence = min(confidence, 0.95)

    suggested_flat = dedupe(
        [
            labels["product_area"],
            labels["issue_type"],
            labels["root_cause"],
            labels["severity"],
            labels["customer_impact"],
            labels["source"],
            f"needs-eng-{labels['needs_engineering']}",
        ]
    )

    jira_label_overrides = labels_from_jira_labels(ticket.get("current_labels") or [])
    final_suggested = {**labels, **jira_label_overrides}
    if jira_label_overrides:
        suggested_flat = dedupe(
            [
                final_suggested["product_area"],
                final_suggested["issue_type"],
                final_suggested["root_cause"],
                final_suggested["severity"],
                final_suggested["customer_impact"],
                final_suggested["source"],
                f"needs-eng-{final_suggested['needs_engineering']}",
            ]
        )

    return {
        **ticket,
        "suggested_labels": final_suggested,
        "suggested_flat_labels": suggested_flat,
        "approved_labels": jira_label_overrides,
        "approved_flat_labels": dedupe(ticket.get("current_labels") or []),
        "root_cause_hypothesis": final_suggested["root_cause"],
        "repo_signal": repo_match,
        "similar_tickets": similar,
        "confidence": round(confidence, 2),
        "confidence_band": "high" if confidence >= 0.75 else "medium" if confidence >= 0.5 else "low",
        "triage_explanation": evidence or ["No strong rule matched; needs manual review."],
        "review_status": "jira-labeled" if jira_label_overrides else "unreviewed",
        "review_notes": "",
    }


def aggregate_labels(tickets: List[Dict]) -> Dict:
    counters = {key: Counter() for key in TAXONOMY}
    for ticket in tickets:
        labels = ticket.get("suggested_labels", {})
        for key in TAXONOMY:
            counters[key][labels.get(key, "unknown")] += 1
    return {key: dict(counter.most_common()) for key, counter in counters.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize and triage inbound Help/Jira tickets.")
    parser.add_argument("--input", required=True, help="Jira search JSON or ticket JSON export.")
    parser.add_argument("--output", required=True, help="Normalized support dashboard JSON output.")
    parser.add_argument("--repo-root", default="", help="Optional repo root for path-based area suggestions.")
    parser.add_argument(
        "--history",
        nargs="*",
        default=[],
        help="Optional historical dashboard/task/result JSON files for similarity hints.",
    )
    args = parser.parse_args()

    tickets, source_metadata = load_tickets(Path(args.input))
    entries = repo_index(Path(args.repo_root)) if args.repo_root else []
    history = load_historical([Path(item) for item in args.history])
    normalized = [suggest_ticket(ticket, entries, history) for ticket in tickets]
    output = {
        "metadata": {
            "workflow": "support_dashboard_review_first",
            "source_file": args.input,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ticket_count": len(normalized),
            "source_metadata": source_metadata,
            "taxonomy": TAXONOMY,
            "review_policy": "Suggestions are not approved labels until a reviewer approves them.",
        },
        "summary": aggregate_labels(normalized),
        "tickets": normalized,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Wrote {len(normalized)} support ticket(s) to {out}")


if __name__ == "__main__":
    main()
