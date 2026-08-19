# Live-drive example evals (pre-release tier)

Specs here (`<example>.live.yaml`) cover the examples whose behaviour the offline suite cannot
prove — model-decided routing, improvised wording, real timeouts, true concurrency, real
network calls. They are marked `tier: live` in `../examples/evals/registry.yaml`.

Each spec deploys the example and drives it against the real CES runtime via
`flows.drive.run_steps`, grading the transcript with the offline expectation vocabulary
restricted to what a live transcript exposes (`said_contains`, `tool_called`, ordering).

Run (needs a GCP checkout + creds):

```
FLOWS_LIVE_EVALS=1 PYTHONPATH=../src pytest -m live ../tests/test_example_evals_live.py
```

PR CI runs `-m "not live"` and never touches this tier; it runs in the pre-release/publish
workflow. Seed the first specs from the `../examples/*_VERIFY.md` scripts (TIMEOUT, AGENT_TOOL,
AGENT_TOOL_ASYNC, PROGRESSIVE_FAN_OUT, REMOTE_TOOL, ROUTER_FILLER, TOOL_TIMEOUTS).

Spec shape mirrors the offline eval format (see `../examples/evals/EVAL_FORMAT.md`), plus a
`cuj:` variable seed and an `app:` reference per scenario.
```
