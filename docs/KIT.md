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
step done by its own stage or by the records that follow from it) · combined lists, put together from several others
and kept up to date by the platform (a stock outlook from stock movements, confirmed purchase lines and open order lines,
each with its own values and conditions; it follows them as they're added, changed, deleted or brought back, reads like
any list, and nobody changes it by hand).

✅ How a field shows, set once for every screen: a choice's colour (by hand, or by meaning and position), text before or
after a value ("ABN", "pcs"), a number coloured by its sign (or the other way round for costs), what yes and no say
("In stock" / "Out of stock").

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
helpers, What the AI knows, Versions, Suggestions, Change my app, "app" not ERP, "Say no" not reject, access as
"Can only look / Can do the work / Can also change the app" · statuses in words ("Waiting for an answer") · errors that
say what to do ("We can't find this page" · Go back) · the analyst names buttons and stages in everyday words.

## Screens: views of a table

✅ List (search, quick filters, sort, group with subtotals, totals, edit in place, select many, bulk change, bulk delete,
PDFs for the selected, paging) · board (drag between stages) · calendar (month, and week with times) · gallery · timeline
(Gantt, dependencies with late starts flagged, lanes per person, truck or room) · map · tree (hierarchies) · spreadsheet
(keyboard editing, paste from Excel, add rows by typing) · inbox (the list beside the open record, worked through with
↑ ↓ or j k; what's done in it shows in the list straight away) · personal saved views · CSV and Excel export · CSV and Excel
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

## On the floor: flows, labels and scanning

✅ **Flows**: work done a screen at a time on a phone or at a bench (receiving a delivery, picking an order, a stock
count, a return), started from a record's button while it's at the right point or from "Step by step" in the menu,
kept to the teams it's for. Steps: read (with the record's details), fill in, scan items in (each code found by its
barcode, number or name; the same twice counts two, or is refused for serial numbers), count lines off to their target
(scan or tap + and −; a line turns green when it's all there; a short count asks why), take a photo, sign, decide
(buttons that set a field and can end the flow there), print. Nothing is saved until the last step; then the record
gets what was filled in, scanned items become its lines, counts go on its lines, photos and signatures are kept with it,
labels print, and the flow's own rule steps run with what was entered. Left part way, it waits to be carried on.
The builder makes them; every try-out does each flow once.

✅ **Work queues**: a task for a team or role is a queue: the most pressing first (low to urgent, fixed or read from
the record), "Next task" takes the next one, "Take it" keeps it from the rest of the team ("Give back" returns it), "Not
now" puts it off till a day, and a task can be done through a flow (starting it opens the flow; finishing the flow does
the task).

✅ **Team menus (spaces)**: a team whose work is a few screens of a big app (the warehouse, the drivers) sees just
those, grouped as they work (Receiving, Picking, Stock counts: a list, a view of it, a page or a flow), with their useful
links (SOPs, a carrier's site), and can switch to everything; the owner and admins keep the whole menu.

✅ **Labels**: any record's label (its name, a few details the reader may see, and its barcode, or a QR code that opens
it), for label printers (62 × 29, 50 × 25, 62 × 100 mm, 4 × 6 in shipping) or sheets (A4 21, Letter 30); a list's own
label designs (which details, which barcode, one per box received); many at once from a list or a flow.

✅ **Printing straight to a printer**, without a print dialog: labels and documents go to the printers set up on the
Mac the app runs on (a Brother or Dymo label printer, the office laser), or labels to a thermal printer on the network
(Zebra, Honeywell, SATO) in its own language (ZPL), with the label stock it has loaded; a test print under Settings ›
Printers. A flow's labels can print by themselves as soon as it's saved.

✅ **Scanning**: a camera button beside search opens whatever a code is (one of the app's labels, a barcode, a record's
number); a USB or Bluetooth scanner typing into search does the same; barcode fields scan with the camera.

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

✅ Owner, admin, editor, viewer and portal roles · teams (from the design; people put in them under Settings › People and teams) ·
invitations · table, record and field permissions everywhere (by role or team)
(screens, search, exports, the assistant, AI look-ups, the API) · two-step sign-in (authenticator apps) · sign in with
Microsoft ⚠️ · single sign-on with Google Workspace, Okta, Auth0, Keycloak or any OpenID Connect provider (linked by
the provider's own identity, never the email it claims; PKCE; the ID token's issuer, audience, nonce and expiry checked) ·
signing people out after the minutes chosen without use (shared computers on the floor) · an audit log of what matters
(sign-ins and refused ones, people, roles and teams, exports and backups, private values revealed, API keys, every
change to how the app works, printers), each entry chained to the one before so a change or a gap afterwards shows,
downloadable as CSV · a health page (the local AI, background work, rules that stopped, the screens' speed and errors,
storage and backups, each with what to do about it) · password reset links (from an admin, or `python -m app.tools reset-link`) · confirming sign-in (the authenticator code,
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

✅ **Ready-made connectors** (Settings › Connections; details checked with the app, kept encrypted): **Shopify** and
**WooCommerce** (orders with their lines, customers, products and stock brought in on a schedule, matched by the app's id
so a changed one is updated, not added again), **Xero** and **QuickBooks Online** (signed in to; invoices, bills and contacts
sent with their lines; invoices, contacts and payments brought back), **Stripe** (a payment page on each invoice; the
invoice marked paid when Stripe says so, by its signed event), **EasyPost** (shipping labels and tracking numbers from
Australia Post, Royal Mail, UPS, FedEx, DHL, USPS and more). Any rule can send to a connected app with its sign-in
added, one item per line where it needs them.

✅ An MCP server (`/api/v1/mcp`): Claude, ChatGPT, Cursor, Copilot and other AI assistants list the tables, find, read,
add and change records with an API key's role and the app's rules and locks; a viewer key only reads · the API described
in OpenAPI 3.1 with the app's own tables and fields as types (`/api/v1/openapi.json`), for code generators and BI tools ·
REST API with keys (list, filter, sort, page, read with lines, create with lines, update with If-Match, delete) ·
webhook addresses that start automations · outgoing webhooks (any method; answers saved) · calendar feeds · Excel and
CSV · Microsoft 365 mail and OneDrive ⚠️.

## Documents

✅ PDFs (line items, statements, several lists with totals) · files in another system's layout: a header, a line per
record listed and a footer, each a template (a bank's fixed-width payment file, a CSV for the accountant); sensitive
values are written in full, so such a file is made when someone with access downloads it, never kept, and its making is
logged with its fingerprint.

## The platform underneath

✅ PostgreSQL only, run on this computer by the platform, with pgvector: records as JSONB with a GIN index, the AI's
passages with an HNSW vector index and PostgreSQL full-text search, each try-out of a change in a throwaway database of
its own · records stored as JSON (any table shape without database changes) · versions on every record · all-or-nothing saves
· rules before commit · gap-free numbering · locks · a background worker (builder, assistant, jobs, automations,
schedules, Microsoft 365, indexing, notification emails, daily backups) · work that waits and retries when the local AI
is away · migrations · daily backups (database, files, key) · the design versioned with one-click undo · an installable
web app (add to the home screen).

## Ready-made parts

✅ Four parts to start an app from, or add to one (Set up › Versions › Add ready-made parts), each tried out like anything
the builder makes, and fitting together (one list of products for all; what joins two parts comes when both are there):

- **Inventory & warehouse**: products with what's on hand (every movement kept: received, sold, moved, counted, damaged,
  returned, made, used), places (warehouses, bins, vans, quarantine) with labels, batches and use-by dates, what's running
  low, stock counts on a phone (scan the shelf; a finished count corrects the stock), and moves between places.
- **Orders & fulfilment**: customers, orders with lines and totals, picked by scanning each item against the order
  ("Pick and pack"), packed, shipped with the carrier and tracking number, packing slips and address labels, returns (an
  item returned undamaged goes back on the stock), and a "to pick" inbox.
- **Procurement**: suppliers, purchase orders with lines, approval for big ones (never by whoever raised it), the order
  as a PDF, and the delivery received by counting it in (a short delivery asked why; what was counted goes on the stock).
- **Production**: what each product is made of, work orders with their materials on a board, and finishing one puts what
  was made on the stock and takes its materials off.

## The App map: the whole app drawn, and changed by hand

✅ **App map** (Set up › App map): how the app works, worked out from its design (never drawn by hand, so it can't
disagree with what the app does; app/appmap.py), written for someone who knows nothing about building apps. **How work
flows** shows each **process** down the page as numbered steps, one list each (a quote, then its job, its invoice, its
payment). Between two steps, the line says in a sentence how work gets there: "People add a quote for an enquiry
themselves", or "When a job reaches Signed off, an invoice is made automatically and the job moves on to Invoiced"
(with what else must be true: "if its balance is at most $0.00"). On each step: its stages with how many are at each now
(in order, ›, when the list has stages of its own; a plain Status is only a choice of where one is), how people move them
on (by hand: changing their Status, or the buttons they press), what it does back to an earlier step (a payment marking
its invoice paid),
where work comes in (a website form, a portal customers add through, emails), what else happens automatically there
(a button people press is marked **By hand**; a daily check says so), who's involved, where the AI helps, what's waiting,
and anything the checks found. Everything the checks found, and any rule that stopped with a problem lately, is in one
box at the top. Lists the work looks things up in (customers, products) and ledgers several processes add to (stock
movements) sit below as **Also in your app**. A process can be **renamed** as the business calls it ("Quote to cash"),
and **Try an example** acts out one of the app's examples in a throwaway copy, told as a story and numbered on the
stages it went through. A key at the top says what the marks mean; the browser's Back goes back through what was opened. Click a step for its list in full: how one moves along (each stage, who presses what to move it
on, what happens automatically when one gets there), where they come from, what happens automatically, where the AI
helps, its step-by-step screens. **Looking is safe**: nothing on the page changes the app until someone presses a Change
button (Change the stages, Change this rule, Change what it keeps) and save; then a bar says they're changing it until
they press Done. A name is only saved when they press Save (or Enter), never by clicking away; switching a rule off and
removing a stage or a choice ask first. Stages are changed where they're shown: renamed in place, dragged into order,
removed, or a new one added after any other ("Add a stage after On site"; put between two, it leads on to the next), on a list with stages of its own or one whose Status is a plain
choice (its choices, never a second Status). **Make something happen automatically** starts a rule in plain words:
when a job reaches a stage, is added or changed, or has a button pressed, tell the owner, the admins or everyone in the
app, or email me (offered once the app's email is connected), said back in one sentence before it's made. The
AI box above it gives an example in the app's own words ("when a job reaches Signed off, tell me"). The old Data model
and Processes addresses lead here.

✅ **What each list keeps** (the App map's other view): every list as a card of its columns (a link to another list
marked, and what's kept with each one in other lists: "their enquiries, quotes, jobs"), with **Add a column** on each
(also from a list's own page, More › Add or change columns); a column clicked opens it, and the Status column a list's
stages are in points to where they're changed; or, at the press of **Diagram**, every list drawn as a box with its columns and a
line from each link to the list it points at (dashed for several): drag a list to move it (kept for everyone), pan and
zoom, show everything, tidy up, download it as a picture. A list clicked opens beside them, read first: **Change what it
keeps** to change it. Admins change it there: a new list; rename a list or what one record is called, or its menu
section; add a column (any kind people fill in, a link to another list, a sum worked out); rename one, change its help,
whether it must be filled in or be unique, kept private, its decimals, unit, currency or starting value; put the
columns in order by dragging; add, remove, rename and order a choice's options; link two lists by dragging one's **+**
onto the other (in the diagram); name records by a column; remove a column or a list.

✅ **Stages, rules and flows** (opened from the App map, read first: **Change** to change them). **Stages** drawn top to bottom,
each with how many records are in it and the rules that run when one gets there, with an arrow for each button that
moves a record on (straight down to the next, round the right to one further on, round the left back, one arrow in for
a move from many stages); add, rename, order and remove stages, set where new ones start, and add or change a move
(its button, from where to where, who may press it, what must be filled in first); give a list stages of its own, or
take them off. **Rules** drawn as a flowchart: when it starts, what it checks, each step, a branch's ways side by side
(each labelled with when records go that way), an approval's "if they say no", and on each step how often it ran,
was skipped or stopped in its last runs. Admins change the trigger and conditions, add a step anywhere (a **+** between
any two), change, move or remove one (telling someone, an email, a task, an approval, changing the record or adding
another, waiting, giving it to someone, a branch, a document, an AI step, sending to another system), add and change a
branch's ways, rename, turn on or off, or delete the rule. AI steps (filling in a field, checking a record, reading a
document, making a record from an email) are marked AI, with who checks their work (every time, when the AI isn't sure,
or nobody) and how often they've been right when people checked, and that's set there too. **Try it on a record** lights up what the rule would do for
one of the list's records, step by step, without doing any of it (the way a branch would go, a step it would skip and
why, what waits for an answer). **Step-by-step flows** drawn screen by screen; rename, add (instructions, fields to
fill in, a photo, a signature), edit, order and remove screens.

✅ On the App map, a box to ask the AI for anything else in words, and every change: checked like the builder's (an app
with a problem from before can still be changed, but not made worse), saved as a version, said in plain words with
**Take it back**. Nothing people entered is thrown away by a click: a field keeps its kind while it has values (text and
notes, number and money, date and date-and-time can swap), a choice or stage in use can't be removed (rename it instead),
and renaming a choice or stage renames it on every record and in every rule, condition, move and flow that names it
(taking it back renames them back). People who aren't admins see the diagrams, of what they may see. Every build's
try-out opens the App map and each diagram in it.

✅ All joined up: a change in a studio is the design itself, so the rules, screens, forms, documents, API, MCP server,
the builder and the assistant work from it at once (tested end to end in tests/test_connected.py). A renamed stage keeps
its rules firing; a field renamed or kept from some people is read again by the AI's search (a private one leaves it at
once); the builder is told of every change made by hand and keeps it; stages added on the map are a field in what it keeps; nothing writes to a combined list but the lists it's made from.

## Where things are

✅ The app's menu has **Set up** (for the team, not portal people) in three parts. **Your app**: App map (the processes and
the lists behind them; stages, rules and step-by-step flows, with each rule's last runs, its address for other systems
and Run it now), Versions (every change, going back, the test copy, ready-made parts). **AI**: AI helpers (every job the AI does,
in processes and fields: add one, switch it off so a person does it, let it take over a job done by hand, choose who
checks it, try it on real records) and What the AI knows. **Keep it right**: Checks and Suggestions. Then Settings, in
three parts: **Your business** (business details, appearance, people and teams), **Connected** (connections to
apps and accounts, the API and AI assistants, public forms, printers) and **Looking after it** (the AI on this computer,
data and backups, security, the audit log, health). Each list's page links to it in both studios; each studio links to
the other; ⌘K jumps to any of them.

✅ AI in a process, configured by people: on the App map an AI step is added like any other (fill in a field, check a
record, read a document, make a record from an email), switched off so a person does it as a task (and back on), given
a job people did by hand (a task with one field), and set to be checked by a person every time, when it isn't sure, or
not at all; in what a list keeps a field can be filled in by the AI, or stop being. The AI helpers page lists them all,
with how often each was right when checked.

## Trying changes safely

✅ **A test copy**: the app copied with its records (links pointing at the copies), numbering and people (not outside
people), to try changes on by asking the builder or by hand, with a banner on every screen saying it's a copy. **Publish
to the live app** says what changes (lists, fields, choices, stages, rules, flows, pages, menus) and what that does to the
live app's records (records missing a field that must now be filled in, records on a choice that's gone, values no
longer shown but kept), warns when the live app has changed since the copy was made, and brings only the design across
as a new version, undoable in one click. Records made in the test copy stay there.

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

- **Accounts with outside services:** SMS and Google Workspace (mail, Drive). The API and webhooks connect to them today,
  but there's no ready-made connector.
- **Languages:** the screens are in English; dates show as 3 Nov 2026.
- **Offline use:** the app installs on a phone but needs a connection.
- **Smaller pieces:** swimlanes on boards, merging duplicate records, steps that run in
  parallel inside one automation (several approvers at once is built; other parallel work goes in separate automations),
  changing on the App map the steps it has no form for (copying a record, going through each line, reading a
  file with the AI: asked of the AI in words instead), email verification when outside people sign up by
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
