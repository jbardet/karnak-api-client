# karnak-api-client

Companion Python package for the Karnak management REST API, **v2.0.0**
(the redesigned management API shipped in Karnak 2.0 — resources are keyed
by UUID and returned as DTOs). The authoritative contract is the v2.0.0
OpenAPI spec (`api-collection/karnak-api.openapi.yaml` in the Karnak fork
repo), plus two fork-only extensions not in that spec: `/api/auth-configs`
(re-added after upstream removed it) and `/api/pseudonyms/import` (bulk
pseudonym import).

Two layers:

- `karnak_api_client.client` — `KarnakClient`: thin authenticated transport
  mirroring the REST resources. Supports three auth modes:
  - `basic` — HTTP Basic on every request (default in-memory IdP enables it);
  - `form` — Spring form-login: POST `/login`, reuse the `JSESSIONID` cookie;
  - `auto` (default) — try Basic, fall back to form-login transparently when
    the server redirects to `/login` or returns 401.

  v2.0.0 has a dedicated stateless `/api/**` filter chain (CSRF disabled, no
  session): HTTP Basic under `IDP=inmemory`, Bearer JWT under `IDP=oidc`.
  Form login is a fallback the client reaches for only if Basic is rejected —
  it is never required up front.

- `karnak_api_client.apply` — `apply_config(client, config)`: idempotent
  desired-state application (profile, project + HMAC secret + profile
  binding, auth configs, forward node, source nodes, destinations,
  pseudonym import) from a JSON config document.

## What changed from the pre-2.0.0 API (this package's own v0.2.x)

- Every resource id is now a UUID string (`profileUuid`, `projectUuid`,
  `forwardNodeUuid`, `destinationUuid`, `sourceNodeUuid`, `secretUuid`), not
  an integer.
- `upload_profile()` now posts to `/api/profiles/import`; bare
  `POST /api/profiles` is a JSON create (`create_profile`).
- A project's linked profile (`profileUuid`) is a first-class, settable
  field — `apply_config` now actively binds/rebinds it instead of leaving it
  unset (the old API couldn't express this link over REST at all).
- Secrets are a sub-resource: `list_secrets`, `add_project_secret` (generate
  or import a hex key), `delete_secret`.
- External IDs became `/api/pseudonyms` (not project-scoped for listing):
  `list_pseudonyms`, `create_pseudonym` (409 = idempotent success),
  `import_pseudonyms`, `delete_project_pseudonyms`.
- `query_transfers` returns a Spring Data `Page` object; `count_transfers`
  returns a bare `int`.
- `update_project` now always sends the full model (`name` + `profileUuid`
  are both required server-side); use `touch_project()` to re-PUT a project
  unchanged (this is what makes Karnak republish its destination config).

## Install

```sh
pip install "karnak-api-client @ git+https://github.com/jbardet/karnak-api-client.git@v0.3.0"
```

## Usage

```python
from karnak_api_client import KarnakClient, KarnakClientConfig

client = KarnakClient(KarnakClientConfig(
    base_url="http://localhost:8081", username="admin", password="karnak",
))
profiles = client.list_profiles()
```

Or configure from the environment (`KARNAK_API_BASE`, `KARNAK_USERNAME`,
`KARNAK_PASSWORD`, `KARNAK_AUTH_MODE`, `KARNAK_API_TIMEOUT_SEC`):

```python
client = KarnakClient()  # KarnakClientConfig.from_env()
```

## Versioning

Semver git tags on this repository (`v0.1.0`, …). Release independently of
Karnak core; bump when REST controllers or apply semantics change.

## Consumers

- MiCo-BID-pipeline (DAG step `00_apply_karnak_config.py`, `micobid` CLI) —
  needs updating for the `id` → `uuid` summary-key rename in `apply_config`'s
  return value (see the WP-B handoff report).
- `setup_deid_gateway.py` in [jbardet/karnak](https://github.com/jbardet/karnak)
