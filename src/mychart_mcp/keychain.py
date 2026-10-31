"""Saved Epic sign-ins in the macOS login Keychain, via Apple's `security` tool.

Going through /usr/bin/security (not Python's Security framework bindings) keeps the item's
access list tied to an Apple-signed binary that doesn't change, so a Homebrew Python upgrade
can't trigger an "allow access?" prompt that a scheduled run has no way to answer.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys

SERVICE = "mychart-mcp"
SECURITY = "/usr/bin/security"
NOT_FOUND = 44  # errSecItemNotFound


class KeychainError(Exception):
    pass


def _account(fhir_base: str) -> str:
    account = fhir_base.rstrip("/")
    if any(c.isspace() or c in "\"'\\" for c in account):
        raise KeychainError(f"Unexpected characters in FHIR base URL: {fhir_base}")
    return account


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise KeychainError("Saved sign-ins need the macOS Keychain.")


def save(fhir_base: str, credential: dict) -> None:
    _require_macos()
    secret = base64.b64encode(json.dumps(credential).encode()).decode()
    # `security -i` reads its command from stdin, so the key never appears in the process list.
    command = f'add-generic-password -U -s {SERVICE} -a "{_account(fhir_base)}" -w {secret}\n'
    result = subprocess.run(
        [SECURITY, "-i"], input=command, capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0 or load(fhir_base) != credential:
        raise KeychainError(f"Couldn't save to the Keychain: {result.stderr.strip()}")


def load(fhir_base: str) -> dict | None:
    _require_macos()
    result = subprocess.run(
        [SECURITY, "find-generic-password", "-s", SERVICE, "-a", _account(fhir_base), "-w"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == NOT_FOUND:
        return None
    if result.returncode != 0:
        # Typically a locked Keychain (no GUI login since boot).
        raise KeychainError(f"Couldn't read the Keychain: {result.stderr.strip()}")
    return json.loads(base64.b64decode(result.stdout.strip()))


def delete(fhir_base: str) -> bool:
    _require_macos()
    result = subprocess.run(
        [SECURITY, "delete-generic-password", "-s", SERVICE, "-a", _account(fhir_base)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode not in (0, NOT_FOUND):
        raise KeychainError(f"Couldn't delete from the Keychain: {result.stderr.strip()}")
    return result.returncode == 0
