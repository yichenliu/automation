# keel.so, studied page by page (3 Nov 2026)

What keel.so offers, from its site (home, Platform, Tools, Tasks, the three use cases, demo, the Bravely case study,
About, the Ops in practice articles), all 187 pages of docs.keel.so (release notes to 0.480, 9 Sep 2026), and its
39-minute product tour. Each part says where we stand. Screenshots were taken in a browser and looked at, not just read.

## What keel is

"The operations platform for fast moving teams": a **code-first** backend (a `schema.keel` file of models, actions,
permissions, jobs, flows and tasks, plus TypeScript functions), from which the **Console** (console.keel.so) generates
the tools people use. Ops teams are offered "Build for me": Keel's engineers model the business and hand it over in
weeks. Developers get "Let me play": the CLI, VS Code extension and docs. Raised $6m; customers include Egg, Plain,
Bravely, Modern Milkman, Hived, smol.

## The two "studios" on their site

- **Data model.** The home page ("Finally, systems as unique as your business") and each use case's "Peek under the
  hood" show the data model as a diagram: white boxes for models with their fields and types, curved lines between
  related ones, on a dotted grid, with zoom in, zoom out and fit. In the product it's the Console's **Schema
  visualiser**: pan, zoom, download as PNG, **read-only**. The model is changed by editing `schema.keel` in code (the
  inventory use case's diagram is a broken embed: "404 DEPLOYMENT_NOT_FOUND"). Field display is set apart from the
  diagram, in Tool settings › Models (label, visible, number format, colours, sensitive) and Enums (display name,
  colour, order).
- **Workflows.** The home page's "Complete agility" card shows a decision tree ("Stock supplier check" → "Out of stock"
  / "Partial stock" → "Split order"). In the product, workflows are **Flows written in TypeScript** (`ctx.step`,
  `ctx.ui.page`); the Console runs them and shows each run's step-by-step trace. There's no drawn, editable workflow:
  their own article "What does Keel supply" says the business's rules and flows are implementation work Keel's team
  does, and advises evaluators to "trace one rule into its implementation".

**Ours (built now):** both studios are drawn *and* changed by hand by the owner, checked, versioned and undoable, with
the AI in the same screen for anything else. On top of their read-only diagram we add moving lists, linking two by
dragging, editing fields and choices in place (renaming a choice renames it on the records), stage diagrams, rules
as flowcharts with how often each step ran, and **trying a rule on a real record** without doing anything. See
"The studios" in KIT.md.

## Everything else they offer, and where we are

| keel.so | Ours |
|---|---|
| Models, relationships, enums, unique, defaults, computed fields | ✅ 36 field types, links, formulas, rollups, lookups |
| **Unions**: one read-only list from several models (stock outlook = movements + PO lines − SO lines) | ✅ combined lists, kept up to date as their lists change |
| Enum display name, **colour and display order** set once for every screen | ✅ set in the Data model studio (colours by hand, or by meaning and position) |
| Number formats (currency, unit, percent), **prefix/suffix, colour +/−**, sensitive masked until hover, boolean labels and colours, URL preview | ✅ currency, unit, decimals, percent, text before and after, colour by sign, yes/no words, private fields, links |
| Generated tools: table, **inbox**, **kanban**, **gallery** layouts; sections (shown by condition); embedded lists; toolbar actions with conditions; related-list tabs; field links | ✅ all, plus calendar, timeline, map, tree, sheet |
| Spaces per team: groups, **useful links** (favicon), **metrics** (live counts) | ✅ team menus with groups, useful links, and how many are in each filtered view now |
| Tasks: queues, priority, assignment, defer, hand over, audit of each change | ✅ built (To do: take, give back, not now, next task) |
| Flows: steps with UI pages (text, number, date, boolean, scan with lookup, image capture with captions, datagrid, pick list, select one/table, iterator, print, banners, markdown, tables, files) | ✅ info, form, scan, pick, photo, signature, decide, print; ⚠️ a table to fill in (datagrid) and repeating a screen per item: later |
| Flows run on a **schedule**, child flows, shareable/**public flows by signed link**, prevent concurrent runs | ⚠️ rules run on schedules; public forms exist; flows by link: later |
| Notifications: email from anywhere, **log with per-recipient delivery status**, attachments, reply-to, verified sender | ✅ email and notifications; ⚠️ delivery log per recipient: later |
| Hardware: thermal label printers, raw printing on macOS, scanners | ✅ built |
| Auth: OIDC (Google, Okta…), password, SSO, hosted password reset | ✅ built |
| Permissions: row- and role-based, deny by default, expressions | ✅ roles, teams, row access, field access, locks |
| APIs: JSON, GraphQL, RPC; **MCP server**; typed SDKs | ✅ REST + OpenAPI + MCP; GraphQL not planned |
| Events/subscribers, scheduled jobs, durable functions | ✅ rules on changes and schedules, waits, retries |
| Database explorer: browse, edit with SQL review, **SQL with "Ask question"**, CSV import, read-replica credentials for BI | ✅ sheet view, import, the assistant answers in words; ⚠️ read-only access for BI tools: later |
| Monitoring: traces per request, error rate, P99 | ✅ health page (timings, errors, the AI, background work) |
| Builds and environments per git branch, approval to deploy, rollback | ✅ versions with undo, a test copy and Publish |
| Audit log | ✅ chained, tamper-evident |
| Version control, CLI, VS Code, TypeScript, testing, seeding, self-hosting on AWS | Not our way in: the AI builds, every build is tried out, and it runs on the owner's own Mac |
| Ready-made base modules (inventory, procurement, orders, fulfilment, warehouse, production) | ✅ four ready-made parts |
| Connectors (Xero, QuickBooks, Shopify, Stripe, Slack, Zapier, ClickUp, Jira, MuleSoft…) | ✅ Shopify, WooCommerce, Xero, QuickBooks, Stripe, EasyPost; webhooks and the API for the rest |
| "Build for me": Keel's engineers implement it | **The AI builds it in minutes, and the owner changes it themselves** |

## What they can't do that we do

The owner changes the app themselves, in words or by hand, and sees it change at once; nothing needs an engineer. AI
is inside the app (fields the AI fills in, AI steps in rules with a person checking until it's proven right, an
assistant that answers questions about the business, a builder that tries out every change before handing it over).
Everything runs locally, on the business's own computer.
