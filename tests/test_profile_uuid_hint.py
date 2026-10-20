"""``config.profile.uuid`` lets an apply reuse a profile without listing the whole store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_apply import PROFILE_UUID, FakeKarnakClient, _local_receiver_config

from karnak_api_client import KarnakApiError, apply_config

OTHER_UUID = "99999999-9999-9999-9999-999999999999"


class HintClient(FakeKarnakClient):
    """A Karnak that counts store listings and answers GET /api/profiles/{uuid}."""

    def __init__(self, profiles: dict[str, dict[str, Any]], get_error: KarnakApiError | None = None) -> None:
        super().__init__()
        self.profiles = profiles
        self.get_error = get_error
        self.listings = 0
        self.gets: list[str] = []

    def list_profiles(self) -> list[dict[str, Any]]:
        self.listings += 1
        return list(self.profiles.values())

    def get_profile(self, profile_uuid: str) -> dict[str, Any]:
        self.gets.append(profile_uuid)
        if self.get_error is not None:
            raise self.get_error
        if profile_uuid not in self.profiles:
            raise KarnakApiError("GET", f"/api/profiles/{profile_uuid}", 404, "not found")
        return self.profiles[profile_uuid]


def _config(tmp_path: Path, **profile: Any) -> dict[str, Any]:
    path = tmp_path / "profile.yml"
    path.write_text("name: Profile A\nversion: '1.0'\n", encoding="utf-8")
    config = _local_receiver_config(path)
    config["profile"].update(profile)
    return config


def _store() -> dict[str, dict[str, Any]]:
    return {PROFILE_UUID: {"uuid": PROFILE_UUID, "name": "Profile A", "version": "1.0"}}


def test_a_matching_uuid_hint_skips_the_listing(tmp_path: Path) -> None:
    client = HintClient(_store())

    summary = apply_config(client, _config(tmp_path, uuid=PROFILE_UUID))  # type: ignore[arg-type]

    assert client.listings == 0
    assert client.gets == [PROFILE_UUID]
    assert summary["profile"] == {"uuid": PROFILE_UUID, "name": "Profile A", "version": "1.0", "created": False}
    assert client.uploaded_profiles == []


def test_without_a_hint_the_store_is_listed(tmp_path: Path) -> None:
    client = HintClient(_store())

    apply_config(client, _config(tmp_path))  # type: ignore[arg-type]

    assert client.listings == 1
    assert client.gets == []


@pytest.mark.parametrize("status", [400, 404])
def test_a_stale_hint_falls_back_to_the_listing(tmp_path: Path, status: int) -> None:
    client = HintClient(_store(), get_error=KarnakApiError("GET", "/api/profiles/x", status, "gone"))

    summary = apply_config(client, _config(tmp_path, uuid=OTHER_UUID))  # type: ignore[arg-type]

    assert client.gets == [OTHER_UUID]
    assert client.listings == 1
    assert summary["profile"]["uuid"] == PROFILE_UUID


@pytest.mark.parametrize(
    "stored",
    [
        {"uuid": OTHER_UUID, "name": "Another profile", "version": "1.0"},
        {"uuid": OTHER_UUID, "name": "Profile A", "version": "2.0"},
    ],
    ids=["other-name", "other-version"],
)
def test_a_hint_for_a_different_profile_is_not_trusted(tmp_path: Path, stored: dict[str, Any]) -> None:
    profiles = _store() | {OTHER_UUID: stored}
    client = HintClient(profiles)

    summary = apply_config(client, _config(tmp_path, uuid=OTHER_UUID))  # type: ignore[arg-type]

    assert client.listings == 1
    assert summary["profile"]["uuid"] == PROFILE_UUID  # found by name and version, not by the hint


def test_a_server_error_on_the_hint_is_not_swallowed(tmp_path: Path) -> None:
    client = HintClient(_store(), get_error=KarnakApiError("GET", "/api/profiles/x", 503, "down"))

    with pytest.raises(KarnakApiError):
        apply_config(client, _config(tmp_path, uuid=PROFILE_UUID))  # type: ignore[arg-type]

    assert client.listings == 0


def test_a_forced_upload_ignores_the_hint(tmp_path: Path) -> None:
    client = HintClient(_store())

    summary = apply_config(client, _config(tmp_path, uuid=PROFILE_UUID, forceUpload=True))  # type: ignore[arg-type]

    assert client.gets == [] and client.listings == 0
    assert len(client.uploaded_profiles) == 1
    assert summary["profile"]["created"] is True
