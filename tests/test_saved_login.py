"""Saved sign-in: dynamic client registration, JWT assertions, Keychain storage. No network."""

from __future__ import annotations

import base64
import json
import shlex
from collections import Counter

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from mychart_mcp import cli, epic, keychain

BASE = "https://fhir.example.test/api/FHIR/R4"
SMART = {
    "authorization_endpoint": "https://fhir.example.test/oauth2/authorize",
    "token_endpoint": "https://fhir.example.test/oauth2/token",
}


def b64url_json(part):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


@pytest.fixture()
def posts(monkeypatch):
    """Record epic._post calls and answer registration and token requests."""
    calls = []

    def fake_post(url, *, form=None, json_body=None, bearer=None):
        calls.append({"url": url, "form": form, "json": json_body, "bearer": bearer})
        if url.endswith("/register"):
            return {"client_id": "dyn-123"}
        return {"access_token": "fresh", "patient": "p1"}

    monkeypatch.setattr(epic, "_post", fake_post)
    return calls


@pytest.fixture()
def credential(posts):
    return epic.register_client(epic.Login("initial", "p1", SMART), "app-client-id")


def test_registration_sends_public_key_only(credential, posts):
    reg = posts[0]
    assert reg["url"] == "https://fhir.example.test/oauth2/register"
    assert reg["bearer"] == "initial"
    assert reg["json"]["software_id"] == "app-client-id"
    (jwk,) = reg["json"]["jwks"]["keys"]
    assert jwk["kty"] == "RSA" and {"n", "e"} <= jwk.keys() and "d" not in jwk
    assert credential["client_id"] == "dyn-123"
    assert credential["patient"] == "p1"


def test_registration_needs_offline_access(posts):
    login = epic.Login("initial", "p1", SMART, "openid patient/*.read")
    with pytest.raises(epic.FhirError, match="didn't grant offline_access"):
        epic.register_client(login, "app-client-id")
    assert posts == []


def test_assertion_is_signed_with_the_saved_key(credential):
    jwt = epic.client_assertion(credential, now=1_000_000)
    header, claims, signature = jwt.split(".")
    assert b64url_json(header) == {"alg": "RS384", "typ": "JWT", "kid": credential["kid"]}
    body = b64url_json(claims)
    assert body["iss"] == body["sub"] == "dyn-123"
    assert body["aud"] == SMART["token_endpoint"]
    assert body["exp"] - body["iat"] == 300

    public = serialization.load_pem_private_key(credential["key"].encode(), None).public_key()
    sig = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    public.verify(sig, f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA384())


def test_saved_login_uses_jwt_bearer_grant(credential, posts):
    assert epic.saved_login(credential) == ("fresh", "p1")
    form = posts[-1]["form"]
    assert form["grant_type"] == epic.JWT_BEARER
    assert form["client_id"] == "dyn-123"


def test_rejected_saved_login_says_so(credential, monkeypatch):
    def refuse(*a, **k):
        raise epic.FhirError("HTTP 400")

    monkeypatch.setattr(epic, "_post", refuse)
    with pytest.raises(epic.FhirError, match="revoked"):
        epic.saved_login(credential)


class FakeSecurity:
    """Stands in for /usr/bin/security, including `-i` commands read from stdin."""

    def __init__(self):
        self.items: dict[tuple[str, str], str] = {}
        self.argv_seen: list[list[str]] = []

    def __call__(self, argv, input=None, **kwargs):
        self.argv_seen.append(argv)
        if argv[1] == "-i":
            words = shlex.split(input)
            opts = dict(zip(words[2::2], words[3::2], strict=False))
            self.items[(opts["-s"], opts["-a"])] = opts["-w"]
            return Result(0)
        opts = dict(zip(argv[2::2], argv[3::2], strict=False))
        key = (opts["-s"], opts["-a"])
        if argv[1] == "find-generic-password":
            return Result(0, self.items[key]) if key in self.items else Result(44)
        if argv[1] == "delete-generic-password":
            return Result(0) if self.items.pop(key, None) else Result(44)
        raise AssertionError(argv)


class Result:
    def __init__(self, returncode, stdout=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


@pytest.fixture()
def fake_security(monkeypatch):
    fake = FakeSecurity()
    monkeypatch.setattr(keychain.subprocess, "run", fake)
    monkeypatch.setattr(keychain.sys, "platform", "darwin")
    return fake


def test_keychain_round_trip_keeps_secret_out_of_argv(fake_security):
    cred = {"client_id": "dyn-123", "key": "-----BEGIN PRIVATE KEY-----\nabc\n"}
    keychain.save(BASE + "/", cred)
    assert keychain.load(BASE) == cred
    (secret,) = fake_security.items.values()
    assert not any(secret in arg for argv in fake_security.argv_seen for arg in argv)
    assert keychain.delete(BASE) is True
    assert keychain.load(BASE) is None


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr(cli.sys, "argv", ["mychart-mcp", *argv])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code


def test_unattended_without_saved_login_never_opens_browser(fake_security, monkeypatch, capsys):
    monkeypatch.setattr(epic, "login", lambda *a, **k: pytest.fail("opened browser"))
    assert run_cli(monkeypatch, "sync", "--unattended", "--fhir-base", BASE) == 1
    assert "--remember" in capsys.readouterr().err


def test_unattended_with_saved_login_syncs(fake_security, credential, monkeypatch, tmp_path):
    monkeypatch.setenv("MYCHART_MCP_DB", str(tmp_path / "health.db"))
    keychain.save(BASE, credential)
    monkeypatch.setattr(epic, "login", lambda *a, **k: pytest.fail("opened browser"))
    monkeypatch.setattr(
        epic, "fetch_patient", lambda http, pid: {"resourceType": "Patient", "id": pid}
    )
    monkeypatch.setattr(
        epic,
        "fetch_chart",
        lambda http, patient, since, progress, stored_note: epic.Chart(
            [patient], Counter(), {"Patient"}, since
        ),
    )
    assert run_cli(monkeypatch, "sync", "--unattended", "--fhir-base", BASE) == 0


def test_logout_removes_saved_login(fake_security, credential, monkeypatch):
    keychain.save(BASE, credential)
    assert run_cli(monkeypatch, "logout", "--fhir-base", BASE) == 0
    assert keychain.load(BASE) is None
