"""Idempotent desired-state application for Karnak REST resources.

``apply_config(client, config)`` takes a JSON-compatible config document
describing the desired profile, project (+ HMAC secret), auth configs,
forward node, source nodes, destinations and external-ID (pseudonym) imports,
and makes the minimum API calls needed to converge Karnak onto that state.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .client import KarnakClient


class KarnakConfigError(ValueError):
    """Raised when a desired Karnak config cannot be built or validated."""


def profile_meta_from_yaml(path: Path) -> dict[str, str]:
    """Extract simple top-level YAML name/version without adding PyYAML."""
    meta: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise KarnakConfigError(f"Cannot read profile YAML {path}: {exc}") from exc

    for line in lines:
        match = re.match(r"^(name|version):\s*[\"']?([^\"'#]+)", line)
        if match:
            meta[match.group(1)] = match.group(2).strip()
        if "name" in meta and "version" in meta:
            break
    return meta


def load_apply_config(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KarnakConfigError(f"Cannot read JSON config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise KarnakConfigError("Karnak config must be a JSON object")
    return raw


def write_json_atomic(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _require_object(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    if not isinstance(value, dict):
        raise KarnakConfigError(f"config.{key} is required")
    return value


def _require_list(config: dict[str, Any], key: str) -> list[Any]:
    value = config.get(key) or []
    if not isinstance(value, list):
        raise KarnakConfigError(f"{key} must be a list")
    return value


def _load_external_ids(config: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _require_list(config, "externalIds")
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise KarnakConfigError("externalIds entries must be objects")
        out.append(row)

    external_ids_path = config.get("externalIdsPath")
    if external_ids_path:
        p = Path(str(external_ids_path))
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KarnakConfigError(f"Cannot read externalIdsPath {p}: {exc}") from exc
        if not isinstance(loaded, list):
            raise KarnakConfigError("externalIdsPath must contain a JSON array")
        for row in loaded:
            if not isinstance(row, dict):
                raise KarnakConfigError("externalIdsPath entries must be objects")
            out.append(row)
    return out


def _find_profile(
    client: KarnakClient,
    *,
    name: str,
    version: str | None,
) -> dict[str, Any] | None:
    for item in client.list_profiles():
        if str(item.get("name", "")) != name:
            continue
        if version is not None and str(item.get("version", "")) != version:
            continue
        return item
    return None


def _ensure_profile(client: KarnakClient, config: dict[str, Any]) -> dict[str, Any]:
    profile_cfg = _require_object(config, "profile")
    path_raw = profile_cfg.get("path")
    if not path_raw:
        raise KarnakConfigError("config.profile.path is required")
    path = Path(str(path_raw))
    if not path.is_file():
        raise KarnakConfigError(f"Profile YAML does not exist: {path}")

    yaml_meta = profile_meta_from_yaml(path)
    name = str(profile_cfg.get("name") or yaml_meta.get("name") or "").strip()
    version = str(profile_cfg.get("version") or yaml_meta.get("version") or "").strip() or None
    if not name:
        raise KarnakConfigError("config.profile.name is required when YAML name cannot be inferred")

    force_upload = bool(profile_cfg.get("forceUpload", False))
    if not force_upload:
        existing = _find_profile(client, name=name, version=version)
        if existing:
            return {
                "uuid": str(existing["uuid"]),
                "name": existing.get("name", name),
                "version": existing.get("version", version),
                "created": False,
            }

    created = client.upload_profile(path)
    return {
        "uuid": str(created["uuid"]),
        "name": created.get("name", name),
        "version": version,
        "created": True,
    }


def _find_project(client: KarnakClient, name: str) -> dict[str, Any] | None:
    for item in client.list_projects():
        if str(item.get("name", "")) == name:
            return item
    return None


def _project_has_active_secret(client: KarnakClient, project_uuid: str) -> bool:
    secrets = client.list_secrets(project_uuid)
    return any(bool(s.get("active")) for s in secrets if isinstance(s, dict))


def _ensure_project(
    client: KarnakClient,
    config: dict[str, Any],
    profile_uuid: str,
) -> dict[str, Any]:
    project_cfg = _require_object(config, "project")
    name = str(project_cfg.get("name", "")).strip()
    if not name:
        raise KarnakConfigError("config.project.name is required")

    existing = _find_project(client, name)
    if existing:
        project_uuid = str(existing["uuid"])
        # Unlike the old API, the project→profile link IS expressible over
        # REST now (ProjectModel.profileUuid) — bind whenever it drifts from
        # the desired profile instead of leaving it unset.
        if str(existing.get("profileUuid", "")) != profile_uuid:
            client.update_project(project_uuid, name=name, profile_uuid=profile_uuid)
        created = False
    else:
        project = client.create_project(name, profile_uuid)
        project_uuid = str(project["uuid"])
        created = True

    secret_created = False
    secret_result: dict[str, Any] | None = None
    ensure_secret = bool(project_cfg.get("ensureSecret", True))
    secret_hex = project_cfg.get("secretHex")
    if secret_hex or (ensure_secret and not _project_has_active_secret(client, project_uuid)):
        secret_result = client.add_project_secret(project_uuid, str(secret_hex) if secret_hex else None)
        secret_created = True

    summary: dict[str, Any] = {
        "uuid": project_uuid,
        "name": name,
        "created": created,
        "secret_created": secret_created,
    }
    if secret_result:
        # Karnak only returns the raw key once. Store it in the apply summary
        # for the operator, but callers must not write it to logs.
        summary["secret"] = secret_result
    return summary


def _ensure_auth_configs(client: KarnakClient, config: dict[str, Any]) -> list[dict[str, Any]]:
    requested = _require_list(config, "authConfigs")
    existing_codes = {
        str(item.get("code"))
        for item in client.list_auth_configs()
        if isinstance(item, dict) and item.get("code")
    }
    summary: list[dict[str, Any]] = []
    for body in requested:
        if not isinstance(body, dict):
            raise KarnakConfigError("authConfigs entries must be objects")
        code = str(body.get("code", "")).strip()
        if not code:
            raise KarnakConfigError("authConfigs[].code is required")
        if code in existing_codes:
            client.update_auth_config(code, body)
            summary.append({"code": code, "created": False})
        else:
            client.create_auth_config(body)
            existing_codes.add(code)
            summary.append({"code": code, "created": True})
    return summary


def _find_forward_node(client: KarnakClient, ae_title: str) -> dict[str, Any] | None:
    for item in client.list_forward_nodes():
        if str(item.get("fwdAeTitle", "")) == ae_title:
            return item
    return None


def _ensure_forward_node(client: KarnakClient, config: dict[str, Any]) -> dict[str, Any]:
    node_cfg = _require_object(config, "forwardNode")
    ae_title = str(node_cfg.get("aeTitle") or node_cfg.get("fwdAeTitle") or "").strip()
    if not ae_title:
        raise KarnakConfigError("config.forwardNode.aeTitle is required")
    description = str(node_cfg.get("description") or node_cfg.get("fwdDescription") or "")

    existing = _find_forward_node(client, ae_title)
    if existing:
        node = client.update_forward_node(str(existing["uuid"]), ae_title, description)
        created = False
    else:
        node = client.create_forward_node(ae_title, description)
        created = True
    return {
        "uuid": str(node["uuid"]),
        "ae_title": ae_title,
        "created": created,
    }


def _source_key(source: dict[str, Any]) -> tuple[str, str, bool]:
    return (
        str(source.get("aeTitle", "")),
        str(source.get("hostname", "")),
        bool(source.get("checkHostname", False)),
    )


def _ensure_sources(
    client: KarnakClient,
    forward_node_uuid: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    requested = _require_list(config, "sourceNodes")
    existing_keys = {
        _source_key(item)
        for item in client.list_source_nodes(forward_node_uuid)
        if isinstance(item, dict)
    }
    summary: list[dict[str, Any]] = []
    for body in requested:
        if not isinstance(body, dict):
            raise KarnakConfigError("sourceNodes entries must be objects")
        if not str(body.get("aeTitle", "")).strip():
            raise KarnakConfigError("sourceNodes[].aeTitle is required")
        key = _source_key(body)
        if key in existing_keys:
            summary.append({"aeTitle": body.get("aeTitle"), "created": False})
            continue
        created = client.add_source_node(forward_node_uuid, body)
        existing_keys.add(key)
        summary.append({"uuid": created.get("uuid"), "aeTitle": body.get("aeTitle"), "created": True})
    return summary


def _destination_key(destination: dict[str, Any]) -> tuple[str, str]:
    dtype = str(destination.get("destinationType") or destination.get("type") or "").lower()
    if dtype == "stow" or destination.get("url"):
        return ("stow", str(destination.get("url", "")))
    return ("dicom", str(destination.get("aeTitle", "")))


def _ensure_destinations(
    client: KarnakClient,
    forward_node_uuid: str,
    project_uuid: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Converge a forward node's destinations onto ``config["destinations"]``.

    Upserts every requested destination, then — unless ``reconcileDestinations``
    is set to false — **deactivates** every still-active destination on the node
    that the config did not ask for.

    That last step is not cosmetic. Without it the apply is additive-only: a
    forward node configured once for a local DICOM receiver and later for a
    Shanoir STOW endpoint keeps *both*, and every subsequent study fans out to
    both destinations. For a de-identification gateway that is a silent
    duplication of patient data to an endpoint nobody asked for, so the safe
    default is to reconcile.

    Unwanted destinations are deactivated (``activate: False``) rather than
    deleted, so Karnak keeps the transfer history attached to them.
    """
    requested = _require_list(config, "destinations")
    reconcile = bool(config.get("reconcileDestinations", True))
    existing_by_key = {
        _destination_key(item): item
        for item in client.list_destinations(forward_node_uuid)
        if isinstance(item, dict)
    }
    requested_keys: set[tuple[str, str]] = set()
    summary: list[dict[str, Any]] = []
    for raw_body in requested:
        if not isinstance(raw_body, dict):
            raise KarnakConfigError("destinations entries must be objects")
        body = dict(raw_body)
        if body.get("desidentification") and not body.get("deIdentificationProjectUuid"):
            body["deIdentificationProjectUuid"] = project_uuid
        if body.get("activateTagMorphing") and not body.get("tagMorphingProjectUuid"):
            body["tagMorphingProjectUuid"] = project_uuid

        key = _destination_key(body)
        if not key[1]:
            raise KarnakConfigError("destinations require aeTitle for DICOM or url for STOW-RS")
        requested_keys.add(key)
        existing = existing_by_key.get(key)
        if existing:
            destination_uuid = str(existing["uuid"])
            if _matches(existing, body):
                # Nothing to change. Skipping the write is not just an
                # optimisation: Karnak's PUT on an existing destination
                # DESTROYS it (returns 200, then the destination is gone from
                # the forward node), so re-applying an unchanged config would
                # silently leave the gateway with nowhere to deliver.
                summary.append(
                    {"uuid": destination_uuid, "key": list(key), "created": False, "unchanged": True}
                )
                continue
            # A real change. PUT is unusable (see above), so replace: the
            # destination uuid changes and its transfer history goes with it,
            # which is the lesser evil against a node left with no destination.
            client.delete_destination(forward_node_uuid, destination_uuid)
            created = client.add_destination(forward_node_uuid, body)
            destination_uuid = str(created.get("uuid", "")) if isinstance(created, dict) else ""
            summary.append(
                {"uuid": destination_uuid, "key": list(key), "created": True, "replaced": True}
            )
        else:
            created = client.add_destination(forward_node_uuid, body)
            destination_uuid = str(created.get("uuid", "")) if isinstance(created, dict) else ""
            summary.append({"uuid": destination_uuid, "key": list(key), "created": True})

    if reconcile:
        summary.extend(
            _deactivate_unrequested(client, forward_node_uuid, existing_by_key, requested_keys)
        )
    return summary



def _matches(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    """True when every field the desired config sets already has that value.

    Only the keys we manage are compared — Karnak decorates a destination with
    server-side fields (uuid, timestamps, association state) that must not
    count as drift, or every apply would look like a change and trigger a
    replace. ``deIdentificationProjectUuid`` / ``tagMorphingProjectUuid`` are
    flat uuid strings in v2.0.0, so no special nested-object handling is
    needed for them any more — plain equality covers everything.
    """
    for field, want in desired.items():
        if existing.get(field) != want:
            return False
    return True


def _deactivate_unrequested(
    client: KarnakClient,
    forward_node_uuid: str,
    existing_by_key: dict[tuple[str, str], dict[str, Any]],
    requested_keys: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Set ``activate: False`` on destinations the config did not request."""
    summary: list[dict[str, Any]] = []
    for key, existing in existing_by_key.items():
        if key in requested_keys:
            continue
        # Already inactive: nothing to converge, and skipping the PUT keeps
        # repeated applies free of pointless writes.
        if not existing.get("activate", True):
            continue
        destination_uuid = str(existing["uuid"])
        merged = dict(existing)
        merged["activate"] = False
        client.update_destination(forward_node_uuid, destination_uuid, merged)
        summary.append(
            {"uuid": destination_uuid, "key": list(key), "created": False, "deactivated": True}
        )
    return summary


def apply_config(client: KarnakClient, config: dict[str, Any]) -> dict[str, Any]:
    profile = _ensure_profile(client, config)
    project = _ensure_project(client, config, str(profile["uuid"]))
    auth_configs = _ensure_auth_configs(client, config)
    forward_node = _ensure_forward_node(client, config)
    sources = _ensure_sources(client, str(forward_node["uuid"]), config)
    destinations = _ensure_destinations(
        client, str(forward_node["uuid"]), str(project["uuid"]), config
    )

    external_rows = _load_external_ids(config)
    external_import: dict[str, Any] | None = None
    if external_rows:
        external_import = client.import_pseudonyms(external_rows)

    return {
        "profile": profile,
        "project": project,
        "auth_configs": auth_configs,
        "forward_node": forward_node,
        "source_nodes": sources,
        "destinations": destinations,
        "external_ids_import": external_import,
    }
