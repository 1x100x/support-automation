# Support Automation

Production automation for the weekly Help Board bug report.

The workflow is intentionally read-only for Jira and GitHub. It fetches Jira HELP tickets, normalizes support labels, generates a Friday Slack share-out, builds structured dashboard data, and optionally posts the report to Slack.

## Schedule

The report covers the previous Friday at 12:00 AM America/New_York through Thursday at 11:59:59 PM America/New_York.

GitHub Actions runs on Fridays at both `11:00 UTC` and `12:00 UTC`. The Python runner includes a schedule gate so only the run that lands at Friday 7:00 AM New York time posts to Slack. This avoids daylight-saving-time drift.

## GitHub Secrets

Configure these in repository settings:

- `JIRA_BASE_URL`
- `JIRA_EMAIL`
- `JIRA_API_TOKEN`
- `SLACK_BOT_TOKEN`
- `SLACK_USER_TOKEN` optional, only if you want native Canvas card sharing with a user-authorized token
- `SLACK_REPORT_CHANNEL_ID`
- `JIRA_HELP_DASHBOARD_URL` optional
- `OPENAI_API_KEY` optional, used for cohesive LLM ticket summaries

Use a least-privilege Jira API token and a Slack bot token scoped only for posting to the target channel and creating the weekly Canvas.

Required Slack bot scopes:

- `chat:write`
- `canvases:write`
- `files:write`, fallback for uploading the Canvas markdown if native Canvas creation is unavailable

Optional Slack user token:

- `SLACK_USER_TOKEN` can be added if native Canvas card sharing is required. The workflow still uses `SLACK_BOT_TOKEN` for the report message, but uses `SLACK_USER_TOKEN` for Canvas create/edit/access/share calls. Treat this as more sensitive than the bot token because it acts as the authorizing Slack user; use a dedicated service/user account if possible.

## GitHub Variables

Optional:

- `SUPPORT_DASHBOARD_LIMIT`, default `100`
- `SUPPORT_USE_LLM_SUMMARIES`, default `true` in GitHub Actions
- `OPENAI_SUMMARY_MODEL`, default `gpt-4.1-mini`
- `SUPPORT_SLACK_CANVAS_MODE`, default `auto`; use `native`, `file`, or `auto`

## Manual Runs

Use **Actions -> Weekly Help Bug Report -> Run workflow**.

Recommended rollout:

1. Run with `dry_run=true` and `post_slack=false`.
2. Review the uploaded Markdown and JSON artifacts.
3. Run with `slack_smoke_test=true` against a private/test channel to validate Slack post, Canvas creation, Canvas access, and original-post update without Jira or OpenAI calls.
4. Test the full report with `post_slack=true` against the private/test channel.
5. Switch `SLACK_REPORT_CHANNEL_ID` to the production Slack channel.
6. Let the Friday schedule run.

## Local Development

```bash
cp .env.example .env.support
```

Edit `.env.support` locally. Never commit real credentials.

Run tests:

```bash
python -m unittest discover -s tests -v
```

Preflight:

```bash
python scripts/run_weekly_help_bug_report.py --preflight
```

Generate report artifacts without Slack:

```bash
python scripts/run_weekly_help_bug_report.py
```

Post to Slack locally only when you intentionally want to test posting:

```bash
python scripts/run_weekly_help_bug_report.py --post-slack
```

Run a Slack-only Canvas smoke test without fetching Jira tickets or calling OpenAI:

```bash
python scripts/run_slack_canvas_smoke_test.py
```

## Outputs

Generated files are ignored by Git and uploaded as GitHub Actions artifacts:

- `data/help_inbound_raw.json`
- `data/support_dashboard_tickets.json`
- `data/support_weekly_bug_report.json`
- `data/support_weekly_bug_report.md`
- `data/support_weekly_bug_report_dashboard.json`
- `data/support_weekly_bug_report_canvas.md`
- `data/support_slack_post_result.json`, only when Slack posting runs
- `data/support_slack_canvas_result.json`, only when Slack Canvas/file posting runs
- `data/support_slack_smoke_report.md`, only when the Slack smoke test runs
- `data/support_slack_smoke_canvas.md`, only when the Slack smoke test runs
- `data/support_slack_smoke_post_result.json`, only when the Slack smoke test posts
- `data/support_slack_smoke_canvas_result.json`, only when the Slack smoke test posts

## Security Notes

- The automation does not write to Jira.
- The automation does not write to GitHub beyond workflow logs and artifacts.
- Slack posting only happens on scheduled production runs or explicit manual runs with `post_slack=true`.
- The Slack smoke test only posts a synthetic test message and Canvas; it does not fetch Jira data or call OpenAI.
- When Slack posting runs, the workflow creates a native Slack Canvas as a channel tab, writes the generated Canvas markdown into it, grants channel access, and updates the original report post with a Canvas dashboard link. Slack rejects `canvases.share` for the bot token type with `not_allowed_token_type`, so native Canvas card sharing is attempted only when `SLACK_USER_TOKEN` is configured. If native Canvas creation fails in `auto` mode, it uploads the Canvas markdown into the thread as a Slack file.
- If Canvas/file posting reports `missing_scope`, add the missing Slack bot scope and reinstall the Slack app so the existing `SLACK_BOT_TOKEN` receives the new permission.
- Slack-facing summaries redact emails, wallet/contract addresses, and raw URLs.
- LLM summary requests are redacted before sending, and model output is redacted again before Slack posting.
- If `OPENAI_API_KEY` is not configured, the report still runs with local heuristic summaries and marks the report metadata accordingly.
- Raw Jira data and generated reports are workflow artifacts, not committed repo files.

## LLM Troubleshooting

Check `metadata.summary_source`, `metadata.llm_summary_count`, and `metadata.summary_error` in `data/support_weekly_bug_report.json`.

- `summary_source: llm` means every ticket used an LLM summary.
- `summary_source: partial-llm` means some tickets used LLM summaries and some used the local fallback.
- `summary_source: heuristic` with an HTTP `429` summary error usually means the OpenAI project is out of quota, billing is not enabled, the project budget is too low, or the selected `OPENAI_SUMMARY_MODEL` is not available to that key.
