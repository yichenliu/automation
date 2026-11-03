# The kit: every component an ERP is assembled from

The AI analyst never writes code. It assembles each ERP from the components below, which the platform renders and
runs. This is the complete list, kept in step with the code: if a component isn't here, the analyst can't use it.
The schema (`app/blueprint/schema.py`) defines and checks each one, `app/builder/prompts.py` teaches it to the analyst,
and its answer format lists every option, because the local model can only write what that format allows.

Status: ✅ built and covered by tests · ⚠️ built, needs a live account to verify · ❌ not built (see the end).

## Data: fields (36 types)

| Kind | Types |
|---|---|
| Text | text, long text, formatted (rich) text, email, phone, web address, postal address, barcode / QR (camera scan), tags (free labels), JSON (structured data) ✅ |
| Numbers | number (decimals, units), money (any currency; per-record currency, or its order's on a line: a USD purchase order's lines; conversion at your rates), percent (with a progress-bar display), duration, star rating ✅ |
| Time | date, date and time, time of day ✅ |
| Choice | single choice (coloured pills), multiple choice, checkbox, colour ✅ |
| Relationships | link to one record, link to many records, person, several people, dependent links (a contact must belong to the chosen customer) ✅ |
| Files | files, image, on-screen signature ✅ |
| Place | location (GPS from the device, map links) ✅ |
| Worked out | auto-number (gap-free), formula (a safe expression language, 40+ functions incl. dates, working days that skip weekends and the business's holidays, hours between times, currency conversion, padding for fixed-width files), total over linked records (count, sum, average, min, max, filtered), total over a whole table by conditions (like SUMIFS: the same item asked for this month), value from a linked record, value found by conditions (like XLOOKUP: the approver whose limit covers the amount, the price in force on the order date) ✅ |
| Kept by the platform | created time, modified time, created by, modified by ✅ |
| Actions | a button in every row that runs an automation ✅ |

Field options ✅: required, required only when a condition holds, unique, minimum and maximum, format rule (pattern with
its own message), default (including `today` and expressions), placeholder, help, shown only when a condition holds,
read-only (filled by automations or the AI), kept to a role or a team (pay rates, costs, buyer-only supplier ratings),
sensitive (bank accounts, tax file numbers: kept encrypted, shown masked as ••••6789, shown in full only on request to
people who may change the record and logged, never sent to the AI, emails, exports or the history), filled in by the AI
(optionally using the business's documents, and suggested in the form as it's filled in).

## Data: tables

✅ Modules (menu sections) and icons · line items edited inside their parent · record sections, shown as cards or tabs ·
a step-by-step form · rules every save must keep (checked with the record's lines, before anything is saved) · locks
(posted invoices: only admins can change them, chosen fields stay editable) · colour rules for rows and cells · who may
see and change the table, per role · which records each person sees (their own, or their customers') · fields kept to a
role · the portal (which records outside people see; a portal for each kind of outside person: clients and
subcontractors each see only theirs) · PDF documents (line items, statements whose lists reach through a link, several
lists with totals that add or subtract to a net: an owner's rent less expenses) · statement records of their own (an
owner's monthly statement: its lists and totals reach the owner's records, kept to the statement's period by filters on
`{{record.period_start}}`) · a public form · stages a record moves through by buttons (a workflow: Draft → Submitted
→ Approved; each move with who may make it (a role, a team, the record's creator, a person field or one through its
links, or the outside person in the portal), fields that must be filled in first, conditions, who may never make it
(separation of duties: whoever raised it, whoever moved it to an earlier stage such as Recommended, a person field or
a team; it binds the owner too), and whether they confirm their sign-in first; the owner can otherwise move a record
anywhere; automations aren't held to it) · no two records alike in some fields together (a supplier's invoice number) ·
changes that wait for approval (a supplier's bank account: someone else approves, never the person who made the change,
after a cooling-off period; the record's registered contact is told with a link to cancel it) · sealed records (bids:
nobody but whoever sent each one sees what's in it until the tender's closing time, admins and the AI included; nobody
changes it after, except the fields the business fills in then, like scores) · every opening of a record kept in its
history · a journey shown on each record across the business (Submitted → Approved → Ordered → Delivered → Paid, each
step done by its own stage or by the records that follow from it).

✅ Filters and conditions: equals, contains, empty, more or less than, one of, before or after today, within the next or
last N days, this or last week, month, quarter, year and financial year (its first month set in settings), changed (to) · "any" (or) and "all" groups, nested · through a link (an invoice's customer's account
manager) · "me" for whoever is looking (a rep's own sales) · rollups with filters give period totals (hours this week);
a rollup can reach through further links, or through a lookup (an owner's rent, through leases and properties);
a lookup can show a field further along (a rent payment's lease's property's owner), three links at most.

## Made for people who aren't technical

✅ Everyone starts at **Home**: what's waiting for them (each with who asked, how much and by when), big buttons for the
few things they start themselves (ranked by what people really add by hand), what they worked on lately, and their
pages; a new app's owner also sees four getting-started steps · every record says in one bar where it is and the one
thing to do now ("This request is waiting for approval. Waiting for you: Approve / Say no"; "Before it can move on:
someone else in Finance has to approve the run, because you raised it"), with the stage strip showing only the normal
path · a new record is sent on in one click ("Submit" or "Save for later"); nobody picks a stage from a list · each list
explains itself once ("This is where your requests are kept…"), keeps its filters behind one button, and becomes cards on
a phone · rules read as sentences ("When a timesheet is moved to Submitted: tell everyone: “Timesheet for [Team member’s
name]…”") with the details filled in each time shown as chips and a real example from the business's own records;
formulas read as words ("90% of Budget", "5 working days after Due date") · one vocabulary everywhere: To do, Rules, AI
helpers, What the AI knows, How it's set up, Suggestions, Change my app, "app" not ERP, "Say no" not reject, access as
"Can only look / Can do the work / Can also change the app" · statuses in words ("Waiting for an answer") · errors that
say what to do ("We can't find this page" · Go back) · the analyst names buttons and stages in everyday words.

## Screens: views of a table

✅ List (search, quick filters, sort, group with subtotals, totals, edit in place, select many, bulk change, bulk delete,
PDFs for the selected, paging) · board (drag between stages) · calendar (month, and week with times) · gallery · timeline
(Gantt, dependencies with late starts flagged, lanes per person, truck or room) · map · tree (hierarchies) · spreadsheet
(keyboard editing, paste from Excel, add rows by typing) · personal saved views · CSV and Excel export · CSV and Excel
import (the AI matches columns; matching rows are updated; one flat sheet fills linked tables: customers named on a row
are found or added with their details, each row can add a line, and the business's own numbers are kept; a sheet
attached in the builder chat is brought in this way, never typed out row by row) · recycle bin · calendar feeds for
Outlook, Google and Apple · compare side by side (two to eight records, from a list's selection or a record's related
list: what differs marked, lowest and highest numbers, their lines lined up by item with each one's total and the
lines one of them is missing: bids, quotes, a contract's versions).

## Screens: a record

✅ Sections or tabs · line items with live totals · related records with "add" · files · comments with @mentions,
shared with the portal when chosen · full history of every change · the automation runs it started (steps on a
branch's way indented) · approval and task cards, and the decisions made on it · the stage path with its move buttons
(a move that can't be made yet says why: "Fill in Amount and Quote file first") ·
action buttons · PDF documents · lock notice · colour · follow (be told of changes) · duplicate (with its lines) · print ·
previous / next ( `[` and `]` ) · invite to the portal (for customers and suppliers) · the journey strip · changes
waiting for approval (what it was and what it would be, who proposed it, when it can be approved) · sealed notices ·
sensitive values with Show · each file's fingerprint (SHA-256, checked every time it's opened; a file changed on disk
isn't served) · files can't be added to a locked record.

## Screens: forms

✅ Every field type's own input · line-items editor (rows appear as you type, totals live) · conditional fields (shown
or hidden, required, or read-only while conditions hold, live as you type, and kept to by the server) · sections shown
only while a condition holds · choices that follow another field (a Plumbing job's types) · the stage shown, not edited ·
searchable long lists · add a linked record without leaving the form · the AI's suggestion for a field as the form is
filled in (Suggest: from what's typed so far, with why; nothing saved) · sensitive inputs (blank shows the mask; type
to change) · file drop zones · a signature pad · camera
scanning · "where I am" · step-by-step forms · out-of-date save protection · AI document intake (PDF, photo, Word,
email) with "check this" highlights · public forms with a robot trap and rate limits.

## Screens: pages and dashboards

✅ A four-column layout where each widget takes 1–4 columns · heading · number (with comparison to the same point last
period) · goal · bar, line, donut, funnel and stacked charts · list · pivot table (rows × columns, with totals) ·
quick-add form · buttons (new record, open a view or page, run an automation, open a link) · activity feed · map ·
embedded report (Power BI, Looker, Google, video) · the AI's daily reading of the numbers · what's waiting for whoever
is looking (approvals, tasks, checks of the AI's work, changes to approve) · a period selector for the whole page (this
financial year too) · a PDF of the page · a home page per role or team · pages kept to a role or team.

## Automations

✅ Triggers: record added, changed, added or changed, deleted · a schedule (hourly, daily, weekly, monthly; for each
matching record) · a button · an email arriving · a file arriving in OneDrive ⚠️ · another system calling a webhook.

✅ Steps: update the record (or a linked one, or one made earlier in the run) · create a record · copy the record with its
lines (a contract's next version or renewal, a change order, a repeat order; linked back to the original) · create one
for each record or person in a field (an invitation per invited supplier, a score sheet per panel member) · create one per line
(of the record, of a record it links to, or of one made earlier in the run: a work order's product's bill of materials,
with quantities multiplied, going down into sub-assemblies' own bills of materials; only the lines that meet conditions) ·
update each related record · delete · assign in turn · wait (a time, until a date of the record, or until the record meets
conditions: until it's paid, at most N days) · notify (in the app and by email; the record's creator too) · send email
with PDFs, files or a dashboard attached ⚠️ · produce a document · ask for approval (the run waits; one approver or
several, any or all of them; reminders; passed up to someone else when nobody answers; what a "no" does before the run
stops; who may never approve it; confirming sign-in first) · a task for a person (the run waits until it's done, with the fields they fill in to finish it) · branch (the
first way whose conditions hold, and an "otherwise"; ways can branch again) · run another automation (a shared
sub-flow, for the record, the one made earlier in the run, or a linked one) · produce and email the PDF of the record made earlier in the run (the invoice a finished job became) · call another system and keep its answer · AI: fill a field, read attachments, turn an email, file or webhook
data into a record with its lines (each can use the business's documents).

✅ Each step can have its own condition; runs are atomic per step group, keep every step's result, pick up where they
were after a wait or a decision (inside a branch too), wait for the AI when it's away, stop loops, and tell admins when
one stops on an error.

✅ Who a step is for: a role, a team (Accounts payable), the record's creator, its outside person (in the portal), a
person field, one through its links (the cost centre's manager), or one found by conditions (the holder of the
delegation limit that covers the amount, from an Approval limits table).

✅ Approvals and tasks page: what's waiting for you (approve or reject with a comment, finish a task filling in what it
asks, pass it to someone else, approve or turn down changes waiting for approval), what's waiting for others (and why
you can't answer some yourself), and what's been decided. People can't approve, through their role, what they asked
for themselves (the owner can); a step kept apart from people binds everyone. Away: each person (or an admin for them)
says who covers their approvals and tasks from one day to another; what's asked of them meanwhile goes to the cover.

## AI inside every ERP

✅ The assistant (looks up totals and lists in the records, and passages in documents, mail and records; cites its
sources; proposes changes for a click) · what the AI knows (documents, attachments, OneDrive folders ⚠️, mail ⚠️, every
record as text; found by meaning with bge-m3 and by words) · AI fields · AI steps · document intake · AI column matching
· the page commentary widget · the analyst reads the business's documents while designing.

✅ AI helpers in any process (AI at work page): each AI step is a helper with one job, shown in plain words with the
process it's in, an on/off switch, who checks its work, how it's doing, and whether it learns. Jobs: write or fill in
a field (a statement of work, a reply, a summary, a category, the matching record), check a record against rules or
reference lists (a quote's lines against the supplier's contract prices: a verdict such as Pass or Issues, what it
found line by line, and the process branches on it), read attached files into the record and its lines (a quote), and
make records from emails, files or data sent in. Each reads the record, its lines and the records it links to, and as
set: its attached files, chosen reference tables (filtered to the record: this supplier's prices), the business's
documents. Switched off, a person gets the same job as a task with the same fields and the process carries on; when the
AI can't do a checked job it hands it to that person with its reason. Set up in place (what it does in plain words,
what it reads, who checks, learning), tried on a real record or a pasted example before saving (nothing saved, nobody
asked), added in four choices (which records, when, what job, who checks), and a job people do by hand can be handed to
the AI. People's corrections are shown to it next time (its last three). The record shows what the AI did on it, how
sure it was and why, and who checked it.

✅ AI doing the work, people checking it where it matters: every AI step (read a document or email into a record, fill
or sort a field, match a record such as the invoice a payment is for, draft a reply) says whether a person checks its
work every time, only when the AI isn't sure (plus 1 in 10 of its sure answers at random, so its record stays honest),
or never. The AI says whether it's sure and why. A check shows what the AI put (unsure fields highlighted) for a person
to confirm or correct; turning it down drops the answer (or bins the record it made) and stops the run; a record the AI
made waits to start its own automations until it's checked. Each AI step keeps a track record (checked, right,
corrected, wrong, done without a check) on the automations page, where an admin sets how much it's checked; the analyst
suggests letting it run on its own once it's been right nearly every time (the lower bound of its share right at least
90% over 20 checks), and checking every one again when people keep fixing it. The analyst decides at design time: the
AI drafts and a person checks whatever leaves the business or commits it; everyday internal work in volume is checked
when the AI isn't sure; harmless enrichment isn't checked; decisions of authority stay with people.

## People and security

✅ Owner, admin, editor, viewer and portal roles · teams (from the design; people put in them under Settings › People) ·
invitations · table, record and field permissions everywhere (by role or team)
(screens, search, exports, the assistant, AI look-ups, the API) · two-step sign-in (authenticator apps) · sign in with
Microsoft ⚠️ · password reset links (from an admin, or `python -m app.tools reset-link`) · confirming sign-in (the authenticator code,
or the password, within the last ten minutes) for the moves and approvals that need it · separation of duties ·
sign-in throttling · CSRF on
every form · a strict content security policy · encrypted tokens · hashed API keys · out-of-date save protection ·
full change history.

## Integrations

✅ The portal: outside people (customers, suppliers, subcontractors; a portal for each kind) see their own records, and
records that reach them through what they link to (a tender's invited suppliers see its published answers), message
the business, add records with their lines (a bid, an invoice), change the fields left to them while a condition
holds (until the tender closes), make the moves meant for them (Acknowledge, Accept), answer what's asked of them
(sign the contract), keep their own details up to date (a bank account change waits for the business's approval, and
"This wasn't me" cancels one), and, where allowed, join by themselves (their record starts at its first stage for the
business to verify).

✅ REST API with keys (list, filter, sort, page, read with lines, create with lines, update with If-Match, delete) ·
webhook addresses that start automations · outgoing webhooks (any method; answers saved) · calendar feeds · Excel and
CSV · Microsoft 365 mail and OneDrive ⚠️.

## Documents

✅ PDFs (line items, statements, several lists with totals) · files in another system's layout: a header, a line per
record listed and a footer, each a template (a bank's fixed-width payment file, a CSV for the accountant); sensitive
values are written in full, so such a file is made when someone with access downloads it, never kept, and its making is
logged with its fingerprint.

## The platform underneath

✅ Records stored as JSON (any table shape without database changes) · versions on every record · all-or-nothing saves
· rules before commit · gap-free numbering · locks · a background worker (builder, assistant, jobs, automations,
schedules, Microsoft 365, indexing, notification emails, daily backups) · work that waits and retries when the local AI
is away · migrations · daily backups (database, files, key) · the design versioned with one-click undo · an installable
web app (add to the home screen).

## The analyst

✅ Designs from a description, a spreadsheet, or the business's Microsoft 365 ⚠️ · builds big requests in up to five
parts in one turn · fills in shorthand where only one reading fits · checks every change and sends every problem back to
the AI to fix (twice) · then keeps whatever fits, cutting as finely as it can (one lock, field, view or block, not the
whole table), and has one more go at what it cut · writes the reply last, from what was really built · asks for a
smaller part when an answer is too long · knows the proven patterns (stock as movements, double-entry books, locking
what's sent, restricting pay and costs) · tested on twelve businesses with the real model (`evals/`).

✅ Keeps learning each business: nightly suggestions from how the ERP is used (fields nobody fills in, text that should
be a choice, defaults, hand-made follow-ups turned into automations replayed on the business's history, stuck records,
failing automations, repeated questions, recurring email), and a weekly review of the whole ERP by the analyst; apply
(undoable) or dismiss under Improvements.

## ❌ Not built yet

These are deliberate gaps, listed so nothing is assumed:

- **Accounts with outside services:** card payments on invoices (Stripe), SMS, Xero and QuickBooks sync, Google Workspace.
  The API and webhooks connect to them today, but there's no ready-made connector.
- **Languages:** the screens are in English; dates show as 3 Nov 2026.
- **Offline use:** the app installs on a phone but needs a connection.
- **Smaller pieces:** swimlanes on boards, merging duplicate records, printing barcode labels, steps that run in
  parallel inside one automation (several approvers at once is built; other parallel work goes in separate automations),
  a drawn diagram of an automation (it's shown as an indented tree), email verification when outside people sign up by
  themselves (the business verifies them before trading instead).
- **Procurement connectors:** e-signature (DocuSign), e-invoicing (Peppol), punchout catalogues (cXML, OCI), and ready
  connectors for SAP and Oracle; the API, webhooks and file documents connect to them today.
- **A marketplace of businesses:** each ERP is one business's (with its portal for outside people). Several buying
  businesses sharing one supplier base, catalogue and verification team, with anonymised market figures across them, is
  a different product.
- **Exact decimal money:** amounts are worked out in floating point and rounded to cents; fine for an SME's books, not
  for bank-grade ledgers.
- **Domain calculations that need more than formulas:** FIFO stock valuation, payroll tax tables, consolidation across
  companies. The analyst can model the data; the calculations would need new components.
- **Enterprise scale:** PostgreSQL with pgvector, filtering inside the database, stored calculated values and more
  worker processes, for hundreds of thousands of records per table.
