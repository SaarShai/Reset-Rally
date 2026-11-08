"""Participant worker: lease a Work Unit, run a local AI CLI on it, submit or release."""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from rally.checks import check_schema

MAX_PROMPT = 200_000
MAX_ARGV_PROMPT = 100_000  # Linux limits one argv string to 128 KiB
BLOCK_S = 3600
RATE = {  # rate-limit text per adapter type, matched on stdout + stderr
    "claude": re.compile(r"usage limit|limit reached|rate.?limit|5-hour limit|weekly limit", re.I),
    "codex": re.compile(r"usage limit|rate.?limit|429|try again in", re.I),
    "agy": re.compile(r"quota reached|resource_exhausted|rate.?limit|429", re.I),
    "opencode": re.compile(r"usage limit|rate.?limit|429|quota", re.I),
}
TYPES = {*RATE, "fake"}
MODEL_FAMILIES = [("claude", "anthropic"), ("gemini", "google"), ("gpt", "openai"), ("o3", "openai"),
                  ("o4", "openai"), ("deepseek", "deepseek"), ("qwen", "qwen"), ("llama", "llama")]
TYPE_FAMILIES = {"claude": "anthropic", "codex": "openai"}
STDIN_TYPES = {"claude", "codex"}
# codex --sandbox read-only still lets the model read any file through these tools (live probe, codex-cli 0.159)
CODEX_TOOLS_OFF = [x for f in ("shell_tool", "unified_exec", "code_mode_host", "apps", "plugins", "browser_use",
                               "computer_use", "multi_agent", "view_image", "image_generation") for x in ("--disable", f)]
PLACEHOLDER = re.compile(r"\{(prompt|model|tmp|timeout_s)\}")


class ApiError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body}")
        self.code, self.body = code, body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None  # a 3xx becomes an HTTPError: the token never goes to another URL


class Client:
    opener = urllib.request.build_opener(_NoRedirect)

    def __init__(self, server, token=None):
        self.server, self.token = server.rstrip("/"), token

    def _call(self, method, path, body=None):
        data = None if body is None else json.dumps(body, allow_nan=False).encode()  # inf or nan is no JSON
        req = urllib.request.Request(self.server + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self.opener.open(req, timeout=30) as r:
                code, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = {"error": raw[:200].decode(errors="replace")}
        if not 200 <= code < 300:
            raise ApiError(code, parsed)
        return parsed

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body):
        return self._call("POST", path, body)


def family_of(acfg: dict, name: str) -> str:
    if acfg.get("family"):
        return acfg["family"]
    for token in re.split(r"[^a-z0-9]+", str(acfg.get("model") or "").lower()):
        for prefix, family in MODEL_FAMILIES:
            if token.startswith(prefix):
                return family
    atype = acfg.get("type", name)
    if atype in TYPE_FAMILIES:
        return TYPE_FAMILIES[atype]
    raise ValueError(f"adapters.{name}: set 'family' or a 'model' whose family is known")


def offers(cfg, names=None) -> list[dict]:
    return [{"adapter": n, "kind": "llm", "family": family_of(a, n), "model": a.get("model"), "modes": ["json"]}
            for n, a in cfg["adapters"].items() if names is None or n in names]


def build_argv(atype, acfg, wu, tmpdir) -> tuple[list[str], str | None]:
    prompt, model = wu["prompt"], acfg.get("model")
    if acfg.get("argv"):  # one pass, so braces inside the prompt are never filled
        values = {"prompt": " " + prompt if atype == "opencode" and prompt.startswith("-") else prompt,
                  "model": model or "", "tmp": str(tmpdir), "timeout_s": str(wu["timeout_s"])}
        return ([PLACEHOLDER.sub(lambda m: values[m[1]], a) for a in acfg["argv"]],
                prompt if atype in STDIN_TYPES else None)
    if atype == "claude":
        return ["claude", "-p", *(["--model", model] if model else []), "--tools", "", "--strict-mcp-config",
                "--no-session-persistence", "--output-format", "text"], prompt
    if atype == "codex":
        return ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
                "-C", str(tmpdir), "-o", os.path.join(tmpdir, "last.txt"), *CODEX_TOOLS_OFF, "-c", "mcp_servers={}",
                *(["-m", model] if model else []), "-"], prompt
    if atype == "agy":
        return ["agy", f"--print={prompt}", *(["--model", model] if model else []), "--mode", "plan",
                "--sandbox", "--print-timeout", f"{wu['timeout_s']}s"], None
    if atype == "opencode":  # a leading "-" would parse as a flag
        return ["opencode", "run", "--agent", "plan", *(["-m", model] if model else []),
                " " + prompt if prompt.startswith("-") else prompt], None
    raise ValueError(f"unknown adapter type {atype!r}")


def _fake(acfg, wu, state):
    state["calls"] = state.get("calls", 0) + 1
    if acfg.get("limit_after") is not None and state["calls"] > acfg["limit_after"]:
        return "rate_limited", "", None
    answers = acfg.get("answers") or {}
    if isinstance(answers, str):
        answers = json.loads(Path(answers).expanduser().read_text())
    for key, answer in answers.items():
        if key in wu["prompt"]:
            return None, answer if isinstance(answer, str) else json.dumps(answer), None
    return "error", "", "no fake answer matches"


def run_adapter(name, acfg, wu, runner=subprocess.run, fake_state=None) -> tuple[str, str, dict]:
    """Run one job. Returns (status, text, usage); status is ok | rate_limited | error."""
    atype, start = acfg.get("type", name), time.monotonic()

    def done(status, text="", error=None):
        usage = {"seconds": round(time.monotonic() - start, 3)}
        if error:
            usage["error"] = error[:200]
        return status, text, usage

    if len(wu["prompt"].encode()) > (MAX_PROMPT if atype in STDIN_TYPES or atype == "fake" else MAX_ARGV_PROMPT):
        return done("error", error="prompt too large")
    if atype == "fake":
        status, text, error = _fake(acfg, wu, {} if fake_state is None else fake_state)
        if status:
            return done(status, text, error)
        return done("ok", text) if extract_json(text) is not None else done("error", text, "no JSON object")
    with tempfile.TemporaryDirectory(prefix="rally-job-") as tmp:
        argv, stdin = build_argv(atype, acfg, wu, tmp)
        try:  # input="" gives an argv-prompt CLI an empty stdin, so it never waits on the terminal
            p = runner(argv, input=stdin or "", cwd=tmp, capture_output=True, text=True, timeout=wu["timeout_s"])
        except (subprocess.TimeoutExpired, OSError) as e:
            return done("error", error=type(e).__name__)
        out, err = p.stdout or "", p.stderr or ""
        text = out
        if atype == "codex":
            last = Path(tmp, "last.txt")
            text = last.read_text(errors="replace") if last.exists() else ""
    if p.returncode == 0 and extract_json(text) is not None:
        return done("ok", text)
    if RATE[atype].search(out + err):
        return done("rate_limited", text)
    tail = (err.strip() or out.strip()).splitlines()[-1:] or [""]  # claude prints a login failure on stdout
    return done("error", text, f"exit {p.returncode}: {tail[0][:120]}")


def extract_json(text) -> dict | None:
    """The last top-level JSON object in text, or None."""
    if not isinstance(text, str):
        return None
    dec, last, i = json.JSONDecoder(), None, text.find("{")
    while i != -1:
        try:
            last, end = dec.raw_decode(text, i)  # at "{" a success is always a dict
        except (ValueError, RecursionError):
            end = i + 1
        i = text.find("{", end)
    return last


class Worker:
    def __init__(self, cfg, client, names=None, max_tasks=None, runner=subprocess.run, sleep=time.sleep, log=print,
                 now=time.time):
        self.cfg, self.client, self.runner, self.sleep, self.log, self.now = cfg, client, runner, sleep, log, now
        self.names = list(names or cfg["adapters"])
        if unknown := [n for n in self.names if n not in cfg["adapters"]]:
            raise ValueError(f"unknown adapter {unknown[0]!r}")
        self.max_tasks, self.done, self.backoff, self.idle = max_tasks, 0, 30, 0
        self.state = {n: {"done": 0, "blocked_until": 0.0, "errors": 0, "disabled": False, "calls": 0}
                      for n in self.names}

    def _usable(self, name, now):
        st, limit = self.state[name], self.cfg["adapters"][name].get("max_tasks")
        return (not st["disabled"] and st["blocked_until"] <= now and (limit is None or st["done"] < limit)
                and (self.max_tasks is None or self.done < self.max_tasks))

    def _post_lease(self, lease_id, action, body):
        """Submit or release. A 4xx drops the lease, no retry, and returns its code."""
        try:
            return self.client.post(f"/v1/leases/{urllib.parse.quote(lease_id, safe='')}/{action}", body)
        except ApiError as e:
            if e.code >= 500:
                raise
            self.log(f"lease {lease_id}: {action} refused ({e.code} {e.body}); dropped")
            return e.code

    def _release(self, lease_id, reason):
        self._post_lease(lease_id, "release", {"reason": reason})

    def check_drift(self):
        """Log each pinned project that the server no longer lists with the pinned hash."""
        try:
            live = {p["id"]: p["manifest_hash"] for p in self.client.get("/v1/projects")}
        except (ApiError, OSError) as e:  # advisory only: the lease path reports real faults
            self.log(f"project check failed: {e}")
            return
        for pid, pinned in self.cfg.get("projects", {}).items():
            if live.get(pid) != pinned:
                self.log(f"project {pid} changed or is not active: run `rally-worker join {pid}` again")

    def step(self) -> bool:
        """One lease, job, submit-or-release cycle. False means stop."""
        usable = [n for n in self.names if self._usable(n, self.now())]
        if not usable:
            self.log("no usable adapter left")
            return False
        if not self.cfg.get("projects"):
            self.log("no project joined: run `rally-worker join <project>`")
            return False
        try:
            lease = self.client.post("/v1/lease", {"projects": self.cfg["projects"], "offers": offers(self.cfg, usable)})
        except ApiError as e:
            if e.code != 403:
                raise
            self.log(f"lease refused: {e.body}")
            return False
        if lease is None:
            self.idle += 1
            if self.idle >= 10:
                self.idle = 0
                self.check_drift()
            self.log(f"no work now; next try in {self.backoff} s")
            self.sleep(self.backoff)
            self.backoff = min(self.backoff * 2, 300)
            return True
        self.backoff, self.idle = 30, 0
        lid, wu, name = lease["lease_id"], lease["wu"], lease["adapter"]
        if wu["manifest_hash"] != self.cfg["projects"].get(wu["project"]):
            self._release(lid, "manifest_changed")
            self.log(f"project {wu['project']} changed: run `rally-worker join {wu['project']}` again")
            return True
        if name not in usable:  # never run an adapter the participant did not offer
            self._release(lid, "error")
            return True
        st = self.state[name]
        try:
            status, text, usage = run_adapter(name, self.cfg["adapters"][name], wu, self.runner, st)
            if status == "ok":
                out = extract_json(text)
                if (why := check_schema(out, wu["schema"])) is None:
                    reply = self._post_lease(lid, "submit", {"output": out, "usage": usage})
                    st["done"], self.done = st["done"] + 1, self.done + 1
                    if not isinstance(reply, int):
                        st["errors"] = 0
                        self.log(f"{name}: {wu['project']}/{wu['id']} submitted: {reply}")
                        return True
                    if reply != 409:  # a 409 (lease expired, unit closed) is no fault of the adapter
                        st["errors"] += 1
                        st["disabled"] = st["errors"] >= 3
                    self.log(f"{name}: submit refused ({reply})" + ("; disabled for this run" * st["disabled"]))
                    return True
                status, usage["error"] = "error", why
        except KeyboardInterrupt:
            self._release(lid, "stop")
            raise
        except Exception as e:  # a bug or a 5xx must not hold the lease until it expires
            status, usage = "error", {"error": f"{type(e).__name__}: {e}"}
        if status == "rate_limited":
            st["blocked_until"] = self.now() + BLOCK_S
            self._release(lid, "rate_limited")
            self.log(f"{name}: rate limited; blocked for {BLOCK_S} s")
        else:
            st["errors"] += 1
            st["disabled"] = st["errors"] >= 3
            self._release(lid, "error")
            self.log(f"{name}: error ({usage.get('error')})" + ("; disabled for this run" * st["disabled"]))
        return True

    def run(self, once=False):
        try:
            self.check_drift()
            while self.step() and not once:
                pass
        except KeyboardInterrupt:
            self.log("stopped")


def config_path() -> Path:
    return Path(os.environ.get("RALLY_HOME") or Path.home() / ".rally") / "worker.json"


def load_config(path=None) -> dict:
    path = Path(path or config_path())
    if not path.exists():
        raise FileNotFoundError(f"no config at {path}: run `rally-worker init` first")
    cfg = json.loads(path.read_text())
    cfg.setdefault("projects", {})
    cfg.setdefault("adapters", {})
    for name, acfg in cfg["adapters"].items():
        if acfg.get("type", name) not in TYPES:
            raise ValueError(f"adapters.{name}: unknown type {acfg.get('type', name)!r}")
        argv = acfg.get("argv")
        if argv is not None and not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
            raise ValueError(f"adapters.{name}.argv: must be a non-empty list of strings")
        family_of(acfg, name)
    return cfg


def save_config(cfg, path=None):
    """Atomic write with mode 600: the file holds the Rally token."""
    path = Path(path or config_path())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # O_CREAT keeps the mode of a file that is already there
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)


def _client(cfg):
    return Client(cfg["server"], cfg["token"])


def cmd_init(args):
    path = config_path()
    cfg = load_config(path) if path.exists() else {"projects": {}, "adapters": {}}
    if args.token:
        token = args.token
    else:
        token = Client(args.server).post("/v1/participants", {"name": args.name, "invite": args.invite})["token"]
    cfg.update(server=args.server, token=token)
    save_config(cfg, path)
    print(f"saved {path}")
    return 0


def cmd_doctor(args):
    found = {}
    for name in ("claude", "codex", "agy", "opencode"):
        exe = shutil.which(name)
        if not exe:
            print(f"{name}: not found")
            continue
        try:  # --version only: it spends no quota
            p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
            version = (p.stdout or p.stderr).strip().splitlines()[:1] or ["?"]
        except (subprocess.TimeoutExpired, OSError) as e:
            version = [type(e).__name__]
        print(f"{name}: {exe} ({version[0]})")
        found[name] = {"family": TYPE_FAMILIES[name]} if name in TYPE_FAMILIES else {"model": "<model id>"}
    print("Suggested config block:")
    print(json.dumps({"adapters": found}, indent=2))
    return 0


def _shown(text):
    """Owner text with each control character but \\t and \\n as \\xNN, so it cannot drive the terminal."""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", lambda m: f"\\x{ord(m[0]):02x}", text)


def cmd_join(args):
    cfg = load_config()
    p = _client(cfg).get(f"/v1/projects/{urllib.parse.quote(args.project, safe='')}")
    stage = p["stages"]["brain"]
    print(f"Project:  {p['id']} - {_shown(p['title'])}")
    print(f"Track:    {p['track']}; stage mode: {stage['mode']}")
    print(f"Families: {_shown(', '.join((stage.get('needs') or {}).get('families') or [])) or 'any'}")
    print(f"Verify:   {p['verify']['mode']}")
    print(f"Prompt template:\n{_shown(stage['prompt'])}")
    if not args.yes and input("Join this project? [y/N] ").strip().lower() not in ("y", "yes"):
        print("not joined")
        return 1
    cfg["projects"][p["id"]] = p["manifest_hash"]
    save_config(cfg)
    print(f"joined {p['id']} at {p['manifest_hash']}")
    return 0


def cmd_leave(args):
    cfg = load_config()
    cfg["projects"].pop(args.project, None)
    save_config(cfg)
    print(f"left {args.project}")
    return 0


def cmd_run(args):
    cfg = load_config()
    worker = Worker(cfg, _client(cfg), names=args.adapter, max_tasks=args.max_tasks)

    def on_term(signum, frame):
        raise KeyboardInterrupt

    old = signal.signal(signal.SIGTERM, on_term)
    try:
        worker.run(once=args.once)
    finally:
        signal.signal(signal.SIGTERM, old)
    return 0


def cmd_status(args):
    cfg = load_config()
    print(json.dumps(_client(cfg).get("/v1/me"), indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rally-worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="register and write the config")
    p.add_argument("--server", required=True)
    p.add_argument("--name")
    p.add_argument("--invite")
    p.add_argument("--token")
    sub.add_parser("doctor", help="find the AI CLIs (runs only --version)")
    p = sub.add_parser("join", help="show a project and pin its manifest hash")
    p.add_argument("project")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("leave")
    p.add_argument("project")
    p = sub.add_parser("run")
    p.add_argument("--once", action="store_true")
    p.add_argument("--max-tasks", type=int)
    p.add_argument("--adapter", action="append")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    if args.cmd == "init" and not (args.token or (args.name and args.invite)):
        ap.error("init needs --token TOKEN, or --name NAME and --invite CODE")
    try:
        return globals()[f"cmd_{args.cmd}"](args)
    except (ApiError, OSError, ValueError, KeyError) as e:
        print(f"rally-worker: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
