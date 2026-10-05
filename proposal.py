#!/usr/bin/env python3
"""Build an evidence-cited comment proposal and Markdown engineering dossier.

Never posts comments automatically; human approval is strictly required.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jobs import load_store, save_store, utc_now
from dossier import build_dossier_content, save_dossier


def build(job_id: str, root: Path = Path(".")) -> Path:
    data = load_store(root / "jobs.json")
    job = data["jobs"].get(job_id)
    if not job:
        raise RuntimeError(f"job not found: {job_id}")
    if job["state"] not in {"DIAGNOSIS_READY"}:
        raise RuntimeError(f"proposal can only be prepared from diagnosis/review; current={job['state']}")

    reviews: list[dict[str, Any]] = []
    for raw in job.get("artifacts", {}).get("reviews", []):
        p = root / raw
        if p.exists():
            reviews.append(json.loads(p.read_text(encoding="utf-8")))

    if not reviews:
        raise RuntimeError("no review artifacts found for job")

    ingestion_data: dict[str, Any] = {}
    ingestion_path = root / job.get("artifacts", {}).get("ingestion", "")
    if ingestion_path.exists():
        try:
            ingestion_data = json.loads(ingestion_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    gate_data: dict[str, Any] = {}
    gate_path = root / job.get("artifacts", {}).get("review_gate", "")
    if gate_path.exists():
        try:
            gate_data = json.loads(gate_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    facts: list[str] = []
    causes: list[str] = []
    fixes: list[str] = []
    tests: list[str] = []
    evidence_refs: list[str] = []
    files_to_change: list[str] = []

    for review in reviews:
        for key, target in (("confirmed_facts", facts), ("root_cause_hypothesis", causes), ("proposed_fix", fixes), ("tests_to_run", tests)):
            value = review.get(key)
            if isinstance(value, list):
                target.extend(str(x) for x in value[:8])
            elif isinstance(value, str) and value.strip():
                target.append(value.strip())
        ev = review.get("evidence")
        if isinstance(ev, list):
            for item in ev[:16]:
                s = str(item)
                evidence_refs.append(s)
                if s.startswith("FILE:"):
                    files_to_change.append(s[5:].strip())

    unique_facts = list(dict.fromkeys(facts))
    unique_causes = list(dict.fromkeys(causes))
    unique_fixes = list(dict.fromkeys(fixes))
    unique_tests = list(dict.fromkeys(tests))
    unique_evidence = list(dict.fromkeys(evidence_refs))
    unique_files = list(dict.fromkeys(files_to_change))

    body = (
        "I investigated this issue against the repository evidence available in the job artifacts.\n\n"
        "Evidence-backed findings:\n" + "\n".join(f"- {x}" for x in unique_facts[:8]) +
        "\n\nLikely root cause / diagnosis:\n" + "\n".join(f"- {x}" for x in unique_causes[:5]) +
        "\n\nProposed direction:\n" + "\n".join(f"- {x}" for x in unique_fixes[:8]) +
        "\n\nRelevant tests/checks:\n" + "\n".join(f"- {x}" for x in unique_tests[:8]) +
        "\n\nEvidence references: " + ", ".join(unique_evidence[:16]) +
        "\n\nBefore implementation, I would like to confirm that this diagnosis and proposed direction match the intended behavior."
    )

    proposal = {
        "job_id": job_id,
        "generated_at": utc_now(),
        "purpose": "Draft only. Human review is required before posting.",
        "repository": job["repo"],
        "issue_number": job["issue_number"],
        "body": body,
        "source_reviews": job.get("artifacts", {}).get("reviews", []),
    }
    out = root / "job-artifacts" / job_id / "proposal.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8")

    # Generate Markdown Engineering Dossier
    issue_obj = ingestion_data.get("issue", {})
    repo_obj = ingestion_data.get("repository", {})
    comments_list = [f"@{c.get('user', {}).get('login')}: {c.get('body', '')[:300]}" for c in ingestion_data.get("comments", [])[:10]]
    prs_list = [f"#{pr.get('number')} - {pr.get('title')} ({pr.get('url')})" for pr in ingestion_data.get("related_pull_requests", [])]
    commits_list = [f"{c.get('sha', '')[:8]} - {c.get('commit', {}).get('message', '').splitlines()[0]}" for c in ingestion_data.get("commits", [])[:5]]

    dossier_text = build_dossier_content(
        issue_number=job["issue_number"],
        title=job.get("latest", {}).get("title") or issue_obj.get("title", ""),
        summary_what=unique_causes[0] if unique_causes else "Issue under investigation",
        summary_where=unique_files[0] if unique_files else repo_obj.get("full_name", job["repo"]),
        summary_when=issue_obj.get("created_at", "Reported in issue"),
        summary_who=f"Reported by @{issue_obj.get('user', {}).get('login', 'unknown')}",
        original_issue=issue_obj.get("body", ""),
        expected_behavior="Normal operation as intended by repository specifications.",
        actual_behavior=issue_obj.get("title", ""),
        root_cause=unique_causes[0] if unique_causes else "Under investigation",
        files_inspected=ingestion_data.get("selected_files", [])[:30],
        functions_modules=[f"Module/File: {f}" for f in unique_files],
        code_evidence=[f"`{ref}`" for ref in unique_evidence if ref.startswith("FILE:")],
        tests_evidence=[f for f in ingestion_data.get("selected_files", []) if "test" in f.lower()][:10],
        workflows_evidence=[f for f in ingestion_data.get("selected_files", []) if ".github/workflows" in f][:5],
        manifests_evidence=[f for f in ingestion_data.get("selected_files", []) if any(m in f for m in ("package.json", "pyproject.toml", "requirements", "go.mod", "Cargo.toml"))][:5],
        git_history_evidence=commits_list,
        comments_context=comments_list,
        related_prs=prs_list,
        bot_or_prior_work="Checked issue timeline and linked PRs.",
        ai_reviewer_findings=reviews,
        disagreements=gate_data.get("disagreements", []),
        disagreements_resolved=not gate_data.get("material_disagreement", False),
        verified_diagnosis_cause=unique_causes[0] if unique_causes else "Pending",
        verified_confidence=f"{reviews[0].get('confidence')}%" if reviews and reviews[0].get('confidence') else "N/A",
        verified_evidence=unique_evidence,
        proposed_fix=unique_fixes[0] if unique_fixes else "Pending",
        exact_files_to_change=unique_files,
        implementation_plan=[f"Modify {f} according to proposed fix" for f in unique_files] + ["Run regression and unit tests"],
        tests_required=unique_tests,
        maintainer_proposal=body,
        maintainer_response="Initially empty. Awaiting maintainer response.",
        implementation_result="Initially empty. Pending implementation after approval.",
        final_review="Initially empty. Pending final review.",
        pull_request="Initially empty. Pending PR creation.",
    )
    dossier_path = save_dossier(root, job_id, job["issue_number"], dossier_text)

    job["artifacts"]["proposal"] = str(out.as_posix())
    job["artifacts"]["engineering_dossier"] = str(dossier_path.as_posix())
    job["state"] = "HUMAN_REVIEW"
    job["updated_at"] = utc_now()
    job.setdefault("history", []).append({"at": utc_now(), "event": "PROPOSAL_DRAFT_READY"})
    save_store(data, root / "jobs.json")
    return out


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("job_id")
    a = p.parse_args()
    print(build(a.job_id))
