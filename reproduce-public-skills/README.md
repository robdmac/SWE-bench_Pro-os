# Reproducing public-skill runs on SWE-bench Pro

This directory contains the **generation harness** used to benchmark Claude Code / codex "skills"
(agent methodology plugins) on SWE-bench Pro, plus the eval-harness fixes needed to score them
reliably. SWE-bench Pro itself only *scores patches* — it has no concept of an agent or skill — so the
skill-running lives here as a separate layer that produces patches, which the stock eval then scores.

## What's on this branch

1. **Eval-harness fix (root `swe_bench_pro_eval.py`)** — the only change to the benchmark itself:
   - `docker.from_env(timeout=3600)` — the SDK's 60s default read-timeout otherwise makes
     `container.wait()` raise `ReadTimeout` on slow (Go/JS) test suites, silently losing `output.json`
     and mis-scoring the instance as unresolved. **Required for correct scores.** (Also proposed
     upstream as PR `fix/docker-client-read-timeout`.)
   - Optional env-gated host-tuning knobs (`SWEPRO_EVAL_CPUS`, `SWEPRO_CPU_SHARES`, `SWEPRO_GOMAXPROCS`,
     `SWEPRO_SERIAL_PYTEST`) — for running many evals concurrently on one box. They don't change
     pass/fail; leave them at defaults to reproduce.

2. **Generation harness (`reproduce-public-skills/`)**:
   - `run_arm.py` — per-instance driver: pulls the instance's `jefzda/sweap-images` image, `docker exec`s
     codex inside it with the skill dir mounted read-only + the skill's trigger prompt prepended,
     extracts the git diff, then calls stock `swe_bench_pro_eval.py` to score it. Resumable, quota-aware.
   - `triggers/` — the prompt that tells the agent to read the mounted skill (`.jinja` for workflow
     skills, `.txt` for the ledger arms).
   - `build_sample.py` + `raw_sample_all.jsonl` — the 731-instance problem set.
   - `drivers/run_full.sh.example` — example batch driver (50 at a time across all 15 batches).
   - `skills-manifest.md` — the public skills, upstream URLs, and pinned commits.

## Inputs you supply

- **codex** CLI + auth (`codex login` → `~/.codex/auth.json`), model access to `gpt-5.5`.
- **Docker** + the public `jefzda/sweap-images` images (pulled on demand per instance).
- **Public skills** cloned per `skills-manifest.md`.

## Run

```bash
export SWEBENCH_PRO_DIR=/path/to/this/repo         # has helper_code/ + swe_bench_pro_eval.py
export SKILLS_DIR=/path/holding/solution/skill     # where you cloned the public skills
export WORK_DIR=/path/to/scratch                   # runs/ output goes here
export CODEX_AUTH=~/.codex/auth.json
# per-batch: SWEPRO_BATCH="" (first 50), "b" (next 50), ... "o"
SWEPRO_BATCH="" python reproduce-public-skills/run_arm.py <arm>
```

`<arm>` ∈ `baseline gsd omc superpowers spv6 karpathy addyosmani` (public), or the in-house
`evidencia*/referencer*/ref*/evd*` arms if you supply those private skill dirs.

Output lands in `$WORK_DIR/runs/<arm>_5p5<batch>/` with a `results.jsonl` (per-instance eval verdicts)
and `inst/<id>/` (patch, codex trajectory, test output).

## Notes / gotchas

- `run_arm.py` reads workers from `$WORK_DIR/runs/_workers.txt` (default 2). Each worker runs one
  instance = one image (2–20 GB) at a time; prune between evals to bound disk.
- Eval is scored **serially** per instance (`SCBENCH_PYTEST_WORKERS` unused here); parallel pytest
  (`-n auto`, e.g. ansible) is deterministic and unaffected — but `SWEPRO_SERIAL_PYTEST=1` forces `-n0`
  if you want to audit that.
- Known open eval issue not fixed here: the jest parser drops per-test results from FAIL-marked suites
  (element-web) — upstream issue #19. Mostly genuine failures, small residual bias.
