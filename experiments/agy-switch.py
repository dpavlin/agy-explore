#!/usr/bin/env python3
"""
agy-switch.py - Lightweight In-Place Google Login Switcher for Antigravity CLI.

Instead of overriding $HOME, this script stores named OAuth tokens in
~/.config/agy-accounts/ and copies the active token directly into ~/.gemini/
when you want to switch accounts.

Commands:
  agy-switch.py list                 # List saved accounts and active one
  agy-switch.py save <name>          # Save current live auth (keyring/file) as <name>
  agy-switch.py switch <name>        # Activate account <name> in ~/.gemini
  agy-switch.py login <name>         # Launch agy in isolated temp dir, then save auth
  agy-switch.py whoami               # Show currently active Google identity in ~/.gemini
"""

import os
import sys
import json
import base64
import shutil
import tempfile
import argparse
import urllib.request
import re
from datetime import datetime, timezone
from pathlib import Path

ACCOUNTS_DIR = Path.home() / ".config" / "agy-accounts"
REAL_HOME = Path.home()
LIVE_GEMINI = REAL_HOME / ".gemini"

DEBUG = False


def log_debug(msg: str) -> None:
    if DEBUG:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        print(f"[{ts}] [DEBUG] {msg}", file=sys.stderr)


def decode_jwt(jwt_str: str) -> dict | None:
    parts = jwt_str.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
        return json.loads(decoded.decode("utf-8", errors="ignore"))
    except Exception as exc:
        log_debug(f"JWT decode error: {exc}")
        return None


def fetch_google_userinfo(access_token: str) -> dict | None:
    if not access_token:
        return None
    req = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": "agy-switch"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as exc:
        log_debug(f"Failed to fetch Google UserInfo: {exc}")
        return None


def get_keyring_antigravity_token() -> str | None:
    try:
        import dbus  # type: ignore

        bus = dbus.SessionBus()
        service = bus.get_object("org.freedesktop.secrets", "/org/freedesktop/secrets")
        collection = bus.get_object("org.freedesktop.secrets", "/org/freedesktop/secrets/aliases/default")
        items = collection.Get(
            "org.freedesktop.Secret.Collection", "Items", dbus_interface="org.freedesktop.DBus.Properties"
        )
        sec_svc = dbus.Interface(service, "org.freedesktop.Secret.Service")
        _, session_path = sec_svc.OpenSession("plain", dbus.String("", variant_level=1))

        for item_path in items:
            item = bus.get_object("org.freedesktop.secrets", item_path)
            attrs = item.Get(
                "org.freedesktop.Secret.Item", "Attributes", dbus_interface="org.freedesktop.DBus.Properties"
            )
            if attrs.get("service") == "gemini" and attrs.get("username") == "antigravity":
                secret_struct = item.GetSecret(session_path, dbus_interface="org.freedesktop.Secret.Item")
                return bytes(secret_struct[2]).decode("utf-8")
    except Exception as exc:
        log_debug(f"Could not read from SecretService: {exc}")
    return None


def get_token_identity(token_path: Path, raw_token_str: str | None = None) -> dict:
    if raw_token_str:
        try:
            data = json.loads(raw_token_str)
        except Exception as exc:
            return {"email": None, "status": f"invalid_json: {exc}"}
    elif token_path.is_file():
        try:
            data = json.loads(token_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"email": None, "status": f"invalid_json: {exc}"}
    else:
        return {"email": None, "status": "no_token"}

    email = None
    expiry = None
    access_token = None

    if isinstance(data, dict):
        token = data.get("token")
        if isinstance(token, dict):
            expiry = token.get("expiry")
            access_token = token.get("access_token")
            for key in ("id_token", "access_token"):
                jwt = token.get(key)
                if isinstance(jwt, str) and jwt.count(".") >= 2:
                    claims = decode_jwt(jwt)
                    if claims and "email" in claims:
                        email = claims["email"]
                        break
        if not email and "id_token" in data:
            jwt = data.get("id_token")
            if isinstance(jwt, str) and jwt.count(".") >= 2:
                claims = decode_jwt(jwt)
                if claims and "email" in claims:
                    email = claims["email"]
        if not access_token and "access_token" in data:
            access_token = data.get("access_token")

    if not email and access_token:
        info = fetch_google_userinfo(access_token)
        if info and "email" in info:
            email = info["email"]

    return {"email": email, "expiry": expiry, "status": "authenticated" if email else "valid_token"}


def live_token_path() -> Path:
    candidates = [
        LIVE_GEMINI / "antigravity-cli" / "antigravity-oauth-token",
        LIVE_GEMINI / "oauth_creds.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


def saved_token_path(name: str) -> Path:
    return ACCOUNTS_DIR / f"{name}.json"


def cmd_list(args) -> int:
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    # Check live identity from keyring first, then file
    keyring_token = get_keyring_antigravity_token()
    if keyring_token:
        live_info = get_token_identity(Path(""), raw_token_str=keyring_token)
    else:
        live_info = get_token_identity(live_token_path())
    live_email = live_info.get("email")

    files = sorted(ACCOUNTS_DIR.glob("*.json"))
    if not files:
        print(f"No saved accounts in {ACCOUNTS_DIR}")
        print("To save current account:  agy-switch.py save <name>")
        print("To log in a new account:  agy-switch.py login <name>")
        return 0

    print(f"{'ACCOUNT':<18} {'ACTIVE':<8} {'GOOGLE ACCOUNT':<32} {'STATUS'}")
    print("-" * 70)
    for f in files:
        name = f.stem
        info = get_token_identity(f)
        email = info.get("email") or "(unknown)"
        is_active = "*" if email and email == live_email else ""
        print(f"{name:<18} {is_active:<8} {email:<32} {info.get('status')}")
    return 0


def cmd_save(args) -> int:
    name = args.name
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    target = saved_token_path(name)

    keyring_tok = get_keyring_antigravity_token()
    if keyring_tok:
        target.write_text(keyring_tok, encoding="utf-8")
        log_debug("Saved token from system keyring")
    else:
        src = live_token_path()
        if not src.is_file():
            print(f"Error: No active token found in keyring or {src}", file=sys.stderr)
            return 1
        shutil.copy2(src, target)

    info = get_token_identity(target)
    print(f"Saved active login to account '{name}' ({info.get('email') or 'saved'})")
    return 0


def cmd_switch(args) -> int:
    name = args.name
    src = saved_token_path(name)
    if not src.is_file():
        print(f"Error: Saved account '{name}' not found at {src}", file=sys.stderr)
        return 1

    dst = LIVE_GEMINI / "antigravity-cli" / "antigravity-oauth-token"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    log_debug(f"Copied {src} -> {dst}")

    # Also update cache marker so keyring does not conflict
    marker = LIVE_GEMINI / "antigravity-cli" / "cache" / "antigravity-keyring-unavailable"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    info = get_token_identity(dst)
    print(f"Switched active agy account to '{name}' ({info.get('email') or 'active'})")
    return 0


def cmd_login(args) -> int:
    name = args.name
    agy_bin = shutil.which("agy") or str(REAL_HOME / ".local" / "bin" / "agy")

    with tempfile.TemporaryDirectory(prefix="agy_login_") as tmpdir:
        tmphome = Path(tmpdir)
        cache_dir = tmphome / ".gemini" / "antigravity-cli" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "antigravity-keyring-unavailable").touch()

        print(f"Launching login session for account '{name}'...")
        print("Authenticate in the browser, then exit agy (/exit) to complete setup.\n")
        sys.stdout.flush()

        os.system(f'HOME="{tmphome}" "{agy_bin}"')

        token_file = tmphome / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        if not token_file.is_file():
            print("Error: No oauth token was generated during login session.", file=sys.stderr)
            return 1

        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        target = saved_token_path(name)
        shutil.copy2(token_file, target)
        info = get_token_identity(target)
        print(f"[OK] Account '{name}' saved successfully ({info.get('email')}).")
        print(f"To activate now: agy-switch.py switch {name}")
    return 0


def cmd_whoami(args) -> int:
    keyring_tok = get_keyring_antigravity_token()
    if keyring_tok:
        info = get_token_identity(Path(""), raw_token_str=keyring_tok)
        print("Source: System Keyring (SecretService)")
    else:
        info = get_token_identity(live_token_path())
        print(f"Source File: {live_token_path()}")
    print(f"Email: {info.get('email') or '(none)'}")
    print(f"Status: {info.get('status')}")
    print(f"Expiry: {info.get('expiry') or 'N/A'}")
    return 0


def main():
    global DEBUG
    parser = argparse.ArgumentParser(description="Lightweight Google Login Switcher for agy")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    subparsers = parser.add_subparsers(dest="subcommand")

    subparsers.add_parser("list", aliases=["ls"], help="List saved accounts")
    subparsers.add_parser("whoami", help="Show currently active Google account")

    p_save = subparsers.add_parser("save", help="Save current login as named account")
    p_save.add_argument("name", help="Account name")

    p_switch = subparsers.add_parser("switch", help="Switch active account")
    p_switch.add_argument("name", help="Account name")

    p_login = subparsers.add_parser("login", help="Log in a new account and save it")
    p_login.add_argument("name", help="Account name")

    args = parser.parse_args()
    if args.debug:
        DEBUG = True

    if not args.subcommand or args.subcommand in ("list", "ls"):
        return cmd_list(args)
    elif args.subcommand == "save":
        return cmd_save(args)
    elif args.subcommand == "switch":
        return cmd_switch(args)
    elif args.subcommand == "login":
        return cmd_login(args)
    elif args.subcommand == "whoami":
        return cmd_whoami(args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
