from __future__ import annotations

import base64
import json
import os
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

# In-process dedup (same warm container); DB dedup covers cross-invocation retries.
_PROCESSED_EVENT_UUIDS: set[str] = set()
_MAX_IN_MEMORY_DEDUP = 500

# --- configuration (from encrypted env vars in DO control panel) ---


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


GITLAB_BASE_URL = (_env("GITLAB_BASE_URL", "https://gitlab.com") or "").rstrip("/")
GITLAB_PROJECT_PATH = _env("GITLAB_PROJECT")
GITLAB_TOKEN = _env("GITLAB_TOKEN")
GITLAB_WEBHOOK_SECRET = _env("GITLAB_WEBHOOK_SECRET")
MODEL_ACCESS_KEY = _env("MODEL_ACCESS_KEY")
INFERENCE_BASE_URL = (_env("INFERENCE_BASE_URL", "https://inference.do-ai.run/v1") or "").rstrip("/")
INFERENCE_MODEL = _env("INFERENCE_MODEL", "anthropic-claude-4.6-sonnet")
MAX_DIFF_CHARS = int(_env("MAX_DIFF_CHARS", "48000") or "48000")
REVIEW_ON_ACTIONS = {
    a.strip()
    for a in (_env("REVIEW_ON_ACTIONS", "open,update,reopen") or "").split(",")
    if a.strip()
}


def _function_web_url() -> str:
    return (_env("FUNCTION_WEB_URL") or "").strip().rstrip("/")


def _worker_secret() -> str:
    return _env("REVIEW_WORKER_SECRET") or _env("GITLAB_WEBHOOK_SECRET") or ""


def _gitlab_webhook_secret() -> str:
    return _env("GITLAB_WEBHOOK_SECRET") or ""

# --- GitLab API ---


class GitLabError(Exception):
    pass


def _gitlab_request(method: str, path: str, body: dict | None = None) -> dict | list:
    token = _env("GITLAB_TOKEN")
    if not token:
        raise GitLabError("GITLAB_TOKEN is not configured")
    url = f"{GITLAB_BASE_URL}/api/v4{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitLabError(f"GitLab API {method} {path} failed ({exc.code}): {detail}") from exc


def fetch_mr_changes(project_id: int, mr_iid: int) -> str:
    changes = _gitlab_request("GET", f"/projects/{project_id}/merge_requests/{mr_iid}/changes")
    if not isinstance(changes, dict):
        return ""
    parts: list[str] = []
    for change in changes.get("changes", []):
        old_path = change.get("old_path") or change.get("new_path")
        new_path = change.get("new_path")
        parts.append(f"\n### {old_path} -> {new_path}\n")
        parts.append(change.get("diff") or "")
    return "\n".join(parts)


def post_mr_note(project_id: int, mr_iid: int, body: str) -> int:
    encoded = urllib.parse.quote(str(project_id), safe="")
    note = _gitlab_request(
        "POST",
        f"/projects/{encoded}/merge_requests/{mr_iid}/notes",
        {"body": body},
    )
    return int(note.get("id", 0)) if isinstance(note, dict) else 0


# --- inference ---


class InferenceError(Exception):
    pass


SYSTEM_PROMPT = """You are an expert software engineer performing merge request code review.
Analyze the diff for bugs, security issues, performance problems, and maintainability concerns.
Respond in Markdown with sections:
## Summary
## Findings
List each finding as a bullet, one per line: `- [SEVERITY] path:line — description`
where SEVERITY is CRITICAL, WARNING, or INFO (example: `- [CRITICAL] src/app.py:5 — issue`).
## Suggested changes
Be specific and actionable. If the diff is empty or trivial, say so briefly."""


@dataclass
class InferenceResult:
    review_text: str
    model_id: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


def run_code_review(diff_text: str, mr_title: str, project_path: str) -> InferenceResult:
    if not MODEL_ACCESS_KEY:
        raise InferenceError("MODEL_ACCESS_KEY is not configured")
    user_content = (
        f"Project: {project_path}\n"
        f"Merge request title: {mr_title}\n\n"
        f"```diff\n{diff_text}\n```"
    )
    payload = {
        "model": INFERENCE_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    req = urllib.request.Request(
        f"{INFERENCE_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {MODEL_ACCESS_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise InferenceError(f"Inference failed ({exc.code}): {detail}") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    choices = raw.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    usage = raw.get("usage") or {}
    return InferenceResult(
        review_text=(message.get("content") or "").strip(),
        model_id=raw.get("model") or INFERENCE_MODEL,
        latency_ms=latency_ms,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
    )

# --- webhook handler ---


def _header_value(value) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value)


def _header_map(event: dict) -> dict[str, str]:
    merged: dict[str, str] = {}
    http = event.get("http") or {}
    for source in (http.get("headers") or {}, event.get("__ow_headers") or {}):
        for key, value in source.items():
            merged[str(key).lower()] = _header_value(value)
    return merged


def _loads_json_body(raw: str | bytes) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = raw.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        try:
            decoded = base64.b64decode(text).decode("utf-8")
            parsed = json.loads(decoded)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, json.JSONDecodeError):
            return {}


def _parse_payload(event: dict) -> dict:
    """
    Return an isolated copy of the GitLab webhook body.

    With web:true, DO merges JSON into `event`. Never return `event` by reference.
    """
    skip_keys = ("http", "context")

    if event.get("object_kind") or event.get("review_worker"):
        snapshot = {
            k: v
            for k, v in event.items()
            if k not in skip_keys and not str(k).startswith("__ow")
        }
        return json.loads(json.dumps(snapshot))

    for candidate in (event.get("body"), (event.get("http") or {}).get("body")):
        if candidate is None:
            continue
        if isinstance(candidate, dict):
            return json.loads(json.dumps(candidate))
        if isinstance(candidate, str):
            parsed = _loads_json_body(candidate)
            if parsed:
                return parsed
    return {}


@dataclass(frozen=True)
class MrJob:
    """Immutable MR target — captured synchronously before background work."""

    project_id: int
    mr_iid: int
    project_path: str
    mr_title: str
    mr_url: str
    event_action: str
    webhook_event_uuid: str | None
    last_commit_sha: str | None


def _extract_mr_job(payload: dict, webhook_event_uuid: str | None) -> MrJob | None:
    project = payload.get("project") or {}
    attrs = payload.get("object_attributes") or {}
    project_id = project.get("id") or attrs.get("target_project_id")
    mr_iid = attrs.get("iid")
    if not project_id or not mr_iid:
        return None
    last_commit = attrs.get("last_commit") or {}
    commit_sha = last_commit.get("id") if isinstance(last_commit, dict) else None
    if not commit_sha:
        commit_sha = attrs.get("last_commit_id")
    return MrJob(
        project_id=int(project_id),
        mr_iid=int(mr_iid),
        project_path=str(project.get("path_with_namespace") or ""),
        mr_title=str(attrs.get("title") or ""),
        mr_url=str(attrs.get("url") or ""),
        event_action=str(attrs.get("action") or ""),
        webhook_event_uuid=webhook_event_uuid,
        last_commit_sha=str(commit_sha) if commit_sha else None,
    )


def _job_to_dict(job: MrJob) -> dict:
    return {
        "project_id": job.project_id,
        "mr_iid": job.mr_iid,
        "project_path": job.project_path,
        "mr_title": job.mr_title,
        "mr_url": job.mr_url,
        "event_action": job.event_action,
        "webhook_event_uuid": job.webhook_event_uuid,
        "last_commit_sha": job.last_commit_sha,
    }


def _job_from_dict(data: dict) -> MrJob:
    return MrJob(
        project_id=int(data["project_id"]),
        mr_iid=int(data["mr_iid"]),
        project_path=str(data.get("project_path") or ""),
        mr_title=str(data.get("mr_title") or ""),
        mr_url=str(data.get("mr_url") or ""),
        event_action=str(data.get("event_action") or ""),
        webhook_event_uuid=data.get("webhook_event_uuid"),
        last_commit_sha=data.get("last_commit_sha"),
    )


def _is_worker_request(headers: dict[str, str]) -> bool:
    secret = _worker_secret()
    if not secret:
        return False
    return headers.get("x-review-worker", "") == secret


def _parse_worker_job(event: dict) -> MrJob | None:
    payload = _parse_payload(event)
    if not payload.get("review_worker"):
        return None
    job_data = payload.get("job")
    if not isinstance(job_data, dict):
        return None
    return _job_from_dict(job_data)


def _worker_invoke_url() -> str:
    """Non-blocking invoke so GitLab handler returns before review finishes."""
    base = _function_web_url()
    if "blocking=" in base:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}blocking=false"


def _try_worker_post(url: str, body: bytes, secret: str, timeout: float) -> tuple[bool, str]:
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Review-Worker": secret,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            resp.read(1024)
        if code in (200, 202):
            return True, f"worker_dispatched_http_{code}"
        return False, f"worker unexpected HTTP {code}"
    except urllib.error.HTTPError as exc:
        if exc.code in (202, 204):
            return True, "worker_dispatched_async"
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return False, f"worker HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if "timed out" in str(exc).lower():
            return True, "worker_dispatched_fire_and_forget"
        return False, f"worker dispatch failed: {exc}"
    except Exception as exc:
        if "timed out" in str(exc).lower():
            return True, "worker_dispatched_fire_and_forget"
        return False, f"worker dispatch failed: {exc}"


def _dispatch_review_worker(job: MrJob) -> tuple[bool, str]:
    """
    Fire a second HTTP invocation (new activation) to run the review.
    Tries non-blocking URL first, then fire-and-forget with short timeout.
    """
    if not _function_web_url():
        return False, "FUNCTION_WEB_URL not set — add your function web URL as an env var"

    secret = _worker_secret()
    if not secret:
        return False, "REVIEW_WORKER_SECRET or GITLAB_WEBHOOK_SECRET must be set for worker dispatch"

    body = json.dumps({"review_worker": True, "job": _job_to_dict(job)}).encode("utf-8")

    ok, msg = _try_worker_post(_worker_invoke_url(), body, secret, timeout=8)
    if ok:
        return True, msg

    # Some DO endpoints reject blocking=false — retry base URL, timeout = fire-and-forget
    if "blocking=false" in msg or "400" in msg or "404" in msg:
        ok2, msg2 = _try_worker_post(_function_web_url(), body, secret, timeout=3)
        if ok2:
            return True, msg2
        return False, f"{msg}; fallback: {msg2}"

    return False, msg


def _dedup_key(job: MrJob) -> str:
    if job.webhook_event_uuid:
        return f"uuid:{job.webhook_event_uuid}"
    commit = job.last_commit_sha or "none"
    return f"mr:{job.project_id}:{job.mr_iid}:{commit}:{job.event_action}"


def _is_duplicate_delivery(job: MrJob) -> bool:
    key = _dedup_key(job)
    if job.webhook_event_uuid and job.webhook_event_uuid in _PROCESSED_EVENT_UUIDS:
        return True
    if _database_url() and job.webhook_event_uuid:
        try:
            import duckdb

            conn_str = _database_url_to_libpq(_database_url()).replace("'", "''")
            con = duckdb.connect()
            try:
                con.execute("INSTALL postgres; LOAD postgres;")
                con.execute(f"ATTACH '{conn_str}' AS metrics_db (TYPE postgres)")
                row = con.execute(
                    """
                    SELECT 1 FROM metrics_db.public.code_review_runs
                    WHERE webhook_event_uuid = ? AND review_status IN ('success', 'started')
                    LIMIT 1
                    """,
                    [job.webhook_event_uuid],
                ).fetchone()
                if row:
                    return True
            finally:
                con.close()
        except Exception as exc:
            print(f"Dedup DB check failed (continuing): {exc}")
    if key in _PROCESSED_EVENT_UUIDS:
        return True
    return False


def _mark_processed(job: MrJob) -> None:
    key = _dedup_key(job)
    _PROCESSED_EVENT_UUIDS.add(key)
    if job.webhook_event_uuid:
        _PROCESSED_EVENT_UUIDS.add(job.webhook_event_uuid)
    while len(_PROCESSED_EVENT_UUIDS) > _MAX_IN_MEMORY_DEDUP:
        _PROCESSED_EVENT_UUIDS.pop()


def _verify_webhook(headers: dict[str, str]) -> bool:
    secret = _gitlab_webhook_secret()
    if not secret:
        return True
    return headers.get("x-gitlab-token", "") == secret


def _should_review_job(job: MrJob) -> tuple[bool, str]:
    if job.event_action not in REVIEW_ON_ACTIONS:
        return False, f"action_skipped:{job.event_action}"
    if GITLAB_PROJECT_PATH and job.project_path and job.project_path != GITLAB_PROJECT_PATH:
        return False, f"project_not_allowed:{job.project_path}"
    return True, "ok"


def _format_note(review_text: str, model_id: str, latency_ms: int, job: MrJob) -> str:
    return (
        "## Automated code review (DigitalOcean)\n\n"
        f"_MR !{job.mr_iid} · `{job.project_path}` · Model: `{model_id}` · "
        f"Inference: {latency_ms} ms_\n\n"
        f"{review_text}\n\n"
        "---\n"
        "_Posted by DO Functions + Serverless Inference._"
    )


def handle_merge_request(job: MrJob) -> dict:
    print(
        f"Review starting: project_id={job.project_id} mr_iid={job.mr_iid} "
        f"action={job.event_action} commit={job.last_commit_sha} "
        f"uuid={job.webhook_event_uuid}"
    )

    base = ReviewMetrics(
        project_id=job.project_id,
        project_path=job.project_path,
        mr_iid=job.mr_iid,
        mr_title=job.mr_title,
        mr_url=job.mr_url,
        event_action=job.event_action,
        review_status="started",
        webhook_event_uuid=job.webhook_event_uuid,
    )

    started = time.perf_counter()
    try:
        diff = fetch_mr_changes(job.project_id, job.mr_iid)
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n\n... (diff truncated)"

        inference = run_code_review(diff, job.mr_title, job.project_path)
        critical, warning, info = count_findings(inference.review_text)
        note_id = post_mr_note(
            job.project_id,
            job.mr_iid,
            _format_note(inference.review_text, inference.model_id, inference.latency_ms, job),
        )

        base.review_status = "success"
        base.latency_ms = int((time.perf_counter() - started) * 1000)
        base.model_id = inference.model_id
        base.prompt_tokens = inference.prompt_tokens
        base.completion_tokens = inference.completion_tokens
        base.total_tokens = inference.total_tokens
        base.findings_critical = critical
        base.findings_warning = warning
        base.findings_info = info
        base.gitlab_note_id = note_id or None
        record_metrics(base)
        print(f"Review posted: mr_iid={job.mr_iid} note_id={note_id}")
        return {
            "status": "success",
            "mr_iid": job.mr_iid,
            "note_id": note_id,
            "findings": {"critical": critical, "warning": warning, "info": info},
        }
    except (GitLabError, InferenceError) as exc:
        base.review_status = "failed"
        base.latency_ms = int((time.perf_counter() - started) * 1000)
        base.error_message = str(exc)[:2000]
        record_metrics(base)
        raise


def _response(status_code: int, body: dict) -> dict:
    """Always return a valid web-action response for DigitalOcean Functions."""
    return {"statusCode": status_code, "body": body}


def _run_review_pipeline(job: MrJob) -> dict:
    """Full review pipeline — runs inside the worker invocation."""
    if _is_duplicate_delivery(job):
        return {"status": "skipped", "reason": "duplicate", "mr_iid": job.mr_iid}

    ok, reason = _should_review_job(job)
    if not ok:
        record_metrics(
            ReviewMetrics(
                project_id=job.project_id,
                project_path=job.project_path,
                mr_iid=job.mr_iid,
                mr_title=job.mr_title,
                mr_url=job.mr_url,
                event_action=job.event_action,
                review_status="skipped",
                error_message=reason,
                webhook_event_uuid=job.webhook_event_uuid,
            )
        )
        return {"status": "skipped", "reason": reason, "mr_iid": job.mr_iid}

    _mark_processed(job)
    return handle_merge_request(job)


def _process_webhook_background(job: MrJob) -> None:
    """Deprecated fallback — prefer worker dispatch."""
    try:
        result = _run_review_pipeline(job)
        print(f"Review pipeline: {json.dumps(result, default=str)}")
    except (GitLabError, InferenceError) as exc:
        print(traceback.format_exc())
        print(f"Review failed mr_iid={job.mr_iid}: {exc}")
    except Exception:
        print(traceback.format_exc())


def main(event, context=None):
    """
    GitLab webhooks must return in ~10s. We validate, dispatch a worker HTTP call
    (separate function activation), then return 200 immediately.
    """
    try:
        event = event or {}
        headers = _header_map(event)

        # --- Worker invocation: runs the full review in its own activation ---
        if _is_worker_request(headers):
            worker_job = _parse_worker_job(event)
            if not worker_job:
                return _response(
                    400,
                    {
                        "error": "invalid_worker_payload",
                        "hint": "Worker body must be JSON: {review_worker: true, job: {...}}",
                        "event_keys": sorted(event.keys())[:40],
                    },
                )
            try:
                result = _run_review_pipeline(worker_job)
                return _response(200, result)
            except (GitLabError, InferenceError) as exc:
                print(traceback.format_exc())
                return _response(200, {"status": "error", "message": str(exc), "mr_iid": worker_job.mr_iid})

        # --- GitLab webhook ---
        if not _verify_webhook(headers):
            return _response(
                401,
                {
                    "error": "invalid_webhook_token",
                    "hint": "GITLAB_WEBHOOK_SECRET must match GitLab webhook Secret token",
                },
            )

        payload = _parse_payload(event)
        if payload.get("object_kind") != "merge_request":
            return _response(
                200,
                {
                    "status": "ignored",
                    "reason": "unrecognized_payload",
                    "hint": "Disable Raw HTTP in function Settings; use Web Function with JSON body",
                    "event_keys": sorted(event.keys())[:30],
                },
            )

        webhook_event_uuid = headers.get("x-gitlab-event-uuid")
        job = _extract_mr_job(payload, webhook_event_uuid)
        if not job:
            return _response(200, {"status": "invalid_payload", "error": "missing project_id or mr iid"})

        if (payload.get("object_attributes") or {}).get("draft"):
            return _response(200, {"status": "skipped", "reason": "draft_merge_request", "mr_iid": job.mr_iid})

        ok, reason = _should_review_job(job)
        if not ok:
            record_metrics(
                ReviewMetrics(
                    project_id=job.project_id,
                    project_path=job.project_path,
                    mr_iid=job.mr_iid,
                    mr_title=job.mr_title,
                    mr_url=job.mr_url,
                    event_action=job.event_action,
                    review_status="skipped",
                    error_message=reason,
                    webhook_event_uuid=job.webhook_event_uuid,
                )
            )
            return _response(
                200,
                {
                    "status": "skipped",
                    "reason": reason,
                    "mr_iid": job.mr_iid,
                    "action": job.event_action,
                    "hint": "Reviews run on open/update/reopen only — not close or merge",
                },
            )

        if _is_duplicate_delivery(job):
            return _response(
                200,
                {"status": "skipped", "reason": "duplicate", "mr_iid": job.mr_iid},
            )

        dispatched, dispatch_detail = _dispatch_review_worker(job)
        if not dispatched:
            return _response(
                200,
                {
                    "status": "error",
                    "mr_iid": job.mr_iid,
                    "message": dispatch_detail,
                    "hint": "Set FUNCTION_WEB_URL to this function's web URL (same as GitLab webhook URL)",
                },
            )

        return _response(
            200,
            {
                "status": "accepted",
                "mr_iid": job.mr_iid,
                "action": job.event_action,
                "dispatch": dispatch_detail,
                "message": f"Code review dispatched for MR !{job.mr_iid}.",
            },
        )
    except Exception as exc:
        print(traceback.format_exc())
        return _response(200, {"status": "error", "message": str(exc)})
