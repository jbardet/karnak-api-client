"""Authenticated transport client for the Karnak REST API (v2.0.0).

The client intentionally mirrors the REST resources from the v2.0.0 OpenAPI
spec and keeps policy decisions out of the transport layer. Higher-level code
decides what "apply" means (see ``karnak_api_client.apply``); this module
only performs authenticated HTTP calls and returns JSON-compatible
dictionaries/lists.

Resource keys are UUID strings in v2.0.0 (``{projectUuid}``, ``{profileUuid}``,
``{forwardNodeUuid}``, ``{destinationUuid}``, ``{sourceNodeUuid}``,
``{secretUuid}``) — every method that used to take an integer ``*_id`` now
takes a ``*_uuid: str``.

Authentication
--------------
v2.0.0 has a dedicated stateless ``/api/**`` filter chain (CSRF disabled, no
session): HTTP Basic under ``IDP=inmemory``, Bearer JWT under ``IDP=oidc``.
Depending on how the Karnak image is configured this may still only be
reachable through Spring form-login (POST ``/login`` once, then carry the
``JSESSIONID`` cookie) — ``auth_mode`` selects the behaviour:

- ``"basic"`` — HTTP Basic on every request, no fallback.
- ``"form"``  — form-login before the first request, cookie afterwards.
- ``"auto"``  (default) — try Basic; when the server answers 401 or redirects
  to ``/login``, switch to form-login transparently and retry once.

Form login is a fallback, never a requirement: the default ``auto`` mode
never performs it unless Basic is rejected first.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

_AUTH_MODES = ("auto", "basic", "form")


class KarnakError(RuntimeError):
    """Base class for all Karnak client errors (incl. network failures)."""


class AuthenticationError(KarnakError):
    """Raised when login fails or the session is rejected."""


class KarnakApiError(KarnakError):
    """Raised when Karnak returns a non-success HTTP response."""

    def __init__(self, method: str, url: str, status_code: int, body: str) -> None:
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        super().__init__(f"Karnak {method} {url} failed with HTTP {status_code}: {body}")


# Backwards-compatible alias kept for callers written against the original
# form-login client (TMLCTP harness, setup_deid_gateway.py).
ApiError = KarnakApiError


@dataclass(frozen=True)
class KarnakClientConfig:
    base_url: str
    username: str
    password: str
    timeout_sec: float = 30.0
    auth_mode: str = "auto"

    def __post_init__(self) -> None:
        if self.auth_mode not in _AUTH_MODES:
            raise ValueError(f"auth_mode must be one of {_AUTH_MODES}, got {self.auth_mode!r}")

    @classmethod
    def from_env(cls) -> "KarnakClientConfig":
        return cls(
            base_url=os.getenv("KARNAK_API_BASE", "http://localhost:8081").rstrip("/"),
            username=os.getenv("KARNAK_USERNAME", os.getenv("KARNAK_LOGIN_ADMIN", "admin")),
            password=os.getenv("KARNAK_PASSWORD", "karnak"),
            timeout_sec=float(os.getenv("KARNAK_API_TIMEOUT_SEC", "30")),
            auth_mode=os.getenv("KARNAK_AUTH_MODE", "auto").strip().lower(),
        )


def _redirects_to_login(resp: requests.Response) -> bool:
    if resp.status_code in (302, 303):
        return "login" in resp.headers.get("Location", "")
    return False


class KarnakClient:
    """Thin wrapper around the Karnak REST API.

    Construction styles (equivalent)::

        KarnakClient()                                  # from environment
        KarnakClient(KarnakClientConfig(...))           # explicit config
        KarnakClient(base_url=..., username=..., password=...)  # kwargs
    """

    def __init__(
        self,
        config: KarnakClientConfig | None = None,
        *,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout_sec: float | None = None,
        auth_mode: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        cfg = config or KarnakClientConfig.from_env()
        overrides = {
            key: value
            for key, value in {
                "base_url": base_url.rstrip("/") if base_url else None,
                "username": username,
                "password": password,
                "timeout_sec": timeout_sec,
                "auth_mode": auth_mode,
            }.items()
            if value is not None
        }
        if overrides:
            cfg = replace(cfg, **overrides)
        self.config = cfg
        self.session = session if session is not None else requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._basic = HTTPBasicAuth(cfg.username, cfg.password)
        self._use_form = cfg.auth_mode == "form"
        self._form_logged_in = False

    # ── authentication ──────────────────────────────────────────────────

    @property
    def base(self) -> str:
        """Base URL (kept as an attribute-style accessor for older callers)."""
        return self.config.base_url

    def login(self) -> None:
        """POST form credentials to /login and establish a session cookie."""
        try:
            resp = self.session.post(
                f"{self.config.base_url}/login",
                data={"username": self.config.username, "password": self.config.password},
                allow_redirects=False,
                timeout=self.config.timeout_sec,
            )
        except requests.RequestException as exc:
            raise KarnakError(f"Cannot reach {self.config.base_url}: {exc}") from exc
        if resp.status_code not in (302, 303):
            raise AuthenticationError(f"Unexpected login response: HTTP {resp.status_code}")
        location = resp.headers.get("Location", "")
        if "error" in location or location.rstrip("/").endswith("/login"):
            raise AuthenticationError(
                f"Login failed — invalid credentials for {self.config.username!r}"
            )
        self._form_logged_in = True

    def invalidate_session(self) -> None:
        """Drop the session cookie (e.g. after a Karnak container restart)."""
        self.session.cookies.clear()
        self._form_logged_in = False

    def check_connectivity(self, timeout: float = 5.0) -> None:
        """Verify Karnak is reachable and credentials are accepted."""
        self._request("GET", "/api/forward-nodes", expected={200, 204}, timeout_sec=timeout)

    # ── transport ───────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.config.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        expected: set[int] | None = None,
        timeout_sec: float | None = None,
        _form_retry: bool = False,
    ) -> Any:
        expected = expected or {200}
        url = self._url(path)
        if self._use_form and not self._form_logged_in:
            self.login()
        try:
            resp = self.session.request(
                method,
                url,
                json=json_body,
                files=files,
                params=params,
                auth=None if self._use_form else self._basic,
                allow_redirects=False,
                timeout=timeout_sec or self.config.timeout_sec,
            )
        except requests.RequestException as exc:
            raise KarnakError(f"Karnak {method} {url} failed: {exc}") from exc

        unauthenticated = resp.status_code == 401 or _redirects_to_login(resp)
        if unauthenticated and not self._use_form and not _form_retry:
            if self.config.auth_mode == "basic":
                raise KarnakApiError(method, url, resp.status_code, resp.text)
            # auto mode: this Karnak build does not accept Basic on the API —
            # switch to form-login for the rest of the client's lifetime.
            self._use_form = True
            self.login()
            return self._request(
                method,
                path,
                json_body=json_body,
                files=files,
                params=params,
                expected=expected,
                timeout_sec=timeout_sec,
                _form_retry=True,
            )
        if unauthenticated and self._use_form and not _form_retry:
            # Session cookie expired (e.g. Karnak restarted): log in again once.
            self.invalidate_session()
            self.login()
            return self._request(
                method,
                path,
                json_body=json_body,
                files=files,
                params=params,
                expected=expected,
                timeout_sec=timeout_sec,
                _form_retry=True,
            )

        if resp.status_code not in expected:
            raise KarnakApiError(method, url, resp.status_code, resp.text)
        if resp.status_code == 204 or not resp.content:
            return None
        content_type = resp.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            return resp.text
        return resp.json()

    # ── health / echo ───────────────────────────────────────────────────

    def echo_destinations(self, src_aet: str) -> Any:
        # This endpoint is intentionally unauthenticated server-side, but using
        # session auth is harmless and keeps the client simple.
        return self._request(
            "GET",
            "/api/echo/destinations",
            params={"srcAet": src_aet},
            expected={200, 204},
        )

    # ── profiles ────────────────────────────────────────────────────────

    def list_profiles(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/profiles", expected={200, 204}) or []

    def upload_profile(self, path: Path | str) -> dict[str, Any]:
        """POST /api/profiles/import — multipart YAML upload (unchanged shape

        from the pre-v2.0.0 API; only the path moved from ``/api/profiles``).
        """
        path = Path(path)
        # Read up-front so the auto-mode form-login retry can resend the body.
        payload = path.read_bytes()
        return self._request(
            "POST",
            "/api/profiles/import",
            files={"file": (path.name, payload, "application/x-yaml")},
            expected={201},
            timeout_sec=max(self.config.timeout_sec, 60.0),
        )

    def create_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/profiles — JSON create (ProfileInput: name, version, ...)."""
        return self._request("POST", "/api/profiles", json_body=body, expected={201})

    def get_profile(self, profile_uuid: str) -> dict[str, Any]:
        return self._request("GET", f"/api/profiles/{profile_uuid}", expected={200})

    def update_profile(self, profile_uuid: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PUT", f"/api/profiles/{profile_uuid}", json_body=body, expected={200}
        )

    def delete_profile(self, profile_uuid: str) -> None:
        self._request("DELETE", f"/api/profiles/{profile_uuid}", expected={204})

    # ── projects ─────────────────────────────────────────────────────────

    def list_projects(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/projects", expected={200, 204}) or []

    def create_project(self, name: str, profile_uuid: str) -> dict[str, Any]:
        """POST /api/projects. ``profileUuid`` is required (``@NotNull``) —

        v2.0.0 makes the project→profile link a first-class, settable field.
        """
        body = {"name": name, "profileUuid": profile_uuid}
        return self._request("POST", "/api/projects", json_body=body, expected={201})

    def get_project(self, project_uuid: str) -> dict[str, Any]:
        return self._request("GET", f"/api/projects/{project_uuid}", expected={200})

    def update_project(self, project_uuid: str, *, name: str, profile_uuid: str) -> dict[str, Any]:
        """PUT /api/projects/{uuid}.

        Always sends the full model. v2.0.0 validates ``name`` (``@NotBlank``)
        and ``profileUuid`` (``@NotNull``) on ProjectInput, so a partial/empty
        PUT now returns 400 — there is no "detach profile" or "rename only"
        shortcut any more. Callers that only want to touch the project
        unchanged (to force Karnak to republish its destination config)
        should use :meth:`touch_project` instead of reconstructing this call.
        """
        body = {"name": name, "profileUuid": profile_uuid}
        return self._request(
            "PUT", f"/api/projects/{project_uuid}", json_body=body, expected={200}
        )

    def touch_project(self, project_uuid: str) -> dict[str, Any]:
        """GET-then-PUT a project back unchanged.

        REST writes to a project's destinations don't notify anything —
        re-PUTting the project itself is what makes Karnak republish its
        destination config to the pool (see MEMORY.md "Karnak config
        propagation").
        """
        project = self.get_project(project_uuid)
        return self.update_project(
            project_uuid, name=project["name"], profile_uuid=project["profileUuid"]
        )

    def delete_project(self, project_uuid: str) -> None:
        self._request("DELETE", f"/api/projects/{project_uuid}", expected={204})

    # ── project secrets ─────────────────────────────────────────────────

    def list_secrets(self, project_uuid: str) -> list[dict[str, Any]]:
        return (
            self._request("GET", f"/api/projects/{project_uuid}/secrets", expected={200, 204})
            or []
        )

    def add_project_secret(self, project_uuid: str, hex_key: str | None = None) -> dict[str, Any]:
        """Create a new active secret for a project.

        ``hex_key`` given → ``POST .../secrets/import`` with body
        ``{"key": hex_key}`` (32 hex chars, dashed groups allowed); otherwise
        ``POST .../secrets/generate`` (random key). Both return 201 and
        activate the new key, deactivating any previous one.
        """
        if hex_key:
            return self._request(
                "POST",
                f"/api/projects/{project_uuid}/secrets/import",
                json_body={"key": hex_key},
                expected={201},
            )
        return self._request(
            "POST",
            f"/api/projects/{project_uuid}/secrets/generate",
            expected={201},
        )

    # Alias kept for callers written against the original form-login client.
    generate_secret = add_project_secret

    def delete_secret(self, project_uuid: str, secret_uuid: str) -> None:
        self._request(
            "DELETE",
            f"/api/projects/{project_uuid}/secrets/{secret_uuid}",
            expected={204},
        )

    # ── pseudonyms ───────────────────────────────────────────────────────
    # Replaces the old /api/projects/{id}/external-ids (+ /import) resource,
    # which is not project-scoped any more: GET lists ALL pseudonyms.

    def list_pseudonyms(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/pseudonyms", expected={200, 204}) or []

    def _create_pseudonym_with_status(self, row: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """POST one pseudonym. Returns ``(created, body)``.

        ``created=False`` means the server answered 409 (a pseudonym already
        exists for this patient + project) and ``body`` is the existing
        PatientModel from the conflict response.
        """
        try:
            body = self._request("POST", "/api/pseudonyms", json_body=row, expected={201})
            return True, body
        except KarnakApiError as exc:
            if exc.status_code != 409:
                raise
            try:
                body = json.loads(exc.body) if exc.body else {}
            except json.JSONDecodeError:
                body = {}
            return False, body

    def create_pseudonym(self, row: dict[str, Any]) -> dict[str, Any]:
        """POST /api/pseudonyms. Idempotent: HTTP 409 (already exists) is

        treated as success and returns the existing PatientModel.
        """
        _created, body = self._create_pseudonym_with_status(row)
        return body

    def import_pseudonyms(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """POST /api/pseudonyms/import (fork-only bulk import).

        Falls back to one :meth:`create_pseudonym` call per row — and
        synthesises the same ``{created, conflict, error, rows}`` summary
        shape — when talking to an older Karnak that doesn't have the fork's
        bulk-import patch (404).
        """
        try:
            return self._request(
                "POST", "/api/pseudonyms/import", json_body=rows, expected={200}
            )
        except KarnakApiError as exc:
            if exc.status_code != 404:
                raise

        created = conflict = error = 0
        row_results: list[dict[str, Any]] = []
        for row in rows:
            try:
                was_created, body = self._create_pseudonym_with_status(row)
            except KarnakApiError as row_exc:
                error += 1
                row_results.append({"error": str(row_exc), "row": row})
                continue
            if was_created:
                created += 1
            else:
                conflict += 1
            row_results.append(body)
        return {"created": created, "conflict": conflict, "error": error, "rows": row_results}

    def delete_project_pseudonyms(self, project_uuid: str) -> None:
        self._request("DELETE", f"/api/pseudonyms/project/{project_uuid}", expected={204})

    # ── forward nodes / sources / destinations ──────────────────────────

    def list_forward_nodes(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/forward-nodes", expected={200, 204}) or []

    def get_forward_node(self, forward_node_uuid: str) -> dict[str, Any]:
        return self._request("GET", f"/api/forward-nodes/{forward_node_uuid}", expected={200})

    def create_forward_node(self, ae_title: str, description: str = "") -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/forward-nodes",
            json_body={"fwdAeTitle": ae_title, "fwdDescription": description},
            expected={201},
        )

    def update_forward_node(
        self,
        forward_node_uuid: str,
        ae_title: str,
        description: str = "",
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/api/forward-nodes/{forward_node_uuid}",
            json_body={"fwdAeTitle": ae_title, "fwdDescription": description},
            expected={200},
        )

    def delete_forward_node(self, forward_node_uuid: str) -> None:
        self._request("DELETE", f"/api/forward-nodes/{forward_node_uuid}", expected={204})

    def list_source_nodes(self, forward_node_uuid: str) -> list[dict[str, Any]]:
        return (
            self._request(
                "GET",
                f"/api/forward-nodes/{forward_node_uuid}/source-nodes",
                expected={200, 204},
            )
            or []
        )

    def add_source_node(self, forward_node_uuid: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/forward-nodes/{forward_node_uuid}/source-nodes",
            json_body=body,
            expected={201},
        )

    def delete_source_node(self, forward_node_uuid: str, source_node_uuid: str) -> None:
        self._request(
            "DELETE",
            f"/api/forward-nodes/{forward_node_uuid}/source-nodes/{source_node_uuid}",
            expected={204},
        )

    def list_destinations(self, forward_node_uuid: str) -> list[dict[str, Any]]:
        return (
            self._request(
                "GET",
                f"/api/forward-nodes/{forward_node_uuid}/destinations",
                expected={200, 204},
            )
            or []
        )

    def add_destination(self, forward_node_uuid: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/forward-nodes/{forward_node_uuid}/destinations",
            json_body=body,
            expected={201},
        )

    # Alias kept for callers written against the original form-login client.
    create_destination = add_destination

    def update_destination(
        self,
        forward_node_uuid: str,
        destination_uuid: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/api/forward-nodes/{forward_node_uuid}/destinations/{destination_uuid}",
            json_body=body,
            expected={200},
        )

    def delete_destination(self, forward_node_uuid: str, destination_uuid: str) -> None:
        self._request(
            "DELETE",
            f"/api/forward-nodes/{forward_node_uuid}/destinations/{destination_uuid}",
            expected={204},
        )

    # ── auth configs ─────────────────────────────────────────────────────
    # Fork-only (upstream v2.0.0 removed this resource): keyed on the string
    # `code`, not a uuid. Not in the v2.0.0 OpenAPI spec.

    def list_auth_configs(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/auth-configs", expected={200, 204}) or []

    def create_auth_config(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/auth-configs", json_body=body, expected={201})

    def update_auth_config(self, code: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/api/auth-configs/{code}", json_body=body, expected={200})

    def delete_auth_config(self, code: str) -> None:
        self._request("DELETE", f"/api/auth-configs/{code}", expected={204})

    # ── monitoring ──────────────────────────────────────────────────────
    # Criteria keys are the v2.0.0 names (statusType, destinationUuid,
    # studyUid, serieUid, sopInstanceUid, start, end) — callers must pass
    # those; the old `status` / `destinationId` names are not translated.

    def query_transfers(self, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """GET /api/monitoring/transfers. Accepts page/size/sort in ``params``.

        Returns the Spring Data ``Page`` object as-is:
        ``{"content": [...], "totalElements": N, "totalPages": N, "number": N,
        "size": N}`` (or ``None`` on 204 — no transfers matched).
        """
        return self._request(
            "GET", "/api/monitoring/transfers", params=params or {}, expected={200, 204}
        )

    def count_transfers(self, params: dict[str, Any] | None = None) -> int:
        """GET /api/monitoring/transfers/count. Returns a bare int, not a dict."""
        result = self._request(
            "GET", "/api/monitoring/transfers/count", params=params or {}, expected={200}
        )
        return int(result)
