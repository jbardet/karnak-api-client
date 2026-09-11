"""Unit tests for the unified Karnak v2.0.0 client (auth modes + transport)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from karnak_api_client import (
    AuthenticationError,
    KarnakApiError,
    KarnakClient,
    KarnakClientConfig,
    KarnakError,
)

BASE = "http://karnak.test:8081"

PROFILE_UUID = "11111111-1111-1111-1111-111111111111"
PROJECT_UUID = "22222222-2222-2222-2222-222222222222"


def make_config(auth_mode: str = "auto") -> KarnakClientConfig:
    return KarnakClientConfig(
        base_url=BASE, username="admin", password="karnak", auth_mode=auth_mode
    )


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        if body is None:
            self.content = b""
            self.text = ""
        elif isinstance(body, str):
            self.content = body.encode()
            self.text = body
            self.headers.setdefault("Content-Type", "text/html")
        else:
            self.text = json.dumps(body)
            self.content = self.text.encode()
            self.headers.setdefault("Content-Type", "application/json")
        self._body = body

    def json(self) -> Any:
        return self._body


class FakeCookies:
    def __init__(self) -> None:
        self.cleared = 0

    def clear(self) -> None:
        self.cleared += 1


class FakeSession:
    """Queue-driven stand-in for requests.Session."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookies()

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"Unexpected request: {method} {url}")
        return self.responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, **kwargs)


def make_client(responses: list[FakeResponse], auth_mode: str = "auto") -> tuple[KarnakClient, FakeSession]:
    session = FakeSession(responses)
    client = KarnakClient(make_config(auth_mode), session=session)  # type: ignore[arg-type]
    return client, session


def login_ok() -> FakeResponse:
    return FakeResponse(302, headers={"Location": f"{BASE}/"})


def login_redirect() -> FakeResponse:
    return FakeResponse(302, headers={"Location": f"{BASE}/login"})


# ── basic mode ──────────────────────────────────────────────────────────────


def test_basic_mode_sends_basic_auth_and_parses_json() -> None:
    client, session = make_client([FakeResponse(200, [{"uuid": PROFILE_UUID}])], auth_mode="basic")
    assert client.list_profiles() == [{"uuid": PROFILE_UUID}]
    call = session.calls[0]
    assert call["auth"] is not None
    assert call["allow_redirects"] is False


def test_basic_mode_does_not_fall_back_on_401() -> None:
    client, session = make_client([FakeResponse(401, "nope")], auth_mode="basic")
    with pytest.raises(KarnakApiError) as exc:
        client.list_profiles()
    assert exc.value.status_code == 401
    assert len(session.calls) == 1


# ── auto mode fallback ──────────────────────────────────────────────────────


@pytest.mark.parametrize("first", [login_redirect(), FakeResponse(401, "nope")])
def test_auto_mode_falls_back_to_form_login(first: FakeResponse) -> None:
    client, session = make_client([first, login_ok(), FakeResponse(200, [{"uuid": PROFILE_UUID}])])
    assert client.list_profiles() == [{"uuid": PROFILE_UUID}]
    methods = [(c["method"], c["url"]) for c in session.calls]
    assert methods == [
        ("GET", f"{BASE}/api/profiles"),
        ("POST", f"{BASE}/login"),
        ("GET", f"{BASE}/api/profiles"),
    ]
    # After fallback the client stays in form mode: no Basic auth on retry.
    assert session.calls[2]["auth"] is None
    # Subsequent calls skip Basic entirely without a new login.
    session.responses = [FakeResponse(200, [])]
    client.list_projects()
    assert session.calls[3]["auth"] is None


def test_auto_mode_raises_authentication_error_on_bad_credentials() -> None:
    client, _ = make_client(
        [login_redirect(), FakeResponse(302, headers={"Location": f"{BASE}/login?error"})]
    )
    with pytest.raises(AuthenticationError):
        client.list_profiles()


# ── form mode ───────────────────────────────────────────────────────────────


def test_form_mode_logs_in_before_first_request() -> None:
    client, session = make_client([login_ok(), FakeResponse(200, [])], auth_mode="form")
    client.list_profiles()
    methods = [(c["method"], c["url"]) for c in session.calls]
    assert methods[0] == ("POST", f"{BASE}/login")
    assert session.calls[1]["auth"] is None


def test_form_mode_relogins_when_session_expires() -> None:
    client, session = make_client(
        [login_ok(), login_redirect(), login_ok(), FakeResponse(200, [{"uuid": PROFILE_UUID}])],
        auth_mode="form",
    )
    assert client.list_profiles() == [{"uuid": PROFILE_UUID}]
    assert session.cookies.cleared == 1
    methods = [(c["method"], c["url"]) for c in session.calls]
    assert methods == [
        ("POST", f"{BASE}/login"),
        ("GET", f"{BASE}/api/profiles"),
        ("POST", f"{BASE}/login"),
        ("GET", f"{BASE}/api/profiles"),
    ]


def test_invalidate_session_clears_cookies_and_forces_relogin() -> None:
    client, session = make_client(
        [login_ok(), FakeResponse(200, []), login_ok(), FakeResponse(200, [])],
        auth_mode="form",
    )
    client.list_profiles()
    client.invalidate_session()
    client.list_profiles()
    posts = [c for c in session.calls if c["method"] == "POST"]
    assert len(posts) == 2


# ── transport behaviour ─────────────────────────────────────────────────────


def test_204_responses_map_to_empty_list() -> None:
    client, _ = make_client([FakeResponse(204)], auth_mode="basic")
    assert client.list_forward_nodes() == []


def test_unexpected_status_raises_api_error_with_details() -> None:
    client, _ = make_client([FakeResponse(500, "boom")], auth_mode="basic")
    with pytest.raises(KarnakApiError) as exc:
        client.get_project(PROJECT_UUID)
    assert exc.value.status_code == 500
    assert exc.value.method == "GET"
    assert "boom" in exc.value.body


def test_connection_error_wrapped_as_karnak_error() -> None:
    class ExplodingSession(FakeSession):
        def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            raise requests.ConnectionError("refused")

    session = ExplodingSession([])
    client = KarnakClient(make_config("basic"), session=session)  # type: ignore[arg-type]
    with pytest.raises(KarnakError):
        client.check_connectivity()


def test_upload_profile_posts_to_import_and_survives_form_fallback(tmp_path: Path) -> None:
    profile = tmp_path / "p.yml"
    profile.write_bytes(b"name: X\n")
    client, session = make_client(
        [login_redirect(), login_ok(), FakeResponse(201, {"uuid": PROFILE_UUID, "name": "X"})]
    )
    created = client.upload_profile(str(profile))
    assert created["uuid"] == PROFILE_UUID
    # Both the first attempt and the retry carried the file payload, against
    # the v2.0.0 /import path (bare POST /api/profiles is now a JSON create).
    uploads = [c for c in session.calls if c["url"].endswith("/api/profiles/import")]
    assert len(uploads) == 2
    for call in uploads:
        name, payload, content_type = call["files"]["file"]
        assert name == "p.yml"
        assert payload == b"name: X\n"
        assert content_type == "application/x-yaml"


def test_create_profile_posts_json_to_bare_profiles_path() -> None:
    client, session = make_client([FakeResponse(201, {"uuid": PROFILE_UUID, "name": "X"})], auth_mode="basic")
    body = {"name": "X", "version": "1.0"}
    created = client.create_profile(body)
    assert created["uuid"] == PROFILE_UUID
    call = session.calls[0]
    assert call["url"] == f"{BASE}/api/profiles"
    assert call["json"] == body


# ── projects ─────────────────────────────────────────────────────────────────


def test_create_project_sends_profile_uuid() -> None:
    client, session = make_client(
        [FakeResponse(201, {"uuid": PROJECT_UUID, "name": "P", "profileUuid": PROFILE_UUID})],
        auth_mode="basic",
    )
    project = client.create_project("P", PROFILE_UUID)
    assert project["uuid"] == PROJECT_UUID
    assert session.calls[0]["json"] == {"name": "P", "profileUuid": PROFILE_UUID}


def test_update_project_always_sends_full_body() -> None:
    client, session = make_client(
        [FakeResponse(200, {"uuid": PROJECT_UUID, "name": "P2", "profileUuid": PROFILE_UUID})],
        auth_mode="basic",
    )
    client.update_project(PROJECT_UUID, name="P2", profile_uuid=PROFILE_UUID)
    assert session.calls[0]["json"] == {"name": "P2", "profileUuid": PROFILE_UUID}


def test_touch_project_gets_then_puts_unchanged() -> None:
    client, session = make_client(
        [
            FakeResponse(200, {"uuid": PROJECT_UUID, "name": "P", "profileUuid": PROFILE_UUID}),
            FakeResponse(200, {"uuid": PROJECT_UUID, "name": "P", "profileUuid": PROFILE_UUID}),
        ],
        auth_mode="basic",
    )
    result = client.touch_project(PROJECT_UUID)
    assert result["uuid"] == PROJECT_UUID
    methods = [(c["method"], c["url"]) for c in session.calls]
    assert methods == [
        ("GET", f"{BASE}/api/projects/{PROJECT_UUID}"),
        ("PUT", f"{BASE}/api/projects/{PROJECT_UUID}"),
    ]
    assert session.calls[1]["json"] == {"name": "P", "profileUuid": PROFILE_UUID}


# ── secrets ──────────────────────────────────────────────────────────────────


def test_add_project_secret_without_hex_key_calls_generate() -> None:
    client, session = make_client([FakeResponse(201, {"uuid": "s1", "key": "abc", "active": True})], auth_mode="basic")
    client.add_project_secret(PROJECT_UUID)
    call = session.calls[0]
    assert call["url"] == f"{BASE}/api/projects/{PROJECT_UUID}/secrets/generate"
    assert call["method"] == "POST"


def test_add_project_secret_with_hex_key_calls_import() -> None:
    client, session = make_client([FakeResponse(201, {"uuid": "s1", "key": "deadbeef", "active": True})], auth_mode="basic")
    client.add_project_secret(PROJECT_UUID, hex_key="deadbeef")
    call = session.calls[0]
    assert call["url"] == f"{BASE}/api/projects/{PROJECT_UUID}/secrets/import"
    assert call["json"] == {"key": "deadbeef"}


def test_list_secrets_returns_empty_list_on_204() -> None:
    client, _ = make_client([FakeResponse(204)], auth_mode="basic")
    assert client.list_secrets(PROJECT_UUID) == []


def test_delete_secret_expects_204() -> None:
    client, session = make_client([FakeResponse(204)], auth_mode="basic")
    client.delete_secret(PROJECT_UUID, "s1")
    assert session.calls[0]["url"] == f"{BASE}/api/projects/{PROJECT_UUID}/secrets/s1"
    assert session.calls[0]["method"] == "DELETE"


# ── pseudonyms ───────────────────────────────────────────────────────────────


def test_create_pseudonym_treats_409_as_success() -> None:
    """Regression: 409 (already exists) must be swallowed, not raised."""
    existing = {"uuid": "p1", "pseudonym": "PSY1", "patientId": "PAT1", "projectUUID": PROJECT_UUID}
    client, session = make_client([FakeResponse(409, existing)], auth_mode="basic")
    result = client.create_pseudonym(
        {"pseudonym": "PSY1", "patientId": "PAT1", "projectUUID": PROJECT_UUID}
    )
    assert result == existing
    assert session.calls[0]["method"] == "POST"


def test_create_pseudonym_raises_on_other_errors() -> None:
    client, _ = make_client([FakeResponse(400, "bad")], auth_mode="basic")
    with pytest.raises(KarnakApiError) as exc:
        client.create_pseudonym({"pseudonym": "PSY1"})
    assert exc.value.status_code == 400


def test_list_pseudonyms_lists_all_not_project_scoped() -> None:
    client, session = make_client([FakeResponse(200, [{"uuid": "p1"}])], auth_mode="basic")
    result = client.list_pseudonyms()
    assert result == [{"uuid": "p1"}]
    assert session.calls[0]["url"] == f"{BASE}/api/pseudonyms"


def test_import_pseudonyms_posts_bulk_array() -> None:
    rows = [{"pseudonym": "PSY1", "patientId": "PAT1", "projectUUID": PROJECT_UUID}]
    summary = {"created": 1, "conflict": 0, "error": 0, "issues": []}
    client, session = make_client([FakeResponse(200, summary)], auth_mode="basic")
    result = client.import_pseudonyms(rows)
    assert result == summary
    assert session.calls[0]["url"] == f"{BASE}/api/pseudonyms/import"
    assert session.calls[0]["json"] == rows


def test_import_pseudonyms_falls_back_per_row_on_404() -> None:
    """Older Karnak without the fork's bulk endpoint: synthesise the summary.

    Shape must mirror the server's ``PseudonymImportResult``/
    ``PseudonymImportIssue``: an ``issues`` list (not ``rows``), one entry
    per conflicting/errored row (not per row), each carrying ``index``,
    ``reason``, and ``existing``.
    """
    rows = [
        {"pseudonym": "PSY1", "patientId": "PAT1", "projectUUID": PROJECT_UUID},
        {"pseudonym": "PSY2", "patientId": "PAT2", "projectUUID": PROJECT_UUID},
    ]
    existing = {"uuid": "p2", **rows[1]}
    client, session = make_client(
        [
            FakeResponse(404, "no such endpoint"),
            FakeResponse(201, {"uuid": "p1", **rows[0]}),
            FakeResponse(409, existing),
        ],
        auth_mode="basic",
    )
    result = client.import_pseudonyms(rows)
    assert result["created"] == 1
    assert result["conflict"] == 1
    assert result["error"] == 0
    # Only the conflicting row (index 1) gets an issue — the created row
    # (index 0) is just counted, same as the native endpoint.
    assert result["issues"] == [
        {
            "index": 1,
            "reason": "A pseudonym already exists for this patient and project",
            "existing": existing,
        }
    ]
    urls = [c["url"] for c in session.calls]
    assert urls == [
        f"{BASE}/api/pseudonyms/import",
        f"{BASE}/api/pseudonyms",
        f"{BASE}/api/pseudonyms",
    ]


def test_import_pseudonyms_fallback_error_row_has_no_existing() -> None:
    """An error row (not a conflict) reports ``existing: None``, matching the

    server's ``PseudonymImportIssue`` field, which is only populated for
    conflicts.
    """
    rows = [{"pseudonym": "PSY1", "patientId": "PAT1", "projectUUID": PROJECT_UUID}]
    client, session = make_client(
        [
            FakeResponse(404, "no such endpoint"),
            FakeResponse(400, "validation failed"),
        ],
        auth_mode="basic",
    )
    result = client.import_pseudonyms(rows)
    assert result["created"] == 0
    assert result["conflict"] == 0
    assert result["error"] == 1
    assert len(result["issues"]) == 1
    issue = result["issues"][0]
    assert issue["index"] == 0
    assert issue["existing"] is None
    assert "reason" in issue


def test_import_pseudonyms_fallback_matches_native_summary_keys() -> None:
    """Whichever path `import_pseudonyms` takes, the summary shape is the same."""
    rows = [{"pseudonym": "PSY1", "patientId": "PAT1", "projectUUID": PROJECT_UUID}]

    native_client, _ = make_client(
        [FakeResponse(200, {"created": 1, "conflict": 0, "error": 0, "issues": []})],
        auth_mode="basic",
    )
    native_result = native_client.import_pseudonyms(rows)

    fallback_client, _ = make_client(
        [
            FakeResponse(404, "no such endpoint"),
            FakeResponse(201, {"uuid": "p1", **rows[0]}),
        ],
        auth_mode="basic",
    )
    fallback_result = fallback_client.import_pseudonyms(rows)

    assert set(native_result.keys()) == set(fallback_result.keys()) == {
        "created",
        "conflict",
        "error",
        "issues",
    }


def test_delete_project_pseudonyms() -> None:
    client, session = make_client([FakeResponse(204)], auth_mode="basic")
    client.delete_project_pseudonyms(PROJECT_UUID)
    assert session.calls[0]["url"] == f"{BASE}/api/pseudonyms/project/{PROJECT_UUID}"
    assert session.calls[0]["method"] == "DELETE"


# ── monitoring ───────────────────────────────────────────────────────────────


def test_query_transfers_returns_page_shape_as_is() -> None:
    page = {
        "content": [{"forwardAeTitle": "FWD"}],
        "totalElements": 1,
        "totalPages": 1,
        "number": 0,
        "size": 20,
    }
    client, session = make_client([FakeResponse(200, page)], auth_mode="basic")
    result = client.query_transfers({"statusType": "SENT", "page": 0, "size": 20})
    assert result == page
    assert session.calls[0]["params"] == {"statusType": "SENT", "page": 0, "size": 20}


def test_count_transfers_returns_bare_int_not_dict() -> None:
    """Regression: /transfers/count returns a bare integer body."""
    client, _ = make_client([FakeResponse(200, 42)], auth_mode="basic")
    result = client.count_transfers({"statusType": "ALL"})
    assert result == 42
    assert isinstance(result, int)


# ── auth configs (fork-only) ──────────────────────────────────────────────────


def test_delete_auth_config() -> None:
    client, session = make_client([FakeResponse(204)], auth_mode="basic")
    client.delete_auth_config("chuv-oidc")
    assert session.calls[0]["url"] == f"{BASE}/api/auth-configs/chuv-oidc"
    assert session.calls[0]["method"] == "DELETE"


# ── compatibility surface ───────────────────────────────────────────────────


def test_aliases_point_at_canonical_methods() -> None:
    assert KarnakClient.generate_secret is KarnakClient.add_project_secret
    assert KarnakClient.create_destination is KarnakClient.add_destination


def test_kwargs_constructor_and_base_property() -> None:
    session = FakeSession([])
    client = KarnakClient(
        base_url=f"{BASE}/",
        username="u",
        password="p",
        auth_mode="form",
        session=session,  # type: ignore[arg-type]
    )
    assert client.base == BASE
    assert client.config.username == "u"
    assert client.config.auth_mode == "form"


def test_invalid_auth_mode_rejected() -> None:
    with pytest.raises(ValueError):
        KarnakClientConfig(base_url=BASE, username="u", password="p", auth_mode="oauth")
