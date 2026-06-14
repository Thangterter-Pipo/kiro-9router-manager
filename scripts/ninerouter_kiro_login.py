"""Import the current Kiro IDC login into local 9router.

Usage:
    python scripts/ninerouter_kiro_login.py --dry-run
    python scripts/ninerouter_kiro_login.py --write --verify

The script reads Kiro's profile ARN and AWS SSO cache, refreshes the IDC token,
then upserts a 9router `kiro` provider connection into the local SQLite DB.
It never prints access tokens, refresh tokens, or client secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:20128"
DEFAULT_IDC_START_URL = "https://d-9066713dd7.awsapps.com/start"


@dataclass(frozen=True)
class KiroLogin:
    profile_arn: str
    profile_name: str
    access_token: str
    refresh_token: str
    expires_at: str
    expires_in: int
    provider_data: dict[str, Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise RuntimeError(f"JSON root is not an object: {path}")
    return data


def _post_json(url: str, payload: dict[str, Any], *, timeout: int = 25) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {error}") from exc
    return json.loads(body) if body.strip() else {}


def _get_json(url: str, *, timeout: int = 25) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {error}") from exc
    return json.loads(body) if body.strip() else {}


def _appdata() -> Path:
    raw = os.environ.get("APPDATA")
    if not raw:
        raise RuntimeError("APPDATA is not set")
    return Path(raw)


def _home() -> Path:
    return Path.home()


def default_kiro_profile_path() -> Path:
    return _appdata() / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent" / "profile.json"


def default_kiro_token_path() -> Path:
    return _home() / ".aws" / "sso" / "cache" / "kiro-auth-token.json"


def default_ninerouter_db_path() -> Path:
    return _appdata() / "9router" / "db" / "data.sqlite"


def load_kiro_login(profile_path: Path, token_path: Path, start_url: str) -> KiroLogin:
    profile = _read_json(profile_path)
    token_cache = _read_json(token_path)

    profile_arn = str(profile.get("arn") or profile.get("profileArn") or "").strip()
    if not profile_arn:
        raise RuntimeError(f"missing Kiro profile ARN in {profile_path}")

    client_hash = str(token_cache.get("clientIdHash") or "").strip()
    if not client_hash:
        raise RuntimeError(f"missing clientIdHash in {token_path}; reopen/login Kiro IDE")

    client_path = token_path.parent / f"{client_hash}.json"
    client = _read_json(client_path)

    refresh_token = str(token_cache.get("refreshToken") or "").strip()
    if not refresh_token:
        raise RuntimeError(f"missing refreshToken in {token_path}; reopen/login Kiro IDE")

    client_id = str(client.get("clientId") or "").strip()
    client_secret = str(client.get("clientSecret") or "").strip()
    region = str(token_cache.get("region") or "us-east-1").strip() or "us-east-1"
    if not client_id or not client_secret:
        raise RuntimeError(f"missing clientId/clientSecret in {client_path}")

    refreshed = _post_json(
        f"https://oidc.{region}.amazonaws.com/token",
        {
            "clientId": client_id,
            "clientSecret": client_secret,
            "refreshToken": refresh_token,
            "grantType": "refresh_token",
        },
    )
    access_token = str(refreshed.get("accessToken") or token_cache.get("accessToken") or "").strip()
    refresh_token = str(refreshed.get("refreshToken") or refresh_token).strip()
    expires_in = int(refreshed.get("expiresIn") or 3600)
    if not access_token:
        raise RuntimeError("AWS IDC refresh succeeded but did not return accessToken")

    return KiroLogin(
        profile_arn=profile_arn,
        profile_name=str(profile.get("name") or "Kiro IDC").strip() or "Kiro IDC",
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=_iso(_utcnow() + timedelta(seconds=expires_in)),
        expires_in=expires_in,
        provider_data={
            "profileArn": profile_arn,
            "clientId": client_id,
            "clientSecret": client_secret,
            "region": region,
            "authMethod": "idc",
            "startUrl": start_url,
            "provider": str(token_cache.get("provider") or "Enterprise"),
        },
    )


def _connection_data(login: KiroLogin, old_data: dict[str, Any] | None = None) -> dict[str, Any]:
    old_data = old_data or {}
    old_provider_data = old_data.get("providerSpecificData") if isinstance(old_data.get("providerSpecificData"), dict) else {}
    data = {
        **old_data,
        "accessToken": login.access_token,
        "refreshToken": login.refresh_token,
        "expiresAt": login.expires_at,
        "expiresIn": login.expires_in,
        "testStatus": "active",
        "providerSpecificData": {**old_provider_data, **login.provider_data},
    }
    for key in ("lastError", "lastErrorAt", "errorCode", "rateLimitedUntil"):
        data.pop(key, None)
    return data


def _find_existing(cur: sqlite3.Cursor, login: KiroLogin) -> tuple[str | None, dict[str, Any]]:
    rows = cur.execute(
        "select id, name, data from providerConnections where provider = 'kiro' and authType = 'oauth'"
    ).fetchall()
    account_name = str(login.provider_data.get("accountName") or "").strip()
    fallback: tuple[str | None, dict[str, Any]] = (None, {})
    for connection_id, row_name, raw in rows:
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {}
        provider_data = data.get("providerSpecificData") if isinstance(data.get("providerSpecificData"), dict) else {}
        row_account_name = str(provider_data.get("accountName") or "").strip()
        if account_name and (provider_data.get("accountName") == account_name or row_name == login.profile_name):
            return str(connection_id), data
        if not fallback[0] and not row_account_name:
            fallback = (str(connection_id), data)
        if not account_name and not row_account_name and login.profile_arn and provider_data.get("profileArn") == login.profile_arn:
            return str(connection_id), data
    if account_name or login.profile_arn:
        return (None, {})
    return fallback


def upsert_sqlite(db_path: Path, login: KiroLogin, *, write: bool) -> dict[str, Any]:
    if not db_path.is_file():
        raise RuntimeError(f"9router SQLite DB not found: {db_path}")

    con = sqlite3.connect(db_path, timeout=30)
    try:
        cur = con.cursor()
        columns = {row[1] for row in cur.execute("pragma table_info(providerConnections)")}
        required = {"id", "provider", "authType", "name", "email", "priority", "isActive", "data", "createdAt", "updatedAt"}
        if not required.issubset(columns):
            raise RuntimeError("providerConnections table shape is unsupported")

        connection_id, old_data = _find_existing(cur, login)
        data = _connection_data(login, old_data)
        action = "update" if connection_id else "insert"

        if not write:
            return {"action": action, "connectionId": connection_id, "backup": None}

        backup = db_path.with_name(f"{db_path.name}.kiro-login-{_utcnow().strftime('%Y%m%dT%H%M%S')}.bak")
        shutil.copy2(db_path, backup)
        now = _iso(_utcnow())

        if connection_id:
            cur.execute(
                """
                update providerConnections
                set name = ?, email = ?, isActive = 1, data = ?, updatedAt = ?
                where id = ?
                """,
                (login.profile_name, None, json.dumps(data, separators=(",", ":")), now, connection_id),
            )
        else:
            connection_id = str(uuid.uuid4())
            max_priority = cur.execute(
                "select coalesce(max(priority), 0) from providerConnections where provider = 'kiro'"
            ).fetchone()[0]
            cur.execute(
                """
                insert into providerConnections
                    (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
                values (?, 'kiro', 'oauth', ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    connection_id,
                    login.profile_name,
                    None,
                    int(max_priority or 0) + 1,
                    json.dumps(data, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        con.commit()
        return {"action": action, "connectionId": connection_id, "backup": str(backup)}
    finally:
        con.close()


def verify_9router(base_url: str, connection_id: str) -> dict[str, Any]:
    base = base_url.rstrip("/")
    providers = _get_json(f"{base}/api/providers", timeout=15)
    connections = providers.get("connections") if isinstance(providers, dict) else []
    kiro_connections = [item for item in connections or [] if isinstance(item, dict) and item.get("provider") == "kiro"]

    models = _get_json(f"{base}/api/providers/{connection_id}/models", timeout=60)
    provider_models = models.get("models") if isinstance(models, dict) else []
    public_models = _get_json(f"{base}/v1/models", timeout=25)
    public_data = public_models.get("data") if isinstance(public_models, dict) else []
    kr_models = [item.get("id") for item in public_data or [] if isinstance(item, dict) and str(item.get("id") or "").startswith("kr/")]

    return {
        "kiroConnections": len(kiro_connections),
        "providerModels": len(provider_models or []),
        "publicKiroModels": len(kr_models),
        "sample": kr_models[:8],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh/import Kiro IDC login into local 9router.")
    parser.add_argument("--profile", type=Path, default=default_kiro_profile_path())
    parser.add_argument("--token-cache", type=Path, default=default_kiro_token_path())
    parser.add_argument("--db", type=Path, default=default_ninerouter_db_path())
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--start-url", default=DEFAULT_IDC_START_URL)
    parser.add_argument("--dry-run", action="store_true", help="Validate inputs without writing DB. Default.")
    parser.add_argument("--write", action="store_true", help="Write/upsert the Kiro connection into 9router DB.")
    parser.add_argument("--verify", action="store_true", help="Call 9router APIs after write.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    write = bool(args.write)
    login = load_kiro_login(args.profile, args.token_cache, args.start_url)
    result = upsert_sqlite(args.db, login, write=write)

    output = {
        "ok": True,
        "mode": "write" if write else "dry-run",
        "action": result["action"],
        "connectionId": result["connectionId"],
        "profileArnSet": bool(login.profile_arn),
        "tokenRefreshed": True,
        "expiresAt": login.expires_at,
        "backup": result["backup"],
    }
    if args.verify:
        if not write or not result["connectionId"]:
            raise RuntimeError("--verify needs --write or an existing connectionId")
        output["verify"] = verify_9router(args.base_url, str(result["connectionId"]))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
