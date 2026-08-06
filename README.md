# karnak-api-client

Companion Python package for the Karnak management REST API
([API reference](https://github.com/nroduit/karnak/blob/master/API.md) on
upstream Karnak; available from Karnak 2.0).

Two layers:

- `karnak_api_client.client` — `KarnakClient`: thin authenticated transport
  mirroring the REST resources. Supports three auth modes:
  - `basic` — HTTP Basic on every request (default in-memory IdP enables it);
  - `form` — Spring form-login: POST `/login`, reuse the `JSESSIONID` cookie;
  - `auto` (default) — try Basic, fall back to form-login transparently when
    the server redirects to `/login` or returns 401.
- `karnak_api_client.apply` — `apply_config(client, config)`: idempotent
  desired-state application (profile, project + HMAC secret, auth configs,
  forward node, source nodes, destinations, external-ID import) from a JSON
  config document.

## Install

```sh
pip install "karnak-api-client @ git+https://github.com/jbardet/karnak-api-client.git@v0.1.0"
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

- MiCo-BID-pipeline (DAG step `00_apply_karnak_config.py`, `micobid` CLI)
- `setup_deid_gateway.py` in [jbardet/karnak](https://github.com/jbardet/karnak)
