# Agent Harness workspace

Branch: `feature/agent-harness-daytona-nosana`.

This initial commit creates the isolated workspace only. Discovery, provider integrations, and recommendations are not implemented in this commit. Existing frontend/backend work and the repository README are preserved.

## Scope

```text
User Context
    -> Job Discovery
    -> Daytona Execution
    -> Job Normalization
    -> Constraint Filtering
    -> Preference / Capability Matching
    -> Ranking
    -> Recommendation JSON
```

The intended backend entrypoint is `run_harness(user_context)`. The implementation will preserve a shared normalized-job contract and return evidence-backed recommendations without inventing unknown job data.

## Sponsor architecture

```text
Daytona = Agent Execution Plane
Nosana  = Optional AI Reasoning / Inference Plane
```

Daytona must be in the real runtime path:

```text
Harness
    -> DaytonaJobDiscoveryRuntime.discover(user_context)
    -> [Daytona: Job Source Adapter -> Scraping / Parsing / Normalization]
    -> NormalizedJob[]
    -> [Harness Core: Constraint Filter -> Match -> Rank]
    -> Recommendation JSON
```

External interaction and execution belong to Daytona. Recommendation decisions belong to Harness Core. A failed live source must use a clearly labelled fixture adapter inside Daytona; local-only execution must never be presented as a Daytona success.

Nosana is optional. Missing credentials, network errors, timeouts, or inference failures must fall back to deterministic weighted ranking. Inference may rank collected jobs and select supporting evidence; it must not create postings or override hard constraints.

## Ownership

- `harness/`: core, runtime, source adapters, normalizer, matching, recommender, fixtures.
- `tests/`: harness tests.
- `examples/`: backend usage and demo inputs.
- `demo.py`: future E2E demo entrypoint; currently a setup notice only.
- `HARNESS_README.md`: harness-specific instructions.

Do not modify frontend/backend directories without explicit integration authorization. Keep this hackathon implementation narrow: one reusable Daytona sandbox, one live source, fixture fallback, deterministic ranking, and optional Nosana inference.
