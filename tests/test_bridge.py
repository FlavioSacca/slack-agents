"""End-to-end tests of the bridge with a simulated Slack and fake CLIs of every flavour."""
import asyncio, json, os, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import slack_agents as S

# One fake CLI script, linked under a name per flavour (fake_claude, fake_codex...) in a temp folder.
FAKES = Path(tempfile.mkdtemp())
FAKE_CLI = Path(__file__).resolve().parent / "fake_cli.py"
os.chmod(FAKE_CLI, 0o755)
for flavour in ("claude", "codex", "gemini", "cursor", "text"):
    (FAKES / f"fake_{flavour}").symlink_to(FAKE_CLI)
HUMAN, STRANGER = "U0FLAVIO1", "U0OTHER99"


class Slack:
    """Fake workspace: delivers every message to all member agents of the channel."""
    def __init__(self):
        self.msgs, self.reactions, self.ts, self.bridge, self.deliveries = [], [], 1000.0, None, set()
        self.members = {"C1": ["Researcher", "Developer", "Reviewer", "Designer", "Writer"],
                        "C0SECOND1": ["Researcher", "Reviewer"], "D1": ["Researcher"], "D2": ["Writer"]}
        self.files_info_calls = 0

    def next_ts(self):
        self.ts += 1
        return f"{self.ts:.6f}"

    def add(self, channel, text, thread_ts=None, user=None, bot_id=None, author=None, files=None):
        m = {"type": "message", "channel": channel, "ts": self.next_ts(), "text": text,
             "channel_type": "im" if channel.startswith("D") else "channel"}
        if user: m["user"] = user
        if bot_id: m["bot_id"] = bot_id
        if thread_ts: m["thread_ts"] = thread_ts
        if files: m["files"] = files
        m["_author"] = author or user
        self.msgs.append(m)
        t = asyncio.create_task(self.deliver(m))
        self.deliveries.add(t); t.add_done_callback(self.deliveries.discard)
        return m

    async def deliver(self, m):
        await asyncio.sleep(0.01)
        for a in self.bridge.agents:
            if a.name in self.members[m["channel"]]:
                await self.bridge.handle_event(a, {k: v for k, v in m.items() if k != "_author"})

    def thread(self, root):
        return [m for m in self.msgs if m["ts"] == root or m.get("thread_ts") == root]

    async def settle(self, max_s=25):
        end = time.monotonic() + max_s
        while time.monotonic() < end:
            await asyncio.sleep(0.15)
            if not self.deliveries and not self.bridge.running:
                await asyncio.sleep(0.2)
                if not self.deliveries and not self.bridge.running:
                    return
        raise AssertionError("the system never settled")


class Client:
    def __init__(self, slack, agent): self.s, self.a = slack, agent
    async def chat_postMessage(self, channel, text, thread_ts=None, **kw):
        self.s.add(channel, text, thread_ts, user=self.a.user_id, bot_id=self.a.bot_id, author=self.a.name)
        return {"ok": True}
    async def reactions_add(self, channel, timestamp, name): self.s.reactions.append((self.a.name, timestamp, name, "+"))
    async def reactions_remove(self, channel, timestamp, name): self.s.reactions.append((self.a.name, timestamp, name, "-"))
    async def conversations_replies(self, channel, ts, limit=200, cursor=None):
        return {"messages": [{k: v for k, v in m.items() if k != "_author"} for m in self.s.thread(ts)]}
    async def conversations_history(self, channel, latest, limit):
        ms = [m for m in self.s.msgs if m["channel"] == channel and float(m["ts"]) < float(latest)]
        return {"messages": [{k: v for k, v in m.items() if k != "_author"} for m in reversed(ms)][:limit]}
    async def files_info(self, file):
        self.s.files_info_calls += 1
        status = "processing" if self.s.files_info_calls == 1 else "complete"
        return {"file": {"id": file, "transcription": {"status": status, "preview": {"content": "transcribed by Slack", "has_more": False}}}}
    async def users_info(self, user):
        names = {HUMAN: ("Flavio", "Flavio Sacca"), STRANGER: ("Mario", "Mario Rossi")}
        d, r = names.get(user, ("", user))
        return {"user": {"name": d.lower(), "profile": {"display_name": d, "real_name": r}}}


def calls(log):
    return [json.loads(l) for l in Path(log).read_text().splitlines()] if Path(log).exists() else []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


CONFIG = """
authorized_users = ["U0FLAVIO1"]
max_agent_exchanges = 4
[voice]
transcribe = true
[cli.claude]
executable = "{h}/fake_claude"
[cli.codex]
executable = "{h}/fake_codex"
[cli.gemini]
executable = "{h}/fake_gemini"
[cli.cursor]
executable = "{h}/fake_cursor"
[cli.mytext]
command = ["{h}/fake_text", "--message", "{{prompt}}"]
prompt = "arg"
output = "text"
"""
AGENTS = [("Researcher", "claude", None), ("Developer", "codex", None), ("Reviewer", "gemini", ["C0SECOND1"]),
          ("Designer", "cursor", None), ("Writer", "mytext", None)]


async def main():
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "cli.log"
    os.environ["FAKE_LOG"] = str(log)
    os.environ["CLAUDECODE"] = "1"
    text = CONFIG.format(h=FAKES)
    for i, (name, cli, dedicated) in enumerate(AGENTS):
        (tmp / name.lower()).mkdir()
        text += (f'\n[[agents]]\nname = "{name}"\ncli = "{cli}"\nfolder = "{tmp / name.lower()}"\n'
                 f'description = "does {name.lower()} things"\nbot_token = "xoxb-{i}000000000-abcdefghijk"\n'
                 f'app_token = "xapp-1-A{i}000-0000-abcdefabcdef"\n')
        if dedicated:
            text += f"dedicated_channels = {json.dumps(dedicated)}\n"
    (tmp / "config.toml").write_text(text)
    settings, agents, backends = S.load_config(tmp / "config.toml")
    for b in backends.values():
        b.path = S.find_executable(b)
    slack = Slack()
    bridge = S.Bridge(settings, agents)
    slack.bridge = bridge
    for i, a in enumerate(agents):
        a.client = Client(slack, a)
        bridge.register(a, f"U0BOT{i}XYZ", f"B0BOT{i}XYZ")
    R, D, V, G, W = agents
    await bridge.prepare()

    async def who_answers(channel, text, thread_ts=None, files=None):
        n0 = len(calls(log))
        m = slack.add(channel, text, thread_ts=thread_ts, user=HUMAN, files=files)
        await slack.settle()
        return m, sorted(c["agent"] for c in calls(log)[n0:])

    def replies(root, name):
        return [m for m in slack.thread(root) if m["_author"] == name]

    print("\n1. Claude: simple mention")
    m1 = slack.add("C1", f"<@{R.user_id}> hi, can you help?", user=HUMAN)
    await slack.settle()
    r = replies(m1["ts"], "Researcher")
    check(len(r) == 1 and r[0]["thread_ts"] == m1["ts"], "Researcher answers once in the thread")
    check("*Done*" in r[0]["text"] and "• point _one_" in r[0]["text"] and "• point *two*" in r[0]["text"], "Markdown converted to Slack format")
    check(("Researcher", m1["ts"], "eyes", "+") in slack.reactions and ("Researcher", m1["ts"], "eyes", "-") in slack.reactions, "👀 added then removed")
    c = calls(log)[-1]
    check(c["flavour"] == "claude" and c["agent"] == "researcher" and c["resume"] is None, "Claude run in the agent's folder, new session")
    check(c["args"][:3] == ["-p", "--output-format", "json"] and "--append-system-prompt-file" in c["args"]
          and c["args"][-2:] == ["--permission-mode", "acceptEdits"], f"Claude args: {c['args']}")
    instr = Path(c["args"][c["args"].index("--append-system-prompt-file") + 1]).read_text()
    check("@Developer" in instr and "@Reviewer" in instr and "@Flavio" in instr and "You are working in Slack" in instr,
          "English Slack instructions list the other agents and the person")
    check("New message for you from Flavio:\n@Researcher hi" in c["prompt"] and "<slack_instructions>" not in c["prompt"], "Readable prompt, instructions passed as a file")

    print("\n2. Claude: follow-up resumes the session")
    sid = bridge.sessions.get(f"Researcher|C1|{m1['ts']}")["session_id"]
    slack.add("C1", "// note without a mention", thread_ts=m1["ts"], user=HUMAN)
    slack.add("C1", f"<@{R.user_id}> and then?", thread_ts=m1["ts"], user=HUMAN)
    await slack.settle()
    c = calls(log)[-1]
    check(c["resume"] == sid and len(calls(log)) == 2, "Uses --resume with the same session")
    check("[Flavio] // note without a mention" in c["prompt"] and "Reply from researcher" not in c["prompt"], "Context: only new messages, not its own replies")

    print("\n3. Codex: JSONL events, resume form")
    t3, who = await who_answers("C1", f"<@{D.user_id}> build it")
    c = calls(log)[-1]
    r = replies(t3["ts"], "Developer")
    check(who == ["developer"] and c["args"] == ["exec", "--json", "--skip-git-repo-check", "-c", "sandbox_mode=workspace-write", "-"], f"Codex new run: {c['args']}")
    check(len(r) == 1 and "Reply from developer (codex)" in r[0]["text"] and "Let me look" not in r[0]["text"], "Only the final agent message is posted")
    check(c["prompt"].startswith("<slack_instructions>") and "You are working in Slack" in c["prompt"], "Instructions prepended for a CLI without a flag")
    sid = bridge.sessions.get(f"Developer|C1|{t3['ts']}")["session_id"]
    await who_answers("C1", f"<@{D.user_id}> more", t3["ts"])
    c = calls(log)[-1]
    check(c["args"] == ["exec", "resume", "--json", "--skip-git-repo-check", "-c", "sandbox_mode=workspace-write", sid, "-"], f"Codex resume: {c['args']}")
    check("<slack_instructions>" not in c["prompt"], "No instructions again in a resumed session")

    print("\n4. Gemini: stream-json, final block only")
    t4, who = await who_answers("C1", f"<@{V.user_id}> review")
    c = calls(log)[-1]
    r = replies(t4["ts"], "Reviewer")
    check(c["args"] == ["--output-format", "stream-json", "--skip-trust", "--approval-mode", "auto_edit"], f"Gemini new run: {c['args']}")
    check(len(r) == 1 and "Reply from reviewer (gemini)" in r[0]["text"] and "check the files" not in r[0]["text"], "Text before tool calls is dropped, chunks joined")
    sid = bridge.sessions.get(f"Reviewer|C1|{t4['ts']}")["session_id"]
    await who_answers("C1", f"<@{V.user_id}> again", t4["ts"])
    check(calls(log)[-1]["args"] == ["--output-format", "stream-json", "--skip-trust", "--resume", sid, "--approval-mode", "auto_edit"], "Gemini resumes with --resume")

    print("\n5. Cursor: JSON, fixed hint as argument, message on stdin")
    t5, who = await who_answers("C1", f"<@{G.user_id}> design it")
    c = calls(log)[-1]
    check(c["args"] == ["-p", "--output-format", "json", "--trust", "Reply to the Slack message provided on standard input."], f"Cursor args: {c['args']}")
    check("design it" in c["prompt"] and len(replies(t5["ts"], "Designer")) == 1, "Message arrives on stdin, reply posted")
    sid = bridge.sessions.get(f"Designer|C1|{t5['ts']}")["session_id"]
    await who_answers("C1", f"<@{G.user_id}> next", t5["ts"])
    check(calls(log)[-1]["args"][:6] == ["-p", "--output-format", "json", "--trust", "--resume", sid], "Cursor resumes with --resume")

    print("\n6. Generic text CLI: prompt as argument, no sessions")
    t6, who = await who_answers("C1", f"<@{W.user_id}> write a line")
    c = calls(log)[-1]
    check(c["args"][0] == "--message" and "write a line" in c["args"][1] and c["prompt"].startswith("<slack_instructions>"), "Message passed as an argument with the instructions")
    await who_answers("C1", f"<@{W.user_id}> shorter please", t6["ts"])
    p = calls(log)[-1]["prompt"]
    check("<slack_instructions>" in p and "The conversation so far:" in p and "[You] *Done*" in p and "[Flavio] @Writer write a line" in p,
          "Stateless CLI gets the whole conversation, its own replies included, every time")
    check(bridge.sessions.get(f"Writer|C1|{t6['ts']}").get("session_id") is None, "No session stored")

    print("\n7. Agents on different CLIs talk to each other, with the loop limit")
    n0 = len(calls(log))
    t7 = slack.add("C1", f"<@{R.user_id}> PINGPONG", user=HUMAN)
    await slack.settle()
    th = slack.thread(t7["ts"])
    runs = [c["flavour"] for c in calls(log)[n0:]]
    check(runs == ["claude", "codex", "claude", "codex", "claude"], f"Claude and Codex alternate: {runs}")
    check(len([m for m in th if "⏸️" in m["text"]]) == 1, "One pause notice")
    check(any(f"<@{D.user_id}>" in m["text"] for m in th if m["_author"] == "Researcher"), "@Developer becomes a real mention")
    n0 = len(calls(log))
    slack.add("C1", f"<@{D.user_id}> PINGPONG go on", thread_ts=t7["ts"], user=HUMAN)
    await slack.settle()
    check(len(calls(log)) - n0 == 5, "A person's message resets the counter")

    print("\n8. 'stop' kills a running CLI")
    t8 = slack.add("C1", f"<@{D.user_id}> SLEEP", user=HUMAN)
    await asyncio.sleep(1.5)
    pid = calls(log)[-1]["pid"]
    slack.add("C1", "Stop!", thread_ts=t8["ts"], user=HUMAN)
    await slack.settle()
    try:
        os.kill(pid, 0); alive = True
    except ProcessLookupError:
        alive = False
    check(not alive and not replies(t8["ts"], "Developer"), "Process killed, nothing posted")
    check(("Developer", t8["ts"], "eyes", "-") in slack.reactions and any(x[2] == "octagonal_sign" for x in slack.reactions), "👀 removed, 🛑 on the stop message")

    print("\n9. Errors from every CLI reach the thread")
    for agent, expected in ((R, "claude reported an error: boom"), (D, "codex reported an error: boom"),
                            (V, "gemini reported an error: boom"), (G, "cursor didn't answer properly: boom"),
                            (W, "mytext didn't answer properly: boom")):
        m, _ = await who_answers("C1", f"<@{agent.user_id}> ERROR")
        r = replies(m["ts"], agent.name)
        check(len(r) == 1 and "⚠️ I couldn't complete the request" in r[0]["text"] and expected in r[0]["text"], f"{agent.cli}: {r[0]['text'][:90] if r else 'nothing'}")

    print("\n10. A lost session is replaced by a new one")
    for agent in (R, D, V, G):
        m, _ = await who_answers("C1", f"<@{agent.user_id}> first")
        bridge.sessions.set(f"{agent.name}|C1|{m['ts']}", {"session_id": "bad-session", "last_ts": m["ts"]})
        n0 = len(calls(log))
        await who_answers("C1", f"<@{agent.user_id}> second", m["ts"])
        new = calls(log)[n0:]
        check(len(new) == 2 and new[0]["resume"] == "bad-session" and new[1]["resume"] is None
              and len(replies(m["ts"], agent.name)) == 2
              and bridge.sessions.get(f"{agent.name}|C1|{m['ts']}")["session_id"] != "bad-session", f"{agent.cli}: retried with a new session")

    print("\n11. Claude without --append-system-prompt-file")
    os.environ["FAKE_NO_FILE_FLAG"] = "1"
    m, _ = await who_answers("C1", f"<@{R.user_id}> hello")
    c = calls(log)[-1]
    check("--append-system-prompt-file" not in c["args"] and c["prompt"].startswith("<slack_instructions>")
          and not R.backend.instructions_flag_ok and len(replies(m["ts"], "Researcher")) == 1, "Falls back to instructions inside the message")
    del os.environ["FAKE_NO_FILE_FLAG"]
    R.backend.instructions_flag_ok = True

    print("\n12. Timeout")
    settings.timeout_minutes = 0.03
    m, _ = await who_answers("C1", f"<@{V.user_id}> SLEEP")
    check(any("timed out" in x["text"] for x in slack.thread(m["ts"])), "Timeout reported in the thread")
    settings.timeout_minutes = 20

    print("\n13. Strangers and other bots are ignored")
    n0 = len(calls(log))
    slack.add("C1", f"<@{R.user_id}> do something", user=STRANGER)
    slack.add("C1", f"<@{R.user_id}> I'm another bot", bot_id="B0STRANGER", user="U0OTHERBOT")
    await slack.settle()
    check(len(calls(log)) == n0, "No runs")

    print("\n14. Direct messages")
    await who_answers("D1", "hello in private")
    dm = [m for m in slack.msgs if m["channel"] == "D1" and m["_author"] == "Researcher"]
    check(len(dm) == 1 and "thread_ts" not in dm[0], "Answers in the chat, not in a thread")
    await who_answers("D1", "second message")
    check(calls(log)[-1]["resume"] is not None, "Second DM resumes the session")
    r = slack.add("D1", "reset", user=HUMAN)
    await slack.settle()
    check(("Researcher", r["ts"], "white_check_mark", "+") in slack.reactions, "reset confirmed ✅")
    await who_answers("D1", "again")
    check(calls(log)[-1]["resume"] is None, "After reset a new conversation starts")
    await who_answers("D2", "first private line")
    await who_answers("D2", "second private line")
    p = calls(log)[-1]["prompt"]
    check("[Flavio] first private line" in p and "[You]" in p, "Stateless CLI in DM gets the chat history")
    slack.add("D2", "reset", user=HUMAN); await slack.settle()
    await who_answers("D2", "after reset")
    p = calls(log)[-1]["prompt"]
    check("first private line" not in p, "...and reset hides older messages from it")

    print("\n15. Talking without mentions")
    t, _ = await who_answers("C1", f"<@{R.user_id}> hi")
    _, who = await who_answers("C1", "and then?", t["ts"])
    check(who == ["researcher"], f"In a thread, whoever spoke last answers: {who}")
    _, who = await who_answers("C1", "Developer, take a look", t["ts"])
    check(who == ["developer"], f"Called by name without @: {who}")
    _, who = await who_answers("C1", "Hey Researcher and Reviewer: title ideas?")
    check(who == ["researcher", "reviewer"], f"Two agents called by name: {who}")
    _, who = await who_answers("C1", "The Reviewer is right", t["ts"])
    check(who == ["developer"], f"A name mid-sentence doesn't count: {who}")
    _, who = await who_answers("C1", f"<@{STRANGER}> what do you think?", t["ts"])
    check(who == [], "Message to another person: no agent")
    _, who = await who_answers("C1", "// note to self", t["ts"])
    check(who == [], "Message starting with //: no agent")
    _, who = await who_answers("C1", "hello everyone")
    check(who == [], "Shared channel, outside a thread, no name: no agent")
    bridge.last_agent.clear()
    _, who = await who_answers("C1", "summarise", t["ts"])
    check(who == ["developer"], f"After a restart it remembers who spoke last: {who}")
    t2, who = await who_answers("C0SECOND1", "check this text")
    check(who == ["reviewer"], f"Dedicated channel: answers without a mention ({who})")
    _, who = await who_answers("C0SECOND1", "Researcher, find sources")
    check(who == ["researcher"], f"Another agent can still be called there ({who})")
    multi = S.Bridge(settings, [S.Agent(name=n, cli="claude", folder=tmp, description="", bot_token="x", app_token="x")
                                for n in ("FitnessCoach", "PersonalAssistant", "TravelAssistant")])
    for txt, expected in [("Fitness coach, how far today?", {"FitnessCoach"}),
                          ("Ok personal assistant and travel assistant: ideas?", {"PersonalAssistant", "TravelAssistant"}),
                          ("Travel-assistant find a flight", {"TravelAssistant"}),
                          ("the fitness coach says run", set())]:
        check(multi._called_by_name(txt) == expected, f"{txt!r} -> {sorted(multi._called_by_name(txt))}")

    print("\n16. Voice messages")
    downloaded = []
    class FakeWhisper:
        silence_filter = False
        async def transcribe(self, path): return Path(path).read_text()
    async def fake_download(agent, f):
        downloaded.append(f["id"])
        if f.get("_fail"):
            raise RuntimeError("download failed")
        p = tmp / f"{f['id']}.webm"; p.write_text(f["_spoken"]); return p
    bridge.whisper = FakeWhisper()
    bridge._download = fake_download
    def voice(fid, spoken, **extra):
        return [{"id": fid, "mimetype": "audio/webm", "filetype": "webm", "subtype": "slack_audio",
                 "url_private_download": "https://files.slack.com/x", "_spoken": spoken, **extra}]
    await who_answers("D1", "", files=voice("F1", "hello, this is a voice note"))
    check("[voice message] hello, this is a voice note" in calls(log)[-1]["prompt"], "Voice DM transcribed for the agent")
    t, _ = await who_answers("C1", f"<@{R.user_id}> hi")
    downloaded.clear()
    _, who = await who_answers("C1", "", t["ts"], files=voice("F2", "Reviewer, what do you think?"))
    check(who == ["reviewer"] and downloaded == ["F2"], f"Spoken name picks the agent, transcribed once ({who}, {downloaded})")
    await who_answers("D1", "", files=voice("F3", "x", _fail=True))
    check("[voice message] transcribed by Slack" in calls(log)[-1]["prompt"], "Falls back to Slack's transcript")
    bridge.whisper = None
    await who_answers("D1", "", files=voice("F4", "x", transcription={"status": "complete", "preview": {"content": "ready", "has_more": True}}))
    check("[voice message] ready […]" in calls(log)[-1]["prompt"], "Without Whisper uses Slack's transcript")

    print("\n17. Long replies and mentions")
    n0 = len(calls(log))
    m = slack.add("C1", f"<@{R.user_id}> LONG", user=HUMAN)
    await slack.settle()
    parts = replies(m["ts"], "Researcher")
    check(len(parts) >= 3 and all(len(p["text"]) <= 4000 for p in parts) and all(p["text"].count("```") % 2 == 0 for p in parts),
          f"Split into {len(parts)} messages, code blocks closed")
    check(sum(f"<@{V.user_id}>" in p["text"] for p in parts) == 1 and f"<@{V.user_id}>" in parts[-1]["text"], "Real mention only in the last message")
    rev = [c for c in calls(log)[n0:] if c["agent"] == "reviewer"]
    check(len(rev) == 1 and "Part 1" in rev[0]["prompt"], "The mentioned agent starts once and sees the whole reply")
    txt, _ = bridge._mentions("Asking @reviewer. Thanks @Flavio, write to mario@reviewer.com", R, True)
    check(f"<@{V.user_id}>" in txt and f"<@{HUMAN}>" in txt and "mario@reviewer.com" in txt, txt)

    print("\nAll bridge tests passed.")


asyncio.run(main())
