#!/usr/bin/env python3
"""Evidence-first verification gate for AI review artifacts.

Validates that:
1. Every claim is grounded in repository evidence (issue evidence + code/repo evidence).
2. Root cause and proposed fix are explicitly articulated with affected locations.
3. Reviewers are not blindly majority-voted; material disagreements halt automatic progression
   and mark the investigation as requiring further investigation.
4. Confidence scores and citations meet minimum verification thresholds.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from jobs import load_store, save_store, utc_now

CITE = re.compile(r"(?:ISSUE|COMMENT:\d+|FILE:[^\s,;\"'\]]+|TREE:[^\s,;\"'\]]+|WORKFLOW:[^\s,;\"'\]]+|TIMELINE:\d+)")
FILE_CITE = re.compile(r"(?:FILE:[^\s,;\"'\]]+|WORKFLOW:[^\s,;\"'\]]+)")
ISSUE_CITE = re.compile(r"(?:ISSUE|COMMENT:\d+|TIMELINE:\d+)")

MIN_CONFIDENCE_THRESHOLD = 50


def check_disagreements(reviews: list[dict[str, Any]]) -> tuple[list[str], bool]:
    """Detect material disagreement between independent AI reviews.

    Returns (list_of_disagreement_descriptions, is_material_disagreement).
    """
    if len(reviews) < 2:
        return [], False

    disagreements = []
    # Extract file sets cited by each provider
    provider_files: dict[str, set[str]] = {}
    provider_causes: dict[str, str] = {}
    provider_confidences: dict[str, float] = {}

    for rev in reviews:
        name = rev.get("provider", "unknown")
        citations = CITE.findall(json.dumps(rev, ensure_ascii=False))
        files = {c.split(":", 1)[1] for c in citations if c.startswith("FILE:")}
        provider_files[name] = files
        provider_causes[name] = rev.get("root_cause_hypothesis", "").lower()
        provider_confidences[name] = float(rev.get("confidence", 0))

    names = list(provider_files.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            p1, p2 = names[i], names[j]
            f1, f2 = provider_files[p1], provider_files[p2]

            # If both reviewers identified specific files to change/investigate, check overlap
            if f1 and f2 and not (f1 & f2):
                disagreements.append(
                    f"Disagreement on affected files: {p1} cited {sorted(f1)} whereas {p2} cited {sorted(f2)}"
                )

            # Check for large divergence in confidence
            if abs(provider_confidences[p1] - provider_confidences[p2]) >= 40:
                disagreements.append(
                    f"Significant confidence divergence: {p1} ({provider_confidences[p1]}%) vs {p2} ({provider_confidences[p2]}%)"
                )

    is_material = len(disagreements) > 0
    return disagreements, is_material


def evaluate_review(review_obj: dict[str, Any], path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    errors = []
    summary = review_obj.get("summary")
    if not summary or not isinstance(summary, str):
        errors.append(f"{path}: summary is missing or empty")

    root_cause = review_obj.get("root_cause_hypothesis")
    if not root_cause or not isinstance(root_cause, str):
        errors.append(f"{path}: root_cause_hypothesis is missing or empty")

    proposed_fix = review_obj.get("proposed_fix")
    if not proposed_fix or not isinstance(proposed_fix, str):
        errors.append(f"{path}: proposed_fix is missing or empty")

    facts = review_obj.get("confirmed_facts")
    if not isinstance(facts, list):
        errors.append(f"{path}: confirmed_facts must be a list")

    evidence = review_obj.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{path}: evidence list is missing or empty")

    joined = json.dumps(review_obj, ensure_ascii=False)
    all_cites = set(CITE.findall(joined))
    issue_cites = set(ISSUE_CITE.findall(joined))
    file_cites = set(FILE_CITE.findall(joined))

    if not issue_cites:
        errors.append(f"{path}: no issue/comment citations found (requires ISSUE or COMMENT:<n>)")
    if not file_cites:
        errors.append(f"{path}: no code/repository citations found (requires FILE:<path> or WORKFLOW:<path>)")

    confidence = review_obj.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 100):
        errors.append(f"{path}: confidence must be a number between 0 and 100")
    elif confidence < MIN_CONFIDENCE_THRESHOLD:
        errors.append(f"{path}: confidence {confidence}% is below minimum threshold ({MIN_CONFIDENCE_THRESHOLD}%)")

    if errors:
        return None, errors

    verified_info = {
        "provider": review_obj.get("provider", "unknown"),
        "confidence": confidence,
        "summary": summary,
        "root_cause": root_cause,
        "proposed_fix": proposed_fix,
        "citations": sorted(all_cites),
        "file_citations": sorted(file_cites),
        "issue_citations": sorted(issue_cites),
        "confirmed_facts": facts[:10] if isinstance(facts, list) else [],
    }
    return verified_info, []


def main() -> int:
    root = Path(".")
    jid = os.environ.get("JOB_ID", "").strip()
    if not jid:
        print("error: JOB_ID environment variable is required", file=sys.stderr)
        return 2

    data = load_store(root / "jobs.json")
    job = data["jobs"].get(jid)
    if not job:
        print(f"error: job not found: {jid}", file=sys.stderr)
        return 2

    if job.get("state") not in {"INVESTIGATING", "CROSS_REVIEW", "DIAGNOSIS_READY"}:
        print(f"error: job {jid} is not awaiting review gate (current: {job.get('state')})", file=sys.stderr)
        return 1

    review_paths = job.get("artifacts", {}).get("reviews", [])
    if not review_paths:
        print(f"error: job {jid} has no review artifacts to evaluate", file=sys.stderr)
        return 1

    raw_reviews: list[dict[str, Any]] = []
    verified_reviews: list[dict[str, Any]] = []
    all_errors: list[str] = []

    for raw in review_paths:
        p = root / raw
        if not p.exists():
            all_errors.append(f"missing review artifact: {raw}")
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            raw_reviews.append(obj)
            v_info, errs = evaluate_review(obj, p)
            if errs:
                all_errors.extend(errs)
            elif v_info:
                verified_reviews.append(v_info)
        except Exception as exc:
            all_errors.append(f"{raw}: failed to parse JSON: {exc}")

    if all_errors and not verified_reviews:
        error_summary = "Review gate failed verification:\n" + "\n".join(f"- {e}" for e in all_errors)
        print(error_summary, file=sys.stderr)
        job["error"] = error_summary[:2000]
        job.setdefault("history", []).append({
            "at": utc_now(),
            "event": "REVIEW_GATE_FAILED",
            "errors": all_errors[:5],
        })
        save_store(data, root / "jobs.json")
        return 1

    # Check for material disagreements between reviewers
    disagreements, is_material = check_disagreements(raw_reviews)
    if is_material:
        print(f"Review gate: material disagreements detected across independent reviews for {jid}:", file=sys.stderr)
        for d in disagreements:
            print(f"  - {d}", file=sys.stderr)

    out = root / "job-artifacts" / jid / "review-gate.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    gate_report = {
        "generated_at": utc_now(),
        "job_id": jid,
        "verified_reviews": verified_reviews,
        "validation_errors": all_errors,
        "disagreements": disagreements,
        "material_disagreement": is_material,
        "verdict": "REQUIRES_FURTHER_INVESTIGATION" if is_material else "VERIFIED",
    }
    out.write_text(json.dumps(gate_report, indent=2, ensure_ascii=False), encoding="utf-8")

    job["artifacts"]["review_gate"] = str(out.as_posix())
    if is_material:
        # Do not pretend confidence; mark that investigation requires further clarification
        job["state"] = "CROSS_REVIEW"
        job["review_status"] = "REQUIRES_FURTHER_INVESTIGATION"
        job["disagreements"] = disagreements
    else:
        job["state"] = "DIAGNOSIS_READY"
        job["review_status"] = "VERIFIED"

    job["updated_at"] = utc_now()
    job.setdefault("history", []).append({
        "at": utc_now(),
        "event": "REVIEW_GATE_COMPLETED",
        "verdict": gate_report["verdict"],
        "providers": [x["provider"] for x in verified_reviews],
    })
    save_store(data, root / "jobs.json")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
