from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_quota_retry_reuses_saved_evidence():
    source = (ROOT / "worker.py").read_text(encoding="utf-8")
    assert 'quota_retry = job.get("state") == "WAITING_FOR_AI_QUOTA"' in source
    assert "_saved_ingestion_path(root, job)" in source
    branch = source.split("if quota_retry:", 1)[1].split("else:", 1)[0]
    assert "accept_and_ingest(" not in branch


def test_saved_ingestion_path_requires_existing_artifact():
    source = (ROOT / "worker.py").read_text(encoding="utf-8")
    assert 'value = (job.get("artifacts") or {}).get("ingestion")' in source
    assert "return candidate if candidate.exists() else None" in source


def test_waiting_quota_state_is_declared_once():
    source = (ROOT / "jobs.py").read_text(encoding="utf-8")
    declarations = [line for line in source.splitlines() if '"WAITING_FOR_AI_QUOTA":' in line]
    assert len(declarations) == 1, declarations
