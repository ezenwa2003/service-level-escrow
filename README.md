# ServiceLevelEscrow

A GenLayer Intelligent Contract that escrows payment for a service with measurable terms (a success rate and a p95 latency target) and settles it with a **graded payout** from the provider's service log.

## The idea

A provider promises, for example, 99.5% successful calls and a p95 latency under 800 ms. The client locks GEN. When the work period is over, the contract reads the provider's published call log and pays the provider a share that reflects how well the terms were met. The rest goes back to the client.

The key design choice: **code computes every number it can, and validators judge only what code cannot.**

- Code (deterministic): fetches the log, hashes it, checks its shape, counts calls and failures, and computes the p95 latency.
- Validators (LLM): decide only which failed calls an agreed exclusion clause excuses, such as "failures caused by invalid client requests (HTTP 4xx) do not count".

## Log format

A JSON array. Each record has `ok` (true or false) and `ms` (latency in milliseconds). A failed record may carry a `note`.

```json
[{"ok": true, "ms": 212}, {"ok": false, "ms": 35, "note": "HTTP 400 invalid request body sent by client"}]
```

`sample-service-log.json` is a 200-call example: 3 failures (two client errors, one provider crash) and a p95 of 1000 ms.

## Lifecycle

`AWAITING_ACCEPT` -> `AWAITING_FUNDS` -> `ACTIVE` -> `SETTLED`

If the log cannot be used three times in a row (`ACTIVE` -> `EVIDENCE_FAILED`), the client can `reclaim` the full amount (`REFUNDED`).

| Step | Who | Method |
| --- | --- | --- |
| Agree to the terms | provider | `accept_terms` |
| Lock GEN | client | `fund_escrow` (payable) |
| Compute and pay out | client or provider | `settle` |
| Recover funds after 3 bad logs | client | `reclaim` |
| Read everything | anyone | `get_agreement` |

The log URL, targets and exclusion clause are fixed at deploy time. The provider accepts them on-chain before any money can be locked, so neither side can swap the evidence source later.

## Payout

```
success_bps = (total - failed + excused) * 10000 / total
success_score = min(10000, success_bps * 10000 / min_success_bps)
latency_score = 10000 if p95 <= max_p95_ms else max_p95_ms * 10000 / p95
payout_bps    = success_score * latency_score / 10000
provider gets amount * payout_bps / 10000, the client gets the rest
```

Example with the sample log: 200 calls, 3 failed, 2 excused, p95 1000 ms, targets 99.5% and 800 ms. Success meets the target, latency scores 80%, so the provider is paid 80%.

## Consensus design

`settle` uses `gl.vm.run_nondet_unsafe` with a custom validator. Each validator re-fetches the log and re-runs the same evaluation, then:

- must match **exactly** on the log's SHA-256 hash, total calls, failed calls and p95 (so every validator read the same bytes and computed the same facts),
- may differ by at most `max(1, failed // 10)` on the **excused** count, the one number a language model influences,
- must see the same status and reason when the log is unusable.

The payout is then computed from the accepted numbers by plain code, so it is never a number the leader typed in.

## Failure handling and safety

- Unreachable, non-JSON, oversized or malformed logs return a fixed error code (`FETCH_FAILED`, `NOT_JSON`, `LOG_TOO_LARGE`, `BAD_RECORD_COUNT`, `BAD_RECORD_SHAPE`). The attempt is counted, no money moves, and after 3 attempts the client can reclaim.
- Every action is bound to the transaction sender and to the current stage.
- Failure notes are treated as untrusted data written by the provider. The prompt tells the model to ignore instructions inside them, only failures actually in the log can be excused, only the first 25 failures are shown to the model, and unclear cases are not excused.
- State is written before any money is sent.
- `log_url` must start with `https://`.

## Tests

`python test_service_level_escrow.py` runs 7 tests against the plain-code logic with GenLayer's runtime stubbed: log parsing and hashing, the p95 calculation, the payout formula, filtering of model output (out-of-range, non-integer and boolean indexes), failing closed on bad logs, and the validator agreement rule. These do not exercise live consensus.

## Tested live

Deployed and run end to end on GenLayer Studionet with the sample log: terms accepted, 1 GEN funded, `settle` reached Accepted consensus with 200 calls, 3 failures, 2 excused, p95 1000 ms and a payout of 8000 bps. The contract then sent 0.8 GEN to the provider and 0.2 GEN back to the client, leaving a balance of 0.

## Limits

- Tested on Studionet only, not on public testnets.
- The log is published by the provider. The contract proves exactly which log was read (by hash) but not that the log is truthful. Signed or client-side logs are not implemented.
- One agreement per deployed contract.
- No deadlines or appeals.
- The same address may be used for both parties, to test alone.
