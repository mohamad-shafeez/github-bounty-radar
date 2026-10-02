# GitHub Bounty Radar

Free GitHub bounty and paid-issue radar using GitHub Actions and ntfy.sh.

## What it does

- Scans GitHub issues for bounty and contribution opportunities.
- Monitors Expensify/App External and Help Wanted issues.
- Detects bounty amounts and bounty-related changes.
- Scores opportunities based on bounty, recency, labels, comments, technology fit, and assignment status.
- Sends phone notifications through ntfy.sh.
- Runs automatically through GitHub Actions.

## Local test

```cmd
python -m py_compile radar.py
```

The production scan runs in GitHub Actions using the built-in GITHUB_TOKEN.

## Security

No GitHub personal access token is required by the workflow.
NTFY_TOPIC is stored as a GitHub Actions secret.
