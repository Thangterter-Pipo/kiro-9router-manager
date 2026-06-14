#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Đăng nhập Kiro qua DEVICE-FLOW OIDC (Builder ID / IAM Identity Center).

Đây là cách đăng nhập KHÔNG cần password/MFA/automation trình duyệt: chương
trình đăng ký một OIDC client, xin mã thiết bị (device code), hiển thị một
đường link + mã ngắn cho người dùng. Người dùng mở link, bấm "Allow", rồi
chương trình poll để nhận accessToken + refreshToken THẬT (sống lâu, refresh
được vĩnh viễn). Tham khảo logic từ repo Kiro-Go (auth/builderid.go, oidc.go).

Hai chế độ:
  - Builder ID:  startUrl = https://view.awsapps.com/start (tài khoản AWS Builder ID cá nhân)
  - IdC/SSO:     startUrl = https://<dir>.awsapps.com/start (IAM Identity Center doanh nghiệp)

Luồng:
  1. POST {oidc}/client/register  -> clientId, clientSecret
  2. POST {oidc}/device_authorization -> deviceCode, userCode, verificationUriComplete, interval
  3. (người dùng mở link, bấm Allow)
  4. POST {oidc}/token grantType=device_code (lặp tới khi xong) -> accessToken, refreshToken, expiresIn

Module KHÔNG in token/secret ra màn hình.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

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
BUILDER_ID_START_URL = "https://view.awsapps.com/start"

# Scope CodeWhisperer mà Kiro IDE dùng.
SCOPES = [
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
    "codewhisperer:transformations",
    "codewhisperer:taskassist",
]

GRANT_TYPES = ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"]


def _oidc_base(region: str) -> str:
    return f"https://oidc.{region}.amazonaws.com"


def register_client(region: str, start_url: str, client_name: str = "Kiro") -> dict:
    """Đăng ký OIDC client public, trả {clientId, clientSecret, ...}."""
    return _post_json(
        f"{_oidc_base(region)}/client/register",
        {
            "clientName": client_name,
            "clientType": "public",
            "scopes": SCOPES,
            "grantTypes": GRANT_TYPES,
            "issuerUrl": start_url,
        },
    )


def start_device_authorization(region: str, client_id: str, client_secret: str, start_url: str) -> dict:
    """Xin device code. Trả {deviceCode, userCode, verificationUri(Complete), interval, expiresIn}."""
    return _post_json(
        f"{_oidc_base(region)}/device_authorization",
        {"clientId": client_id, "clientSecret": client_secret, "startUrl": start_url},
    )


def begin_login(region: str = DEFAULT_REGION, start_url: str = BUILDER_ID_START_URL) -> dict:
    """Bắt đầu device-flow: register + device_authorization.

    Trả dict gồm clientId/clientSecret/deviceCode + link cho người dùng mở.
    """
    region = region or DEFAULT_REGION
    start_url = start_url or BUILDER_ID_START_URL
    reg = register_client(region, start_url)
    client_id = str(reg.get("clientId") or "").strip()
    client_secret = str(reg.get("clientSecret") or "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("đăng ký client thất bại (thiếu clientId/clientSecret)")

    auth = start_device_authorization(region, client_id, client_secret, start_url)
    device_code = str(auth.get("deviceCode") or "").strip()
    user_code = str(auth.get("userCode") or "").strip()
    verification_uri = str(auth.get("verificationUriComplete")
                           or auth.get("verificationUri") or "").strip()
    interval = int(auth.get("interval") or 5)
    expires_in = int(auth.get("expiresIn") or 600)
    if not device_code or not verification_uri:
        raise RuntimeError("xin device code thất bại")

    return {
        "region": region, "start_url": start_url,
        "client_id": client_id, "client_secret": client_secret,
        "device_code": device_code, "user_code": user_code,
        "verification_uri": verification_uri,
        "interval": max(interval, 1), "expires_in": expires_in,
        "deadline": time.time() + expires_in,
    }


def poll_once(session: dict) -> dict:
    """Poll một lần. Trả {status: pending|slow_down|completed, ...token nếu completed}."""
    region = session["region"]
    resp, status_code = _post_json_status(
        f"{_oidc_base(region)}/token",
        {
            "clientId": session["client_id"],
            "clientSecret": session["client_secret"],
            "grantType": "urn:ietf:params:oauth:grant-type:device_code",
            "deviceCode": session["device_code"],
        },
    )
    if status_code == 200:
        return {
            "status": "completed",
            "access_token": str(resp.get("accessToken") or "").strip(),
            "refresh_token": str(resp.get("refreshToken") or "").strip(),
            "expires_in": int(resp.get("expiresIn") or 3600),
        }
    err = str(resp.get("error") or "").strip()
    if err == "authorization_pending":
        return {"status": "pending"}
    if err == "slow_down":
        session["interval"] = session.get("interval", 5) + 5
        return {"status": "slow_down"}
    if err in ("expired_token", "access_denied", "invalid_grant"):
        raise RuntimeError(f"đăng nhập thất bại: {err}")
    raise RuntimeError(f"lỗi không rõ ({status_code}): {err or resp}")


def _post_json_status(url: str, payload: dict) -> tuple[dict, int]:
    """Như _post_json nhưng trả cả (json, status_code) và KHÔNG raise khi 4xx."""
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            return (json.loads(body) if body else {}), r.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"error": body}
        return parsed, exc.code


def wait_for_token(session: dict, *, on_tick: Callable[[int], None] | None = None,
                   should_cancel: Callable[[], bool] | None = None) -> dict:
    """Poll cho tới khi người dùng Allow hoặc hết hạn. Trả token dict."""
    while True:
        if should_cancel and should_cancel():
            raise RuntimeError("đã hủy")
        if time.time() > session["deadline"]:
            raise RuntimeError("hết thời gian chờ phê duyệt (device code expired)")
        result = poll_once(session)
        if result["status"] == "completed":
            return result
        wait = session.get("interval", 5)
        if on_tick:
            on_tick(wait)
        time.sleep(wait)


def fetch_profile_arn(access_token: str, region: str = DEFAULT_REGION) -> str:
    """Gọi CodeWhisperer ListAvailableProfiles để lấy profileArn từ accessToken.

    Trả profileArn đầu tiên, hoặc "" nếu không lấy được.
    """
    import urllib.error
    import urllib.request

    url = f"https://codewhisperer.{region}.amazonaws.com/"
    payload = json.dumps({"maxResults": 10}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-amz-json-1.0")
    req.add_header("X-Amz-Target", "AWSCodeWhispererService.ListAvailableProfiles")
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
        data = json.loads(body) if body else {}
        profiles = data.get("profiles") or []
        if profiles:
            return str(profiles[0].get("arn") or profiles[0].get("profileArn") or "").strip()
    except Exception:
        pass
    return ""


def finalize_login(
    session: dict,
    token: dict,
    *,
    name: str = "",
    targets: tuple[str, ...] = ("9router",),
    db_path: Path | None = None,
    cache_dir: Path | None = None,
    verify: bool = False,
    base_url: str = "http://127.0.0.1:20128",
) -> dict:
    """Sau khi có token, nạp vào 9router và/hoặc IDE cache."""
    db_path = db_path or default_ninerouter_db_path()
    access_token = token["access_token"]
    refresh_token = token["refresh_token"]
    expires_in = int(token.get("expires_in") or 3600)
    auth_method = "builderId" if session["start_url"] == BUILDER_ID_START_URL else "idc"
    name = name or (f"BuilderID-{int(time.time())}" if auth_method == "builderId"
                    else f"IdC-{int(time.time())}")

    # Lấy profileArn qua ListAvailableProfiles của CodeWhisperer.
    profile_arn = fetch_profile_arn(access_token, session["region"])
    login = KiroLogin(
        profile_arn=profile_arn,
        profile_name=name,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=_iso(_utcnow() + timedelta(seconds=expires_in)),
        expires_in=expires_in,
        provider_data={
            "accountName": name,
            "profileArn": profile_arn,
            "clientId": session["client_id"],
            "clientSecret": session["client_secret"],
            "region": session["region"],
            "authMethod": auth_method,
            "startUrl": session["start_url"],
            "provider": "AWS Builder ID" if auth_method == "builderId" else "IAM Identity Center",
        },
    )
    result: dict[str, Any] = {"ok": False, "name": name, "authMethod": auth_method,
                              "hasRefresh": bool(refresh_token), "targets": list(targets)}
    if "9router" in targets:
        res = upsert_sqlite(db_path, login, write=True)
        result["connectionId"] = res.get("connectionId")
        result["action"] = res.get("action")
    if "ide" in targets:
        if kiro_ide_login is None:
            raise RuntimeError("thiếu module kiro_ide_login")
        ide = kiro_ide_login.write_ide_login(
            access_token=access_token, refresh_token=refresh_token,
            profile_arn=login.profile_arn, region=session["region"],
            expires_at=login.expires_at, client_id=session["client_id"],
            client_secret=session["client_secret"], auth_method=auth_method,
            provider=login.provider_data["provider"], cache_dir=cache_dir,
        )
        result["idePath"] = ide.get("tokenPath")
    result["ok"] = True
    if verify and "9router" in targets and result.get("connectionId"):
        try:
            result["verify"] = verify_9router(base_url, str(result["connectionId"]))
        except Exception as exc:
            result["verify"] = {"error": str(exc)}
    return result


def login_device_flow(
    *, region: str = DEFAULT_REGION, start_url: str = BUILDER_ID_START_URL,
    name: str = "", targets: tuple[str, ...] = ("9router",), open_browser: bool = True,
    db_path: Path | None = None, on_prompt: Callable[[dict], None] | None = None,
    on_tick: Callable[[int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None, verify: bool = False,
) -> dict:
    """Chạy trọn vẹn device-flow (dùng cho CLI và GUI)."""
    session = begin_login(region, start_url)
    if on_prompt:
        on_prompt(session)
    if open_browser:
        try:
            webbrowser.open(session["verification_uri"])
        except Exception:
            pass
    token = wait_for_token(session, on_tick=on_tick, should_cancel=should_cancel)
    return finalize_login(session, token, name=name, targets=targets,
                          db_path=db_path, verify=verify)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Đăng nhập Kiro qua device-flow (Builder ID / IdC).")
    parser.add_argument("--start-url", default=BUILDER_ID_START_URL,
                        help="startUrl (Builder ID mặc định, hoặc https://<dir>.awsapps.com/start cho IdC)")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--name", default="", help="Tên hiển thị cho tài khoản")
    parser.add_argument("--targets", default="9router", help="9router,ide")
    parser.add_argument("--no-browser", action="store_true", help="Không tự mở trình duyệt")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--begin-only", action="store_true", help="Chỉ lấy link+code rồi thoát (test)")
    args = parser.parse_args()

    if args.begin_only:
        session = begin_login(args.region, args.start_url)
        print(json.dumps({"ok": True, "userCode": session["user_code"],
                          "verificationUri": session["verification_uri"],
                          "expiresIn": session["expires_in"],
                          "hasClient": bool(session["client_id"] and session["client_secret"])},
                         ensure_ascii=False, indent=2))
        return 0

    targets = tuple(t.strip() for t in args.targets.split(",") if t.strip())

    def on_prompt(s: dict) -> None:
        print(f"\n>>> Mở link sau và bấm Allow để đăng nhập:\n    {s['verification_uri']}")
        print(f"    Mã xác thực: {s['user_code']}\n")

    result = login_device_flow(region=args.region, start_url=args.start_url, name=args.name,
                               targets=targets, open_browser=not args.no_browser,
                               on_prompt=on_prompt, verify=args.verify)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
