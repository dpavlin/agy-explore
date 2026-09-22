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
- Cross-profile session management: import, export, and live-share conversations.
- Commands: list, login, run, use (set default), whoami, import-current, import, export, remove.
- Includes --debug flag for verbose diagnostics (timestamps, paths, environment).

Usage:
  agyp.py <profile> [agy-options...]                    # Run agy with specified profile
  agyp.py [agy-options...]                              # Run agy with default active profile
  agyp.py list                                          # List profiles and Google account emails
  agyp.py login <profile>                               # Authenticate a profile with Google OAuth
  agyp.py use <profile>                                 # Set default profile
  agyp.py whoami [profile]                              # Show identity/email of profile
  agyp.py import-current <profile>                      # Import current live login (keyring/file) into profile
  agyp.py import [profile] <conv_id_or_archive>         # Import / share session into profile (default: symlink)
  agyp.py export [source_prof] <conv_id> <target>       # Export session to profile or tarball (.tar.gz)
  agyp.py <profile> import <conv_id_or_archive>         # Alias-friendly session import
  agyp.py <profile> export <conv_id> <target>           # Alias-friendly session export
  agyp.py --debug <profile> ...                         # Enable debug diagnostics

Import & Export Options:
  --link, --shared   Create symlinks for zero-disk live session sharing (default)
  --copy             Clone session files independently
  --from <profile>   Explicit source profile (default: auto-detected across all profiles)
  -o, --output       Target archive path when exporting
"""

import os
import sys
import json
import base64
import shutil
import re
import argparse
import sqlite3
import tarfile
import urllib.request
import urllib.error
import pwd
from datetime import datetime, timezone
from pathlib import Path

try:
    REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
except Exception:
    REAL_HOME = Path.home()

DEFAULT_PROFILES_DIR = REAL_HOME / ".config" / "agy-profiles"
DEFAULT_STATE_FILE = DEFAULT_PROFILES_DIR / ".state.json"

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


def get_gemini_cli_dir(profile_name: str | None) -> Path:
    if not profile_name or profile_name == "default":
        return REAL_HOME / ".gemini" / "antigravity-cli"
    return get_profile_home(profile_name) / ".gemini" / "antigravity-cli"


def list_all_profiles() -> list[str]:
    names = ["default"]
    pdir = get_profiles_dir()
    if pdir.is_dir():
        for p in sorted(pdir.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                names.append(p.name)
    return names


def find_session_locations(conv_id: str) -> list[tuple[str, Path]]:
    """Returns list of (profile_name, gemini_cli_dir) where conv_id exists."""
    found = []
    for prof in list_all_profiles():
        gdir = get_gemini_cli_dir(prof)
        db_file = gdir / "conversations" / f"{conv_id}.db"
        brain_dir = gdir / "brain" / conv_id
        if db_file.exists() or brain_dir.exists():
            found.append((prof, gdir))
    return found


def extract_session_workspace(gemini_dir: Path, conv_id: str) -> str | None:
    """Attempts to find the workspace directory mapped to conv_id."""
    # 1. Check last_conversations.json
    last_conv_file = gemini_dir / "cache" / "last_conversations.json"
    if last_conv_file.is_file():
        try:
            data = json.loads(last_conv_file.read_text(encoding="utf-8"))
            for ws, cid in data.items():
                if cid == conv_id:
                    return ws
        except Exception:
            pass

    # 2. Check trajectory_metadata_blob in conversations/<conv_id>.db
    db_file = gemini_dir / "conversations" / f"{conv_id}.db"
    if db_file.is_file():
        try:
            conn = sqlite3.connect(db_file)
            cur = conn.cursor()
            cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'")
            row = cur.fetchone()
            if row and row[0]:
                m = re.search(rb"file://(/[^\x00-\x1f\x7f-\xff\s\"'\)]+)", row[0])
                if m:
                    ws = m.group(1).decode("utf-8", "ignore")
                    if len(ws) > 1 and ws.endswith("/"):
                        ws = ws[:-1]
                    return ws
        except Exception:
            pass

    # 3. Check transcript logs
    for log_name in ("transcript_full.jsonl", "transcript.jsonl"):
        log_path = gemini_dir / "brain" / conv_id / ".system_generated" / "logs" / log_name
        if log_path.is_file():
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f):
                        if i > 50:
                            break
                        if not line.strip():
                            continue
                        d = json.loads(line)
                        for tc in d.get("tool_calls", []):
                            args = tc.get("args", {})
                            for k in ("DirectoryPath", "Cwd", "SearchDirectory", "SearchPath"):
                                p = args.get(k)
                                if p and isinstance(p, str) and os.path.isabs(p) and not p.startswith("/tmp") and ".gemini" not in p:
                                    return p
            except Exception:
                pass
    return None


def clean_broken_session_links(target_profile: str, conv_id: str) -> None:
    phome = get_profile_home(target_profile)
    root_link = phome / conv_id
    if root_link.is_symlink() and not root_link.exists():
        try:
            root_link.unlink()
            log_debug(f"Removed broken root symlink: {root_link}")
        except OSError:
            pass

    gdir = get_gemini_cli_dir(target_profile)
    for sub in ["conversations", "brain", "annotations"]:
        candidate = gdir / sub / (f"{conv_id}.db" if sub == "conversations" else f"{conv_id}.pbtxt" if sub == "annotations" else conv_id)
        if candidate.is_symlink() and not candidate.exists():
            try:
                candidate.unlink()
                log_debug(f"Removed broken symlink in {sub}: {candidate}")
            except OSError:
                pass


def update_workspace_pointer(gemini_dir: Path, workspace: str, conv_id: str) -> None:
    cache_dir = gemini_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    last_conv_file = cache_dir / "last_conversations.json"
    data = {}
    if last_conv_file.is_file():
        try:
            data = json.loads(last_conv_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[workspace] = conv_id
    last_conv_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log_debug(f"Updated pointer in {last_conv_file}: {workspace} -> {conv_id}")


def copy_summary_row(src_gemini: Path, dst_gemini: Path, conv_id: str) -> None:
    src_db = src_gemini / "conversation_summaries.db"
    dst_db = dst_gemini / "conversation_summaries.db"
    if not src_db.is_file():
        return
    try:
        src_conn = sqlite3.connect(src_db)
        src_cur = src_conn.cursor()
        src_cur.execute("SELECT * FROM conversation_summaries WHERE conversation_id = ?", (conv_id,))
        row = src_cur.fetchone()
        if not row:
            return
        cols = [d[0] for d in src_cur.description]
        if dst_db.is_file():
            dst_conn = sqlite3.connect(dst_db)
            dst_cur = dst_conn.cursor()
            placeholders = ", ".join(["?"] * len(cols))
            col_names = ", ".join([f"`{c}`" for c in cols])
            dst_cur.execute(f"INSERT OR REPLACE INTO conversation_summaries ({col_names}) VALUES ({placeholders})", row)
            dst_conn.commit()
            log_debug(f"Copied conversation summary row for {conv_id}")
    except Exception as exc:
        log_debug(f"Could not copy summary row: {exc}")


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


def do_import_session(
    target_profile: str,
    conv_id_or_archive: str,
    source_profile: str | None = None,
    copy_mode: bool = False,
) -> int:
    phome = get_profile_home(target_profile)
    ensure_profile_layout(phome)
    dst_gemini = get_gemini_cli_dir(target_profile)

    # 1. Check if conv_id_or_archive is a tarball / archive file
    archive_path = Path(conv_id_or_archive).expanduser().resolve()
    if archive_path.is_file() and (archive_path.name.endswith(".tar.gz") or archive_path.name.endswith(".tgz") or archive_path.name.endswith(".tar")):
        print(f"Extracting session archive '{archive_path.name}' into profile '{target_profile}'...")
        try:
            with tarfile.open(archive_path, "r:*") as tar:
                tar.extractall(path=dst_gemini)
            print(f"[OK] Successfully extracted session archive into {dst_gemini}")
            return 0
        except Exception as exc:
            print(f"Error extracting archive: {exc}", file=sys.stderr)
            return 1

    # Strip URL/arg prefixes if provided (e.g. conversation://<uuid> or --conversation=<uuid>)
    conv_id = conv_id_or_archive
    if conv_id.startswith("--conversation="):
        conv_id = conv_id.split("=", 1)[1]
    elif conv_id.startswith("conversation://"):
        conv_id = conv_id.replace("conversation://", "")
    conv_id = conv_id.strip()

    clean_broken_session_links(target_profile, conv_id)

    # 2. Locate source session
    if source_profile:
        src_gemini = get_gemini_cli_dir(source_profile)
        src_db = src_gemini / "conversations" / f"{conv_id}.db"
        src_brain = src_gemini / "brain" / conv_id
        if not src_db.exists() and not src_brain.exists():
            print(f"Error: Conversation '{conv_id}' not found in source profile '{source_profile}'.", file=sys.stderr)
            return 1
        src_prof_name = source_profile
    else:
        locations = find_session_locations(conv_id)
        viable = [loc for loc in locations if loc[0] != target_profile]
        if not viable and locations:
            viable = locations
        if not viable:
            print(f"Error: Conversation '{conv_id}' was not found in any profile or default store.", file=sys.stderr)
            return 1
        src_prof_name, src_gemini = viable[0]

    src_db = src_gemini / "conversations" / f"{conv_id}.db"
    src_brain = src_gemini / "brain" / conv_id
    src_annot = src_gemini / "annotations" / f"{conv_id}.pbtxt"

    if not src_db.exists() and not src_brain.exists():
        print(f"Error: Conversation '{conv_id}' has neither database nor brain directory in '{src_prof_name}'.", file=sys.stderr)
        return 1

    # Ensure destination directories exist
    (dst_gemini / "conversations").mkdir(parents=True, exist_ok=True)
    (dst_gemini / "brain").mkdir(parents=True, exist_ok=True)
    (dst_gemini / "annotations").mkdir(parents=True, exist_ok=True)
    (dst_gemini / "cache").mkdir(parents=True, exist_ok=True)

    dst_db = dst_gemini / "conversations" / f"{conv_id}.db"
    dst_brain = dst_gemini / "brain" / conv_id
    dst_annot = dst_gemini / "annotations" / f"{conv_id}.pbtxt"

    mode_str = "Copied" if copy_mode else "Shared (Symlink)"

    # Import DB
    if src_db.exists():
        if dst_db.is_symlink() or dst_db.exists():
            if dst_db.is_dir():
                shutil.rmtree(dst_db)
            else:
                dst_db.unlink()
        if copy_mode:
            shutil.copy2(src_db, dst_db)
        else:
            dst_db.symlink_to(src_db.resolve())
        log_debug(f"Imported DB {src_db} -> {dst_db} ({mode_str})")

    # Import Brain
    if src_brain.exists():
        if dst_brain.is_symlink() or dst_brain.exists():
            if dst_brain.is_dir() and not dst_brain.is_symlink():
                shutil.rmtree(dst_brain)
            else:
                dst_brain.unlink()
        if copy_mode:
            shutil.copytree(src_brain, dst_brain, symlinks=True, dirs_exist_ok=True)
        else:
            dst_brain.symlink_to(src_brain.resolve())
        log_debug(f"Imported Brain {src_brain} -> {dst_brain} ({mode_str})")

    # Import Annotations if available
    if src_annot.exists():
        if dst_annot.is_symlink() or dst_annot.exists():
            dst_annot.unlink()
        if copy_mode:
            shutil.copy2(src_annot, dst_annot)
        else:
            dst_annot.symlink_to(src_annot.resolve())

    # Detect workspace & set last_conversations pointer
    workspace = extract_session_workspace(src_gemini, conv_id)
    if workspace:
        update_workspace_pointer(dst_gemini, workspace, conv_id)

    copy_summary_row(src_gemini, dst_gemini, conv_id)

    print(f"\n[+] Successfully imported conversation into profile '{target_profile}'!")
    print(f"    Conversation ID: {conv_id}")
    print(f"    Source:          {src_prof_name} ({src_gemini})")
    print(f"    Mode:            {mode_str}")
    if workspace:
        print(f"    Workspace:       {workspace}")
    print(f"\nTo resume this session:")
    if workspace:
        print(f"    cd {workspace} && agyp.py {target_profile} --conversation {conv_id}")
        print(f"    (or alias: agy-{target_profile} --conversation {conv_id})")
    else:
        print(f"    agyp.py {target_profile} --conversation {conv_id}")
    return 0


def do_export_session(
    source_profile: str | None,
    conv_id: str,
    target: str,
    copy_mode: bool = False,
) -> int:
    # Clean conv_id
    if conv_id.startswith("--conversation="):
        conv_id = conv_id.split("=", 1)[1]
    elif conv_id.startswith("conversation://"):
        conv_id = conv_id.replace("conversation://", "")
    conv_id = conv_id.strip()

    # Determine if target is an archive file
    target_path = Path(target).expanduser()
    is_archive = target.endswith(".tar.gz") or target.endswith(".tgz") or target.endswith(".tar") or target.endswith(".zip")

    # Locate source session
    if source_profile:
        src_gemini = get_gemini_cli_dir(source_profile)
        src_prof_name = source_profile
    else:
        locations = find_session_locations(conv_id)
        if not locations:
            print(f"Error: Conversation '{conv_id}' was not found in any profile or default store.", file=sys.stderr)
            return 1
        src_prof_name, src_gemini = locations[0]

    src_db = src_gemini / "conversations" / f"{conv_id}.db"
    src_brain = src_gemini / "brain" / conv_id
    src_annot = src_gemini / "annotations" / f"{conv_id}.pbtxt"

    if not src_db.exists() and not src_brain.exists():
        print(f"Error: Conversation '{conv_id}' not found in '{src_prof_name}'.", file=sys.stderr)
        return 1

    if is_archive:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Exporting session '{conv_id}' from '{src_prof_name}' to archive: {target_path}...")
        try:
            with tarfile.open(target_path, "w:gz") as tar:
                if src_db.is_file():
                    tar.add(src_db, arcname=f"conversations/{conv_id}.db")
                if src_brain.is_dir():
                    tar.add(src_brain, arcname=f"brain/{conv_id}")
                if src_annot.is_file():
                    tar.add(src_annot, arcname=f"annotations/{conv_id}.pbtxt")
            print(f"[OK] Successfully created session archive: {target_path}")
            return 0
        except Exception as exc:
            print(f"Error creating archive: {exc}", file=sys.stderr)
            return 1
    else:
        # Target is another profile
        target_profile = target
        return do_import_session(
            target_profile=target_profile,
            conv_id_or_archive=conv_id,
            source_profile=src_prof_name,
            copy_mode=copy_mode,
        )


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
    elif subcmd in ("import", "import-session"):
        sub_args = argv[1:]
        copy_mode = "--copy" in sub_args
        from_prof = None
        if "--from" in sub_args:
            f_idx = sub_args.index("--from")
            if f_idx + 1 < len(sub_args):
                from_prof = sub_args[f_idx + 1]
                sub_args = [a for i, a in enumerate(sub_args) if i != f_idx and i != f_idx + 1]
        sub_args = [a for a in sub_args if a not in ("--copy", "--link", "--shared")]

        if not sub_args:
            print("Usage: agyp.py import [target_profile] <conversation_id_or_archive> [--copy] [--from <source>]", file=sys.stderr)
            return 1

        all_profs = list_all_profiles()
        if len(sub_args) == 1:
            state = load_state()
            target_prof = state.get("default_profile")
            if not target_prof:
                print("Error: No profile specified and no default profile set.", file=sys.stderr)
                print(f"Available profiles: {', '.join([p for p in all_profs if p != 'default'])}", file=sys.stderr)
                return 1
            conv_id = sub_args[0]
        else:
            target_prof = sub_args[0]
            conv_id = sub_args[1]

        return do_import_session(target_prof, conv_id, source_profile=from_prof, copy_mode=copy_mode)
    elif subcmd in ("export", "export-session"):
        sub_args = argv[1:]
        copy_mode = "--copy" in sub_args
        from_prof = None
        if "-o" in sub_args or "--output" in sub_args:
            o_idx = sub_args.index("-o") if "-o" in sub_args else sub_args.index("--output")
            if o_idx + 1 < len(sub_args):
                out_target = sub_args[o_idx + 1]
                sub_args = [a for i, a in enumerate(sub_args) if i != o_idx and i != o_idx + 1]
                sub_args.append(out_target)
        if "--from" in sub_args:
            f_idx = sub_args.index("--from")
            if f_idx + 1 < len(sub_args):
                from_prof = sub_args[f_idx + 1]
                sub_args = [a for i, a in enumerate(sub_args) if i != f_idx and i != f_idx + 1]
        sub_args = [a for a in sub_args if a not in ("--copy", "--link", "--shared")]

        if len(sub_args) < 2:
            print("Usage: agyp.py export [source_profile] <conversation_id> <target_profile_or_archive.tar.gz> [--copy]", file=sys.stderr)
            return 1

        if len(sub_args) == 2:
            conv_id = sub_args[0]
            target = sub_args[1]
        else:
            from_prof = sub_args[0]
            conv_id = sub_args[1]
            target = sub_args[2]

        return do_export_session(from_prof, conv_id, target, copy_mode=copy_mode)

    # Check if first arg is an existing profile name or starts with '-'
    pdir = get_profiles_dir()
    candidate_profile = argv[0]
    if (pdir / candidate_profile).is_dir() or not candidate_profile.startswith("-"):
        profile = candidate_profile
        remaining_args = argv[1:]
        if remaining_args and remaining_args[0] in ("import", "import-session"):
            sub_args = remaining_args[1:]
            copy_mode = "--copy" in sub_args
            from_prof = None
            if "--from" in sub_args:
                f_idx = sub_args.index("--from")
                if f_idx + 1 < len(sub_args):
                    from_prof = sub_args[f_idx + 1]
                    sub_args = [a for i, a in enumerate(sub_args) if i != f_idx and i != f_idx + 1]
            sub_args = [a for a in sub_args if a not in ("--copy", "--link", "--shared")]
            if not sub_args:
                print(f"Usage: agyp.py {profile} import <conversation_id_or_archive> [--copy] [--from <source>]", file=sys.stderr)
                return 1
            return do_import_session(profile, sub_args[0], source_profile=from_prof, copy_mode=copy_mode)
        elif remaining_args and remaining_args[0] in ("export", "export-session"):
            sub_args = remaining_args[1:]
            copy_mode = "--copy" in sub_args
            if "-o" in sub_args or "--output" in sub_args:
                o_idx = sub_args.index("-o") if "-o" in sub_args else sub_args.index("--output")
                if o_idx + 1 < len(sub_args):
                    out_target = sub_args[o_idx + 1]
                    sub_args = [a for i, a in enumerate(sub_args) if i != o_idx and i != o_idx + 1]
                    sub_args.append(out_target)
            sub_args = [a for a in sub_args if a not in ("--copy", "--link", "--shared")]
            if len(sub_args) < 2:
                print(f"Usage: agyp.py {profile} export <conversation_id> <target_profile_or_archive> [--copy]", file=sys.stderr)
                return 1
            return do_export_session(profile, sub_args[0], sub_args[1], copy_mode=copy_mode)
        else:
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
