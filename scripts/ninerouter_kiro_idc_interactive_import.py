"""Interactively import Kiro IAM Identity Center accounts into local 9router.

This uses the normal OAuth browser flow. It does not store or print passwords.

Usage:
    python scripts/ninerouter_kiro_idc_interactive_import.py \
        --account "jcooperr2048|https://d9022339b6s6.awsapps.com/start" \
        --account "jcooperr2049|https://d9022339b6s6.awsapps.com/start" \
        --write --verify
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from ninerouter_kiro_login import (
    DEFAULT_BASE_URL,
    KiroLogin,
    _iso,
    _post_json,
    _utcnow,
    default_ninerouter_db_path,
    upsert_sqlite,
    verify_9router,
)


SCOPES = [
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
    "codewhisperer:transformations",
    "codewhisperer:taskassist",
]


@dataclass(frozen=True)
class AccountSpec:
    name: str
    start_url: str
    region: str = "us-east-1"


def _post_json_auth(url: str, payload: dict[str, Any], access_token: str, *, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "aws-sdk-js/1.0.0 ua/2.1 os/Windows lang/js md/nodejs#20 api/codewhispererruntime#1.0.0 m/N,E KiroIDE-0.2.0",
            "x-amz-user-agent": "aws-sdk-js/1.0.0 KiroIDE-0.2.0",
            "x-amzn-codewhisperer-optout": "true",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


class _CallbackHandler(BaseHTTPRequestHandler):
    server: "_OAuthServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/oauth/callback":
            self.send_response(404)
            self.end_headers()
            return
        self.server.callback_query = urllib.parse.parse_qs(parsed.query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h3>Kiro OAuth captured.</h3><p>You can close this tab and return to Codex.</p></body></html>"
        )


class _OAuthServer(HTTPServer):
    callback_query: dict[str, list[str]] | None = None


def _code_verifier() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _register_client(oidc_base: str, start_url: str, redirect_uri: str) -> tuple[str, str]:
    data = _post_json(
        f"{oidc_base}/client/register",
        {
            "clientName": "Kiro",
            "clientType": "public",
            "scopes": SCOPES,
            "grantTypes": ["authorization_code", "refresh_token"],
            "redirectUris": [redirect_uri],
            "issuerUrl": start_url,
        },
    )
    client_id = str(data.get("clientId") or "").strip()
    client_secret = str(data.get("clientSecret") or "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("OIDC register returned no clientId/clientSecret")
    return client_id, client_secret


def _authorize_url(oidc_base: str, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scopes": ",".join(SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{oidc_base}/authorize?{params}"


def _wait_for_callback(server: _OAuthServer, state: str, timeout_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        server.timeout = 1
        server.handle_request()
        query = server.callback_query or {}
        if not query:
            continue
        if query.get("error", [""])[0]:
            raise RuntimeError(f"OAuth error: {query.get('error', [''])[0]}")
        if query.get("state", [""])[0] != state:
            raise RuntimeError("OAuth state mismatch")
        code = query.get("code", [""])[0]
        if not code:
            raise RuntimeError("OAuth callback did not include code")
        return code
    raise RuntimeError("OAuth login timed out")


def _exchange_token(
    oidc_base: str,
    client_id: str,
    client_secret: str,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    return _post_json(
        f"{oidc_base}/token",
        {
            "clientId": client_id,
            "clientSecret": client_secret,
            "grantType": "authorization_code",
            "redirectUri": redirect_uri,
            "code": code,
            "codeVerifier": verifier,
        },
    )


def _resolve_profile_arn(access_token: str, token_data: dict[str, Any]) -> str:
    profile_arn = str(token_data.get("profileArn") or "").strip()
    if profile_arn:
        return profile_arn
    data = _post_json_auth(
        "https://codewhisperer.us-east-1.amazonaws.com/ListAvailableProfiles",
        {"maxResults": 10},
        access_token,
    )
    for item in data.get("profiles") or []:
        arn = str(item.get("arn") or "").strip() if isinstance(item, dict) else ""
        if arn:
            return arn
    raise RuntimeError("no Kiro profileArn returned")


def import_account(spec: AccountSpec, db_path: Path, *, write: bool, timeout_seconds: int) -> dict[str, Any]:
    oidc_base = f"https://oidc.{spec.region}.amazonaws.com"
    server = _OAuthServer(("127.0.0.1", 0), _CallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth/callback"
    try:
        client_id, client_secret = _register_client(oidc_base, spec.start_url, redirect_uri)
        verifier, challenge = _code_verifier()
        state = secrets.token_urlsafe(24)
        url = _authorize_url(oidc_base, client_id, redirect_uri, state, challenge)
        if not webbrowser.open(url, new=1, autoraise=True):
            raise RuntimeError("could not open browser")
        code = _wait_for_callback(server, state, timeout_seconds)
        token_data = _exchange_token(oidc_base, client_id, client_secret, code, verifier, redirect_uri)
        access_token = str(token_data.get("accessToken") or "").strip()
        refresh_token = str(token_data.get("refreshToken") or "").strip()
        expires_in = int(token_data.get("expiresIn") or 3600)
        if not access_token or not refresh_token:
            raise RuntimeError("OAuth token response missing accessToken/refreshToken")
        profile_arn = _resolve_profile_arn(access_token, token_data)
        login = KiroLogin(
            profile_arn=profile_arn,
            profile_name=spec.name,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=_iso(_utcnow() + timedelta(seconds=expires_in)),
            expires_in=expires_in,
            provider_data={
                "profileArn": profile_arn,
                "clientId": client_id,
                "clientSecret": client_secret,
                "region": spec.region,
                "authMethod": "idc",
                "startUrl": spec.start_url,
                "provider": "Enterprise",
            },
        )
        result = upsert_sqlite(db_path, login, write=write)
        return {
            "ok": True,
            "name": spec.name,
            "mode": "write" if write else "dry-run",
            "action": result.get("action"),
            "connectionId": result.get("connectionId"),
            "profileArnSet": True,
            "tokenReceived": True,
            "backup": result.get("backup"),
        }
    finally:
        server.server_close()


def _parse_account(raw: str) -> AccountSpec:
    parts = [item.strip() for item in raw.split("|")]
    if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
        raise argparse.ArgumentTypeError("account format: name|startUrl[|region]")
    return AccountSpec(name=parts[0], start_url=parts[1], region=parts[2] if len(parts) == 3 and parts[2] else "us-east-1")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactively import Kiro IAM IDC accounts into local 9router.")
    parser.add_argument("--account", action="append", type=_parse_account, required=True, help="name|startUrl[|region]")
    parser.add_argument("--db", type=Path, default=default_ninerouter_db_path())
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=int, default=600, help="Seconds to wait for each browser login")
    parser.add_argument("--write", action="store_true", help="Write/upsert into 9router DB")
    parser.add_argument("--verify", action="store_true", help="Verify final 9router model list")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    items = []
    last_connection_id = ""
    for spec in args.account:
        try:
            item = import_account(spec, args.db, write=bool(args.write), timeout_seconds=args.timeout)
            last_connection_id = str(item.get("connectionId") or last_connection_id)
            items.append(item)
        except Exception as exc:
            items.append({"ok": False, "name": spec.name, "error": str(exc)})
    output: dict[str, Any] = {
        "ok": all(item.get("ok") for item in items),
        "mode": "write" if args.write else "dry-run",
        "total": len(items),
        "success": sum(1 for item in items if item.get("ok")),
        "failed": sum(1 for item in items if not item.get("ok")),
        "items": items,
    }
    if args.verify and args.write and last_connection_id:
        output["verify"] = verify_9router(args.base_url, last_connection_id)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
