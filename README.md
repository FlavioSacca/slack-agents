# Slack agents

Turn the command-line AI agents on your computer into Slack teammates. Each agent gets its own Slack bot (name, avatar, @mention), works in its own folder with its own instructions, and answers in Slack. Agents can also ask each other for help.

Supported out of the box: **Claude Code**, **Codex CLI**, **Gemini CLI** and **Cursor CLI**. Any other command-line agent can be connected by describing it in the config file. Different agents can use different CLIs and still talk to each other.

The bridge runs on your computer and uses Slack's Socket Mode, so there are no ports to open and no server to run. Every reply is a normal non-interactive run of the agent's CLI inside its folder, with your own sign-in, instruction files and settings.

## 1. Requirements

- **Python 3.11 or newer.** On Windows, Python 3.14 is the safe choice: some libraries for voice transcription aren't published for 3.15 yet. Install it with `py install 3.14` and run everything with `py -V:3.14` instead of `python`.
- **The CLIs your agents use,** installed and signed in. Check that each works in a terminal (`claude`, `codex`, `gemini`, `agent`).
- **The Slack libraries:**
  ```
  python -m pip install slack-bolt aiohttp
  ```
- **Optional: Whisper,** to transcribe voice messages on your computer:
  ```
  python -m pip install faster-whisper
  ```

## 2. Create one Slack app per agent (about 5 minutes)

Repeat these steps for every agent:

1. Go to https://api.slack.com/apps → **Create New App** → **From a manifest** and pick your workspace.
2. Paste the contents of `slack_manifest.yaml`, replace `AGENT_NAME` with the agent's name, then **Create**.
3. **Install App** → **Install to Workspace** → **Allow**.
4. In **OAuth & Permissions**, copy the *Bot User OAuth Token* (`xoxb-…`). This is the `bot_token`.
5. In **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**, add the `connections:write` scope and press **Generate**. Copy the token (`xapp-…`). This is the `app_token`.
6. (Optional) In **Basic Information** → **Display Information**, upload an avatar.

## 3. Configure

1. Copy `config.example.toml` to `config.toml`, next to the script.
2. For each agent fill in its name, `cli`, folder, description and the two tokens.
3. Put your Slack member ID in `authorized_users`: profile picture → **Profile** → **⋮** → **Copy member ID**.
4. Check that everything is in place:
   ```
   python slack_agents.py --check
   ```

## 4. Start

```
python slack_agents.py
```

The agents are online as long as this window stays open. The log is in `state/slack_agents.log`.

## 5. Use them in Slack

- **Group:** create a private channel (e.g. `#agents`) and run `/invite @Researcher @Developer @Reviewer`.
- **Talk to one:** `@Researcher find last year's sales figures and summarise them`. The agent adds 👀 while working and replies in the thread.
- **Carry on:** keep writing in the thread, even without a mention. The agent that spoke last answers, remembers the conversation and also sees what others wrote in the thread meanwhile.
- **Call by name:** start the message with the name, without @: `Reviewer, what do you think?`. That agent receives the whole thread, so it also reads the first agent's answer.
- **Let them collaborate:** `@Developer write the script, then ask @Reviewer to check it`. When an agent writes `@Reviewer` in its reply, the Reviewer receives it and answers in the same thread.
- **Dedicated channel:** put a channel ID in an agent's `dedicated_channels`, and there the agent answers every message.
- **Direct messages:** open the agent from Slack's Apps list and write to it like a colleague. Write `reset` to start over.
- **Voice messages:** send them anywhere you would write. They are transcribed and passed on as text. You can also call an agent by saying its name first.
- **Stop them:** write `stop` in the thread. Outside a thread, it stops the whole channel. The agents confirm with 🛑.
- **Notes for people:** messages starting with `//`, or mentioning a person, are not passed to any agent.
- **Automatic limit:** after `max_agent_exchanges` replies in a row between agents (6 by default), they pause and wait for you to write in the thread.

Each thread is a separate conversation. Sessions are saved in `state/sessions.json`, so they survive a restart.

## Supported CLIs

| CLI | `cli =` | Instructions file in the folder | Default permissions | To allow more |
| --- | --- | --- | --- | --- |
| Claude Code | `claude` | `CLAUDE.md` | reads and edits files (`--permission-mode acceptEdits`) | `args = ["--permission-mode", "auto"]` or `"bypassPermissions"` |
| Codex CLI | `codex` | `AGENTS.md` | edits files in its folder (`-c sandbox_mode=workspace-write`) | `args = ["-c", "sandbox_mode=danger-full-access"]` |
| Gemini CLI | `gemini` | `GEMINI.md` | approves file edits (`--approval-mode auto_edit`) | `args = ["--approval-mode", "yolo"]` |
| Cursor CLI | `cursor` | `AGENTS.md`, `.cursor/rules` | reads and proposes, changes nothing | `args = ["--force"]` |

Remember that `args` replaces the default arguments, so include the permission setting you want. The bridge also gives every agent short Slack instructions: who the other agents are, how to mention them and how to write in chat. You don't need to add anything to the agents' own instruction files, but you can add your own rules there (e.g. "always have the Reviewer check your code").

For Codex, set permissions with `-c sandbox_mode=…`, not `-s`/`--sandbox`: Codex refuses `-s` when continuing a conversation.

**Gemini CLI needs an API key.** Since June 18, 2026 Google no longer serves Gemini CLI to individual Google-account sign-ins (free, Google AI Pro and AI Ultra). Set the `GEMINI_API_KEY` environment variable with a key from Google AI Studio before starting the bridge: usage is then billed per token, with a small free quota. Vertex AI and Gemini Code Assist Standard/Enterprise sign-ins work too. The preset passes `--skip-trust`, because headless Gemini CLI refuses to run in a folder that isn't marked as trusted.

**Signing in for unattended use.** The bridge runs the CLIs in the background, where they can't open a browser to sign in again. If an agent replies with an authentication error, sign that CLI in again in a terminal. For Claude Code, `claude setup-token` creates a token valid for one year. Set it as the `CLAUDE_CODE_OAUTH_TOKEN` environment variable and Claude Code won't need to sign in again.

## Connecting another CLI

Describe it in a `[cli.NAME]` section of `config.toml` and use `cli = "NAME"` in an agent. You say how to start it, whether the message goes on standard input or as an argument, and how to read its output: plain text, one JSON object, or one JSON event per line. `config.example.toml` explains every key and has two complete examples. The built-in CLIs are defined the same way, and a `[cli.claude]`-style section can change any of their settings, such as `executable` when the program isn't on the PATH.

## Start automatically

**Windows.** Create `start_agents.bat` next to the script:
```
@echo off
cd /d "%~dp0"
py -V:3.14 slack_agents.py
```
Press Win+R, type `shell:startup` and put a shortcut to the `.bat` there. In the shortcut's properties, set *Run: Minimized*.

**macOS.** Create `~/Library/LaunchAgents/com.slack.agents.plist`, using your paths. Launch agents don't see your shell's PATH, so in `config.toml` set each CLI's full path, e.g. `[cli.claude]` with `executable = "/Users/you/.local/bin/claude"` (find it with `which claude`).
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.slack.agents</string>
  <key>ProgramArguments</key><array>
    <string>/usr/local/bin/python3</string>
    <string>/Users/you/slack-agents/slack_agents.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```
Then run `launchctl load ~/Library/LaunchAgents/com.slack.agents.plist`.

## Good to know

- **Security.** Whoever can write to the agents can make them act on your computer's files. That's why only the IDs in `authorized_users` are listened to and other bots are ignored. Keep `config.toml` private: it holds the tokens.
- **Permissions.** From Slack, agents can't ask you "may I run this?" as they do in a terminal. Anything not already allowed is refused, and the agent says so in its reply.
- **Costs.** Every reply is a run of the CLI and uses your plan or API credit, just as if you ran it yourself. The log shows an estimated cost when the CLI reports one. For Claude Code, if the `ANTHROPIC_API_KEY` environment variable is set, it is used and billed even when you have a subscription.
- **Slack permissions.** At startup the bridge checks each bot's scopes and says exactly which are missing. The easiest fix is to paste `slack_manifest.yaml` into the app's **App Manifest** page and reinstall the app.
- **Attachments.** Voice messages are transcribed. Other files attached in Slack are not passed to the agents: put files in their folders.
- **Slack instructions.** The instructions the bridge gives the agents are in `state/slack_instructions_<Name>.md`. They are rewritten at every start, so put your own rules in the agent's instruction file instead.

## Tests

The tests run the bridge against a simulated Slack and fake versions of every supported CLI (each prints output in that CLI's real format), so they need no Slack workspace and no AI account:

```
python tests/test_units.py
python tests/test_bridge.py
```

They need Linux or macOS. The audio checks also need `ffmpeg` and `faster-whisper`; without them they are skipped.
