"""Tests for idempotent Karnak apply orchestration (v2.0.0 UUID-keyed API).

Ported from MiCo-BID-pipeline tests/unit/test_karnak_apply.py when the apply
layer moved into this package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from karnak_api_client import apply_config

PROFILE_UUID = "11111111-1111-1111-1111-111111111111"
PROJECT_UUID = "22222222-2222-2222-2222-222222222222"
FORWARD_NODE_UUID = "33333333-3333-3333-3333-333333333333"
DESTINATION_UUID = "44444444-4444-4444-4444-444444444444"
NEW_DESTINATION_UUID = "55555555-5555-5555-5555-555555555555"


class FakeKarnakClient:
    def __init__(
        self,
        destinations: list[dict[str, Any]] | None = None,
        secrets: list[dict[str, Any]] | None = None,
    ) -> None:
        self.updated_destinations: list[dict[str, Any]] = []
        self.added_destinations: list[dict[str, Any]] = []
        self.deleted_destinations: list[str] = []
        self.uploaded_profiles: list[Path] = []
        self.updated_projects: list[dict[str, Any]] = []
        self.destinations: list[dict[str, Any]] = (
            destinations
            if destinations is not None
            else [{"uuid": DESTINATION_UUID, "destinationType": "dicom", "aeTitle": "LOCAL-STORAGE"}]
        )
        self.secrets: list[dict[str, Any]] = (
            secrets if secrets is not None else [{"uuid": "s1", "active": True}]
        )

    def list_profiles(self) -> list[dict[str, Any]]:
        return [{"uuid": PROFILE_UUID, "name": "Profile A", "version": "1.0"}]

    def upload_profile(self, path: Path) -> dict[str, Any]:
        self.uploaded_profiles.append(path)
        return {"uuid": PROFILE_UUID, "name": "Uploaded"}

    def list_projects(self) -> list[dict[str, Any]]:
        return [{"uuid": PROJECT_UUID, "name": "Project A", "profileUuid": PROFILE_UUID}]

    def update_project(self, project_uuid: str, *, name: str, profile_uuid: str) -> dict[str, Any]:
        self.updated_projects.append({"uuid": project_uuid, "name": name, "profileUuid": profile_uuid})
        return {"uuid": project_uuid, "name": name, "profileUuid": profile_uuid}

    def create_project(self, name: str, profile_uuid: str) -> dict[str, Any]:
        return {"uuid": PROJECT_UUID, "name": name, "profileUuid": profile_uuid}

    def list_secrets(self, project_uuid: str) -> list[dict[str, Any]]:
        return self.secrets

    def add_project_secret(self, project_uuid: str, hex_key: str | None = None) -> dict[str, Any]:
        return {"projectUuid": project_uuid, "key": hex_key or "generated", "active": True}

    def list_auth_configs(self) -> list[dict[str, Any]]:
        return []

    def list_forward_nodes(self) -> list[dict[str, Any]]:
        return [{"uuid": FORWARD_NODE_UUID, "fwdAeTitle": "KARNAK-GATEWAY"}]

    def update_forward_node(self, forward_node_uuid: str, ae_title: str, description: str = "") -> dict[str, Any]:
        return {"uuid": forward_node_uuid, "fwdAeTitle": ae_title, "fwdDescription": description}

    def create_forward_node(self, ae_title: str, description: str = "") -> dict[str, Any]:
        return {"uuid": FORWARD_NODE_UUID, "fwdAeTitle": ae_title, "fwdDescription": description}

    def list_source_nodes(self, _forward_node_uuid: str) -> list[dict[str, Any]]:
        return []

    def list_destinations(self, _forward_node_uuid: str) -> list[dict[str, Any]]:
        return self.destinations

    def update_destination(
        self,
        _forward_node_uuid: str,
        destination_uuid: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        self.updated_destinations.append({"uuid": destination_uuid, **body})
        return {"uuid": destination_uuid, **body}

    def add_destination(self, _forward_node_uuid: str, body: dict[str, Any]) -> dict[str, Any]:
        self.added_destinations.append(body)
        return {"uuid": NEW_DESTINATION_UUID, **body}

    def delete_destination(self, _forward_node_uuid: str, destination_uuid: str) -> None:
        self.deleted_destinations.append(destination_uuid)


def test_apply_config_reuses_existing_resources_and_links_destination(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yml"
    profile.write_text("name: Profile A\nversion: '1.0'\n", encoding="utf-8")
    client = FakeKarnakClient()

    summary = apply_config(
        client,  # type: ignore[arg-type]
        {
            "profile": {"path": str(profile), "name": "Profile A", "version": "1.0"},
            "project": {"name": "Project A", "ensureSecret": True},
            "forwardNode": {"aeTitle": "KARNAK-GATEWAY"},
            "sourceNodes": [],
            "destinations": [
                {
                    "destinationType": "dicom",
                    "aeTitle": "LOCAL-STORAGE",
                    "hostname": "dicom_receiver",
                    "port": 11104,
                    "activate": True,
                    "desidentification": True,
                }
            ],
        },
    )

    assert summary["profile"]["created"] is False
    assert summary["profile"]["uuid"] == PROFILE_UUID
    assert summary["project"]["created"] is False
    assert summary["project"]["uuid"] == PROJECT_UUID
    assert summary["project"]["secret_created"] is False
    assert client.uploaded_profiles == []
    # Existing project already matches the desired profile (profileUuid
    # equal), so no rebind PUT is issued.
    assert client.updated_projects == []
    # The stub destination lacks hostname/port, so this is a real change and
    # takes the delete+add path — PUT is never used (it deletes server-side).
    assert client.added_destinations[0]["deIdentificationProjectUuid"] == PROJECT_UUID
    assert client.updated_destinations == []


def test_apply_config_rebinds_project_to_profile_when_mismatched(tmp_path: Path) -> None:
    """v2.0.0 can express the project->profile link over REST — use it."""
    profile = tmp_path / "profile.yml"
    profile.write_text("name: Profile A\nversion: '1.0'\n", encoding="utf-8")

    class MismatchedProfileClient(FakeKarnakClient):
        def list_projects(self) -> list[dict[str, Any]]:
            return [{"uuid": PROJECT_UUID, "name": "Project A", "profileUuid": "old-profile-uuid"}]

    client = MismatchedProfileClient()
    summary = apply_config(
        client,  # type: ignore[arg-type]
        {
            "profile": {"path": str(profile), "name": "Profile A", "version": "1.0"},
            "project": {"name": "Project A", "ensureSecret": False},
            "forwardNode": {"aeTitle": "KARNAK-GATEWAY"},
            "sourceNodes": [],
            "destinations": [],
        },
    )

    assert client.updated_projects == [
        {"uuid": PROJECT_UUID, "name": "Project A", "profileUuid": PROFILE_UUID}
    ]
    assert summary["project"]["uuid"] == PROJECT_UUID


def _stow_only_config(profile: Path) -> dict[str, Any]:
    """Desired state with a single Shanoir STOW destination."""
    return {
        "profile": {"path": str(profile), "name": "Profile A", "version": "1.0"},
        "project": {"name": "Project A", "ensureSecret": True},
        "forwardNode": {"aeTitle": "KARNAK-GATEWAY"},
        "sourceNodes": [],
        "destinations": [
            {
                "destinationType": "stow",
                "url": "https://shanoir.example/shanoir-ng/datasets/dicomweb/studies",
                "activate": True,
                "desidentification": True,
            }
        ],
    }


def test_apply_config_deactivates_destinations_the_config_no_longer_requests(
    tmp_path: Path,
) -> None:
    """A local receiver left over from an earlier run must not keep receiving.

    Without reconciliation the apply is additive: switching a forward node from
    the local receiver to Shanoir STOW leaves both wired, and every subsequent
    study is delivered twice.
    """
    profile = tmp_path / "profile.yml"
    profile.write_text("name: Profile A\nversion: '1.0'\n", encoding="utf-8")
    client = FakeKarnakClient(
        destinations=[
            {
                "uuid": DESTINATION_UUID,
                "destinationType": "dicom",
                "aeTitle": "LOCAL-STORAGE",
                "activate": True,
            }
        ]
    )

    summary = apply_config(client, _stow_only_config(profile))  # type: ignore[arg-type]

    deactivated = [d for d in summary["destinations"] if d.get("deactivated")]
    assert [d["uuid"] for d in deactivated] == [DESTINATION_UUID]
    stale_writes = [d for d in client.updated_destinations if d["uuid"] == DESTINATION_UUID]
    assert stale_writes and stale_writes[0]["activate"] is False


def test_apply_config_reconcile_can_be_disabled(tmp_path: Path) -> None:
    """``reconcileDestinations: false`` keeps the historical additive behaviour."""
    profile = tmp_path / "profile.yml"
    profile.write_text("name: Profile A\nversion: '1.0'\n", encoding="utf-8")
    client = FakeKarnakClient(
        destinations=[
            {
                "uuid": DESTINATION_UUID,
                "destinationType": "dicom",
                "aeTitle": "LOCAL-STORAGE",
                "activate": True,
            }
        ]
    )

    config = _stow_only_config(profile)
    config["reconcileDestinations"] = False
    summary = apply_config(client, config)  # type: ignore[arg-type]

    assert not any(d.get("deactivated") for d in summary["destinations"])
    assert all(d["uuid"] != DESTINATION_UUID for d in client.updated_destinations)


def test_apply_config_leaves_already_inactive_destinations_alone(tmp_path: Path) -> None:
    """Repeated applies must not keep rewriting a destination already disabled."""
    profile = tmp_path / "profile.yml"
    profile.write_text("name: Profile A\nversion: '1.0'\n", encoding="utf-8")
    client = FakeKarnakClient(
        destinations=[
            {
                "uuid": DESTINATION_UUID,
                "destinationType": "dicom",
                "aeTitle": "LOCAL-STORAGE",
                "activate": False,
            }
        ]
    )

    summary = apply_config(client, _stow_only_config(profile))  # type: ignore[arg-type]

    assert not any(d.get("deactivated") for d in summary["destinations"])
    assert all(d["uuid"] != DESTINATION_UUID for d in client.updated_destinations)


def _local_receiver_config(profile: Path) -> dict[str, Any]:
    return {
        "profile": {"path": str(profile), "name": "Profile A", "version": "1.0"},
        "project": {"name": "Project A", "ensureSecret": True},
        "forwardNode": {"aeTitle": "KARNAK-GATEWAY"},
        "sourceNodes": [],
        "destinations": [
            {
                "destinationType": "dicom",
                "aeTitle": "LOCAL-STORAGE",
                "hostname": "dicom_receiver",
                "port": 11104,
                "activate": True,
                "desidentification": True,
            }
        ],
    }


def test_reapplying_an_unchanged_destination_writes_nothing(tmp_path: Path) -> None:
    """Karnak's PUT on an existing destination deletes it (200, then gone).

    Re-provisioning with an unchanged config must therefore be a pure no-op, or
    the gateway silently ends up with nowhere to deliver.
    """
    profile = tmp_path / "profile.yml"
    profile.write_text("name: Profile A\nversion: '1.0'\n", encoding="utf-8")
    client = FakeKarnakClient(
        destinations=[
            {
                "uuid": DESTINATION_UUID,
                "destinationType": "dicom",
                "aeTitle": "LOCAL-STORAGE",
                "hostname": "dicom_receiver",
                "port": 11104,
                "activate": True,
                "desidentification": True,
                "deIdentificationProjectUuid": PROJECT_UUID,
                "createdAt": "2026-08-21T10:00:00Z",
            }
        ]
    )

    summary = apply_config(client, _local_receiver_config(profile))  # type: ignore[arg-type]

    assert client.updated_destinations == []
    assert client.deleted_destinations == []
    assert client.added_destinations == []
    assert summary["destinations"][0]["unchanged"] is True


def test_a_changed_destination_is_replaced_not_put(tmp_path: Path) -> None:
    """A real change deletes and re-adds, because PUT cannot be used at all."""
    profile = tmp_path / "profile.yml"
    profile.write_text("name: Profile A\nversion: '1.0'\n", encoding="utf-8")
    client = FakeKarnakClient(
        destinations=[
            {
                "uuid": DESTINATION_UUID,
                "destinationType": "dicom",
                "aeTitle": "LOCAL-STORAGE",
                "hostname": "OLD-HOST",
                "port": 11104,
                "activate": True,
                "desidentification": True,
                "deIdentificationProjectUuid": PROJECT_UUID,
            }
        ]
    )

    summary = apply_config(client, _local_receiver_config(profile))  # type: ignore[arg-type]

    assert client.updated_destinations == []
    assert client.deleted_destinations == [DESTINATION_UUID]
    assert client.added_destinations[0]["hostname"] == "dicom_receiver"
    assert summary["destinations"][0]["replaced"] is True


def test_ensure_project_skips_secret_creation_when_active_secret_exists(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yml"
    profile.write_text("name: Profile A\nversion: '1.0'\n", encoding="utf-8")
    client = FakeKarnakClient(secrets=[{"uuid": "s1", "active": True}])

    summary = apply_config(
        client,  # type: ignore[arg-type]
        {
            "profile": {"path": str(profile), "name": "Profile A", "version": "1.0"},
            "project": {"name": "Project A", "ensureSecret": True},
            "forwardNode": {"aeTitle": "KARNAK-GATEWAY"},
            "sourceNodes": [],
            "destinations": [],
        },
    )

    assert summary["project"]["secret_created"] is False


def test_ensure_project_creates_secret_when_none_active(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yml"
    profile.write_text("name: Profile A\nversion: '1.0'\n", encoding="utf-8")
    client = FakeKarnakClient(secrets=[])

    summary = apply_config(
        client,  # type: ignore[arg-type]
        {
            "profile": {"path": str(profile), "name": "Profile A", "version": "1.0"},
            "project": {"name": "Project A", "ensureSecret": True},
            "forwardNode": {"aeTitle": "KARNAK-GATEWAY"},
            "sourceNodes": [],
            "destinations": [],
        },
    )

    assert summary["project"]["secret_created"] is True
    assert "secret" in summary["project"]


def test_apply_config_calls_import_pseudonyms_for_external_ids(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yml"
    profile.write_text("name: Profile A\nversion: '1.0'\n", encoding="utf-8")

    class PseudonymTrackingClient(FakeKarnakClient):
        def __init__(self) -> None:
            super().__init__()
            self.imported_rows: list[list[dict[str, Any]]] = []

        def import_pseudonyms(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
            self.imported_rows.append(rows)
            return {"created": len(rows), "conflict": 0, "error": 0, "rows": rows}

    client = PseudonymTrackingClient()
    rows = [{"pseudonym": "PSY1", "patientId": "PAT1", "projectUUID": PROJECT_UUID}]
    summary = apply_config(
        client,  # type: ignore[arg-type]
        {
            "profile": {"path": str(profile), "name": "Profile A", "version": "1.0"},
            "project": {"name": "Project A", "ensureSecret": False},
            "forwardNode": {"aeTitle": "KARNAK-GATEWAY"},
            "sourceNodes": [],
            "destinations": [],
            "externalIds": rows,
        },
    )

    assert client.imported_rows == [rows]
    assert summary["external_ids_import"] == {
        "created": 1,
        "conflict": 0,
        "error": 0,
        "rows": rows,
    }
