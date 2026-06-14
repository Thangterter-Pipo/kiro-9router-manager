#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Đăng nhập Kiro bằng token dạng JSON (dán thẳng / nạp từ file).

Nhiều tài khoản Kiro mua về được giao dưới dạng JSON token sẵn (không cần
đăng nhập qua trình duyệt). Module này nhận JSON đó, chuẩn hoá, làm mới token
nếu có thể, rồi nạp vào 9router (DB) và/hoặc AWS SSO cache (để mở IDE Kiro là
đã đăng nhập).

JSON chấp nhận các dạng:
  1. Một object kiểu kiro-auth-token.json:
     {"accessToken","refreshToken","profileArn","expiresAt","authMethod",
      "provider","region"?, "clientId"?, "clientSecret"?, "startUrl"?, "name"?}
  2. Một object kiểu IDC export:
     {"name","profileArn","refreshToken","clientId","clientSecret","region",
      "startUrl","provider"}
  3. Một mảng gồm nhiều object như trên.
  4. Một object bao ngoài: {"accounts":[...]} hoặc {"connections":[...]}.

Module KHÔNG in token/secret ra màn hình.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.ninerouter_kiro_login import (
    KiroLogin,
    _iso,
    _post_json,
    _utcnow,
    default_ninerouter_db_path,
    upsert_sqlite,
    verify_9router,
)

try:
    from scripts import kiro_ide_login
except Exception:  # pragma: no cover
    kiro_ide_login = None


DEFAULT_REGION = "us-east-1"
DEFAULT_START_URL = "https://view.awsapps.com/start"


def _first(d: dict, *keys: str, default: str = "") -> str:
    """Lấy giá trị đầu tiên không rỗng trong các khoá (hỗ trợ nhiều biến thể tên)."""
    for k in keys:
        v = d.get(k)
        if v:
            return str(v).strip()
    return default


def extract_entries(raw: Any) -> list[dict]:
    """Trả về danh sách object token từ JSON đã parse (mọi dạng hỗ trợ)."""
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for key in ("accounts", "connections", "items", "data"):
            inner = raw.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        return [raw]
    raise ValueError("JSON phải là object hoặc mảng các object token")


def parse_json_text(text: str) -> list[dict]:
    text = (text or "").strip()
    if not text:
        raise ValueError("JSON rỗng")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON không hợp lệ: {exc}") from exc
    return extract_entries(raw)


def _normalize(entry: dict, index: int) -> dict:
    """Chuẩn hoá 1 object về các khoá thống nhất. Đọc cả providerSpecificData nếu có."""
    psd = entry.get("providerSpecificData")
    psd = psd if isinstance(psd, dict) else {}
    merged = {**psd, **entry}  # khoá gốc ưu tiên hơn providerSpecificData

    access_token = _first(merged, "accessToken", "access_token")
    refresh_token = _first(merged, "refreshToken", "refresh_token")
    profile_arn = _first(merged, "profileArn", "profile_arn", "arn")
    client_id = _first(merged, "clientId", "client_id")
    client_secret = _first(merged, "clientSecret", "client_secret")
    region = _first(merged, "region", default=DEFAULT_REGION) or DEFAULT_REGION
    start_url = _first(merged, "startUrl", "start_url", default=DEFAULT_START_URL) or DEFAULT_START_URL
    auth_method = _first(merged, "authMethod", "auth_method", default="idc") or "idc"
    provider = _first(merged, "provider", default="Enterprise") or "Enterprise"
    name = _first(merged, "name", "accountName", "email", "profileName",
                  default=f"Kiro-JSON-{index}") or f"Kiro-JSON-{index}"
    expires_at = _first(merged, "expiresAt", "expires_at")

    if not refresh_token and not access_token:
        raise ValueError(f"mục {index}: thiếu cả accessToken lẫn refreshToken")
    if not profile_arn:
        raise ValueError(f"mục {index}: thiếu profileArn")

    return {
        "name": name, "access_token": access_token, "refresh_token": refresh_token,
        "profile_arn": profile_arn, "client_id": client_id, "client_secret": client_secret,
        "region": region, "start_url": start_url, "auth_method": auth_method,
        "provider": provider, "expires_at": expires_at,
    }


def _refresh_token(norm: dict) -> tuple[str, str, int]:
    """Làm mới token. Trả (access_token, refresh_token, expires_in).

    - IDC (có clientId+clientSecret+refreshToken): gọi OIDC /token grantType=refresh_token.
    - Social (chỉ refreshToken): gọi endpoint refresh của Kiro desktop.
    - Không refresh được: dùng access_token sẵn có (nếu có).
    """
    region = norm["region"]
    rt = norm["refresh_token"]
    cid = norm["client_id"]
    csec = norm["client_secret"]

    # IDC refresh
    if rt and cid and csec:
        refreshed = _post_json(
            f"https://oidc.{region}.amazonaws.com/token",
            {"clientId": cid, "clientSecret": csec, "refreshToken": rt, "grantType": "refresh_token"},
        )
        at = str(refreshed.get("accessToken") or "").strip()
        if at:
            return at, str(refreshed.get("refreshToken") or rt).strip(), int(refreshed.get("expiresIn") or 3600)

    # Social refresh (Kiro desktop auth)
    if rt and norm["auth_method"] == "social":
        try:
            refreshed = _post_json(
                "https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken",
                {"refreshToken": rt},
            )
            at = str(refreshed.get("accessToken") or "").strip()
            if at:
                return at, str(refreshed.get("refreshToken") or rt).strip(), int(refreshed.get("expiresIn") or 3600)
        except Exception:
            pass

    # Fallback: dùng access token sẵn có
    if norm["access_token"]:
        return norm["access_token"], rt, 3600
    raise RuntimeError(f"{norm['name']}: không làm mới được token và không có accessToken sẵn")


def _build_login(norm: dict, *, refresh: bool) -> KiroLogin:
    if refresh:
        access_token, refresh_token, expires_in = _refresh_token(norm)
    else:
        access_token = norm["access_token"] or ""
        refresh_token = norm["refresh_token"]
        expires_in = 3600
        if not access_token:
            access_token, refresh_token, expires_in = _refresh_token(norm)
    return KiroLogin(
        profile_arn=norm["profile_arn"],
        profile_name=norm["name"],
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=norm["expires_at"] or _iso(_utcnow() + timedelta(seconds=expires_in)),
        expires_in=expires_in,
        provider_data={
            "profileArn": norm["profile_arn"],
            "accountName": norm["name"],
            "clientId": norm["client_id"],
            "clientSecret": norm["client_secret"],
            "region": norm["region"],
            "authMethod": norm["auth_method"],
            "startUrl": norm["start_url"],
            "provider": norm["provider"],
        },
    )


def login_from_json(
    text: str,
    *,
    targets: tuple[str, ...] = ("9router",),
    refresh: bool = True,
    db_path: Path | None = None,
    cache_dir: Path | None = None,
    verify: bool = False,
    base_url: str = "http://127.0.0.1:20128",
) -> dict:
    """Nạp 1 hoặc nhiều token JSON vào các đích chỉ định.

    targets: tập con của {"9router", "ide"}.
    Trả dict {ok, total, success, failed, results:[...], verify?}.
    """
    db_path = db_path or default_ninerouter_db_path()
    entries = parse_json_text(text)
    results: list[dict] = []
    last_conn = ""

    for index, entry in enumerate(entries, start=1):
        item: dict[str, Any] = {"index": index, "ok": False}
        try:
            norm = _normalize(entry, index)
            item["name"] = norm["name"]
            login = _build_login(norm, refresh=refresh)

            if "9router" in targets:
                res = upsert_sqlite(db_path, login, write=True)
                item["connectionId"] = res.get("connectionId")
                last_conn = str(res.get("connectionId") or last_conn)
                item["action"] = res.get("action")

            if "ide" in targets:
                if kiro_ide_login is None:
                    raise RuntimeError("thiếu module kiro_ide_login")
                ide = kiro_ide_login.write_ide_login(
                    access_token=login.access_token,
                    refresh_token=login.refresh_token,
                    profile_arn=login.profile_arn,
                    region=norm["region"],
                    expires_at=login.expires_at,
                    client_id=norm["client_id"],
                    client_secret=norm["client_secret"],
                    auth_method=norm["auth_method"],
                    provider=norm["provider"],
                    cache_dir=cache_dir,
                )
                item["idePath"] = ide.get("tokenPath")

            item["ok"] = True
            item["refreshToken"] = bool(login.refresh_token)
        except Exception as exc:
            item["error"] = str(exc)
        results.append(item)

    output: dict[str, Any] = {
        "ok": all(r["ok"] for r in results) and bool(results),
        "total": len(results),
        "success": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "targets": list(targets),
        "results": results,
    }
    if verify and "9router" in targets and last_conn:
        try:
            output["verify"] = verify_9router(base_url, last_conn)
        except Exception as exc:
            output["verify"] = {"error": str(exc)}
    return output


def _main() -> int:
    parser = argparse.ArgumentParser(description="Đăng nhập Kiro bằng token JSON vào 9router và/hoặc IDE.")
    parser.add_argument("--file", type=Path, help="File JSON chứa token (1 object hoặc mảng)")
    parser.add_argument("--stdin", action="store_true", help="Đọc JSON từ stdin")
    parser.add_argument("--targets", default="9router", help="Đích, ngăn bằng dấu phẩy: 9router,ide")
    parser.add_argument("--no-refresh", action="store_true", help="Không làm mới token, dùng nguyên access token")
    parser.add_argument("--db", type=Path, help="Đường dẫn 9router DB")
    parser.add_argument("--verify", action="store_true", help="Gọi API 9router xác minh sau khi nạp")
    parser.add_argument("--dry-parse", action="store_true", help="Chỉ parse JSON, không nạp")
    args = parser.parse_args()

    if args.file:
        text = args.file.read_text(encoding="utf-8-sig")
    elif args.stdin:
        text = sys.stdin.read()
    else:
        parser.error("cần --file hoặc --stdin")

    if args.dry_parse:
        entries = parse_json_text(text)
        norm = [_normalize(e, i) for i, e in enumerate(entries, start=1)]
        print(json.dumps({"ok": True, "count": len(norm),
                          "accounts": [{"name": n["name"], "authMethod": n["auth_method"],
                                        "hasRefresh": bool(n["refresh_token"]),
                                        "hasClient": bool(n["client_id"] and n["client_secret"])}
                                       for n in norm]}, ensure_ascii=False, indent=2))
        return 0

    targets = tuple(t.strip() for t in args.targets.split(",") if t.strip())
    result = login_from_json(text, targets=targets, refresh=not args.no_refresh,
                            db_path=args.db, verify=args.verify)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
