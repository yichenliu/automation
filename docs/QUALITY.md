# How the builder gets apps right

People who aren't technical build their business app by describing it. They can't spot a broken rule or a total that
adds GST twice, so the platform has to. Six layers do it, from the cheapest to the most thorough.

## 1. Prevention: what the AI is told

`app/builder/prompts.py` (HOW TO WORK, "Before you send your answer, check it…") lists the mistakes the try-out keeps
catching, so the model avoids them in the first place. When the try-out finds a new kind of mistake, it gets a line
there.

## 2. Corrections: mistakes with one sensible reading, put right as the design comes in

`app/blueprint/ops.py` (`_resolve_sense`) fixes these before anything is saved, and lists each fix in what changed:

| Mistake | Correction |
|---|---|
| Moves for "more than 10%" and "less than 10%" (exactly 10%, or none, gets stuck) | the lower one becomes "10% or less"; a discount defaults to 0 |
| A stage no button leads to | Cancelled gets a "Cancel" button wherever work is still going; another gets a button from the stage before it that leads nowhere |
| A field required to equal two things at once | either one ("any") |
| A choice spelled differently from its option ("completed") | the option's spelling |
| Approvals in turn written inside the first one's "if they say no" | lifted out, one after another |
| A step setting the new record's stage on the record that started the rule | "target": "created" |
| An approval with nothing after it, so an approved request stays "Submitted" | the step that moves it on, filling in Approved by and the date (read-only from then on) |
| An approval rule on "moved to Pending" when new records start at Pending (so it never runs) | it also starts when one is added |
| "Over $20k asks the owner; under is sent": the approved way stops at the approval | it then does what the other way does |
| A stage button and a rule's button with the same name, one skipping the approval | the rule's button only |
| People titled by their first name ("Emma") | their whole name |
| A duration of 30 with "minutes" as its unit (kept in hours: 30 hours) | half an hour |

On today's 11 eval designs these corrections took the mistakes found from 10 to 2, and the stories passed from 21/24 to 24/24.

What the platform guarantees whatever the AI designs (found by the beginner testers, `tests/test_wave1.py`):

- While a record waits for an approval, its own buttons are the approval (Approve is a yes, Reject a no, for whoever
  may answer); nobody else can move it on, and a New form's main button only ever sends a record on, never approves,
  rejects or closes it. Cancelling it withdraws the approval.
- What people entered survives a design change: a link that now points to another list finds (or adds) the record of
  the same name there; a field that was worked out and is now kept keeps what it showed; undo puts it back.
- A rule that stops half way puts what it had made in the bin (no empty $0 invoices) and says so.
- A change to who can see a list or field is the first thing the builder's reply says; when the try-out found
  something still wrong, that comes before what the AI says it did, and the reply never opens with "Fixed".
- The AI is never asked while the database is held (a half-saved write would block every other save for minutes), and
  work a failure left half done goes back in the queue instead of showing "working…" for ever.

And from the second wave (a food wholesaler, a furniture maker, a property manager, `tests/test_wave2.py`):

- A move that needs something filled in first ("Delivered" needs the delivery date) asks for it beside its button; it's
  never hidden, and onward moves always come before Cancel.
- What was approved stays approved: once a record is approved (or goes straight through under a limit), its totals can't
  go above what was approved except by whoever approved it, the owner or the rules. Sending it back for approval starts
  it over. Money that's meant to change later (paid, received) isn't held.
- Whoever is asked to approve a record can open it, even where people see only their own (finance approving a rep's order).
- A rule can name a record by what it's for (`{"product": ..., "warehouse": ...}`: this product's stock at that
  warehouse); one that's only a pairing of others is added the first time it's needed.
- A person field that decides who sees a record, or says who raised it, is filled in with whoever adds it when they leave
  it empty, so nobody's own order vanishes from their list.
- A record named after one of its links is named by its other links too ("Olive oil · Welshpool"), so pickers never
  show the same name twice.
- A change that works out money on records people already have differently (a GST fix changing a sent invoice) is said
  first in the reply and kept in each record's history; Checks counts rules that stopped while people used the app.
- A rule can go through the records of something shown from another record ("the bill of materials of the work order's
  product", a lookup from its order), or a path through links ("order.product").
- A rule waiting for a field "changed to" a value people choose as they add a record (a quality check saved as Fail)
  also starts when one is added that way.
- The builder never adds a record that's already there (a recipe's legs a second time doubled a quote), and undoing a
  change puts the records it added in the bin, unless people have used them since.
- An example (the builder's or an owner's check) can't do by hand what it checks the app does: steps that add what
  its expectations count up, after the move or button that should make them, are left out unless its words say a person
  adds them. A check that took stock out by hand and "passed" now fails until the app does it.
- When the owner says some people mustn't see something and the design keeps nothing from anyone, the try-out counts
  it as not done, and the reply says so.
- Checks never says "Everything checked works" with nothing checked, and a check that couldn't be written stays on the
  page with why. Every list keeps an "All" tab; the same document made twice is kept once.
- A percentage's default and limits are kept as percentages (a default of 10 is 10%, never 1000% on every new form).
- "Only their own" can name teams who still see every record (Finance doing the books), and the try-out finds a team
  that adds records whose link they can't fill (a bookkeeper with no leases to pick); a list kept from someone says
  who it's kept to instead of "moved or deleted".
- A rule rewritten in a change keeps starting when a record is added if it did before (rent stopped counting new
  payments when "added" quietly became "changed").
- A few loose words that can't be built get one plain question with choices back, not "couldn't fit"; a part that
  fails twice in a row stops the "Carry on building" loop and asks for a smaller piece.
- An approval says why it asks ("Over $500"); a step that changed nothing isn't announced; a rule that emails people
  outside the business can only be run early by someone who runs the app; a record known by a number is named by what
  it links to ("L-0003 · Danny Cooper"); a page named like a list is called its overview.

And from scoring what was handed over, not first drafts (`tests/test_handover.py`). Most apps that weren't right when
handed over had the builder's own worked examples failing for reasons that weren't the app's, so the owner saw "2 of 5
worked" on an app that worked, and the fix rounds risked changing a right app to fit a wrong example:

- An example plays out as people would: someone it names who isn't in the copy ("the team lead, Priya") joins it as a
  stand-in, so whoever tries it isn't everyone; "approve" on a record with its own Approve or Sign off button presses
  it; what the approver writes goes with their answer; an email is "sent" in the copy (it reaches nobody) and the rule
  carries on.
- An example's own mistake is called that, and the builder corrects the example, not the app: "approve" where nothing
  ever asks for an OK, or where the rule decided this one didn't need it ("Under $5,000"). Steps take durations as
  01:30:00 too.
- An approval named for the one person kept from it (a team lead approving their own request) goes to an admin and says
  why; it never waits for ever. A rule after a "no" can record why: "{{reason}}" is what the approver wrote.
- Hours between two times of day work (a shift from 09:00 to 12:00 is 3, 22:00 to 06:00 is 8), so timesheets and shifts
  add up.
- An app that converts other currencies asks for exchange rates in Getting started, and the try-out tells the builder to
  say so, not leave totals empty. Stock movements are typed as counted (2 sold is 2) with In or Out, never a minus.
- Dates stay on the calendar when an example lets days pass: an invoice issued on the 10th is due on the 24th however
  long it then waits. "Run" is only for scheduled rules (running one that starts by itself did it twice: two fees).
- An OK given from To do moves the record on as its Approve button would, when the rule doesn't (apps built before the
  builder added that step). A stage's own "Approved by" and "Approved on" are filled in by its button.
- A rule that adds a record without something it must have (a child with no last name) is found when the design is
  read, with the detail to fill in, not only as "the rule stopped" once tried.

And from looking at every app as a beginner would (`evals/ux.py`, `tests/test_eval_scores.py`):

- Screens use the app's own words: "Choose what to change…" (not "Set a field…"), Rules (not automations), lists (not
  tables), and the quick-jump names match the menu. A step that hasn't run yet says what it will do, never its code name.
- The builder's replies have no code names (bid_lines is "bid lines"), and it's told the words to use: list, details,
  rule, stages, app. When its try-out still finds something wrong, it says which everyday cases didn't come out right,
  by name; the numbers behind them are in the details under the reply, never pasted into it.
- A long form shows what must be filled in, what already is and the first eight details; the rest wait under "More
  details (optional)". Lists turn into cards on a phone.

And for consistency: the same request should come out the same, run after run (`tests/test_handover.py`,
`tests/test_eval_scores.py`):

- The builder's own work asks the model with less chance (AI_BUILDER_TEMPERATURE 0.3, against 0.6 for the app's own AI)
  and a fixed seed (AI_BUILDER_SEED), so a request gets the same design each time it's asked.
- Mistakes the model makes only sometimes are caught every time, by reading the design: a rule that makes a record only
  on some of a branch's paths ("an invoice only when the job has a quote"); one that adds a record without what it must
  have. Two kinds of record that link to each other (a job's quote, the quote's job) are kept in step by the platform,
  so a rule reading either side finds the pair.
- A condition comparing two of a record's own numbers by name ("on hand less than reorder_level") compares them, and
  says so in words ("less than its reorder level").
- `--repeat N` reports consistency: how many businesses came out the same every run, which were right only sometimes,
  and which stories flip between runs.

## 3. The try-out: every change is tried before it's handed over

`app/builder/trial.py` makes a throwaway copy of the app (its design and people, none of its records, nothing that reaches
outside) and:

- reads the design for mistakes (`app/builder/checks.py`): a choice that isn't one, a stage nothing reaches, a stage a
  record gets stuck at, conditions that can never hold, an approval only asked when someone says no, a rule putting one
  kind of record where another belongs (a staff member into a payment's customer);
- adds a record to every list, takes each one along its stages (onward before any stop, filling in what a move asks
  for, and saying where a stage can only be left by cancelling), presses every button, runs every scheduled rule, makes
  every document and opens every screen (as the owner, and again as someone in each team), and notes every rule that
  stops; then does it again with only the required
  fields filled in, as people do (a rule copying what's usually left empty into what can't be empty only fails then);
- checks sums that can't be right for any business (a discount bigger than the subtotal, tax counted twice, a total that
  leaves out the discount);
- acts out the AI's **worked examples**. For each change the AI writes 1–4 examples of the business's ordinary day ("a
  wedding quote for 80 guests with 15% off": discount $756, GST $428.40) and the platform compares what the app shows with
  what was expected. Examples are kept and played again after every later change, so nothing that worked gets broken.

What's wrong goes back to the AI to fix (two rounds at most). A fix that makes things worse is undone. The owner sees
"I tried it out" under the reply: each example with its real numbers, ticked or crossed. Anything still not right is said
plainly in the reply, and never claimed as done.

## 4. Every app keeps its own checks

Each app's worked examples are its checks (Set up → **Checks**): the builder adds them for what it builds, and the owner
adds their own in plain words ("a quote for $1,000 with 10% off comes to $990 with GST"), which the AI turns into a check
and tries at once. They're run in a copy of the app (`app/builder/checking.py`):

- after every builder change (part of its try-out);
- after any other change to the design: an undo, a suggestion applied, a rule turned off, an edit by hand;
- after the platform itself is updated (the worker notices new platform code when it starts and checks every app).

When something that worked stops working, whoever runs the app is told what, after which change, and how to go back.

    .venv/bin/python -m app.builder.checking      # every app's checks against the current platform code

## 5. The eval: does it do what each business asked?

`evals/analyst.py` builds 17 businesses with the real model, then:

- **stories** (`evals/stories.py`): the business's own asks acted out against whatever was built. "Under $2,000 needs one
  approval, over $20,000 three", "a quote's lines less 15% plus GST", "a sent invoice can't be edited except to record
  payment", "a rep sees only their own customers' orders";
- **vague requests** (the `vague*` scenarios): half a sentence, typos, "like Xero but easier", "make it simpler", "the
  invoice thing isn't working". A good analyst asks a few plain questions when it can't tell, builds a small sensible start
  when it can, never deletes what's there because a request was vague, and answers without jargon;
- **the try-out**, as above, counting what's wrong;
- **right when handed over**: the share of built apps that, when the builder says they're ready, do what was asked (every
  story and owner's check passes), have nothing wrong when tried out, show no errors and left nothing out. This is the
  number that matters most: it's what the owner gets;
- **kept working**: the stories are acted out after every turn too, and a story that worked before a later change and
  doesn't after it is a change that broke what was right. That should be none;
- **beginner's eyes** (`evals/ux.py`): every screen the owner and an ordinary staff member (one per team) can reach, the
  builder's replies and what the app says when it won't do something, read the way a beginner reads them. **Stuck**: a
  menu link that doesn't open, code on the screen, a reply that says it's ready when its own try-out said not; any one
  and the app isn't right when handed over. **Confusing**: a word they don't use (table, field, workflow, automation,
  ERP; unless the owner used it), a code name, a date written 2026-11-03. **Ideas**: a form asking for more than 15
  things, a list wider than 8 columns (a phone), a menu longer than 14, a reply over 160 words. No AI, so runs compare;
  the totals list the most common of each, which is where the platform's screens need work next;
- **first draft right**: the share of build turns where the try-out found nothing wrong and every example was right
  *before the builder's own fixes*. A side number: fewer fix rounds make builds quicker and leave less to go wrong, and
  only first drafts that were right are learned from (docs/FINETUNE.md). A builder that catches and fixes its own
  mistake before handing over has still done its job.

    .venv/bin/python -m evals.analyst --out ~/keel-evals/run --repeat 3         # the model varies: 3 runs each
    .venv/bin/python -m evals.analyst --regrade ~/keel-evals/run                # score saved designs again (no model)
    .venv/bin/python -m evals.analyst --regrade ~/keel-evals/run --resolve      # …as today's corrections would save them

## 6. From real use

- **"Something not right here?"** at the foot of every page: anyone says what they expected. The owner sees it under
  Suggestions, and "Ask the builder to fix it" makes it a builder request. The fix is tried out with an example of the
  problem, and that example is kept, so the same problem is checked after every later change.
- **Training data**: every builder turn's conversations with the model, and how they turned out, are kept locally
  (`app/builder/training.py`). The good ones become a fine-tuning set (docs/FINETUNE.md).

## Release checklist

1. `.venv/bin/python -m pytest -q`: all pass.
2. The eval with `--repeat 3`: right when handed over, kept working, stories and things wrong no worse than the last
   release (keep the totals in the release notes).
3. The beginner tester (`evals/beginner.md`): an agent playing a non-technical owner builds a medium-sized business app
   from scratch on a copy of the platform and reports every place they got stuck.
4. `.venv/bin/python -m app.builder.checking`: every app's own checks still pass on the new platform code (the worker
   also does this by itself after an update, and tells each app's owner what broke).
5. Every new kind of mistake found gets a correction (2) or a check (3), a line in the prompt (1), and a test.
