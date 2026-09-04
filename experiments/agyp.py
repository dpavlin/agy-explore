#!/usr/bin/env python3
"""
agyp.py - Lightweight Antigravity CLI (agy) Profile Manager & Wrapper.

Features:
- Zero external dependencies (Python 3.10+ standard library only).
- Isolated profile environments (~/.config/agy-profiles/<profile>).
- Bypasses system GNOME keyring via `antigravity-keyring-unavailable` to guarantee
  per-profile OAuth credential isolation.
- Supports importing live credentials from system GNOME Keyring / SecretService.
- Resolves Google account identity via JWT claims, local logs, and Google UserInfo API.
- Automatically links ~/.gitconfig, ~/.ssh, and ~/.vimrc into profile home so git and ssh work seamlessly.
- Commands: list, login, run, use (set default), whoami, import-current, remove.
- Includes --debug flag for verbose diagnostics (timestamps, paths, environment).

Usage:
  agyp.py <profile> [agy-options...]       # Run agy with specified profile
  agyp.py [agy-options...]                 # Run agy with default active profile
  agyp.py list                             # List profiles and Google account emails
  agyp.py login <profile>                  # Authenticate a profile with Google OAuth
  agyp.py use <profile>                    # Set default profile
  agyp.py whoami [profile]                 # Show identity/email of profile
  agyp.py import-current <profile>         # Import current live login (keyring/file) into profile
  agyp.py --debug <profile> ...            # Enable debug diagnostics
"""

import os
import sys
import json
import base64
import shutil
import re
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PROFILES_DIR = Path.home() / ".config" / "agy-profiles"
DEFAULT_STATE_FILE = DEFAULT_PROFILES_DIR / ".state.json"
REAL_HOME = Path.home()

DEBUG = False


def log_debug(msg: str) -> None:
    if DEBUG:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        print(f"[{ts}] [DEBUG] {msg}", file=sys.stderr)


def get_profiles_dir() -> Path:
    env_dir = os.environ.get("AGY_PROFILES_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return DEFAULT_PROFILES_DIR


def get_profile_home(profile_name: str) -> Path:
    return get_profiles_dir() / profile_name


def load_state() -> dict:
    state_file = get_profiles_dir() / ".state.json"
    if state_file.is_file():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            log_debug(f"Failed to read state file: {exc}")
    return {"default_profile": None}


def save_state(state: dict) -> None:
    pdir = get_profiles_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    state_file = pdir / ".state.json"
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    log_debug(f"State saved to {state_file}")


def decode_jwt_payload(jwt_str: str) -> dict | None:
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
    """Fetch user info from Google OAuth API using the access token."""
    if not access_token:
        return None
    req = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": "agyp-manager"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            log_debug(f"Fetched Google UserInfo: {data.get('email')}")
            return data
    except Exception as exc:
        log_debug(f"Failed to fetch Google UserInfo: {exc}")
        return None


def get_keyring_antigravity_token() -> str | None:
    """Extract live antigravity token stored in SecretService / GNOME Keyring."""
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
                raw = bytes(secret_struct[2]).decode("utf-8")
                log_debug("Successfully retrieved antigravity token from system SecretService keyring")
                return raw
    except Exception as exc:
        log_debug(f"Could not read from SecretService: {exc}")
    return None


def search_logs_for_email(profile_home: Path) -> str | None:
    """Scans profile's own CLI log files for authenticated email address."""
    ldir = profile_home / ".gemini" / "antigravity-cli" / "log"
    if not ldir.is_dir():
        return None
    try:
        log_files = sorted(
            (f for f in ldir.iterdir() if f.is_file() and f.name.endswith(".log")),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None

    for log_file in log_files[:5]:
        try:
            content = log_file.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r"applyAuthResult:\s+email=([^,\s]+)", content)
            if match:
                return match.group(1).strip()
            match = re.search(r"OAuth:\s+authenticated successfully as\s+([^,\s]+)", content)
            if match:
                return match.group(1).strip()
        except OSError:
            pass
    return None


def find_token_file(profile_home: Path) -> Path | None:
    candidates = [
        profile_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token",
        profile_home / ".gemini" / "oauth_creds.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]  # default expected location


def extract_identity(profile_home: Path, token_path: Path | None = None) -> dict:
    """Extract email, expiry, and status for a profile."""
    if token_path is None:
        token_path = find_token_file(profile_home)

    cache_file = profile_home / ".gemini" / "antigravity-cli" / ".identity_cache.json"

    if not token_path or not token_path.is_file():
        return {"email": None, "expiry": None, "status": "no_token"}

    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"email": None, "expiry": None, "status": f"invalid_json: {exc}"}

    email = None
    expiry = None
    access_token = None

    if isinstance(data, dict):
        token = data.get("token")
        if isinstance(token, dict):
            expiry = token.get("expiry")
            access_token = token.get("access_token")
            for field in ("id_token", "access_token"):
                jwt = token.get(field)
                if isinstance(jwt, str) and jwt.count(".") >= 2:
                    claims = decode_jwt_payload(jwt)
                    if claims and "email" in claims:
                        email = claims["email"]
                        break
        # Fallback for oauth_creds.json format if token_path is oauth_creds.json
        if not email and "id_token" in data:
            jwt = data.get("id_token")
            if isinstance(jwt, str) and jwt.count(".") >= 2:
                claims = decode_jwt_payload(jwt)
                if claims and "email" in claims:
                    email = claims["email"]
        if not expiry and "expiry_date" in data:
            expiry = str(data.get("expiry_date"))
        if not access_token and "access_token" in data:
            access_token = data.get("access_token")

    # 1. Directly query Google UserInfo API using access_token (authoritative ground truth)
    if not email and access_token:
        info = fetch_google_userinfo(access_token)
        if info and "email" in info:
            email = info["email"]
            log_debug(f"Resolved email from Google UserInfo: {email}")

    # 2. If offline/failed, fallback to profile's local identity cache
    if not email and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("email"):
                email = cached["email"]
                log_debug(f"Resolved email from cache: {email}")
        except Exception:
            pass

    # 3. If still not found, check profile's local CLI logs (never global logs)
    if not email:
        email = search_logs_for_email(profile_home)
        if email:
            log_debug(f"Resolved email from profile logs: {email}")

    # Cache detected email
    if email:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"email": email, "updated_at": datetime.now(timezone.utc).isoformat()}))
        except Exception:
            pass

    status = "authenticated" if email else "valid_token"
    return {"email": email, "expiry": expiry, "status": status}


def ensure_profile_layout(profile_home: Path) -> None:
    """Sets up profile home directory, symlinks common configs, and disables keyring."""
    profile_home.mkdir(parents=True, exist_ok=True)
    cache_dir = profile_home / ".gemini" / "antigravity-cli" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Disable system keyring to prevent leakage across accounts
    keyring_marker = cache_dir / "antigravity-keyring-unavailable"
    if not keyring_marker.exists():
        keyring_marker.touch()
        log_debug(f"Created keyring isolation marker at {keyring_marker}")

    # Symlink user configs so tools (git, ssh) behave normally inside agy
    for item in [".gitconfig", ".ssh", ".vimrc"]:
        src = REAL_HOME / item
        dst = profile_home / item
        if src.exists() and not dst.exists() and not dst.is_symlink():
            try:
                dst.symlink_to(src)
                log_debug(f"Symlinked {src} -> {dst}")
            except OSError as exc:
                log_debug(f"Failed symlink {src} -> {dst}: {exc}")


def find_agy_binary() -> str:
    env_bin = os.environ.get("AGY_BINARY")
    if env_bin and Path(env_bin).is_file():
        return env_bin
    which_bin = shutil.which("agy")
    if which_bin:
        return which_bin
    standard_bin = REAL_HOME / ".local" / "bin" / "agy"
    if standard_bin.is_file() and os.access(standard_bin, os.X_OK):
        return str(standard_bin)
    raise FileNotFoundError("Could not find `agy` executable in PATH or ~/.local/bin/agy")


def cmd_list(args) -> int:
    pdir = get_profiles_dir()
    state = load_state()
    default_prof = state.get("default_profile")

    if not pdir.is_dir():
        print(f"No profiles found in {pdir}")
        return 0

    profiles = sorted([p.name for p in pdir.iterdir() if p.is_dir() and not p.name.startswith(".")])
    if not profiles:
        print(f"No profiles found in {pdir}")
        print("To create one: agyp login <profile_name>")
        return 0

    print(f"{'PROFILE':<18} {'DEFAULT':<9} {'GOOGLE ACCOUNT':<32} {'STATUS'}")
    print("-" * 75)
    for name in profiles:
        phome = get_profile_home(name)
        info = extract_identity(phome)
        is_default = "*" if name == default_prof else ""
        email_str = info.get("email") or "(not logged in)"
        status_str = info.get("status") or "unknown"
        print(f"{name:<18} {is_default:<9} {email_str:<32} {status_str}")
    return 0


def cmd_use(args) -> int:
    name = args.profile
    phome = get_profile_home(name)
    if not phome.is_dir():
        print(f"Error: Profile '{name}' does not exist.", file=sys.stderr)
        return 1
    state = load_state()
    state["default_profile"] = name
    save_state(state)
    print(f"Default profile set to: {name}")
    return 0


def cmd_whoami(args) -> int:
    name = args.profile
    if not name:
        state = load_state()
        name = state.get("default_profile")
        if not name:
            print("Error: No profile specified and no default profile set.", file=sys.stderr)
            return 1

    phome = get_profile_home(name)
    if not phome.is_dir():
        print(f"Error: Profile '{name}' does not exist.", file=sys.stderr)
        return 1

    info = extract_identity(phome)
    print(f"Profile: {name}")
    print(f"Directory: {phome}")
    print(f"Email: {info.get('email') or 'Unknown / Not Logged In'}")
    print(f"Status: {info.get('status')}")
    print(f"Token Expiry: {info.get('expiry') or 'N/A'}")
    return 0


def cmd_import_current(args) -> int:
    name = args.profile
    phome = get_profile_home(name)
    ensure_profile_layout(phome)

    # Invalidate stale identity cache before importing new token
    cache_file = phome / ".gemini" / "antigravity-cli" / ".identity_cache.json"
    if cache_file.is_file():
        cache_file.unlink()

    target_token = phome / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    target_token.parent.mkdir(parents=True, exist_ok=True)

    # 1. Try exporting live token from SecretService keyring
    keyring_token = get_keyring_antigravity_token()
    if keyring_token:
        target_token.write_text(keyring_token, encoding="utf-8")
        log_debug(f"Exported live keyring token to {target_token}")
    else:
        # 2. Try file token
        real_token = REAL_HOME / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        if real_token.is_file():
            shutil.copy2(real_token, target_token)
            log_debug(f"Copied {real_token} -> {target_token}")
        else:
            # 3. Try legacy oauth_creds.json
            legacy_creds = REAL_HOME / ".gemini" / "oauth_creds.json"
            if legacy_creds.is_file():
                shutil.copy2(legacy_creds, target_token)
                log_debug(f"Copied {legacy_creds} -> {target_token}")

    # Copy settings.json if present
    settings_src = REAL_HOME / ".gemini" / "antigravity-cli" / "settings.json"
    settings_dst = phome / ".gemini" / "antigravity-cli" / "settings.json"
    if settings_src.is_file():
        shutil.copy2(settings_src, settings_dst)

    # Clean up old misplaced oauth_creds.json in profile if it exists
    misplaced_creds = phome / ".gemini" / "oauth_creds.json"
    if misplaced_creds.is_file() and target_token.is_file() and misplaced_creds != target_token:
        try:
            misplaced_creds.unlink()
        except OSError:
            pass

    info = extract_identity(phome)
    email = info.get("email") or "imported profile"
    print(f"Imported current live credentials into profile '{name}' ({email})")
    return 0


def cmd_login(args) -> int:
    name = args.profile
    phome = get_profile_home(name)
    ensure_profile_layout(phome)

    # Remove old token to force fresh OAuth flow
    token_file = phome / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    if token_file.is_file():
        token_file.unlink()
        log_debug(f"Removed previous token {token_file}")

    # Remove cached identity
    cache_file = phome / ".gemini" / "antigravity-cli" / ".identity_cache.json"
    if cache_file.is_file():
        cache_file.unlink()

    agy_bin = find_agy_binary()
    print(f"Starting Google login session for profile '{name}'...")
    print("Authenticate in the browser when prompted, then exit agy (/exit) to finish setup.\n")
    sys.stdout.flush()

    env = os.environ.copy()
    env["HOME"] = str(phome)
    env["AGY_PROFILE"] = name
    env["DBUS_SESSION_BUS_ADDRESS"] = ""
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"

    try:
        import subprocess
        proc = subprocess.run([agy_bin], env=env)
        ret = proc.returncode
    except Exception as exc:
        print(f"Error launching agy: {exc}", file=sys.stderr)
        return 1

    info = extract_identity(phome)
    if info.get("email"):
        print(f"\n[OK] Successfully logged in as: {info.get('email')}")
        state = load_state()
        if not state.get("default_profile"):
            state["default_profile"] = name
            save_state(state)
    else:
        print("\n[NOTE] Login session finished. Run 'agyp.py list' to check token status.")
    return 0


def cmd_run(profile_name: str, agy_args: list[str]) -> int:
    phome = get_profile_home(profile_name)
    ensure_profile_layout(phome)
    agy_bin = find_agy_binary()

    env = os.environ.copy()
    env["HOME"] = str(phome)
    env["AGY_PROFILE"] = profile_name
    env["DBUS_SESSION_BUS_ADDRESS"] = ""
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"

    log_debug(f"Executing: HOME={phome} DBUS_SESSION_BUS_ADDRESS='' {agy_bin} {' '.join(agy_args)}")
    sys.stdout.flush()
    sys.stderr.flush()

    # Direct execvp replaces current process with agy
    os.environ.update(env)
    os.execv(agy_bin, [agy_bin] + agy_args)


def main():
    global DEBUG

    argv = sys.argv[1:]
    if "--debug" in argv:
        DEBUG = True
        argv = [a for a in argv if a != "--debug"]
        log_debug(f"Debug mode enabled. PID: {os.getpid()}, REAL_HOME={REAL_HOME}")

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    subcmd = argv[0]

    if subcmd in ("list", "ls"):
        return cmd_list(None)
    elif subcmd == "use":
        if len(argv) < 2:
            print("Usage: agyp.py use <profile_name>", file=sys.stderr)
            return 1
        parser = argparse.Namespace(profile=argv[1])
        return cmd_use(parser)
    elif subcmd == "whoami":
        prof = argv[1] if len(argv) > 1 else None
        parser = argparse.Namespace(profile=prof)
        return cmd_whoami(parser)
    elif subcmd == "login":
        if len(argv) < 2:
            print("Usage: agyp.py login <profile_name>", file=sys.stderr)
            return 1
        parser = argparse.Namespace(profile=argv[1])
        return cmd_login(parser)
    elif subcmd == "import-current":
        if len(argv) < 2:
            print("Usage: agyp.py import-current <profile_name>", file=sys.stderr)
            return 1
        parser = argparse.Namespace(profile=argv[1])
        return cmd_import_current(parser)

    # Check if first arg is an existing profile name or starts with '-'
    pdir = get_profiles_dir()
    candidate_profile = argv[0]
    if (pdir / candidate_profile).is_dir() or not candidate_profile.startswith("-"):
        profile = candidate_profile
        remaining_args = argv[1:]
        cmd_run(profile, remaining_args)
    else:
        state = load_state()
        default_prof = state.get("default_profile")
        if not default_prof:
            print("Error: No profile specified and no default profile set.", file=sys.stderr)
            print("Specify a profile: agyp.py <profile_name> [args...]", file=sys.stderr)
            print("Or set a default:   agyp.py use <profile_name>", file=sys.stderr)
            return 1
        cmd_run(default_prof, argv)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
