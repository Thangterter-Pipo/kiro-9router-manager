"""Account store for the Kiro -> 9router importer.

Persists accounts (credentials + MFA secret + metadata) to a local JSON file so
they can be re-logged in later without re-typing. The store lives next to the
executable / repo root so it survives rebuilds.

Schema (one entry per account):
    {
        "id": "<uuid>",
        "name": "user@example.com",       # login email / username
        "password": "<current password>",  # current AWS password (may be rotated)
        "start_url": "https://d-xxxx.awsapps.com/start",
        "region": "us-east-1",
        "mfa_secret": "BASE32SECRET",       # TOTP secret, "" if none
        "new_password": "",                 # password to set on first-login change page
        "note": "",                          # free text
        "last_status": "",                  # "ok" | "error" | ""
        "last_error": "",
        "last_login_at": "",                 # ISO timestamp
        "connection_id": "",                 # 9router connection id after import
        "created_at": "<iso>",
        "updated_at": "<iso>"
    }

The store never prints secrets; callers are responsible for redaction in logs.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _store_root() -> Path:
    """Directory that holds the JSON store. Next to the .exe when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def default_store_path() -> Path:
    override = os.environ.get("KIRO_ACCOUNT_STORE")
    if override:
        return Path(override)
    return _store_root() / "kiro_accounts.json"


@dataclass
class Account:
    name: str
    password: str
    start_url: str
    region: str = "us-east-1"
    mfa_secret: str = ""
    new_password: str = ""
    note: str = ""
    last_status: str = ""
    last_error: str = ""
    last_login_at: str = ""
    connection_id: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        # Backfill required fields defensively.
        filtered.setdefault("name", "")
        filtered.setdefault("password", "")
        filtered.setdefault("start_url", "")
        return cls(**filtered)

    def to_runner_json(self) -> dict[str, str]:
        """Shape expected by the .mjs runner (one account)."""
        return {
            "name": self.name,
            "password": self.password,
            "newPassword": self.new_password or "",
            "startUrl": self.start_url,
            "region": self.region or "us-east-1",
            "mfaSecret": self.mfa_secret or "",
        }

    def masked(self) -> dict[str, Any]:
        """Dict safe for display: secrets reduced to presence flags."""
        d = self.to_dict()
        d["password"] = "********" if self.password else ""
        d["mfa_secret"] = "set" if self.mfa_secret else ""
        d["new_password"] = "set" if self.new_password else ""
        return d


class AccountStore:
    """Thread-safe JSON-backed account store."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_store_path()
        self._lock = threading.RLock()
        self._accounts: list[Account] = []
        self.load()

    # ---- persistence -------------------------------------------------
    def load(self) -> None:
        with self._lock:
            self._accounts = []
            if not self.path.is_file():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return
            items = raw.get("accounts", raw) if isinstance(raw, dict) else raw
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        self._accounts.append(Account.from_dict(item))

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "updated_at": _utcnow_iso(),
                "accounts": [a.to_dict() for a in self._accounts],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    # ---- queries -----------------------------------------------------
    def all(self) -> list[Account]:
        with self._lock:
            return list(self._accounts)

    def get(self, account_id: str) -> Account | None:
        with self._lock:
            return next((a for a in self._accounts if a.id == account_id), None)

    def get_by_name(self, name: str) -> Account | None:
        with self._lock:
            return next((a for a in self._accounts if a.name == name), None)

    def select(self, ids: list[str]) -> list[Account]:
        idset = set(ids)
        with self._lock:
            return [a for a in self._accounts if a.id in idset]

    # ---- mutations ---------------------------------------------------
    def add(self, account: Account) -> Account:
        with self._lock:
            existing = self.get_by_name(account.name)
            if existing:
                # Update in place rather than duplicating by login name.
                return self.update(existing.id, **{
                    k: v for k, v in account.to_dict().items()
                    if k not in {"id", "created_at"}
                })
            self._accounts.append(account)
            self.save()
            return account

    def update(self, account_id: str, **fields: Any) -> Account:
        with self._lock:
            acc = self.get(account_id)
            if not acc:
                raise KeyError(f"account not found: {account_id}")
            for key, value in fields.items():
                if key in {"id", "created_at"}:
                    continue
                if hasattr(acc, key):
                    setattr(acc, key, value)
            acc.updated_at = _utcnow_iso()
            self.save()
            return acc

    def delete(self, account_id: str) -> bool:
        with self._lock:
            before = len(self._accounts)
            self._accounts = [a for a in self._accounts if a.id != account_id]
            changed = len(self._accounts) != before
            if changed:
                self.save()
            return changed

    def upsert_from_line(self, raw_line: str, *, default_region: str = "us-east-1") -> Account | None:
        """Parse a `mail|pass|startUrl[|mfaSecret]` line and add/update it."""
        line = raw_line.strip()
        if not line or line.startswith("#"):
            return None
        if "|" not in line:
            raise ValueError("expected mail|pass|startUrl[|mfaSecret]")
        name, rest = line.split("|", 1)
        segments = rest.split("|")
        url_idx = next((i for i, s in enumerate(segments) if s.strip().startswith("https://")), None)
        if url_idx is None or url_idx == 0:
            raise ValueError("expected mail|pass|startUrl[|mfaSecret]")
        password = "|".join(segments[:url_idx]).strip()
        start_url = segments[url_idx].strip()
        mfa_secret = "|".join(segments[url_idx + 1:]).strip()
        name = name.strip()
        if not name or not password:
            raise ValueError("missing mail or password")
        return self.add(Account(
            name=name, password=password, start_url=start_url,
            region=default_region, mfa_secret=mfa_secret,
        ))

    def record_result(self, account_id: str, *, ok: bool, error: str = "",
                      connection_id: str = "", new_password: str = "",
                      mfa_secret: str = "") -> None:
        """Update an account after a login/relogin attempt."""
        fields: dict[str, Any] = {
            "last_status": "ok" if ok else "error",
            "last_error": "" if ok else error[:500],
            "last_login_at": _utcnow_iso(),
        }
        if connection_id:
            fields["connection_id"] = connection_id
        # Persist rotated password / freshly captured MFA secret so relogin works.
        if new_password:
            fields["password"] = new_password
            fields["new_password"] = ""
        if mfa_secret:
            fields["mfa_secret"] = mfa_secret
        self.update(account_id, **fields)
