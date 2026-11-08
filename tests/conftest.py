"""Shared test helpers: a fake clock, manifests built in code, and Coordinator fixtures."""

import copy

import pytest

START = 1_800_000_000  # an integer epoch, so stored times stay integers
TEXT = "The sample shows alpha at 1.5 units. A second line follows."
QUOTE = "alpha at 1.5 units"
FAMILIES = ["anthropic", "openai", "google", "deepseek"]
DROP = object()  # make_manifest(x=DROP) removes key x


class FakeClock:
    def __init__(self, t=START):
        self.t = t

    def now(self):
        return self.t

    __call__ = now

    def advance(self, seconds):
        self.t += seconds


def _base_manifest():
    return {
        "id": "tst-quorum",
        "version": 1,
        "title": "Demo quorum project",
        "owner": "tst-owner",
        "track": "brain",
        "stages": {
            "brain": {
                "mode": "json",
                "prompt": "Read the text and extract the facts.\nTEXT:\n${text}",
                "schema": {
                    "type": "object",
                    "required": ["kind", "value", "quotes"],
                    "properties": {
                        "kind": {"enum": ["alpha", "beta", "unclear"]},
                        "value": {"type": ["number", "null"]},
                        "quotes": {"type": "array", "items": {"type": "string", "maxLength": 400}, "maxItems": 4},
                    },
                },
                "timeout_s": 60,
                "lease_ttl_s": 600,
                "points": 2,
            }
        },
        "verify": {
            "mode": "quorum",
            "checks": [{"type": "quote_span", "field": "quotes", "source": "text"}],
            "quorum": {
                "replicas": 2,
                "agree": 2,
                "distinct_families": True,
                "fields": ["kind", "value"],
                "max_replicas": 3,
                "digits": 3,
            },
        },
        "canary": {"rate": 0, "quarantine_after": 2},
    }


def _apply(m, overrides):
    for key, value in overrides.items():
        *parents, last = key.split("__")
        d = m
        for p in parents:
            d = d.setdefault(p, {})
        if value is DROP:
            d.pop(last, None)
        else:
            d[last] = copy.deepcopy(value)
    return m


def make_manifest(**overrides):
    """A valid quorum manifest. A key path uses "__": make_manifest(verify__quorum__replicas=3)."""
    return _apply(_base_manifest(), overrides)


def make_det_manifest(**overrides):
    """The same project shape with deterministic verify (no quorum block)."""
    m = make_manifest(id="tst-det", title="Demo deterministic project")
    m["verify"] = {"mode": "deterministic", "checks": [{"type": "quote_span", "field": "quotes", "source": "text"}]}
    return _apply(m, overrides)


def make_units(n, text=TEXT):
    return [{"id": f"tst-u{i}", "payload": {"text": text}} for i in range(1, n + 1)]


def make_canaries(n, answer=None, text=TEXT):
    answer = answer or {"kind": "alpha", "value": 1.5}
    return [{"payload": {"text": text}, "answer": dict(answer)} for _ in range(n)]


def make_output(kind="alpha", value=1.5, quotes=(QUOTE,)):
    return {"kind": kind, "value": value, "quotes": list(quotes)}


def offer(family, adapter="fake", model=None):
    return {"adapter": adapter, "kind": "llm", "family": family, "model": model, "modes": ["json"]}


def add_people(coord, workers=4):
    """tst-admin, the manifest owner tst-owner and workers tst-w1..tst-wN. Returns {name: {"id", "name", "token"}}."""
    people = {}
    for name, role in [("tst-admin", "admin"), ("tst-owner", "owner")] + [(f"tst-w{i}", "worker") for i in range(1, workers + 1)]:
        token = coord.add_participant(name, role)
        people[name] = {"id": coord.auth(token)["id"], "name": name, "token": token}
    return people


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "rally.db"


@pytest.fixture
def coord(db_path, clock):
    from rally.server import Coordinator  # lazy: checks tests run without server.py

    return Coordinator(db_path, now=clock.now)
