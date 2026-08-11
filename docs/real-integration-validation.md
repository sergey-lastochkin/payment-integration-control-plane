# Local n8n / bank validation

This repository contains a runnable local integration route:

```text
HTTP client -> n8n -> payment control plane -> bank emulator
            <- n8n callback <- bank emulator
```

It uses the pinned official image `n8nio/n8n:1.82.3`, two FastAPI services and
synthetic `TEST-*` document references only. The Docker services communicate
on an isolated project network. The only published ports bind to `127.0.0.1`.
No client documents, payment details, credentials, real bank endpoints or 1C
runtime are part of this stack.

The runner proves eight cases: normal route, duplicate submit, delayed response,
callback before sender response, duplicate callback, response loss after durable
bank commit with restart/status lookup, repeated bank event and a conflicting
callback that reaches `manual_check`. It also returns n8n `$execution.id` from
the real intake workflow and saves it in the evidence record.

The redacted evidence is [summary.json](../real_run/runs/local-n8n-bank-2026-08-11/summary.json).
It records `one_c_participated: false` and must be read as local n8n/bank
evidence only.

The local result is deliberately not evidence of a 1C:УПП integration. For that
claim, a test UPP HTTPService must create a synthetic payment document, invoke
the same route and receive the status back through its own service. Until then,
the 1C boundary remains blocked.

The existing test UPP environment was observed, but its configurator was already
locked by an active session. No document, HTTPService or configuration object
was changed. The concrete blocker is `TEST_UPP_CONFIGURATOR_ACTIVE_SESSION_LOCK`.
