# GitHub Bounty Radar

A free, cloud-only GitHub bounty/work-opportunity radar that can run while the laptop is off.

## Final lifecycle

```text
targets.json
    ↓
GitHub Actions radar (5 min)
    ↓
candidate detection + bounty evidence + scoring + deduplication
    ↓
ntfy phone notification
    ↓
YOU ACCEPT / DECLINE
    ↓
real GitHub issue + repository evidence ingestion
    ↓
Gemini / Grok independent analysis
    ↓
evidence/confidence/disagreement gate
    ↓
engineering dossier + human-readable proposal
    ↓
YOU approve POST_COMMENT
    ↓
proposal is posted to the target issue
    ↓
maintainer response watcher
    ↓
YOU explicitly confirm maintainer agreement
    ↓
YOU approve implementation
    ↓
AI-generated patch in isolated checkout
    ↓
tests + path/security safety checks
    ↓
final review
    ↓
PR_READY
    ↓
explicit PR workflow
    ↓
fork + branch + GitHub PR
```

**No public comment, implementation, or PR is automatic.** The radar can discover and notify automatically, but every consequential external action is human-gated.

## Add repositories without editing Python

Edit `targets.json`.

Repository:

```json
{
  "url": "https://github.com/owner/repository",
  "enabled": true,
  "priority": "normal",
  "minimum_bounty_usd": 50,
  "labels": ["bounty"],
  "search_bounty_text": true,
  "search_bounty_label": true,
  "search_help_wanted": false
}
```

Specific issue:

```json
{
  "url": "https://github.com/owner/repository/issues/123",
  "enabled": true,
  "priority": "high"
}
```

For an issue URL, the radar uses the GitHub issue API directly rather than relying on a broad search result.

The current default target is:

```text
https://github.com/Expensify/App
```

with its useful `External`, `Help Wanted`, and `💎 Bounty` signals.

### Important

Adding a repository does **not** mean every issue becomes a notification. The radar:

- searches only configured repositories/issues;
- detects bounty/reward evidence;
- applies the configured minimum bounty;
- distinguishes confirmed from uncertain bounty evidence;
- deduplicates the same issue across multiple searches;
- scores opportunities;
- rate-limits notifications;
- stores a durable job before notifying.

A repository-specific label can be useful discovery evidence, but it is not by itself proof that a cash bounty exists.

## Notification philosophy

The phone is a **radar**, not the investigation system.

A notification means:

> "This candidate deserves your attention."

It does not mean:

> "This bounty is definitely legitimate and ready to claim."

After you ACCEPT, the worker performs the expensive investigation.

## Investigation

The accepted job gathers real GitHub evidence including:

- issue body and metadata;
- comments;
- issue timeline;
- repository metadata;
- default-branch tree;
- relevant documentation/manifests;
- tests;
- workflows;
- selected source files;
- recent commits;
- linked/cross-referenced pull requests.

The evidence is saved as an auditable artifact.

Gemini and Grok review the same repository evidence independently. One model is not allowed to treat the other model's conclusion as evidence.

The review gate requires citations and confidence. Material disagreement is preserved and stops automatic progression.

## Engineering dossier

Each serious job gets:

`job-artifacts/<job_id>/issue-<number>.md`

The dossier is a living engineering record containing:

1. Issue Summary
2. Original Issue
3. Reproduction
4. Root Cause
5. Repository Evidence
6. Issue Comments/Maintainer Context
7. Related PRs/Existing Work/bots
8. AI Reviewer Findings
9. Disagreements
10. Verified Diagnosis
11. Proposed Fix
12. Exact Files To Change
13. Implementation Plan
14. Tests Required
15. Maintainer Proposal
16. Maintainer Response
17. Implementation Result
18. Final Review
19. Pull Request

## Human-like proposal, without impersonation

The proposal is written as concise, evidence-backed engineering communication. It is not allowed to invent reproduction steps, maintainer intent, APIs, files, or test results.

Posting is blocked until the job is in `HUMAN_REVIEW` and you explicitly select `POST_COMMENT`.

The system does not silently interpret a maintainer's random reply as approval. You explicitly confirm `MAINTAINER_AGREED`.

## AI providers

Only these providers are supported:

- Gemini: `GEMINI_API_KEY`
- xAI Grok: `GROK_API_KEY`

The system uses retries and fallback when configured. Provider availability, models, quotas, and free-tier terms can change; the system does not assume unlimited free inference.

Never put API keys into `targets.json`, `jobs.json`, `state.json`, a dossier, an issue comment, or an ntfy message.

## Required GitHub secrets

Already used:

- `NTFY_TOPIC`

Optional AI:

- `GEMINI_API_KEY`
- `GROK_API_KEY`

For posting comments to target repositories:

- `UPSTREAM_GITHUB_TOKEN`

For pushing a fork and opening a PR:

- `CONTRIBUTOR_GITHUB_TOKEN`

These credentials are read only inside the relevant GitHub Actions jobs.

## Persistent files

- `state.json` — radar scan memory.
- `jobs.json` — durable bounty-job lifecycle and audit history.
- `targets.json` — user-maintained repository/issue targets.
- `job-artifacts/<job_id>/` — evidence, reviews, proposal, implementation and final-review artifacts.

## Local verification

```cmd
python -m py_compile radar.py targets.py jobs.py pipeline.py ai_review.py worker.py proposal.py control.py implement.py final_review.py pr_worker.py maintainer_watch.py notify.py
python -m unittest discover -s . -p "test_*.py"
```

The tests use mocked APIs/credentials for deterministic safety. A passing test suite is not a claim that a real target repository accepted a proposal.

## Security boundary

GitHub Actions' built-in token is appropriate for this control repository's own state and workflows. It is not assumed to have arbitrary target-repository write permission.

Target comments, fork pushes and PR creation therefore use separate credentials and remain explicit gates.

The implementation worker also rejects unsafe patch paths, workflow/credential changes and path traversal before applying an AI-generated diff.

## No paid infrastructure

The architecture uses GitHub Actions, GitHub APIs, repository storage and ntfy. No VPS, paid database, paid automation platform or always-on laptop is required.
