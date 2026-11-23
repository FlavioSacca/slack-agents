#!/usr/bin/env python3
"""Fake agent CLI for the tests. Its file name picks the flavour it imitates:
fake_claude, fake_codex, fake_gemini, fake_cursor, fake_text."""
import json, os, sys, time, uuid

flavour = os.path.basename(sys.argv[0]).replace("fake_", "").replace(".py", "")
args = sys.argv[1:]
if "--version" in args:
    print(f"{flavour} 9.9.9 (fake)"); sys.exit(0)

def arg_after(flag):
    return args[args.index(flag) + 1] if flag in args else None

# How the message arrives
if flavour == "text":
    prompt = args[-1]
    stdin_text = ""
else:
    stdin_text = sys.stdin.read()
    prompt = stdin_text

# Session to resume
resume = None
if flavour in ("claude", "gemini", "cursor"):
    resume = arg_after("--resume")
elif flavour == "codex" and args[:2] == ["exec", "resume"]:
    resume = args[-2]

name = os.path.basename(os.getcwd())
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"flavour": flavour, "agent": name, "args": args, "prompt": prompt,
                        "resume": resume, "pid": os.getpid()}) + "\n")

if flavour == "claude" and os.environ.get("FAKE_NO_FILE_FLAG") and "--append-system-prompt-file" in args:
    print("error: unknown option '--append-system-prompt-file'", file=sys.stderr); sys.exit(1)
if resume == "bad-session":
    msg = {"claude": "No conversation found with session ID: bad-session",
           "codex": "Error: thread bad-session not found",
           "gemini": "Error resuming session: session bad-session not found",
           "cursor": "Chat bad-session not found"}[flavour]
    print(msg, file=sys.stderr); sys.exit(1)

sid = resume or str(uuid.uuid4())
new = prompt.rsplit("New message for you from", 1)[-1]
if "SLEEP" in new:
    time.sleep(30)
others = {"researcher": "Developer", "developer": "Researcher"}
other = others.get(name, "Researcher")
if "ERROR" in new:
    text = None
elif "PINGPONG" in new:
    text = f"**Ok** from {name}. PINGPONG @{other}"
elif "ASK" in new:
    text = "Asking @reviewer to check. Thanks @Flavio, write to mario@reviewer.com"
elif "LONG" in new:
    block = "\n".join(f"line {i} <tag> & stuff" for i in range(150))
    text = ("Part 1 " + "x" * 3000 + "\n\nPlease @Reviewer have a look.\n\n```python\n" + block
            + "\n```\n\n" + "end " * 400)
else:
    text = f"## Done\nReply from {name} ({flavour}).\n- point *one*\n- point **two**"

if flavour in ("claude", "cursor"):
    if text is None:
        if flavour == "cursor":
            print("boom", file=sys.stderr); sys.exit(1)
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "result": "boom", "session_id": sid})); sys.exit(1)
    out = {"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid}
    if flavour == "claude":
        out["total_cost_usd"] = 0.01
    print(json.dumps(out))
elif flavour == "codex":
    events = [{"type": "thread.started", "thread_id": sid}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "thinking..."}},
              {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Let me look."}},
              {"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "command": "ls"}}]
    if text is None:
        events.append({"type": "turn.failed", "error": {"message": "boom"}})
        print("\n".join(json.dumps(e) for e in events)); sys.exit(1)
    events += [{"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": text}},
               {"type": "turn.completed", "usage": {"input_tokens": 10}}]
    print("\n".join(json.dumps(e) for e in events))
elif flavour == "gemini":
    events = [{"type": "init", "session_id": sid, "model": "gemini-x"},
              {"type": "message", "role": "user", "content": prompt[:50]},
              {"type": "message", "role": "assistant", "content": "I'll check the files ", "delta": True},
              {"type": "message", "role": "assistant", "content": "first.", "delta": True},
              {"type": "tool_use", "tool_name": "ls", "tool_id": "t1"},
              {"type": "tool_result", "tool_id": "t1", "status": "success", "output": "a.txt"}]
    if text is None:
        events.append({"type": "result", "status": "error", "error": {"type": "ApiError", "message": "boom"}})
        print("\n".join(json.dumps(e) for e in events)); sys.exit(1)
    half = len(text) // 2
    events += [{"type": "message", "role": "assistant", "content": text[:half], "delta": True},
               {"type": "message", "role": "assistant", "content": text[half:], "delta": True},
               {"type": "result", "status": "success", "stats": {}}]
    print("not json: a warning line")
    print("\n".join(json.dumps(e) for e in events))
else:  # text
    if text is None:
        print("boom", file=sys.stderr); sys.exit(3)
    print(text)
