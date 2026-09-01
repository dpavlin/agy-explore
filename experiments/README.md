# Lightweight Antigravity (`agy`) Multi-Account Wrappers & Experiments

This directory contains lightweight alternatives to the complex `agy-cli-manager` daemon/rotation stack. These tools focus strictly on allowing multiple Google accounts/logins with `agy`.

---

## Comparison of Implementations

| Feature / Property | `agy-cli-manager` (Original) | `agyp.py` (Option 1) | `agyp.sh` (Option 2) | `agy-switch.py` (Option 3) |
| :--- | :--- | :--- | :--- | :--- |
| **Lines of Code** | ~3,500+ lines (4 modules) | ~250 lines (1 script) | ~90 lines (bash) | ~180 lines (1 script) |
| **Dependencies** | Custom package + state engine | Python standard library | Pure POSIX shell | Python standard library |
| **Mechanism** | Sliding window + quota watcher + daemon | Isolated Profile (`HOME=~/.config/agy-profiles/<name>`) | Isolated Profile (`HOME=~/.config/agy-profiles/<name>`) | In-place Token Copy (`~/.gemini/antigravity-cli/antigravity-oauth-token`) |
| **Concurrent Sessions** | Complex lock coordination | **Yes (fully isolated)** | **Yes (fully isolated)** | No (shares live directory) |
| **Keyring Isolation** | Custom lock logic | `antigravity-keyring-unavailable` flag | `antigravity-keyring-unavailable` flag | File token swap |
| **Git / SSH Integration** | Implicit | Automatic symlinks to user home | Automatic symlinks to user home | Direct user home |

---

## 1. Option 1: `agyp.py` (Recommended Python Profile Wrapper)

Full-featured lightweight wrapper that runs `agy` in an isolated profile directory.

### Quick Start

```bash
# 1. Add / authenticate a new Google account
./experiments/agyp.py login work

# 2. Run agy under that profile
./experiments/agyp.py work models
./experiments/agyp.py work -p "Explain this project"
./experiments/agyp.py work                      # Interactive session

# 3. List all profiles and see detected Google emails
./experiments/agyp.py list

# 4. Set a default profile
./experiments/agyp.py use work
./experiments/agyp.py models                   # Uses 'work' default
```

### Key Highlights
- **No daemon / no polling**: Only runs when you execute a command.
- **Keyring isolation**: Automatically places `cache/antigravity-keyring-unavailable` into the profile directory so Linux desktop GNOME Keyring does not mix credentials across accounts.
- **Developer symlinks**: Automatically creates symlinks for `.gitconfig`, `.ssh`, and `.vimrc` so subagents and CLI tools retain your developer environment without sharing Google auth tokens.
- **Shell alias**: Add `alias agyp="$PWD/experiments/agyp.py"` or copy to `~/.local/bin/agyp`.

---

## 2. Option 2: `agyp.sh` (Minimalist Bash Wrapper)

Zero-dependency shell script implementing the same isolated profile pattern.

```bash
# Run with a profile:
./experiments/agyp.sh work models

# Authenticate new profile:
./experiments/agyp.sh --login work

# List profiles:
./experiments/agyp.sh --list
```

---

## 3. Option 3: `agy-switch.py` (In-Place Token Switcher)

If you prefer keeping your standard `HOME` and want to switch the active login in `~/.gemini`:

```bash
# Save your current login as 'personal'
./experiments/agy-switch.py save personal

# Log into a second account and save as 'work'
./experiments/agy-switch.py login work

# Switch active account:
./experiments/agy-switch.py switch work
./experiments/agy-switch.py switch personal
```
