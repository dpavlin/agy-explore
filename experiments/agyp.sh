#!/usr/bin/env bash
#
# agyp.sh - Ultra-lightweight POSIX Shell Wrapper for Antigravity CLI (agy)
#
# Usage:
#   ./agyp.sh <profile_name> [agy-options...]
#   ./agyp.sh --list
#   ./agyp.sh --login <profile_name>
#   ./agyp.sh --use <profile_name>
#

set -euo pipefail

PROFILES_DIR="${AGY_PROFILES_DIR:-$HOME/.config/agy-profiles}"
STATE_FILE="$PROFILES_DIR/.default_profile"
AGY_BIN="${AGY_BINARY:-$(command -v agy || echo "$HOME/.local/bin/agy")}"
DEBUG="${AGY_DEBUG:-0}"

log_debug() {
    if [[ "$DEBUG" == "1" ]]; then
        local ts
        ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
        echo "[$ts] [DEBUG] $*" >&2
    fi
}

ensure_profile() {
    local profile="$1"
    local pdir="$PROFILES_DIR/$profile"
    local cache_dir="$pdir/.gemini/antigravity-cli/cache"

    mkdir -p "$cache_dir"

    # Disable system keyring to prevent cross-profile auth pollution
    touch "$cache_dir/antigravity-keyring-unavailable"

    # Symlink common user configs so git, ssh, etc. work inside agy
    for f in .gitconfig .ssh .vimrc; do
        if [[ -e "$HOME/$f" && ! -e "$pdir/$f" ]]; then
            ln -snf "$HOME/$f" "$pdir/$f" 2>/dev/null || true
            log_debug "Symlinked $HOME/$f -> $pdir/$f"
        fi
    done
}

cmd_list() {
    echo "Profiles directory: $PROFILES_DIR"
    mkdir -p "$PROFILES_DIR"
    local default_prof=""
    [[ -f "$STATE_FILE" ]] && default_prof="$(cat "$STATE_FILE" 2>/dev/null || true)"

    printf "%-18s %-9s %s\n" "PROFILE" "DEFAULT" "DIRECTORY"
    printf -- "---------------------------------------------------------\n"
    for d in "$PROFILES_DIR"/*; do
        if [[ -d "$d" ]]; then
            local name
            name="$(basename "$d")"
            local is_def=""
            [[ "$name" == "$default_prof" ]] && is_def="*"
            printf "%-18s %-9s %s\n" "$name" "$is_def" "$d"
        fi
    done
}

if [[ $# -eq 0 || "$1" == "-h" || "$1" == "--help" ]]; then
    echo "Usage: $(basename "$0") <profile_name> [agy-options...]"
    echo "       $(basename "$0") --list"
    echo "       $(basename "$0") --login <profile_name>"
    echo "       $(basename "$0") --use <profile_name>"
    exit 0
fi

case "$1" in
    --list|-l|list|ls)
        cmd_list
        exit 0
        ;;
    --use|use)
        if [[ $# -lt 2 ]]; then
            echo "Error: Profile name required. Usage: $(basename "$0") --use <name>" >&2
            exit 1
        fi
        mkdir -p "$PROFILES_DIR"
        echo "$2" > "$STATE_FILE"
        echo "Default profile set to: $2"
        exit 0
        ;;
    --login|login)
        if [[ $# -lt 2 ]]; then
            echo "Error: Profile name required. Usage: $(basename "$0") --login <name>" >&2
            exit 1
        fi
        PROFILE="$2"
        PDIR="$PROFILES_DIR/$PROFILE"
        ensure_profile "$PROFILE"
        # Clear previous token
        rm -f "$PDIR/.gemini/antigravity-cli/antigravity-oauth-token"
        echo "Starting Google login session for profile '$PROFILE'..."
        echo "Sign in when prompted, then type /exit to complete."
        HOME="$PDIR" AGY_PROFILE="$PROFILE" DBUS_SESSION_BUS_ADDRESS="" "$AGY_BIN"
        exit 0
        ;;
    --debug)
        export DEBUG=1
        shift
        exec "$0" "$@"
        ;;
esac

# Check if first argument is a profile
if [[ -d "$PROFILES_DIR/$1" ]] || [[ ! "$1" =~ ^- ]]; then
    PROFILE="$1"
    shift
else
    if [[ -f "$STATE_FILE" ]]; then
        PROFILE="$(cat "$STATE_FILE")"
    else
        echo "Error: No profile specified and no default set." >&2
        echo "Usage: $(basename "$0") <profile_name> [agy-args...]" >&2
        exit 1
    fi
fi

PDIR="$PROFILES_DIR/$PROFILE"
ensure_profile "$PROFILE"

log_debug "Executing: HOME=$PDIR DBUS_SESSION_BUS_ADDRESS='' $AGY_BIN $*"
export HOME="$PDIR"
export AGY_PROFILE="$PROFILE"
export DBUS_SESSION_BUS_ADDRESS=""
export PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring"

exec "$AGY_BIN" "$@"
