"""Unit tests: config validation, output parsing, Windows argument safety, downloads, audio, Bolt wiring."""
import asyncio, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import slack_agents as S


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def config_errors(text: str) -> str:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "a").mkdir()
    p = tmp / "config.toml"
    p.write_text(text.replace("FOLDER", str(tmp / "a")))
    try:
        S.load_config(p)
        return ""
    except SystemExit as e:
        return str(e)


AGENT = ('[[agents]]\nname = "A"\ncli = "{cli}"\nfolder = "FOLDER"\n'
         'bot_token = "xoxb-1111111111-2222222222-abc"\napp_token = "xapp-1-A111-2222-abcdefabcdef"\n')

print("1. Configuration")
check(config_errors('authorized_users = ["U0FLAVIO1"]\n' + AGENT.format(cli="claude")) == "", "Valid minimal config")
e = config_errors('authorized_users = ["U0123456789"]\n' + AGENT.format(cli="claude"))
check("example ID U0123456789" in e, "Example member ID rejected")
e = config_errors(AGENT.format(cli="nope"))
check("unknown cli 'nope'" in e, "Unknown CLI name explained")
e = config_errors('[cli.x]\ncommand = ["x"]\nprompt = "arg"\noutput = "yaml"\ncolour = 1\n' + AGENT.format(cli="x"))
check("{prompt} placeholder" in e and 'output must be' in e and "unknown keys colour" in e, "Custom CLI mistakes explained")
e = config_errors('[cli.y]\ncommand = ["y"]\noutput = "json"\n' + AGENT.format(cli="y"))
check("set text_field" in e, "json output without text_field explained")
e = config_errors('[cli.z]\ncommand = ["z"]\noutput = "jsonl"\n' + AGENT.format(cli="z"))
check("set events.text" in e, "jsonl output without events.text explained")
tmp = Path(tempfile.mkdtemp()); (tmp / "a").mkdir()
(tmp / "c.toml").write_text(('[cli.codex]\nexecutable = "/opt/codex"\ndefault_args = ["-c", "sandbox_mode=read-only"]\n'
                             + AGENT.format(cli="codex")).replace("FOLDER", str(tmp / "a")))
_, agents, backends = S.load_config(tmp / "c.toml")
b = backends["codex"]
check(b.executables == ["/opt/codex"] and b.default_args == ["-c", "sandbox_mode=read-only"]
      and b.output == "jsonl" and b.keeps_sessions, "A [cli.codex] section tweaks the preset and keeps the rest")
(tmp / "d.toml").write_text(('[cli.claude]\ncommand = ["/usr/local/bin/claude", "-p", "--output-format", "json", "{args}"]\n'
                             + AGENT.format(cli="claude")).replace("FOLDER", str(tmp / "a")))
_, _, backends = S.load_config(tmp / "d.toml")
check(backends["claude"].executables == ["/usr/local/bin/claude"], "Overriding a preset's command also changes its executable")

print("2. Reading outputs")
cl = S.build_backend("claude", S.BUILTIN_BACKENDS["claude"], [])
p = S.parse_output(cl, '{"type":"result","is_error":false,"result":"hi","session_id":"s1","total_cost_usd":0.2}')
check((p.text, p.session_id, p.error, p.cost) == ("hi", "s1", None, 0.2), "Claude JSON")
p = S.parse_output(cl, '[{"type":"system"},{"type":"result","is_error":false,"result":"hi","session_id":"s1"}]')
check(p.text == "hi", "Claude JSON as a list of events")
p = S.parse_output(cl, '{"type":"result","subtype":"error_max_turns","is_error":true,"session_id":"s1"}')
check(p.text is None and p.error == "error_max_turns", "Claude error flag")
gx = S.build_backend("gemini", S.BUILTIN_BACKENDS["gemini"], [])
lines = [{"type": "init", "session_id": "g1"},
         {"type": "message", "role": "model", "content": "Thinking "},
         {"type": "tool_use"}, {"type": "tool_result"},
         {"type": "message", "role": "assistant", "content": "Final ", "delta": True},
         {"type": "message", "role": "assistant", "content": "answer.", "delta": True},
         {"type": "error", "severity": "warning", "message": "slow"}, {"type": "result", "status": "success"}]
p = S.parse_output(gx, "garbage line\n" + "\n".join(json.dumps(x) for x in lines))
check((p.text, p.session_id, p.error) == ("Final answer.", "g1", "slow"), f"Gemini stream-json: {p}")
cx = S.build_backend("codex", S.BUILTIN_BACKENDS["codex"], [])
lines = [{"type": "thread.started", "thread_id": "t1"},
         {"type": "item.completed", "item": {"type": "assistant_message", "text": "older"}},
         {"type": "item.completed", "item": {"type": "agent_message", "text": "newest"}}]
p = S.parse_output(cx, "\n".join(json.dumps(x) for x in lines))
check((p.text, p.session_id) == ("newest", "t1"), "Codex JSONL (both item type names)")
tx = S.build_backend("t", {"command": ["t"], "output": "text"}, [])
check(S.parse_output(tx, "  plain answer \n").text == "plain answer" and not tx.keeps_sessions, "Plain text CLI, no sessions")
cu = S.build_backend("cursor", S.BUILTIN_BACKENDS["cursor"], [])
check(S.parse_output(cu, "").text is None, "Cursor failure prints no JSON")

print("3. Commands and Windows safety")
agent = S.Agent(name="A", cli="t", folder=Path("/w"), description="", bot_token="x", app_token="x")
custom = S.build_backend("t", {"command": ["t", "--msg", "{prompt}", "--dir={workdir}", "{args}"], "prompt": "arg",
                               "default_args": ["--fast"]}, [])
custom.path = "/bin/t"
agent.backend = custom
check(S.build_command(custom, agent, None, 'say "hi"\n100%', None) == ["/bin/t", "--msg", 'say "hi"\n100%', "--dir=/w", "--fast"],
      "Placeholders filled, prompt as argument untouched outside Windows batch files")
agent.args = ["--slow"]
check(S.build_command(custom, agent, None, "x", None)[-1] == "--slow", "Agent args replace the preset default args")
check(S.cmd_safe('run "x" & del %PATH% | y\nnext ^ !') == "run x  del PATH  y / next  ", "cmd_safe strips cmd.exe metacharacters")
real_name = os.name
try:
    custom.path = "C:/tools/t.cmd"
    S.os.name = "nt"
    cmd = S.build_command(custom, agent, None, 'a "b" & calc\nline2', None)
finally:
    S.os.name = real_name
check(cmd[2] == "a b  calc / line2", f"On Windows a .cmd CLI gets a sanitised prompt: {cmd}")

print("4. Downloads from Slack")
from aiohttp import web


async def downloads():
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    token, outside = "xoxb-right", []

    async def start(host, port, routes):
        app = web.Application()
        for path, h in routes.items():
            app.router.add_get(path, h)
        r = web.AppRunner(app); await r.setup(); await web.TCPSite(r, host, port).start(); return r

    async def f(req): raise web.HTTPFound("http://127.0.0.1:8766/real")
    async def expired(req): return web.Response(status=403, text="forbidden")
    async def external(req): raise web.HTTPFound("http://127.0.0.1:8767/x")
    async def real(req):
        if req.headers.get("Authorization") == f"Bearer {token}":
            return web.Response(body=b"AUDIO-OK", content_type="audio/webm")
        return web.Response(status=403, text="forbidden")
    async def x(req):
        outside.append(req.headers.get("Authorization"))
        return web.Response(body=b"OUTSIDE", content_type="audio/webm")
    r1 = await start("127.0.0.1", 8765, {"/f": f, "/expired": expired, "/external": external})
    r2 = await start("127.0.0.1", 8766, {"/real": real})
    r3 = await start("127.0.0.1", 8767, {"/x": x})
    original = S.is_slack_host
    S.is_slack_host = lambda url: url.startswith(("http://127.0.0.1:8765", "http://127.0.0.1:8766"))
    try:
        check(await S.download_from_slack("http://127.0.0.1:8765/f", token) == b"AUDIO-OK", "Follows redirects keeping the token between Slack hosts")
        check(await S.download_from_slack("http://127.0.0.1:8765/external", token) == b"OUTSIDE" and outside == [None], "Never sends the token outside Slack")
        tmp = Path(tempfile.mkdtemp())
        settings = S.Settings(authorized_users=[], state_folder=tmp)
        ag = S.Agent(name="Assistant", cli="claude", folder=tmp, description="", bot_token=token, app_token="x")
        class Fresh:
            async def files_info(self, file): return {"file": {"url_private_download": "http://127.0.0.1:8765/f"}}
        ag.client = Fresh()
        bridge = S.Bridge(settings, [ag])
        p = await bridge._download(ag, {"id": "F1", "filetype": "webm", "url_private_download": "http://127.0.0.1:8765/expired"})
        check(p.read_bytes() == b"AUDIO-OK", "403: asks Slack for a fresh address and retries")
        class Broken:
            async def files_info(self, file): raise RuntimeError("missing_scope")
        ag.client = Broken()
        try:
            await bridge._download(ag, {"id": "F2", "filetype": "webm", "url_private_download": "http://127.0.0.1:8765/expired"}); msg = ""
        except RuntimeError as e:
            msg = str(e)
        check("HTTP 403" in msg and "files:read" in msg and "Assistant" in msg, f"Explains what to fix: {msg[:80]}…")
    finally:
        S.is_slack_host = original
        for r in (r1, r2, r3):
            await r.cleanup()

asyncio.run(downloads())

class Resp:
    headers = {"X-OAuth-Scopes": "chat:write,channels:history, files:read"}
check(S.token_scopes(Resp()) == {"chat:write", "channels:history", "files:read"}, "Reads token scopes")
warning = S.check_scopes("Bot", {"chat:write"}, True, "xoxb-1234567890-abcdefghWXYZ")
check("files:read (needed for downloading voice messages)" in warning and "only has: chat:write" in warning, "Explains missing scopes")
check(S.check_scopes("Bot", set(S.REQUIRED_SCOPES), True, "x") is None, "No warning when all scopes are there")

print("5. Audio decoding (real clips in Slack's formats)")
audio = Path(tempfile.mkdtemp())
have_audio_tools = shutil.which("ffmpeg") and all(
    __import__("importlib").util.find_spec(m) for m in ("av", "numpy", "faster_whisper"))
if have_audio_tools:
    def ffmpeg(*a):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *a], check=True)
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=3"]
    ffmpeg(*tone, "-ac", "2", "-ar", "48000", "-c:a", "libopus", str(audio / "clip.webm"))
    ffmpeg(*tone, "-c:a", "aac", str(audio / "clip.m4a"))
    ffmpeg(*tone, "-f", "lavfi", "-i", "color=size=64x64:duration=3", "-shortest", "-c:a", "aac", "-c:v", "libx264",
           str(audio / "clip.mp4"))
    ffmpeg("-f", "lavfi", "-i", "color=size=64x64:duration=1", "-c:v", "libx264", str(audio / "video_only.mp4"))
    (audio / "broken.webm").write_bytes(os.urandom(5000))
    for name in ("clip.webm", "clip.m4a", "clip.mp4"):
        a = S.Transcriber.decode(audio / name)
        check(abs(len(a) / 16000 - 3.0) < 0.05 and str(a.dtype) == "float32", f"{name}: {len(a)/16000:.2f}s at 16 kHz")
    for name in ("broken.webm", "video_only.mp4"):
        try:
            S.Transcriber.decode(audio / name); ok = False
        except Exception:
            ok = True
        check(ok, f"{name}: error raised (the bridge then falls back)")
    import faster_whisper
    used = {}
    class FakeModel:
        def __init__(self, name, device, compute_type): used.update(name=name, device=device, compute_type=compute_type)
        def transcribe(self, audio, language, vad_filter, beam_size):
            used.update(language=language, kind=type(audio).__name__)
            class Seg: text = " hello world "
            return iter([Seg()]), None
    original = faster_whisper.WhisperModel
    faster_whisper.WhisperModel = FakeModel
    w = S.Transcriber("small", "it")
    text = asyncio.run(w.transcribe(audio / "clip.webm"))
    faster_whisper.WhisperModel = original
    check(text == "hello world" and used == {"name": "small", "device": "cpu", "compute_type": "int8", "language": "it", "kind": "ndarray"},
          f"Whisper gets decoded audio, on CPU: {used}")

else:
    print("  skipped: needs ffmpeg, PyAV, numpy and faster-whisper")
print("6. Slack library wiring (message + app_mention for the same message)")
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.request.async_request import AsyncBoltRequest


async def bolt():
    seen = []
    tmp = Path(tempfile.mkdtemp())
    settings = S.Settings(authorized_users=["U0FLAVIO1"], state_folder=tmp)
    ag = S.Agent(name="Coach", cli="claude", folder=tmp, description="", bot_token="x", app_token="x")
    bridge = S.Bridge(settings, [ag])
    bridge.register(ag, "U0COACH01", "B0COACH01")
    bridge._start = lambda agent, event, channel, root, is_dm: seen.append(event["type"])
    async def authorize(**kw):
        return AuthorizeResult(enterprise_id=None, team_id="T1", bot_token="xoxb-test", bot_id="B0COACH01", bot_user_id="U0COACH01")
    app = AsyncApp(authorize=authorize)
    async def handler(event): await bridge.handle_event(ag, event)
    app.event("message")(handler); app.event("app_mention")(handler)
    base = {"channel": "C1", "ts": "1762100000.000100", "text": "<@U0COACH01> hi", "user": "U0FLAVIO1"}
    for ev in ({"type": "app_mention", **base}, {"type": "message", "channel_type": "channel", **base},
               {"type": "message", "channel_type": "channel", **base, "ts": "1.2", "user": "U0COACH01", "bot_id": "B0COACH01"}):
        body = {"type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event": ev,
                "authorizations": [{"team_id": "T1", "user_id": "U0COACH01", "is_bot": True}]}
        await app.async_dispatch(AsyncBoltRequest(body=body, mode="socket_mode"))
    await asyncio.sleep(0.3)
    check(seen == ["app_mention"], f"Handled once, own messages ignored: {seen}")

asyncio.run(bolt())
print("\nAll unit tests passed.")
