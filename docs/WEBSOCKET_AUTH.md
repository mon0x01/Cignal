# WebSocket event authorization

Cignal's authenticated `/api/ws/incidents` stream carries two different classes of events and must not treat them as one broadcast permission.

| Event class | Admin | Analyst | Responder | Citizen |
|---|---:|---:|---:|---:|
| `incident.*` | Yes | Yes | Yes | No |
| `ingestion.completed` | Yes | Yes | Yes | No |
| `notification.*` | Only own | Only own | Only own | Only own |
| Unknown/malformed | No | No | No | No |

The event policy lives in `app/services/websocket_auth.py`. It fails closed for unknown/malformed messages. WebSocket authentication continues to load the active user from the database and enforce MFA state; the JWT role claim is not used as the authorization source.

If Redis fails after a WebSocket has been accepted, the API closes that connection with code `1011` instead of leaving a half-open connection.

## Automated tests

`tests/test_websocket_auth.py` covers the role matrix, recipient isolation, malformed-event rejection, and the actual FastAPI WebSocket endpoint with a deterministic fake Redis stream. These tests do not contact Redis or external services.

## Full local verification

From the project root, with the six standard Compose services running:

```bash
sudo bash scripts/verify.sh
```

The script validates Compose configuration, service availability, Python compilation, pytest, Ruff, high-severity Bandit findings, current Alembic revision, API health, and frontend HTTP status. Tests run inside the API container using the disposable SQLite test database configured by `tests/conftest.py`; they do not write to the live PostgreSQL volume.
