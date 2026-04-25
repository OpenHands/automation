# Automation KV Store - Design Document

## Problem Statement

One of the use cases for the automations system is implementing integrations. Some kinds of integrations—like many webhook responders—will have a stateless implementation. They receive an event, do some work, and complete. No memory of previous runs is needed.

But other kinds of jobs require small amounts of data storage to work effectively.

For example, consider an automation that summarizes data from Slack or another source. A common pattern would be for each run of the integration to store the last timestamp of the retrieved dataset, and then on the next scheduled run, look for items since that date. This avoids reprocessing the entire history on every run and enables efficient incremental sync patterns.

**But where should the automation store this data?**

Surely a GitHub repo wouldn't be a great fit—commits for every timestamp update would pollute the history and is simply the wrong tool for the job.

We could have integration authors use custom solutions for persistence—JSONBin.io, Redis Cloud, a personal database, or some other external service. These work, but they require users to provision, configure, and manage external infrastructure.

If external systems are required for such a prevalent use case, that erodes the simplicity of having a **batteries-included** solution. The promise of the automation platform is that you can build and deploy integrations without managing infrastructure. Requiring external storage for basic state persistence breaks that promise.

## Solution

Provide a built-in **key-value store API** scoped to each automation. Automations can opt-in to persistent storage that:

- **Is easy to use** — simple GET/SET operations, familiar Redis-like semantics
- **Is flexible** — supports JSON values, counters, lists/queues, nested paths
- **Is secure** — application-level encryption, isolated per-automation (one automation cannot access another's data)

We don't need massive storage capacity or high-performance operations. An automation might run once per hour and make 5-10 KV operations. **Simplicity and security matter more than raw speed.**

## Goals

1. Provide a simple key-value store API scoped to each automation
2. Ensure strict isolation — automation A cannot access automation B's data
3. Support atomic operations for safe concurrent access (Redis-like guarantees)
4. **Application-level encryption** for all stored values — customers can trust storing sensitive data (API keys, tokens, cursors)
5. Follow OpenHands encryption conventions

## Non-Goals

**We are not building Redis.** We borrow Redis's well-designed API semantics because they're familiar and battle-tested, but we have different requirements:

| Aspect | Redis | Automation KV Store |
|--------|-------|---------------------|
| **Use case** | High-throughput cache, real-time apps | Occasional state persistence for scheduled agents |
| **Operations/sec** | Millions | Tens (at most) |
| **Storage** | In-memory | PostgreSQL (durable) |
| **Latency target** | Sub-millisecond | Hundreds of milliseconds is fine |
| **Encryption** | Optional, at-rest only | **Required, application-level** |

The overhead of JWT verification, JWE encryption, and PostgreSQL round-trips is completely acceptable for our use case. **Correctness, security, and durability matter more than raw speed.**

---

## Security Design

### The Problem with User-Level Auth

Initial idea: Use the existing `OPENHANDS_API_KEY` (user's temp API key) to authenticate KV requests, with `automation_id` in the URL path.

**Flaw**: Two automations owned by the same user could access each other's data, since both run with the same user's credentials.

### Solution: Per-Run JWT Tokens

Generate a short-lived, signed JWT token for each automation run that embeds the `automation_id` as a trusted claim.

**Flow:**
```
Dispatcher creates run
    ↓
Generate JWT: {automation_id, run_id, exp}
    ↓
Sign with service's secret key
    ↓
Pass as AUTOMATION_KV_TOKEN env var to sandbox
    ↓
Agent includes token in KV API requests
    ↓
API verifies signature, extracts automation_id from trusted claim
    ↓
All KV operations scoped to that automation_id
```

**Why JWT over per-automation secrets:**
- Tokens are time-limited (expire with the run)
- Single signing key to manage (vs N secrets for N automations)
- Stateless verification (no DB lookup to identify automation)
- Can include additional context (run_id for audit)

### Encryption at Rest (Required)

**All KV values are encrypted at the application level before storage.** This ensures:

- Database administrators cannot read sensitive values
- Database backups contain only encrypted data
- Customers can confidently store API keys, tokens, and credentials
- Compliance with security best practices

Following OpenHands conventions from the parent project:

| Component | Approach |
|-----------|----------|
| **Auth tokens** | JWS (JSON Web Signature) with HS256 |
| **KV values** | JWE (JSON Web Encryption) with A256GCM |
| **Key management** | Single master key from `AUTOMATION_JWT_SECRET` env var |
| **Libraries** | `pyjwt` + `jwcrypto` (matching OpenHands/OpenHands) |

**Pattern from OpenHands:**
```python
# encrypt_utils.py pattern
def encrypt_value(value: str) -> str:
    return jwt_service.create_jwe_token({'v': value})

def decrypt_value(encrypted: str) -> str:
    return jwt_service.decrypt_jwe_token(encrypted)['v']
```

**What's stored in the database:**
```
value_encrypted: "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0...<encrypted blob>"
```

**What the application sees after decryption:**
```json
{"api_key": "sk-secret-123", "last_cursor": "abc"}
```

---

## API Design

### Authentication

All KV endpoints require the `AUTOMATION_KV_TOKEN` in the Authorization header:
```
Authorization: Bearer <jwt_token>
```

The token contains:
```json
{
  "automation_id": "uuid",
  "run_id": "uuid", 
  "exp": 1234567890
}
```

### Base Path

```
/api/automation/v1/kv
```

Note: No `automation_id` in URL - it comes from the verified JWT claim.

---

## API Endpoints

### Overview

| Endpoint | Method | Redis Equivalent | Description |
|----------|--------|------------------|-------------|
| `/kv` | GET | `KEYS *` | List all keys |
| `/kv/{key}` | GET | `GET` | Get value |
| `/kv/{key}?path=x.y` | GET | `HGET` | Get nested field |
| `/kv/{key}?meta=true` | GET | - | Get value with metadata |
| `/kv/{key}` | PUT | `SET` | Set value |
| `/kv/{key}?nx=true` | PUT | `SET ... NX` / `SETNX` | Set if not exists |
| `/kv/{key}?xx=true` | PUT | `SET ... XX` | Set if exists |
| `/kv/{key}` | PATCH | `HSET` | Update nested path |
| `/kv/{key}` | DELETE | `DEL` | Delete key |
| `/kv/{key}/incr` | POST | `INCR` / `INCRBY` | Atomic increment |
| `/kv/{key}/decr` | POST | `DECR` / `DECRBY` | Atomic decrement |
| `/kv/{key}/lpush` | POST | `LPUSH` | Push to left of list |
| `/kv/{key}/rpush` | POST | `RPUSH` | Push to right of list |
| `/kv/{key}/lpop` | POST | `LPOP` | Pop from left |
| `/kv/{key}/rpop` | POST | `RPOP` | Pop from right |
| `/kv/{key}/len` | GET | `LLEN` | Get list length |

### Redis Alignment

The API uses familiar Redis conventions where applicable:

| Redis Flag | Query Param | Meaning |
|------------|-------------|---------|
| `NX` | `?nx=true` | Only set if key does **not** exist |
| `XX` | `?xx=true` | Only set if key **does** exist |
| `EX` | `?ex=3600` | Set TTL in seconds (future) |

List operation names (`lpush`, `rpush`, `lpop`, `rpop`) match Redis exactly:
- `l` = left (front of list)
- `r` = right (back of list)
- `push` = add item
- `pop` = remove and return item

**Queue patterns:**
- FIFO queue: `rpush` to enqueue, `lpop` to dequeue
- LIFO stack: `rpush` to push, `rpop` to pop

---

## Request/Response Format

All responses are JSON objects for consistency and extensibility.

### List Keys

**Request:**
```http
GET /kv
```

**Response:**
```json
{
  "keys": ["config", "counter", "task-queue"],
  "count": 3
}
```

### Get Value

**Request:**
```http
GET /kv/config
```

**Response:**
```json
{
  "key": "config",
  "value": {
    "database": {"host": "localhost", "port": 5432},
    "retries": 3
  }
}
```

### Get Nested Path

**Request:**
```http
GET /kv/config?path=database.host
```

**Response:**
```json
{
  "key": "config",
  "path": "database.host",
  "value": "localhost"
}
```

### Get with Metadata

**Request:**
```http
GET /kv/config?meta=true
```

**Response:**
```json
{
  "key": "config",
  "value": {
    "database": {"host": "localhost", "port": 5432},
    "retries": 3
  },
  "created_at": "2024-01-15T10:00:00Z",
  "updated_at": "2024-01-15T12:30:00Z"
}
```

### Set Value

**Request:**
```http
PUT /kv/config
Content-Type: application/json

{
  "database": {"host": "localhost", "port": 5432},
  "retries": 3
}
```

**Response:**
```json
{
  "key": "config",
  "value": {
    "database": {"host": "localhost", "port": 5432},
    "retries": 3
  },
  "created": true,
  "updated_at": "2024-01-15T12:30:00Z"
}
```

### Set If Not Exists (SETNX)

**Request:**
```http
PUT /kv/lock?nx=true
Content-Type: application/json

{"owner": "run-123", "acquired_at": "2024-01-15T12:30:00Z"}
```

**Response (success - key was created):**
```json
{
  "key": "lock",
  "value": {"owner": "run-123", "acquired_at": "2024-01-15T12:30:00Z"},
  "created": true,
  "updated_at": "2024-01-15T12:30:00Z"
}
```

**Response (failure - key already exists):**
```json
{
  "key": "lock",
  "created": false,
  "error": "key_exists"
}
```
HTTP Status: `409 Conflict`

### Update Nested Path

**Request:**
```http
PATCH /kv/config
Content-Type: application/json

{
  "path": "database.port",
  "value": 5433
}
```

**Response:**
```json
{
  "key": "config",
  "path": "database.port",
  "value": 5433,
  "updated_at": "2024-01-15T12:35:00Z"
}
```

### Delete Key

**Request:**
```http
DELETE /kv/config
```

**Response:**
```json
{
  "key": "config",
  "deleted": true
}
```

### Increment

**Request:**
```http
POST /kv/counter/incr
Content-Type: application/json

{"by": 1}
```

Note: `by` defaults to 1 if not provided.

**Response:**
```json
{
  "key": "counter",
  "value": 43
}
```

### Decrement

**Request:**
```http
POST /kv/counter/decr
Content-Type: application/json

{"by": 5}
```

**Response:**
```json
{
  "key": "counter",
  "value": 38
}
```

### Push to List (Left)

**Request:**
```http
POST /kv/task-queue/lpush
Content-Type: application/json

{"value": {"task_id": "abc123", "action": "process"}}
```

**Response:**
```json
{
  "key": "task-queue",
  "length": 5
}
```

### Push to List (Right)

**Request:**
```http
POST /kv/task-queue/rpush
Content-Type: application/json

{"value": {"task_id": "def456", "action": "notify"}}
```

**Response:**
```json
{
  "key": "task-queue",
  "length": 6
}
```

### Pop from List (Left)

**Request:**
```http
POST /kv/task-queue/lpop
```

**Response (item returned):**
```json
{
  "key": "task-queue",
  "value": {"task_id": "abc123", "action": "process"}
}
```

**Response (list empty):**
```json
{
  "key": "task-queue",
  "value": null
}
```

### Pop from List (Right)

**Request:**
```http
POST /kv/task-queue/rpop
```

**Response:**
```json
{
  "key": "task-queue",
  "value": {"task_id": "def456", "action": "notify"}
}
```

### Get List Length

**Request:**
```http
GET /kv/task-queue/len
```

**Response:**
```json
{
  "key": "task-queue",
  "length": 42
}
```

---

## Error Responses

All errors return JSON with consistent structure:

```json
{
  "error": "error_code",
  "message": "Human-readable description"
}
```

| HTTP Status | Error Code | Description |
|-------------|------------|-------------|
| 400 | `invalid_request` | Malformed request body |
| 400 | `invalid_path` | Invalid JSON path syntax |
| 400 | `type_mismatch` | Operation doesn't match value type (e.g., incr on object) |
| 401 | `unauthorized` | Missing or invalid token |
| 403 | `token_expired` | JWT token has expired |
| 404 | `key_not_found` | Key does not exist |
| 409 | `key_exists` | Key already exists (for `?nx=true`) |
| 409 | `key_not_exists` | Key doesn't exist (for `?xx=true`) |

---

## Why Atomic Operations Matter

**Scenario:** Two runs of the same automation overlap (previous run slow, next scheduled run starts):

Without atomics:
```
Run A: GET counter → 5
Run B: GET counter → 5
Run A: PUT counter → 6
Run B: PUT counter → 6  # Lost update!
```

With INCR:
```
Run A: INCR counter → 6
Run B: INCR counter → 7  # Correct!
```

---

## Implementation Notes

### Single-Document Storage Design

Each automation has exactly **ONE row** in the database containing its entire state as an encrypted JSON document. API "keys" (e.g., `/kv/config`, `/kv/counter`) are top-level fields within this single document.

**Why single-document?**
- **Eliminates deadlocks**: Only one row per automation to lock. All operations serialize through that single lock. No possibility of lock ordering issues.
- **Simpler model**: One encryption boundary, one row to manage per automation.
- **Acceptable trade-off**: Every operation reads/writes the entire state blob, but automation state is small and access is infrequent.

```
┌─────────────────────────────────────────────────────────────┐
│ Database Row (ONE per automation)                           │
├─────────────────────────────────────────────────────────────┤
│ automation_id: uuid-123 (UNIQUE)                            │
│ state_encrypted: <encrypted JSON blob>                      │
│                                                             │
│   Decrypted contents:                                       │
│   {                                                         │
│     "config": {"host": "localhost", "port": 5432},          │
│     "counter": 42,                                          │
│     "task-queue": [{"task_id": "abc"}]                      │
│   }                                                         │
└─────────────────────────────────────────────────────────────┘
```

### Atomic Operations with Encryption

Since values are encrypted at the application level, we **cannot** use native PostgreSQL operations like `value = value + 1`. Instead, atomic operations lock the single state row and perform read-modify-write:

```python
async def incr(self, automation_id: UUID, key: str, by: int = 1) -> int:
    async with session.begin():
        # 1. Lock the automation's state row (ONE row per automation)
        row = await session.execute(
            select(AutomationKV)
            .where(AutomationKV.automation_id == automation_id)
            .with_for_update()
        )
        kv = row.scalar_one_or_none()
        
        # 2. Decrypt entire state, modify target key, encrypt
        if kv is None:
            state = {key: by}
            kv = AutomationKV(automation_id=automation_id)
            session.add(kv)
        else:
            state = decrypt_value(kv.state_encrypted)
            if key not in state:
                state[key] = by
            else:
                value = state[key]
                if not isinstance(value, int):
                    raise TypeError("Cannot increment non-integer value")
                state[key] = value + by
        
        # 3. Update with encrypted state
        kv.state_encrypted = encrypt_value(state)
        
        # 4. Commit releases lock
        return state[key]
```

**Concurrency model:**
- Each automation has ONE row → all operations serialize through one lock
- No deadlock risk between keys (there's only one lock to acquire)
- Different automations → completely isolated (different rows)

This is acceptable for our use case (automations doing 5-10 KV ops per run). The brief lock during decrypt-modify-encrypt is negligible.

### SETNX (Set If Not Exists)

For conditional set operations, we lock the state row, check if the key exists in the decrypted state, and proceed accordingly:

```python
# Lock state row
state = decrypt_value(kv.state_encrypted) if kv else {}

if nx and key in state:
    return 409  # Key already exists

state[key] = value
kv.state_encrypted = encrypt_value(state)
```

### Path Syntax

Use dot notation for nested paths: `database.host`

For keys containing dots, use bracket notation: `config["my.key.with.dots"]`

---

## Data Model

```python
class AutomationKV(Base):
    """Single-document state store for automation persistence.
    
    Each automation has exactly ONE row containing its entire state as an
    encrypted JSON document. The API presents a key-value interface, but
    "keys" are top-level fields within this single document.
    """
    __tablename__ = "automation_kv"
    
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    automation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, 
        ForeignKey("automations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # ONE row per automation
    )
    
    # Encrypted JSON document containing all KV pairs
    # Decrypted example: {"config": {...}, "counter": 42, "queue": [...]}
    state_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    
    # Timestamps (foundation for future TTL support)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=utcnow,
        nullable=False,
    )
    
    __table_args__ = (
        Index("ix_automation_kv_automation_id", "automation_id", unique=True),
    )
```

### Future TTL Support

The `created_at` and `updated_at` timestamps provide the foundation for TTL:

```python
# Future addition for TTL
expires_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), 
    nullable=True,
    index=True,  # For efficient cleanup queries
)
```

TTL can be set via `?ex=3600` query param:
```http
PUT /kv/session?ex=3600
```

---

## Dependencies to Add

```toml
# pyproject.toml
dependencies = [
    # ... existing ...
    "pyjwt>=2.8",
    "jwcrypto>=1.5.6",
]
```

---

## Environment Variables

```bash
# Required: Master key for JWT signing and JWE encryption
AUTOMATION_JWT_SECRET=<random-secret-string>
```

---

## Agent Integration

Agents need a simple way to interact with the KV store. We provide a client library and tools.

### Package Structure

**Recommended: Separate `openhands-kv` package** hosted in its own repo under the OpenHands org.

```
openhands-kv/
├── openhands/
│   └── kv/
│       ├── __init__.py
│       ├── client.py      ← KVClient class (HTTP wrapper)
│       └── tool.py        ← KVStoreTool definition
├── pyproject.toml
└── README.md
```

This package is installed in the sandbox via `setup.sh`:

```bash
# presets/prompt/setup.sh
pip install -q --no-cache-dir \
  "openhands-sdk==${SDK_VERSION}" \
  "openhands-workspace==${SDK_VERSION}" \
  "openhands-tools==${SDK_VERSION}" \
  "openhands-kv==0.1.0"
```

**Why a separate package?**
- Independent release cycle from SDK and automation service
- Clean separation of concerns
- Can be used outside automations if needed
- No changes required to agent-sdk or automation build systems

> **Future consideration:** If automation grows more packages, consider converting
> the automation repo to a monorepo structure (like agent-sdk) to co-locate
> related packages while maintaining independent releases.

### Client Library

```python
from openhands.kv import KVClient

# Auto-reads AUTOMATION_KV_TOKEN and OPENHANDS_CLOUD_API_URL from environment
kv = KVClient()

# Basic operations
config = kv.get("config")
kv.set("config", {"database": {"host": "localhost"}})
kv.delete("old-key")

# Counters
kv.set("counter", 0)
new_value = kv.incr("counter")  # Returns 1
kv.incr("counter", by=5)        # Returns 6

# Nested paths
host = kv.get("config", path="database.host")
kv.patch("config", path="database.port", value=5433)

# Lists/Queues
kv.rpush("task-queue", {"task_id": "abc", "action": "process"})
kv.rpush("task-queue", {"task_id": "def", "action": "notify"})
task = kv.lpop("task-queue")  # FIFO dequeue
length = kv.len("task-queue")

# Conditional set (for locks, idempotency)
created = kv.set("lock", {"owner": "run-123"}, nx=True)
if not created:
    print("Lock already held by another run")
```

### Agent Tool

The tool is conditionally loaded in the preset's `sdk_main.py`:

```python
# In presets/prompt/sdk_main.py

if os.environ.get("AUTOMATION_ENABLE_KV_STORE") == "true":
    from openhands.kv import KVStoreTool
    # Register tool with agent
```

### Environment Variables

The dispatcher passes these env vars when KV is enabled:

| Env Var | Purpose |
|---------|---------|
| `AUTOMATION_ENABLE_KV_STORE` | Feature flag (`"true"` to enable) |
| `AUTOMATION_KV_TOKEN` | JWT token scoped to this automation |

### Environment Detection

The library auto-detects when running in an automation context:

```python
class KVClient:
    def __init__(self, token: str | None = None, base_url: str | None = None):
        self.token = token or os.environ.get("AUTOMATION_KV_TOKEN")
        self.base_url = base_url or os.environ.get("OPENHANDS_CLOUD_API_URL")
        
        if not self.token:
            raise KVNotAvailableError(
                "KV store is only available within automation runs. "
                "AUTOMATION_KV_TOKEN environment variable not found."
            )
```

This gives a clear error if someone tries to use KV outside an automation context.

---

## Open Questions / Limits

| Topic | Question | Suggested Default |
|-------|----------|-------------------|
| **Key length** | Max characters for key names? | 255 characters |
| **State size** | Max total state size per automation? | 1 MB (encrypted) |
| **Retention** | What happens when automation is deleted? | Cascade delete all KV data |
| **TTL** | Support key expiration? | Deferred (timestamps in place for future) |

These limits are generous for the intended use case (state persistence between automation runs). They can be adjusted based on usage patterns.

---

## Next Steps

### Design (Complete)
1. [x] Decide on MVP API scope - CRUD + counters + lists + paths
2. [x] Decide on value types - Any JSON value
3. [x] Define response format - Consistent JSON objects
4. [x] Define agent integration approach - Separate `openhands-kv` package
5. [x] Define security model - Per-run JWT tokens + JWE encryption

### Implementation (TODO)

**Automation Service (this repo):**
1. [ ] Add `enable_kv_store` field to Automation model
2. [ ] Update schemas for create/update requests
3. [ ] Implement JwtService (port from OpenHands)
4. [ ] Implement encrypt_utils.py
5. [ ] Create database migration for `automation_kv` table
6. [ ] Implement KV API router (`/api/automation/v1/kv/...`)
7. [ ] Update dispatcher to generate and pass `AUTOMATION_KV_TOKEN`
8. [ ] Update preset `sdk_main.py` to conditionally load KV tool
9. [ ] Update preset `setup.sh` to install `openhands-kv`
10. [ ] Frontend: Add KV toggle to automation create/edit form

**New `openhands-kv` Package (new repo):**
1. [ ] Create repo under OpenHands org
2. [ ] Implement `KVClient` (HTTP client library)
3. [ ] Implement `KVStoreTool` (agent tool definition)
4. [ ] Publish to PyPI
5. [ ] Documentation

### Testing
1. [ ] Unit tests for KV API endpoints
2. [ ] Unit tests for JWT/encryption
3. [ ] Integration tests for full flow (automation → sandbox → KV API)
4. [ ] Test atomic operations (concurrent INCR, etc.)

---

## References

- [JSONBin.io API](https://jsonbin.io/api-reference)
- [Redis Commands](https://redis.io/commands/)
- OpenHands encryption: `OpenHands/OpenHands/enterprise/storage/encrypt_utils.py`
- OpenHands JWT service: `OpenHands/OpenHands/openhands/app_server/services/jwt_service.py`
