#!/usr/bin/env python3
"""Fetch inbound Help/Jira tickets as raw Jira search JSON for local triage."""

import argparse
import base64
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict
from datetime import datetime, timezone


DEFAULT_FIELDS = [
    "summary",
    "description",
    "status",
    "created",
    "updated",
    "reporter",
    "creator",
    "labels",
    "priority",
    "issuetype",
]


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def jira_headers(email: str, token: str) -> Dict[str, str]:
    auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("utf-8")
    return {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def search_payload(jql: str, max_results: int, next_page_token: str = "") -> Dict:
    payload = {
        "jql": jql,
        "maxResults": max_results,
        "fields": DEFAULT_FIELDS,
    }
    if next_page_token:
        payload["nextPageToken"] = next_page_token
    return payload


def fetch_page(base_url: str, headers: Dict[str, str], jql: str, max_results: int, next_page_token: str = "") -> Dict:
    url = f"{base_url.rstrip('/')}/rest/api/3/search/jql"
    body_text = json.dumps(search_payload(jql, max_results, next_page_token))
    body = body_text.encode("utf-8")
    req = urllib.request.Request(url=url, headers=headers, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        if isinstance(exc, urllib.error.HTTPError):
            raise
        return fetch_page_with_curl(url, headers, body_text)


def fetch_page_with_curl(url: str, headers: Dict[str, str], body_text: str) -> Dict:
    config_lines = [
        'request = "POST"',
        f'url = "{url}"',
        'silent',
        'show-error',
        'fail-with-body',
    ]
    for key, value in headers.items():
        config_lines.append(f'header = "{key}: {value}"')
    config_lines.append(f"data = {json.dumps(body_text)}")

    config_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as config:
            config_path = config.name
            config.write("\n".join(config_lines))
        os.chmod(config_path, 0o600)
        result = subprocess.run(
            ["curl", "--config", config_path],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        body = exc.stdout or exc.stderr or ""
        raise SystemExit(f"Jira fetch failed through curl fallback: {body.strip()}") from exc
    finally:
        if config_path:
            Path(config_path).unlink(missing_ok=True)
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Jira Help tickets for the Support Dashboard.")
    parser.add_argument(
        "--jql",
        default=os.getenv("JIRA_HELP_JQL", 'project = HELP ORDER BY created DESC'),
        help="JQL for inbound Help tickets. Defaults to JIRA_HELP_JQL or project = HELP.",
    )
    parser.add_argument("--output", default="data/help_inbound_raw.json", help="Raw Jira JSON output.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum tickets to fetch.")
    parser.add_argument("--page-size", type=int, default=50, help="Jira search page size.")
    args = parser.parse_args()

    base_url = require_env("JIRA_BASE_URL")
    email = require_env("JIRA_EMAIL")
    token = require_env("JIRA_API_TOKEN")
    headers = jira_headers(email, token)

    issues = []
    total = None
    next_page_token = ""
    while len(issues) < args.limit:
        page = fetch_page(
            base_url=base_url,
            headers=headers,
            jql=args.jql,
            max_results=min(args.page_size, args.limit - len(issues)),
            next_page_token=next_page_token,
        )
        total = page.get("total")
        batch = page.get("issues", [])
        issues.extend(batch)
        next_page_token = page.get("nextPageToken") or ""
        if not batch or page.get("isLast") or not next_page_token:
            break

    payload = {
        "metadata": {
            "base_url": base_url.rstrip("/"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "jql": args.jql,
        "total": total,
        "fetched": len(issues),
        "issues": issues,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Fetched {len(issues)} Jira issue(s) to {out}")


if __name__ == "__main__":
    main()
