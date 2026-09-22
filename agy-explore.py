#!/usr/bin/env python3
import json
import os
import sys
import re
import sqlite3
import urllib.parse
from datetime import datetime
from pathlib import Path

# Load agyp profile manager if available from experiments/
try:
    script_dir = os.path.dirname(os.path.realpath(__file__))
    exp_dir = os.path.join(script_dir, "experiments")
    if exp_dir not in sys.path:
        sys.path.insert(0, exp_dir)
    import agyp
except Exception:
    agyp = None

# ANSI Escape Codes for Beautiful Terminal Output
CLR_RESET = "\033[0m"
CLR_BOLD = "\033[1m"
CLR_DIM = "\033[2m"
CLR_RED = "\033[31m"
CLR_GREEN = "\033[32m"
CLR_YELLOW = "\033[33m"
CLR_BLUE = "\033[34m"
CLR_MAGENTA = "\033[35m"
CLR_CYAN = "\033[36m"
CLR_WHITE = "\033[37m"

BASE_DIR = os.path.expanduser("~/.gemini/antigravity-cli")

DEBUG_MODE = False

def get_all_profiles():
    if agyp:
        try:
            return agyp.list_all_profiles()
        except Exception:
            pass
    profiles = ["default"]
    pdir = Path(os.environ.get("AGY_PROFILES_DIR", "~/.config/agy-profiles")).expanduser()
    if pdir.is_dir():
        for p in sorted(pdir.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                profiles.append(p.name)
    return profiles

def get_profile_gemini_dir(profile_name):
    if agyp:
        try:
            return Path(agyp.get_gemini_cli_dir(profile_name))
        except Exception:
            pass
    if not profile_name or profile_name == "default":
        return Path(os.path.expanduser("~/.gemini/antigravity-cli"))
    pdir = Path(os.environ.get("AGY_PROFILES_DIR", "~/.config/agy-profiles")).expanduser()
    return pdir / profile_name / ".gemini" / "antigravity-cli"

def find_session_locations(conv_id):
    if agyp:
        try:
            return agyp.find_session_locations(conv_id)
        except Exception:
            pass
    found = []
    for prof in get_all_profiles():
        gdir = get_profile_gemini_dir(prof)
        db_file = gdir / "conversations" / f"{conv_id}.db"
        brain_dir = gdir / "brain" / conv_id
        if db_file.exists() or brain_dir.exists():
            found.append((prof, gdir))
    return found

def debug_log(msg):
    if DEBUG_MODE:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        sys.stderr.write(f"{CLR_DIM}{CLR_YELLOW}[DEBUG {timestamp}] {msg}{CLR_RESET}\n")
        sys.stderr.flush()

def print_help():
    print(f"{CLR_BOLD}Explore Conversation Transcripts & Manage Sessions{CLR_RESET}")
    print("Usage:")
    print("  agy-explore                       # List conversations for the current directory")
    print("  agy-explore -a, --all             # List all conversations across all workspaces")
    print("  agy-explore <conversation_id>     # Print full conversation log")
    print("  agy-explore <path_to_jsonl_file>  # Print specific JSONL file")
    print("\nOptions:")
    print("  -a, --all               List all conversations (default shows only current directory)")
    print("  -p, --profile <name>    Filter conversations to a specific profile (default: all profiles)")
    print("  -d, --debug             Enable detailed stderr telemetry logging")
    print("  -v, --verbose           Set verbosity level (-v: show all stats/file details, -vv: show all stats plus full file contents)")
    print("  --grep <words>          Find conversations containing all specified words case-insensitively")
    print("  --turn                  Restrict --grep search to match all words within a single user-assistant turn")
    print("  --search-all            Search all fields (thoughts, tool outputs) instead of just dialogue")
    print("  --first <N>             Specify number of first prompts to show (default: 1)")
    print("  --last <M>              Specify number of last prompts to show (default: 1)")
    print("  --no-color              Disable ANSI color codes")
    print("  --no-thoughts           Exclude internal assistant thinking processes")
    print("  --no-tools              Exclude tool call/output logs")
    print("\nExamples:")
    print("  agy-explore 4ba35ed8-5ef9-497c-b6d9-1a6cb3e11056 | less -R")
    sys.exit(0)

def clean_workspace_path(path_str):
    if not path_str or not isinstance(path_str, str):
        return None
    p = path_str.strip().strip("\"'")
    if p.startswith("file://"):
        p = p[7:]
    p = urllib.parse.unquote(p)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p

def load_workspace_mappings(gemini_dir=None):
    """Builds a conversation-to-workspace mapping from all available telemetry stores:
    1. conversations/*.db (ground truth trajectory metadata)
    2. conversation_summaries.db
    3. cache/conversation_metadata.json
    4. cache/last_conversations.json
    5. history.jsonl
    """
    if gemini_dir is None:
        gemini_dir = BASE_DIR
    gemini_dir = str(gemini_dir)
    mappings = {}

    # 1. conversations/*.db (trajectory metadata blob)
    convs_dir = os.path.join(gemini_dir, "conversations")
    if os.path.isdir(convs_dir):
        db_count = 0
        for f in os.listdir(convs_dir):
            if not f.endswith(".db"):
                continue
            cid = f[:-3]
            try:
                conn = sqlite3.connect(os.path.join(convs_dir, f))
                cur = conn.cursor()
                cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'")
                row = cur.fetchone()
                if row and row[0]:
                    m = re.search(rb"file://(/[^\x00-\x1f\x7f-\xff\s\"'\)]+)", row[0])
                    if m:
                        clean_ws = clean_workspace_path(m.group(1).decode("utf-8", "ignore"))
                        if clean_ws:
                            mappings[cid] = clean_ws
                            db_count += 1
            except Exception as ex:
                debug_log(f"Failed to read trajectory metadata from {f}: {ex}")
                continue
        debug_log(f"Loaded {db_count} workspace mappings from {convs_dir}/*.db")

    # 2. conversation_summaries.db
    summaries_db = os.path.join(gemini_dir, "conversation_summaries.db")
    if os.path.exists(summaries_db):
        try:
            conn = sqlite3.connect(summaries_db)
            cur = conn.cursor()
            cur.execute("SELECT conversation_id, workspace_uris FROM conversation_summaries")
            sum_count = 0
            for cid, ws_json in cur.fetchall():
                if cid not in mappings and ws_json:
                    try:
                        uris = json.loads(ws_json)
                        if uris and isinstance(uris, list):
                            clean_ws = clean_workspace_path(uris[0])
                            if clean_ws:
                                mappings[cid] = clean_ws
                                sum_count += 1
                    except Exception:
                        m = re.search(r"file://(/[^\s\"'\]]+)", ws_json)
                        if m:
                            clean_ws = clean_workspace_path(m.group(1))
                            if clean_ws:
                                mappings[cid] = clean_ws
                                sum_count += 1
            debug_log(f"Loaded {sum_count} additional workspace mappings from {summaries_db}")
        except Exception as ex:
            debug_log(f"Failed to read {summaries_db}: {ex}")

    # 3. cache/conversation_metadata.json
    cache_meta_path = os.path.join(gemini_dir, "cache", "conversation_metadata.json")
    if os.path.exists(cache_meta_path):
        try:
            with open(cache_meta_path, "r", encoding="utf-8") as f:
                cmd = json.load(f)
                meta_count = 0
                for cid, meta in cmd.get("conversations", {}).items():
                    if cid not in mappings:
                        uris = meta.get("summary", {}).get("WorkspaceURIs")
                        if uris and isinstance(uris, list) and uris:
                            clean_ws = clean_workspace_path(uris[0])
                            if clean_ws:
                                mappings[cid] = clean_ws
                                meta_count += 1
                debug_log(f"Loaded {meta_count} additional workspace mappings from {cache_meta_path}")
        except Exception as ex:
            debug_log(f"Failed to read {cache_meta_path}: {ex}")

    # 4. cache/last_conversations.json
    last_conv_path = os.path.join(gemini_dir, "cache", "last_conversations.json")
    if os.path.exists(last_conv_path):
        try:
            with open(last_conv_path, "r", encoding="utf-8") as f:
                last_convs = json.load(f)
                last_count = 0
                for ws, cid in last_convs.items():
                    if cid not in mappings:
                        clean_ws = clean_workspace_path(ws)
                        if clean_ws:
                            mappings[cid] = clean_ws
                            last_count += 1
                debug_log(f"Loaded {last_count} additional workspace mappings from {last_conv_path}")
        except Exception as ex:
            debug_log(f"Failed to read {last_conv_path}: {ex}")

    # 5. history.jsonl
    history_path = os.path.join(gemini_dir, "history.jsonl")
    if os.path.exists(history_path):
        try:
            line_count = 0
            hist_count = 0
            with open(history_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    line_count += 1
                    try:
                        data = json.loads(line)
                        cid = data.get("conversationId")
                        workspace = data.get("workspace")
                        if cid and workspace and cid not in mappings:
                            clean_ws = clean_workspace_path(workspace)
                            if clean_ws:
                                mappings[cid] = clean_ws
                                hist_count += 1
                    except Exception:
                        continue
            debug_log(f"Loaded {hist_count} additional workspace mappings from {line_count} history records in {history_path}.")
        except Exception as ex:
            debug_log(f"Error loading workspace mappings from {history_path}: {ex}")

    debug_log(f"Total resolved workspace mappings for {gemini_dir}: {len(mappings)}")
    return mappings

def infer_workspace_from_transcript(log_path):
    """Infers the project workspace directory from early tool call paths in a transcript log."""
    candidates = {}
    debug_log(f"Attempting transcript heuristic workspace inference on: {log_path}")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 50:
                    break
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                for tc in d.get("tool_calls", []):
                    args = tc.get("args", {})
                    # High confidence directory arguments
                    for k in ["DirectoryPath", "Cwd", "SearchDirectory", "SearchPath"]:
                        p = args.get(k)
                        if p and isinstance(p, str):
                            p = clean_workspace_path(p)
                            if p and os.path.isabs(p) and not p.startswith("/tmp") and ".gemini" not in p:
                                candidates[p] = candidates.get(p, 0) + 3
                    # File path arguments (use parent dir)
                    for k in ["AbsolutePath", "TargetFile"]:
                        p = args.get(k)
                        if p and isinstance(p, str):
                            p = clean_workspace_path(p)
                            if p and os.path.isabs(p) and not p.startswith("/tmp") and ".gemini" not in p:
                                pdir = os.path.dirname(p)
                                candidates[pdir] = candidates.get(pdir, 0) + 1
    except Exception as ex:
        debug_log(f"Error inferring workspace from transcript: {ex}")
        pass

    if candidates:
        best_ws = max(candidates.items(), key=lambda x: x[1])[0]
        debug_log(f"Heuristically inferred workspace '{best_ws}' from transcript {log_path} (candidates: {candidates})")
        return best_ws
    return None

def format_duration(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m {int(seconds % 60)}s"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h {int(minutes % 60)}m"
    days = hours / 24
    return f"{int(days)}d {int(hours % 24)}h"

def format_size(bytes_val):
    if bytes_val < 1024:
        return f"{bytes_val} B"
    kb = bytes_val / 1024.0
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024.0
    return f"{mb:.1f} MB"

def is_text_file(filepath):
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(1024)
        if b'\x00' in chunk:
            return False
        chunk.decode("utf-8", errors="strict")
        return True
    except Exception:
        return False

def extract_searchable_text(step_data, search_all=False):
    parts = []
    source = step_data.get("source")
    step_type = step_data.get("type")
    
    if search_all:
        if step_data.get("content"):
            parts.append(step_data["content"])
        if step_data.get("thinking"):
            parts.append(step_data["thinking"])
        if step_data.get("tool_calls"):
            for tc in step_data["tool_calls"]:
                if tc.get("name"):
                    parts.append(tc["name"])
                if tc.get("args"):
                    parts.append(json.dumps(tc["args"]))
    else:
        is_user_input = (source == "USER_EXPLICIT" and step_type == "USER_INPUT")
        is_assistant_response = (source == "MODEL" and step_type == "PLANNER_RESPONSE")
        if (is_user_input or is_assistant_response) and step_data.get("content"):
            parts.append(step_data["content"])
            
    return " ".join(parts)

def highlight_text(text, words, highlight_color=CLR_BOLD + CLR_YELLOW, context_color=CLR_WHITE):
    if not words or not text:
        return text
    try:
        escaped_words = [re.escape(w) for w in words]
        escaped_words.sort(key=len, reverse=True)
        pattern = re.compile(rf"\b({'|'.join(escaped_words)})", re.IGNORECASE)
        replacement = f"{highlight_color}\\1{CLR_RESET}{context_color}"
        return pattern.sub(replacement, text)
    except Exception:
        return text

def get_generated_brain_files_detailed(session_uuid, gemini_dir=None):
    if gemini_dir is None:
        gemini_dir = BASE_DIR
    session_dir = os.path.join(str(gemini_dir), "brain", session_uuid)
    files_found = []
    if not os.path.isdir(session_dir):
        return files_found
        
    for root, dirs, files in os.walk(session_dir):
        # Exclude .system_generated and hidden directories (starting with .)
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != ".system_generated"]
            
        for file in files:
            # Exclude metadata.json files and hidden files (starting with .)
            if file.endswith(".metadata.json") or file.startswith("."):
                continue
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, session_dir)
            
            size_bytes = 0
            mtime_str = "N/A"
            ext = os.path.splitext(file)[1].lower()
            content = None
            is_text = False
            
            try:
                stat = os.stat(full_path)
                size_bytes = stat.st_size
                mtime_str = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')
                
                is_text = is_text_file(full_path)
                if is_text:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as tf:
                        content = tf.read()
            except Exception:
                pass
                
            files_found.append({
                "path": rel_path,
                "size": size_bytes,
                "mtime_str": mtime_str,
                "ext": ext,
                "is_text": is_text,
                "content": content
            })
            
    return sorted(files_found, key=lambda x: x["path"])

def process_conversation_session(uuid_str, prof, gdir, workspace_mappings, cwd, show_all, search_words, use_turn_matching, search_all):
    brain_dir = os.path.join(str(gdir), "brain")
    log_path = os.path.join(brain_dir, uuid_str, ".system_generated", "logs", "transcript_full.jsonl")
    if not os.path.exists(log_path):
        log_path = os.path.join(brain_dir, uuid_str, ".system_generated", "logs", "transcript.jsonl")
    if not os.path.exists(log_path):
        debug_log(f"Skipping directory {uuid_str} in profile '{prof}': no transcript log found")
        return None

    workspace = workspace_mappings.get(uuid_str)
    if not workspace or workspace == "Unknown Workspace":
        inferred = infer_workspace_from_transcript(log_path)
        if inferred:
            workspace = inferred
            workspace_mappings[uuid_str] = workspace
        else:
            workspace = "Unknown Workspace"

    # Filter by current directory if not showing all
    if not show_all:
        norm_workspace = os.path.realpath(workspace) if workspace != "Unknown Workspace" else None
        if norm_workspace != cwd:
            debug_log(f"Filtering out session {uuid_str} in profile '{prof}': workspace '{norm_workspace}' does not match CWD '{cwd}'")
            return None

    debug_log(f"Processing session: {uuid_str} [profile: {prof}] (workspace: {workspace})")
    mtime = os.path.getmtime(log_path)
    mtime_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')

    user_prompts = []
    start_time = None
    end_time = None

    tool_call_count = 0
    thought_count = 0
    total_steps = 0
    tool_breakdown = {}

    is_matched = True
    try:
        debug_log(f"Opening transcript log file: {log_path}")
        with open(log_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
            total_steps = len(lines)
            debug_log(f"Read {total_steps} log entries for session {uuid_str}")

            steps_searchable = []
            for idx, line in enumerate(lines):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as ex:
                    debug_log(f"JSON decode failed on transcript line {idx + 1} of session {uuid_str}: {ex}")
                    continue
                created_at_str = data.get("created_at")

                # Convert created_at to timestamp
                t_val = None
                if created_at_str and created_at_str != "N/A":
                    try:
                        clean_t = created_at_str.replace("Z", "").split("+")[0]
                        t_val = datetime.fromisoformat(clean_t)
                    except Exception:
                        pass

                if t_val:
                    if start_time is None or t_val < start_time:
                        start_time = t_val
                    if end_time is None or t_val > end_time:
                        end_time = t_val

                source = data.get("source")
                step_type = data.get("type")
                content = data.get("content", "")

                # Track statistics
                if data.get("thinking"):
                    thought_count += 1
                if data.get("tool_calls"):
                    t_calls = data.get("tool_calls")
                    tool_call_count += len(t_calls)
                    for tc in t_calls:
                        tc_name = tc.get("name", "unknown")
                        tool_breakdown[tc_name] = tool_breakdown.get(tc_name, 0) + 1
                elif step_type in ["RUN_COMMAND", "VIEW_FILE", "LIST_DIRECTORY", "GREP_SEARCH", "COMMAND_OUTPUT"]:
                    tool_call_count += 1
                    tool_breakdown[step_type.lower()] = tool_breakdown.get(step_type.lower(), 0) + 1

                if source == "USER_EXPLICIT" and step_type == "USER_INPUT":
                    clean_content = content
                    if "<USER_REQUEST>" in content:
                        clean_content = content.split("<USER_REQUEST>")[-1].split("</USER_REQUEST>")[0].strip()

                    clean_content = clean_content.replace("\n", " ").strip()

                    user_prompts.append(clean_content)

                # Extract searchable text for matching
                steps_searchable.append((source, step_type, extract_searchable_text(data, search_all), idx + 1))

            # Match check
            matched_snippets = []
            total_matches = 0
            if search_words:
                if use_turn_matching:
                    turns_text = []
                    current_turn_parts = []
                    for src, st_type, text, step_num in steps_searchable:
                        if src == "USER_EXPLICIT" and st_type == "USER_INPUT":
                            if current_turn_parts:
                                turns_text.append(" ".join(current_turn_parts))
                                current_turn_parts = []
                        current_turn_parts.append(text)
                    if current_turn_parts:
                        turns_text.append(" ".join(current_turn_parts))

                    matched = False
                    for turn_text in turns_text:
                        turn_matched = True
                        for word in search_words:
                            if not re.search(rf"\b{re.escape(word)}", turn_text, re.IGNORECASE):
                                turn_matched = False
                                break
                        if turn_matched:
                            matched = True
                            break
                    is_matched = matched
                else:
                    session_text = " ".join([text for _, _, text, _ in steps_searchable])
                    matched = True
                    for word in search_words:
                        if not re.search(rf"\b{re.escape(word)}", session_text, re.IGNORECASE):
                            matched = False
                            break
                    is_matched = matched

                # Extract matched snippets
                if is_matched:
                    candidates = []
                    for src, st_type, text, step_num in steps_searchable:
                        desc = ""
                        if src == "USER_EXPLICIT" and st_type == "USER_INPUT":
                            desc = "User Prompt"
                        elif src == "MODEL":
                            if st_type == "PLANNER_RESPONSE":
                                desc = "Assistant Response"
                            else:
                                desc = "Assistant Thought"
                        else:
                            desc = st_type.replace("_", " ").title()

                        lines = text.splitlines()
                        for line in lines:
                            line_stripped = line.strip()
                            if not line_stripped:
                                continue
                            matched_words = {w for w in search_words if re.search(rf"\b{re.escape(w)}", line_stripped, re.IGNORECASE)}
                            if matched_words:
                                total_matches += 1
                                candidates.append({
                                    "step_num": step_num,
                                    "desc": desc,
                                    "line": line_stripped,
                                    "matched_words": matched_words,
                                    "index": len(candidates)
                                })

                    selected_candidates = []
                    uncovered_words = set(search_words)

                    # Pass 1: cover as many uncovered words as possible
                    while len(selected_candidates) < 10 and uncovered_words:
                        best_cand = None
                        best_cover_count = 0
                        for cand in candidates:
                            if cand in selected_candidates:
                                continue
                            cover_count = len(cand["matched_words"] & uncovered_words)
                            if cover_count > best_cover_count:
                                best_cover_count = cover_count
                                best_cand = cand
                            elif cover_count == best_cover_count and best_cover_count > 0:
                                if best_cand is None:
                                    best_cand = cand
                                else:
                                    cand_total = len(cand["matched_words"])
                                    best_total = len(best_cand["matched_words"])
                                    if cand_total > best_total:
                                        best_cand = cand
                                    elif cand_total == best_total:
                                        if cand["index"] < best_cand["index"]:
                                            best_cand = cand

                        if best_cand is None or best_cover_count == 0:
                            break

                        selected_candidates.append(best_cand)
                        uncovered_words -= best_cand["matched_words"]

                    # Pass 2: fill remaining slots up to 10
                    if len(selected_candidates) < 10:
                        for cand in candidates:
                            if len(selected_candidates) >= 10:
                                break
                            if cand not in selected_candidates:
                                selected_candidates.append(cand)

                    # Sort chronologically
                    selected_candidates.sort(key=lambda x: x["index"])

                    for cand in selected_candidates:
                        line_hl = highlight_text(cand["line"], search_words, CLR_BOLD + CLR_YELLOW, CLR_WHITE)
                        matched_snippets.append(
                            f"    {CLR_DIM}[Step {cand['step_num']} | {cand['desc']}]{CLR_RESET} {CLR_WHITE}{line_hl}{CLR_RESET}"
                        )

    except Exception as e:
        debug_log(f"Error processing {uuid_str} in profile '{prof}': {e}")
        print(f"[!] Error processing {uuid_str} in profile '{prof}': {e}")
        is_matched = False

    if not is_matched:
        debug_log(f"Session {uuid_str} did not match grep filter.")
        return None

    duration_str = "N/A"
    if start_time and end_time:
        diff_sec = (end_time - start_time).total_seconds()
        duration_str = format_duration(diff_sec)

    brain_files = get_generated_brain_files_detailed(uuid_str, str(gdir))

    return {
        "uuid": uuid_str,
        "profiles": [prof],
        "gemini_dir": str(gdir),
        "mtime": mtime,
        "mtime_str": mtime_str,
        "user_prompts": user_prompts,
        "duration": duration_str,
        "total_steps": total_steps,
        "tool_calls": tool_call_count,
        "thoughts": thought_count,
        "workspace": workspace,
        "brain_files": brain_files,
        "tool_breakdown": tool_breakdown,
        "matched_snippets": matched_snippets,
        "total_matches": total_matches
    }

def list_conversations(show_all=False, num_first=1, num_last=1, verbosity=0, search_words=None, use_turn_matching=False, search_all=False, profile_filter=None):
    all_profiles = get_all_profiles()
    if profile_filter:
        profiles_to_scan = [p for p in all_profiles if p == profile_filter]
        if not profiles_to_scan:
            print(f"{CLR_RED}[-] Error: Profile '{profile_filter}' not found. Available profiles: {', '.join(all_profiles)}{CLR_RESET}")
            return
    else:
        profiles_to_scan = all_profiles

    conversations = []
    seen_conversations = {}
    cwd = os.path.realpath(os.getcwd())
    debug_log(f"Target query CWD path: {cwd}")

    if show_all:
        prof_info = f" (profile: {profile_filter})" if profile_filter else f" across {len(profiles_to_scan)} profile(s)"
        print(f"[*] Analyzing conversation log(s) across all workspaces{prof_info}...")
    else:
        prof_info = f" [profile: {profile_filter}]" if profile_filter else ""
        print(f"[*] Analyzing conversation log(s) for current directory ({cwd}){prof_info}...")
        print(f"[*] (Use '-a' or '--all' to display all workspaces)")

    if search_words:
        mode_str = "Turn-restricted" if use_turn_matching else "Session-wide"
        words_formatted = ", ".join([f"'{w}'" for w in search_words])
        print(f"[*] Filtered by --grep query: {words_formatted} (Matching Mode: {mode_str})")

    for prof in profiles_to_scan:
        gdir = get_profile_gemini_dir(prof)
        brain_dir = os.path.join(str(gdir), "brain")
        debug_log(f"Scanning profile '{prof}' brain directory: {brain_dir}")
        if not os.path.isdir(brain_dir):
            debug_log(f"Brain directory not found for profile '{prof}' at: {brain_dir}")
            continue

        dirs = []
        for entry in os.listdir(brain_dir):
            full_path = os.path.join(brain_dir, entry)
            if os.path.isdir(full_path):
                dirs.append(entry)
        debug_log(f"Profile '{prof}': found {len(dirs)} candidate directories in brain store.")

        workspace_mappings = load_workspace_mappings(gdir)

        for uuid_str in dirs:
            if uuid_str in seen_conversations:
                if prof not in seen_conversations[uuid_str]["profiles"]:
                    seen_conversations[uuid_str]["profiles"].append(prof)
                continue

            conv_entry = process_conversation_session(
                uuid_str, prof, gdir, workspace_mappings, cwd, show_all,
                search_words, use_turn_matching, search_all
            )
            if conv_entry:
                seen_conversations[uuid_str] = conv_entry
                conversations.append(conv_entry)

    # Sort by mtime descending
    conversations.sort(key=lambda x: x["mtime"], reverse=True)
    
    if not conversations:
        print(f"{CLR_YELLOW}[!] No matching conversation logs found for current directory.{CLR_RESET}")
        print(f"[*] Use {CLR_BOLD}agy-explore -a{CLR_RESET} to list all sessions.")
        return
    
    for idx, c in enumerate(conversations, 1):
        profs_str = ", ".join(c.get("profiles", ["default"]))
        print(f"\n{CLR_BOLD}{CLR_BLUE}================================================================================{CLR_RESET}")
        print(f"{CLR_BOLD}{CLR_GREEN}[#{idx}] Conversation ID: {c['uuid']}{CLR_RESET} {CLR_DIM}(Profile: {profs_str}){CLR_RESET}")
        print(f"{CLR_BOLD}{CLR_BLUE}================================================================================{CLR_RESET}")
        print(f"  {CLR_BOLD}Profile:          {CLR_RESET} {CLR_YELLOW}{profs_str}{CLR_RESET}")
        print(f"  {CLR_BOLD}Project Directory:{CLR_RESET} {CLR_CYAN}{c['workspace']}{CLR_RESET}")
        print(f"  {CLR_BOLD}Active Duration:  {CLR_RESET} {c['duration']} (Last Activity: {c['mtime_str']})")
        print(f"  {CLR_BOLD}Statistics:       {CLR_RESET} {c['total_steps']} total steps | {c['tool_calls']} tool calls executed | {c['thoughts']} reasoning cycles")
        
        # Display Tool Breakdown at Level 1+
        if verbosity >= 1 and c.get("tool_breakdown"):
            breakdown = c.get("tool_breakdown")
            breakdown_str = ", ".join([f"{name}: {count}" for name, count in sorted(breakdown.items())])
            print(f"  {CLR_BOLD}Tool Breakdown:   {CLR_RESET} {CLR_CYAN}{breakdown_str}{CLR_RESET}")
            
        brain_files = c.get("brain_files", [])
        if brain_files:
            if verbosity == 0:
                files_str = ", ".join([f["path"] for f in brain_files])
                files_str_hl = highlight_text(files_str, search_words, CLR_BOLD + CLR_YELLOW, CLR_CYAN)
                print(f"  {CLR_BOLD}Generated Files:  {CLR_RESET} {CLR_CYAN}{files_str_hl}{CLR_RESET}")
            else:
                print(f"  {CLR_BOLD}Generated Files ({len(brain_files)}):{CLR_RESET}")
                for f in brain_files:
                    path_hl = highlight_text(f['path'], search_words, CLR_BOLD + CLR_YELLOW, CLR_CYAN)
                    print(f"    {CLR_CYAN}- {path_hl} ({format_size(f['size'])}, {f['mtime_str']}){CLR_RESET}")
                
                # Display Aggregated Stats
                exts = [f["ext"] for f in brain_files]
                from collections import Counter
                counts = Counter(exts)
                ext_stats = ", ".join([f"{count} {ext if ext else 'no-ext'}" for ext, count in counts.items()])
                total_size = sum([f["size"] for f in brain_files])
                print(f"  {CLR_BOLD}Generated Files Stat:{CLR_RESET} {CLR_CYAN}{len(brain_files)} file(s) ({ext_stats}) | Total Payload: {format_size(total_size)}{CLR_RESET}")
        
        user_prompts = c.get("user_prompts", [])
        
        # Extract first N prompts
        if num_first == 1:
            val = f"\"{user_prompts[0]}\"" if user_prompts else "\"N/A\""
            val_hl = highlight_text(val, search_words, CLR_BOLD + CLR_YELLOW, CLR_WHITE)
            print(f"  {CLR_BOLD}First Prompt:     {CLR_RESET} {CLR_WHITE}{val_hl}{CLR_RESET}")
        elif num_first > 1:
            first_subset = user_prompts[:num_first]
            if first_subset:
                print(f"  {CLR_BOLD}First Prompts ({len(first_subset)}):{CLR_RESET}")
                for p in first_subset:
                    p_hl = highlight_text(p, search_words, CLR_BOLD + CLR_YELLOW, CLR_WHITE)
                    print(f"    {CLR_DIM}-{CLR_RESET} {CLR_WHITE}\"{p_hl}\"{CLR_RESET}")
            else:
                print(f"  {CLR_BOLD}First Prompts:    {CLR_RESET} {CLR_WHITE}\"N/A\"{CLR_RESET}")
                
        # Extract last M prompts
        if num_last == 1:
            val = f"\"{user_prompts[-1]}\"" if user_prompts else "\"N/A\""
            val_hl = highlight_text(val, search_words, CLR_BOLD + CLR_YELLOW, CLR_WHITE)
            print(f"  {CLR_BOLD}Last Prompt:      {CLR_RESET} {CLR_WHITE}{val_hl}{CLR_RESET}")
        elif num_last > 1:
            last_subset = user_prompts[-num_last:]
            if last_subset:
                print(f"  {CLR_BOLD}Last Prompts ({len(last_subset)}):{CLR_RESET}")
                for p in last_subset:
                    p_hl = highlight_text(p, search_words, CLR_BOLD + CLR_YELLOW, CLR_WHITE)
                    print(f"    {CLR_DIM}-{CLR_RESET} {CLR_WHITE}\"{p_hl}\"{CLR_RESET}")
            else:
                print(f"  {CLR_BOLD}Last Prompts:     {CLR_RESET} {CLR_WHITE}\"N/A\"{CLR_RESET}")
        
        # Output resume command instructions
        primary_prof = c.get("profiles", ["default"])[0]
        if primary_prof == "default":
            cli_cmd = f"agy --conversation {c['uuid']}"
        else:
            cli_cmd = f"agyp {primary_prof} --conversation {c['uuid']}"
        if c['workspace'] != "Unknown Workspace":
            resume_cmd = f"cd {c['workspace']} && {cli_cmd}"
        else:
            resume_cmd = cli_cmd
        print(f"  {CLR_BOLD}{CLR_YELLOW}Resume CLI:{CLR_RESET}       {CLR_BOLD}{resume_cmd}{CLR_RESET}")
        if primary_prof != "default":
            alias_cmd = f"cd {c['workspace']} && agy-{primary_prof} --conversation {c['uuid']}" if c['workspace'] != "Unknown Workspace" else f"agy-{primary_prof} --conversation {c['uuid']}"
            print(f"                    {CLR_DIM}(or: {alias_cmd}){CLR_RESET}")
            
        # Display Matched Snippets
        matched_snippets = c.get("matched_snippets", [])
        total_matches = c.get("total_matches", len(matched_snippets))
        if matched_snippets:
            print(f"  {CLR_BOLD}Matched Snippets ({total_matches}):{CLR_RESET}")
            for snip in matched_snippets:
                print(snip)
            if total_matches > len(matched_snippets):
                print(f"    {CLR_DIM}... and {total_matches - len(matched_snippets)} more matching lines ...{CLR_RESET}")
                
        # Display full file content at verbosity level 2+ (-vv), after all stats/prompts/restores are shown
        if verbosity >= 2 and brain_files:
            print(f"\n  {CLR_BOLD}Generated Files Content:{CLR_RESET}")
            for f in brain_files:
                if f['is_text'] and f['content'] is not None:
                    path_hl = highlight_text(f['path'], search_words, CLR_BOLD + CLR_YELLOW, CLR_CYAN)
                    print(f"    {CLR_CYAN}[File: {path_hl}]{CLR_RESET}")
                    print(f"    {CLR_DIM}--------------------------------------------------------------------------------{CLR_RESET}")
                    for l in f['content'].splitlines():
                        l_hl = highlight_text(l, search_words, CLR_BOLD + CLR_YELLOW, CLR_WHITE)
                        print(f"    {CLR_WHITE}{l_hl}{CLR_RESET}")
                    print(f"    {CLR_DIM}--------------------------------------------------------------------------------{CLR_RESET}")
            
    print("\n" + "="*80 + "\n")

def render_transcript(file_path, use_color=True, show_thoughts=True, show_tools=True):
    if not use_color:
        global CLR_RESET, CLR_BOLD, CLR_DIM, CLR_RED, CLR_GREEN, CLR_YELLOW, CLR_BLUE, CLR_MAGENTA, CLR_CYAN, CLR_WHITE
        CLR_RESET = CLR_BOLD = CLR_DIM = CLR_RED = CLR_GREEN = CLR_YELLOW = CLR_BLUE = CLR_MAGENTA = CLR_CYAN = CLR_WHITE = ""
        
    debug_log(f"Starting rendering for transcript file: {file_path}")
    print(f"{CLR_BOLD}{CLR_CYAN}[*] Streaming transcript: {file_path}{CLR_RESET}\n")
    
    step_count = 0
    pending_tool_calls = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            if not line.strip():
                continue
            step_count += 1
            try:
                step = json.loads(line)
            except json.JSONDecodeError as e:
                debug_log(f"JSON decode failed on transcript line {line_num}: {e}")
                print(f"{CLR_RED}[!] JSON decode error at line {line_num}: {e}{CLR_RESET}")
                continue
                
            source = step.get("source")
            step_type = step.get("type")
            time = step.get("created_at", "N/A")
            content = step.get("content", "").strip()
            thinking = step.get("thinking", "").strip()
            tool_calls = step.get("tool_calls", [])
            
            if source == "USER_EXPLICIT" and step_type == "USER_INPUT":
                pending_tool_calls.clear()
                req = content
                if "<USER_REQUEST>" in content:
                    req = content.split("<USER_REQUEST>")[-1].split("</USER_REQUEST>")[0].strip()
                print(f"{CLR_BOLD}{CLR_BLUE}================================================================================{CLR_RESET}")
                print(f"{CLR_BOLD}{CLR_BLUE}👤 USER INPUT | Timestamp: {time}{CLR_RESET}")
                print(f"{CLR_BOLD}{CLR_BLUE}================================================================================{CLR_RESET}")
                print(f"{req}\n")
                
            elif source == "MODEL":
                if step_type == "PLANNER_RESPONSE":
                    if show_thoughts and thinking:
                        print(f"{CLR_DIM}{CLR_YELLOW}--------------------------------------------------------------------------------{CLR_RESET}")
                        print(f"{CLR_DIM}{CLR_YELLOW}🤖 ASSISTANT INTERNAL THOUGHTS{CLR_RESET}")
                        print(f"{CLR_DIM}{CLR_YELLOW}--------------------------------------------------------------------------------{CLR_RESET}")
                        print(f"{CLR_DIM}{CLR_YELLOW}{thinking}{CLR_RESET}\n")
                        
                    if content:
                        print(f"{CLR_BOLD}{CLR_GREEN}--------------------------------------------------------------------------------{CLR_RESET}")
                        print(f"{CLR_BOLD}{CLR_GREEN}🤖 ASSISTANT RESPONSE | Timestamp: {time}{CLR_RESET}")
                        print(f"{CLR_BOLD}{CLR_GREEN}--------------------------------------------------------------------------------{CLR_RESET}")
                        print(f"{content}\n")
                        
                    if tool_calls:
                        for tc in tool_calls:
                            tc_name = tc.get("name") or "tool"
                            pending_tool_calls.append(tc_name)
                        if show_tools:
                            print(f"{CLR_BOLD}{CLR_MAGENTA}🛠️ TOOL CALLS REQUESTED:{CLR_RESET}")
                            for tc in tool_calls:
                                tc_name = tc.get("name")
                                tc_args = json.dumps(tc.get("args"), indent=2)
                                print(f"  {CLR_BOLD}Tool: {tc_name}{CLR_RESET}")
                                print(f"  Arguments:\n{CLR_DIM}{tc_args}{CLR_RESET}\n")
                            
                elif step_type == "GENERIC" or step_type in [
                    "RUN_COMMAND", "VIEW_FILE", "LIST_DIRECTORY", "GREP_SEARCH",
                    "COMMAND_OUTPUT", "CODE_ACTION", "READ_URL_CONTENT",
                    "SEARCH_WEB", "ASK_QUESTION"
                ]:
                    tool_label = pending_tool_calls.pop(0) if pending_tool_calls else step_type
                    if show_tools:
                        if step_type != "GENERIC" and step_type.lower() != tool_label.lower():
                            display_label = f"{tool_label} ({step_type})"
                        else:
                            display_label = tool_label
                        print(f"{CLR_BOLD}{CLR_CYAN}⚙️ TOOL EXECUTION OUTPUT | {display_label} | Timestamp: {time}{CLR_RESET}")
                        print(f"{CLR_CYAN}{content}{CLR_RESET}\n")

            elif source == "SYSTEM" and step_type == "ERROR_MESSAGE":
                err_text = step.get("error") or content
                if err_text:
                    print(f"{CLR_BOLD}{CLR_RED}⚠️ SYSTEM ERROR | Timestamp: {time}{CLR_RESET}")
                    print(f"{CLR_RED}{err_text}{CLR_RESET}\n")
    debug_log(f"Successfully rendered {step_count} transcript steps.")

def main():
    global DEBUG_MODE
    args = sys.argv[1:]
    
    if "--help" in args or "-h" in args:
        print_help()
        
    use_color = "--no-color" not in args
    show_thoughts = "--no-thoughts" not in args
    show_tools = "--no-tools" not in args
    show_all = "-a" in args or "--all" in args
    
    # Parse verbosity levels dynamically
    verbosity = 0
    for arg in args:
        if arg == "-v":
            verbosity = max(verbosity, 1)
        elif arg == "-vv":
            verbosity = max(verbosity, 2)
        elif arg == "-vvv":
            verbosity = max(verbosity, 3)
        elif arg == "-vvvv":
            verbosity = max(verbosity, 4)
        elif arg == "--verbose":
            verbosity = max(verbosity, 1)
        elif arg.startswith("-") and not arg.startswith("--"):
            clean_arg = arg[1:]
            if "v" in clean_arg:
                v_count = clean_arg.count("v")
                verbosity = max(verbosity, v_count)
                
    DEBUG_MODE = "-d" in args or "--debug" in args
    
    num_first = 1
    num_last = 1
    
    if "--first" in args:
        try:
            idx = args.index("--first")
            if idx + 1 < len(args):
                num_first = int(args[idx + 1])
            else:
                print(f"{CLR_RED}[-] Error: --first requires a numeric argument.{CLR_RESET}")
                sys.exit(1)
        except ValueError:
            print(f"{CLR_RED}[-] Error: --first requires a numeric argument.{CLR_RESET}")
            sys.exit(1)
            
    if "--last" in args:
        try:
            idx = args.index("--last")
            if idx + 1 < len(args):
                num_last = int(args[idx + 1])
            else:
                print(f"{CLR_RED}[-] Error: --last requires a numeric argument.{CLR_RESET}")
                sys.exit(1)
        except ValueError:
            print(f"{CLR_RED}[-] Error: --last requires a numeric argument.{CLR_RESET}")
            sys.exit(1)
            
    profile_filter = None
    if "-p" in args or "--profile" in args:
        try:
            p_idx = args.index("-p") if "-p" in args else args.index("--profile")
            if p_idx + 1 < len(args):
                profile_filter = args[p_idx + 1]
            else:
                print(f"{CLR_RED}[-] Error: --profile requires a profile name.{CLR_RESET}")
                sys.exit(1)
        except ValueError:
            pass

    debug_log(f"Starting agy-explore. Arguments: {args}")
    debug_log(f"Options parsed - show_all: {show_all}, profile: {profile_filter}, use_color: {use_color}, show_thoughts: {show_thoughts}, show_tools: {show_tools}, debug: {DEBUG_MODE}, num_first: {num_first}, num_last: {num_last}, verbosity: {verbosity}")
    
    # Parse grep options and search terms
    search_words = []
    use_turn_matching = "--turn" in args or "--grep-turn" in args
    search_all = "--search-all" in args or "--all-fields" in args
    
    if "--grep" in args:
        idx = args.index("--grep")
        grep_terms = []
        for arg in args[idx+1:]:
            if arg.startswith("-"):
                break
            grep_terms.append(arg)
        for term in grep_terms:
            search_words.extend([w.strip() for w in term.split() if w.strip()])
            
        debug_log(f"Grep search enabled. Words: {search_words}, Turn-matching: {use_turn_matching}")
        list_conversations(
            show_all=show_all,
            num_first=num_first,
            num_last=num_last,
            verbosity=verbosity,
            search_words=search_words,
            use_turn_matching=use_turn_matching,
            search_all=search_all,
            profile_filter=profile_filter
        )
        sys.exit(0)

    # Robustly parse positional arguments while excluding options and their values
    pos_args = []
    skip_next = False
    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg in ["--first", "--last", "-p", "--profile"]:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        pos_args.append(arg)

    if not pos_args:
        debug_log("No positional arguments. Cataloging active conversations.")
        list_conversations(
            show_all=show_all,
            num_first=num_first,
            num_last=num_last,
            verbosity=verbosity,
            profile_filter=profile_filter
        )
        sys.exit(0)

    target = pos_args[0]

    # Check if target is a file path
    debug_log(f"Evaluating target argument: '{target}'")
    if os.path.exists(target):
        debug_log(f"Target '{target}' exists as a local file. Rendering transcript.")
        render_transcript(target, use_color, show_thoughts, show_tools)
        sys.exit(0)

    # Check if target matches a conversation across profiles
    locations = find_session_locations(target)
    if profile_filter:
        locations = [(p, g) for p, g in locations if p == profile_filter]

    for prof, gdir in locations:
        gdir_str = str(gdir)
        brain_path = os.path.join(gdir_str, "brain", target, ".system_generated", "logs", "transcript_full.jsonl")
        if not os.path.exists(brain_path):
            brain_path = os.path.join(gdir_str, "brain", target, ".system_generated", "logs", "transcript.jsonl")
        debug_log(f"Checking profile '{prof}' brain transcript path: '{brain_path}'")
        if os.path.exists(brain_path):
            debug_log(f"Target matches brain path in profile '{prof}'. Rendering transcript: {brain_path}")
            render_transcript(brain_path, use_color, show_thoughts, show_tools)
            sys.exit(0)

        # Check binary state files
        pb_path = os.path.join(gdir_str, "conversations", f"{target}.pb")
        db_path = os.path.join(gdir_str, "conversations", f"{target}.db")
        debug_log(f"Checking profile '{prof}' conversations path: '{pb_path}' or '{db_path}'")
        if os.path.exists(pb_path) or os.path.exists(db_path):
            found_path = pb_path if os.path.exists(pb_path) else db_path
            debug_log(f"Target matches state file '{found_path}' in profile '{prof}'. Redirecting.")
            print(f"{CLR_YELLOW}[!] Found binary state file at {found_path} (Profile: {prof}).{CLR_RESET}")
            print(f"[*] Redirecting automatically to the JSONL log file under brain directory...")
            redirect_path = os.path.join(gdir_str, "brain", target, ".system_generated", "logs", "transcript_full.jsonl")
            if not os.path.exists(redirect_path):
                redirect_path = os.path.join(gdir_str, "brain", target, ".system_generated", "logs", "transcript.jsonl")
            debug_log(f"Redirecting to: {redirect_path}")
            if os.path.exists(redirect_path):
                render_transcript(redirect_path, use_color, show_thoughts, show_tools)
                sys.exit(0)
            else:
                debug_log(f"Redirect transcript file path does not exist: {redirect_path}")
                print(f"{CLR_RED}[-] Error: Log file {redirect_path} does not exist.{CLR_RESET}")
                sys.exit(1)

    print(f"{CLR_RED}[-] Error: Could not resolve '{target}' to an existing file or conversation ID in any profile.{CLR_RESET}")
    print("Run without arguments to list all active conversations.")
    sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except Exception:
            pass
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)

