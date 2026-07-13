"""Golden-ticket evaluation suite for the planner/classifier pipeline.

Two consumers, one scorer:

- tests/test_golden_tickets.py (CI, deterministic): replays each golden
  ticket's RECORDED model outputs through the real graph — classifier
  parsing, username extraction, JSON guardrails, sensitivity gating,
  routing — and requires a 100% pass. This pins the pipeline's contract:
  a prompt/parser/policy change that alters what happens to these tickets
  fails CI immediately.

- evals/run_live.py (manual / scheduled, uses the real configured LLM):
  same tickets, same scoring, but the model answers for itself — this is
  what catches MODEL drift (provider swaps, model version bumps, prompt
  regressions that only show up in real generations). Run it before
  changing LLM_PROVIDER/model pins, and periodically against production
  config.
"""
