"""Direct Epic FHIR sync: SMART standalone patient launch, then pull the chart into SQLite.

This is the only module that makes network calls. The MCP server never imports it.
"""

from __future__ import annotations

import base64
import hashlib
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
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer

from .importers import delete_records, insert_resource

FHIR_ORIGIN = "fhir"
EPIC_SANDBOX = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
DEFAULT_REDIRECT = "http://127.0.0.1:8765/callback"
SCOPES = "openid fhirUser launch/patient patient/*.read"

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

# Preferred order when a note is offered in several formats.
NOTE_TYPES = ("text/html", "text/plain", "text/rtf")


class FhirError(Exception):
    pass


class Http:
    """GETs against one FHIR server. Refuses to send the token to any other host."""

    RETRY_STATUSES = {502, 503, 504}

    def __init__(
        self, base: str, token: str | None = None, timeout: float = 120, attempts: int = 3
    ) -> None:
        self.base = base.rstrip("/") + "/"
        self.token = token
        self.timeout = timeout
        self.attempts = attempts

    def resolve(self, url: str) -> str:
        full = urllib.parse.urljoin(self.base, url)
        if urllib.parse.urlsplit(full).netloc != urllib.parse.urlsplit(self.base).netloc:
            raise FhirError(f"Refusing to follow a link off {self.base}: {_redact(full)}")
        return full

    def get(self, url: str, accept: str = "application/fhir+json") -> bytes:
        full = self.resolve(url)
        headers = {"Accept": accept}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # Large charts make some Epic searches slow; retry timeouts and gateway errors.
        for attempt in range(1, self.attempts + 1):
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(full, headers=headers), timeout=self.timeout
                ) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code not in self.RETRY_STATUSES or attempt == self.attempts:
                    raise FhirError(f"HTTP {e.code} for {_redact(full)}") from None
            except (TimeoutError, urllib.error.URLError) as e:
                if attempt == self.attempts:
                    reason = getattr(e, "reason", None) or "timed out"
                    raise FhirError(f"{reason} for {_redact(full)}") from None
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
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "aud": fhir_base,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{smart_config['authorization_endpoint']}?{urllib.parse.urlencode(params)}"


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
        self.server = TCPServer((target.hostname, target.port), Handler)
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


def exchange_code(
    token_url: str, *, code: str, client_id: str, redirect_uri: str, verifier: str
) -> dict:
    data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        }
    ).encode()
    req = urllib.request.Request(
        token_url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise FhirError(f"Token exchange failed: HTTP {e.code}") from None


def login(
    fhir_base: str,
    client_id: str,
    redirect_uri: str = DEFAULT_REDIRECT,
    *,
    cert: str | None = None,
    key: str | None = None,
    open_url: Callable[[str], object],
) -> tuple[str, str]:
    """Run the browser sign-in. Returns (access_token, patient_id); nothing is saved."""
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
    return token["access_token"], token["patient"]


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


def fetch_chart(
    http: Http, patient_id: str, progress: Callable[[str], object] = lambda _: None
) -> tuple[list[dict], Counter]:
    """Download everything the app is allowed to read. Returns (resources, problems)."""
    problems: Counter = Counter()
    resources = [http.get_json(f"Patient/{urllib.parse.quote(patient_id)}")]
    for rtype, params in SEARCHES:
        label = f"{rtype} ({params['category']})" if params else rtype
        try:
            found = list(search(http, rtype, {"patient": patient_id, **params}))
        except FhirError as e:
            problems[f"{label}: {e}"] += 1
            progress(f"  {label:<38} skipped")
            continue
        progress(f"  {label:<38} {len(found)}")
        if rtype == "DocumentReference":
            for i, doc in enumerate(found, 1):
                try:
                    inline_note(http, doc)
                except FhirError:
                    problems["note text unavailable"] += 1
                if i % max(25, len(found) // 10) == 0 or i == len(found):
                    progress(f"    note text {i}/{len(found)}")
        resources.extend(found)
    return resources, problems


def store(conn: sqlite3.Connection, source: str, fhir_base: str, resources: list[dict]) -> Counter:
    """Replace this source's FHIR rows with `resources`."""
    counts: Counter = Counter()
    with conn:
        delete_records(conn, FHIR_ORIGIN, source)
        for resource in resources:
            if insert_resource(conn, FHIR_ORIGIN, source, resource):
                counts[resource["resourceType"]] += 1
            else:
                counts["_duplicate"] += 1
        conn.execute(
            "INSERT OR REPLACE INTO import_meta VALUES (?, ?, ?, ?)",
            (
                FHIR_ORIGIN,
                datetime.now(UTC).isoformat(timespec="seconds"),
                _redact(fhir_base),
                json.dumps(counts),
            ),
        )
    return counts


def source_name(patient: dict, fhir_base: str) -> str:
    org = patient.get("managingOrganization") or {}
    return org.get("display") or urllib.parse.urlsplit(fhir_base).hostname or fhir_base
