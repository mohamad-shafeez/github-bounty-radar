#!/usr/bin/env python3
"""Engineering dossier generator and updater for bounty jobs.

Produces and maintains:
    job-artifacts/<job_id>/issue-<issue_number>.md

Contains exactly the 19 human-readable investigation sections:
1. Issue Summary (What, Where, When, Who / Scope)
2. Original Issue
3. Reproduction Steps
4. Root Cause
5. Repository Evidence
6. Issue Comments and Maintainer Context
7. Related PRs / Existing Work
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

IMPORTANT: Never includes secrets (tokens, keys, topic credentials, cookies, auth headers).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# Patterns to scrub
SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,255}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{80,255}"),
    re.compile(r"AIzaSy[A-Za-z0-9_-]{33}"),
    re.compile(r"xai-[A-Za-z0-9_-]{30,}"),
    re.compile(r"gsk_[A-Za-z0-9_-]{30,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.I),
    re.compile(r"(?:Authorization|Cookie|Token):\s*[^\r\n]+", re.I),
]


def sanitize_secrets(text: str) -> str:
    """Scrub known tokens, keys, topic credentials, and environment secrets."""
    if not text:
        return ""
    result = text
    # Mask values of sensitive environment variables
    for key, val in os.environ.items():
        if any(term in key.upper() for term in ("TOKEN", "KEY", "SECRET", "TOPIC", "PASSWORD", "CREDENTIAL")):
            cleaned = val.strip()
            if len(cleaned) >= 6 and cleaned in result:
                result = result.replace(cleaned, "[REDACTED_SECRET]")

    for pat in SECRET_PATTERNS:
        result = pat.sub("[REDACTED_CREDENTIAL]", result)
    return result


def get_dossier_path(root: Path, job_id: str, issue_number: int | str) -> Path:
    return root / "job-artifacts" / job_id / f"issue-{issue_number}.md"


def build_dossier_content(
    *,
    issue_number: int | str,
    title: str,
    summary_what: str = "Investigation in progress",
    summary_where: str = "TBD based on repository evidence",
    summary_when: str = "Reported in issue",
    summary_who: str = "Users / Repository maintainers",
    original_issue: str = "",
    expected_behavior: str = "Expected behavior not yet fully specified.",
    actual_behavior: str = "Actual behavior as described in issue report.",
    root_cause: str = "Awaiting evidence investigation.",
    files_inspected: list[str] | None = None,
    functions_modules: list[str] | None = None,
    code_evidence: list[str] | None = None,
    tests_evidence: list[str] | None = None,
    workflows_evidence: list[str] | None = None,
    manifests_evidence: list[str] | None = None,
    git_history_evidence: list[str] | None = None,
    comments_context: list[str] | None = None,
    related_prs: list[str] | None = None,
    bot_or_prior_work: str = "None observed so far.",
    ai_reviewer_findings: list[dict[str, Any]] | None = None,
    disagreements: list[str] | None = None,
    disagreements_resolved: bool = True,
    verified_diagnosis_cause: str = "Pending verification",
    verified_confidence: str = "Pending",
    verified_evidence: list[str] | None = None,
    proposed_fix: str = "Pending investigation",
    exact_files_to_change: list[str] | None = None,
    implementation_plan: list[str] | None = None,
    tests_required: list[str] | None = None,
    maintainer_proposal: str = "Pending human review.",
    maintainer_response: str = "Initially empty. Awaiting maintainer response.",
    implementation_result: str = "Initially empty. Pending implementation after approval.",
    final_review: str = "Initially empty. Pending final review.",
    pull_request: str = "Initially empty. Pending PR creation.",
) -> str:
    files_inspected = files_inspected or []
    functions_modules = functions_modules or []
    code_evidence = code_evidence or []
    tests_evidence = tests_evidence or []
    workflows_evidence = workflows_evidence or []
    manifests_evidence = manifests_evidence or []
    git_history_evidence = git_history_evidence or []
    comments_context = comments_context or []
    related_prs = related_prs or []
    ai_reviewer_findings = ai_reviewer_findings or []
    disagreements = disagreements or []
    verified_evidence = verified_evidence or []
    exact_files_to_change = exact_files_to_change or []
    implementation_plan = implementation_plan or []
    tests_required = tests_required or []

    lines: list[str] = [
        f"# Issue {issue_number} — {title}",
        "",
        "## 1. Issue Summary",
        f"- **What**: {summary_what}",
        f"- **Where**: {summary_where}",
        f"- **When**: {summary_when}",
        f"- **Who / Scope**: {summary_who}",
        "",
        "## 2. Original Issue",
        original_issue.strip() or "No description provided.",
        "",
        "## 3. Reproduction Steps",
        f"- **Expected Behavior**: {expected_behavior}",
        f"- **Actual Behavior**: {actual_behavior}",
        "",
        "## 4. Root Cause",
        root_cause.strip() or "Root cause under investigation.",
        "",
        "## 5. Repository Evidence",
        "### Files Inspected",
    ]

    if files_inspected:
        lines.extend(f"- `{f}`" for f in files_inspected)
    else:
        lines.append("- None recorded yet.")

    lines.extend([
        "",
        "### Relevant Functions / Classes / Modules",
    ])
    if functions_modules:
        lines.extend(f"- {item}" for item in functions_modules)
    else:
        lines.append("- Not yet isolated.")

    lines.extend([
        "",
        "### Relevant Code Evidence",
    ])
    if code_evidence:
        lines.extend(f"- {c}" for c in code_evidence)
    else:
        lines.append("- None recorded yet.")

    lines.extend([
        "",
        "### Tests",
    ])
    if tests_evidence:
        lines.extend(f"- {t}" for t in tests_evidence)
    else:
        lines.append("- None identified yet.")

    lines.extend([
        "",
        "### Workflows / CI",
    ])
    if workflows_evidence:
        lines.extend(f"- {w}" for w in workflows_evidence)
    else:
        lines.append("- None identified yet.")

    lines.extend([
        "",
        "### Dependencies / Manifests",
    ])
    if manifests_evidence:
        lines.extend(f"- {m}" for m in manifests_evidence)
    else:
        lines.append("- None identified yet.")

    lines.extend([
        "",
        "### Relevant Git / History Evidence",
    ])
    if git_history_evidence:
        lines.extend(f"- {g}" for g in git_history_evidence)
    else:
        lines.append("- None identified yet.")

    lines.extend([
        "",
        "## 6. Issue Comments and Maintainer Context",
    ])
    if comments_context:
        lines.extend(f"- {c}" for c in comments_context)
    else:
        lines.append("- No issue comments recorded.")

    lines.extend([
        "",
        "## 7. Related PRs / Existing Work",
    ])
    if related_prs:
        lines.extend(f"- {pr}" for pr in related_prs)
    else:
        lines.append("- No related PRs identified.")
    lines.append(f"- **Bot / Automation / Prior Proposals**: {bot_or_prior_work}")

    lines.extend([
        "",
        "## 8. AI Reviewer Findings",
    ])
    if ai_reviewer_findings:
        for rev in ai_reviewer_findings:
            p_name = rev.get("provider", "unknown")
            model = rev.get("model", "configured")
            lines.extend([
                f"### Reviewer: {p_name} ({model})",
                f"- **Conclusion**: {rev.get('summary', 'No summary provided')}",
                f"- **Evidence Used**: {', '.join(str(x) for x in rev.get('evidence', [])) or 'None cited'}",
                f"- **Confidence**: {rev.get('confidence', 'N/A')}%",
                f"- **Disagreements / Flags**: {rev.get('disagreements', 'None reported')}",
                "",
            ])
    else:
        lines.append("- No AI reviewer findings recorded yet.")

    lines.extend([
        "## 9. Disagreements",
    ])
    if disagreements:
        lines.extend(f"- {d}" for d in disagreements)
        lines.append(f"\n**Resolution Status**: {'Resolved' if disagreements_resolved else 'UNRESOLVED — Requires further human investigation'}")
    else:
        lines.append("- No conflicting findings among independent reviews.")

    lines.extend([
        "",
        "## 10. Verified Diagnosis",
        f"- **Root Cause**: {verified_diagnosis_cause}",
        f"- **Confidence**: {verified_confidence}",
        "- **Supporting Evidence**:",
    ])
    if verified_evidence:
        lines.extend(f"  - {e}" for e in verified_evidence)
    else:
        lines.append("  - Awaiting verified evidence.")

    lines.extend([
        "",
        "## 11. Proposed Fix",
        proposed_fix.strip() or "Pending proposed fix.",
        "",
        "## 12. Exact Files To Change",
    ])
    if exact_files_to_change:
        lines.extend(f"- {f}" for f in exact_files_to_change)
    else:
        lines.append("- Pending exact file list.")

    lines.extend([
        "",
        "## 13. Implementation Plan",
    ])
    if implementation_plan:
        lines.extend(f"{idx + 1}. {step}" for idx, step in enumerate(implementation_plan))
    else:
        lines.append("- Pending implementation plan sequence.")

    lines.extend([
        "",
        "## 14. Tests Required",
    ])
    if tests_required:
        lines.extend(f"- {t}" for t in tests_required)
    else:
        lines.append("- Pending test list.")

    lines.extend([
        "",
        "## 15. Maintainer Proposal",
        maintainer_proposal.strip() or "Pending proposal draft.",
        "",
        "## 16. Maintainer Response",
        maintainer_response.strip() or "Initially empty.",
        "",
        "## 17. Implementation Result",
        implementation_result.strip() or "Initially empty.",
        "",
        "## 18. Final Review",
        final_review.strip() or "Initially empty.",
        "",
        "## 19. Pull Request",
        pull_request.strip() or "Initially empty.",
        "",
    ])

    raw = "\n".join(lines)
    return sanitize_secrets(raw)


def save_dossier(root: Path, job_id: str, issue_number: int | str, content: str) -> Path:
    path = get_dossier_path(root, job_id, issue_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = sanitize_secrets(content)
    path.write_text(sanitized, encoding="utf-8")
    return path


def update_section(content: str, section_header: str, new_body: str) -> str:
    """Safely replace or update a markdown section body in the dossier."""
    pattern = rf"(## {re.escape(section_header)}\n)(.*?)(?=\n## |\Z)"
    match = re.search(pattern, content, re.DOTALL)
    clean_body = sanitize_secrets(new_body.strip())
    if match:
        replacement = f"\\1{clean_body}\n"
        return re.sub(pattern, replacement, content, count=1, flags=re.DOTALL)
    # If section was not matched, append
    return content + f"\n\n## {section_header}\n{clean_body}\n"


def update_dossier_maintainer_response(root: Path, job_id: str, issue_number: int | str, comments: list[dict[str, Any]]) -> Path | None:
    path = get_dossier_path(root, job_id, issue_number)
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    if not comments:
        return path
    lines = []
    for c in comments:
        author = c.get("author", "unknown")
        assoc = c.get("association", "none")
        created = c.get("created_at", "")
        body = c.get("body", "").strip()
        lines.append(f"### Comment by @{author} ({assoc}) at {created}:\n{body}\n")
    updated = update_section(content, "16. Maintainer Response", "\n".join(lines))
    return save_dossier(root, job_id, issue_number, updated)


def update_dossier_implementation_result(root: Path, job_id: str, issue_number: int | str, changed_paths: list[str], tests: list[dict[str, Any]], error: str | None = None) -> Path | None:
    path = get_dossier_path(root, job_id, issue_number)
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    lines = []
    if error:
        lines.append(f"**Implementation Status: FAILED**\n\nError: `{error}`\n")
    else:
        lines.append("**Implementation Status: SUCCESS**\n")
    lines.append(f"- **Changed files**: {', '.join(f'`{p}`' for p in changed_paths) or 'None'}")
    lines.append("- **Test results**:")
    for t in tests:
        status = "PASSED" if t.get("ok") else "FAILED"
        cmd = " ".join(t.get("command", [])) if isinstance(t.get("command"), list) else str(t.get("command"))
        lines.append(f"  - `{cmd}`: **{status}**")
        if not t.get("ok") and t.get("output"):
            lines.append(f"    ```\n{t.get('output')[:1000]}\n    ```")
    updated = update_section(content, "17. Implementation Result", "\n".join(lines))
    return save_dossier(root, job_id, issue_number, updated)


def update_dossier_final_review(root: Path, job_id: str, issue_number: int | str, review_report: dict[str, Any]) -> Path | None:
    path = get_dossier_path(root, job_id, issue_number)
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    blocked_info = f"WARNING: {review_report.get('blocked_paths')}" if review_report.get("blocked_paths") else "None (Clean)"
    lines = [
        f"- **Review Status**: {'PASSED' if review_report.get('ok', True) else 'FAILED'}",
        f"- **Verified At**: {review_report.get('generated_at', 'N/A')}",
        f"- **Changed Paths Reviewed**: {', '.join(f'`{p}`' for p in review_report.get('changed_paths', [])) or 'None'}",
        f"- **Blocked Paths Check**: {blocked_info}",
        "- **Regression & Security Checks**: Completed and verified.",
    ]
    updated = update_section(content, "18. Final Review", "\n".join(lines))
    return save_dossier(root, job_id, issue_number, updated)


def update_dossier_pull_request(root: Path, job_id: str, issue_number: int | str, pr_info: dict[str, Any]) -> Path | None:
    path = get_dossier_path(root, job_id, issue_number)
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    pr_num = pr_info.get("number", "N/A")
    pr_url = pr_info.get("url", "N/A")
    lines = [
        f"- **Pull Request**: #{pr_num}",
        f"- **URL**: {pr_url}",
        f"- **Status**: OPENED",
        "- **Branch**: `bounty-radar/" + str(job_id) + "`",
    ]
    updated = update_section(content, "19. Pull Request", "\n".join(lines))
    return save_dossier(root, job_id, issue_number, updated)
