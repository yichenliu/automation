# Keel: an ERP built by an AI analyst

Describe how your business works, or let the platform read your Microsoft 365. The AI analyst designs your ERP
(tables, links, totals, line items, screens, dashboards, PDF documents, public forms and automations, including AI
steps), and the platform turns that design into a working system straight away. Change anything by asking; undo any
change with one click. The AI runs on this computer through Ollama, so your records and documents never leave it.

```
brew install postgresql@18 pgvector    once: the database (PostgreSQL, with pgvector for the AI)
./start.sh setup      once: the platform's own PostgreSQL in data/postgres (port 5433), its address put in .env
./start.sh            start it (http://localhost:5050), and its PostgreSQL: the web, and the worker beside it; logs in data/logs
./start.sh install    start it automatically when you log in to this Mac, and restart it if it stops (one process)
./start.sh status | restart | stop | backup | uninstall
.venv/bin/python -m app.worker                               a worker of its own (as many as you like, on any computer)
WORKER=0 .venv/bin/uvicorn asgi:app --workers 4 --no-proxy-headers   several web processes, the worker(s) beside them
.venv/bin/python -m app.database import-sqlite data/erp.db   bring an app made before PostgreSQL across (once)
.venv/bin/python -m app.tools reset-link you@example.com    a link to choose a new password
.venv/bin/python -m pytest -q
```

Settings live in `.env` (see `.env.example`): the database's address (`DATABASE_URL`, written by `./start.sh setup`),
the AI's model servers (Ollama on this computer, or vLLM or SGLang on GPU servers), timezone and currency, and the
Microsoft 365 app's details.

## The database

PostgreSQL only, with pgvector (`app/database.py`). The platform runs its own on this computer: kept in
`data/postgres`, listening only on this computer (port 5433), with a login of its own. Records are JSONB with a GIN
index; the AI's passages have a pgvector embedding (an HNSW index for each vector length, cosine distance) and a
full-text column (PostgreSQL's English search, titles weighted above text) with a GIN index. The builder tries each
change out in a throwaway PostgreSQL database of its own, as the tests and evals do; it's dropped after.

## What's in it

**Three ways to start an ERP.** Describe the business in the builder chat; drop in the spreadsheet you run things from
(it becomes tables, and its rows are imported); or connect Microsoft 365 and let the AI read your OneDrive spreadsheets
and the mail that keeps coming, then propose an ERP for you to approve.

**The kit the AI builds with.** Each piece is the platform's own, so every ERP looks and works consistently. The
complete list (36 field types, 8 views, 16 widgets, 9 triggers, 15 automation steps, and everything around them) is in
[docs/KIT.md](docs/KIT.md), with what isn't built yet.

- **Fields:**
  - text, long and formatted text, numbers, money (in any currency, converted at your exchange rates), percent,
    durations, star ratings, dates, dates with times, checkboxes;
  - single and multiple choice, email, phone, web address;
  - links to other tables, people on the team, files and images;
  - automatic numbering (INV-0001);
  - formulas, totals over linked records, and lookups.

  Fields can be required, unique, limited by minimum and maximum, shown only when a condition holds, kept to admins
  (pay rates, costs), or filled in by the AI.
- **Rules and locks:** rules every record must keep, checked with its lines before anything is saved ("debits equal
  credits when posted", "the end can't be before the start"); records that lock once sent, posted or approved, so only
  an admin can change them (with chosen fields, such as payment status, still editable).
- **Permissions:** owner, admin, editor and viewer roles, plus per table (Payroll for admins only), per record (sales
  reps see only their own deals) and per field. Screens, search, exports, the assistant, the AI's look-ups and the API
  all keep to them.
- **Line items:** tables edited inside their parent (invoice lines, quote lines), with live totals.
- **Views:** list (search, filters, sorting, edit in place, bulk changes, totals), board (drag between stages),
  calendar, gallery and timeline.
- **Record pages:** sections, line items, related records, files, comments with @mentions, full history, automation
  runs, approvals, buttons and PDF documents.
- **Dashboards and reports:** numbers, bar, line and donut charts, goals, lists, notes and pivot tables (sales by
  customer by month, amounts owed by age).
- **PDF documents:** invoices, quotes and orders, with your business details, logo and colours.
- **Public forms:** a link anyone can fill in; each answer becomes a record.
- **Import and export:** CSV or Excel import, where the AI matches the columns; CSV export; everything as one zip.
- **API and webhooks:** keys for other systems (a web shop, a bank feed, payroll, a reporting tool) to read, add and
  change records with the same rules, locks and permissions as people; webhook addresses that start automations with
  whatever another system sends.
- **Automations:**
  - Triggers: a record is added or changed, a schedule, a button, an email arriving, a file landing in a OneDrive
    folder, another system calling the automation's webhook.
  - Steps: update or create records (one for each line, too), notify people, email with PDFs attached, produce a
    document, ask someone to approve (the run waits), call a web address, and AI steps that fill a field, read
    attachments, or turn an email or file into a record with its lines.

  Every step can have its own condition, and every run is listed step by step.
- **AI inside every ERP:**
  - an assistant anyone can ask about their records and their business; it looks the answer up first (totals and lists
    from the records, and passages from documents, mail and records found by meaning), cites what it used, never
    guesses numbers, and the changes it proposes wait for a click;
  - what the AI knows: documents uploaded for it (policies, price lists, terms, manuals), files attached to records,
    OneDrive folders, the mailbox, and every record written out as text; the AI steps of automations can use it too
    ("draft a reply quoting our price list");
  - "From a document": upload an invoice, receipt or photo and the AI fills the record and its lines for you to
    check;
  - AI-filled fields, which never overwrite what a person typed.
- **People:** invitations, an approvals inbox, notifications (in the app, and by email if wanted), two-step sign-in,
  sign in with Microsoft, password reset links, and a portal where customers or suppliers see their own records.
- **Everyday tools:** ⌘K to jump anywhere, personal saved views, follow a record, duplicate, print, a recycle bin,
  Excel export, PDFs for many records at once, and calendar feeds for Outlook, Google and Apple.
- **An analyst that keeps learning each business:** every night it looks at how the ERP is really used, and admins
  find its suggestions under Improvements:
  - take off fields nobody fills in;
  - turn typed-in text that repeats into a choice list;
  - start a field at the value people almost always pick;
  - automate what people do by hand straight after a change (make the invoice when a job is done);
  - nudge records that have sat untouched;
  - fix automations that keep failing;
  - answer what people keep asking the assistant;
  - handle the kinds of email that keep arriving.

  Once a week the analyst also reviews the whole ERP. Each suggestion shows its evidence, and an automation is
  replayed on the business's own history first ("someone made the invoice by hand 10 of 12 times; this would have
  done it"). Applying one is a design version that can be undone; a dismissed one isn't suggested again.

## How it's built

- **The design (blueprint)** is one versioned JSON document per ERP. The AI never writes code: it sends operations
  that the platform tidies, checks and applies all together or not at all (`app/blueprint/`, `app/builder/`).
  - Shorthand the model often writes is filled in where only one reading fits: a lookup that names its link but not
    the field, a timeline without its end date, "make the lines" after making an invoice, a schedule without its table.
  - When something doesn't fit, every problem goes back to the AI to fix (twice).
  - What still doesn't fit is cut as finely as possible: one lock, field, view or dashboard block rather than a whole
    table or page. The rest is saved, and the AI gets one more go at just the parts that were cut.
  - The reply is written last, from what was actually built, and says plainly what wasn't.
- **The analyst's evaluation** (`evals/`): twelve businesses, from a solo cleaner to a food wholesaler with
  double-entry books, sent through the real builder on the real model. Each design is scored, then exercised (a record
  in every table, every screen, page and PDF opened). Run `.venv/bin/python -m evals.analyst --out DIR [scenario …]`;
  it takes about an hour on a local 27B model.
- **Records** live in one table as JSON, so any table of any shape works without a database change, and a design
  change never touches data. A removed field is only hidden, and going back to an earlier version brings it back. Every
  change to a record is kept (`app/engine/records.py`). Saving is safe for an ERP:
  - a record and its lines are saved together or not at all, and only once the table's rules hold;
  - every record has a version, and a save made from an out-of-date copy is refused instead of overwriting someone
    else's change (the screens say so; the API answers 409);
  - automatic numbers are taken in one statement, so they never repeat, and a refused save leaves no gap.
- **What the AI knows** (`app/knowledge.py`): text is cut into passages, each with a vector from a local embedding
  model (bge-m3 through Ollama, multilingual) kept in pgvector, and its words in PostgreSQL's full-text index. Look-ups
  combine both rankings, keep to what the person asking may see, and fall back to words alone if the embedding model is
  away. Fields kept to admins never go into the index, and when the design changes how records read (a field renamed,
  or kept from some people) they're read again.
- **Automations, jobs and the worker:** the web (uvicorn) and the worker run as processes of their own, and more of
  either can run at once (`./start.sh` starts one of each; `python run.py` alone, with `WORKER=1`, runs the worker's
  threads inside the web for development). The worker does builder turns, the assistant, jobs (imports, reading
  documents, discovery, AI fields), rule runs, the email and text outbox, indexing for the AI, and the scheduler's work
  (schedules, connected apps, watching Microsoft 365, daily backups); `WORKER_KINDS`, `BUILDER_CONCURRENCY` and
  `JOB_CONCURRENCY` say which and how many. All work is in the database, and if the AI can't be reached, work waits and
  retries (`app/worker.py`, `app/jobs.py`, `app/engine/automations.py`).
  - Each item is claimed before it's worked on (`SELECT … FOR UPDATE SKIP LOCKED`, with a lease the worker renews while
    it works), so nothing is done twice, and a process that stops leaves its work to the others once the lease runs out
    (`app/leases.py`).
  - Only the process holding a PostgreSQL advisory lock is the scheduler; if it stops, another takes over. Each
    schedule's time is taken by one row in `schedule_run`, so it runs once, and only workspaces whose schedules are due
    are read (`app/scheduler.py`).
  - Emails and text messages a rule sends go into an outbox in the rule's own save; the worker delivers them, trying
    again later if the mailbox can't be reached, and the run's history says what happened (`app/outbox.py`).
  - The rules a save starts are found in an index of the design's rules (by list and trigger, per design version), and
    sign-in and API rate limits are counted in PostgreSQL, so every web process shares them.
- **Security:**
  - passwords hashed, and sign-ins limited after repeated wrong passwords (behind a proxy, set `TRUST_PROXY=1` so
    the limit counts the real address; otherwise forwarded addresses are ignored);
  - roles checked on every page; CSRF on every form;
  - a strict Content-Security-Policy (no inline scripts);
  - Microsoft tokens encrypted;
  - uploads size-limited and type-checked;
  - public forms rate-limited, with a robot trap.
- **Backups:** daily copies of the database (pg_dump), files and the key for stored sign-ins, in `data/backups` (the
  last 14 are kept). To restore: unzip a backup into an empty `data` folder, `./start.sh setup`, then
  `pg_restore --clean --if-exists --no-owner -d <DATABASE_URL without +psycopg> database.dump`.
- **API** (`app/web/api.py`): `/api/v1/tables`, `/api/v1/tables/<table>/records` (list with filters, sort and paging;
  add with lines; change with `If-Match`; delete), and `/hooks/...` for webhook automations. Keys are made in
  Settings › API & webhooks; only their hash is kept.
- **Upgrades:** `app/migrate.py` applies database changes once each, in order (recorded in `schema_step`).

## Scale

On this Mac, an ERP with 30,000 records answers in about 0.1 s for lists, 0.15 s for a record, and 0.6 s for a busy
dashboard. The AI's look-ups take about 30 ms, and bge-m3 reads about 25 passages a second; finding by meaning stays
under 10 ms up to 200,000 passages. It runs on PostgreSQL with pgvector, so many people save at once without
waiting on each other, and finding by meaning goes through an HNSW index rather than memory. For larger scale the next
steps are:

- moving filtering and sorting into the database (records are JSONB with a GIN index ready for it);
- storing calculated values instead of working them out on each view.
