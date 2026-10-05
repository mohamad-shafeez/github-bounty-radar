#!/usr/bin/env python3
"""Cloud worker for the accepted -> evidence -> independent review stage."""
from __future__ import annotations

import os
import time
from pathlib import Path

from ai_review import review_job, is_quota_error
from review_gate import main as review_gate_main
from pipeline import accept_and_ingest
from jobs import load_store, save_store, utc_now
from proposal import build as build_proposal
from notify import send
from dossier import build_dossier_content, save_dossier


def _saved_ingestion_path(root: Path, job: dict) -> Path | None:
    value = (job.get("artifacts") or {}).get("ingestion")
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate if candidate.exists() else None


def main() -> int:
    root = Path(".")
    data = load_store(root / "jobs.json")
    requested = os.environ.get("JOB_ID", "").strip()

    # Process ACCEPTED or INGESTING jobs
    processable_states = {"ACCEPTED", "INGESTING", "WAITING_FOR_AI_QUOTA"}
    ids = [requested] if requested else [
        jid for jid, job in data["jobs"].items() if job.get("state") in processable_states
    ]
    if not ids:
        print("No ACCEPTED jobs to process.")
        return 0

    for job_id in ids:
        data = load_store(root / "jobs.json")
        job = data["jobs"].get(job_id)
        if not job:
            print(f"Skipping unknown job {job_id}")
            continue
        if job.get("state") not in processable_states:
            print(f"Skipping {job_id}: state={job.get('state')}")
            continue

        # Durable quota cooldown: evidence already collected is preserved.
        if job.get("state") == "WAITING_FOR_AI_QUOTA":
            retry_after = float(job.get("ai_retry_after", 0) or 0)
            if time.time() < retry_after:
                print(f"Waiting for AI quota cooldown: {job_id}")
                continue
            job["state"] = "INVESTIGATING"
            job.pop("ai_retry_after", None)
            data["jobs"][job_id] = job
            from jobs import save_store
            save_store(data, root / "jobs.json")

        quota_retry = job.get("state") == "WAITING_FOR_AI_QUOTA"

        if quota_retry:
            saved_path = _saved_ingestion_path(root, job)
            if saved_path is None:
                raise RuntimeError(f"Quota retry for {job_id} has no saved ingestion evidence; refusing to re-ingest automatically.")
            path = saved_path
            print(f"Reusing saved evidence for quota retry {job_id}: {path}")
        else:
            print(f"Ingesting evidence for {job_id} ({job['repo']}#{job['issue_number']})")
            path = accept_and_ingest(job_id, root, os.environ.get("GITHUB_TOKEN"))
            print(f"Evidence: {path}")

        # Check for Gemini or Grok credentials
        has_ai_key = any(os.environ.get(k, "").strip() for k in ("GEMINI_API_KEY", "GROK_API_KEY"))
        if not has_ai_key:
            note = root / "job-artifacts" / job_id / "ai-review-required.json"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text('{"status":"waiting_for_ai_provider_credentials","required":["GEMINI_API_KEY","GROK_API_KEY"]}\n', encoding="utf-8")
            print("Evidence ingestion complete; no Gemini or Grok secret is configured yet.")

            # Still generate initial engineering dossier with repository evidence
            dossier_text = build_dossier_content(
                issue_number=job["issue_number"],
                title=job.get("latest", {}).get("title", ""),
                summary_what="Awaiting AI provider credentials (GEMINI_API_KEY or GROK_API_KEY)",
                summary_where=job["repo"],
                root_cause="NOT TESTED — requires external credential/permission for AI investigation.",
                maintainer_proposal="Pending AI investigation after secrets configuration.",
            )
            d_path = save_dossier(root, job_id, job["issue_number"], dossier_text)
            data = load_store(root / "jobs.json")
            job = data["jobs"][job_id]
            job["artifacts"]["engineering_dossier"] = str(d_path.as_posix())
            save_store(data, root / "jobs.json")
            continue

        print(f"Running independent AI review for {job_id}")
        try:
            outputs = review_job(root, job_id)
        except Exception as exc:
            if is_quota_error(exc):
                cooldown = int(os.environ.get("AI_QUOTA_COOLDOWN_SECONDS", "900"))
                data = load_store(root / "jobs.json")
                job = data["jobs"][job_id]
                job["state"] = "WAITING_FOR_AI_QUOTA"
                job["ai_retry_after"] = time.time() + cooldown
                job["ai_quota_error"] = str(exc)[-2000:]
                job.setdefault("history", []).append({"at": utc_now(), "state": "WAITING_FOR_AI_QUOTA", "reason": "AI quota/rate limit"})
                save_store(data, root / "jobs.json")
                print(f"AI quota exhausted; preserved evidence and deferred retry for {cooldown}s")
                continue
            raise
        print("Review outputs:")
        for output in outputs:
            print(f" - {output}")

        os.environ["JOB_ID"] = job_id
        gate_code = review_gate_main()
        if gate_code != 0:
            print(f"Review gate halted job {job_id} from automatic progression.")
            continue

        # Prepare proposal and generate the complete 19-section engineering dossier
        print(f"Preparing proposal and engineering dossier for {job_id}")
        build_proposal(job_id, root)

        send(
            f"Bounty investigation ready — {job['repo']}#{job['issue_number']}",
            f"Independent review and proposal completed for job {job_id}. Review the proposal and engineering dossier to decide on POST_COMMENT or DECLINE.",
            click=f"https://github.com/{os.environ.get('GITHUB_REPOSITORY','')}/actions/workflows/job-control.yml",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
