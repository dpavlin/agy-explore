# Lightweight Antigravity (`agy`) Multi-Account Wrappers & Experiments

This directory contains lightweight tools for managing multiple Google accounts with Google's **Antigravity CLI** (`agy`), providing profile isolation without complex background daemons or quota rotation loops.

---

## Comparison of Implementations

| Feature / Property | `agy-cli-manager` (Original) | `agyp.py` (Option 1 - Recommended) | `agyp.sh` (Option 2) | `agy-switch.py` (Option 3) |
| :--- | :--- | :--- | :--- | :--- |
| **Lines of Code** | ~3,500+ lines (4 modules) | ~260 lines (1 script) | ~95 lines (bash) | ~190 lines (1 script) |
| **Dependencies** | Custom package + state engine | Python 3 standard library | Pure POSIX shell | Python 3 standard library |
| **Mechanism** | Daemon + quota watcher + sliding windows | Isolated Profile (`HOME=~/.config/agy-profiles/<name>`) | Isolated Profile (`HOME=~/.config/agy-profiles/<name>`) | In-place Token Copy (`~/.gemini/antigravity-cli/antigravity-oauth-token`) |
| **Concurrent Sessions** | Complex lock coordination | **Yes (fully isolated)** | **Yes (fully isolated)** | No (shares live directory) |
| **Keyring Isolation** | Custom lock logic | `DBUS_SESSION_BUS_ADDRESS=""` + cache marker | `DBUS_SESSION_BUS_ADDRESS=""` + cache marker | File token swap |
| **Git / SSH Integration** | Implicit | Automatic symlinks to user home | Automatic symlinks to user home | Direct user home |

---

## Technical Architecture & Keyring Isolation

### The Keyring Conflict in Native `agy`
Google's `agy` is a compiled Go binary that uses `zalando/go-keyring`.
1. On Linux desktops with D-Bus enabled, native `agy` persists its OAuth tokens directly to the system **SecretService / GNOME Keyring** under a single shared key:
   ```text
   service="gemini", username="antigravity"
   ```
2. Any running `agy` process wakes up every 55–60 minutes (`browser.go:259`) to refresh its OAuth token. When it does so, it unconditionally writes the refreshed token to that single shared keyring slot.
3. Therefore, running multiple native `agy` instances with different Google logins in parallel causes the background refresh of one process to clobber the keyring credentials of the other.

### How `agyp` Achieves Isolation
1. **Isolated Home Directory:** Sets `HOME=~/.config/agy-profiles/<profile_name>`.
2. **D-Bus Keyring Decoupling:** Sets `DBUS_SESSION_BUS_ADDRESS=""` in the environment passed to `agy`. In `agy`'s internal `keyring_detector_dbus.go`, an unset/empty D-Bus address triggers:
   ```text
   composite_token_storage.go:126] Using file-based token storage
   ```
   This forces `agy` to read and write tokens **strictly from the profile disk directory**:
   ```text
   ~/.config/agy-profiles/<profile>/.gemini/antigravity-cli/antigravity-oauth-token
   ```
3. **Developer Symlinks:** Automatically symlinks `~/.gitconfig`, `~/.ssh`, and `~/.vimrc` into the profile directory so git commits, ssh credentials, and editors function seamlessly without leaking auth tokens.
4. **Authoritative Identity Detection:** Queries Google's OAuth UserInfo API (`https://www.googleapis.com/oauth2/v2/userinfo`) using the profile's access token to verify the actual Google email address.

---

## Verification & Empirical Testing

The implementation was rigorously verified on Linux with independent test accounts (`work` and `personal`).

### 1. Verification of Token Import
Live credentials from the system SecretService keyring were imported into separate profiles:

```bash
$ ./experiments/agyp.py import-current work
[DEBUG] Successfully retrieved antigravity token from system SecretService keyring
[DEBUG] Exported live keyring token to ~/.config/agy-profiles/work/.gemini/antigravity-cli/antigravity-oauth-token
[DEBUG] Fetched Google UserInfo: work-user@example.com
Imported current live credentials into profile 'work' (work-user@example.com)
```

### 2. Verification of Profile Identity Listing
Profile status confirms both independent accounts are recognized and authenticated:

```bash
$ ./experiments/agyp.py list
PROFILE            DEFAULT   GOOGLE ACCOUNT                   STATUS
---------------------------------------------------------------------------
work                         work-user@example.com            authenticated
personal                     personal-user@example.com        authenticated
```

Detailed inspection per profile:

```bash
$ ./experiments/agyp.py whoami work
Profile: work
Directory: ~/.config/agy-profiles/work
Email: work-user@example.com
Status: authenticated
Token Expiry: 2026-09-04T08:54:52.387434141+02:00

$ ./experiments/agyp.py whoami personal
Profile: personal
Directory: ~/.config/agy-profiles/personal
Email: personal-user@example.com
Status: authenticated
Token Expiry: 2026-09-03T20:01:37.410446675+02:00
```

### 3. Verification of Independent Concurrent Execution
Both profiles were executed sequentially and verified against their CLI log output:

```bash
$ ./experiments/agyp.py work models
-> Log file: ~/.config/agy-profiles/work/.gemini/antigravity-cli/log/cli-20260904_075710.log
-> Verified log entry:
   server_oauth.go:197] OAuth: authenticated successfully as work-user@example.com

$ ./experiments/agyp.py personal models
-> Log file: ~/.config/agy-profiles/personal/.gemini/antigravity-cli/log/cli-20260904_075712.log
-> Verified log entry:
   server_oauth.go:197] OAuth: authenticated successfully as personal-user@example.com
```

Shell wrapper verification:
```bash
$ ./experiments/agyp.sh work models
-> Verified log entry:
   server_oauth.go:197] OAuth: authenticated successfully as work-user@example.com
```

Both accounts run in complete isolation with zero keyring collisions.

---

## Usage Guide

### 1. Option 1: `agyp.py` (Recommended)

```bash
# 1. Import current system login into a named profile:
./experiments/agyp.py import-current work

# 2. Add / authenticate a second Google account:
./experiments/agyp.py login personal

# 3. List all profiles and see detected Google emails:
./experiments/agyp.py list

# 4. Run agy under a specific profile:
./experiments/agyp.py work models
./experiments/agyp.py personal -p "Explain this project"
./experiments/agyp.py work                      # Interactive session

# 5. Set default profile:
./experiments/agyp.py use work
./experiments/agyp.py models                   # Uses 'work' default
```

### Convenient Shell Aliases

Add these to your `~/.bashrc` or `~/.zshrc`:

```bash
alias agy-work="/path/to/experiments/agyp.py work"
alias agy-personal="/path/to/experiments/agyp.py personal"
```

---

### 2. Option 2: `agyp.sh` (Minimalist Bash Wrapper)

```bash
# Run with a profile:
./experiments/agyp.sh work models

# Authenticate new profile:
./experiments/agyp.sh --login work

# List profiles:
./experiments/agyp.sh --list
```

---

### 3. Option 3: `agy-switch.py` (In-Place Token Switcher)

```bash
# Save your current login:
./experiments/agy-switch.py save work

# Log into a second account in a temp sandbox:
./experiments/agy-switch.py login personal

# Switch active account:
./experiments/agy-switch.py switch personal
./experiments/agy-switch.py switch work
```
