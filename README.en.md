# ES-Memory RAG 仓管 (ES-Memory RAG Warehouse)

[中文](README.md) | **English**

Give your Agent's memory a **lifecycle** instead of letting it pile up.

This is a Skill package for agents that need long-term memory. It turns an
ESDM-style memory curve into a real retrieval pipeline, and keeps a **separate
overnight warehouse** that maintains the memory lifecycle for you. The main
chat only ever receives a fact projection retrieved for the current topic — it
never does real-time tidying.

> **For agents:** copy the whole `es-memory-rag/` directory into your agent's
> skills directory. See [INSTALL.md](INSTALL.md) for paths and the three ways
> people get this wrong.
>
> **Before you start:** read §0 of [GUIDE.md](GUIDE.md). If it says "don't",
> then don't — this package is not a reason to rebuild your agent.

## The problem it solves

Most "make the AI remember" solutions focus on **storage**. This one focuses on
the **lifecycle**:

- A memory has a **weight, a curve and a profile**. It decays over time, and
  keeps full detail or compresses to a core projection depending on how
  important it is.
- Compression is **lossy**, so the invariants matter: never drop the subject,
  the direction of the relation, the negation, the time, or the source.
  "Alice doesn't like X" must never compress into "Alice likes X".
- **State and narrative are physically separate.** The relationship ledger, the
  open loops, the veto list and the current task live in a state layer that is
  **never compressed, never decayed, never drawn for deletion**.
- Context compaction turns relationships into information. So the rule is
  **move the loss from write time to read time**: keep the original text, and
  give less of it at read time.
- A context has no "missing page" signal — the model doesn't know what it is
  missing, it just confabulates. So every injection must **declare what it left
  out** ("14 more related records not loaded; fetch them if needed").
- Decay, the deletion lottery, permissions, CAS and deletion are executed by
  **code**. The model only judges value and fact equivalence.

## Install

See [INSTALL.md](INSTALL.md) — per-agent paths, the three common mistakes, and
how to verify the install actually works.

## Quick start

```sh
git clone https://github.com/unhuman1146/es-memory-rag.git
cd es-memory-rag

# create the database (six tables + WAL)
python scripts/memory_engine.py --db demo.db init

# ingest one event; the same event again is dropped by the 120s transport dedup
python scripts/memory_engine.py --db demo.db event assets/example-event.json

# nightly maintenance: dry run by default, --apply to write
python scripts/memory_engine.py --db demo.db maintain
python scripts/memory_engine.py --db demo.db maintain --apply

# pending jobs for the RAG adapter
python scripts/memory_engine.py --db demo.db outbox

# tests: 135 in total
python scripts/test_memory_engine.py
python scripts/test_context_budget.py
python scripts/test_adapter.py
python scripts/test_chat_path.py
```

Requires Python 3.11+, **standard library only** — no third-party dependencies.

## Layout

```
es-memory-rag/
├── SKILL.md          the behavioural spec — read this first
├── GUIDE.md          construction entry point: when to do this, the six steps, acceptance
├── INSTALL.md        per-agent install paths, common mistakes, verification, upgrade
├── CHANGELOG.md      version history
├── NOTICE.md         third-party notice
├── references/
│   ├── design.md           rules, formulas, state machine
│   ├── integration.md      RAG / overnight-warehouse wiring contract
│   ├── adapters.md         reference adapter wiring (embedding backends, credentials, thresholds)
│   ├── emotion.md          the three emotion layers
│   ├── state-ledger.md     the state layer and its failure modes
│   ├── context-budget.md   512K hard ceiling / 256K soft line
│   └── evidence.md         what was measured, what was overturned
├── scripts/
│   ├── memory_engine.py    reference engine (SQLite transactions, outbox, stable lottery, CAS)
│   ├── context_budget.py   context assembly and budgeting
│   ├── demo_live_rag.py    live acceptance script (needs a key, not in CI)
│   ├── demo_live_injection.py  live acceptance of the injection path (needs a key, not in CI)
│   └── test_*.py           135 tests
├── adapters/        reference adapter (embedding / vector store / rerank / contract / injection path / outbox)
└── assets/
    ├── config.example.json
    ├── es-memory-warehouse.service / .timer   systemd wiring templates
    └── example-event.json / example-emotion-event.json
```

## The two boundaries people trip over

1. **The reference engine is not a connected RAG.** It does no embedding, no
   reranking and no model calls. A non-empty outbox means *a job was written*,
   not that anything was synced. A real integration needs your own
   `RAGAdapter` (`upsert` / `delete` / `search` / `rerank` / `count`), and you
   may only `ack` after the remote side confirms.
2. **A missing reranker or an unavailable index must be reported as
   `degraded`.** "Not reranked" is not "reranked".

Full acceptance gates: [GUIDE.md §6](GUIDE.md).

## What's measured, and what's only a default

[references/evidence.md](references/evidence.md) keeps the two apart — **do not
cite a default as a conclusion**:

- **Measured** (a commercial long-context endpoint, temperature 0, 34 calls in
  total): tokenizer ratio ≈ 1 token per 1.5 CJK characters; single-needle recall
  12/12; semantic interference 15/15 strictly correct with 0 false retrievals;
  latency 2.9–21.6 s with no length trend.
- **Uncalibrated engineering defaults**: the six λ rates, the 60/30 compression
  thresholds, the 20 % daily deletion probability, and every emotion parameter.

The 135 tests are **offline unit tests**. They prove the reference engine, the
reference adapter and the proactive injection path behave as specified. They do
**not** prove that your RAG or your 02:00 job is live.
`python scripts/demo_live_rag.py` (retrieval) and
`python scripts/demo_live_injection.py` (injection) run the real thing — both need
an embedding credential and network access, and neither is part of CI.

## Documentation map

| File | What it covers |
|---|---|
| [SKILL.md](SKILL.md) | The behavioural spec — read this first |
| [GUIDE.md](GUIDE.md) | When to do this, the six construction steps, acceptance gates |
| [references/design.md](references/design.md) | Rules, formulas, state machine |
| [references/integration.md](references/integration.md) | RAG / warehouse wiring contract and gates |
| [references/adapters.md](references/adapters.md) | Reference adapter: embedding backends, credentials, threshold calibration |
| [references/emotion.md](references/emotion.md) | L1 snapshot / L2 load / L3 appraisal |
| [references/state-ledger.md](references/state-ledger.md) | The state layer, veto list, failure modes |
| [references/context-budget.md](references/context-budget.md) | HARD/SOFT budget lines, cache prefix order |
| [references/evidence.md](references/evidence.md) | Measured numbers vs. engineering defaults |
| [INSTALL.md](INSTALL.md) | Installing into an agent, verifying, upgrading |
| [CHANGELOG.md](CHANGELOG.md) | Version history |
| [NOTICE.md](NOTICE.md) | Third-party notice (no third-party code in this package) |

## License

MIT — see [LICENSE](LICENSE). Third-party notice: [NOTICE.md](NOTICE.md).
