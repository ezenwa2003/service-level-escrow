"""Tests for the deterministic parts of ServiceLevelEscrow.
Run with:  python test_service_level_escrow.py   (or pytest)
GenLayer's runtime is stubbed, so these cover the plain-code logic, the
validator agreement rule, and the LLM-output filtering, not live consensus."""
import hashlib, importlib.util, json, pathlib, sys, types

HERE = pathlib.Path(__file__).parent

def _install_stub():
    gl = types.SimpleNamespace()
    gl.Contract = object
    def write(fn): return fn
    write.payable = write
    gl.public = types.SimpleNamespace(write=write, view=lambda fn: fn)
    gl.evm = types.SimpleNamespace(contract_interface=lambda cls: cls)
    gl.vm = types.SimpleNamespace(UserError=type("UserError", (Exception,), {}),
                                  Return=type("Return", (), {}))
    gl.nondet = types.SimpleNamespace(web=types.SimpleNamespace(get=None), exec_prompt=None)
    mod = types.ModuleType("genlayer")
    mod.gl, mod.u256, mod.Address = gl, int, str
    mod.__all__ = ["gl", "u256", "Address"]
    sys.modules["genlayer"] = mod
    return gl

gl = _install_stub()
spec = importlib.util.spec_from_file_location("sle", HERE / "service_level_escrow.py")
sle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sle)

BODY = (HERE / "sample-service-log.json").read_bytes()
CLAUSE = "Failures caused by invalid client requests (HTTP 4xx) do not count. Provider errors always count."

def _web(body, status=200):
    gl.nondet.web.get = lambda url: types.SimpleNamespace(body=body, status=status)

def test_sample_log_numbers():
    err, digest, recs = sle._parse_logs(BODY)
    assert err == "" and len(recs) == 200
    assert digest == hashlib.sha256(BODY).hexdigest()
    assert len(sle._failures(recs)) == 3
    assert sle._p95(recs) == 1000

def test_graded_payout():
    assert sle._payout_bps(200, 3, 2, 1000, 9950, 800) == 8000   # 80%
    assert sle._payout_bps(200, 0, 0, 500, 9950, 800) == 10000   # meets both targets
    assert sle._payout_bps(200, 3, 0, 800, 9950, 800) < 10000    # unexcused failures cost money
    assert sle._payout_bps(100, 100, 0, 0, 9950, 800) == 0       # nothing succeeded

def test_evaluate_with_llm():
    _web(BODY)
    gl.nondet.exec_prompt = lambda task: '```json\n{"excused": [17, 88]}\n```'
    r = sle._evaluate("https://x/log.json", CLAUSE)
    assert (r["status"], r["total"], r["failed"], r["excused"], r["p95"]) == ("OK", 200, 3, [17, 88], 1000)

def test_llm_cannot_excuse_unknown_or_bad_indexes():
    fails = [(17, "a"), (88, "b")]
    gl.nondet.exec_prompt = lambda task: '{"excused": [17, 999, true, "88", -1, 17]}'
    assert sle._excused(CLAUSE, fails) == [17]
    gl.nondet.exec_prompt = lambda task: "not json at all"
    assert sle._excused(CLAUSE, fails) == []
    assert sle._excused("   ", fails) == []          # empty clause excuses nothing

def test_only_first_failures_reach_the_llm():
    many = [(i, "n") for i in range(100)]
    gl.nondet.exec_prompt = lambda task: json.dumps({"excused": list(range(100))})
    assert len(sle._excused(CLAUSE, many)) == sle.MAX_LLM_FAILURES

def test_invalid_evidence_fails_closed():
    gl.nondet.exec_prompt = lambda task: '{"excused": []}'
    for body, code in [(b"nope", "NOT_JSON"), (b"[]", "BAD_RECORD_COUNT"),
                       (b'[{"ok": true}]', "BAD_RECORD_SHAPE"),
                       (b'[{"ok": true, "ms": -5}]', "BAD_RECORD_SHAPE"),
                       (b'[{"ok": "yes", "ms": 5}]', "BAD_RECORD_SHAPE"),
                       (b"x" * (sle.MAX_BYTES + 1), "LOG_TOO_LARGE")]:
        _web(body)
        r = sle._evaluate("https://x/log.json", CLAUSE)
        assert (r["status"], r["reason"]) == ("INVALID", code), (body[:20], r)
    _web(b"[]", status=404)
    assert sle._evaluate("https://x/log.json", CLAUSE)["reason"] == "FETCH_FAILED"
    def boom(url): raise RuntimeError("network")
    gl.nondet.web.get = boom
    assert sle._evaluate("https://x/log.json", CLAUSE)["reason"] == "FETCH_FAILED"

def test_validator_agreement_rule():
    mine = sle._result("OK", "", "h", 200, 3, [17, 88], 1000)
    assert sle._agrees(dict(mine), mine)
    assert not sle._agrees({**mine, "excused": [17]}, mine)            # one fewer excused
    assert not sle._agrees({**mine, "excused": [17, 88, 151]}, mine)   # one more excused
    assert not sle._agrees({**mine, "excused": [17, 151]}, mine)       # same count, different failures
    assert not sle._agrees({**mine, "excused": [True, 88]}, mine)      # wrong type
    assert not sle._agrees({**mine, "excused": "17,88"}, mine)
    assert not sle._agrees({**mine, "p95": 900}, mine)                 # deterministic fact differs
    assert not sle._agrees({**mine, "hash": "other"}, mine)            # read different evidence
    assert not sle._agrees({**mine, "total": 199}, mine)
    assert not sle._agrees("garbage", mine)
    bad = sle._result("INVALID", "NOT_JSON", "h")
    assert sle._agrees(dict(bad), bad)
    assert not sle._agrees(dict(mine), bad)                            # leader OK, validator INVALID

def test_every_accepted_result_has_the_same_allocation():
    import itertools
    mine = sle._result("OK", "", "h", 200, 3, [17, 88], 1000)
    payout = lambda r: sle._payout_bps(r["total"], r["failed"], len(r["excused"]), r["p95"], 9950, 800)
    accepted = 0
    for n in range(4):
        for combo in itertools.combinations([17, 88, 151], n):
            variant = {**mine, "excused": list(combo)}
            if sle._agrees(variant, mine):
                accepted += 1
                assert payout(variant) == payout(mine)
                assert (1000000000000000000 * payout(variant)) // 10000 == 800000000000000000
    assert accepted == 1                                              # only the identical result passes

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t(); print("PASS", t.__name__)
    print(len(tests), "tests passed")
