# Setting up direct Epic sync

This guide takes you from nothing to `mychart-mcp sync` downloading your own chart from an
Epic health system: registering an app on Epic's developer site, testing against Epic's
sandbox, going to production, and connecting to your health system. It also covers how the
code works and what tends to go wrong.

Plan on about an hour of hands-on time, plus up to a day of waiting while Epic delivers your
app to health systems.

## How it works

```
fhir.epic.com app registration ──client ID──┐
                                            ▼
mychart-mcp sync ──browser──▶ MyChart sign-in (your health system)
       ▲                            │ redirect with one-time code
       └── https://127.0.0.1:8765 ◀─┘
       │ code + PKCE verifier ──▶ token endpoint ──▶ access token (memory only)
       │ read-only FHIR searches ──▶ Patient, Condition, Observation, notes, …
       ▼
local SQLite database  ◀── read-only ── mychart-mcp serve ── AI client
```

- It's a **SMART on FHIR standalone patient launch** from a **public client** using
  **PKCE**: the app has no client secret, and a stolen authorization code is useless without
  the verifier that never leaves your machine.
- The **redirect** goes to a one-shot listener on `127.0.0.1`. It accepts only the redirect
  with the expected `state` and ignores anything else.
- The **access token** is held in memory for one run and is never written anywhere. The
  HTTP client refuses to send it to any host other than the FHIR server you signed in to.
- **Everything is downloaded before anything is written.** If a run fails, your database
  is untouched. A successful run updates that health system's previously synced rows.
- `src/mychart_mcp/epic.py` is the **only module that makes network calls**. The MCP server
  (`server.py`) never imports it.

## 1. Register an app on fhir.epic.com

Each person running this should register their **own** app. Patients see the app's name
and the developer's name on the MyChart consent screen, and the developer account is
responsible for the app under Epic's terms.

Create a free account at [fhir.epic.com](https://fhir.epic.com), then go to **Build Apps →
Create**. The form grows as you fill it in. Use these settings:

| Field | Value | Why |
|---|---|---|
| Application Name | anything recognizable | Shown on the MyChart consent screen |
| Application Audience | **Patients** | Standalone patient launch |
| Automatic Client Distribution | **USCDI v3** | Health systems receive the app automatically. With **None**, each one would have to approve it manually. |
| Public Documentation URL | your repo or docs page | Must be reachable (a private repo won't do) |
| Endpoint URI (redirect) | `http://127.0.0.1:8765/callback` **and** `https://127.0.0.1:8765/callback` | http for sandbox testing; production requires https. Pick the scheme in the dropdown and type only `127.0.0.1:8765/callback`. |
| Can Register Dynamic Clients | **checked** | Not used yet; keeps the door open for refresh tokens. Can't be changed after production. |
| Is Confidential Client | unchecked | A desktop CLI can't keep a secret, so it uses PKCE instead |
| SMART on FHIR Version | **R4** | |
| SMART Scope Version | **SMART v1** | What `sync` requests (`patient/*.read`); the most widely supported |
| FHIR ID Generation Scheme | default (Unconstrained) | |

### Incoming APIs

Use the search box and move over only **(R4)** entries. Leave out anything marked
Create, Update, DSTU2 or STU3, and billing or admin APIs such as Account, Coverage,
ExplanationOfBenefit and scheduling. Those can stop the app qualifying for automatic
distribution.

Take both the **(Patient Chart)** and **(Outside Record)** versions where both exist.
Outside Record covers data your health system received from other organizations (Care
Everywhere).

| Resource | APIs |
|---|---|
| Patient | `Patient.Read (Demographics)` |
| Condition | `Condition.Search`: Problems, Encounter Diagnosis, Health Concerns, Care Plan Problem, and their Outside Record versions |
| Observation | `Observation.Search`: Labs, Vital Signs, Social History, plus Outside Record versions |
| DiagnosticReport | `.Read` and `.Search` |
| DocumentReference | `DocumentReference.Search (Clinical Notes)` (+ Outside Record) |
| Binary | `Binary.Read (Clinical Notes)`. **Required for note text.** |
| Encounter | `.Read` and `.Search` |
| MedicationRequest, Medication | `MedicationRequest.Search`, `Medication.Read` |
| AllergyIntolerance, Immunization, Procedure | `.Search` |
| Practitioner, Organization, Location | `.Read` only |

Anything you leave out simply shows up under **Skipped** when you sync, so a missing API
is easy to spot.

Save. The app page now shows a **Non-Production Client ID** (sandbox) and a **Client ID**
(production). Client IDs aren't secret for a public client, but keep them out of the repo.
Pass them with `--client-id` or `MYCHART_MCP_CLIENT_ID`.

## 2. Test against Epic's sandbox

The sandbox works as soon as the app is saved. Point the sync at a **scratch database**
(`--sandbox` refuses to run without one, so test patients can't mix with real records):

```sh
MYCHART_MCP_DB=/tmp/sandbox.db uv run mychart-mcp sync --sandbox --client-id <non-production id>
```

A sandbox MyChart page opens. **Your real MyChart login won't work there.** Use one of the
test-patient MyChart logins listed in fhir.epic.com's documentation (look for the test data
or sandbox test patients page). Approve the consent screen and the browser shows "Signed in".
The terminal prints one line per search, then a summary. A few hundred records with nothing
under **Skipped** means the app registration and the code agree.

## 3. Mark the app ready for production

On the app's page, the remaining fields must be filled in before **Save & Ready for
Production** is enabled. **After this the app can't be edited**, so get everything right
first. Use **Save & Ready for Sandbox** to save without locking, and re-run step 2 after any
change.

- **Summary / Description:** what the app does and that data stays on the user's device.
- **Intended Purposes:** Individuals' Access to their EHI. **Intended Users:**
  Individual/Caregiver.
- **Terms and Conditions Secure URL:** must be a public https page, **without a `#`
  fragment** (the form rejects it). This repo uses [`TERMS.md`](../TERMS.md).
- **Data Use Questionnaire:** answer truthfully. For this design: stored only on the
  user's device, no developer servers, never sold or shared, deletable at any time, and data
  goes to an AI provider only if the user connects a hosted assistant.
- Accept the open.epic terms of use, then **Save & Ready for Production**.

Epic then delivers the production client ID to every health system that qualifies. **This
can take up to about a day.** Until then, your health system shows a generic "OAuth2 Error:
Something went wrong trying to authorize the client".

## 4. Find your health system's FHIR endpoint

Epic publishes every organization's endpoints:

- Searchable list: <https://open.epic.com/MyApps/Endpoints>
- Raw JSON: <https://open.epic.com/Endpoints/R4> (the URL is in each entry's `address`)

Search for your health system's name. The listed name is sometimes the parent system, not
the clinic you know, and the endpoint's domain often matches your MyChart website's. Copy
the **R4** base URL, usually ending in `/api/FHIR/R4`. `sync` discovers the sign-in and
token endpoints from `<base>/.well-known/smart-configuration`.

## 5. Create a certificate for the https redirect

Production redirects must be https, so the local listener needs a certificate the browser
trusts. [mkcert](https://github.com/FiloSottile/mkcert) creates one signed by a local
certificate authority (CA):

```sh
brew install mkcert
cd ~/path/to/mychart-mcp
mkcert -install      # trusts mkcert's local CA; shows a macOS approval prompt
mkcert 127.0.0.1     # writes 127.0.0.1.pem and 127.0.0.1-key.pem (git-ignored)
```

- Run `mkcert -install` **in a terminal on the Mac's own screen**, not over SSH or in a tmux
  session that was started over SSH. Otherwise macOS can't show the approval prompt and
  fails with "The authorization was denied since no user interaction was possible."
- The CA has to be trusted by **the machine whose browser does the sign-in**.
- Firefox keeps its own trust store: `brew install nss` before `mkcert -install`.

## 6. Sync your chart

```sh
uv run mychart-mcp sync \
    --fhir-base <your R4 base URL> \
    --client-id <production client id> \
    --redirect-uri https://127.0.0.1:8765/callback \
    --cert 127.0.0.1.pem --key 127.0.0.1-key.pem
```

Sign in with your real MyChart account. A large chart (thousands of notes) takes several
minutes, because each note's text is a separate request. Progress prints per search. To
avoid repeating the flags, set `MYCHART_MCP_FHIR_BASE`, `MYCHART_MCP_CLIENT_ID`,
`MYCHART_MCP_REDIRECT_URI`, `MYCHART_MCP_CERT` and `MYCHART_MCP_KEY`.

Run it in your own terminal, not through an AI assistant's shell tool, so your records'
contents stay out of the assistant's context. The summary contains counts only.

Every run opens the browser. The first run downloads everything; later ones download only
what changed (next section).

## 7. Keeping it current

Re-run step 6 when you want fresh records, for example after a visit or when new results
post. A recurring reminder works well (Reminders or your calendar, monthly or after
appointments).

Re-runs are **incremental**: the output starts with "Downloading changes since <date>".

- **What's fetched incrementally:** Observation (labs, vitals), DiagnosticReport,
  DocumentReference (notes) and Encounter are searched with `_lastUpdated=ge<last sync>`,
  so only new or changed records come back, **if your health system's server supports it**.
  Servers silently ignore search parameters they don't support, so each incremental sync
  first reads the server's CapabilityStatement (`metadata`) and filters only the types it
  lists `_lastupdated` for; the rest are refreshed in full. The first line of output names
  them. Epic's sandbox (August 2026 release) supports all four; a May 2026 release
  supports only DocumentReference. Epic leaves `meta.lastUpdated` off vitals and
  clinical notes, so those return every time. For each note, the sync compares it with the
  stored copy and reuses the stored text when nothing changed. Note text is where most of a
  full sync's time goes, since each note is a separate request. A full sync always refetches
  it.
- **What's always refetched in full:** Condition, MedicationRequest, AllergyIntolerance,
  Immunization and Procedure. Epic can filter these only by clinical date, which would miss
  late corrections, and they're small.
- **Full syncs:** a change-only search can't report records deleted upstream, so a full
  sync still runs:
  - on the first sync
  - every 30 days
  - after `forget fhir`
  - when the patient, source name or list of searches changes
  - whenever you pass `--full`
- **Restart point:** each sync records where the next one starts in the `sync_state` table
  (one row per FHIR server). That point is the run's start time minus a day of overlap;
  overlapping records are simply updated again.
- **If a change-only search or a note's text fails:** the restart point stays put, so the
  next run asks for those changes again.

### Saved sign-ins (experimental, not working yet)

The code supports unattended syncs through Epic's dynamic client registration: after one
browser sign-in, `sync --remember` creates an RSA key on this Mac, registers its public half
with Epic, and stores the key in the Keychain. Later runs (`sync --unattended`, or the
LaunchAgent from `schedule-config`) sign a five-minute JWT with it instead of opening the
browser.

In testing this never got past registration. Epic only allows it when the sign-in grants
`offline_access`, which in turn depends on MyChart offering the patient a choice of how long
to allow access. Epic's sandbox shows a fixed "1 hour" and drops `offline_access`, and the
app can't be changed to fix that: checking **Can Register Dynamic Clients** forces the app to
be a public client, and the persistent-access options only exist for confidential clients.
A health system's own MyChart may still offer durations; if yours does, try:

```sh
uv run mychart-mcp sync --remember   # plus the step 6 flags; pick the longest duration
```

If Epic doesn't grant it, the run says "Epic didn't grant offline_access" and syncs as
usual. If it does, `sync --unattended` and `schedule-config` take it from there; the key is
stored with `/usr/bin/security` (service `mychart-mcp`, one item per FHIR base URL), and
`mychart-mcp logout --fhir-base <R4 base>` deletes it.

### If you also imported from Apple Health

The same records now exist under both origins. Compare them with `mychart-mcp stats`, which
breaks counts and date ranges down by origin and source. Then remove the Apple copy:

```sh
uv run mychart-mcp forget apple_health                         # everything from Apple Health
uv run mychart-mcp forget apple_health --source "<name>"       # just one health system
```

Keep any Apple Health source you haven't set up for direct sync.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "Saved sign-in was rejected … may have been revoked" | Access period ended or the app was removed in MyChart. Run `sync --remember` interactively again. |
| "Couldn't read the Keychain" in the scheduled log | The login Keychain is locked (no GUI login since boot). Log in at the Mac. |
| "OAuth2 Error: Something went wrong…" on your health system's page | The production client hasn't reached that health system yet (wait up to a day after marking ready), or the system turned off automatic distribution (ask them or Epic support). Epic shows the same page for request problems, so check the sandbox still works. |
| Sandbox login says "Login failed" | Use a sandbox test-patient login, not your real MyChart or fhir.epic.com account. |
| "Timed out waiting for sign-in." | The browser never reached the redirect. Did it open? If not, open the printed URL yourself. Sign-in must finish within 5 minutes. |
| Browser certificate warning on `127.0.0.1` | `mkcert -install` didn't complete on the machine running the browser (see step 5). |
| "Certificate file not found" | Run `mkcert 127.0.0.1` in the folder you pass to `--cert`/`--key`. |
| A search listed under **Skipped** with `HTTP 403` | That API isn't enabled for the app, or the health system doesn't offer it. |
| A search listed under **Skipped** with "timed out" | The server was slow even after three tries (120 s each). Re-run later. Other searches are kept. |
| "Redirect URI must be a loopback address" / "needs an explicit port" | Use `http(s)://127.0.0.1:<port>/callback`, exactly as registered. |

## Code map

`src/mychart_mcp/epic.py`:

- `Http`: GETs with a bearer token, host pinning, a 120 s timeout, and up to three
  attempts on timeouts and 502/503/504.
- `pkce_pair`, `authorize_url`, `CallbackListener`, `exchange_code`, `login`: the SMART
  standalone launch. `login` returns `(access_token, patient_id)`.
- `SEARCHES`: the searches `sync` runs. Epic requires a `category` on Condition and
  Observation searches, which is why those appear several times.
- `search`: follows `next` links and drops Epic's `OperationOutcome` warning entries.
- `inline_note`: fetches a note's `Binary` (HTML, then plain text, then RTF) and stores it as
  inline base64 `attachment.data`. `fhir.extract` then indexes it like any inline note.
- `register_client`, `client_assertion`, `saved_login`: dynamic client registration and
  JWT-bearer token requests for saved sign-ins. `keychain.py` stores the credential.
- `sync_since` → `fetch_chart` → `store`: decide full vs incremental from `sync_state`,
  download (adding `_lastUpdated` for `INCREMENTAL_TYPES`), then write in one transaction.
  A full sync replaces each type it fetched completely. An incremental one upserts changed
  records (`importers.replace_resource`) and replaces the always-full types. Types whose
  search failed keep their rows.

`tests/test_epic.py` exercises all of this against a fake in-memory FHIR server. The only
real socket it opens is a loopback one for the redirect listener.
