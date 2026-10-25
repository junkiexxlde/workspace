#!/usr/bin/env python3
"""
chat-gemma.py -- streaming terminal chat for the server started by start-gemma-26b.sh

The LM Studio runtime only ships llama.cpp, without the usual bundled web UI
(the root path returns 404), so this is the convenient way to actually talk to
the model. It streams tokens as they arrive, shows the reasoning steps dimmed,
and reports tokens per second.

Usage:
    ./chat-gemma.py                       # interactive chat
    ./chat-gemma.py -p "Explain MoE"      # one answer, then exit (scriptable)
    ./chat-gemma.py --think off           # start with reasoning disabled
    ./chat-gemma.py --url http://127.0.0.1:8081
    ./chat-gemma.py --url http://127.0.0.1:8080 --model gemma-4-26b-a4b-it-qat
    ./chat-gemma.py --allow ~/notes       # grant read access up front
    ./chat-gemma.py --allow-write ~/docs  # grant write access to a folder
    ./chat-gemma.py --no-tools            # plain chat, no file access at all

In the interactive session:
    /think on|off     toggle the reasoning steps (no server restart needed)
    /system TEXT      replace the system prompt
    /model NAME       set the model name sent to the server (bare /model shows it)
    /tools on|off     allow or refuse the read-only file tools
    /allow PATH       grant read access to a folder for this session
    /allow-write PATH grant write access to a folder for this session
    /grants           show what is granted (read and write)
    /revoke PATH      drop a grant
    /reset            forget the conversation
    /save [FILE]      write the conversation to FILE as Markdown
    /help             this list
    /exit, /quit      quit (Ctrl+D works too)

File access is read-only and asks first. The model can call list_folder, read_file,
and search_files, but the client refuses any path the user has not granted: the first
time a folder is touched it prints the exact path and asks
[o]nce / [f]older for this session / [n]o. Granting is per folder and lasts for
the session (or comes from --allow). Write access must be explicitly granted with
--allow-write or /allow-write; read access never implies write permission. Without a
terminal to ask on (piped stdin, -p) nothing is granted implicitly: unreadable paths
are simply denied.

Editing works like a normal terminal program. GNU readline provides arrows,
Home/End, Ctrl+A/E/K/U/W and Tab completion, and the input history is kept in
$XDG_STATE_HOME/chat-gemma/history (~/.local/state/chat-gemma/history by
default). A line ending in a backslash continues on the next line, so multi-line
prompts are possible, and terminals that support bracketed paste deliver a pasted
block as a single message. Ctrl+C clears the line being typed (during an answer
it stops the answer), Ctrl+D leaves. Colours are dropped when the output is not a
terminal or NO_COLOR is set.

The server must be running, e.g. ./start-gemma-26b.sh --think off.

Persisted grants:
    Grants are saved to ~/.config/chat-gemma/grants.json across sessions (if the
    directory exists). They can be overridden by command-line flags and /allow
    commands. Use /revoke or /tools off to clear them, or delete the file manually.
    Write grants are stored separately from read grants for safety.
"""

import argparse
import glob
import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.request
from fnmatch import fnmatch

try:
    import readline
except ImportError:  # pragma: no cover - Windows or a minimal Python build
    readline = None


def _use_color():
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


COLOR = _use_color()
DIM = "\033[2m" if COLOR else ""
BOLD = "\033[1m" if COLOR else ""
CYAN = "\033[36m" if COLOR else ""
YELLOW = "\033[33m" if COLOR else ""
RESET = "\033[0m" if COLOR else ""

COMMANDS = ["/think", "/system", "/model", "/tools", "/allow", "/allow-write",
            "/grants", "/revoke", "/reset", "/save", "/help", "/exit", "/quit"]

HELP = """\
Commands:
  /think on|off    toggle the reasoning steps
  /system TEXT     replace the system prompt
  /model NAME      set the model name sent to the server (bare /model shows it)
  /reset           forget the conversation
  /save [FILE]     write the conversation to FILE (default: chat-<timestamp>.md)
  /help            this list
  /exit, /quit     quit

File access (read-only and writes, where you allow them):
  /tools on|off    allow or refuse the file tools for this session
  /allow PATH      grant read access to a path (no prompt for it any more)
  /allow-write PATH grant write access to a folder (creates files in it)
  /grants          show what is granted (read and write roots)
  /revoke PATH     drop a grant

<...648 lines remaining, use read_file_lines(path="chat-gemma.py", start_line=101, lines_to_read=648) to continue reading>"""

# The model gets these; every path they name is checked against the grants.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_folder",
            "description": "List the entries of one folder (not recursive).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Folder path, absolute or starting with ~"},
                    "pattern": {"type": "string", "description": "Optional glob to filter names, e.g. *.md"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file, optionally a slice of it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path, absolute or starting with ~"},
                    "start_line": {"type": "integer", "description": "First line to read (default 1)"},
                    "lines": {"type": "integer", "description": "How many lines (default 200, max 500)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for files matching a pattern in a folder (recursive). Returns file paths and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Folder path to start search, absolute or starting with ~"},
                    "pattern": {"type": "string", "description": "Glob pattern for filenames (e.g. *.md)"},
                    "max_results": {"type": "integer", "description": "Maximum files to return (default 50)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text to a file. Only works in folders where the user granted write access with /allow-write.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path, absolute or starting with ~"},
                    "content": {"type": "string", "description": "The text content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
]

TOOLS_NOTE = (
    "You can read the user's files with the tools list_folder, read_file, and search_files. "
    "Access is read-only for reading and folder-by-folder, and only where the user granted it: a path "
    "that was not granted is denied, and the user is asked first. If a read is refused, "
    "say so instead of guessing. Never claim to have read something you did not read.\n\n"
    "For writing, use write_file — it only works in folders where the user explicitly granted write access "
    "with /allow-write or --allow-write. A read grant never implies write permission."
)

MAX_TOOL_ROUNDS = 12
MAX_LIST_ENTRIES = 200
MAX_TOOL_CHARS = 8000
MAX_SEARCH_RESULTS = 50


class ChatError(Exception):
    """The server could not be reached, or answered with an error."""


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def newline_after_stream():
    """End streamed output without printing a stray blank line when uncoloured."""
    sys.stdout.write(RESET + "\n" if COLOR else "\n")
    sys.stdout.flush()


def arg_of(text):
    """'/think off' -> 'off', '/think' -> ''."""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def human_size(count):
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# --------------------------------------------------------------------------- #
# Talking to the model
# --------------------------------------------------------------------------- #

def chat(url, model, messages, think, max_tokens, tools=None):
    """Stream one completion.

    Returns (content, reasoning, tool_calls, tokens, seconds). Tool calls arrive
    as streamed fragments and are reassembled by index.
    """
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": bool(think)},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )

    answer, reasoning, tokens = [], [], 0
    calls = {}
    started = time.time()
    first_token = None
    in_reasoning = False
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                for fragment in delta.get("tool_calls") or []:
                    slot = calls.setdefault(fragment.get("index", 0), {
                        "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if fragment.get("id"):
                        slot["id"] = fragment["id"]
                    function = fragment.get("function") or {}
                    if function.get("name"):
                        slot["function"]["name"] += function["name"]
                    if function.get("arguments"):
                        slot["function"]["arguments"] += function["arguments"]
                piece = delta.get("reasoning_content")
                if piece:
                    if first_token is None:
                        first_token = time.time() - started
                    if not in_reasoning:
                        print(DIM + "[thinking] ", end="", flush=True)
                        in_reasoning = True
                    reasoning.append(piece)
                    tokens += 1
                    print(piece, end="", flush=True)
                piece = delta.get("content")
                if piece:
                    if first_token is None:
                        first_token = time.time() - started
                    if in_reasoning:
                        print(RESET + "\n" + BOLD, end="", flush=True)
                        in_reasoning = False
                    answer.append(piece)
                    tokens += 1
                    print(piece, end="", flush=True)
    except urllib.error.HTTPError as exc:
        newline_after_stream()
        raise ChatError(
            f"Server returned HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}"
        )
    except urllib.error.URLError as exc:
        newline_after_stream()
        raise ChatError(
            f"Cannot reach {url} ({exc.reason}). Start the server first: ./start-gemma-26b.sh"
        )
    except KeyboardInterrupt:
        print(RESET + "\n" + DIM + "(interrupted - the partial answer is kept)" + RESET)

    elapsed = time.time() - started
    newline_after_stream()
    if tokens:
        ttft = first_token if first_token is not None else elapsed
        window = max(elapsed - ttft, 0.05)
        print(f"{DIM}  {tokens} tokens | TTFT {ttft:.2f}s | generation {tokens / window:.1f} tok/s{RESET}")
    ordered = [calls[index] for index in sorted(calls)]
    return "".join(answer), "".join(reasoning), ordered, tokens, elapsed


# --------------------------------------------------------------------------- #
# File tools -- every path is checked against the user's grants
# --------------------------------------------------------------------------- #

def resolve(path):
    return os.path.realpath(os.path.expanduser(path))


def covered_read(path, roots):
    """True when path is one of roots, or lives inside one of them."""
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def covered_write(path, roots):
    """For writes: the parent folder must be in grants."""
    parent = os.path.dirname(path)
    return covered_read(parent, roots)


def can_prompt():
    return sys.stdin.isatty()


def ask_permission(tool, path, is_folder=True):
    """Ask for read access. Returns 'once', 'folder' or 'deny'."""
    kind = "folder" if is_folder else "file"
    print(f"{YELLOW}the model wants to read {short_path(path)}{RESET}")
    print(f"{DIM}  tool: {tool} | not granted yet | nothing is read before you answer{RESET}")
    try:
        answer = input(f"  allow this {kind}? [o]nce / [f]{kind} for this session / [n]o > ")
    except (EOFError, KeyboardInterrupt):
        print()
        return "deny"
    answer = answer.strip().lower()
    if answer in ("o", "once"):
        return "once"
    if answer in ("f", f"{kind}", "folder", "file"):
        return "folder"
    return "deny"


def tool_list_folder(path, pattern):
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        return f"Error: cannot list {path}: {exc.strerror}"
    if not os.path.isdir(path):
        return f"Error: {path} is not a folder"
    matched = [name for name in names if fnmatch(name, pattern or "*")]
    lines = []
    for name in matched[:MAX_LIST_ENTRIES]:
        full = os.path.join(path, name)
        try:
            lines.append(f"{name}/" if os.path.isdir(full) else f"{name}  {human_size(os.path.getsize(full))}")
        except OSError:
            lines.append(name)
    header = f"{path}: {len(names)} entries, {len(matched)} matching {pattern!r}"
    if not lines:
        return header + "\n(no matches)"
    if len(matched) > MAX_LIST_ENTRIES:
        lines.append(f"... {len(matched) - MAX_LIST_ENTRIES} more not shown")
    return header + "\n" + "\n".join(lines)


def tool_read_file(path, start_line, lines):
    if not os.path.isfile(path):
        return f"Error: {path} is not a file"
    try:
        start = max(1, int(start_line if start_line is not None else 1))
    except (TypeError, ValueError):
        start = 1
    try:
        count = min(500, max(1, int(lines if lines is not None else 200)))
    except (TypeError, ValueError):
        count = 200
    try:
        with open(path, "rb") as probe:
            if b"\0" in probe.read(4096):
                return f"{path} looks like a binary file, not text."
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            chunk = list(itertools.islice(handle, start - 1, start - 1 + count))
            more = bool(handle.readline())
    except OSError as exc:
        return f"Error: cannot read {path}: {exc.strerror}"
    body = "\n".join(line.rstrip("\n") for line in chunk)
    if len(body) > MAX_TOOL_CHARS:
        body = body[:MAX_TOOL_CHARS] + "\n... [truncated]"
    span = f"lines {start}-{start + len(chunk) - 1}" if chunk else "no lines"
    tail = ", more lines follow" if more else ""
    return f"{path}, {span}{tail}:\n{body}"


def tool_search_files(path, pattern, max_results):
    """Recursive file search with glob filtering. Returns matching paths and snippets."""
    try:
        entries = os.scandir(path)
    except OSError as exc:
        return f"Error: cannot access {path}: {exc.strerror}"
    if not os.path.isdir(path):
        return f"Error: {path} is not a folder"

    max_results = min(max_results or MAX_SEARCH_RESULTS, MAX_SEARCH_RESULTS)
    results = []
    queue = [(path, 0)]
    depth = 0
    while queue and len(results) < max_results:
        current_path, _ = queue.pop(0)
        try:
            for entry in os.scandir(current_path):
                if not entry.is_file():
                    continue
                full_path = entry.path
                base_name = os.path.basename(full_path)
                if fnmatch(base_name, pattern or "*"):
                    results.append({
                        "path": full_path,
                        "depth": depth + 1,
                        "snippet": tool_read_file(full_path, None, 5).split("\n", 1)[1] if len(tool_read_file(full_path, None, 5).split("\n", 1)) > 1 else "(empty)"
                    })
        except (OSError, PermissionError):
            pass

    header = f"search_files({short_path(path)}, {pattern!r}, max={max_results or MAX_SEARCH_RESULTS})"
    if not results:
        return header + "\n(no matches)"

    lines = []
    for i, r in enumerate(results[:max_results], 1):
        lines.append(f"{i}. {short_path(r['path'])} (depth={r['depth']})")
        snippet = r["snippet"].strip()
        if len(snippet) > 60:
            snippet = snippet[:57] + "..."
        lines.append(f"   {snippet}")
    return header + "\n\n" + "\n".join(lines)


def tool_write_file(path, content):
    """Write content to a file. Only works in folders with explicit write grant."""
    if not os.path.isfile(path) and not os.path.isdir(os.path.dirname(path)):
        return f"Error: folder for {path} does not exist"
    try:
        dirname = os.path.dirname(path) or "."
        parent_realpath = resolve(dirname)
    except Exception:
        return f"Error: cannot resolve path"

    if not covered_write(parent_realpath, grants["write_roots"]):
        if not can_prompt():
            return (f"Denied: write access to {short_path(path)} was not granted, and there is no "
                    f"terminal to ask the user on.")
        # This shouldn't happen for writes — we should always prompt first
        # But just in case, try to get permission
        print(f"{YELLOW}the model wants to write to {short_path(path)}{RESET}")
        print(f"{DIM}  tool: write_file | not granted yet{RESET}")
        while True:
            answer = input("  grant write access? [o]nce / [f]older for this session / [n]o > ")
            if answer.strip().lower() in ("o", "once"):
                grants["write_roots"].append(parent_realpath)
                print(f"{DIM}  (granted write access to {short_path(parent_realpath)}){RESET}")
                break
            elif answer.strip().lower() in ("f", "folder", "file"):
                grants["write_roots"].append(parent_realpath)
                print(f"{DIM}  (granted write access to {short_path(parent_realpath)} for this session){RESET}")
                break
            else:
                return f"Denied: the user did not grant write access to {path}."

    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        return f"OK: wrote {human_size(size)} to {short_path(path)}"
    except OSError as exc:
        return f"Error: cannot write {path}: {exc.strerror}"


def run_tool(call, grants):
    """Execute one tool call. Asks the user first if the path is not granted yet."""
    name = call["function"]["name"]
    raw = call["function"].get("arguments") or "{}"
    try:
        args = json.loads(raw)
        if not isinstance(args, dict):
            raise ValueError("arguments are not an object")
    except (json.JSONDecodeError, ValueError) as exc:
        return f"Error: could not read the arguments ({exc})"
    if name not in ("list_folder", "read_file", "search_files", "write_file"):
        return f"Error: unknown tool {name}"

    given = str(args.get("path") or "").strip()
    if not given:
        return "Error: 'path' is missing"

    path = resolve(given)

    if name in ("list_folder", "read_file", "search_files"):
        # Read tools need read grant
        if not covered_read(path, grants["roots"]):
            if not os.path.exists(path) and name != "search_files":
                return f"Error: {path} does not exist"
            if not can_prompt():
                return (f"Denied: read access to {path} was not granted, and there is no "
                        f"terminal to ask the user on.")
            choice = ask_permission(name, path, name == "list_folder")
            if choice == "deny":
                return f"Denied: the user did not grant read access to {path}."
            if choice == "folder":
                grants["roots"].append(path)
                print(f"{DIM}  (granted read access to {short_path(path)} for this session){RESET}")

    if name == "write_file":
        return tool_write_file(path, args.get("content") or "")
    if name in ("list_folder", "read_file"):
        return tool_list_folder if name == "list_folder" else tool_read_file(
            path, args.get("start_line"), args.get("lines"))

    if name == "search_files":
        return tool_search_files(path, str(args.get("pattern") or "*"), args.get("max_results"))

    # Fallback (shouldn't reach here)
    return f"Error: unknown tool {name}"


def ask_model(url, model, messages, think, max_tokens, tools, grants):
    """Run the model until it answers in text, executing tool calls on the way."""
    for _ in range(MAX_TOOL_ROUNDS):
        content, _, calls, _, _ = chat(url, model, messages, think, max_tokens, tools)
        if not calls:
            messages.append({"role": "assistant", "content": content})
            return content
        messages.append({"role": "assistant", "content": content, "tool_calls": calls})
        for call in calls:
            arguments = call["function"].get("arguments") or "{}"
            print(f"{DIM}  -> {call['function']['name']} {arguments[:180]}{RESET}")
            result = run_tool(call, grants)
            first = (result.splitlines() or [""])[0]
            print(f"{DIM}     {first[:160]}{RESET}")
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    print(f"{YELLOW}(tool round limit reached, asking for a final answer){RESET}")
    content, _, _, _, _ = chat(url, model, messages, think, max_tokens, None)
    messages.append({"role": "assistant", "content": content})
    return content


# --------------------------------------------------------------------------- #
# Session plumbing with persisted grants
# --------------------------------------------------------------------------- #

def grants_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "chat-gemma")


def grants_file():
    return os.path.join(grants_dir(), "grants.json")


def load_persisted_grants():
    """Load persisted grants from disk. Returns a dict with read_roots and write_roots."""
    path = grants_file()
    if not os.path.exists(path):
        return {"roots": [], "write_roots": []}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return {
            "roots": [resolve(p) for p in data.get("roots", [])],
            "write_roots": [resolve(p) for p in data.get("write_roots", [])]
        }
    except (json.JSONDecodeError, OSError):
        return {"roots": [], "write_roots": []}


def save_persisted_grants(grants):
    """Save grants to disk."""
    path = grants_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "roots": [os.path.realpath(r) for r in grants["roots"]],
            "write_roots": [os.path.realpath(r) for r in grants["write_roots"]]
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except OSError:
        pass


def clear_persisted_grants():
    """Clear persisted grants file."""
    path = grants_file()
    try:
        os.remove(path)
        print(f"{DIM}(persisted grants cleared from {short_path(path)}){RESET}\n")
    except OSError:
        pass


def system_message(system_text, tools_on):
    content = system_text
    if tools_on:
        content = f"{system_text}\n\n{TOOLS_NOTE}"
    return {"role": "system", "content": content}


def banner(url, model, think, tty, tools_on, grants):
    print(f"{CYAN}Chat with the local model{RESET} {DIM}({url}, model={model}){RESET}")
    if not tty:
        editing = "stdin is not a terminal, editing is up to the caller"
    elif readline is None:
        editing = "readline unavailable - editing is up to your terminal"
    else:
        editing = "Tab completes, Up/Down recalls, history persists"
    access = "tools: read-only, asks before each new folder" if tools_on else "tools: off"
    if tools_on and grants["roots"]:
        access += f" | read: {', '.join(short_path(root) for root in grants['roots'][:5])}"
        if len(grants["roots"]) > 5:
            access += f" +{len(grants['roots'])-5} more"
    if tools_on and grants["write_roots"]:
        access += " | write: "
        for i, root in enumerate(sorted(grants["write_roots"])):
            if i == 0:
                access += short_path(root)
            else:
                access += f", {short_path(root)}"
    print(f"{DIM}thinking={'on' if think else 'off'} | {access}{RESET}")
    print(f"{DIM}{editing} | /help for commands{RESET}\n")


def save_conversation(messages, path):
    path = os.path.expanduser(path)
    if os.path.exists(path):
        print(f"{YELLOW}{path} already exists, choose another name.{RESET}\n")
        return
    labels = {"system": "system", "user": "you", "assistant": "assistant", "tool": "tool"}
    lines = ["# chat-gemma transcript", "", f"_{time.strftime('%Y-%m-%d %H:%M:%S')}_", ""]
    for message in messages:
        role = labels.get(message["role"], message["role"])
        content = message.get("content") or ""
        lines += [f"**{role}**", "", content, ""]
        for call in message.get("tool_calls") or []:
            lines += [f"_tool call: {call['function']['name']}{call['function']['arguments']}_", ""]
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
    except OSError as exc:
        print(f"{YELLOW}Cannot write {path}: {exc}{RESET}\n")
        return
    print(f"{DIM}(conversation saved to {path}){RESET}\n")


def show_grants(grants):
    if not grants["roots"] and not grants["write_roots"]:
        print(f"{DIM}(nothing granted yet - ask the model for a file, or use /allow PATH){RESET}\n")
        return
    print(f"{DIM}grants:{RESET}")
    if grants["roots"]:
        print(f"{DIM}  read: {', '.join(short_path(r) for r in sorted(grants['roots']))}{RESET}")
    if grants["write_roots"]:
        print(f"{DIM}  write: {', '.join(short_path(r) for r in sorted(grants['write_roots']))}{RESET}")
    print()


# --------------------------------------------------------------------------- #
# Interactive input: readline (editing, history, completion)
# --------------------------------------------------------------------------- #

_PROMPT_TEXT = "you> "
_PROMPT_CONT_TEXT = "...> "


def _prompt(text, color):
    """Prompt with readline's zero-width markers around the colour codes."""
    if not COLOR:
        return text
    return f"\001{color}\002{text}\001{RESET}\002"


PROMPT = _prompt(_PROMPT_TEXT, CYAN)
PROMPT_CONT = _prompt(_PROMPT_CONT_TEXT, DIM)


def history_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "chat-gemma", "history")


def _complete_paths(text):
    home = os.path.expanduser("~")
    paths = []
    for match in sorted(glob.glob(os.path.expanduser(text) + "*")):
        if text.startswith("~") and match.startswith(home):
            match = "~" + match[len(home):]
        if os.path.isdir(os.path.expanduser(match)):
            match += "/"
        paths.append(match)
    return paths


def _completer(text, state):
    buffer = readline.get_line_buffer()
    if buffer.startswith("/think "):
        options = [word for word in ("on", "off") if word.startswith(text)]
    elif buffer.startswith("/tools "):
        options = [word for word in ("on", "off") if word.startswith(text)]
    elif buffer.startswith(("/save ", "/allow ", "/revoke ")):
        options = [path for path in _complete_paths(text) if path.startswith(text)]
    elif " " not in buffer and buffer.startswith("/"):
        options = [cmd for cmd in COMMANDS if cmd.startswith(buffer)]
    else:
        options = []
    return options[state] if state < len(options) else None


def setup_readline():
    """Turn on editing, persistent history and completion. Returns the history file."""
    if readline is None:
        return None
    path = history_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        readline.read_history_file(path)
    except OSError:
        pass
    readline.set_history_length(1000)
    readline.set_auto_history(False)  # the loop adds the finished message itself
    readline.set_completer_delims(" \t\n")
    for binding in ("tab: complete", "set enable-bracketed-paste on"):
        try:
            readline.parse_and_bind(binding)
        except Exception:  # not every readline understands every binding
            pass
    readline.set_completer(_completer)
    return path


def save_history(history):
    if readline is None or history is None:
        return
    try:
        readline.write_history_file(history)
    except OSError:
        pass


def _last_history_item():
    if readline is None:
        return None
    count = readline.get_current_history_length()
    return readline.get_history_item(count) if count else None


def read_message():
    """Read one message.

    Returns the text, '' for a cleared/empty line, None on EOF (Ctrl+D).
    A trailing backslash continues on the next line; a double backslash ends a
    line with a literal one.
    """
    parts = []
    prompt = PROMPT
    while True:
        try:
            line = input(prompt)
        except EOFError:
            return None
        except KeyboardInterrupt:
            print()
            return ""
        if line.endswith("\\\\"):
            line = line[:-1]
            break
        if line.endswith("\\"):
            parts.append(line[:-1])
            prompt = PROMPT_CONT
            continue
        break
    parts.append(line)
    text = "\n".join(parts).strip()
    if readline and text and text != _last_history_item():
        readline.add_history(text)
    return text


# --------------------------------------------------------------------------- #
# Session plumbing with persisted grants
# --------------------------------------------------------------------------- #

def system_message(system_text, tools_on):
    content = system_text
    if tools_on:
        content = f"{system_text}\n\n{TOOLS_NOTE}"
    return {"role": "system", "content": content}


def banner(url, model, think, tty, tools_on, grants):
    print(f"{CYAN}Chat with the local model{RESET} {DIM}({url}, model={model}){RESET}")
    if not tty:
        editing = "stdin is not a terminal, editing is up to the caller"
    elif readline is None:
        editing = "readline unavailable - editing is up to your terminal"
    else:
        editing = "Tab completes, Up/Down recalls, history persists"
    access = "tools: read-only, asks before each new folder" if tools_on else "tools: off"
    if tools_on and grants["roots"]:
        access += f" | read: {', '.join(short_path(root) for root in grants['roots'][:5])}"
        if len(grants["roots"]) > 5:
            access += f" +{len(grants['roots'])-5} more"
    if tools_on and grants["write_roots"]:
        access += " | write: "
        for i, root in enumerate(sorted(grants["write_roots"])):
            if i == 0:
                access += short_path(root)
            else:
                access += f", {short_path(root)}"
    print(f"{DIM}thinking={'on' if think else 'off'} | {access}{RESET}")
    print(f"{DIM}{editing} | /help for commands{RESET}\n")


def save_conversation(messages, path):
    path = os.path.expanduser(path)
    if os.path.exists(path):
        print(f"{YELLOW}{path} already exists, choose another name.{RESET}\n")
        return
    labels = {"system": "system", "user": "you", "assistant": "assistant", "tool": "tool"}
    lines = ["# chat-gemma transcript", "", f"_{time.strftime('%Y-%m-%d %H:%M:%S')}_", ""]
    for message in messages:
        role = labels.get(message["role"], message["role"])
        content = message.get("content") or ""
        lines += [f"**{role}**", "", content, ""]
        for call in message.get("tool_calls") or []:
            lines += [f"_tool call: {call['function']['name']}{call['function']['arguments']}_", ""]
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
    except OSError as exc:
        print(f"{YELLOW}Cannot write {path}: {exc}{RESET}\n")
        return
    print(f"{DIM}(conversation saved to {path}){RESET}\n")


def show_grants(grants):
    if not grants["roots"] and not grants["write_roots"]:
        print(f"{DIM}(nothing granted yet - ask the model for a file, or use /allow PATH){RESET}\n")
        return
    print(f"{DIM}grants:{RESET}")
    if grants["roots"]:
        print(f"{DIM}  read: {', '.join(short_path(r) for r in sorted(grants['roots']))}{RESET}")
    if grants["write_roots"]:
        print(f"{DIM}  write: {', '.join(short_path(r) for r in sorted(grants['write_roots']))}{RESET}")
    print()


def main():
    ap = argparse.ArgumentParser(add_help=True, description="Streaming chat for start-gemma-26b.sh")
    ap.add_argument("--url", default="http://127.0.0.1:8081", help="base URL of the server")
    ap.add_argument("--model", default="local",
                    help="model name sent to the server; llama-server ignores it, "
                         "LM Studio wants its key, e.g. gemma-4-26b-a4b-it-qat")
    ap.add_argument("--think", choices=["on", "off"], default="on", help="reasoning steps (default: on)")
    ap.add_argument("--system", default="You are a helpful assistant. Answer in the user's language.",
                    help="system prompt")
    ap.add_argument("--max-tokens", type=int, default=-1, help="reply limit, -1 = unlimited (default)")
    ap.add_argument("--allow", action="append", default=[], metavar="PATH",
                    help="grant read access to PATH for this session (repeatable)")
    ap.add_argument("--allow-write", action="append", default=[], metavar="PATH",
                    help="grant WRITE access to PATH for this session (repeatable, creates files there)")
    ap.add_argument("--no-tools", action="store_true", help="do not let the model touch files at all")
    ap.add_argument("-p", "--prompt", help="ask one question, print the answer and exit")
    args = ap.parse_args()

    think = args.think == "on"
    tools_on = not args.no_tools
    
    # Load persisted grants
    persisted = load_persisted_grants()
    
    # Command-line grants override persisted ones
    read_roots = [resolve(p) for p in args.allow] + persisted["roots"]
    write_roots = [resolve(p) for p in args.allow_write] + persisted["write_roots"]
    
    # Clear persisted grants if --no-tools
    if args.no_tools:
        clear_persisted_grants()

    grants = {"roots": read_roots, "write_roots": write_roots}
    messages = [system_message(args.system, tools_on)]

    if args.prompt:
        messages.append({"role": "user", "content": args.prompt})
        try:
            ask_model(args.url, args.model, messages, think, args.max_tokens,
                      TOOLS if tools_on else None, grants)
        except ChatError as exc:
            fail(str(exc))
        return

    tty = sys.stdin.isatty()
    history = setup_readline() if tty else None
    banner(args.url, args.model, think, tty, tools_on, grants)
    try:
        while True:
            text = read_message()
            if text is None:
                print(f"{DIM}bye{RESET}")
                break
            if not text:
                continue

            if text in ("/exit", "/quit"):
                break
            if text == "/help":
                print(HELP + "\n")
                continue
            if text == "/reset":
                messages = [system_message(args.system, tools_on)]
                print(f"{DIM}(conversation cleared){RESET}\n")
                continue
            if text.startswith("/think"):
                arg = arg_of(text)
                if arg in ("on", "off"):
                    think = arg == "on"
                    print(f"{DIM}(thinking {'on' if think else 'off'}){RESET}\n")
                else:
                    print(f"{YELLOW}Usage: /think on|off{RESET}\n")
                continue
            if text.startswith("/system"):
                arg = arg_of(text)
                if arg:
                    args.system = arg
                    messages[0] = system_message(args.system, tools_on)
                    print(f"{DIM}(system prompt updated){RESET}\n")
                else:
                    print(f"{YELLOW}Usage: /system TEXT{RESET}\n")
                continue
            if text.startswith("/model"):
                arg = arg_of(text)
                if arg:
                    args.model = arg
                print(f"{DIM}(model = {args.model}){RESET}\n")
                continue
            if text.startswith("/tools"):
                arg = arg_of(text)
                if arg in ("on", "off"):
                    tools_on = arg == "on"
                    messages[0] = system_message(args.system, tools_on)
                    note = "" if tools_on else f"{DIM}  (the model can no longer read files){RESET}"
                    print(f"{DIM}(tools {arg}){RESET} {note}\n")
                else:
                    print(f"{YELLOW}Usage: /tools on|off{RESET}\n")
                continue
            if text.startswith("/allow"):
                arg = arg_of(text)
                if not arg:
                    print(f"{YELLOW}Usage: /allow PATH{RESET}\n")
                    continue
                path = resolve(arg)
                if covered_read(path, grants["roots"]):
                    print(f"{DIM}({short_path(path)} is already granted for read){RESET}\n")
                else:
                    grants["roots"].append(path)
                    exists = "" if os.path.exists(path) else f" {YELLOW}(does not exist yet){RESET}"
                    print(f"{DIM}(granted read access to {short_path(path)} for this session){RESET}{exists}\n")
                # Save persisted grants
                save_persisted_grants(grants)
                continue
            if text.startswith("/allow-write"):
                arg = arg_of(text)
                if not arg:
                    print(f"{YELLOW}Usage: /allow-write PATH{RESET}\n")
                    continue
                path = resolve(arg)
                if covered_write(path, grants["write_roots"]):
                    print(f"{DIM}({short_path(path)} is already granted for write){RESET}\n")
                else:
                    grants["write_roots"].append(path)
                    exists = "" if os.path.isdir(path) else f" {YELLOW}(does not exist yet){RESET}"
                    print(f"{DIM}(granted WRITE access to {short_path(path)} for this session){RESET}{exists}\n")
                # Save persisted grants
                save_persisted_grants(grants)
                continue
            if text in ("/grants", "/granted"):
                show_grants(grants)
                continue
            if text.startswith("/revoke"):
                arg = arg_of(text)
                path = resolve(arg)
                # Try to revoke from both read and write
                revoked = False
                for root in list(grants["roots"]):
                    if path == root or covered_read(path, [root]):
                        grants["roots"].remove(root)
                        print(f"{DIM}(read access to {short_path(root)} revoked){RESET}\n")
                        revoked = True
                        break
                if not revoked:
                    for root in list(grants["write_roots"]):
                        if path == root or covered_write(path, [root]):
                            grants["write_roots"].remove(root)
                            print(f"{DIM}(write access to {short_path(root)} revoked){RESET}\n")
                            revoked = True
                            break
                if not revoked:
                    print(f"{YELLOW}{short_path(path)} is not granted{RESET}\n")
                save_persisted_grants(grants)
                continue
            if text.startswith("/save"):
                arg = arg_of(text)
                save_conversation(messages, arg or time.strftime("chat-%Y%m%d-%H%M%S.md"))
                continue
            if text.startswith("/"):
                print(f"{YELLOW}Unknown command - try /help{RESET}\n")
                continue

            messages.append({"role": "user", "content": text})
            try:
                ask_model(args.url, args.model, messages, think, args.max_tokens,
                          TOOLS if tools_on else None, grants)
            except ChatError as exc:
                # keep the session alive; the next reply answers the open message
                print(f"{YELLOW}{exc}{RESET}\n")
                continue
            print()
    except KeyboardInterrupt:
        print()
    finally:
        save_history(history)


if __name__ == "__main__":
    main()
