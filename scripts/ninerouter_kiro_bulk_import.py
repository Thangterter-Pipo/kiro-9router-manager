"""Bulk import already-authenticated Kiro IDC accounts into local 9router.

This intentionally does NOT support username/password login automation.
Use pre-authorized Kiro/AWS IDC refresh tokens only.

CSV headers:
    name,profileArn,refreshToken,clientId,clientSecret,region,startUrl,provider

Usage:
    python scripts/ninerouter_kiro_bulk_import.py accounts.csv --dry-run
    python scripts/ninerouter_kiro_bulk_import.py accounts.csv --write --verify
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from ninerouter_kiro_login import (
    DEFAULT_BASE_URL,
    DEFAULT_IDC_START_URL,
    KiroLogin,
    _iso,
    _post_json,
    _utcnow,
    default_ninerouter_db_path,
    upsert_sqlite,
    verify_9router,
)


def _read_accounts(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise RuntimeError(f"accounts file not found: {path}")
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise RuntimeError("JSON root must be an array")
        return [item for item in data if isinstance(item, dict)]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _required(row: dict[str, Any], key: str, index: int) -> str:
    value = str(row.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"row {index}: missing {key}")
    return value


def _refresh(row: dict[str, Any], index: int, default_start_url: str) -> KiroLogin:
    profile_arn = _required(row, "profileArn", index)
    refresh_token = _required(row, "refreshToken", index)
    client_id = _required(row, "clientId", index)
    client_secret = _required(row, "clientSecret", index)
    region = str(row.get("region") or "us-east-1").strip() or "us-east-1"
    start_url = str(row.get("startUrl") or default_start_url).strip() or default_start_url
    provider = str(row.get("provider") or "Enterprise").strip() or "Enterprise"
    name = str(row.get("name") or row.get("email") or f"Kiro-{index}").strip() or f"Kiro-{index}"

    refreshed = _post_json(
        f"https://oidc.{region}.amazonaws.com/token",
        {
            "clientId": client_id,
            "clientSecret": client_secret,
            "refreshToken": refresh_token,
            "grantType": "refresh_token",
        },
    )
    access_token = str(refreshed.get("accessToken") or "").strip()
    if not access_token:
        raise RuntimeError(f"row {index}: token refresh returned no accessToken")
    expires_in = int(refreshed.get("expiresIn") or 3600)

    return KiroLogin(
        profile_arn=profile_arn,
        profile_name=name,
        access_token=access_token,
        refresh_token=str(refreshed.get("refreshToken") or refresh_token).strip(),
        expires_at=_iso(_utcnow() + timedelta(seconds=expires_in)),
        expires_in=expires_in,
        provider_data={
            "profileArn": profile_arn,
            "clientId": client_id,
            "clientSecret": client_secret,
            "region": region,
            "authMethod": "idc",
            "startUrl": start_url,
            "provider": provider,
        },
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bulk import Kiro IDC refresh tokens into 9router.")
    parser.add_argument("accounts", type=Path, help="CSV/JSON accounts file")
    parser.add_argument("--db", type=Path, default=default_ninerouter_db_path())
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--start-url", default=DEFAULT_IDC_START_URL)
    parser.add_argument("--dry-run", action="store_true", help="Validate/refresh only. Default.")
    parser.add_argument("--write", action="store_true", help="Write/upsert into 9router DB")
    parser.add_argument("--verify", action="store_true", help="Verify final 9router model list")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    rows = _read_accounts(args.accounts)
    write = bool(args.write)
    items: list[dict[str, Any]] = []
    last_connection_id = ""

    for index, row in enumerate(rows, start=1):
        try:
            login = _refresh(row, index, args.start_url)
            result = upsert_sqlite(args.db, login, write=write)
            last_connection_id = str(result.get("connectionId") or last_connection_id)
            items.append(
                {
                    "row": index,
                    "ok": True,
                    "mode": "write" if write else "dry-run",
                    "action": result.get("action"),
                    "connectionId": result.get("connectionId"),
                    "name": login.profile_name,
                    "profileArnSet": True,
                    "tokenRefreshed": True,
                }
            )
        except Exception as exc:
            items.append({"row": index, "ok": False, "error": str(exc)})

    output: dict[str, Any] = {
        "ok": all(item.get("ok") for item in items),
        "mode": "write" if write else "dry-run",
        "total": len(items),
        "success": sum(1 for item in items if item.get("ok")),
        "failed": sum(1 for item in items if not item.get("ok")),
        "items": items,
    }
    if args.verify and write and last_connection_id:
        output["verify"] = verify_9router(args.base_url, last_connection_id)

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
