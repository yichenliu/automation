# Keel v2 architecture

Keel v2 rebuilds the platform around one idea: **an app is proven, not just generated.** Every app carries the owner's
needs as checks the platform can run. Every change must keep them passing before it goes live, whether the AI builder,
a person on the App map, or an outside agent made it. The builder doesn't write an app in one go. It searches: it makes
a small, typed design, proves it in a fast simulator, and repairs only what fails.

Why: rounds 25–32 showed a ceiling. The builder wrote the whole app in a few long passes, in a loose language, from
about 14,000 tokens of instructions. Its examples were written by the same model, and a slow checker with its own bugs
tried them. Round 31 had 11 root causes, and 10 were architectural:
- the test was wrong (2);
- the checker had bugs (3);
- the language let a meaningless rule look valid (3);
- nothing checked the whole app (1);
- a platform default was missing (1).
Only 1 was the model's own judgment.

## Layers

```
            owner · App map · outside agents (MCP / REST)
                              │
   app/api      FastAPI (ASGI) front: typed tools, REST v2, MCP, streaming; the Flask UI mounted inside
                              │
   app/builder  understand → needs and checks → data → behaviour → compile → simulate → repair by search → hand over
                              │                              ▲
   app/lang     Keel language: typed (Pydantic), patterns, patches, static and whole-app checks; compiles to the design
                              │                              │
   app/sim      simulator: the same engine on in-memory SQLite, with snapshots; milliseconds per check
                              │
   app/engine   runs the design: records, rules, stages, documents (SQL-side queries; rule index per version)
                              │
   app/runtime  web processes · worker processes (SKIP LOCKED queues, scheduler leader, email/SMS outbox) · AI gateway
                              │
   PostgreSQL (row security as a second lock) · object storage · self-hosted model servers (vLLM/SGLang; Ollama in dev)
```

## Decisions

**D1. Two levels: the Keel language and the design.**
- The design (`workspace.blueprint`, normalized by `app/blueprint/schema.py`) is what the engine runs. It stays, as the
  compiled form.
- The Keel language (`app/lang`) is what builders write. It is typed, has explicit paths, has patterns, and is edited by
  patches.
- The compiler is deterministic.
- A pattern instance is stored in the design under `features` with its parameters. Everything it made is tagged
  `"feature": key`, so changing a feature regenerates its parts and removing it removes them.

**D2. One typed definition.** Pydantic v2 models define the language. From them come:
- the JSON schema for the model's constrained output, one exact shape per patch kind (a discriminated union, not one
  bag of optional properties);
- validation that names the exact place and the fix;
- the REST and MCP tool schemas.

**D3. Patterns first.** Common business needs are patterns with parameters:
- stage alert, approval, chase, overdue, line totals, payments, limits, convert, document send, booking reminder,
  recurring, and stock from lines.
- A pattern compiles to the right rules every time and brings its own checks.
- The model makes about 10 large, checked choices instead of about 50 small, unchecked ones.
- Custom rules remain for what no pattern covers, with explicit paths (`this.order.status`).

**D4. Needs and checks first.**
- The owner's words become numbered needs, each a plain sentence of what the app will do, quoted from what they said.
  Nothing is invented.
- Every need has checks:
  - patterns write their own;
  - custom needs get typed given/when/then examples;
  - expected numbers are worked out by the platform from the arithmetic, never by the model.
- The Checks page is the definition of done.

**D5. A simulator, not throwaway databases.**
- Try-outs run the same engine against in-memory SQLite. The engine already works on rows in Python, and its SQL is
  kept portable.
- A copy is made once and restored from a snapshot for each check and each candidate.

**D6. A builder that searches.**
- Each step gets only the context it needs.
- Compile errors are repaired where they occur.
- Failing checks are traced to the pieces involved. N candidate repairs are generated in parallel, and the one that
  passes the most checks without breaking others is kept.
- The platform decides the defaults (views, pages, menus), so the model decides only what's specific to the business.
- The handover is honest, need by need. "Fix it" re-enters the same loop with the same checks.

**D7. A runtime built to scale out.**
- Web, worker and scheduler run as separate processes.
- Queues are claimed with `FOR UPDATE SKIP LOCKED`, and the scheduler takes a PostgreSQL advisory lock so only one
  runs.
- Rules change data inside the save transaction. Email and SMS delivery go through an outbox.
- Rules are indexed by (list, event) per design version.
- List queries, counts and sums run in SQL, not over a whole list loaded into Python.

**D8. An AI gateway.**
- Providers: Ollama (dev) and OpenAI-compatible servers (vLLM/SGLang: batching, prefix caching, fast constrained
  decoding).
- Priorities: an owner chatting comes before background work.
- Fairness between customers, a concurrency cap, and deadlines.

**D9. FastAPI front.**
- ASGI, with the current Flask app mounted inside (a2wsgi); routes move over one at a time.
- One typed tool layer (describe, explain, what-if, apply, test, undo) serves the builder, the in-app assistant, MCP
  and REST v2.

**D10. Tenancy and cells.**
- Row-level security as a second lock behind the code's workspace filters.
- Per-tenant filtering in the vector index.
- Files in object storage.
- Cells: many small customers share one, and an enterprise can have its own, running the same code.

## Work and status

| # | Work | Status |
|---|------|--------|
| 1 | Simulation database: the engine runs on in-memory SQLite (models portable, migrations PG-only) | in progress |
| 2 | Keel language: models, patches, compiler to the design, decompiler, static and whole-app checks | |
| 3 | Patterns, each with its own checks | |
| 4 | Checks language (given/when/then) and runner on the simulator; snapshots | |
| 5 | Builder pipeline: needs → data → behaviour → compile → simulate → search → hand over; the chat turn moves onto it | |
| 6 | Runtime: worker processes, SKIP LOCKED, scheduler leader, outbox, rule index | |
| 7 | Queries in SQL: filters, sorts, search, paging, totals, unique and limit checks, imports | |
| 8 | AI gateway: providers, priorities, fairness, deadlines | |
| 9 | FastAPI front, typed tools, MCP and REST v2, streaming | |
| 10 | Tenancy: row-level security, vector filtering, object storage | |
| 11 | Evals on the new builder | |
