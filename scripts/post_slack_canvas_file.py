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

from generate_support_bug_report import slack_text_from_report


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
        raise SlackApiError(f"Slack file content upload failed with HTTP {exc.code}: {body}") from exc


def slack_canvas_url(result: Dict) -> str:
    if result.get("canvas_url"):
        return str(result["canvas_url"])
    canvas = result.get("canvas") or {}
    for key in ("url", "permalink", "canvas_url"):
        if canvas.get(key):
            return str(canvas[key])
    return ""


def slack_team_id(token: str) -> str:
    result = slack_api("auth.test", {}, token)
    team_id = str(result.get("team_id") or "").strip()
    if not team_id:
        raise SlackApiError("Slack auth.test did not return team_id.")
    return team_id


def slack_canvas_url_or_construct(result: Dict, *, token: str) -> str:
    url = slack_canvas_url(result)
    if url:
        return url
    canvas_id = slack_canvas_id(result)
    if not canvas_id:
        return ""
    return f"https://app.slack.com/docs/{slack_team_id(token)}/{canvas_id}"


def slack_canvas_id(result: Dict) -> str:
    for key in ("canvas_id", "id", "file_id"):
        if result.get(key):
            return str(result[key])
    canvas = result.get("canvas") or {}
    for key in ("id", "canvas_id", "file_id"):
        if canvas.get(key):
            return str(canvas[key])
    return ""


def safe_response_keys(result: Dict) -> Dict:
    canvas = result.get("canvas") or {}
    return {
        "top_level_keys": sorted(str(key) for key in result.keys()),
        "canvas_keys": sorted(str(key) for key in canvas.keys()) if isinstance(canvas, dict) else [],
        "canvas_id": slack_canvas_id(result),
        "canvas_url_present": bool(slack_canvas_url(result)),
    }


def write_status(path: str, payload: Dict) -> None:
    if not path:
        return
    status_path = Path(path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def canvas_document_content(content: str) -> Dict:
    return {
        "type": "markdown",
        "markdown": content,
    }


def populate_native_canvas(*, canvas_id: str, content: str, token: str) -> Dict:
    return slack_api(
        "canvases.edit",
        {
            "canvas_id": canvas_id,
            "changes": [
                {
                    "operation": "replace",
                    "document_content": canvas_document_content(content),
                }
            ],
        },
        token,
    )


def create_native_canvas(*, file_path: Path, title: str, token: str, channel: str = "") -> Dict:
    content = file_path.read_text(encoding="utf-8")
    if not content.strip():
        raise SystemExit(f"Canvas file is empty: {file_path}")
    payload = {
        "title": title,
        "document_content": canvas_document_content(content),
    }
    if channel:
        payload["channel_id"] = channel
    result = slack_api(
        "canvases.create",
        payload,
        token,
    )
    canvas_id = slack_canvas_id(result)
    if canvas_id:
        result["content_edit"] = populate_native_canvas(canvas_id=canvas_id, content=content, token=token)
    return result


def share_canvas_with_channel(*, canvas_id: str, channel: str, token: str) -> Dict:
    attempts = [
        {
            "canvas_id": canvas_id,
            "access": [
                {
                    "type": "channel",
                    "channel_id": channel,
                    "permission": "read",
                }
            ],
        },
        {
            "canvas_id": canvas_id,
            "channel_ids": [channel],
            "permission": "read",
        },
        {
            "canvas_id": canvas_id,
            "channel_ids": [channel],
            "access_level": "read",
        },
        {
            "canvas_id": canvas_id,
            "channel_id": channel,
            "permission": "read",
        },
        {
            "canvas_id": canvas_id,
            "channel_id": channel,
            "access_level": "read",
        },
        {
            "canvas_id": canvas_id,
            "access": [
                {
                    "type": "channel",
                    "id": channel,
                    "access_level": "read",
                }
            ],
        },
    ]
    errors = []
    for payload in attempts:
        try:
            return slack_api("canvases.access.set", payload, token)
        except SlackApiError as exc:
            errors.append(str(exc))
    raise SlackApiError("; ".join(errors))


def share_canvas_card(*, canvas_id: str, channel: str, token: str, thread_ts: str = "") -> Dict:
    attempts = [
        {"canvas_id": canvas_id, "channel_id": channel},
        {"canvas_id": canvas_id, "channel": channel},
        {"canvas_id": canvas_id, "channel_id": channel, "thread_ts": thread_ts},
        {"canvas_id": canvas_id, "channel": channel, "thread_ts": thread_ts},
        {"file_id": canvas_id, "channel_id": channel},
        {"file_id": canvas_id, "channel_id": channel, "thread_ts": thread_ts},
    ]
    errors = []
    for payload in attempts:
        payload = {key: value for key, value in payload.items() if value}
        try:
            return slack_api("canvases.share", payload, token)
        except SlackApiError as exc:
            errors.append(str(exc))
    raise SlackApiError("; ".join(errors))


def bot_token_canvas_share_note() -> str:
    return "Skipped canvases.share because Slack rejects bot tokens with not_allowed_token_type."


def try_share_canvas_with_channel(*, canvas_id: str, channel: str, token: str) -> tuple[Dict, str]:
    if not canvas_id:
        return {}, "Slack Canvas response did not include a canvas_id."
    try:
        return share_canvas_with_channel(canvas_id=canvas_id, channel=channel, token=token), ""
    except SlackApiError as exc:
        return {}, str(exc)


def canvas_link_line(title: str) -> str:
    return f"Canvas dashboard: {title} shared in this channel."


def update_report_message_with_canvas(
    *,
    report_markdown_path: Path,
    channel: str,
    message_ts: str,
    title: str,
    token: str,
) -> Dict:
    markdown = report_markdown_path.read_text(encoding="utf-8")
    text = slack_text_from_report(markdown)
    link_line = canvas_link_line(title)
    if link_line not in text:
        text = f"{text.rstrip()}\n\n{link_line}"
    payload = {
        "channel": channel,
        "ts": message_ts,
        "text": text,
        "unfurl_links": True,
        "unfurl_media": True,
    }
    return slack_api("chat.update", payload, token)


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
    parser.add_argument("--report-md", required=True, help="Slack report Markdown used to update the original post.")
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
    parser.add_argument(
        "--status-output",
        default=os.getenv("SUPPORT_SLACK_CANVAS_RESULT_JSON", ""),
        help="Optional JSON path for Slack Canvas/upload outcome metadata.",
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
            result = create_native_canvas(file_path=file_path, title=args.title, token=token, channel=channel)
            canvas_url = slack_canvas_url_or_construct(result, token=token)
            if not canvas_url:
                raise RuntimeError(
                    "Slack created a Canvas response but did not return a URL or canvas_id. "
                    f"Response shape: {safe_response_keys(result)}"
            )
            canvas_id = slack_canvas_id(result)
            access_result = {}
            access_error = ""
            share_result = {}
            share_error = bot_token_canvas_share_note()
            if canvas_id:
                access_result, access_error = try_share_canvas_with_channel(
                    canvas_id=canvas_id,
                    channel=channel,
                    token=token,
                )
                if access_error:
                    print(
                        "::warning title=Slack Canvas access share failed::"
                        f"Created Canvas but could not grant channel access. Error: {access_error}"
                    )
            update_report_message_with_canvas(
                report_markdown_path=Path(args.report_md),
                channel=channel,
                message_ts=thread_ts,
                title=args.title,
                token=token,
            )
            print(f"Created native Slack Canvas tab and updated original Slack report in channel {channel}: {canvas_url}")
            canvas_visible = bool(access_result.get("ok"))
            write_status(
                args.status_output,
                {
                    "ok": canvas_visible,
                    "mode": "native",
                    "surface": "channel_tab",
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "canvas_url": canvas_url,
                    "canvas_id": canvas_id,
                    "channel_tab_created": True,
                    "access_set": bool(access_result.get("ok")),
                    "access_error": access_error,
                    "shared_card": bool(share_result.get("ok")),
                    "share_error": share_error,
                    "post_updated": True,
                },
            )
            return
        except Exception as exc:
            if args.mode == "native":
                raise
            native_error = str(exc)
            print(f"::warning title=Native Slack Canvas failed::Falling back to file upload. Error: {native_error}")

    try:
        result = upload_canvas_file(
            file_path=file_path,
            title=args.title,
            channel=channel,
            thread_ts=thread_ts,
            initial_comment=args.initial_comment,
            token=token,
        )
    except Exception as exc:
        message = str(exc)
        print(f"::warning title=Slack Canvas file upload failed::{message}")
        write_status(
            args.status_output,
            {
                "ok": False,
                "mode": args.mode,
                "channel": channel,
                "thread_ts": thread_ts,
                "native_error": locals().get("native_error", ""),
                "file_error": message,
            },
        )
        if args.mode == "auto":
            return
        raise
    files = result.get("files") or []
    file_id = files[0].get("id") if files else "unknown"
    write_status(
        args.status_output,
        {
            "ok": True,
            "mode": "file",
            "channel": channel,
            "thread_ts": thread_ts,
            "file_id": file_id,
            "native_error": locals().get("native_error", ""),
        },
    )
    print(f"Uploaded Canvas markdown to Slack channel {channel} in thread {thread_ts or 'none'} as file {file_id}")


if __name__ == "__main__":
    main()
