#!/usr/bin/env python3
"""
slack_agents.py - Connect your command-line AI agents to Slack.

Every agent becomes a Slack bot with its own name and avatar, backed by a
command-line agent (Claude Code, Codex, Gemini CLI, Cursor CLI or any other CLI
described in the config) that runs in the agent's own folder on this computer.
When someone talks to the bot, the bridge runs the CLI in that folder (so it
uses that folder's instruction files, skills and settings) and posts the reply.

Agents can mention each other (@Name) and talk in the same thread. Agent-to-agent
exchanges are capped to avoid endless loops, and writing "stop" in a thread
halts them right away.

Usage:
    python slack_agents.py                 start (reads config.toml next to the script)
    python slack_agents.py --check         check configuration, tokens and CLIs, then exit
    python slack_agents.py other.toml      use another configuration file

Requirements: Python 3.11+, pip install slack-bolt aiohttp
Optional, to transcribe voice messages locally: pip install faster-whisper
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 or newer is required (check with: python --version).")

import argparse
import asyncio
import base64
import collections
import copy
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_sdk.web.async_client import AsyncWebClient
    from slack_sdk.errors import SlackApiError
    import aiohttp
except ImportError:  # pragma: no cover
    sys.exit("The Slack libraries are missing. Install them with:  python -m pip install slack-bolt aiohttp")

log = logging.getLogger("slack_agents")

# Longest single Slack message before the reply is split.
MESSAGE_LIMIT = 3800
# Slack message subtypes that carry text written by someone.
VALID_SUBTYPES = {None, "bot_message", "thread_broadcast", "file_share"}
# Words that may precede an agent's name when calling it without @ ("Hey Researcher, ...").
CALL_FILLERS = {"hi", "hey", "hello", "ok", "okay", "so", "and", "ciao", "ehi", "e", "ed", "allora"}


# --------------------------------------------------------------------------- #
# CLI backends
# --------------------------------------------------------------------------- #
#
# A backend describes how to run one command-line agent non-interactively:
#   command          argument list for a new conversation
#   resume_command   argument list to continue a conversation ({session_id}); optional
#   prompt           "stdin" (message on standard input) or "arg" (message in {prompt})
#   stdin_hint       with prompt = "stdin", the text put in a {prompt} placeholder, if any
#   output           "text", "json" (one JSON object) or "jsonl" (one JSON event per line)
#   default_args     arguments used for {args} when the agent sets none (permissions, model...)
#   instructions     "args" (passed with instructions_args) or "prepend" (put before the message)
#   json fields      text_field, session_field, error_field, error_flag, cost_field (dotted paths)
#   events           rules to read jsonl events (see config.example.toml)
#
# Placeholders: {args} {instructions_args} {session_id} {prompt} {instructions_file} {workdir}

BUILTIN_BACKENDS: dict[str, dict[str, Any]] = {
    "claude": {
        "executables": ["claude"],
        "command": ["claude", "-p", "--output-format", "json", "{instructions_args}", "{args}"],
        "resume_command": ["claude", "-p", "--output-format", "json", "--resume", "{session_id}",
                           "{instructions_args}", "{args}"],
        "prompt": "stdin",
        "output": "json",
        "text_field": "result",
        "session_field": "session_id",
        "error_flag": "is_error",
        "cost_field": "total_cost_usd",
        "default_args": ["--permission-mode", "acceptEdits"],
        "instructions": "args",
        "instructions_args": ["--append-system-prompt-file", "{instructions_file}"],
        "session_errors": [r"no conversation found"],
    },
    "codex": {
        "executables": ["codex"],
        "command": ["codex", "exec", "--json", "--skip-git-repo-check", "{args}", "-"],
        # Codex rejects -s/--sandbox after "resume": use -c sandbox_mode=... in args instead.
        "resume_command": ["codex", "exec", "resume", "--json", "--skip-git-repo-check", "{args}",
                           "{session_id}", "-"],
        "prompt": "stdin",
        "output": "jsonl",
        "events": {
            "session": [{"match": {"type": "thread.started"}, "field": "thread_id"}],
            "text": {"match": {"type": "item.completed",
                               "item.type": ["agent_message", "assistant_message"]},
                     "field": "item.text", "join": "last"},
            "error": [{"match": {"type": "turn.failed"}, "field": "error.message"},
                      {"match": {"type": "error"}, "field": "message"}],
        },
        "default_args": ["-c", "sandbox_mode=workspace-write"],
        "instructions": "prepend",
        # e.g. "thread/resume failed: no rollout found for thread id ..."
        "session_errors": [r"(session|thread|conversation|rollout).{0,40}(not found|no such|does not exist)",
                           r"no (rollout|session|thread|conversation) found", r"thread/resume failed"],
    },
    "gemini": {
        "executables": ["gemini"],
        # --skip-trust: headless Gemini CLI refuses to run in a folder not marked as trusted.
        "command": ["gemini", "--output-format", "stream-json", "--skip-trust", "{args}"],
        "resume_command": ["gemini", "--output-format", "stream-json", "--skip-trust", "--resume", "{session_id}",
                           "{args}"],
        "prompt": "stdin",
        "output": "jsonl",
        "events": {
            "session": [{"match": {"type": "init"}, "field": "session_id"}],
            "text": {"match": {"type": "message", "role": ["assistant", "model"]},
                     "field": "content", "join": "last_block"},
            "reset": [{"type": "tool_use"}, {"type": "tool_result"}],
            "error": [{"match": {"type": "result", "status": "error"}, "field": "error.message"},
                      {"match": {"type": "error"}, "field": "message"}],
        },
        "default_args": ["--approval-mode", "auto_edit"],
        "instructions": "prepend",
        "session_errors": [r"(session).{0,40}(not found|invalid|no such|does not exist)"],
    },
    "cursor": {
        "executables": ["agent", "cursor-agent"],
        "command": ["agent", "-p", "--output-format", "json", "--trust", "{args}", "{prompt}"],
        "resume_command": ["agent", "-p", "--output-format", "json", "--trust", "--resume", "{session_id}",
                           "{args}", "{prompt}"],
        "prompt": "stdin",
        "stdin_hint": "Reply to the Slack message provided on standard input.",
        "output": "json",
        "text_field": "result",
        "session_field": "session_id",
        "error_flag": "is_error",
        "default_args": [],
        "instructions": "prepend",
        "session_errors": [r"(chat|session).{0,40}(not found|no such|does not exist)"],
    },
}

BACKEND_KEYS = {"executable", "executables", "command", "resume_command", "prompt", "stdin_hint", "output",
                "text_field", "session_field", "error_field", "error_flag", "cost_field", "events",
                "default_args", "instructions", "instructions_args", "session_errors", "version_args"}


@dataclass
class Backend:
    name: str
    command: list[str]
    resume_command: list[str] | None = None
    executables: list[str] = field(default_factory=list)
    prompt: str = "stdin"
    stdin_hint: str = ""
    output: str = "text"
    text_field: str | None = None
    session_field: str | None = None
    error_field: str | None = None
    error_flag: str | None = None
    cost_field: str | None = None
    events: dict[str, Any] = field(default_factory=dict)
    default_args: list[str] = field(default_factory=list)
    instructions: str = "prepend"
    instructions_args: list[str] = field(default_factory=list)
    session_errors: list[str] = field(default_factory=list)
    version_args: list[str] = field(default_factory=lambda: ["--version"])
    # Filled in at startup
    path: str = ""
    instructions_flag_ok: bool = True

    @property
    def keeps_sessions(self) -> bool:
        """True if the CLI can continue a conversation, so the bridge resumes it per thread."""
        if not self.resume_command:
            return False
        if self.output == "json":
            return bool(self.session_field)
        if self.output == "jsonl":
            return bool(self.events.get("session"))
        return False


def build_backend(name: str, spec: dict[str, Any], errors: list[str]) -> Backend:
    spec = dict(spec)
    executable = spec.pop("executable", None)
    command = list(spec.get("command") or [])
    resume = list(spec["resume_command"]) if spec.get("resume_command") else None
    executables = list(spec.get("executables") or ([command[0]] if command else []))
    if executable:  # a name or full path chosen by the user replaces the default executable
        executables = [str(executable)]
    if not command:
        errors.append(f"CLI '{name}': 'command' is missing.")
        command = ["?"]
    b = Backend(
        name=name,
        command=command,
        resume_command=resume,
        executables=executables,
        prompt=str(spec.get("prompt", "stdin")),
        stdin_hint=str(spec.get("stdin_hint", "")),
        output=str(spec.get("output", "text")),
        text_field=spec.get("text_field"),
        session_field=spec.get("session_field"),
        error_field=spec.get("error_field"),
        error_flag=spec.get("error_flag"),
        cost_field=spec.get("cost_field"),
        events=copy.deepcopy(spec.get("events") or {}),
        default_args=[str(a) for a in spec.get("default_args", [])],
        instructions=str(spec.get("instructions", "prepend")),
        instructions_args=[str(a) for a in spec.get("instructions_args", [])],
        session_errors=[str(s) for s in spec.get("session_errors", [])],
        version_args=[str(a) for a in spec.get("version_args", ["--version"])],
    )
    if b.prompt not in ("stdin", "arg"):
        errors.append(f"CLI '{name}': prompt must be \"stdin\" or \"arg\".")
    if b.output not in ("text", "json", "jsonl"):
        errors.append(f"CLI '{name}': output must be \"text\", \"json\" or \"jsonl\".")
    if b.instructions not in ("args", "prepend"):
        errors.append(f"CLI '{name}': instructions must be \"args\" or \"prepend\".")
    if b.prompt == "arg" and not any("{prompt}" in t for t in b.command):
        errors.append(f"CLI '{name}': with prompt = \"arg\" the command needs a {{prompt}} placeholder.")
    if b.output == "json" and not b.text_field:
        errors.append(f"CLI '{name}': with output = \"json\" set text_field (where the reply text is).")
    if b.output == "jsonl" and not (b.events.get("text") or {}).get("field"):
        errors.append(f"CLI '{name}': with output = \"jsonl\" set events.text (which events hold the reply).")
    return b


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass
class Agent:
    name: str
    cli: str
    folder: Path
    description: str
    bot_token: str
    app_token: str
    args: list[str] | None = None
    dedicated_channels: list[str] = field(default_factory=list)
    # Filled in at startup
    backend: Backend | None = None
    user_id: str = ""
    bot_id: str = ""
    instructions_file: Path | None = None
    client: Any = None

    @property
    def cli_args(self) -> list[str]:
        return list(self.args) if self.args is not None else list(self.backend.default_args)


@dataclass
class Settings:
    authorized_users: list[str]
    state_folder: Path
    max_agent_exchanges: int = 6
    timeout_minutes: float = 20
    max_context_messages: int = 40
    max_parallel_agents: int = 3
    stop_words: list[str] = field(default_factory=lambda: ["stop"])
    reset_words: list[str] = field(default_factory=lambda: ["reset", "new conversation"])
    follow_threads: bool = True
    call_by_name: bool = True
    ignore_prefix: str = "//"
    transcribe_voice: bool = True
    whisper_model: str = "small"
    voice_language: str = ""


def load_config(path: Path) -> tuple[Settings, list[Agent], dict[str, Backend]]:
    if not path.exists():
        sys.exit(f"Configuration file not found: {path}\n"
                 "Copy config.example.toml to config.toml and fill it in.")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"Error in {path.name}: {e}")

    errors: list[str] = []
    base = path.resolve().parent

    settings = Settings(
        authorized_users=[str(u).strip() for u in data.get("authorized_users", []) if str(u).strip()],
        state_folder=(base / data.get("state_folder", "state")).resolve(),
    )
    for key in ("max_agent_exchanges", "timeout_minutes", "max_context_messages", "max_parallel_agents",
                "stop_words", "reset_words", "follow_threads", "call_by_name", "ignore_prefix"):
        if key in data:
            setattr(settings, key, data[key])
    voice = data.get("voice", {})
    settings.transcribe_voice = bool(voice.get("transcribe", settings.transcribe_voice))
    settings.whisper_model = str(voice.get("whisper_model", settings.whisper_model))
    settings.voice_language = str(voice.get("language", settings.voice_language))
    settings.stop_words = [w.lower().strip() for w in settings.stop_words]
    settings.reset_words = [w.lower().strip() for w in settings.reset_words]

    if "U0123456789" in settings.authorized_users:
        errors.append("authorized_users still contains the example ID U0123456789: put your Slack member ID "
                      "there (profile picture > Profile > ⋮ > Copy member ID). If you can't find it, set "
                      "authorized_users = [] and start: when you write to an agent, the log shows your ID.")
    for u in settings.authorized_users:
        if not re.fullmatch(r"[UW][A-Z0-9]{6,}", u):
            errors.append(f"authorized_users: '{u}' doesn't look like a Slack member ID (e.g. U0123ABCD).")

    # CLI backends: built-in presets, which [cli.NAME] sections can tweak or extend.
    specs = copy.deepcopy(BUILTIN_BACKENDS)
    for name, spec in (data.get("cli") or {}).items():
        if not isinstance(spec, dict):
            errors.append(f"[cli.{name}] must be a table.")
            continue
        unknown = set(spec) - BACKEND_KEYS
        if unknown:
            errors.append(f"[cli.{name}]: unknown keys {', '.join(sorted(unknown))}.")
        merged = dict(specs.get(name, {}))
        if "command" in spec and name in BUILTIN_BACKENDS and "executable" not in spec:
            merged.pop("executables", None)
        merged.update(spec)
        specs[name] = merged

    agents: list[Agent] = []
    seen: set[str] = set()
    for i, a in enumerate(data.get("agents", []), start=1):
        name = str(a.get("name", "")).strip()
        label = name or f"agent #{i}"
        if not re.fullmatch(r"[^\W\d_][\w-]*", name):
            errors.append(f"{label}: the name must be a single word (e.g. Researcher).")
        if name.lower() in seen:
            errors.append(f"{label}: duplicate name.")
        seen.add(name.lower())
        cli = str(a.get("cli", "claude")).strip()
        if cli not in specs:
            errors.append(f"{label}: unknown cli '{cli}'. Use claude, codex, gemini, cursor or a [cli.{cli}] "
                          "section in the config.")
        folder = Path(os.path.expandvars(os.path.expanduser(str(a.get("folder", ""))))).resolve()
        if not a.get("folder") or not folder.is_dir():
            errors.append(f"{label}: folder '{a.get('folder', '')}' does not exist.")
        bot_token = str(a.get("bot_token", "")).strip()
        app_token = str(a.get("app_token", "")).strip()
        if not bot_token.startswith("xoxb-") or len(bot_token) < 20:
            errors.append(f"{label}: bot_token (xoxb-...) is missing or invalid.")
        if not app_token.startswith("xapp-") or len(app_token) < 20:
            errors.append(f"{label}: app_token (xapp-...) is missing or invalid.")
        args = a.get("args")
        agent = Agent(
            name=name, cli=cli, folder=folder,
            description=str(a.get("description", "")).strip(),
            bot_token=bot_token, app_token=app_token,
            args=[str(x) for x in args] if args is not None else None,
            dedicated_channels=[str(c).strip() for c in a.get("dedicated_channels", []) if str(c).strip()],
        )
        for c in agent.dedicated_channels:
            if not re.fullmatch(r"[CG][A-Z0-9]{6,}", c):
                errors.append(f"{label}: dedicated_channels: '{c}' doesn't look like a channel ID (e.g. C0123ABCD).")
        agents.append(agent)

    if not agents:
        errors.append("No agents configured: add at least one [[agents]] section.")

    backends: dict[str, Backend] = {}
    for name in sorted({a.cli for a in agents if a.cli in specs}):
        backends[name] = build_backend(name, specs[name], errors)
    for a in agents:
        a.backend = backends.get(a.cli)

    if errors:
        sys.exit("Configuration problems:\n  - " + "\n  - ".join(errors))
    return settings, agents, backends


def find_executable(backend: Backend) -> str:
    for candidate in backend.executables:
        found = shutil.which(candidate)
        if found:
            return found
    names = " or ".join(f"'{c}'" for c in backend.executables)
    sys.exit(f"Can't find the {backend.name} CLI ({names}). Check that it works in a terminal, or set its "
             f"full path with  [cli.{backend.name}]  executable = \"...\"  in the config.")


# --------------------------------------------------------------------------- #
# Conversation memory (one CLI session per thread)
# --------------------------------------------------------------------------- #

class SessionStore:
    """Maps each (agent, channel, thread) to the CLI session to resume and what it has seen."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict[str, dict] = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def get(self, key: str) -> dict | None:
        return self.data.get(key)

    def set(self, key: str, value: dict) -> None:
        self.data[key] = value
        self._save()

    def delete(self, key: str) -> bool:
        if self.data.pop(key, None) is not None:
            self._save()
            return True
        return False

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# Slack text <-> Markdown
# --------------------------------------------------------------------------- #

def slack_escape(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_unescape(t: str) -> str:
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _convert_inline(t: str) -> str:
    """Converts the Markdown of one line (outside code) into Slack mrkdwn."""
    parts = re.split(r"(`[^`]*`)", t)
    for i in range(0, len(parts), 2):  # only the parts outside inline code
        s = parts[i]
        s = re.sub(r"!?\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", s)
        s = re.sub(r"\*\*(.+?)\*\*", "\x01\\1\x01", s)
        s = re.sub(r"(?<!\w)__(.+?)__(?!\w)", "\x01\\1\x01", s)
        s = re.sub(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<!\s)\*(?![\w*])", r"_\1_", s)
        s = re.sub(r"~~(.+?)~~", r"~\1~", s)
        parts[i] = s.replace("\x01", "*")
    return "".join(parts)


def markdown_to_slack(text: str) -> str:
    """Converts the Markdown written by the agents into Slack mrkdwn."""
    out: list[str] = []
    table: list[str] = []
    in_code = False

    def flush_table() -> None:
        if table:
            out.append("```")
            out.extend(table)
            out.append("```")
            table.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_table()
            in_code = not in_code
            out.append("```")  # Slack ignores the language tag
            continue
        if in_code:
            out.append(slack_escape(line))
            continue
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1:
            table.append(slack_escape(line))  # tables become monospace blocks
            continue
        flush_table()
        line = slack_escape(line)
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if heading:
            out.append("*" + _convert_inline(heading.group(1)).replace("*", "") + "*")
            continue
        if re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", line):
            out.append("──────────")
            continue
        line = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line)
        out.append(_convert_inline(line))
    flush_table()
    if in_code:
        out.append("```")
    return "\n".join(out)


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Splits a long text into several messages without breaking code blocks."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    in_code = False

    def close() -> None:
        nonlocal current, length
        if in_code:
            current.append("```")
        chunks.append("\n".join(current).strip("\n"))
        current = ["```"] if in_code else []
        length = sum(len(r) + 1 for r in current)

    for line in text.split("\n"):
        while len(line) > limit - 10:
            if current:
                close()
            current.append(line[: limit - 10])
            length += limit - 9
            line = line[limit - 10:]
        if length + len(line) + 1 > limit - 4 and current:
            close()
        current.append(line)
        length += len(line) + 1
        if line.strip() == "```":
            in_code = not in_code
    if any(r.strip() for r in current):
        chunks.append("\n".join(current).strip("\n"))
    return [c for c in chunks if c.strip()]


# --------------------------------------------------------------------------- #
# Voice messages
# --------------------------------------------------------------------------- #

def is_audio(f: dict) -> bool:
    """True if the attached file is audio or video (Slack voice messages are audio clips)."""
    return ((f.get("mimetype") or "").startswith(("audio/", "video/"))
            or f.get("subtype") in ("slack_audio", "slack_video")
            or (f.get("filetype") or "").lower() in ("webm", "m4a", "mp3", "wav", "ogg", "opus", "aac", "mp4"))


def vtt_to_text(vtt: str) -> str:
    """Extracts the text from WebVTT captions (also when base64-encoded)."""
    if not vtt:
        return ""
    t = vtt
    if not t.lstrip().startswith("WEBVTT"):
        try:
            t = base64.b64decode(t, validate=True).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
        if not t.lstrip().startswith("WEBVTT"):
            return ""
    lines = []
    for r in t.splitlines():
        r = r.strip()
        if not r or r.startswith(("WEBVTT", "NOTE", "STYLE")) or "-->" in r or r.isdigit():
            continue
        lines.append(r)
    return " ".join(lines).strip()


def slack_transcript(f: dict) -> str | None:
    """The transcript Slack made of a clip, if it is ready."""
    tr = f.get("transcription") or {}
    if tr.get("status") != "complete":
        return None
    text = vtt_to_text(f.get("vtt") or "")
    if text:
        return text
    preview = tr.get("preview") or {}
    text = (preview.get("content") or "").strip()
    if text and preview.get("has_more"):
        text += " […]"
    return text or None


def is_slack_host(url: str) -> bool:
    """The bot token is only ever sent to Slack addresses, never to external sites."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host == "slack.com" or host.endswith(".slack.com")


def token_scopes(response: Any) -> set[str] | None:
    """The token's scopes, read from the x-oauth-scopes header of a Slack response."""
    headers = getattr(response, "headers", None) or {}
    for key, value in dict(headers).items():
        if str(key).lower() == "x-oauth-scopes":
            value = value[0] if isinstance(value, (list, tuple)) else value
            return {x.strip() for x in str(value).split(",") if x.strip()}
    return None


REQUIRED_SCOPES = {
    "chat:write": "replying",
    "channels:history": "reading messages in public channels",
    "groups:history": "reading messages in private channels",
    "im:history": "reading direct messages",
    "users:read": "showing people's names",
    "reactions:write": "adding 👀 while working",
    "files:read": "downloading voice messages",
}


def check_scopes(name: str, scopes: set[str] | None, voice: bool, token: str) -> str | None:
    """Explains which permissions the bot token lacks, what won't work and what it actually has."""
    if scopes is None:
        return None
    needed = [s for s in REQUIRED_SCOPES if s != "files:read" or voice]
    missing = [s for s in needed if s not in scopes]
    if not missing:
        return None
    listing = "; ".join(f"{s} (needed for {REQUIRED_SCOPES[s]})" for s in missing)
    return (f"{name}: the bot token in use ({token[:9]}…{token[-4:]}) is missing: {listing}. "
            f"The token only has: {', '.join(sorted(scopes)) or 'no scopes'}. "
            f"Check that the permissions are under Bot Token Scopes (not User Token Scopes) of {name}'s app, "
            "reinstall the app and copy into the config the Bot User OAuth Token shown after reinstalling.")


class DownloadError(Exception):
    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"HTTP {status} {detail}".strip())
        self.status = status


async def download_from_slack(url: str, token: str) -> bytes:
    """Downloads a Slack file following redirects by hand: left alone, the HTTP library would drop the
    token when files.slack.com redirects to another Slack address (-> 403)."""
    async with aiohttp.ClientSession(trust_env=True) as session:
        for _ in range(6):
            headers = {"Authorization": f"Bearer {token}"} if is_slack_host(url) else {}
            async with session.get(url, headers=headers, allow_redirects=False) as r:
                if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    url = urllib.parse.urljoin(url, r.headers["Location"])
                    continue
                kind = r.headers.get("Content-Type", "")
                if r.status == 200 and not kind.startswith("text/html"):
                    return await r.read()
                raise DownloadError(r.status, "(Slack sign-in page)" if kind.startswith("text/html") else "")
        raise DownloadError(0, "too many redirects")


class Transcriber:
    """Transcribes audio on this computer with faster-whisper, one file at a time."""

    def __init__(self, model: str, language: str | None):
        self.model_name = model
        self.language = language or None
        self._model = None
        self._lock = asyncio.Lock()
        # The silence filter needs onnxruntime, which isn't published for every Python version.
        self.silence_filter = importlib.util.find_spec("onnxruntime") is not None

    @staticmethod
    def installed() -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        return self._model

    async def prepare(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._load)

    async def transcribe(self, path: Path) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._transcribe, path)

    @staticmethod
    def decode(path: Path):
        """Reads audio as 16 kHz mono float32. Done here rather than by faster-whisper, which passes PyAV
        an option (metadata_errors) that recent PyAV versions reject."""
        import av
        import numpy as np
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        pieces = []
        with av.open(str(path), mode="r") as container:
            streams = [s for s in container.streams if s.type == "audio"]
            if not streams:
                raise RuntimeError("the file has no audio")
            for packet in container.demux(streams[0]):
                try:
                    frames = packet.decode()
                except av.error.InvalidDataError:
                    continue  # damaged packets are skipped, as faster-whisper does
                for frame in frames:
                    pieces += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
            pieces += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
        if not pieces:
            raise RuntimeError("empty audio")
        return np.concatenate(pieces).astype(np.float32) / 32768.0

    def _transcribe(self, path: Path) -> str:
        audio = self.decode(path)
        segments, _ = self._load().transcribe(audio, language=self.language,
                                              vad_filter=self.silence_filter, beam_size=5)
        return " ".join(s.text.strip() for s in segments).strip()


# --------------------------------------------------------------------------- #
# Running a CLI and reading its answer
# --------------------------------------------------------------------------- #

@dataclass
class Result:
    text: str
    session_id: str | None
    cost: float | None = None
    seconds: float = 0.0


class CliError(Exception):
    pass


def get_path(obj: Any, path: str | None) -> Any:
    """Reads a dotted path ("item.text") from nested dictionaries."""
    if not path:
        return None
    for part in path.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


def event_matches(event: Any, match: dict) -> bool:
    if not isinstance(event, dict):
        return False
    for key, wanted in (match or {}).items():
        actual = get_path(event, key)
        if isinstance(wanted, list):
            if actual not in wanted:
                return False
        elif actual != wanted:
            return False
    return True


@dataclass
class Parsed:
    text: str | None
    session_id: str | None
    error: str | None
    cost: float | None


def _json_objects(stdout: str) -> list[Any]:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                out.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                pass
    return out


def parse_output(backend: Backend, stdout: str) -> Parsed:
    """Reads the reply text, session id, error and cost from a CLI's output."""
    if backend.output == "text":
        return Parsed(stdout.strip() or None, None, None, None)

    if backend.output == "json":
        data = None
        whole = stdout.strip()
        try:
            data = json.loads(whole)
        except (json.JSONDecodeError, ValueError):
            objects = [o for o in _json_objects(stdout) if isinstance(o, (dict, list))]
            data = objects[-1] if objects else None
        if isinstance(data, list):  # some versions print the list of events
            dicts = [d for d in data if isinstance(d, dict)]
            data = next((d for d in reversed(dicts) if d.get("type") == "result"), dicts[-1] if dicts else None)
        if not isinstance(data, dict):
            return Parsed(None, None, None, None)
        text = get_path(data, backend.text_field)
        error = get_path(data, backend.error_field)
        if backend.error_flag and get_path(data, backend.error_flag):
            error = error or text or get_path(data, "subtype") or "the CLI reported an error"
            text = None
        cost = get_path(data, backend.cost_field)
        return Parsed(str(text).strip() if text else None,
                      str(get_path(data, backend.session_field) or "") or None,
                      str(error) if error else None,
                      cost if isinstance(cost, (int, float)) else None)

    # jsonl: one event per line
    rules = backend.events
    text_rule = rules.get("text") or {}
    join = text_rule.get("join", "last")
    block = 0
    texts: list[tuple[int, str]] = []
    session_id = None
    errors: list[str] = []
    cost = None
    for event in _json_objects(stdout):
        if not isinstance(event, dict):
            continue
        if any(event_matches(event, m) for m in rules.get("reset", [])):
            block += 1
        for rule in rules.get("session", []):
            if event_matches(event, rule.get("match", {})):
                value = get_path(event, rule.get("field"))
                if value:
                    session_id = str(value)
        if text_rule and event_matches(event, text_rule.get("match", {})):
            value = get_path(event, text_rule.get("field"))
            if isinstance(value, str) and value:
                texts.append((block, value))
        for rule in rules.get("error", []):
            if event_matches(event, rule.get("match", {})):
                value = get_path(event, rule.get("field"))
                if value:
                    errors.append(str(value))
        cost_rule = rules.get("cost")
        if cost_rule and event_matches(event, cost_rule.get("match", {})):
            value = get_path(event, cost_rule.get("field"))
            if isinstance(value, (int, float)):
                cost = value
    if join == "concat":
        text = "".join(t for _, t in texts)
    elif join == "last_block":
        last = texts[-1][0] if texts else 0
        text = "".join(t for b, t in texts if b == last)
    else:
        text = texts[-1][1] if texts else ""
    return Parsed(text.strip() or None, session_id, "; ".join(errors[-3:]) or None, cost)


def cmd_safe(text: str) -> str:
    """Makes text safe as an argument of a .cmd/.bat program on Windows, where cmd.exe would otherwise
    interpret quotes, % and newlines (and could even run commands). Lossy, used only in that case."""
    text = re.sub(r"[\r\n]+", " / ", text)
    return re.sub(r'["%^&|<>!]', "", text)


def build_command(backend: Backend, agent: Agent, session_id: str | None, prompt: str,
                  instructions_file: Path | None) -> list[str]:
    template = backend.resume_command if (session_id and backend.resume_command) else backend.command
    use_instruction_args = (backend.instructions == "args" and backend.instructions_flag_ok
                            and instructions_file is not None)
    windows_batch = os.name == "nt" and backend.path.lower().endswith((".cmd", ".bat"))
    prompt_value = prompt if backend.prompt == "arg" else backend.stdin_hint
    if backend.prompt == "arg" and windows_batch:
        prompt_value = cmd_safe(prompt_value)

    def fill(token: str) -> str:
        return (token.replace("{session_id}", session_id or "")
                     .replace("{instructions_file}", str(instructions_file or ""))
                     .replace("{workdir}", str(agent.folder))
                     .replace("{prompt}", prompt_value))

    out: list[str] = []
    for token in template:
        if token == "{args}":
            out += agent.cli_args
        elif token == "{instructions_args}":
            if use_instruction_args:
                out += [fill(t) for t in backend.instructions_args]
        elif token == "{prompt}" and not prompt_value:
            continue
        else:
            out.append(fill(token))
    out[0] = backend.path or out[0]
    return out


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Stops the CLI and every process it started."""
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            k = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            await k.wait()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)  # avoids Claude Code's "session inside a session" refusal
    return env


def child_flags() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}  # so the whole process group can be stopped


async def cli_version(backend: Backend) -> str:
    """Checks that the CLI starts and returns its version line."""
    try:
        p = await asyncio.create_subprocess_exec(
            backend.path, *backend.version_args, env=child_env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **child_flags())
        out, _ = await asyncio.wait_for(p.communicate(), timeout=60)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"The {backend.name} CLI doesn't start ({backend.path}): {e}")
    lines = out.decode("utf-8", errors="replace").strip().splitlines()
    return lines[0] if lines else "?"


# --------------------------------------------------------------------------- #
# The Slack <-> agents bridge
# --------------------------------------------------------------------------- #

class Bridge:
    def __init__(self, settings: Settings, agents: list[Agent]):
        self.settings = settings
        self.agents = agents
        self.by_id: dict[str, Agent] = {}
        self.sessions = SessionStore(settings.state_folder / "sessions.json")
        self.exchanges: dict[tuple[str, str], int] = {}
        self.warned: set[tuple[str, str]] = set()
        self.stopped: set[tuple[str, str]] = set()
        self.last_agent: dict[tuple[str, str], str] = {}
        self.running: dict[tuple[str, str, str], set[asyncio.Task]] = collections.defaultdict(set)
        self.locks: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self.semaphore = asyncio.Semaphore(max(1, int(settings.max_parallel_agents)))
        self.seen: collections.OrderedDict[tuple[str, str, str], None] = collections.OrderedDict()
        self.user_names: dict[str, str] = {}
        self._profiles: dict[str, dict] = {}
        self.human_aliases: dict[str, str] = {}
        self.reported_users: set[str] = set()
        self.whisper: Transcriber | None = None
        self.transcripts: dict[str, str | None] = {}
        self.file_locks: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)

    # ----- setup ----------------------------------------------------------- #

    def register(self, agent: Agent, user_id: str, bot_id: str | None) -> None:
        agent.user_id = user_id
        agent.bot_id = bot_id or ""
        self.by_id[user_id] = agent
        if bot_id:
            self.by_id[bot_id] = agent

    async def prepare(self) -> None:
        """Looks up the authorized people's names and writes each agent's Slack instructions."""
        for uid in self.settings.authorized_users:
            name = await self._user_name(uid)
            if name != uid:
                self.human_aliases[name.lower()] = uid
                profile = self._profiles.get(uid, {})
                for alias in (profile.get("display_name"), profile.get("real_name"),
                              (profile.get("real_name") or "").split(" ")[0]):
                    if alias and len(alias) >= 3:
                        self.human_aliases.setdefault(alias.lower(), uid)
        self.settings.state_folder.mkdir(parents=True, exist_ok=True)
        for agent in self.agents:
            agent.instructions_file = self.settings.state_folder / f"slack_instructions_{agent.name}.md"
            agent.instructions_file.write_text(self._instructions(agent), encoding="utf-8")

    def _instructions(self, agent: Agent) -> str:
        others = [a for a in self.agents if a is not agent]
        people = sorted({f"@{self.user_names.get(u, u)}" for u in self.settings.authorized_users})
        lines = [
            "# You are working in Slack",
            "",
            f'You are "{agent.name}", an AI agent connected to Slack. {agent.description}'.strip(),
            "You receive Slack messages, and the final text of your reply is posted as-is as a Slack "
            "message under your name.",
            "",
            "## Who is here",
            f"- People who give you instructions: {', '.join(people) if people else 'the workspace members'}.",
        ]
        if others:
            lines.append("- Other agents:")
            lines += [f"  - @{a.name}: {a.description or 'AI agent'}" for a in others]
        lines += [
            "",
            "## How it works",
            '- Messages reach you as "[Name] text". You only see messages addressed to you, plus the '
            "thread messages included with them.",
            '- Text after "[voice message]" is an automatic transcript of audio: it may contain '
            "recognition errors, go for the meaning.",
        ]
        if others:
            lines += [
                f"- To involve another agent, write @Name in your reply (e.g. @{others[0].name}): it will "
                "receive your message and answer in the same thread. Only do it when you really need its "
                "contribution.",
                "- Don't mention other agents to greet, thank or confirm: every mention sets them off. "
                "When the matter is settled, mention nobody.",
                "- When another agent asks you something, answer on the merits.",
                f"- Consecutive exchanges between agents are capped at {self.settings.max_agent_exchanges}; "
                "after that a person has to step in.",
            ]
        lines += [
            "- You can't ask for interactive confirmation. If you lack a permission to do something, say so "
            "in your reply and explain what is needed.",
            "",
            "## Style",
            "- Chat messages: short and direct, in the language of the person writing to you.",
            "- You can use bold, bullet lists and code blocks; avoid headings and long tables.",
            "- If you create or change files, give their paths relative to your working folder.",
            "",
        ]
        return "\n".join(lines)

    # ----- helpers --------------------------------------------------------- #

    def _author_agent(self, m: dict) -> Agent | None:
        return self.by_id.get(m.get("user") or "") or self.by_id.get(m.get("bot_id") or "")

    async def _user_name(self, uid: str) -> str:
        if uid in self.by_id:
            return self.by_id[uid].name
        if uid in self.user_names:
            return self.user_names[uid]
        name = uid
        try:
            r = await self.agents[0].client.users_info(user=uid)
            u = r["user"]
            p = u.get("profile", {})
            name = p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name") or uid
            self._profiles[uid] = {"display_name": p.get("display_name"),
                                   "real_name": p.get("real_name") or u.get("real_name")}
        except Exception as e:  # noqa: BLE001
            log.debug("Name of %s not available: %s", uid, e)
        self.user_names[uid] = name
        return name

    async def _author_name(self, m: dict) -> str:
        agent = self._author_agent(m)
        if agent:
            return agent.name
        if m.get("bot_id") and not m.get("user"):
            return (m.get("bot_profile") or {}).get("name") or m.get("username") or "bot"
        return await self._user_name(m.get("user") or "?")

    async def _readable_text(self, m: dict, agent: Agent | None = None) -> str:
        """A Slack message's text with readable mentions and links, and voice messages transcribed."""
        t = m.get("text") or ""
        for uid in set(re.findall(r"<@([UWB][A-Z0-9]+)(?:\|[^>]*)?>", t)):
            name = await self._user_name(uid)
            t = re.sub(rf"<@{uid}(?:\|[^>]*)?>", f"@{name}", t)
        t = re.sub(r"<#[A-Z0-9]+\|([^>]*)>", r"#\1", t)
        t = re.sub(r"<!(here|channel|everyone)(?:\|[^>]*)?>", r"@\1", t)
        t = re.sub(r"<((?:https?|mailto):[^|>]+)\|([^>]+)>", r"\2 (\1)", t)
        t = re.sub(r"<((?:https?|mailto):[^>]+)>", r"\1", t)
        t = slack_unescape(t)
        other_files = []
        for f in m.get("files", []) or []:
            if is_audio(f) and self.settings.transcribe_voice:
                transcript = await self._transcribe_file(agent or self.agents[0], f)
                t += ("\n[voice message] " + transcript) if transcript else "\n[voice message: transcription failed]"
            else:
                other_files.append(f.get("name") or f.get("title") or "file")
        if other_files:
            t += "\n(attachments not available to the agent: " + ", ".join(other_files) + ")"
        return t.strip()

    async def _transcribe_file(self, agent: Agent, f: dict) -> str | None:
        """Transcribes a voice message only once, even when several agents need it."""
        fid = f.get("id") or f.get("url_private") or str(id(f))
        if fid in self.transcripts:
            return self.transcripts[fid]
        async with self.file_locks[fid]:
            if fid in self.transcripts:
                return self.transcripts[fid]
            text: str | None = None
            if self.whisper:
                path = None
                try:
                    start = time.monotonic()
                    path = await self._download(agent, f)
                    text = await self.whisper.transcribe(path) or None
                    log.info("Voice message transcribed in %.0fs: %s", time.monotonic() - start, (text or "")[:80])
                except Exception as e:  # noqa: BLE001
                    log.warning("Local transcription failed: %s", e)
                finally:
                    if path:
                        path.unlink(missing_ok=True)
            if not text:
                text = await self._wait_for_slack_transcript(agent, f)
            if text:
                self.transcripts[fid] = text
            return text

    async def _download(self, agent: Agent, f: dict) -> Path:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            raise RuntimeError("the file has no download address")
        if (f.get("size") or 0) > 200 * 1024 * 1024:
            raise RuntimeError("file too large")
        folder = self.settings.state_folder / "voice"
        folder.mkdir(parents=True, exist_ok=True)
        extension = re.sub(r"\W", "", f.get("filetype") or "") or "audio"
        file_name = re.sub(r"\W", "_", f.get("id") or "voice")
        path = folder / f"{file_name}.{extension}"
        try:
            data = await download_from_slack(url, agent.bot_token)
        except DownloadError as first:
            data = None
            if f.get("id"):  # second attempt with a fresh address from Slack (files.info)
                try:
                    info = (await agent.client.files_info(file=f["id"])).get("file") or {}
                    fresh = info.get("url_private_download") or info.get("url_private")
                    if fresh:
                        data = await download_from_slack(fresh, agent.bot_token)
                except Exception as e:  # noqa: BLE001
                    log.debug("Second download attempt failed: %s", e)
            if data is None:
                raise RuntimeError(
                    f"Slack won't let me download the voice message ({first}). Check that {agent.name}'s app "
                    "has the files:read scope under Bot Token Scopes, that it was reinstalled and that the bot "
                    "is in the conversation of the voice message.") from None
        path.write_bytes(data)
        return path

    async def _wait_for_slack_transcript(self, agent: Agent, f: dict) -> str | None:
        """Fallback: Slack's own transcript (it can show up a few seconds later)."""
        text = slack_transcript(f)
        if text or not f.get("id"):
            return text
        for _ in range(6):
            try:
                r = await agent.client.files_info(file=f["id"])
            except Exception as e:  # noqa: BLE001
                log.debug("files.info not available: %s", e)
                return None
            info = r.get("file") or {}
            text = slack_transcript(info)
            status = (info.get("transcription") or {}).get("status")
            if text or status not in ("processing", "pending", "queued", None):
                return text
            await asyncio.sleep(5)
        return None

    @staticmethod
    def _command_word(text: str) -> str:
        t = re.sub(r"<@[^>]+>", " ", text or "")
        return re.sub(r"[\s!.?,;:]+", " ", t).strip().lower()

    async def _react(self, client, channel: str, ts: str, name: str, add: bool) -> None:
        try:
            if add:
                await client.reactions_add(channel=channel, timestamp=ts, name=name)
            else:
                await client.reactions_remove(channel=channel, timestamp=ts, name=name)
        except Exception as e:  # noqa: BLE001
            log.debug("Reaction %s failed: %s", name, e)

    def _already_seen(self, key: tuple[str, str, str]) -> bool:
        if key in self.seen:
            return True
        self.seen[key] = None
        while len(self.seen) > 5000:
            self.seen.popitem(last=False)
        return False

    # ----- events ----------------------------------------------------------- #

    async def handle_event(self, agent: Agent, event: dict) -> None:
        if event.get("subtype") not in VALID_SUBTYPES or event.get("hidden"):
            return
        channel, ts = event.get("channel"), event.get("ts")
        # The same message may arrive both as "message" and "app_mention": handled once.
        if not channel or not ts or self._already_seen((agent.name, channel, ts)):
            return

        author_agent = self._author_agent(event)
        if author_agent is agent:
            return
        from_bot = bool(event.get("bot_id")) or event.get("subtype") == "bot_message"
        if from_bot and author_agent is None:
            return  # other bots and integrations are ignored
        if author_agent is None and self.settings.authorized_users \
                and event.get("user") not in self.settings.authorized_users:
            uid = event.get("user") or "?"
            if uid not in self.reported_users:
                self.reported_users.add(uid)
                log.warning('Ignoring messages from %s (%s): not in authorized_users. If that is you, add '
                            '"%s" to authorized_users and restart.', await self._user_name(uid), uid, uid)
            return

        is_dm = event.get("channel_type") == "im"
        root = event.get("thread_ts") or ("dm" if is_dm else ts)
        thread = (channel, root)
        text = event.get("text") or ""
        mentioned = f"<@{agent.user_id}>" in text
        word = self._command_word(text)

        if author_agent is None:  # ---- a person's message
            if not self.settings.authorized_users and event.get("user") not in self.reported_users:
                self.reported_users.add(event.get("user"))
                log.info("Message from %s, member ID %s", await self._user_name(event.get("user") or "?"),
                         event.get("user"))
            if word in self.settings.stop_words:
                await self._stop(agent, event, channel, None if (root == ts and not is_dm) else root)
                return
            self.exchanges[thread] = 0
            self.warned.discard(thread)
            self.stopped.discard(thread)
            if is_dm and root == "dm" and word in self.settings.reset_words:
                self.sessions.set(f"{agent.name}|{channel}|dm", {"reset_ts": ts})
                await self._react(agent.client, channel, ts, "white_check_mark", True)
                return
            if self.settings.ignore_prefix and text.lstrip().startswith(self.settings.ignore_prefix):
                return  # a note for people, not for the agents
            if not await self._addressed_to(agent, event, text, channel, root, is_dm, mentioned):
                return
        else:  # ---- another agent's message
            if is_dm or not mentioned or thread in self.stopped:
                return
            n = self.exchanges.get(thread, 0)
            if n >= self.settings.max_agent_exchanges:
                if thread not in self.warned:
                    self.warned.add(thread)
                    await self._post(
                        agent, channel, root,
                        f"⏸️ The agents have exchanged {n} messages in a row in this thread, so I'm pausing "
                        "here. Write in the thread to get us going again.", convert=False)
                log.info("[%s] exchange limit reached in thread %s", agent.name, thread)
                return
            self.exchanges[thread] = n + 1

        self._start(agent, event, channel, root, is_dm)

    def _called_by_name(self, text: str) -> set[str]:
        """Agents called by name at the start of a message, without @: "Reviewer, have a look"."""
        if not self.settings.call_by_name:
            return set()

        def norm(x: str) -> str:
            return re.sub(r"[\W_]+", "", x.lower())

        names = {norm(a.name): a.name for a in self.agents}
        t = slack_unescape(re.sub(r"<@[^>]+>", " ", text or ""))
        words = [w for w in re.split(r"[\s,&]+", t.strip()) if norm(w)][:12]
        called: set[str] = set()
        i = 0
        while i < len(words):
            # a name may be written or spoken as several words: "Fitness coach" = FitnessCoach
            for n in (3, 2, 1):
                if i + n <= len(words) and norm("".join(words[i:i + n])) in names:
                    called.add(names[norm("".join(words[i:i + n]))])
                    i += n
                    break
            else:
                if norm(words[i]) not in CALL_FILLERS:
                    break
                i += 1
        return called

    def _last_agent(self, channel: str, root: str) -> str | None:
        """The agent that spoke last in a thread."""
        if (channel, root) in self.last_agent:
            return self.last_agent[(channel, root)]
        best, best_ts = None, -1.0
        for a in self.agents:  # after a restart, worked out from the saved sessions
            s = self.sessions.get(f"{a.name}|{channel}|{root}")
            if s and s.get("last_ts") and float(s["last_ts"]) > best_ts:
                best, best_ts = a.name, float(s["last_ts"])
        return best

    async def _addressed_to(self, agent: Agent, event: dict, text: str, channel: str, root: str,
                            is_dm: bool, mentioned: bool) -> bool:
        """Decides whether a person's message is meant for this agent."""
        if mentioned or is_dm:
            return True
        called = self._called_by_name(text)
        if not called and self.settings.call_by_name and self.settings.transcribe_voice:
            for f in event.get("files", []) or []:  # a name spoken at the start of a voice message
                if is_audio(f):
                    called |= self._called_by_name(await self._transcribe_file(agent, f) or "")
        if called:
            return agent.name in called
        if "<@" in text:
            return False  # meant for another agent or another person
        if self.settings.follow_threads and event.get("thread_ts"):
            last = self._last_agent(channel, root)
            if last:
                return last == agent.name  # in a thread, whoever spoke last carries on
        return channel in agent.dedicated_channels

    async def _stop(self, agent: Agent, event: dict, channel: str, root: str | None) -> None:
        """'stop': inside a thread stops that thread; outside a thread stops the whole channel."""
        if root is None:
            threads = {k for k in self.exchanges if k[0] == channel}
            threads |= {(c, r) for (_, c, r) in self.running if c == channel}
        else:
            threads = {(channel, root)}
        self.stopped |= threads
        cancelled = 0
        for (name, c, r), tasks in list(self.running.items()):
            if name == agent.name and (c, r) in threads:
                for t in list(tasks):
                    t.cancel()
                    cancelled += 1
        log.info("[%s] stop in %s: %d jobs cancelled", agent.name, channel, cancelled)
        await self._react(agent.client, channel, event["ts"], "octagonal_sign", True)

    def _start(self, agent: Agent, event: dict, channel: str, root: str, is_dm: bool) -> None:
        key = (agent.name, channel, root)
        task = asyncio.create_task(self._reply(agent, event, channel, root, is_dm))
        self.running[key].add(task)
        self.last_agent[(channel, root)] = agent.name

        def done(t: asyncio.Task) -> None:
            tasks = self.running.get(key)
            if tasks is not None:
                tasks.discard(t)
                if not tasks:
                    self.running.pop(key, None)
            if not t.cancelled() and t.exception():
                log.error("[%s] unexpected error: %r", agent.name, t.exception())

        task.add_done_callback(done)

    async def _reply(self, agent: Agent, event: dict, channel: str, root: str, is_dm: bool) -> None:
        key = f"{agent.name}|{channel}|{root}"
        ts = event["ts"]
        reply_thread = None if root == "dm" else root
        async with self.locks[key]:
            record = self.sessions.get(key) or {}
            await self._react(agent.client, channel, ts, "eyes", True)
            try:
                async with self.semaphore:
                    prompt = await self._build_prompt(agent, event, channel, root, record)
                    result = await self._run_cli(agent, prompt, record.get("session_id"))
            except asyncio.CancelledError:
                log.info("[%s] job cancelled (%s)", agent.name, key)
                raise
            except Exception as e:  # noqa: BLE001
                if isinstance(e, CliError):
                    log.error("[%s] %s", agent.name, e)
                else:
                    log.exception("[%s] error", agent.name)
                await self._post(agent, channel, reply_thread,
                                 f"⚠️ I couldn't complete the request: {e}", convert=False)
                return
            finally:
                await self._react(agent.client, channel, ts, "eyes", False)

            previous = float(record.get("last_ts", 0) or 0)
            new_record = dict(record)
            new_record.update({
                "session_id": result.session_id or record.get("session_id"),
                "last_ts": ts if float(ts) > previous else record.get("last_ts"),
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            if not new_record["session_id"]:
                new_record.pop("session_id")
            self.sessions.set(key, new_record)
            log.info("[%s] replied in %.0fs%s", agent.name, result.seconds,
                     f", estimated cost ${result.cost:.3f}" if result.cost else "")
            await self._post(agent, channel, reply_thread, result.text)

    async def _read_thread(self, client, channel: str, root: str) -> list[dict]:
        messages: list[dict] = []
        cursor = None
        try:
            while True:
                r = await client.conversations_replies(channel=channel, ts=root, limit=200, cursor=cursor)
                messages += r.get("messages", [])
                cursor = (r.get("response_metadata") or {}).get("next_cursor")
                if not cursor or len(messages) >= 2000:
                    break
        except Exception as e:  # noqa: BLE001
            log.warning("Can't read thread %s/%s: %s", channel, root, e)
        return messages

    async def _read_dm(self, client, channel: str, before_ts: str) -> list[dict]:
        try:
            r = await client.conversations_history(channel=channel, latest=before_ts,
                                                   limit=int(self.settings.max_context_messages))
            return [m for m in reversed(r.get("messages", [])) if not m.get("thread_ts") or m.get("thread_ts") == m.get("ts")]
        except Exception as e:  # noqa: BLE001
            log.warning("Can't read direct message history %s: %s", channel, e)
            return []

    async def _build_prompt(self, agent: Agent, event: dict, channel: str, root: str, record: dict) -> str:
        """The message for the CLI: the new message plus the thread messages the agent hasn't seen.
        CLIs that can't resume a conversation get the whole conversation every time instead."""
        ts = float(event["ts"])
        keeps_session = agent.backend.keeps_sessions and bool(record.get("session_id"))
        stateless = not agent.backend.keeps_sessions
        after = float(record["last_ts"]) if keeps_session and record.get("last_ts") else None
        floor = float(record["reset_ts"]) if record.get("reset_ts") else None

        history: list[dict] = []
        if event.get("thread_ts"):
            history = await self._read_thread(agent.client, channel, root)
        elif root == "dm" and stateless:
            history = await self._read_dm(agent.client, channel, event["ts"])
        context: list[dict] = []
        for m in history:
            mts = float(m.get("ts", 0))
            if mts >= ts or (after is not None and mts <= after) or (floor is not None and mts <= floor):
                continue
            if m.get("subtype") not in VALID_SUBTYPES:
                continue
            if self._author_agent(m) is agent and not stateless:
                continue  # a resumed session already knows its own replies
            context.append(m)
        context = context[-int(self.settings.max_context_messages):]

        parts: list[str] = []
        new_session = not keeps_session
        if new_session and (agent.backend.instructions == "prepend" or not agent.backend.instructions_flag_ok):
            parts += ["<slack_instructions>", agent.instructions_file.read_text(encoding="utf-8"),
                      "</slack_instructions>", ""]
        if context:
            parts.append("Thread messages you haven't seen yet:" if keeps_session
                         else "The conversation so far:")
            for m in context:
                author = "You" if self._author_agent(m) is agent else await self._author_name(m)
                parts.append(f"[{author}] {await self._readable_text(m, agent)}")
            parts.append("")
        parts.append(f"New message for you from {await self._author_name(event)}:")
        parts.append(await self._readable_text(event, agent))
        return "\n".join(parts)

    async def _run_cli(self, agent: Agent, prompt: str, session_id: str | None) -> Result:
        backend = agent.backend
        if not backend.keeps_sessions:
            session_id = None
        start = time.monotonic()
        code, out, err = await self._call_cli(agent, prompt, session_id)
        flag = next((t for t in backend.instructions_args if t.startswith("--")), None)
        if code != 0 and backend.instructions == "args" and backend.instructions_flag_ok and flag \
                and re.search(rf"(unknown|unexpected).{{0,20}}{re.escape(flag)}", out + err, re.I):
            log.warning("This %s version doesn't know %s: putting the Slack instructions in the message.",
                        backend.name, flag)
            backend.instructions_flag_ok = False
            prompt = ("<slack_instructions>\n" + agent.instructions_file.read_text(encoding="utf-8")
                      + "</slack_instructions>\n\n" + prompt)
            code, out, err = await self._call_cli(agent, prompt, session_id)
        parsed = parse_output(backend, out)
        if session_id and not parsed.text and any(re.search(p, out + err, re.I) for p in backend.session_errors):
            log.warning("[%s] session %s not found: starting a new one", agent.name, session_id)
            if backend.instructions == "prepend" or not backend.instructions_flag_ok:
                prompt = ("<slack_instructions>\n" + agent.instructions_file.read_text(encoding="utf-8")
                          + "</slack_instructions>\n\n" + prompt)
            code, out, err = await self._call_cli(agent, prompt, None)
            parsed = parse_output(backend, out)
        # An error counts when there is no reply, or when the CLI also exited with an error
        # (a failed turn can leave half a reply behind; warnings in a successful run don't count).
        if parsed.error and (not parsed.text or code != 0):
            raise CliError(f"{backend.name} reported an error: {parsed.error[-800:]}")
        if not parsed.text:
            if code != 0:
                detail = (err.strip() or out.strip() or f"exit code {code}")[-800:]
                raise CliError(f"{backend.name} didn't answer properly: {detail}")
            parsed.text = "✅ Done (the reply had no text)."
        return Result(text=parsed.text, session_id=parsed.session_id or session_id,
                      cost=parsed.cost, seconds=time.monotonic() - start)

    async def _call_cli(self, agent: Agent, prompt: str, session_id: str | None) -> tuple[int, str, str]:
        backend = agent.backend
        args = build_command(backend, agent, session_id, prompt, agent.instructions_file)
        log.info("[%s] running %s%s", agent.name, backend.name, " (resuming the conversation)" if session_id else "")
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(agent.folder), env=child_env(),
            stdin=asyncio.subprocess.PIPE if backend.prompt == "stdin" else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **child_flags())
        data = prompt.encode("utf-8") if backend.prompt == "stdin" else None
        try:
            out, err = await asyncio.wait_for(proc.communicate(data),
                                              timeout=float(self.settings.timeout_minutes) * 60)
        except asyncio.TimeoutError:
            await kill_tree(proc)
            raise CliError(f"timed out after {self.settings.timeout_minutes} minutes")
        except asyncio.CancelledError:
            await kill_tree(proc)
            raise
        return (proc.returncode or 0,
                out.decode("utf-8", errors="replace"),
                err.decode("utf-8", errors="replace"))

    # ----- posting ----------------------------------------------------------- #

    def _mentions(self, text: str, author: Agent, include_agents: bool) -> tuple[str, set[str]]:
        """Turns @Name into real Slack mentions. Returns the text and the agents mentioned."""
        mentioned: set[str] = set()
        names: list[tuple[str, str, bool]] = [(a.name, a.user_id, True) for a in self.agents if a is not author]
        names += [(alias, uid, False) for alias, uid in self.human_aliases.items()]
        for name, uid, is_agent in sorted(names, key=lambda x: -len(x[0])):
            pattern = re.compile(rf"(?<![\w@.<|/])@{re.escape(name)}(?!\w)", re.I)
            if not pattern.search(text):
                continue
            if is_agent:
                mentioned.add(uid)
                if not include_agents:
                    continue
            text = pattern.sub(f"<@{uid}>", text)
        return text, mentioned

    async def _post(self, agent: Agent, channel: str, thread_ts: str | None, text: str,
                    convert: bool = True) -> None:
        body = markdown_to_slack(text) if convert else slack_escape(text)
        chunks = split_message(body)
        # Agent mentions go in the last message only, so the mentioned agent starts once and
        # sees the whole reply as context.
        everyone: set[str] = set()
        for i, c in enumerate(chunks):
            last = i == len(chunks) - 1
            c, mentioned = self._mentions(c, agent, include_agents=last)
            everyone |= mentioned
            if last:
                missing = [u for u in everyone if f"<@{u}>" not in c]
                if missing:
                    c += "\n→ " + " ".join(f"<@{u}>" for u in missing)
            chunks[i] = c
        for c in chunks:
            kwargs: dict[str, Any] = {"channel": channel, "text": c, "unfurl_links": False, "unfurl_media": False}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            try:
                await agent.client.chat_postMessage(**kwargs)
            except Exception as e:  # noqa: BLE001
                log.error("[%s] posting to Slack failed: %s", agent.name, e)


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #

def setup_logging(state_folder: Path) -> None:
    state_folder.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logfile = logging.FileHandler(state_folder / "slack_agents.log", encoding="utf-8")
    logfile.setFormatter(fmt)
    root.handlers[:] = [console, logfile]
    for noisy in ("slack_bolt", "slack_sdk", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def load_whisper(w: Transcriber) -> None:
    """Loads the Whisper model ahead of time (the first time it downloads a few hundred MB)."""
    log.info("Loading the Whisper model '%s' (the first time it is downloaded, this can take a few minutes)...",
             w.model_name)
    try:
        await w.prepare()
        log.info("Whisper model ready: voice messages will be transcribed on this computer.")
    except Exception as e:  # noqa: BLE001
        log.warning("Can't load Whisper (%s): I'll use Slack's transcript when there is one.", e)


async def main_async(config_path: Path, check_only: bool) -> None:
    settings, agents, backends = load_config(config_path)
    setup_logging(settings.state_folder)
    for backend in backends.values():
        backend.path = find_executable(backend)
        log.info("%s CLI %s (%s)", backend.name, await cli_version(backend), backend.path)
    if not settings.authorized_users:
        log.warning("authorized_users is empty: ANYONE in the workspace can use the agents. "
                    "Your member ID will show up below with the first message you write.")

    bridge = Bridge(settings, agents)
    apps: list[tuple[Agent, AsyncApp]] = []
    width = max(len(a.name) for a in agents)
    for agent in agents:
        app = AsyncApp(token=agent.bot_token)
        agent.client = app.client
        try:
            info = await app.client.auth_test()
        except SlackApiError as e:
            sys.exit(f"{agent.name}: the bot_token doesn't work ({e.response.get('error')}).")
        except Exception as e:  # noqa: BLE001
            sys.exit(f"{agent.name}: can't reach Slack ({e}). Check the connection.")
        bridge.register(agent, info["user_id"], info.get("bot_id"))
        warning = check_scopes(agent.name, token_scopes(info), settings.transcribe_voice, agent.bot_token)
        if warning:
            log.warning(warning)
        log.info("%-*s [%s] bot @%s (%s) -> %s", width, agent.name, agent.cli, info.get("user"),
                 info["user_id"], agent.folder)

        def make_handler(a: Agent):
            async def handler(event: dict) -> None:
                await bridge.handle_event(a, event)
            return handler

        app.event("message")(make_handler(agent))
        # If the app also subscribes to app_mention, the same message arrives twice:
        # the bridge recognises it by its timestamp and answers once.
        app.event("app_mention")(make_handler(agent))
        apps.append((agent, app))

    await bridge.prepare()

    if settings.transcribe_voice:
        if Transcriber.installed():
            bridge.whisper = Transcriber(settings.whisper_model, settings.voice_language)
            log.info("Voice messages: transcribed on this computer with Whisper (model %s%s).",
                     settings.whisper_model, "" if bridge.whisper.silence_filter else ", without silence filter")
        else:
            log.warning("Voice messages: faster-whisper isn't installed, so I'll use Slack's own transcript "
                        "when there is one. To transcribe locally: python -m pip install faster-whisper")

    if check_only:
        for agent in agents:
            try:
                await AsyncWebClient().apps_connections_open(app_token=agent.app_token)
            except SlackApiError as e:
                sys.exit(f"{agent.name}: the app_token doesn't work ({e.response.get('error')}). "
                         "Check that Socket Mode is on and that the token has connections:write.")
            except Exception as e:  # noqa: BLE001
                sys.exit(f"{agent.name}: can't reach Slack ({e}).")
        log.info("All good: configuration, Slack tokens and CLIs checked.")
        log.info("Slack instructions for the agents written to: %s", settings.state_folder)
        return

    handlers: list[AsyncSocketModeHandler] = []
    try:
        for agent, app in apps:
            h = AsyncSocketModeHandler(app, agent.app_token)
            await h.connect_async()
            handlers.append(h)
        log.info("Listening on Slack with %d agents. Ctrl+C to quit.", len(agents))
        if bridge.whisper:
            asyncio.create_task(load_whisper(bridge.whisper))
        await asyncio.Event().wait()
    finally:
        for h in handlers:
            try:
                await h.close_async()
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Connect your command-line AI agents to Slack.")
    parser.add_argument("config", nargs="?", default=str(Path(__file__).with_name("config.toml")),
                        help="configuration file (default: config.toml next to the script)")
    parser.add_argument("--check", action="store_true",
                        help="check configuration, tokens and CLIs, then exit")
    a = parser.parse_args()
    try:
        asyncio.run(main_async(Path(a.config), a.check))
    except KeyboardInterrupt:
        print("\nClosed.")


if __name__ == "__main__":
    main()
