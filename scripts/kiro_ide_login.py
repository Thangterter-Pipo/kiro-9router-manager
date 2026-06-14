#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Ghi token Kiro (IDC / social) vao AWS SSO cache de mo IDE Kiro la da dang nhap san.

Kiro IDE doc token tu thu muc C:\\Users\\<user>\\.aws\\sso\\cache\\ :
  1. kiro-auth-token.json : accessToken, refreshToken, profileArn, expiresAt,
     region, authMethod, provider (va clientIdHash neu la IDC).
  2. <clientIdHash>.json   : clientId, clientSecret, expiresAt, scopes,
     registrationExpiresAt (chi can cho IDC).

Module nay KHONG bao gio in token/secret ra man hinh.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Scope mac dinh cho CodeWhisperer / Kiro IDC.
DEFAULT_SCOPES = ["codewhisperer:completions", "codewhisperer:analysis"]


def default_cache_dir() -> Path:
    """Tra ve thu muc cache AWS SSO mac dinh: <home>/.aws/sso/cache."""
    return Path.home() / ".aws" / "sso" / "cache"


def _utc_stamp() -> str:
    """Timestamp UTC dang %Y%m%dT%H%M%S dung cho ten file backup."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _backup_if_exists(path: Path, backups: list) -> None:
    """Neu file da ton tai thi copy sang <path>.<timestamp>.bak va ghi vao list backups."""
    if path.exists():
        bak = path.with_name(f"{path.name}.{_utc_stamp()}.bak")
        shutil.copy2(path, bak)
        backups.append(str(bak))


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Ghi JSON atomic: ghi ra file .tmp roi os.replace de tranh file hong giua chung."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def write_ide_login(
    *,
    access_token,
    refresh_token,
    profile_arn,
    region="us-east-1",
    expires_at,
    client_id="",
    client_secret="",
    auth_method="idc",
    provider="Enterprise",
    cache_dir=None,
    scopes=None,
) -> dict:
    """Ghi token vao cache de Kiro IDE nhan dien la da dang nhap.

    - Tinh clientIdHash = sha1(client_id) neu co client_id.
    - Backup file cu truoc khi ghi (kiro-auth-token.json va <hash>.json).
    - auth_method=='idc' + co client_id => them clientIdHash vao token file.
    - Co client_id & client_secret => ghi them file <hash>.json.
    - Ghi atomic. Tra ve dict CHI chua duong dan, KHONG chua token/secret.
    """
    cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)

    backups: list = []
    client_id_hash = None
    if client_id:
        client_id_hash = hashlib.sha1(client_id.encode()).hexdigest()

    # --- Ghi kiro-auth-token.json ---
    token_path = cache / "kiro-auth-token.json"
    token_payload = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "profileArn": profile_arn,
        "expiresAt": expires_at,
        "region": region,
        "authMethod": auth_method,
        "provider": provider,
    }
    if auth_method == "idc" and client_id:
        token_payload["clientIdHash"] = client_id_hash

    _backup_if_exists(token_path, backups)
    _atomic_write_json(token_path, token_payload)

    # --- Ghi file <clientIdHash>.json (chi khi co ca client_id va client_secret) ---
    client_path = None
    if client_id and client_secret:
        client_path = cache / f"{client_id_hash}.json"
        client_payload = {
            "clientId": client_id,
            "clientSecret": client_secret,
            "expiresAt": expires_at,
            "scopes": scopes or list(DEFAULT_SCOPES),
            "registrationExpiresAt": expires_at,
        }
        _backup_if_exists(client_path, backups)
        _atomic_write_json(client_path, client_payload)

    return {
        "ok": True,
        "tokenPath": str(token_path),
        "clientPath": str(client_path) if client_path else None,
        "backups": backups,
        "clientIdHash": client_id_hash,
    }


def _default_db_path() -> Path:
    """Duong dan DB 9router mac dinh: %APPDATA%/9router/db/data.sqlite."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        # Fallback hop ly tren Windows neu thieu bien moi truong.
        appdata = str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "9router" / "db" / "data.sqlite"


def _iter_kiro_connections(cur):
    """Yield (id, name, data_dict) cho moi connection provider='kiro'."""
    cur.execute(
        "SELECT id, name, data FROM providerConnections WHERE provider='kiro'"
    )
    for cid, name, data in cur.fetchall():
        try:
            d = json.loads(data) if data else {}
        except (ValueError, TypeError):
            d = {}
        yield cid, name, d


def list_kiro_connections(*, db_path=None) -> list:
    """Liet ke cac connection kiro: tra ve list dict {id, name, accountName}. KHONG co token."""
    db = Path(db_path) if db_path is not None else _default_db_path()
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        out = []
        for cid, name, d in _iter_kiro_connections(cur):
            psd = d.get("providerSpecificData", {}) or {}
            out.append(
                {
                    "id": cid,
                    "name": name,
                    "accountName": psd.get("accountName"),
                }
            )
        return out
    finally:
        conn.close()


def write_ide_login_from_9router(connection_id_or_name, *, db_path=None, cache_dir=None) -> dict:
    """Doc token tu DB 9router theo id/name/accountName roi goi write_ide_login.

    Tra ve dict ket qua cua write_ide_login + them khoa 'name'.
    """
    db = Path(db_path) if db_path is not None else _default_db_path()
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        matched = None
        for cid, name, d in _iter_kiro_connections(cur):
            psd = d.get("providerSpecificData", {}) or {}
            account_name = psd.get("accountName")
            if (
                cid == connection_id_or_name
                or name == connection_id_or_name
                or account_name == connection_id_or_name
            ):
                matched = (cid, name, d, psd)
                break
    finally:
        conn.close()

    if matched is None:
        raise ValueError(
            f"Khong tim thay connection kiro khop voi '{connection_id_or_name}'"
        )

    cid, name, d, psd = matched

    result = write_ide_login(
        access_token=d.get("accessToken", ""),
        refresh_token=d.get("refreshToken", ""),
        profile_arn=psd.get("profileArn", ""),
        region=psd.get("region", "us-east-1") or "us-east-1",
        expires_at=d.get("expiresAt", ""),
        client_id=psd.get("clientId", "") or "",
        client_secret=psd.get("clientSecret", "") or "",
        auth_method=psd.get("authMethod") or "idc",
        provider=psd.get("provider") or "Enterprise",
        cache_dir=cache_dir,
    )
    result["name"] = name
    return result


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Ghi token Kiro tu 9router DB vao AWS SSO cache cho IDE Kiro."
    )
    parser.add_argument(
        "--connection",
        help="ID hoac name (hoac accountName) cua connection kiro can ghi.",
    )
    parser.add_argument("--db", help="Duong dan DB 9router (mac dinh %%APPDATA%%/9router/db/data.sqlite).")
    parser.add_argument(
        "--list",
        action="store_true",
        help="Chi liet ke cac connection kiro (id + name + accountName), KHONG in token.",
    )
    args = parser.parse_args()

    if args.list:
        conns = list_kiro_connections(db_path=args.db)
        print(json.dumps({"ok": True, "count": len(conns), "connections": conns}, ensure_ascii=False, indent=2))
        return 0

    if not args.connection:
        parser.error("Can --connection <id|name> hoac --list.")

    result = write_ide_login_from_9router(args.connection, db_path=args.db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
