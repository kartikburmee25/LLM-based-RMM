# RMM-style control plane + Windows agent

A minimal Remote Monitoring and Management system: a FastAPI control plane
and a C# Windows agent that runs PowerShell jobs dispatched to it. Built to
the spec's five capabilities (enrollment, device listing, script execution,
execution status, auth/audit) and nothing beyond them -- no dashboard, no
approval flow, no chat UI, no patching/AV/remote desktop.

```
control-plane/    FastAPI + PostgreSQL + WebSockets
windows-agent/     C# (.NET 8) Windows Service + installer scripts
docker-compose.yml  runs the control plane + Postgres
```

## The main design decision: NAT and low-latency dispatch

The device sits behind NAT, so the server can never open a connection to
it -- the agent has to initiate contact. Three shapes were on the table:

- **Short polling** ("any jobs for me?" every few seconds): NAT-friendly and
  trivial to build, but latency is bounded by the poll interval, and a
  useful interval (a few seconds) means constant load for mostly-empty
  responses.
- **Long polling**: better latency, but every request needs the server to
  hold a connection open and correctly resume the next request the moment
  one closes -- more moving parts for a "keep it simple" project.
- **A single outbound, persistent connection per device** (WebSocket): the
  agent dials out once, NAT and firewalls treat it like any outbound HTTPS
  connection, and it stays open. The server can then push a job the instant
  it's created -- no polling delay at all -- and the same channel carries
  the result back.

This project uses the third option. The agent connects to `wss://.../ws/agent`
with its agent token in the `Authorization` header and holds that socket
open for as long as it runs, reconnecting with backoff if it drops. The
control plane keeps an in-memory map of `device_id -> open socket`
(`app/connection_manager.py`); when a job is created for a device that's
connected, it's written straight to that socket. When the device isn't
connected, the job is left `pending` and gets flushed to it the moment it
reconnects.

Liveness/"reachable right now" rides on the same connection: the agent sets
`ClientWebSocket.KeepAliveInterval`, so .NET automatically sends and answers
WebSocket protocol-level pings without any app-level heartbeat message. A
device is "reachable" exactly when the server has an open socket for it.

**Known limitation of this tradeoff**, stated plainly rather than hidden:
the connection map is in-process memory, so this design assumes a single
control-plane instance. Running more than one instance would need the
socket map moved to something shared (e.g. Redis pub/sub, so any instance
can hand a job to whichever instance actually holds that device's socket).
Out of scope here on purpose -- the spec asked to keep this simple, and a
single instance is a reasonable starting point for an MSP-scale deployment.

## Enrollment: no standing access from the installer alone

The installer carries an **enrollment token**: single-use, expiring, and
tied to whichever API key created it. `POST /enroll` consumes it exactly
once (a second attempt gets `409 Conflict`) and, in exchange, issues the
device its own brand-new **agent token** -- a completely separate
credential that only the enrolled device ever sees. So the enrollment token
never doubles as a standing credential, and a copy of the installer package
sitting around (which is exactly "whatever we hand the installer") can't be
used to enroll a machine of someone else's choosing after it's been used
once, or after it expires.

**Stable ID across reinstall**: at enroll time the agent reads the Windows
`MachineGuid` (`HKLM\SOFTWARE\Microsoft\Cryptography`), which is generated
once per Windows installation and survives an application
uninstall/reinstall. The server hashes it and uses it to recognize "this is
the same machine enrolling again" (`crud.create_or_reuse_device`): the
existing `device_id` is kept and only the agent token is rotated, rather
than a new device being created. A full OS reimage will still produce a new
device, which is the correct behavior -- it *is* a different install.

## Script execution: idempotent, bounded, non-blocking

`POST /devices/{id}/jobs` accepts raw PowerShell in the body and returns a
job handle immediately (`201`, `status: pending` or `dispatched`) -- it
never blocks waiting for the script to finish. An optional
`Idempotency-Key` header makes retries safe: `(device_id, idempotency_key)`
is a unique constraint, so a duplicated or retried POST returns the
existing job instead of dispatching the script a second time.

Output is bounded on both ends independently (agent caps at 64K characters
per stream while capturing; the server caps again on the way into the
database), and each stream carries its own `truncated` flag so the caller
always knows when it was cut short rather than silently losing data.

## Execution status: what "how it ended" means

`GET /jobs/{id}` always returns one of six statuses, matching the two ways
a job can go wrong along with the normal outcomes:

| Status | Meaning |
|---|---|
| `pending` | created, device not connected yet |
| `dispatched` | sent to the agent, no result yet |
| `succeeded` | agent reported `exit_code == 0` |
| `failed` | agent reported a non-zero `exit_code` |
| `timed_out` | dispatched, but no result in time -- the script ran too long (agent self-enforces the timeout and reports this explicitly; a background sweeper is the backstop for cases where the agent never reports back at all) |
| `device_unreachable` | never dispatched -- the device was offline for the entire timeout window |

Results are structured (`exit_code`, `stdout`, `stderr`, `duration_ms`,
plus the two truncation flags), never a blob of text meant for a human to
parse.

## Auth and audit

- **Callers** (technicians / MSP tooling) authenticate with a static API key
  (`Authorization: Bearer <key>`), created via `scripts/create_api_key.py`
  or `POST /bootstrap/api-keys`.
- **Agents** authenticate with their own per-device token, presented only
  during the WebSocket handshake.
- Every job is a permanent row in `jobs`, including the exact script
  content submitted and who submitted it (`created_by_api_key_id`). Nothing
  about a job is ever deleted -- that table *is* the audit trail.

## Data model

```
api_keys            caller credentials
enrollment_tokens    single-use installer credentials, linked to the api_key that minted them
devices              stable id, hardware fingerprint hash, current agent_token hash
jobs                 one row per script execution: request + result + timestamps
```

See `control-plane/app/models.py` for exact columns, and
`control-plane/app/schemas.py` for the WebSocket wire protocol (documented
inline as comments -- two message types: `job` and `job_result`).

## Running the control plane

```bash
cd control-plane   # or repo root, either works with docker compose
docker compose up --build -d          # from the repo root
docker compose exec api python scripts/create_api_key.py "my technician"
# -> prints a raw API key, save it

curl -X POST http://localhost:8000/enrollment-tokens \
  -H "Authorization: Bearer <api key>"
# -> {"token": "enr_...", "expires_at": "..."}
```

## Building and installing the Windows agent

```powershell
cd windows-agent\RmmAgent
dotnet publish -c Release -r win-x64 --self-contained -o ..\publish

cd ..
.\install.ps1 -ServerUrl "https://rmm.example.com" -EnrollmentToken "enr_..."
# registers + starts the "RmmAgent" Windows Service (LocalSystem, auto-restart on crash)

# later, to remove cleanly:
.\uninstall.ps1
```

`install.ps1` takes everything as parameters specifically so an MSP's
deployment tooling can run it silently across a fleet -- no wizard, no
prompts. Enrollment happens on the agent's first run (it reads the
one-time bootstrap file `install.ps1` drops, enrolls, saves its own
credential, and deletes the bootstrap), so "install" and "enroll" are one
step from the operator's point of view, as the spec asked for.

## Calling the API end to end

```bash
API=http://localhost:8000
KEY="<api key>"

curl $API/devices -H "Authorization: Bearer $KEY"

curl -X POST $API/devices/<device_id>/jobs \
  -H "Authorization: Bearer $KEY" \
  -H "Idempotency-Key: <any client-generated uuid>" \
  -H "Content-Type: application/json" \
  -d '{"script": "Get-Process | Select-Object -First 5", "timeout_seconds": 60}'
# -> {"id": "<job_id>", "status": "dispatched", ...}

curl $API/jobs/<job_id> -H "Authorization: Bearer $KEY"
```

## What's deliberately not here

Per the spec: no technician dashboard/UI, no human-in-the-loop approval
flow, no chat interface, no patch management, no AV, no remote desktop.
Also skipped as unnecessary complexity for a project this size: Alembic
migrations (the app creates tables with `create_all` on startup -- fine
until the schema needs a real migration), a message broker for multi-
instance dispatch (see the NAT section above), and a WiX/MSI installer
(the PowerShell install/uninstall scripts satisfy "one installer that runs
unattended" without the extra build tooling an MSI needs).
