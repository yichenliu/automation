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
   app/api      FastAPI (ASGI) front: typed tools, REST v2, MCP, streaming; the pages (app/web: FastAPI routers,
                with app/web/http.py)
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
- Local or cloud, swapped without a restart (app/ai_providers.py). The catalog has Ollama, your own OpenAI-compatible
  server, and the cloud providers OpenAI, Anthropic, Gemini, Azure OpenAI, Mistral, Groq, DeepSeek, xAI, OpenRouter,
  Together, Fireworks, Cerebras and Cohere, with Voyage for embeddings.
  - Three protocols cover them: Ollama's, OpenAI's chat completions and Anthropic's messages, which carries adaptive
    thinking, effort, JSON output and a cached system prompt.
  - Each purpose (everyday, builder, quick jobs, search by meaning) goes where a platform admin chose under Settings › AI.
    That choice is stored in `platform_setting`, with keys Fernet-encrypted, and every process reads it within 5 s.
    Without one, the .env decides (AI_PROVIDER, AI_MODEL, ...; *_BUILDER, *_FAST, *_EMBED; OPENAI_API_KEY and the
    like). A cloud provider given no model uses the newest of its leading family that the key can list.
  - What a model refuses is learned, not hard-coded per model (`_negotiate`). Each optional part of a request is a
    ladder of ways to send it: max_completion_tokens or max_tokens; temperature, top_p and seed; thinking by
    reasoning_effort, a chat-template switch or Anthropic's thinking and effort; the JSON schema as json_schema, then
    json_object plus the schema in the prompt, then the prompt alone. A 400 that names a part steps it down a rung, and
    that's remembered for that server and model. A length cap named in a refusal is learned the same way. So a new
    model or provider quirk works without code.
  - How hard a model thinks is AI_EFFORT (xhigh unless set), stepped down a level at a time where a model lacks one.
    Ollama doesn't refuse a level a model lacks, so each model's levels are read from /api/show instead (Qwen 3.8: off,
    low, medium, xhigh). "think": true alone is the model's default, medium. There's no thinking budget: every model is
    asked for the same length, AI_MAX_TOKENS (128,000 unless set), thinking and answer together, said outright so no
    server's smaller default applies. A model that allows less names its own limit in the refusal, and that's learned;
    a server that names only its context is sent no length. Runaways are stopped by loop detection, per-step ceilings
    measured from real runs (build.THINK_LIMITS) and the deadline.
  - Search by meaning stays on this computer unless chosen elsewhere. Changing its model reads the knowledge again.
  - A key turned away, or an account out of credit, waits like a server that's down: the work carries on once fixed.
  - Recorded builder calls say which provider and model wrote them, so cloud answers can be kept apart from the
    platform's own model's when choosing training data.
- Priorities: an owner chatting comes before background work.
- Fairness between customers, a concurrency cap, and deadlines.

**D9. FastAPI and Starlette, used directly.**
- The core (engine, builder, language, simulator, runtime) depends on no web framework: plain SQLAlchemy 2 sessions
  (`app/core/database.py`), a settings mapping (`core.settings`), and context variables for the app and who is acting
  (`core.context`, `core.actor`). The platform object (`app/platform.py`, made by `create_app()`) holds the config, the
  database engine, the log and the Jinja environment, and opens that context for workers, scripts and tests
  (`app.app_context()`).
- One FastAPI app (`app.asgi`, built by `app/api`'s `front()`; uvicorn serves it) is the whole web: REST v2, streaming,
  and every page, the v1 API, MCP, webhooks and /static as FastAPI routers in `app/web`. There is no web kit or
  framework-shaped layer between them and the views.
- `app/web/http.py` adds only what pages need beyond FastAPI and Starlette, written once:
  - `PageRoute`, every page router's route class: each request served in the platform's context (its settings, a
    database session of its own, who is acting), with its session read from the signed cookie, and finished the same
    way (the cookies, the security headers, the time for Settings › Health, an error as its page, JSON under /api/);
  - the session, signing in (`signed_in`, `login_user`), CSRF (`check_csrf`), messages (`flash`), forms and uploads
    (`form_of`), addresses by name (`url_for`, in a script too), and answers (`render`, `redirect`, `json_response`,
    `send_file`, `raise Redirect(...)`);
  - `install()`, which puts the routers into the FastAPI app.
  docs/WEB.md is the guide to writing a page with it. Tests and try-outs make requests with `app.test_client()`
  (`app/web/testing.py`, on Starlette's TestClient).
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

**D12. The language core, written for the AI (v3).** Rounds 33–37 showed the builder failing at the seams of the language,
not at the business: of 29 owners who described their business, 3 apps were right at handover. Five classes of
failure were the language's: a "can't … until …" held back only staff's buttons (13 apps); rules about other records
had no way to be said (every round a new feature); time passing in a try-out didn't run the daily rules (12 apps); a
rule stopped on a must-have it couldn't know (10 apps); stages nothing could reach (7 apps). So:
- **One formula language, which can look at other records.** The records linking to one (`client.invoices`,
  `job.payments`) and whole lists (`bookings`), with `count`, `sum`, `avg`, `min`, `max`, `any`, `all`, `none`, `first`,
  `last` and `where`; `this` is the formula's own record; `overlaps()`, `days_since()`, `days_until()`. It's what the
  model already writes (SQL, spreadsheets). Names inside a count(...) are checked against the counted list, and words
  compared with a choice must be one of its choices.
- **Lengths of time are written as people write them.** `returned_at > due_back_at + 2h`, `confirmed_at < now() - 48
  hours`, `hours_since(sent_at) > 48`; a plain number with a date stays days, as in a spreadsheet. Round 38 had two
  builders write `+ 2` and `- 48h` for hours with no way to say it. A condition's value written as a formula without
  braces (`"now() - 48h"`) is worked out, not compared as text.
- **The AI is told every function.** The catalog's FORMULAS block comes from `expr.SAID`, and a test fails for a
  function it doesn't name, as it does for a piece of the language without a docstring. The design checks catch the two
  time mistakes a formula can't refuse: a date set from `today()` at every save (it moves each time), and a rule its
  own name says is about hours that counts days.
- **What the AI writes is read as it means it, where only one meaning is possible** (round 39: four of seven apps that
  never came right were stuck on refusals the builder repeated through every fix). A formula in a condition's field
  (`"field": "days_since(due_date)"`) is that formula; a patch sent inside a design_op is that patch; `cond ? a : b`
  is `if(cond, a, b)`; a rule on a schedule that names no list is about the one list that has what it checks; a rule
  or a feature taken away by the other's name goes; a stage added where it already is is nothing to do. A feature's
  rule can be changed with change_rule, and is then the writer's own, as one written anew already was.
- **Make before break.** What a need's patches take away waits while what they make for it is refused, and goes only
  once that's made. A fix that took every rule away to start again, with its one new rule refused, left the dance
  studio's app with none.
- **Round 40's, the same way.** A formula that works out a date (`confirmed_at + 2h`) is a date whatever it was left
  showing, so features can count from it; `client.name` in a rule about clients is that client; `owner` is the
  business's owner where a person goes; a gate can wait on a worked-out field coming out right; a validation is taken
  away by a part of its check; a chase counts from a date on a list with no stages; `join()`. A feature taken away
  leaves the fields a rule made from it (now someone's own) still reads. A try-out's wait of no days moves no dates.
- **Round 41's.** A feature made again keeps what it made itself (an approval took its own “Done on” for someone else's
  and dropped it, and a chase reading it blocked every stage change after); a field another rule or feature reads is
  never dropped with a feature. The behaviour step is told it may make a list a need requires. A percentage written out
  of a hundred is kept as a fraction in the language too; a formula field repeating one the list has is refused (a model
  stuck repeating itself made fifteen). The day a record got to a stage reads “Unpaid since”, never “Unpaid on”.
- **Round 42's.** A rule's date worked out from a date left empty (sent_on + 3 before it's sent) is left as it is, not
  set to 3 and the rule stopped; a field named in a rule about no list is said, not a crash that failed the turn.
- **Round 43's, with the builder thinking.**
  - **Pressing a button refused:** a "press, refused" step for a button the record doesn't show yet (Send on a Draft
    quote, before its Approve) is refused as it stands. It isn't walked through the stages between.
  - **Numbered names:** "cr2" is the second record the example added of that list, not the latest.
  - **Dated checks:** a kept check is marked with the day it was written. Its dates written out in full are read on
    that day's calendar, moved on to today's, so it stays true on later days.
  - **Runaway thinking:** a step whose thinking runs away is answered without thinking, not asked again to think the
    same way.
  - **A fix that changes nothing:** it asks the owner what should happen, instead of offering Fix again.
- **Round 44's, with the builder thinking.** Each of its four apps that never came right was the platform's fault, not
  the builder's, so no ask could have put it right:
  - **On the day itself:** a reminder 0 days before a date asked for one within 0 days that was also after today. It's
    the day itself now.
  - **A date the app works out** (due 14 days after it's issued) can't be written into an example. A wait can now be
    `{"do": "wait", "record": q1, "until": due_on, "days": n}`, and the overdue and reminder features' own checks use it.
    Before, the overdue check waited 3 days for an invoice due in 14.
  - **A refused move is tried as it stands:** the try-out attached a signature the move needs before pressing it, even
    for a move meant to be refused, so "not released until signed off" was signed off by the try-out itself.
  - **allocate** (a step, and a feature): a link filled with records picked from a list, such as a driver and two
    helpers from the staff, free that day, the least used first. `assign` only gives a record to people who log in.
  - **"Only I can sign off", tried by staff,** is the app stopping them when the approver's move is there for the owner
    only. Before, the try-out said nothing asks for an OK, and the owner's check couldn't be tried.
  - **Thinking:** the understand step stops at 30,000 characters of thinking (it never needed 15,000 when right) and
    answers without it. The fix search's candidates are recorded with the turn: each thread had started from an empty
    context.
- **Conditions are formulas too.** Anywhere a condition goes (only_if, blocked_if, a gate's needs, a total's only),
  text is a formula that must hold; `{field, op, value}` stays as the App map's point-and-click form.
- **What must not happen is a rule every save keeps.** A gate is table rules (with `holds` for what must hold), kept
  by the owner, staff, the app's own rules and imports alike; its buttons still say what they wait for. A rule refuses
  a save that breaks it, or that touches what it's about while it's broken; a change beside the point (a note on a job
  started before the client fell behind) is kept.
- **The platform works out what the builder used to keep consistent by hand.** Every stage gets a button that reaches
  it, and none with stages after it is a dead end (`schema.complete_moves`). A record a rule adds without a must-have it
  couldn't know is added for people to finish, and they're told; the rule never stops on it.
- **A try-out's time passes like the app's.** Each day of a wait, the rules that run on a schedule run, so a check says
  "wait 14 days", never which rule to run.
- Apps made before are made again on the next platform update (`patterns.refresh`): their gates become rules, and
  their unreachable stages get buttons.

**D13. AI-first and embedded: one language, one AI, and the reasons why.** The platform is built for an AI that lives
inside it and builds there, not for a chat feature on the side.
- **One way to change an app.** The Keel language is how apps change, for every editor: the builder, the App map, the
  analyst that suggests improvements, ready-made modules, and outside agents (REST v2, and MCP for an owner's or admin's
  key: `keel_describe`, `keel_what_if`, `keel_apply`, `keel_test`, `keel_undo`). Keel makes small changes as well as
  large ones (set_field, order_fields, remove_stage, order_stages, add_move, change_move, remove_move, remove_stages,
  add_step, change_step, remove_step, move_step, change_rule enabled). Where the App map already had tested logic for a
  change, Keel compiles to it, so a click and a patch do the same thing. The design is what Keel compiles to.
- **One AI with different jobs.** The builder makes and changes the app. The in-app assistant answers about records,
  and when someone asks it to change how the app works, it hands the request to the builder for someone who runs the
  app, or says who can. The analyst suggests improvements in Keel.
- **The AI can ask why.** A check that comes out wrong carries a trace in the app's own terms: the rules on the list
  and what each found, or which rule sets the field and what it was waiting for, with the record's values. The fix step
  and the tools are given it.
- **It starts from what works.** Recipes (app/builder/recipes.py) give owners' common needs, each with the Keel that
  makes it, and every recipe compiles in the tests. Builds verified by the owner's own checks are added to them. The
  builder is shown the closest few, matched by meaning.
- **It builds to how it will be tried.** How each need beyond keeping records will be tried is written before what makes
  it, and the behaviour step is shown it.
- **What it builds scales with the business.** A formula that looks across a list is answered by the database where it
  can be: narrowed by SQL, counted and added up there, with every record counted whoever is saving. Every record keeps
  its values as equality compares them (`record.norm`, set on every save), and one GIN index over them answers a rule's
  "equals" first; the exact test then runs on what it finds (never fewer records than match). One save with an overlap
  rule and a count rule over 200,000 bookings: about 0.4 s.
- **Every rule reads back into Keel** (app/lang/back.py): the App map's rule editor, the ready-made parts and the
  analyst all write rules in Keel. A feature's rule rewritten by someone becomes theirs, and the feature leaves it be.
- **explain** (a tool for the AI and outside agents): why a record is where it is, and why a check comes out as it
  does.
- **The web layer is invisible to the AI.** FastAPI on Starlette serves the screens, REST and MCP directly (D9): each
  page is a FastAPI route, with `app/web/http.py` adding only the session, sign-in, CSRF, messages and addresses. The
  tool schemas come from the same Pydantic models as the language.

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
| 3 | Features with their own checks; the registry; the guard test | done: 15 features |
| 4 | Checks: told, not-equals, arithmetic, relative dates, staff-then-approver moves | done |
| 5 | Builder: needs → data → behaviour → checks → simulate → best of several fixes → need-by-need handover | done; every turn, discovery, the analyst's suggestions (its detectors' too) and the ready-made parts are written in the language. Left outside it, as design ops, until it can say them: a purchase order's approval never by whoever raised it and back to Draft when turned down (`modules.unsaid_for`), and how much a person checks an AI step change_step can't reach (`learning._checking`) |
| 6 | Runtime: worker processes, SKIP LOCKED, scheduler leader, outbox, rule index | done |
| 7 | Queries in SQL (planner) | done: on 200,000 records, a list page goes from 1.8 s to 0.1 s and search from 4.1 s to 0.3 s |
| 8 | AI gateway | done |
| 9a | Core free of Flask (database, settings, who is acting) | done |
| 9b | FastAPI: typed tools, MCP and REST v2, streaming | done |
| 9c | The pages on a Starlette web kit, and Flask removed | done (the kit itself deleted in 9d) |
| 9d | The pages on native FastAPI routes, and the web kit deleted (docs/WEB.md) | done: every page a FastAPI route (app/web/http.py); app/webkit deleted, the platform object is app/platform.py |
| 10 | Tenancy: row-level security, per-workspace vector search, object storage; cells | done |
| 11 | Evals on the finished platform | next: v2 is complete |
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
