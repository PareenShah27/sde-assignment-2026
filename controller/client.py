"""HTTP client for the simulator. The ONLY way the controller learns about the world.

Retries transport errors and 5xx with backoff. 4xx are returned to the caller as SimError so that the
governance layer can decide what a 409/410/403/402 means. Nothing here retries a non-idempotent POST blindly:
callers that mutate (approvals, apply, auto-apply) use their own idempotency keys and reconcile on error.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

log = logging.getLogger("controller.client")


class SimError(Exception):
    def __init__(self, status: int, code: str, detail):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class TransportError(Exception):
    """The request may or may not have reached the simulator. Callers must reconcile."""


class SimClient:
    def __init__(self, base_url: str, timeout: float = 300.0, retries: int = 6, transport=None):
        self.http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self.retries = retries
        self.calls = 0

    def _req(self, method: str, path: str, *, json_body=None, params=None, raw: bool = False,
             retry_mutation: bool = True):
        delay = 0.5
        last: Exception | None = None
        for attempt in range(self.retries):
            self.calls += 1
            try:
                r = self.http.request(method, path, json=json_body, params=params)
            except httpx.TransportError as e:  # connection refused/reset, timeout
                last = e
                if method != "GET" and not retry_mutation:
                    raise TransportError(str(e)) from e
                log.warning("transport error on %s %s (attempt %d): %s", method, path, attempt + 1, e)
                time.sleep(delay)
                delay = min(delay * 2, 10)
                continue
            if r.status_code >= 500:
                last = SimError(r.status_code, "server_error", r.text[:200])
                if method != "GET" and not retry_mutation:
                    raise TransportError(f"{r.status_code} on {method} {path}")
                log.warning("%s on %s %s (attempt %d)", r.status_code, method, path, attempt + 1)
                time.sleep(delay)
                delay = min(delay * 2, 10)
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    body = {"error": "http_error", "detail": r.text[:200]}
                code = body.get("error") or body.get("detail") or "http_error"
                if not isinstance(code, str):
                    code = "http_error"
                raise SimError(r.status_code, code, body.get("detail", body))
            return r.text if raw else r.json()
        if isinstance(last, httpx.TransportError) or method != "GET":
            raise TransportError(f"giving up on {method} {path}: {last}")
        raise last  # type: ignore[misc]

    # ---- reads
    def state(self) -> dict:
        return self._req("GET", "/sim/state")

    def workflows(self) -> list[dict]:
        return self._req("GET", "/workflows")

    def workflow(self, wid: str) -> dict:
        return self._req("GET", f"/workflows/{wid}")

    def config(self, wid: str) -> dict:
        return self._req("GET", f"/workflows/{wid}/config")

    def config_history(self, wid: str) -> list[dict]:
        return self._req("GET", f"/workflows/{wid}/config/history")

    def chunks(self, kind: str, after: int = -1) -> list[dict]:
        return self._req("GET", "/events/chunks", params={"kind": kind, "after": after})

    def chunk_lines(self, n: int) -> list[str]:
        return self._req("GET", f"/events/chunks/{n}", raw=True).splitlines()

    def approval(self, aid: str) -> dict:
        return self._req("GET", f"/approvals/{aid}")

    def approvals(self, workflow_id: str | None = None, status: str | None = None) -> list[dict]:
        params = {k: v for k, v in (("workflow_id", workflow_id), ("status", status)) if v}
        return self._req("GET", "/approvals", params=params)

    # ---- time
    def advance(self, minutes: int) -> dict:
        # advance is NOT idempotent: a retry after a lost response would skip time. We never retry it;
        # the loop re-reads /sim/state and the chunk list instead.
        return self._req("POST", "/sim/advance", json_body={"minutes": minutes}, retry_mutation=False)

    # ---- mutations (idempotent by proposal_id; never auto-retried — caller reconciles)
    def request_approval(self, body: dict) -> dict:
        return self._req("POST", "/approvals", json_body=body, retry_mutation=False)

    def apply(self, wid: str, approval_id: str) -> dict:
        return self._req("POST", f"/workflows/{wid}/apply", json_body={"approval_id": approval_id},
                         retry_mutation=False)

    def auto_apply(self, wid: str, body: dict) -> dict:
        return self._req("PUT", f"/workflows/{wid}/config", json_body=body, retry_mutation=False)

    def suggest_fixes(self, wid: str, problem: str) -> dict:
        return self._req("POST", "/llm/suggest-fixes", json_body={"workflow_id": wid, "problem": problem},
                         retry_mutation=False)

    def validate(self, wid: str, prompt_version: str, n: int) -> dict:
        return self._req("POST", "/validation/runs",
                         json_body={"workflow_id": wid, "prompt_version": prompt_version, "n": n},
                         retry_mutation=False)


def dumps(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=str)
