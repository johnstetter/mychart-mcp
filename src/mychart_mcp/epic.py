"""Direct Epic FHIR sync: SMART standalone patient launch, then pull the chart into SQLite.

This is the only module that makes network calls. The MCP server never imports it.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import secrets
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer

from .importers import delete_records, insert_resource, replace_resource

FHIR_ORIGIN = "fhir"
EPIC_SANDBOX = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
DEFAULT_REDIRECT = "http://127.0.0.1:8765/callback"
SCOPES = "openid fhirUser launch/patient patient/*.read"
# Asks the patient how long to allow access; Epic only lets a sign-in with this scope register
# a device key (see register_client).
OFFLINE_SCOPE = "offline_access"

# Epic requires a category on Condition and Observation searches.
SEARCHES: list[tuple[str, dict[str, str]]] = [
    ("Condition", {"category": "problem-list-item"}),
    ("Condition", {"category": "encounter-diagnosis"}),
    ("Condition", {"category": "health-concern"}),
    ("Observation", {"category": "laboratory"}),
    ("Observation", {"category": "vital-signs"}),
    ("Observation", {"category": "social-history"}),
    ("DiagnosticReport", {}),
    ("DocumentReference", {"category": "clinical-note"}),
    ("Encounter", {}),
    ("MedicationRequest", {}),
    ("AllergyIntolerance", {}),
    ("Immunization", {}),
    ("Procedure", {}),
]

# Types Epic can search by _lastUpdated (from the August 2026 release; older servers list fewer,
# see changes_only_types). The rest have only clinical-date filters, which miss late
# corrections, so every sync refetches them in full.
INCREMENTAL_TYPES = frozenset({"Observation", "DiagnosticReport", "DocumentReference", "Encounter"})
# A _lastUpdated search can't report deletions; a periodic full sync drops them.
FULL_SYNC_EVERY = timedelta(days=30)
# Ask for a day more than strictly needed, for clock skew and server indexing lag.
OVERLAP = timedelta(days=1)

# Preferred order when a note is offered in several formats.
NOTE_TYPES = ("text/html", "text/plain", "text/rtf")


class FhirError(Exception):
    pass


class Http:
    """GETs against one FHIR server. Refuses to send the token to any other host."""

    RETRY_STATUSES = {502, 503, 504}
    REDIRECT_STATUSES = {301, 302, 303, 307, 308}

    def __init__(
        self, base: str, token: str | None = None, timeout: float = 120, attempts: int = 3
    ) -> None:
        self.base = base.rstrip("/") + "/"
        self.token = token
        self.timeout = timeout
        self.attempts = attempts
        self._conn: http.client.HTTPConnection | None = None

    def resolve(self, url: str) -> str:
        full = urllib.parse.urljoin(self.base, url)
        if urllib.parse.urlsplit(full).netloc != urllib.parse.urlsplit(self.base).netloc:
            raise FhirError(f"Refusing to follow a link off {self.base}: {_redact(full)}")
        return full

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _request(
        self, full: str, headers: dict[str, str]
    ) -> tuple[http.client.HTTPResponse, bytes]:
        # One kept-alive connection for the whole sync instead of a TLS handshake per request.
        reused = self._conn is not None
        if self._conn is None:
            parts = urllib.parse.urlsplit(self.base)
            cls = (
                http.client.HTTPSConnection
                if parts.scheme == "https"
                else http.client.HTTPConnection
            )
            self._conn = cls(parts.netloc, timeout=self.timeout)
        parts = urllib.parse.urlsplit(full)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        try:
            self._conn.request("GET", path, headers=headers)
            resp = self._conn.getresponse()
            body = resp.read()
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
            self.close()
            if not reused:
                raise
            return self._request(full, headers)  # the server closed an idle connection
        except (OSError, http.client.HTTPException):
            self.close()
            raise
        if resp.will_close:
            self.close()
        return resp, body

    def get(self, url: str, accept: str = "application/fhir+json", _redirects: int = 5) -> bytes:
        full = self.resolve(url)
        headers = {"Accept": accept}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # Large charts make some Epic searches slow; retry timeouts and gateway errors.
        for attempt in range(1, self.attempts + 1):
            try:
                resp, body = self._request(full, headers)
            except (OSError, http.client.HTTPException) as e:
                if attempt == self.attempts:
                    reason = "timed out" if isinstance(e, TimeoutError) else str(e) or repr(e)
                    raise FhirError(f"{reason} for {_redact(full)}") from None
            else:
                if 200 <= resp.status < 300:
                    return body
                location = resp.getheader("Location")
                if resp.status in self.REDIRECT_STATUSES and location and _redirects:
                    return self.get(location, accept, _redirects - 1)
                if resp.status not in self.RETRY_STATUSES or attempt == self.attempts:
                    raise FhirError(f"HTTP {resp.status} for {_redact(full)}")
            time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def get_json(self, url: str, accept: str = "application/fhir+json") -> dict:
        body = self.get(url, accept)
        try:
            return json.loads(body)
        except ValueError:
            raise FhirError(f"Response wasn't JSON for {_redact(self.resolve(url))}") from None


def _redact(url: str) -> str:
    # Query strings carry the patient ID; keep them out of error messages.
    return urllib.parse.urlsplit(url)._replace(query="").geturl()


# --- SMART on FHIR login ------------------------------------------------------------------


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(
    smart_config: dict,
    *,
    client_id: str,
    redirect_uri: str,
    fhir_base: str,
    state: str,
    challenge: str,
    offline: bool = False,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": f"{SCOPES} {OFFLINE_SCOPE}" if offline else SCOPES,
        "state": state,
        "aud": fhir_base,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{smart_config['authorization_endpoint']}?{urllib.parse.urlencode(params)}"


class _ReusableTCPServer(TCPServer):
    allow_reuse_address = True


class CallbackListener:
    """One-shot loopback HTTP(S) server that receives the OAuth redirect."""

    def __init__(self, redirect_uri: str, cert: str | None = None, key: str | None = None):
        target = urllib.parse.urlsplit(redirect_uri)
        if target.hostname not in ("127.0.0.1", "localhost"):
            raise FhirError(f"Redirect URI must be a loopback address: {redirect_uri}")
        self.path = target.path or "/"
        self.result: dict[str, str] = {}
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                url = urllib.parse.urlsplit(self.path)
                if url.path != listener.path:
                    self.send_error(404)
                    return
                listener.result = dict(urllib.parse.parse_qsl(url.query))
                body = b"<p>Signed in. You can close this tab and return to the terminal.</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass  # the request line contains the authorization code

        if target.port is None:
            raise FhirError(f"Redirect URI needs an explicit port: {redirect_uri}")
        # Not HTTPServer: its reverse-DNS lookup of the bind address can hang for ~30s.
        # Reuse the address like HTTPServer does, so a re-run right after the last one can bind
        # while the old connection is in TIME_WAIT.
        self.server = _ReusableTCPServer((target.hostname, target.port), Handler)
        self.server.handle_error = lambda *a: None  # type: ignore[method-assign]
        if target.scheme == "https":
            if not (cert and key):
                raise FhirError("An https redirect URI needs --cert and --key (see mkcert).")
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def wait(self, state: str, timeout: float = 300) -> str:
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                self.server.timeout = min(1.0, deadline - time.monotonic())
                self.server.handle_request()
                if not self.result:
                    continue
                result, self.result = self.result, {}
                if result.get("error"):
                    raise FhirError(
                        f"Sign-in failed: {result['error']} {result.get('error_description', '')}"
                    )
                if result.get("state") == state and result.get("code"):
                    return result["code"]
                # Wrong or missing state: ignore it and keep waiting for the real redirect.
            raise FhirError("Timed out waiting for sign-in.")
        finally:
            self.server.server_close()


def _post(
    url: str, *, form: dict | None = None, json_body: dict | None = None, bearer: str | None = None
) -> dict:
    headers = {"Accept": "application/json"}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise FhirError(f"HTTP {e.code} from {_redact(url)}{_oauth_error(e)}") from None
    except (TimeoutError, urllib.error.URLError, ValueError) as e:
        raise FhirError(f"{getattr(e, 'reason', e)} from {_redact(url)}") from None


def _oauth_error(e: urllib.error.HTTPError) -> str:
    """The OAuth `error`/`error_description` from a failed token or registration call."""
    try:
        body = json.loads(e.read(4096))
    except (OSError, ValueError):
        return ""
    if not isinstance(body, dict):
        return ""
    parts = [str(body[k]) for k in ("error", "error_description") if body.get(k)]
    return f": {' - '.join(parts)[:300]}" if parts else ""


def exchange_code(
    token_url: str, *, code: str, client_id: str, redirect_uri: str, verifier: str
) -> dict:
    return _post(
        token_url,
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )


@dataclass
class Login:
    access_token: str
    patient_id: str
    smart: dict  # the server's .well-known/smart-configuration
    scope: str = ""  # what the server granted, which can be less than requested


def login(
    fhir_base: str,
    client_id: str,
    redirect_uri: str = DEFAULT_REDIRECT,
    *,
    cert: str | None = None,
    key: str | None = None,
    open_url: Callable[[str], object],
    offline: bool = False,
) -> Login:
    """Run the browser sign-in. Nothing is saved. `offline` requests persistent access."""
    smart = Http(fhir_base).get_json(".well-known/smart-configuration", "application/json")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    listener = CallbackListener(redirect_uri, cert, key)
    open_url(
        authorize_url(
            smart,
            client_id=client_id,
            redirect_uri=redirect_uri,
            fhir_base=fhir_base,
            state=state,
            challenge=challenge,
            offline=offline,
        )
    )
    code = listener.wait(state)
    token = exchange_code(
        smart["token_endpoint"],
        code=code,
        client_id=client_id,
        redirect_uri=redirect_uri,
        verifier=verifier,
    )
    if not token.get("access_token") or not token.get("patient"):
        raise FhirError("The server didn't return an access token and patient ID.")
    return Login(token["access_token"], token["patient"], smart, token.get("scope", ""))


# --- Saved sign-in (Epic dynamic client registration) ------------------------------------
#
# After one browser sign-in, register a per-device client that authenticates with its own
# RSA key. Later runs sign a short-lived JWT with that key to get a fresh access token, so
# scheduled syncs need no browser. The saved credential is that key plus the client ID.

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64url(n: int) -> str:
    return _b64url(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def register_client(login: Login, software_id: str) -> dict:
    """Register this device as a dynamic client. Returns the credential to save."""
    if login.scope and OFFLINE_SCOPE not in login.scope.split():
        raise FhirError(
            f"Epic didn't grant {OFFLINE_SCOPE} (granted: {login.scope}). "
            "Choose a longer access period on the consent screen."
        )
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()
    kid = secrets.token_hex(8)
    jwk = {
        "kty": "RSA",
        "kid": kid,
        "e": _int_b64url(numbers.e),
        "n": _int_b64url(numbers.n),
    }
    endpoint = login.smart.get("registration_endpoint") or (
        login.smart["authorization_endpoint"].rsplit("/", 1)[0] + "/register"
    )
    response = _post(
        endpoint,
        json_body={"software_id": software_id, "jwks": {"keys": [jwk]}},
        bearer=login.access_token,
    )
    if not response.get("client_id"):
        raise FhirError("Registration didn't return a client ID.")
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return {
        "client_id": response["client_id"],
        "kid": kid,
        "key": pem,
        "token_endpoint": login.smart["token_endpoint"],
        "patient": login.patient_id,
    }


def client_assertion(credential: dict, now: float | None = None) -> str:
    """A five-minute RS384 JWT proving possession of the saved key."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    issued = int(now if now is not None else time.time())
    header = {"alg": "RS384", "typ": "JWT", "kid": credential["kid"]}
    claims = {
        "iss": credential["client_id"],
        "sub": credential["client_id"],
        "aud": credential["token_endpoint"],
        "jti": secrets.token_urlsafe(16),
        "iat": issued,
        "nbf": issued,
        "exp": issued + 300,
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
    )
    key = serialization.load_pem_private_key(credential["key"].encode(), password=None)
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA384())
    return f"{signing_input}.{_b64url(signature)}"


def saved_login(credential: dict) -> tuple[str, str]:
    """Get an access token with the saved key. Returns (access_token, patient_id)."""
    try:
        token = _post(
            credential["token_endpoint"],
            form={
                "grant_type": JWT_BEARER,
                "assertion": client_assertion(credential),
                "client_id": credential["client_id"],
            },
        )
    except FhirError as e:
        raise FhirError(f"Saved sign-in was rejected ({e}); it may have been revoked.") from None
    if not token.get("access_token"):
        raise FhirError("Saved sign-in didn't return an access token.")
    return token["access_token"], token.get("patient") or credential["patient"]


# --- Fetch and store ----------------------------------------------------------------------


def search(http: Http, rtype: str, params: dict[str, str]) -> Iterator[dict]:
    """Yield every resource of `rtype` across all pages, skipping OperationOutcome entries."""
    url: str | None = f"{rtype}?{urllib.parse.urlencode(params)}"
    seen: set[str] = set()
    while url and url not in seen:
        seen.add(url)
        bundle = http.get_json(url)
        for entry in bundle.get("entry") or []:
            resource = entry.get("resource") or {}
            if resource.get("resourceType") == rtype:
                yield resource
        url = next(
            (link["url"] for link in bundle.get("link") or [] if link.get("relation") == "next"),
            None,
        )


def inline_note(http: Http, doc: dict) -> bool:
    """Fetch a clinical note's text from Binary and embed it so it's searchable."""
    attachments = [c.get("attachment") or {} for c in doc.get("content") or []]
    if any(a.get("data") for a in attachments):
        return False
    for ctype in NOTE_TYPES:
        for attachment in attachments:
            if attachment.get("url") and attachment.get("contentType", "").startswith(ctype):
                content = http.get(attachment["url"], accept=ctype)
                if content.lstrip().startswith(b"{"):
                    # Some servers ignore Accept and return a FHIR Binary resource.
                    binary = json.loads(content)
                    if binary.get("resourceType") == "Binary":
                        content = base64.b64decode(binary.get("data") or "")
                attachment["data"] = base64.b64encode(content).decode()
                attachment["contentType"] = ctype
                return True
    return False


@dataclass
class Chart:
    resources: list[dict]
    problems: Counter
    complete: set[str]  # types whose every search succeeded
    since: str | None  # None for a full sync
    changes_only: frozenset[str] = frozenset()  # types searched with _lastUpdated

    @property
    def missed_changes(self) -> bool:
        """Whether a change-only search or a note's text failed (others refetch in full)."""
        return bool(self.changes_only - self.complete) or "note text unavailable" in self.problems


def changes_only_types(http: Http) -> frozenset[str]:
    """INCREMENTAL_TYPES this server says it can search by _lastUpdated.

    Servers silently ignore a search parameter they don't support, which would return
    everything while the sync treated it as only the changes.
    """
    try:
        statement = http.get_json("metadata")
    except FhirError:
        return frozenset()
    supported = {
        resource.get("type")
        for rest in statement.get("rest") or []
        for resource in rest.get("resource") or []
        if any(
            (param.get("name") or "").lower() == "_lastupdated"
            for param in resource.get("searchParam") or []
        )
    }
    return INCREMENTAL_TYPES & supported


def fetch_patient(http: Http, patient_id: str) -> dict:
    return http.get_json(f"Patient/{urllib.parse.quote(patient_id)}")


def fetch_chart(
    http: Http,
    patient: dict,
    since: str | None = None,
    progress: Callable[[str], object] = lambda _: None,
    stored_note: Callable[[str], dict | None] = lambda _: None,
    changes_only: frozenset[str] = INCREMENTAL_TYPES,
) -> Chart:
    """Download what the app may read; with `since`, only changes for `changes_only` types.

    Epic returns notes without meta.lastUpdated on every incremental search, so with `since`
    a note whose stored copy (`stored_note(id)`) matches reuses that copy's text.
    """
    problems: Counter = Counter()
    resources = [patient]
    failed: set[str] = set()
    for rtype, params in SEARCHES:
        label = f"{rtype} ({params['category']})" if params else rtype
        query = {"patient": patient["id"], **params}
        if since and rtype in changes_only:
            query["_lastUpdated"] = f"ge{since}"
        try:
            found = list(search(http, rtype, query))
        except FhirError as e:
            problems[f"{label}: {e}"] += 1
            failed.add(rtype)
            progress(f"  {label:<38} skipped")
            continue
        progress(f"  {label:<38} {len(found)}")
        if rtype == "DocumentReference":
            reused = 0
            for i, doc in enumerate(found, 1):
                old = stored_note(str(doc.get("id", ""))) if since else None
                if old and _note_key(old) == _note_key(doc):
                    doc["content"] = old["content"]
                    reused += 1
                    continue
                try:
                    inline_note(http, doc)
                except FhirError:
                    problems["note text unavailable"] += 1
                if i % max(25, len(found) // 10) == 0 or i == len(found):
                    progress(f"    note text {i}/{len(found)}")
            if reused:
                progress(f"    note text unchanged for {reused}, reused")
        resources.extend(found)
    complete = {"Patient"} | {rtype for rtype, _ in SEARCHES} - failed
    return Chart(resources, problems, complete, since, changes_only if since else frozenset())


def _note_key(doc: dict) -> str:
    """Everything about a note except its fetched text; equal keys mean the same note."""
    rest = {k: v for k, v in doc.items() if k != "content"}
    urls = sorted((c.get("attachment") or {}).get("url") or "" for c in doc.get("content") or [])
    return json.dumps([rest, urls], sort_keys=True)


def stored_note(conn: sqlite3.Connection, source: str, note_id: str) -> dict | None:
    row = conn.execute(
        "SELECT raw FROM resources WHERE origin = ? AND source = ? "
        "AND resource_type = 'DocumentReference' AND resource_id = ?",
        (FHIR_ORIGIN, source, note_id),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _searches_signature() -> str:
    return json.dumps(sorted([rtype, sorted(params.items())] for rtype, params in SEARCHES))


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync_since(
    conn: sqlite3.Connection,
    fhir_base: str,
    patient_id: str,
    source: str,
    *,
    full: bool = False,
    now: datetime | None = None,
) -> str | None:
    """Where an incremental sync can start, or None when this one must be full."""
    row = conn.execute(
        "SELECT patient_id, source, searches, since, full_at FROM sync_state WHERE fhir_base = ?",
        (fhir_base,),
    ).fetchone()
    if full or row is None:
        return None
    saved_patient, saved_source, searches, since, full_at = row
    if (saved_patient, saved_source, searches) != (patient_id, source, _searches_signature()):
        return None
    now = now or datetime.now(UTC)
    if now - datetime.fromisoformat(full_at) > FULL_SYNC_EVERY:
        return None
    return since


def store(
    conn: sqlite3.Connection, source: str, fhir_base: str, chart: Chart, started: datetime
) -> Counter:
    """Save a fetched chart and where the next sync starts.

    A full sync replaces each type it fetched completely. An incremental one replaces the
    types without _lastUpdated and upserts the changed records of the rest. Rows of a type
    whose search failed are kept. An incremental sync that missed changes keeps the old
    starting point so the next one asks again; a full sync always moves it (a permanently
    refused API would otherwise make every sync full).
    """
    replace = chart.complete - chart.changes_only
    counts: Counter = Counter()
    with conn:
        for rtype in replace:
            delete_records(conn, FHIR_ORIGIN, source, rtype)
        for resource in chart.resources:
            if resource["resourceType"] in replace:
                if not insert_resource(conn, FHIR_ORIGIN, source, resource):
                    counts["_duplicate"] += 1
                    continue
            else:
                replace_resource(conn, FHIR_ORIGIN, source, resource)
            counts[resource["resourceType"]] += 1
        conn.execute(
            "INSERT OR REPLACE INTO import_meta VALUES (?, ?, ?, ?)",
            (
                FHIR_ORIGIN,
                datetime.now(UTC).isoformat(timespec="seconds"),
                _redact(fhir_base),
                json.dumps(counts),
            ),
        )
        previous = conn.execute(
            "SELECT since, full_at FROM sync_state WHERE fhir_base = ?", (fhir_base,)
        ).fetchone()
        since = _utc(started - OVERLAP)
        full_at = started.isoformat()
        if chart.since and previous:
            full_at = previous[1]
            if chart.missed_changes:
                since = previous[0]  # ask for them again next time
        conn.execute(
            "INSERT OR REPLACE INTO sync_state VALUES (?, ?, ?, ?, ?, ?)",
            (
                fhir_base,
                chart.resources[0]["id"],
                source,
                _searches_signature(),
                since,
                full_at,
            ),
        )
    return counts


def source_name(patient: dict, fhir_base: str) -> str:
    org = patient.get("managingOrganization") or {}
    return org.get("display") or urllib.parse.urlsplit(fhir_base).hostname or fhir_base
