# Implementation Plan - Standalone `git-brain` CLI Tool

This plan introduces `git-brain` as a completely standalone Python 3 CLI utility. It copies session artifacts into the project's local Git repository (`.brain/` folder) and commits them historically using the original file timestamps.

---

## Technical Specifications & Workflow

### 1. Script Architecture (`git-brain`)
*   **Location:** We will create a premium standalone python3 script `/home/dpavlin/agy-explore/git-brain`.
*   **Global Access:** We will symlink `/home/dpavlin/agy-explore/git-brain` to `/home/dpavlin/.local/bin/git-brain` so it is globally available in the developer's shell.

### 2. Execution Flow

#### Step A: Resolve Session ID & Git Workspace
If no session ID is supplied:
1.  Query `os.getcwd()` to find the current directory path.
2.  Inspect `~/.gemini/antigravity-cli/cache/last_conversations.json` (falling back to scanning `history.jsonl`) to automatically identify the last active session ID mapped to the current directory.
3.  If found, proceed with this session ID. If not found, print an error and instruct the user to provide a session ID.

If a session ID is supplied:
1.  Verify the brain directory exists: `~/.gemini/antigravity-cli/brain/<conv_id>`.
2.  Scan `history.jsonl` to locate the mapped workspace directory. If none found, fallback to `/home/dpavlin` with a warning, or search for it in `cache/last_conversations.json`.

Verify that the target workspace is a valid directory and contains a Git repository (i.e. `.git` directory exists).

#### Step B: Scanner & Timestamps Sorting
1.  Crawl `brain/<conv_id>/` recursively to gather all files.
2.  Ignore system/meta files:
    *   `.system_generated` internal logs.
    *   Hidden directories and hidden files (starting with `.`).
    *   Metadata tracks (`*.metadata.json`).
3.  For each file of interest, capture the relative path and modification epoch time (`os.stat().st_mtime`).
4.  Sort the file list by modification epoch time **ascending** (oldest first) to ensure historical chronological timeline reconstruction in Git.

#### Step C:timeline-based Commits
For each file in chronological order:
1.  Copy it to `.brain/<rel_path>` inside the workspace (preserves timestamps via `shutil.copy2()`).
2.  Stage the file: `git add .brain/<rel_path>`.
3.  Verify if the file actually has staged modifications: `git diff --cached --name-only`.
4.  If changed: Commit the file setting `GIT_AUTHOR_DATE` and `GIT_COMMITTER_DATE` environment variables to the file's original epoch timestamp (`@<epoch_time>`).
    *   `git commit -m "Import brain file <rel_path> from session <conv_id>"`
    *   Print confirmation: `[+] Committed .brain/<rel_path> with original timestamp <mtime_str>`.
5.  If unchanged: Print `[*] .brain/<rel_path> is already up to date.`

---

## Detailed Code Adjustments

### [CREATE] [git-brain](file:///home/dpavlin/agy-explore/git-brain)
A standalone, fully self-contained python3 script implementing the resolved workflows.

*(No modifications are required for `agy-explore` itself, maintaining strict separation of concerns).*

---

## Verification Plan

### Automated Verification
```bash
python3 -m py_compile git-brain
```

### End-to-End Execution Check
1.  Link the script: `ln -sf /home/dpavlin/agy-explore/git-brain /home/dpavlin/.local/bin/git-brain`.
2.  Run `git-brain` in the `/home/dpavlin/agy-explore` directory without arguments (to verify automatic session lookup).
3.  Verify that `.brain/` is successfully created and populated under `/home/dpavlin/agy-explore/.brain/`.
4.  Verify that commits are correctly registered with their original timestamps using `git log --format=fuller -n 5`.
