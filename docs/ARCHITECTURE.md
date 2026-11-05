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

**D9. FastAPI replaces Flask entirely.**
- v2 has no Flask. The core (engine, builder, language, simulator, runtime) depends on no web framework:
  - plain SQLAlchemy 2 sessions instead of Flask-SQLAlchemy's `Model.query`;
  - a settings object instead of `current_app.config`;
  - context variables instead of `flask.g`.
- FastAPI (ASGI, uvicorn) is a thin layer over that core:
  - pages (the same Jinja templates, through Starlette);
  - sign-in sessions and CSRF protection;
  - REST v2, MCP and streaming.
- While pages move over, the routes not yet moved stay mounted inside FastAPI (a2wsgi), so the app works at every
  step. The last step deletes Flask, Flask-Login, Flask-WTF and Flask-SQLAlchemy.
- One typed tool layer (describe, explain, what-if, apply, test, undo) serves the builder, the in-app assistant, MCP
  and REST v2.

**D11. One registry, and the AI learns new components by itself.**
- Every piece of the language registers itself (`app/lang/registry.py`): field kinds, triggers, steps, patterns and
  patches.
- Each one carries its shape, a one-line description, a short example, how it compiles and, for patterns, its own
  checks.
- Generated from the registry on every run, so a new component needs no prompt edit:
  - the catalog in the builder's instructions;
  - the output schema the model is held to;
  - the App map's palette;
  - the MCP and REST tool schemas.
- A guard test fails the suite if a registered component has no description or example, if its example doesn't
  compile on a sample app, or if its checks don't pass in the simulator.
- When the registry changes, every app's checks run again (checking.after_platform_update).

**D10. Tenancy and cells.**
- Row-level security as a second lock behind the code's workspace filters.
- Per-tenant filtering in the vector index.
- Files in object storage.
- Cells: many small customers share one, and an enterprise can have its own, running the same code.

## Work and status

| # | Work | Status |
|---|------|--------|
| 1 | Simulation database: the engine runs on in-memory SQLite | done |
| 2 | Keel language: models, patches, compiler (exact: `ops.apply(strict=True)`), the app as the builder reads it | done. It covers lists and fields (with conditions, who may see them, sensitive), stages, rules (record, button, schedule, email and file triggers; set, add, add each, update each, notify, email, document, task, approval, AI fill/extract/create/check, assign, copy, delete, run, webhook, payment link, wait), validations, views, documents, records, imports, teams and access, and a `design_op` escape for pages, forms, portals, flows, syncs and themes |
| 3 | Features with their own checks; the registry; the guard test | done: 13 features |
| 4 | Checks: told, not-equals, arithmetic, relative dates, staff-then-approver moves | done |
| 5 | Builder: needs → data → behaviour → checks → simulate → best of several fixes → need-by-need handover | done; every turn, discovery and the analyst's suggestions are written in the language |
| 6 | Runtime: worker processes, SKIP LOCKED, scheduler leader, outbox, rule index | done |
| 7 | Queries in SQL (planner) | done: on 200,000 records, a list page goes from 1.8 s to 0.1 s and search from 4.1 s to 0.3 s |
| 8 | AI gateway | done |
| 9a | Core free of Flask (database, settings, who is acting) | done |
| 9b | FastAPI: typed tools, MCP and REST v2, streaming | done |
| 9c | The pages on a Starlette web kit, and Flask removed | in progress |
| 10 | Tenancy: row-level security, per-workspace vector search, object storage; cells | done |
| 11 | Evals on the finished platform | after 9c |
| 12 | The first builder deleted | done (dc3a1ce) |

## Adding a feature

1. Write its class in `app/lang/patterns.py`:
   - decorate it with `@register("feature", <an example>)`;
   - give it `do: Literal["<name>"]` and its parameters;
   - write a docstring saying in one or two sentences what it does for a business. The AI reads exactly this.
2. Write `build(d, where)`. It returns a `Made`: the fields, rules and design ops it needs. Raise `Problem` with what's
   wrong and what's there instead.
3. Write `checks(d)`: worked examples, in the try-out's format, that prove it works.
4. Run `pytest tests/test_lang.py`. The guard test applies your example to a sample app that has every list, runs its
   checks in the simulator, and fails if anything is missing or wrong.

Nothing else is needed. The builder's catalog, the output schema and the MCP tools pick the feature up on the next
run.
