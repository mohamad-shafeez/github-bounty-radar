#!/usr/bin/env python3
"""Multi-provider evidence reviewer with Gemini, OpenRouter, and Groq support.

Provides independent Gemini/OpenRouter review with Groq fallback.
- Gemini and OpenRouter are independent reviewers; Groq is fallback when primary reviewers are unavailable.
- Quota/outage failures are handled without hammering a provider.
- Gemini, OpenRouter, and Groq are supported.
- Keys are read from environment variables / secrets only.
- Never assumes free-tier is permanently available; reports availability honestly.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

from jobs import load_store, save_store, utc_now

SYSTEM = (
    "You are an independent software-repository investigator. Evidence comes from a GitHub issue, "
    "comments, repository metadata, tree, tests, workflows, manifests and selected source files. "
    "Do not treat another AI's answer as evidence. Do not invent files, behavior, root causes, "
    "maintainer intent, or test results. Every substantive claim must cite evidence using one of: "
    "ISSUE, COMMENT:<index>, FILE:<path>, TREE:<path>, WORKFLOW:<path>, or TIMELINE:<index>. "
    "If evidence is insufficient, say so and request specific additional evidence. "
    "Return strict JSON with keys: summary, root_cause_hypothesis, confirmed_facts, unknowns, "
    "evidence, proposed_fix, risks, tests_to_run, confidence (0-100)."
)

SYSTEM_DIFF = (
    "You are a conservative coding agent. Return ONLY a unified git diff. "
    "Never modify CI/workflows, credentials, secrets, dependency lockfiles unless directly necessary, "
    "or unrelated files. Do not invent APIs. The diff must be grounded in supplied evidence."
)

MAX_RETRIES = int(os.environ.get("AI_MAX_RETRIES", "3"))
INITIAL_BACKOFF = float(os.environ.get("AI_INITIAL_BACKOFF", "1.0"))
BACKOFF_FACTOR = float(os.environ.get("AI_BACKOFF_FACTOR", "2.0"))


def get_gemini_config() -> dict[str, str]:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    return {"key": key, "url": url, "model": model}


def get_groq_config() -> dict[str, str]:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    url = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1/chat/completions").strip()
    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
    return {"key": key, "url": url, "model": model}


def get_openrouter_config() -> dict[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions").strip()
    model = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-r1:free").strip()
    return {"key": key, "url": url, "model": model}

def build_prompt(evidence: dict[str, Any]) -> str:
    compact = {
        "repository": evidence.get("repository"),
        "issue": evidence.get("issue"),
        "comments": evidence.get("comments", [])[:250],
        "timeline": evidence.get("timeline", [])[:250],
        "commits": evidence.get("commits", [])[:10],
        "related_pull_requests": evidence.get("related_pull_requests", []),
        "tree": evidence.get("tree", [])[:5000],
        "selected_files": evidence.get("selected_files", []),
        "files": evidence.get("files", {}),
    }
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _call_gemini_raw(prompt: str, system: str, json_mode: bool = True, timeout: int = 120) -> str:
    cfg = get_gemini_config()
    key = cfg["key"]
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    payload: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1},
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    r = requests.post(cfg["url"], params={"key": key}, json=payload, timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError(f"Gemini rate limit / quota exceeded (HTTP 429): {r.text[:300]}")
    if r.status_code >= 400:
        raise RuntimeError(f"Gemini API error (HTTP {r.status_code}): {r.text[:300]}")
    data = r.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"Gemini returned empty candidates: {data}")
    return "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []))


def _call_openai_compatible_raw(provider: str, cfg: dict[str, str], prompt: str, system: str, json_mode: bool = True, timeout: int = 120) -> str:
    key = cfg["key"]
    if not key:
        raise RuntimeError(f"{provider.upper()}_API_KEY is not configured")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if provider == "openrouter":
        headers["HTTP-Referer"] = os.environ.get("OPENROUTER_HTTP_REFERER", "https://github.com/mohamad-shafeez/github-bounty-radar")
        headers["X-Title"] = os.environ.get("OPENROUTER_X_TITLE", "GitHub Bounty Radar")
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = requests.post(cfg["url"], headers=headers, json=payload, timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError(f"{provider.title()} rate limit / quota exceeded (HTTP 429): {r.text[:300]}")
    if r.status_code >= 400:
        raise RuntimeError(f"{provider.title()} API error (HTTP {r.status_code}): {r.text[:300]}")
    data = r.json()
    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError(f"{provider.title()} returned empty choices: {data}")
    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return str(content)


def _call_groq_raw(prompt: str, system: str, json_mode: bool = True, timeout: int = 120) -> str:
    return _call_openai_compatible_raw("groq", get_groq_config(), prompt, system, json_mode=json_mode, timeout=timeout)


def _call_openrouter_raw(prompt: str, system: str, json_mode: bool = True, timeout: int = 120) -> str:
    return _call_openai_compatible_raw("openrouter", get_openrouter_config(), prompt, system, json_mode=json_mode, timeout=timeout)

def _parse_and_validate_review_json(text: str, provider: str) -> dict[str, Any]:
    # Extract json block if wrapped in markdown code fence
    cleaned = text.strip()
    match = re.search(r"```(?:json)?\s*\n(.*?)```", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(1).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"malformed JSON from {provider}: {exc} (raw: {text[:200]})") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"expected JSON object from {provider}, got {type(obj).__name__}")
    required_keys = {"summary", "root_cause_hypothesis", "confirmed_facts", "evidence", "proposed_fix", "confidence"}
    missing = required_keys - set(obj.keys())
    if missing:
        raise RuntimeError(f"provider {provider} missing required keys: {sorted(missing)}")
    obj["provider"] = provider
    obj["generated_at"] = utc_now()
    return obj


def is_quota_error(exc):
    text=str(exc).lower()
    return any(x in text for x in ("429", "rate limit", "rate-limit", "quota", "resource exhausted"))


def run_provider_with_retry(name: str, prompt: str, system: str = SYSTEM, json_mode: bool = True) -> dict[str, Any] | str:
    """Execute provider call with retry policy on timeout, rate-limits, and transient failures."""
    name = name.lower().strip()
    if name not in ("gemini", "openrouter", "groq"):
        raise ValueError(f"unsupported provider: {name}. Supported: gemini, openrouter, groq")

    max_retries = int(os.environ.get("AI_MAX_RETRIES", "3"))
    backoff = float(os.environ.get("AI_INITIAL_BACKOFF", "1.0"))
    backoff_factor = float(os.environ.get("AI_BACKOFF_FACTOR", "2.0"))
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            if name == "gemini":
                raw_text = _call_gemini_raw(prompt, system, json_mode=json_mode)
            elif name == "openrouter":
                raw_text = _call_openrouter_raw(prompt, system, json_mode=json_mode)
            else:
                raw_text = _call_groq_raw(prompt, system, json_mode=json_mode)

            if json_mode:
                return _parse_and_validate_review_json(raw_text, name)
            return raw_text
        except Exception as exc:
            last_error = exc
            err_msg = str(exc)
            # Quota/rate-limit errors must immediately stop this provider so the
            # fallback provider gets a chance. Never hammer a rate-limited API.
            if is_quota_error(exc):
                raise
            # Permanent errors (missing key, auth error 401/403) should not retry
            if "not configured" in err_msg or "HTTP 401" in err_msg or "HTTP 403" in err_msg:
                break
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
                backoff *= BACKOFF_FACTOR

    raise RuntimeError(f"Provider {name} failed after {MAX_RETRIES} attempts: {last_error}")


def run_with_fallback(
    prompt: str,
    system: str = SYSTEM,
    json_mode: bool = True,
    primary: str = "gemini",
    fallback: str = "openrouter",
) -> tuple[dict[str, Any] | str, str]:
    """Attempt primary provider with retry; if it fails, fall back to alternate provider."""
    errors: list[str] = []
    # 1. Attempt primary
    try:
        res = run_provider_with_retry(primary, prompt, system, json_mode=json_mode)
        return res, primary
    except Exception as exc:
        msg = f"Primary provider '{primary}' failed: {exc}"
        errors.append(msg)

    # 2. Attempt fallback
    try:
        res = run_provider_with_retry(fallback, prompt, system, json_mode=json_mode)
        return res, fallback
    except Exception as exc:
        msg = f"Fallback provider '{fallback}' also failed: {exc}"
        errors.append(msg)

    raise RuntimeError(f"All AI providers failed: {' | '.join(errors)}")


def configured_providers() -> list[str]:
    """Return configured providers in deterministic review order."""
    available: list[str] = []
    if get_gemini_config()["key"]:
        available.append("gemini")
    if get_openrouter_config()["key"]:
        available.append("openrouter")
    if get_groq_config()["key"]:
        available.append("groq")
    return available


def review_job(root: Path, job_id: str) -> list[Path]:
    """Perform independent AI review for an accepted bounty job."""
    data = load_store(root / "jobs.json")
    job = data["jobs"].get(job_id)
    if not job:
        raise RuntimeError(f"job not found: {job_id}")
    if job["state"] not in {"INVESTIGATING", "CROSS_REVIEW"}:
        raise RuntimeError(f"job must be INVESTIGATING/CROSS_REVIEW; current={job['state']}")

    ingestion = Path(job["artifacts"]["ingestion"])
    evidence = json.loads(ingestion.read_text(encoding="utf-8"))
    prompt = build_prompt(evidence)

    out = root / "job-artifacts" / job_id / "reviews"
    out.mkdir(parents=True, exist_ok=True)

    available = configured_providers()
    if not available:
        raise RuntimeError("No AI provider credentials configured. Set GEMINI_API_KEY, OPENROUTER_API_KEY, or GROQ_API_KEY.")

    outputs: list[Path] = []
    errors: list[dict[str, str]] = []

    # If user explicitly requested providers via AI_PROVIDERS, filter to supported ones
    requested_raw = [x.strip().lower() for x in os.environ.get("AI_PROVIDERS", "").split(",") if x.strip()]
    target_providers = [p for p in requested_raw if p in ("gemini", "openrouter", "groq")] or [p for p in ("gemini", "openrouter") if p in available]

    for name in target_providers:
        try:
            # Run the provider with its retry policy
            result = run_provider_with_retry(name, prompt, system=SYSTEM, json_mode=True)
            p = out / f"{name}.json"
            p.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            outputs.append(p)
        except Exception as exc:
            errors.append({"provider": name, "error": str(exc)[:1000]})

    # If no provider succeeded and we have a fallback available that wasn't tried, try fallback
    if not outputs:
        # Determine fallback
        failed_names = {e["provider"] for e in errors}
        fallback_candidates = [p for p in ("gemini", "openrouter", "groq") if p not in failed_names and p in available]
        for name in fallback_candidates:
            try:
                result = run_provider_with_retry(name, prompt, system=SYSTEM, json_mode=True)
                p = out / f"{name}.json"
                p.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
                outputs.append(p)
                break
            except Exception as exc:
                errors.append({"provider": name, "error": str(exc)[:1000]})

    if not outputs:
        raise RuntimeError(f"All AI providers failed: {errors}")

    data = load_store(root / "jobs.json")
    job = data["jobs"][job_id]
    job["artifacts"]["reviews"] = [str(p.as_posix()) for p in outputs]
    job["artifacts"]["review_errors"] = errors
    job["review_summary"] = {"providers": [p.stem for p in outputs], "independent": True}
    job["state"] = "CROSS_REVIEW" if len(outputs) > 1 else "DIAGNOSIS_READY"
    job["updated_at"] = utc_now()
    job.setdefault("history", []).append({
        "at": utc_now(),
        "event": "AI_REVIEWS_COMPLETED",
        "providers": [p.stem for p in outputs],
    })
    save_store(data, root / "jobs.json")
    return outputs


def generate_diff(prompt: str, primary: str = "gemini", fallback: str = "openrouter") -> str:
    """Generate a unified diff with Gemini primary and OpenRouter fallback."""
    res, provider = run_with_fallback(
        prompt=prompt,
        system=SYSTEM_DIFF,
        json_mode=False,
        primary=primary,
        fallback=fallback,
    )
    return str(res)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    args = parser.parse_args()
    paths = review_job(Path("."), args.job_id)
    print("\n".join(map(str, paths)))
