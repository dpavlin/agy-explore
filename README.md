# agy-explore

`agy-explore` is an advanced terminal-based conversation transcript explorer and session management utility designed for the **Antigravity CLI** agent platform.

It aggregates statistics, active time spans, workspace directories, tool executions, and prompt contexts across all your agent sessions, providing a beautiful color-coded interactive visualization in your terminal.

## Key Features

- 👤 **Prompt Identity Mapping:** Parses and displays customizable subsets of the first and last prompts in full uncropped detail to instantly identify what the session was about (using the `--first` and `--last` options).
- ⚙️ **No Truncation Streams:** Allows you to read raw tool execution results and assistant plans in full detail without arbitrary line constraints.
- ⏱️ **Time Span Calculations:** Displays active duration ranges (in seconds, minutes, hours, or days) along with the last activity timestamp.
- 🛠️ **Detailed Session Statistics:** Aggregates logs to show step counts, tool execution rates, and assistant reasoning/thinking iterations.
- 📂 **Workspace Detection:** Automatically maps conversation IDs to their active local directories.
- 🔄 **Active Pointer Restoration (`--restore`):** Modifies the CLI session pointers cache seamlessly, allowing you to instantly switch active contexts in the target workspace.

---

## Installation

Simply copy or link `agy-explore` into your system path (e.g. `~/.local/bin/` or `/usr/local/bin/`) and mark it as executable:

```bash
cp agy-explore ~/.local/bin/
chmod +x ~/.local/bin/agy-explore
```

Now you can type `agy-` in your terminal and press `<TAB>` for automatic tab completion!

---

## Command Usage

### 1. Catalog Conversations for Current Directory
To display a highly data-dense, structured overview of past conversations filtered by the current workspace:
```bash
agy-explore
```

### 2. Catalog All Conversations Across All Workspaces
To list all past conversations globally across all projects/directories:
```bash
agy-explore -a
# or: agy-explore --all
```

### 3. Display Custom Prompt Subsets in Full
To list conversations and view the first 2 and last 2 user prompts in full uncropped detail:
```bash
agy-explore -a --first 2 --last 2
```

### 4. Stream a Conversation Log to `less`
To read a specific conversation session with full terminal color highlighting and tool detail:
```bash
agy-explore <conversation_id> | less -R
```

### 5. Restore an Active Session Pointer
To mark a past conversation as the active pointer for its project workspace (updates `last_conversations.json` pointer):
```bash
agy-explore --restore <conversation_id>
```

---

## CLI Options

- `-a`, `--all`: List all conversations globally (by default, only current directory sessions are listed).
- `-d`, `--debug`: Enable detailed real-time diagnostic logging (emitted to `sys.stderr` for easy redirection).
- `--first <N>`: Specify the number of first prompts to show for each conversation (default: `1`).
- `--last <M>`: Specify the number of last prompts to show for each conversation (default: `1`).
- `--no-color`: Disable ANSI color terminal sequences.
- `--no-thoughts`: Exclude assistant internal reasoning blocks to see only final user/assistant dialog.
- `--no-tools`: Exclude raw tool execution logs.

---

## Technical Details

- **Binary Backend:** Integrates natively with `~/.local/bin/agy` (Antigravity CLI).
- **Log Files Location:** Reads from `~/.gemini/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript.jsonl` and `~/.gemini/antigravity-cli/history.jsonl`.
- **Cache File Location:** Manages pointers in `~/.gemini/antigravity-cli/cache/last_conversations.json`.
