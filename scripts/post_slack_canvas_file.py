#!/usr/bin/env python3
"""Create or upload the generated Canvas markdown in Slack."""

import argparse
import json
import mimetypes
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict


SLACK_API_BASE = "https://slack.com/api"


class SlackApiError(RuntimeError):
    """Raised when Slack returns an API-level failure."""


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def slack_api(method: str, payload: Dict, token: str) -> Dict:
    req = urllib.request.Request(
        url=f"{SLACK_API_BASE}/{method}",
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
        raise SlackApiError(f"Slack {method} failed with HTTP {exc.code}: {body}") from exc
    if not result.get("ok"):
        raise SlackApiError(f"Slack {method} failed: {result.get('error', 'unknown_error')}")
    return result


def upload_bytes(upload_url: str, file_path: Path) -> None:
    content_type = mimetypes.guess_type(file_path.name)[0] or "text/markdown"
    req = urllib.request.Request(
        url=upload_url,
        data=file_path.read_bytes(),
        method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Slack file content upload failed with HTTP {exc.code}: {body}") from exc


def slack_canvas_url(result: Dict) -> str:
    if result.get("canvas_url"):
        return str(result["canvas_url"])
    canvas = result.get("canvas") or {}
    for key in ("url", "permalink", "canvas_url"):
        if canvas.get(key):
            return str(canvas[key])
    return ""


def create_native_canvas(*, file_path: Path, title: str, token: str) -> Dict:
    content = file_path.read_text(encoding="utf-8")
    if not content.strip():
        raise SystemExit(f"Canvas file is empty: {file_path}")
    return slack_api(
        "canvases.create",
        {
            "title": title,
            "document_content": {
                "type": "markdown",
                "markdown": content,
            },
        },
        token,
    )


def post_canvas_link(*, channel: str, thread_ts: str, canvas_url: str, title: str, token: str) -> Dict:
    text = f"Canvas dashboard: <{canvas_url}|{title}>"
    payload = {
        "channel": channel,
        "text": text,
        "unfurl_links": True,
        "unfurl_media": True,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return slack_api("chat.postMessage", payload, token)


def upload_canvas_file(
    *,
    file_path: Path,
    title: str,
    channel: str,
    thread_ts: str,
    initial_comment: str,
    token: str,
) -> Dict:
    if not file_path.exists():
        raise SystemExit(f"Canvas file does not exist: {file_path}")
    length = file_path.stat().st_size
    if length <= 0:
        raise SystemExit(f"Canvas file is empty: {file_path}")

    upload = slack_api(
        "files.getUploadURLExternal",
        {"filename": file_path.name, "length": length},
        token,
    )
    upload_bytes(upload["upload_url"], file_path)
    complete_payload = {
        "files": [{"id": upload["file_id"], "title": title}],
        "channel_id": channel,
        "initial_comment": initial_comment,
    }
    if thread_ts:
        complete_payload["thread_ts"] = thread_ts
    return slack_api("files.completeUploadExternal", complete_payload, token)


def read_slack_result(path: Path) -> Dict:
    if not path.exists():
        raise SystemExit(f"Slack result JSON does not exist: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload generated Canvas markdown to Slack.")
    parser.add_argument("--file", required=True, help="Canvas markdown file to upload.")
    parser.add_argument("--slack-result-json", required=True, help="JSON output from the Slack report post.")
    parser.add_argument("--title", required=True, help="Slack file title.")
    parser.add_argument(
        "--mode",
        choices=("native", "file", "auto"),
        default=os.getenv("SUPPORT_SLACK_CANVAS_MODE", "auto"),
        help="Use native Slack Canvas, Slack file upload, or native-with-file-fallback.",
    )
    parser.add_argument(
        "--initial-comment",
        default="Canvas dashboard for this weekly Help bug report.",
        help="Comment shown with the uploaded file.",
    )
    args = parser.parse_args()

    token = require_env("SLACK_BOT_TOKEN")
    slack_result = read_slack_result(Path(args.slack_result_json))
    channel = str(slack_result.get("channel") or "").strip()
    thread_ts = str(slack_result.get("ts") or "").strip()
    if not channel:
        raise SystemExit("Slack result JSON is missing channel.")

    file_path = Path(args.file)
    if args.mode in {"native", "auto"}:
        try:
            result = create_native_canvas(file_path=file_path, title=args.title, token=token)
            canvas_url = slack_canvas_url(result)
            if not canvas_url:
                raise RuntimeError("Slack did not return a canvas URL.")
            post_canvas_link(
                channel=channel,
                thread_ts=thread_ts,
                canvas_url=canvas_url,
                title=args.title,
                token=token,
            )
            print(f"Created native Slack Canvas and linked it in channel {channel}: {canvas_url}")
            return
        except Exception as exc:
            if args.mode == "native":
                raise
            print(f"::warning title=Native Slack Canvas failed::Falling back to file upload. Error: {exc}")

    result = upload_canvas_file(
        file_path=file_path,
        title=args.title,
        channel=channel,
        thread_ts=thread_ts,
        initial_comment=args.initial_comment,
        token=token,
    )
    files = result.get("files") or []
    file_id = files[0].get("id") if files else "unknown"
    print(f"Uploaded Canvas markdown to Slack channel {channel} in thread {thread_ts or 'none'} as file {file_id}")


if __name__ == "__main__":
    main()
