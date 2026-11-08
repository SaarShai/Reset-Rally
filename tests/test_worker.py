"""Worker: JSON extraction, adapter argv, real subprocess shims, Worker.step against a stub client, CLI."""

import copy
import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rally import worker as W
from rally.worker import ApiError, Client, Worker, build_argv, extract_json, family_of, offers, run_adapter

SCHEMA = {"type": "object", "required": ["kind"], "properties": {"kind": {"type": "string"}}}
PROMPT = "Read the text.\nReply with JSON."
SENTINEL = "sk-SENTINEL-7f3a"

# A tiny CLI stand-in: records what it got, then plays the script in script.json next to it.
SHIM = r'''
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
seen = {"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(),
        "cwd_files": os.listdir("."), "key": os.environ.get("ANTHROPIC_API_KEY")}
json.dump(seen, open(os.path.join(here, "seen.json"), "w"))
spec = json.load(open(os.path.join(here, "script.json")))
time.sleep(spec.get("sleep", 0))
if "file" in spec:
    open(sys.argv[sys.argv.index("-o") + 1], "w").write(spec["file"])
sys.stdout.write(spec.get("stdout", ""))
sys.stderr.write(spec.get("stderr", ""))
sys.exit(spec.get("rc", 0))
'''


@pytest.fixture
def shim(tmp_path):
    path = tmp_path / "shim.py"
    path.write_text(SHIM)

    def script(**spec):
        (tmp_path / "script.json").write_text(json.dumps(spec))

    def seen():
        return json.loads((tmp_path / "seen.json").read_text())

    inputs = []

    def runner(argv, **kw):  # real subprocess; only argv[0] changes
        inputs.append(kw.get("input"))
        return subprocess.run([sys.executable, str(path), *argv[1:]], **kw)

    script(stdout="")
    return {"script": script, "seen": seen, "runner": runner, "dir": tmp_path, "inputs": inputs}


def wu(prompt=PROMPT, timeout_s=30, schema=SCHEMA, project="tst-p1", mhash="sha256:aa"):
    return {"id": "tst-wu1", "project": project, "manifest_hash": mhash, "stage": "brain", "mode": "json",
            "prompt": prompt, "schema": schema, "timeout_s": timeout_s, "workspace": None,
            "image": None, "command": None, "files": {}}


# --- extract_json -----------------------------------------------------------------------------

def test_extract_json_from_prose():
    assert extract_json('Sure. Here it is: {"kind": "a", "n": 2} Hope it helps.') == {"kind": "a", "n": 2}


def test_extract_json_from_code_fence():
    assert extract_json('```json\n{"kind": "a"}\n```\n') == {"kind": "a"}


def test_extract_json_nested_object_returns_outer():
    assert extract_json('{"a": {"b": {"c": 1}}}') == {"a": {"b": {"c": 1}}}


def test_extract_json_braces_inside_strings():
    assert extract_json('noise {not json} {"q": "a } b { c"} tail') == {"q": "a } b { c"}


def test_extract_json_two_objects_gives_last():
    assert extract_json('{"try": 1}\nCorrected:\n{"try": 2}') == {"try": 2}


@pytest.mark.parametrize("text", ["no json here", "{broken", "[1, 2]", "", "} {"])
def test_extract_json_none(text):
    assert extract_json(text) is None


def test_extract_json_survives_deep_nesting():
    assert extract_json('{"a": ' + "[" * 100000) is None


# --- build_argv -------------------------------------------------------------------------------

def test_build_argv_claude():
    argv, stdin = build_argv("claude", {"model": "sonnet"}, wu(), "/tmp/x")
    assert argv == ["claude", "-p", "--model", "sonnet", "--tools", "", "--strict-mcp-config",
                    "--no-session-persistence", "--output-format", "text"]
    assert stdin == PROMPT


def test_build_argv_codex():
    argv, stdin = build_argv("codex", {"model": "gpt-5"}, wu(), "/tmp/x")
    assert argv == ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
                    "-C", "/tmp/x", "-o", os.path.join("/tmp/x", "last.txt"), *W.CODEX_TOOLS_OFF, "-c", "mcp_servers={}",
                    "-m", "gpt-5", "-"]
    assert {"shell_tool", "unified_exec", "code_mode_host"} <= set(W.CODEX_TOOLS_OFF[1::2])  # the file-read paths
    assert stdin == PROMPT


def test_build_argv_agy_uses_print_equals_form():
    argv, stdin = build_argv("agy", {"model": "gemini-3.8-flash-high"}, wu(timeout_s=240), "/tmp/x")
    assert argv == ["agy", "--print=" + PROMPT, "--model", "gemini-3.8-flash-high", "--mode", "plan",
                    "--sandbox", "--print-timeout", "240s"]
    assert stdin is None


def test_build_argv_opencode():
    argv, stdin = build_argv("opencode", {"model": "openai/gpt-5"}, wu(), "/tmp/x")
    assert argv == ["opencode", "run", "--agent", "plan", "-m", "openai/gpt-5", PROMPT]
    assert stdin is None


def test_build_argv_opencode_leading_dash_guard():
    argv, _ = build_argv("opencode", {"model": "m"}, wu(prompt="--help me"), "/tmp/x")
    assert argv[-1] == " --help me"


@pytest.mark.parametrize("atype,flag", [("claude", "--model"), ("codex", "-m"), ("agy", "--model"),
                                        ("opencode", "-m")])
def test_build_argv_no_model_flag_without_model(atype, flag):
    argv, _ = build_argv(atype, {}, wu(), "/tmp/x")
    assert flag not in argv


def test_build_argv_override_fills_placeholders_and_keeps_stdin():
    acfg = {"model": "m-1", "argv": ["my-claude", "--model={model}", "--dir", "{tmp}", "--t", "{timeout_s}"]}
    argv, stdin = build_argv("claude", acfg, wu(timeout_s=240), "/tmp/x")
    assert argv == ["my-claude", "--model=m-1", "--dir", "/tmp/x", "--t", "240"]
    assert stdin == PROMPT


def test_build_argv_override_prompt_in_argv_is_not_filled_twice():
    acfg = {"model": "m-1", "argv": ["agy", "--print={prompt}", "--model", "{model}"]}
    argv, stdin = build_argv("agy", acfg, wu(prompt="say {tmp} and {model}"), "/tmp/x")
    assert argv == ["agy", "--print=say {tmp} and {model}", "--model", "m-1"]
    assert stdin is None


def test_build_argv_override_keeps_opencode_dash_guard():
    argv, _ = build_argv("opencode", {"model": "m", "argv": ["oc", "{prompt}"]}, wu(prompt="-x"), "/tmp/x")
    assert argv == ["oc", " -x"]


def test_run_adapter_runs_override_argv(shim):
    shim["script"](stdout='{"kind": "a"}')
    acfg = {"model": "m-1", "argv": ["ignored-by-shim", "--alt", "{model}", "{timeout_s}"]}
    assert run_adapter("claude", acfg, wu(), runner=shim["runner"])[0] == "ok"
    assert shim["seen"]()["argv"] == ["--alt", "m-1", "30"]
    assert shim["seen"]()["stdin"] == PROMPT


@pytest.mark.parametrize("bad", ["claude -p", [], ["claude", 3]])
def test_load_config_rejects_bad_argv(tmp_path, bad):
    path = tmp_path / "worker.json"
    path.write_text(json.dumps({"server": "s", "token": "t", "adapters": {"claude": {"argv": bad}}}))
    with pytest.raises(ValueError):
        W.load_config(path)


# --- run_adapter through a real subprocess ------------------------------------------------------

@pytest.mark.parametrize("atype", ["claude", "codex", "agy", "opencode"])
def test_run_adapter_ok_and_prompt_arrives(shim, atype):
    out = json.dumps({"kind": "a"})
    if atype == "codex":
        shim["script"](stdout="codex chatter, not the answer", file=out)
    else:
        shim["script"](stdout="Answer:\n" + out)
    status, text, usage = run_adapter(atype, {"model": "m-1", "family": "x"}, wu(), runner=shim["runner"])
    assert status == "ok"
    assert extract_json(text) == {"kind": "a"}
    assert isinstance(usage["seconds"], float) and "error" not in usage
    seen = shim["seen"]()
    assert seen["cwd"] != os.getcwd()  # a fresh temp dir
    assert [f for f in seen["cwd_files"] if f != "last.txt"] == []
    if atype in ("claude", "codex"):
        assert seen["stdin"] == PROMPT
    else:
        assert shim["inputs"] == [""]  # stdin closed at once, never the terminal
        assert seen["stdin"] == "" and any(PROMPT in a for a in seen["argv"])
    tmp = seen["argv"][seen["argv"].index("-C") + 1] if atype == "codex" else seen["cwd"]
    assert os.path.realpath(tmp) == os.path.realpath(seen["cwd"])
    assert seen["argv"] == build_argv(atype, {"model": "m-1"}, wu(), tmp)[0][1:]


def test_run_adapter_codex_reads_o_file_not_stdout(shim):
    shim["script"](stdout='{"kind": "from-stdout"}', file='{"kind": "from-file"}')
    status, text, _ = run_adapter("codex", {}, wu(), runner=shim["runner"])
    assert status == "ok" and extract_json(text) == {"kind": "from-file"}
    seen = shim["seen"]()
    out_file = seen["argv"][seen["argv"].index("-o") + 1]
    assert os.path.realpath(out_file) == os.path.join(os.path.realpath(seen["cwd"]), "last.txt")


@pytest.mark.parametrize("atype,where,msg", [
    ("claude", "stdout", "Claude usage limit reached. Your limit will reset at 8pm."),
    ("codex", "stderr", "You've hit your usage limit. Try again in 3 hours."),
    ("agy", "stderr", "Individual quota reached"),
    ("opencode", "stdout", "Go usage limit exceeded"),
])
def test_run_adapter_rate_limit_samples(shim, atype, where, msg):
    shim["script"](rc=1, **{where: msg})
    status, _, _ = run_adapter(atype, {"family": "x"}, wu(), runner=shim["runner"])
    assert status == "rate_limited"


def test_run_adapter_rate_limit_regex_is_per_type(shim):
    shim["script"](rc=1, stderr="Individual quota reached")  # an agy message, not a claude one
    assert run_adapter("claude", {}, wu(), runner=shim["runner"])[0] == "error"


def test_run_adapter_json_wins_over_limit_text(shim):
    shim["script"](stdout='{"kind": "rate limit"}')
    assert run_adapter("claude", {}, wu(), runner=shim["runner"])[0] == "ok"


def test_run_adapter_error_shows_stdout_when_stderr_is_empty(shim):
    shim["script"](stdout="Failed to authenticate: OAuth session expired", rc=1)
    status, _, usage = run_adapter("claude", {}, wu(), runner=shim["runner"])
    assert status == "error" and usage["error"] == "exit 1: Failed to authenticate: OAuth session expired"


def test_run_adapter_nonzero_exit_is_error(shim):
    shim["script"](rc=3, stdout='{"kind": "a"}', stderr="boom")
    status, _, usage = run_adapter("claude", {}, wu(), runner=shim["runner"])
    assert status == "error" and "3" in usage["error"]


def test_run_adapter_no_json_is_error(shim):
    shim["script"](stdout="I cannot help with that.")
    assert run_adapter("claude", {}, wu(), runner=shim["runner"])[0] == "error"


def test_run_adapter_timeout_is_error(shim):
    shim["script"](sleep=5, stdout='{"kind": "a"}')
    status, _, usage = run_adapter("claude", {}, wu(timeout_s=0.15), runner=shim["runner"])
    assert status == "error" and "Timeout" in usage["error"]


def test_run_adapter_missing_binary_is_error(tmp_path):
    def runner(argv, **kw):
        return subprocess.run([str(tmp_path / "no-such-cli"), *argv[1:]], **kw)
    assert run_adapter("claude", {}, wu(), runner=runner)[0] == "error"


def test_run_adapter_big_prompt_is_error_without_running():
    calls = []
    status, _, _ = run_adapter("claude", {}, wu(prompt="x" * 200_001), runner=lambda *a, **k: calls.append(a))
    assert status == "error" and calls == []
    for atype in ("agy", "opencode"):  # an argv prompt has a lower cap
        assert run_adapter(atype, {}, wu(prompt="x" * 100_001), runner=lambda *a, **k: calls.append(a))[0] == "error"
    assert calls == []


def test_run_adapter_codex_without_o_file_is_error(shim):
    shim["script"](stdout='{"kind": "echoed from the prompt"}')  # stdout echoes the prompt: never an answer
    assert run_adapter("codex", {}, wu(), runner=shim["runner"])[0] == "error"


# --- fake adapter -------------------------------------------------------------------------------

def test_fake_matches_substring_and_counts_limit():
    acfg = {"type": "fake", "family": "openai", "limit_after": 2,
            "answers": {"zebra": {"kind": "z"}, "Read": "prose then {\"kind\": \"r\"}"}}
    state = {}
    first = run_adapter("fk", acfg, wu(), fake_state=state)
    assert first[0] == "ok" and extract_json(first[1]) == {"kind": "r"}
    assert run_adapter("fk", acfg, wu(prompt="a zebra"), fake_state=state)[0] == "ok"
    assert run_adapter("fk", acfg, wu(), fake_state=state)[0] == "rate_limited"


def test_fake_no_match_or_text_is_error():
    acfg = {"type": "fake", "family": "openai", "answers": {"zebra": "no json at all"}}
    assert run_adapter("fk", acfg, wu())[0] == "error"
    assert run_adapter("fk", acfg, wu(prompt="zebra"))[0] == "error"


def test_fake_answers_from_file(tmp_path):
    path = tmp_path / "answers.json"
    path.write_text(json.dumps({"Read": {"kind": "file"}}))
    status, text, _ = run_adapter("fk", {"type": "fake", "family": "x", "answers": str(path)}, wu())
    assert status == "ok" and extract_json(text) == {"kind": "file"}


# --- family_of and offers -----------------------------------------------------------------------

@pytest.mark.parametrize("acfg,name,family", [
    ({"family": "qwen", "model": "claude-opus"}, "claude", "qwen"),
    ({"model": "claude-sonnet-5"}, "agy", "anthropic"),
    ({"model": "gemini-3.8-flash-high"}, "agy", "google"),
    ({"model": "openai/gpt-5"}, "opencode", "openai"),
    ({"model": "o3-mini"}, "codex", "openai"),
    ({"model": "o4-mini"}, "opencode", "openai"),
    ({"model": "deepseek/deepseek-v4"}, "opencode", "deepseek"),
    ({"model": "Qwen3:32b"}, "opencode", "qwen"),
    ({"model": "llama3.1"}, "opencode", "llama"),
    ({"model": "sonnet"}, "claude", "anthropic"),
    ({}, "codex", "openai"),
    ({"type": "claude"}, "work-claude", "anthropic"),
])
def test_family_of(acfg, name, family):
    assert family_of(acfg, name) == family


@pytest.mark.parametrize("acfg,name", [({}, "agy"), ({"model": "mystery-1"}, "opencode"),
                                       ({"type": "opencode"}, "oc")])
def test_family_of_unknown_raises(acfg, name):
    with pytest.raises(ValueError):
        family_of(acfg, name)


def test_load_config_rejects_adapter_without_family(tmp_path):
    path = tmp_path / "worker.json"
    path.write_text(json.dumps({"server": "s", "token": "t", "adapters": {"agy": {}}}))
    with pytest.raises(ValueError):
        W.load_config(path)


def test_load_config_rejects_unknown_type(tmp_path):
    path = tmp_path / "worker.json"
    path.write_text(json.dumps({"server": "s", "token": "t", "adapters": {"x": {"type": "ollama", "family": "q"}}}))
    with pytest.raises(ValueError):
        W.load_config(path)


def test_offers_shape_and_filter():
    cfg = {"adapters": {"claude": {"model": "sonnet"}, "agy": {"model": "gemini-3"}}}
    assert offers(cfg) == [
        {"adapter": "claude", "kind": "llm", "family": "anthropic", "model": "sonnet", "modes": ["json"]},
        {"adapter": "agy", "kind": "llm", "family": "google", "model": "gemini-3", "modes": ["json"]},
    ]
    assert [o["adapter"] for o in offers(cfg, ["agy"])] == ["agy"]


# --- Worker.step against a stub client ----------------------------------------------------------

class StubClient:
    """Plays scripted lease replies; records every request body."""

    def __init__(self, leases=(), submit=None, release=None, projects=None):
        self.leases, self.submit, self.release, self.calls = list(leases), submit, release, []
        self.projects = [{"id": "tst-p1", "manifest_hash": "sha256:aa"}] if projects is None else projects

    def get(self, path):
        self.calls.append(("GET", path, None))
        if isinstance(self.projects, BaseException):
            raise self.projects
        return self.projects

    def post(self, path, body):
        self.calls.append(("POST", path, copy.deepcopy(body)))
        if path == "/v1/lease":
            item = self.leases.pop(0) if self.leases else None
        else:
            item = getattr(self, path.rsplit("/", 1)[1]) or (
                {"status": "accepted", "points": 1} if path.endswith("/submit") else {"ok": True})
        if isinstance(item, BaseException):
            raise item
        return item

    def paths(self):
        return [p.split("/v1/")[1] for _, p, _ in self.calls]

    def bodies(self, kind):
        return [b for _, p, b in self.calls if p.endswith(kind)]


def lease(n=1, adapter="fk", **kw):
    return {"lease_id": f"L{n}", "expires_at": 0, "heartbeat_s": 100, "adapter": adapter, "wu": wu(**kw)}


def cfg_with(**adapters):
    return {"server": "http://127.0.0.1:9", "token": "tok", "projects": {"tst-p1": "sha256:aa"},
            "adapters": adapters or {"fk": {"type": "fake", "family": "openai", "answers": {"": {"kind": "a"}}}}}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_worker(client, cfg=None, **kw):
    sleeps, logs = [], []
    w = Worker(cfg or cfg_with(), client, sleep=sleeps.append, log=logs.append, **kw)
    return w, sleeps, logs


def test_step_submits_valid_output_with_usage():
    client = StubClient([lease()])
    w, _, _ = make_worker(client)
    assert w.step() is True
    lease_body = client.bodies("lease")[0]
    assert lease_body["projects"] == {"tst-p1": "sha256:aa"}
    assert lease_body["offers"] == [{"adapter": "fk", "kind": "llm", "family": "openai", "model": None,
                                     "modes": ["json"]}]
    assert client.paths() == ["lease", "leases/L1/submit"]
    sub = client.bodies("submit")[0]
    assert sub["output"] == {"kind": "a"} and isinstance(sub["usage"]["seconds"], float)


def test_step_204_backs_off_doubling_to_300_and_resets():
    client = StubClient([None] * 6 + [lease()] + [None])
    w, sleeps, _ = make_worker(client)
    for _ in range(8):
        assert w.step() is True
    assert sleeps == [30, 60, 120, 240, 300, 300, 30]


def test_step_lease_403_stops():
    w, _, logs = make_worker(StubClient([ApiError(403, {"error": "not_active"})]))
    assert w.step() is False and logs


def test_step_manifest_drift_releases_and_asks_to_join():
    client = StubClient([lease(mhash="sha256:new")])
    w, _, logs = make_worker(client)
    assert w.step() is True
    assert client.paths() == ["lease", "leases/L1/release"]
    assert client.bodies("release") == [{"reason": "manifest_changed"}]
    assert any("rally-worker join tst-p1" in str(line) for line in logs)
    assert w.state["fk"]["calls"] == 0  # the adapter never ran


def test_step_schema_fail_releases_error_and_no_submit():
    cfg = cfg_with(fk={"type": "fake", "family": "openai", "answers": {"": {"other": 1}}})
    client = StubClient([lease()])
    w, _, _ = make_worker(client, cfg)
    assert w.step() is True
    assert client.paths() == ["lease", "leases/L1/release"]
    assert client.bodies("release") == [{"reason": "error"}]


def test_step_rate_limit_releases_and_blocks_adapter_on_injected_clock():
    cfg = cfg_with(fk={"type": "fake", "family": "openai", "limit_after": 1, "answers": {"": {"kind": "a"}}})
    client = StubClient([lease(), lease(2), lease(3)])
    clock = Clock()
    w, _, _ = make_worker(client, cfg, now=clock)
    assert w.step() is True  # call 1 submits
    assert w.step() is True  # call 2 is over limit_after
    assert client.bodies("release") == [{"reason": "rate_limited"}]
    assert w.state["fk"]["blocked_until"] == 1000.0 + 3600
    clock.t += 3599
    assert w.step() is False  # still blocked: no lease request
    assert client.paths() == ["lease", "leases/L1/submit", "lease", "leases/L2/release"]
    clock.t += 1
    assert w.step() is True  # blocked_until <= now: usable again
    assert client.paths()[-2] == "lease"


def test_step_three_errors_in_a_row_disable_adapter():
    cfg = cfg_with(fk={"type": "fake", "family": "openai", "answers": {"": "garbage"}})
    client = StubClient([lease(i) for i in range(1, 6)])
    w, _, _ = make_worker(client, cfg)
    assert [w.step() for _ in range(4)] == [True, True, True, False]
    assert client.bodies("release") == [{"reason": "error"}] * 3


def test_step_success_resets_error_count():
    answers = iter(["bad", "bad", {"kind": "a"}, "bad", "bad", "bad"])
    cfg = cfg_with(fk={"type": "fake", "family": "openai", "answers": {"": None}})
    client = StubClient([lease(i) for i in range(1, 7)])
    w, _, _ = make_worker(client, cfg)
    steps = []
    for _ in range(6):
        cfg["adapters"]["fk"]["answers"][""] = next(answers)
        steps.append(w.step())
    assert steps == [True] * 6 and w.step() is False
    assert len(client.bodies("submit")) == 1 and len(client.bodies("release")) == 5


def test_step_submit_409_is_dropped_without_retry():
    client = StubClient([lease()], submit=ApiError(409, {"error": "lease_expired"}))
    w, _, _ = make_worker(client)
    assert w.step() is True
    assert client.paths() == ["lease", "leases/L1/submit"]


def test_step_refused_submits_count_as_errors_and_disable_the_adapter():
    client = StubClient([lease(i) for i in range(1, 5)], submit=ApiError(422, {"status": "rejected", "reason": "x"}))
    w, _, _ = make_worker(client)
    assert [w.step() for _ in range(4)] == [True, True, True, False]
    assert len(client.bodies("submit")) == 3 and client.bodies("release") == []


def test_step_lease_401_is_raised_not_a_quiet_stop():
    w, _, _ = make_worker(StubClient([ApiError(401, {"error": "unauthorized"})]))
    with pytest.raises(ApiError):
        w.step()


def test_client_follows_no_redirect(api):
    api["routes"][("GET", "/v1/me")] = (302, None, {"Location": api["url"] + "/v1/elsewhere"})
    with pytest.raises(ApiError) as e:
        Client(api["url"], "tok").get("/v1/me")
    assert e.value.code == 302 and [r["path"] for r in api["seen"]] == ["/v1/me"]  # the token went nowhere else


def test_step_release_409_is_dropped_without_retry():
    cfg = cfg_with(fk={"type": "fake", "family": "openai", "answers": {"": "garbage"}})
    client = StubClient([lease()], release=ApiError(409, {"error": "lease_not_active"}))
    w, _, _ = make_worker(client, cfg)
    assert w.step() is True
    assert client.paths() == ["lease", "leases/L1/release"]


def test_step_server_error_on_submit_releases_error():
    client = StubClient([lease()], submit=ApiError(500, {"error": "internal"}))
    w, _, _ = make_worker(client)
    assert w.step() is True
    assert client.paths() == ["lease", "leases/L1/submit", "leases/L1/release"]
    assert client.bodies("release") == [{"reason": "error"}]
    assert w.state["fk"]["errors"] == 1 and w.state["fk"]["done"] == 0


def test_step_any_exception_in_job_releases_error_and_three_disable(tmp_path):
    cfg = cfg_with(fk={"type": "fake", "family": "openai", "answers": str(tmp_path / "missing.json")})
    client = StubClient([lease(i) for i in range(1, 5)])
    w, _, logs = make_worker(client, cfg)
    assert [w.step() for _ in range(4)] == [True, True, True, False]
    assert client.bodies("release") == [{"reason": "error"}] * 3
    assert w.state["fk"]["disabled"] and any("FileNotFoundError" in str(x) for x in logs)


def test_step_runner_bug_releases_error():
    def runner(argv, **kw):
        raise RuntimeError("adapter bug")
    client = StubClient([lease(adapter="c")])
    w, _, _ = make_worker(client, cfg_with(c={"type": "claude"}), runner=runner)
    assert w.step() is True
    assert client.bodies("release") == [{"reason": "error"}] and w.state["c"]["errors"] == 1


def test_step_global_max_tasks():
    client = StubClient([lease(i) for i in range(1, 5)])
    w, _, _ = make_worker(client, max_tasks=2)
    assert [w.step() for _ in range(3)] == [True, True, False]
    assert len(client.bodies("submit")) == 2


def test_step_adapter_max_tasks_drops_only_that_adapter():
    answers = {"": {"kind": "a"}}
    cfg = cfg_with(a={"type": "fake", "family": "openai", "answers": answers, "max_tasks": 1},
                   b={"type": "fake", "family": "google", "answers": answers})
    client = StubClient([lease(1, adapter="a"), lease(2, adapter="b")])
    w, _, _ = make_worker(client, cfg)
    assert w.step() and w.step()
    assert [o["adapter"] for o in client.bodies("lease")[1]["offers"]] == ["b"]


def test_step_names_limit_offers():
    answers = {"": {"kind": "a"}}
    cfg = cfg_with(a={"type": "fake", "family": "openai", "answers": answers},
                   b={"type": "fake", "family": "google", "answers": answers})
    client = StubClient([lease(1, adapter="b")])
    w, _, _ = make_worker(client, cfg, names=["b"])
    w.step()
    assert [o["adapter"] for o in client.bodies("lease")[0]["offers"]] == ["b"]
    with pytest.raises(ValueError):
        Worker(cfg, client, names=["nope"])


def test_step_lease_for_unoffered_adapter_is_released():
    client = StubClient([lease(adapter="ghost")])
    w, _, _ = make_worker(client)
    assert w.step() is True
    assert client.bodies("release") == [{"reason": "error"}]


def test_step_keyboard_interrupt_releases_stop_and_run_exits():
    def runner(argv, **kw):
        raise KeyboardInterrupt
    cfg = cfg_with(c={"type": "claude"})
    client = StubClient([lease(adapter="c"), lease(2, adapter="c")])
    w, _, _ = make_worker(client, cfg, runner=runner)
    with pytest.raises(KeyboardInterrupt):
        w.step()
    assert client.bodies("release") == [{"reason": "stop"}]
    w.run()  # catches it and returns
    assert client.bodies("release") == [{"reason": "stop"}] * 2


def test_run_once_does_one_step():
    client = StubClient([lease(), lease(2)])
    w, _, _ = make_worker(client)
    w.run(once=True)
    assert client.paths() == ["projects", "lease", "leases/L1/submit"]


def test_run_start_logs_drift_for_missing_and_changed_projects():
    cfg = cfg_with()
    cfg["projects"] = {"tst-p1": "sha256:aa", "tst-p2": "sha256:old", "tst-p3": "sha256:cc"}
    client = StubClient([None], projects=[{"id": "tst-p1", "manifest_hash": "sha256:aa"},
                                          {"id": "tst-p2", "manifest_hash": "sha256:new"}])
    w, _, logs = make_worker(client, cfg)
    w.run(once=True)
    assert client.paths() == ["projects", "lease"]
    drift = [str(x) for x in logs if "again" in str(x)]
    assert len(drift) == 2
    assert any("rally-worker join tst-p2" in x for x in drift) and any("rally-worker join tst-p3" in x for x in drift)


def test_drift_check_after_ten_204_in_a_row():
    client = StubClient([None] * 9 + [lease()] + [None] * 10, projects=[])
    w, _, logs = make_worker(client)
    for _ in range(10):
        w.step()
    assert "projects" not in client.paths()  # the lease after nine 204 resets the count
    for _ in range(9):
        w.step()
    assert "projects" not in client.paths()
    w.step()
    assert client.paths().count("projects") == 1
    assert any("rally-worker join tst-p1" in str(x) for x in logs)


def test_drift_check_failure_only_logs():
    client = StubClient([None], projects=ApiError(500, {"error": "internal"}))
    w, _, logs = make_worker(client)
    w.run(once=True)
    assert client.paths() == ["projects", "lease"] and logs


def test_sentinel_env_reaches_cli_but_never_the_server(shim, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    shim["script"](stdout='{"kind": "a"}', stderr="debug noise")
    cfg = cfg_with(claude={"model": "sonnet"})
    client = StubClient([lease(adapter="claude")])
    w, _, logs = make_worker(client, cfg, runner=shim["runner"])
    assert w.step() is True
    assert shim["seen"]()["key"] == SENTINEL
    assert client.paths() == ["lease", "leases/L1/submit"]
    assert "SENTINEL" not in json.dumps(client.calls)
    assert "SENTINEL" not in json.dumps([str(x) for x in logs])


# --- config -------------------------------------------------------------------------------------

def test_save_config_mode_600_even_over_existing_file(tmp_path):
    path = tmp_path / "home" / "worker.json"
    path.parent.mkdir()
    path.write_text("{}")
    os.chmod(path, 0o644)
    stale = path.with_name("worker.json.tmp")  # left by a crash in a past write
    stale.write_text("")
    os.chmod(stale, 0o644)
    W.save_config({"server": "s", "token": "t", "projects": {}, "adapters": {}}, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert W.load_config(path)["token"] == "t"


# --- CLI ----------------------------------------------------------------------------------------

@pytest.fixture
def api():
    """A local HTTP server: routes {(method, path): (code, body)}; records each request."""
    routes, seen = {}, []

    class H(BaseHTTPRequestHandler):
        def _go(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                         "body": json.loads(raw) if raw else None})
            code, body, *headers = routes.get((self.command, self.path), (404, {"error": "not_found"}))
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(code)
            for k, v in (headers[0] if headers else {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _go

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{srv.server_address[1]}", "routes": routes, "seen": seen}
    srv.shutdown()
    srv.server_close()


def test_client_json_auth_204_and_errors(api):
    api["routes"].update({("GET", "/v1/me"): (200, {"name": "n"}), ("POST", "/v1/lease"): (204, None),
                          ("POST", "/v1/leases/L1/submit"): (422, {"status": "rejected", "reason": "size"}),
                          ("POST", "/v1/leases/L1/release"): (409, {"error": "lease_not_active"})})
    c = Client(api["url"] + "/", "tok")
    assert c.get("/v1/me") == {"name": "n"}
    assert c.post("/v1/lease", {"projects": {}}) is None
    with pytest.raises(ApiError) as e:
        c.post("/v1/leases/L1/submit", {"output": {}})
    assert e.value.code == 422 and e.value.body == {"status": "rejected", "reason": "size"}
    with pytest.raises(ApiError) as e:
        c.post("/v1/leases/L1/release", {"reason": "error"})
    assert e.value.code == 409 and e.value.body == {"error": "lease_not_active"}
    assert {r["auth"] for r in api["seen"]} == {"Bearer tok"}
    assert api["seen"][1]["body"] == {"projects": {}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("RALLY_HOME", str(tmp_path / "rh"))
    return tmp_path / "rh" / "worker.json"


def test_cli_init_with_invite_registers_and_saves_600(api, home, capsys):
    api["routes"][("POST", "/v1/participants")] = (200, {"id": 7, "token": "secret-tok"})
    assert W.main(["init", "--server", api["url"], "--name", "tst-zed", "--invite", "code1"]) == 0
    assert api["seen"][0]["body"] == {"name": "tst-zed", "invite": "code1"} and api["seen"][0]["auth"] is None
    cfg = json.loads(home.read_text())
    assert cfg["server"] == api["url"] and cfg["token"] == "secret-tok"
    assert stat.S_IMODE(home.stat().st_mode) == 0o600
    assert "secret-tok" not in capsys.readouterr().out


def test_cli_init_with_token_keeps_adapters(home):
    W.save_config({"server": "old", "token": "o", "projects": {}, "adapters": {"claude": {}}}, home)
    assert W.main(["init", "--server", "http://h", "--token", "T2"]) == 0
    cfg = W.load_config(home)
    assert (cfg["server"], cfg["token"], cfg["adapters"]) == ("http://h", "T2", {"claude": {}})


def test_cli_join_shows_manifest_pins_hash_and_leave(api, home, capsys, monkeypatch):
    W.save_config({"server": api["url"], "token": "tok", "projects": {}, "adapters": {}}, home)
    api["routes"][("GET", "/v1/projects/tst-p1")] = (200, {
        "id": "tst-p1", "title": "Demo title", "track": "brain", "manifest_hash": "sha256:bb",
        "stages": {"brain": {"mode": "json", "prompt": "Extract ${text}", "needs": {"families": ["google"]}}},
        "verify": {"mode": "quorum"}})
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert W.main(["join", "tst-p1"]) == 1
    assert W.load_config(home)["projects"] == {}
    assert W.main(["join", "tst-p1", "--yes"]) == 0
    out = capsys.readouterr().out
    for word in ("Demo title", "brain", "json", "google", "Extract ${text}", "quorum"):
        assert word in out
    assert W.load_config(home)["projects"] == {"tst-p1": "sha256:bb"}
    assert W.main(["leave", "tst-p1"]) == 0
    assert W.load_config(home)["projects"] == {}


def test_cli_status_prints_me(api, home, capsys):
    W.save_config({"server": api["url"], "token": "tok", "projects": {}, "adapters": {}}, home)
    api["routes"][("GET", "/v1/me")] = (200, {"name": "tst-zed", "points": 4})
    assert W.main(["status"]) == 0
    assert '"points": 4' in capsys.readouterr().out


def test_cli_run_once_with_fake_adapter(api, home):
    W.save_config({"server": api["url"], "token": "tok", "projects": {"tst-p1": "sha256:aa"},
                   "adapters": {"fk": {"type": "fake", "family": "openai", "answers": {"": {"kind": "a"}}}}}, home)
    api["routes"][("GET", "/v1/projects")] = (200, [{"id": "tst-p1", "manifest_hash": "sha256:aa"}])
    api["routes"][("POST", "/v1/lease")] = (200, lease())
    api["routes"][("POST", "/v1/leases/L1/submit")] = (200, {"status": "accepted", "points": 1})
    assert W.main(["run", "--once"]) == 0
    assert [r["path"] for r in api["seen"]] == ["/v1/projects", "/v1/lease", "/v1/leases/L1/submit"]
    assert api["seen"][2]["body"]["output"] == {"kind": "a"}


def test_cli_without_config_fails_cleanly(home, capsys):
    assert W.main(["status"]) == 1
    assert "init" in capsys.readouterr().err


def test_doctor_only_passes_version(tmp_path, monkeypatch, capsys):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("claude", "codex", "agy", "opencode"):
        p = bindir / name
        p.write_text(f"#!{sys.executable}\nimport json, sys\n"
                     f"open({str(tmp_path / (name + '.argv'))!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                     f"print('{name} 9.9.9')\n")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert W.main(["doctor"]) == 0
    for name in ("claude", "codex", "agy", "opencode"):
        lines = (tmp_path / (name + ".argv")).read_text().splitlines()
        assert [json.loads(x) for x in lines] == [["--version"]]
    out = capsys.readouterr().out
    assert "claude 9.9.9" in out and '"adapters"' in out
