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
- `SLACK_REPORT_CHANNEL_ID`
- `JIRA_HELP_DASHBOARD_URL` optional
- `OPENAI_API_KEY` optional, used for cohesive LLM ticket summaries

Use a least-privilege Jira API token and a Slack bot token scoped only for posting to the target channel.

## GitHub Variables

Optional:

- `SUPPORT_DASHBOARD_LIMIT`, default `100`
- `SUPPORT_USE_LLM_SUMMARIES`, default `true` in GitHub Actions
- `OPENAI_SUMMARY_MODEL`, default `gpt-4.1-mini`

## Manual Runs

Use **Actions -> Weekly Help Bug Report -> Run workflow**.

Recommended rollout:

1. Run with `dry_run=true` and `post_slack=false`.
2. Review the uploaded Markdown and JSON artifacts.
3. Test with `post_slack=true` against a private/test channel.
4. Switch `SLACK_REPORT_CHANNEL_ID` to the production Slack channel.
5. Let the Friday schedule run.

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

## Outputs

Generated files are ignored by Git and uploaded as GitHub Actions artifacts:

- `data/help_inbound_raw.json`
- `data/support_dashboard_tickets.json`
- `data/support_weekly_bug_report.json`
- `data/support_weekly_bug_report.md`
- `data/support_weekly_bug_report_dashboard.json`
- `data/support_weekly_bug_report_canvas.md`

## Security Notes

- The automation does not write to Jira.
- The automation does not write to GitHub beyond workflow logs and artifacts.
- Slack posting only happens on scheduled production runs or explicit manual runs with `post_slack=true`.
- Slack-facing summaries redact emails, wallet/contract addresses, and raw URLs.
- LLM summary requests are redacted before sending, and model output is redacted again before Slack posting.
- If `OPENAI_API_KEY` is not configured, the report still runs with local heuristic summaries and marks the report metadata accordingly.
- Raw Jira data and generated reports are workflow artifacts, not committed repo files.
