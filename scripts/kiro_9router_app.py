"""Local app for importing Kiro IAM Identity Center accounts into 9router.

Input format, one account per line:
    mail|pass|startUrl

Accounts that land on the AWS "set new password" page are assigned the
configured new password, defaulting to ChangeMe@123.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_NEW_PASSWORD = "ChangeMe@123"
DEFAULT_BASE_URL = "http://127.0.0.1:20128"
DEFAULT_REDIRECT_URI = "http://127.0.0.1/oauth/callback"
DEFAULT_REGION = "us-east-1"
PLAYWRIGHT_VERSION = "1.54.2"


@dataclass(frozen=True)
class AccountLine:
    name: str
    password: str
    start_url: str
    region: str
    new_password: str
    mfa_secret: str = ""

    def to_runner_json(self) -> dict[str, str]:
        return {
            "name": self.name,
            "password": self.password,
            "newPassword": self.new_password,
            "startUrl": self.start_url,
            "region": self.region,
            "mfaSecret": self.mfa_secret,
        }


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(str(sys._MEIPASS))
    return Path(__file__).resolve().parents[1]


def _output_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    return _resource_root()


def _default_db() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set; pass --db explicitly")
    return Path(appdata) / "9router" / "db" / "data.sqlite"


def _default_chrome() -> str:
    candidates = [
        Path(os.environ.get("ProgramFiles", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def parse_account_line(raw: str, index: int, *, new_password: str, region: str) -> AccountLine | None:
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if "|" not in line:
        raise ValueError(f"line {index}: expected mail|pass|startUrl[|mfaSecret]")
    # name is everything before the first '|'; the rest may contain '|' inside the password.
    name, rest = line.split("|", 1)
    segments = rest.split("|")
    # Locate the startUrl segment (the one beginning with https://).
    url_idx = next((i for i, s in enumerate(segments) if s.strip().startswith("https://")), None)
    if url_idx is None or url_idx == 0:
        raise ValueError(f"line {index}: expected mail|pass|startUrl[|mfaSecret]")
    password = "|".join(segments[:url_idx]).strip()
    start_url = segments[url_idx].strip()
    mfa_secret = "|".join(segments[url_idx + 1:]).strip()
    name = name.strip()
    if not name:
        raise ValueError(f"line {index}: missing mail/username")
    if not password:
        raise ValueError(f"line {index}: missing password")
    if not start_url.startswith("https://"):
        raise ValueError(f"line {index}: startUrl must start with https://")
    return AccountLine(name=name, password=password, start_url=start_url, region=region, new_password=new_password, mfa_secret=mfa_secret)


def load_accounts(args: argparse.Namespace) -> list[AccountLine]:
    raw_lines: list[str] = []
    if args.input:
        raw_lines.extend(args.input.read_text(encoding="utf-8-sig").splitlines())
    raw_lines.extend(args.line or [])
    if args.stdin:
        raw_lines.extend(sys.stdin.read().splitlines())
    accounts: list[AccountLine] = []
    for index, raw in enumerate(raw_lines, start=1):
        parsed = parse_account_line(raw, index, new_password=args.new_password, region=args.region)
        if parsed:
            accounts.append(parsed)
    if not accounts:
        raise RuntimeError("no accounts provided; use --input, --line, or --stdin")
    return accounts


def _npm_cmd() -> str:
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found; install Node.js/npm or pass PW_NODE_MODULES to an existing playwright-core")
    return npm


def ensure_playwright_core(*, node_modules: Path | None, install: bool) -> Path:
    env_root = os.environ.get("PW_NODE_MODULES") or os.environ.get("PLAYWRIGHT_NODE_MODULES")
    candidates = []
    if node_modules:
        candidates.append(node_modules)
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path(os.environ.get("TEMP", ".")) / "codex-playwright-core" / "node_modules")
    for candidate in candidates:
        if (candidate / "playwright-core").is_dir():
            return candidate
    target = candidates[-1]
    if not install:
        raise RuntimeError(f"playwright-core not found under {target}; rerun without --no-install")
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"Installing playwright-core into {target.parent} ...", file=sys.stderr)
    env = os.environ.copy()
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    subprocess.run(
        [_npm_cmd(), "install", "--prefix", str(target.parent), f"playwright-core@{PLAYWRIGHT_VERSION}", "--no-audit", "--no-fund", "--silent"],
        check=True,
        env=env,
    )
    if not (target / "playwright-core").is_dir():
        raise RuntimeError("playwright-core install finished but package is missing")
    return target


def _db_helper() -> int:
    from scripts.ninerouter_kiro_login import KiroLogin, upsert_sqlite

    obj = json.load(sys.stdin)
    login = KiroLogin(**obj["login"])
    result = upsert_sqlite(Path(obj["db"]), login, write=True)
    print(
        json.dumps(
            {
                "ok": True,
                "name": login.profile_name,
                "action": result.get("action"),
                "connectionId": result.get("connectionId"),
                "backup": result.get("backup"),
                "profileArnSet": bool(login.profile_arn),
                "tokenReceived": True,
            },
            ensure_ascii=False,
        )
    )
    return 0


def run_import(
    args: argparse.Namespace,
    accounts: list[AccountLine],
    node_modules: Path,
    *,
    log_callback: Any | None = None,
) -> dict[str, Any]:
    repo = _repo_root()
    runner = repo / "scripts" / "ninerouter_kiro_idc_auto_import.mjs"
    node = args.node or shutil.which("node")
    if not node:
        raise RuntimeError("node not found")
    chrome = args.chrome or _default_chrome()
    if not chrome or not Path(chrome).is_file():
        raise RuntimeError("Chrome/Edge executable not found; pass --chrome")
    db_path = args.db or _default_db()
    if not db_path.is_file():
        raise RuntimeError(f"9router DB not found: {db_path}")

    payload = json.dumps([account.to_runner_json() for account in accounts], ensure_ascii=False)
    env = os.environ.copy()
    env.update(
        {
            "PW_NODE_MODULES": str(node_modules),
            "PLAYWRIGHT_NODE_MODULES": str(node_modules),
            "CHROME_PATH": chrome,
            "NINEROUTER_DB": str(db_path),
            "NINEROUTER_BASE_URL": args.base_url,
            "KIRO_REDIRECT_URI": args.redirect_uri,
            "KIRO_LOGIN_TIMEOUT_MS": str(int(args.timeout_minutes * 60 * 1000)),
            "AUTOREG_ROOT": str(repo),
            "PYTHON": sys.executable,
        }
    )
    if getattr(sys, "frozen", False):
        env["KIRO_DB_HELPER"] = sys.executable

    proc = subprocess.Popen(
        [node, str(runner)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=repo,
        env=env,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def read_pipe(pipe: Any, chunks: list[str], callback: Any | None) -> None:
        try:
            for line in iter(pipe.readline, ""):
                chunks.append(line)
                if callback:
                    callback(line.rstrip("\n"))
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    stdout_thread = threading.Thread(target=read_pipe, args=(proc.stdout, stdout_chunks, None), daemon=True)
    stderr_thread = threading.Thread(target=read_pipe, args=(proc.stderr, stderr_chunks, log_callback), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    assert proc.stdin is not None
    proc.stdin.write(payload)
    proc.stdin.close()
    try:
        return_code = proc.wait(timeout=int(args.timeout_minutes * 60 * max(1, len(accounts)) + 180))
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise RuntimeError("runner timed out") from exc
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    if stderr and not log_callback:
        sys.stderr.write(stderr)
    if return_code != 0:
        raise RuntimeError(f"runner failed with exit code {return_code}: {stdout[-1000:]}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"runner returned non-JSON output: {stdout[-1000:]}") from exc


def write_report(result: dict[str, Any], *, report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = report_dir / f"kiro_9router_import_{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def print_summary(result: dict[str, Any], report: Path | None) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if report:
        print(f"Report: {report}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Kiro accounts into local 9router from mail|pass|startUrl lines.")
    parser.add_argument("--input", type=Path, help="Text file with one mail|pass|startUrl per line")
    parser.add_argument("--line", action="append", help="Single mail|pass|startUrl account line; may repeat")
    parser.add_argument("--stdin", action="store_true", help="Read account lines from stdin")
    parser.add_argument("--new-password", default=DEFAULT_NEW_PASSWORD, help="Password to set on AWS first-login password-change pages")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    parser.add_argument("--chrome", help="Chrome/Edge executable path")
    parser.add_argument("--node", help="Node.js executable path")
    parser.add_argument("--playwright-node-modules", type=Path, help="node_modules path containing playwright-core")
    parser.add_argument("--no-install", action="store_true", help="Do not auto-install playwright-core into temp")
    parser.add_argument("--timeout-minutes", type=float, default=15)
    parser.add_argument("--dry-parse", action="store_true", help="Only parse input; do not open browser or write DB")
    parser.add_argument("--no-report", action="store_true", help="Do not save JSON report under artifacts")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if argv and argv[0] == "--db-helper":
        return _db_helper()
    args = parse_args(argv)
    accounts = load_accounts(args)
    if args.dry_parse:
        print(json.dumps({"ok": True, "accounts": [account.name for account in accounts], "count": len(accounts)}, ensure_ascii=False, indent=2))
        return 0
    node_modules = ensure_playwright_core(node_modules=args.playwright_node_modules, install=not args.no_install)
    result = run_import(args, accounts, node_modules)
    report = None if args.no_report else write_report(result, report_dir=_output_root() / "artifacts")
    print_summary(result, report)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
