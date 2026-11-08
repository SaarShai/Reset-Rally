"""Unit tests for rally.worker covering W-01 through W-10."""

import json
import pytest

from rally.worker import (
    AdapterBusy,
    AdapterError,
    AdapterTimeout,
    Busy,
    QuotaExhausted,
    Worker,
    build_argv,
    classify_cli_result,
    resolve_family,
)


class MockClient:
    """Mock Coordinator client that records all calls and arguments."""

    def __init__(self, lease_payload=None):
        self.lease_payload = lease_payload or {"lease_id": "lease-101", "wu": {"prompt": "Solve item 1"}}
        self.lease_calls = []
        self.submit_calls = []
        self.release_calls = []
        self.lease_status = 200
        self.submit_status = 200

    def lease(self, **kwargs):
        self.lease_calls.append(kwargs)
        if self.lease_status == 403:
            return {"status": 403, "error": "quarantined"}
        if self.lease_status == 204:
            return None
        return self.lease_payload

    def submit(self, lease_id, raw_output=None, model=None, **kwargs):
        call = {"lease_id": lease_id, "raw_output": raw_output, "model": model, **kwargs}
        self.submit_calls.append(call)
        return {"status": self.submit_status}

    def release(self, lease_id, reason=""):
        self.release_calls.append({"lease_id": lease_id, "reason": reason})
        return {"status": 200}


def test_w01_window_start():
    """W-01: step() makes zero lease calls before window_start."""
    clock_time = 17 * 3600 + 59 * 60  # 17:59
    client = MockClient()
    worker = Worker(
        participant="alice",
        cli="claude",
        model="claude-sonnet-5-5",
        rallies=["B", "C"],
        client=client,
        now=lambda: clock_time,
        window_start=18 * 3600,  # 18:00
        window_end=20 * 3600,    # 20:00
    )

    res = worker.step()
    assert res is False
    assert worker.lease_calls == 0
    assert len(client.lease_calls) == 0

    # Advance time to 18:00; step should now request a lease.
    clock_time = 18 * 3600
    runner_called = False

    def fake_runner(argv, stdin, env):
        nonlocal runner_called
        runner_called = True
        return 0, '{"ans": 1}', ""

    worker.runner = fake_runner
    res = worker.step()
    assert res is True
    assert worker.lease_calls == 1
    assert runner_called is True
    assert len(client.lease_calls) == 1


def test_w02_window_end_buffer():
    """W-02: step() does not take lease when execution buffer exceeds window end."""
    # window_end = 20:00 (72000s), buffer_s = 600s (10 min), est_wu_s = 600s (10 min)
    # Effective deadline is window_end - buffer_s = 19:50.
    # At 19:45, t + est_wu_s = 19:55 > 19:50, so step() must refuse new work.
    clock_time = 19 * 3600 + 45 * 60
    client = MockClient()
    worker = Worker(
        participant="alice",
        cli="claude",
        model="claude-sonnet-5-5",
        client=client,
        now=lambda: clock_time,
        window_start=18 * 3600,
        window_end=20 * 3600,
        buffer_s=600.0,
        est_wu_s=600.0,
    )

    assert worker.in_window(clock_time) is False
    assert worker.step() is False
    assert worker.lease_calls == 0
    assert len(client.lease_calls) == 0

    # At 19:39, 19:39 + 10 min = 19:49 <= 19:50, so execution is allowed.
    assert worker.in_window(19 * 3600 + 39 * 60) is True


def test_w03_max_tasks_budget():
    """W-03: worker stops with state 'stopped:budget' after reaching max_tasks."""
    client = MockClient()
    worker = Worker(
        participant="alice",
        cli="claude",
        model="claude-sonnet-5-5",
        client=client,
        runner=lambda argv, stdin, env: (0, '{"result": 42}', ""),
        max_tasks=4,
    )

    for i in range(4):
        client.lease_payload = {"lease_id": f"lease-{i}", "prompt": f"item-{i}"}
        active = worker.step()
        assert active is True
        assert worker.submits == i + 1

    assert worker.state == "stopped:budget"
    assert worker.submits == 4

    # Fifth step must immediately return False without calling client.lease.
    assert worker.step() is False
    assert worker.lease_calls == 4


def test_w04_model_family_resolution():
    """W-04: family resolves from model ID string, independent of CLI prefix."""
    # Anthropic
    assert resolve_family("claude/claude-sonnet-5-5") == "anthropic"
    assert resolve_family("agy/claude-opus-5-5-high") == "anthropic"
    assert resolve_family("haiku-3") == "anthropic"
    assert resolve_family("fable-preview") == "anthropic"

    # Google
    assert resolve_family("agy/gemini-3.8-flash-low") == "google"
    assert resolve_family("opencode-go/gemini-4-argon-contributor") == "google"
    assert resolve_family("gemini-2.5-pro") == "google"

    # OpenAI
    assert resolve_family("codex/gpt-6-astra") == "openai"
    assert resolve_family("agy/gpt-oss-120b-medium") == "openai"
    assert resolve_family("codex/custom") == "openai"
    assert resolve_family("openai/o1-mini") == "openai"
    assert resolve_family("o3-high") == "openai"
    assert resolve_family("sol-preview") == "openai"

    # DeepSeek and Qwen
    assert resolve_family("opencode/deepseek-v4.1-flash-free") == "deepseek"
    assert resolve_family("deepseek-chat") == "deepseek"
    assert resolve_family("ollama/qwen2.5-coder") == "qwen"

    # Brawn / Local / Empty
    assert resolve_family(None) == "brawn"
    assert resolve_family("") == "brawn"
    assert resolve_family("brawn") == "brawn"
    assert resolve_family("local") == "brawn"

    with pytest.raises(ValueError):
        resolve_family("unknown-vendor-model-123")


def test_w05_build_argv_for_all_clis():
    """W-05: command construction produces expected argv for all 5 CLIs."""
    # claude
    argv, stdin = build_argv("claude", "claude-sonnet-5-5", "prompt text")
    assert argv == ["claude", "-p", "--model", "claude-sonnet-5-5", "prompt text"]
    assert stdin is None

    # codex
    argv, stdin = build_argv("codex", "gpt-6-astra", "prompt text")
    assert argv == ["codex", "exec", "-m", "gpt-6-astra", "prompt text"]
    assert stdin is None

    # agy
    argv, stdin = build_argv("agy", "gemini-3.8-flash-low", "prompt text")
    assert argv == ["agy", "-p", "--model", "gemini-3.8-flash-low", "prompt text"]
    assert stdin is None

    # opencode
    argv, stdin = build_argv("opencode", "deepseek-v4.1-flash-free", "prompt text")
    assert argv == ["opencode", "run", "-m", "deepseek-v4.1-flash-free", "prompt text"]
    assert stdin is None

    # brawn
    argv, stdin = build_argv("brawn", None, "prompt text")
    assert argv == ["brawn", "prompt text"]
    assert stdin is None

    with pytest.raises(ValueError):
        build_argv("unsupported-cli", None, "prompt")


@pytest.mark.parametrize(
    "cli,model,rc,stderr,sample_name",
    [
        ("claude", "claude-sonnet-5-5", 1, "Claude usage limit reached. Your limit will reset at 8pm.", "claude"),
        ("codex", "codex/gpt-6-astra", 1, "You've hit your usage limit. Try again in 3 hours.", "codex"),
        ("agy", "agy/gemini-3.8-flash-low", 1, "Individual quota reached", "agy"),
        ("opencode", "opencode/deepseek-v4.1-flash-free", 1, "Go usage limit exceeded", "opencode"),
        ("agy", "gemini-4-argon", 1, "RESOURCE_EXHAUSTED: quota exceeded for model", "resource_exhausted"),
    ],
)
def test_w06_quota_exhausted_releases_and_stops(cli, model, rc, stderr, sample_name):
    """W-06: quota exhaustion releases lease with 'rate_limited' and sets stopped:quota."""
    # Check classifier exception directly.
    with pytest.raises(QuotaExhausted):
        classify_cli_result(cli, rc, "", stderr)

    client = MockClient(lease_payload={"lease_id": f"lease-{sample_name}", "prompt": "task"})
    worker = Worker(
        participant="alice",
        cli=cli,
        model=model,
        client=client,
        runner=lambda argv, stdin, env: (rc, "", stderr),
    )

    res = worker.step()
    assert res is False
    assert worker.state == "stopped:quota"
    assert worker.releases == 1
    assert len(worker.held_leases) == 0
    assert len(client.release_calls) == 1
    assert client.release_calls[0] == {"lease_id": f"lease-{sample_name}", "reason": "rate_limited"}

    # Worker stays stopped on subsequent steps.
    assert worker.step() is False
    assert worker.lease_calls == 1


def test_w07_adapter_busy_releases_and_continues():
    """W-07: model capacity error releases lease with 'busy' and worker can continue."""
    with pytest.raises(AdapterBusy):
        classify_cli_result("codex", 1, "", "model is at capacity")

    client = MockClient(lease_payload={"lease_id": "lease-busy", "prompt": "task"})
    call_count = 0

    def runner_flaky(argv, stdin, env):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return 1, "", "model is at capacity"
        return 0, '{"ans": 1}', ""

    worker = Worker(
        participant="bob",
        cli="codex",
        model="codex/gpt-6-astra",
        client=client,
        runner=runner_flaky,
    )

    # First step encounters busy; releases lease and returns False for this cycle.
    res1 = worker.step()
    assert res1 is False
    assert worker.state != "stopped:quota"
    assert worker.releases == 1
    assert len(worker.held_leases) == 0
    assert client.release_calls[-1]["reason"] == "busy"

    # Second step succeeds when capacity clears.
    res2 = worker.step()
    assert res2 is True
    assert worker.submits == 1


def test_w08_adapter_error_releases_and_continues():
    """W-08: unexpected non-zero exit code releases lease with 'adapter_error' and continues."""
    with pytest.raises(AdapterError):
        classify_cli_result("agy", 3, "", "unexpected crash")

    client = MockClient(lease_payload={"lease_id": "lease-err", "prompt": "task"})
    worker = Worker(
        participant="carol",
        cli="agy",
        model="agy/gemini-3.8-flash-low",
        client=client,
        runner=lambda argv, stdin, env: (3, "", "fatal failure"),
    )

    res = worker.step()
    assert res is True  # Worker continues to next work unit.
    assert worker.releases == 1
    assert len(worker.held_leases) == 0
    assert client.release_calls[0] == {"lease_id": "lease-err", "reason": "adapter_error"}


def test_w09_submit_returns_409_drops_lease_without_retry():
    """W-09: 409 stale lease response drops lease and does not retry submit."""
    client = MockClient(lease_payload={"lease_id": "lease-stale", "prompt": "task"})
    client.submit_status = 409

    worker = Worker(
        participant="eve",
        cli="opencode",
        model="opencode/deepseek-v4.1-flash-free",
        client=client,
        runner=lambda argv, stdin, env: (0, '{"ans": 2}', ""),
    )

    res = worker.step()
    assert res is True
    assert len(client.submit_calls) == 1  # Exactly one submit attempt, no retry.
    assert len(worker.held_leases) == 0
    assert worker.submits == 0


def test_w10_zero_credentials_never_reach_coordinator():
    """W-10 & I-10: credentials remain in runner subprocess env and never reach client."""
    secret_env = {
        "ANTHROPIC_API_KEY": "sk-SENTINEL-7f3a",
        "OPENAI_API_KEY": "sk-SENTINEL-7f3a",
        "GEMINI_API_KEY": "SENTINEL-7f3a",
        "USER": "tester",
    }
    client = MockClient(lease_payload={"lease_id": "lease-sec", "prompt": "task"})
    runner_env_received = {}

    def spy_runner(argv, stdin, env):
        nonlocal runner_env_received
        runner_env_received = env
        return 0, '{"data": "clean"}', ""

    worker = Worker(
        participant="alice",
        cli="claude",
        model="claude-sonnet-5-5",
        client=client,
        runner=spy_runner,
        env=secret_env,
    )

    res = worker.step()
    assert res is True

    # The runner must receive the local environment with credentials.
    assert runner_env_received["ANTHROPIC_API_KEY"] == "sk-SENTINEL-7f3a"

    # The client calls must NEVER receive any SENTINEL string.
    recorded_calls = json.dumps(
        {
            "lease": client.lease_calls,
            "submit": client.submit_calls,
            "release": client.release_calls,
        }
    )
    assert "SENTINEL" not in recorded_calls


def test_brawn_adapter_timeout_submits_verdict():
    """Brawn adapter timeout produces a submitted timeout verdict."""
    client = MockClient(lease_payload={"lease_id": "lease-brawn", "prompt": "compute"})
    worker = Worker(
        participant="dave",
        cli="brawn",
        model=None,
        client=client,
        runner=lambda argv, stdin, env: (124, "", "timeout"),
    )

    res = worker.step()
    assert res is True
    assert worker.submits == 1
    assert len(client.submit_calls) == 1
    payload = json.loads(client.submit_calls[0]["raw_output"])
    assert payload == {"verdict": "timeout"}


def test_quarantine_handling_on_lease_and_submit():
    """Worker transitions to quarantined on 403 response from lease or submit."""
    # 403 on lease
    client_lease_403 = MockClient()
    client_lease_403.lease_status = 403
    w1 = Worker(participant="eve", cli="opencode", model="deepseek", client=client_lease_403)
    assert w1.step() is False
    assert w1.state == "quarantined"

    # 403 on submit (e.g. following canary failure)
    client_submit_403 = MockClient()
    client_submit_403.submit_status = 403
    w2 = Worker(
        participant="eve",
        cli="opencode",
        model="deepseek",
        client=client_submit_403,
        runner=lambda argv, stdin, env: (0, '{"ans": 1}', ""),
    )
    assert w2.step() is False
    assert w2.state == "quarantined"


def test_gate1_422_rejection():
    """Gate 1 rejection (422) does not increment submits and worker continues."""
    client = MockClient()
    client.submit_status = 422
    worker = Worker(
        participant="eve",
        cli="opencode",
        model="deepseek",
        client=client,
        runner=lambda argv, stdin, env: (0, '{"ans": 1}', ""),
    )
    assert worker.step() is True
    assert worker.submits == 0
    assert len(worker.held_leases) == 0


def test_held_leases_set_and_int_equality():
    """held_leases conforms to both set behavior and integer equality comparison."""
    client = MockClient()
    worker = Worker(
        participant="alice",
        cli="claude",
        model="claude-sonnet-5-5",
        client=client,
    )
    assert worker.held_leases == 0
    assert worker.held_leases == set()
    assert isinstance(worker.held_leases, set)
    assert (worker.submits, worker.releases, worker.state, worker.held_leases) == (0, 0, "idle", 0)

    worker.held_leases.add("lease-test")
    assert worker.held_leases == 1
    assert worker.held_leases == {"lease-test"}
    worker.held_leases.discard("lease-test")
    assert worker.held_leases == 0


def test_classify_cli_result_return_mode_and_success():
    """classify_cli_result returns exception when raise_exc=False and None on rc=0."""
    assert classify_cli_result("claude", 0, "success", "") is None

    exc_quota = classify_cli_result("claude", 1, "", "usage limit exceeded", raise_exc=False)
    assert isinstance(exc_quota, QuotaExhausted)

    exc_busy = classify_cli_result("codex", 1, "", "try again in a moment", raise_exc=False)
    assert isinstance(exc_busy, AdapterBusy)

    exc_timeout = classify_cli_result("brawn", 124, "", "", raise_exc=False)
    assert isinstance(exc_timeout, AdapterTimeout)

    exc_err = classify_cli_result("brawn", 2, "", "syntax error", raise_exc=False)
    assert isinstance(exc_err, AdapterError)


def test_lease_calls_before_and_worker_run_loop():
    """Worker tracks lease call timestamps and run() loops until completion."""
    client = MockClient()
    t_start = 18 * 3600
    current_time = t_start

    worker = Worker(
        participant="alice",
        cli="claude",
        model="claude-sonnet-5-5",
        client=client,
        now=lambda: current_time,
        runner=lambda argv, stdin, env: (0, '{"data": "ok"}', ""),
        window_start=t_start,
        window_end=20 * 3600,
        max_tasks=2,
    )

    worker.run()
    assert worker.submits == 2
    assert worker.state == "stopped:budget"
    assert worker.lease_calls_before(t_start - 10) == 0
    assert worker.lease_calls_before(t_start + 10) == 2
    assert worker.lease_calls_before("18:00") == 0


def test_default_runner_subprocess_execution():
    """default_runner executes command in real subprocess and returns streams."""
    from rally.worker import default_runner

    rc, stdout, stderr = default_runner(
        ["python3", "-c", "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(0)"],
        None,
        None,
    )
    assert rc == 0
    assert stdout == "out"
    assert stderr == "err"

