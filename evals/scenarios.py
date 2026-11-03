"""Businesses the analyst is tested on: what the owner says, turn by turn, and what a good ERP for them has.

expect:
    tables    groups of words; each group must match a table (key or label), e.g. ["customer", "client"]
    features  building blocks a good design uses (see evals/analyst.py FEATURES)
    max_tables  for small businesses: more than this is over-building
    asks      True when the right answer is a question, not a build
    imports   True when the turn should bring a spreadsheet's rows in
Each turn can have its own "expect" (checked after that turn), and the last turn's expectations are checked on the
finished design.
"""

SCENARIOS = [
    {"key": "plumbing", "title": "Plumbing business (field service)", "turns": [
        {"say": "We're a plumbing business in Perth with 6 plumbers. Customers ring us or fill in a form on our website, we "
                "quote, then book the job, the plumber goes out and gets the customer to sign off on site, and we invoice. "
                "I want to see where jobs are on a map and each plumber's week.",
         "expect": {"tables": [["customer", "client"], ["quote"], ["job"], ["invoice"], ["plumber", "staff", "technician", "team", "employee", "worker"]],
                    "features": ["line_items", "money_totals", "document", "public_form", "signature", "location", "schedule_view",
                                 "automation", "board", "page"]}},
        {"say": "Urgent jobs should stand out in red, and when a job is marked done, draft its invoice automatically.",
         "expect": {"features": ["colour", "auto_create"]}},
        {"say": "What happens when a quote is accepted?", "expect": {"question": True}},
    ]},
    {"key": "wholesale", "title": "Food wholesaler (stock, accounts, permissions)", "turns": [
        {"say": "We're Harbour Wholesale, a food and beverage wholesaler. We buy stock from overseas suppliers (some invoice in "
                "USD) into two warehouses and sell to cafes and restaurants on 30-day account. I need proper double-entry "
                "books, stock levels per warehouse with reorder alerts, and payroll for our 9 staff that only I and my manager "
                "can see. Our 3 sales reps should only see their own customers' orders. Once an invoice is sent it must not "
                "be edited except to record payment. I want a report of sales by customer by month.",
         "expect": {"tables": [["customer"], ["supplier"], ["warehouse"], ["stock", "movement"], ["invoice"], ["journal", "ledger"],
                               ["account"], ["payroll", "pay", "staff", "employee"]],
                    "features": ["line_items", "row_access", "lock", "rule", "restricted", "currency", "pivot", "automation", "stock_ledger"]}},
    ]},
    {"key": "consultancy", "title": "Consultancy (timesheets, billing, utilisation)", "turns": [
        {"say": "I run a 12-person data consultancy. We track clients and projects, and everyone logs timesheets against "
                "projects. Projects are fixed fee or time and materials. Each month I invoice the T&M projects for the hours "
                "logged. I want utilisation per consultant and project profitability. Consultants shouldn't see rates or margins.",
         "expect": {"tables": [["client", "customer"], ["project"], ["timesheet", "time_entr", "time_log", "hours"], ["invoice"]],
                    "features": ["rollup", "restricted", "line_items", "chart_or_pivot", "document", "automation"]}},
        {"say": "Every Friday at 3pm remind anyone who hasn't logged 30 hours that week.",
         "expect": {"features": ["schedule"]}},
    ]},
    {"key": "candles", "title": "Online shop (web orders by webhook, stock)", "turns": [
        {"say": "We make candles and sell them online through Shopify and at weekend markets. Orders from the website should "
                "come in automatically. I need stock of each scent and size, to know when to pour more, and sales by "
                "channel each month.",
         "expect": {"tables": [["product", "candle", "scent", "item"], ["order", "sale"]],
                    "features": ["webhook_trigger", "line_items", "stock_ledger_or_rollup", "chart_or_pivot", "automation"]}},
    ]},
    {"key": "furniture", "title": "Furniture factory (bills of materials, work orders)", "turns": [
        {"say": "Small furniture factory. We make tables, chairs and cabinets from timber, hardware and finishes. Each product "
                "has a bill of materials, and some use sub-assemblies (a drawer unit goes into a cabinet). Customer orders "
                "become work orders, and work orders use up materials. I need to see material shortages before we start a job.",
         "expect": {"tables": [["product"], ["material", "component", "part"], ["bom", "bill", "recipe", "component", "assembl"],
                               ["work_order", "work order", "production", "job"], ["order"]],
                    "features": ["line_items", "rollup", "automation", "page"]}},
    ]},
    {"key": "physio", "title": "Physio clinic (appointments, private notes, consent)", "turns": [
        {"say": "Physio clinic with 4 physios. Patients book appointments in time slots. We keep treatment notes that only the "
                "physios and I can see, new patients sign a consent form at their first visit, and we invoice either the "
                "patient or their health fund.",
         "expect": {"tables": [["patient", "client"], ["appointment", "booking"], ["note", "treatment"], ["invoice"]],
                    "features": ["datetime", "schedule_view", "restricted", "signature", "document"]}},
    ]},
    {"key": "builder", "title": "Residential builder (stages, subcontractors, variations)", "turns": [
        {"say": "Residential builder. Each build is a project with stages (slab, frame, lock-up, fit-out, handover) that depend "
                "on each other. Subcontractors need to see the jobs they're assigned to and send us their invoices. Clients "
                "sign off variations, and we claim a progress payment at each stage.",
         "expect": {"tables": [["project", "build"], ["stage", "task", "milestone"], ["subcontract", "trade", "supplier"],
                               ["variation"], ["claim", "invoice", "payment"]],
                    "features": ["dependencies", "portal_or_restricted", "signature_or_approval", "document"]}},
    ]},
    {"key": "rescue", "title": "Animal rescue charity (donors, volunteers, adoptions)", "turns": [
        {"say": "We're a small animal rescue charity: donors and donations (one-off and monthly), volunteers and their shifts, "
                "and animals in care and their adoptions. We need tax receipts for donations and a report of donations by "
                "month and campaign.",
         "expect": {"tables": [["donor"], ["donation"], ["volunteer"], ["shift", "roster"], ["animal"], ["adoption"]],
                    "features": ["document", "chart_or_pivot", "schedule_view_or_datetime"]}},
    ]},
    {"key": "property", "title": "Property manager (leases, rent, maintenance, owner statements)", "turns": [
        {"say": "I manage 40 rental properties for their owners. Tenants, leases, rent due monthly, maintenance requests from "
                "tenants that I give to tradies, and a monthly statement to each owner showing rent collected less expenses.",
         "expect": {"tables": [["propert"], ["owner", "landlord"], ["tenant"], ["lease"], ["rent", "payment"], ["maintenance", "repair", "request"]],
                    "features": ["document", "automation", "portal_or_public_form", "rollup"]}},
    ]},
    {"key": "cleaner", "title": "Solo cleaner (must stay small)", "turns": [
        {"say": "I'm a solo cleaner. I need to keep track of my clients, when I clean for them, and send invoices. Keep it simple.",
         "expect": {"tables": [["client", "customer"], ["clean", "visit", "job", "booking", "appointment"], ["invoice"]],
                    "features": ["document"], "max_tables": 5}},
    ]},
    {"key": "approvals", "title": "Purchase approvals (approval flow, stages, tasks)", "turns": [
        {"say": "We're a 40-person engineering firm. Staff raise purchase requests with the supplier's quote attached. Under "
                "$2,000 their team lead approves; over $2,000 the finance manager must approve as well, and over $20,000 the "
                "director too. A rejected request goes back to whoever raised it to fix and resubmit. Once it's approved, "
                "purchasing places the order with the supplier and enters the PO number. Chase approvals left more than 2 "
                "days, and pass them to the director after 5. Capital items need an asset tag and a depreciation category; "
                "other items don't.",
         "expect": {"tables": [["request", "requisition", "purchase"], ["supplier", "vendor"]],
                    "features": ["workflow", "approval_threshold", "if_rejected", "escalation", "task_or_required_move", "conditional_field"]}},
    ]},
    {"key": "inbox", "title": "AI-run inbox (orders, bills, complaints, payments)", "turns": [
        {"say": "We distribute office supplies. Customer orders arrive by email, usually as a PDF; supplier bills arrive by "
                "email too. Turn order emails into sales orders with their lines, and bills into supplier bills. Customers "
                "also email complaints: sort them by type and draft a reply that follows our returns policy, but I want to "
                "check each reply before it goes. Payments from the bank feed should be matched to the invoice they pay.",
         "expect": {"tables": [["order"], ["bill", "supplier_invoice", "purchase_invoice"], ["complaint", "ticket", "case"], ["payment"],
                               ["invoice"]],
                    "features": ["ai_intake", "ai_checks_set", "ai_reply_checked", "ai_match", "ai_knowledge"]}},
    ]},
    {"key": "frontdoor", "title": "Purchasing front door (AI drafts and checks)", "turns": [
        {"say": "We want a purchasing front door. Staff raise purchase requests saying what they need and why, with any brief "
                "attached. When a request is submitted, the AI should draft a statement of work from it for the requester to "
                "check. Suppliers send quotes as PDFs: the AI reads the quote's lines and checks each price against our contract "
                "price list for that supplier, flagging anything over contract. Anything flagged needs the procurement manager's "
                "approval; clean quotes go straight to approved, then purchasing raises the PO.",
         "expect": {"tables": [["request", "requisition"], ["supplier", "vendor"], ["contract", "price_list", "agreed_price"], ["quote"]],
                    "features": ["ai_draft_checked", "ai_reads_documents", "ai_check_reference", "routes_on_ai", "line_items"]}},
    ]},
    {"key": "procurement", "title": "Mining procurement, source to pay (what Wawasu does)", "turns": [
        {"say": "We're a mining company and need our procurement system. Staff raise purchase requests; each is approved by "
                "whoever's delegation limit covers its amount and category (we keep a table of approval limits), never by the "
                "person who raised it. Approved requests become purchase orders to suppliers, who acknowledge them in a "
                "supplier portal. We receive goods against the PO, suppliers invoice us, and each invoice is matched to its PO "
                "and receipt within a 2% tolerance; the same supplier invoice number can never be entered twice, and whoever "
                "enters an invoice can't approve it. Finance pays approved invoices in payment runs: one person prepares a run, "
                "someone else in Finance approves it after confirming their sign-in, and we download the bank file for our bank.",
         "expect": {"tables": [["request", "requisition"], ["limit", "delegation", "authority"], ["purchase_order", "po"], ["supplier", "vendor"],
                               ["receipt", "goods_received", "grn"], ["invoice", "bill"], ["payment_run", "payment"]],
                    "features": ["limits_lookup", "separation_of_duties", "step_up", "unique_together", "bank_file", "supplier_portal",
                                 "invoice_match", "line_items"]}},
        {"say": "Now sourcing. For bigger buys we run tenders: we invite suppliers, and they submit sealed bids with their line "
                "prices through the portal and can change them until the closing time. Nobody here can see a bid before it "
                "closes, not even admins. Then we compare the bids side by side, the evaluation panel scores them, procurement "
                "recommends an award, and someone other than whoever recommended it approves the award.",
         "expect": {"tables": [["tender", "rfq", "sourcing"], ["bid", "response", "offer"]],
                    "features": ["sealed_bids", "separation_of_duties", "supplier_portal"]}},
        {"say": "Suppliers should be able to register themselves in the portal, and we verify them before we trade. They keep "
                "their own details up to date there, but a change to their bank details must be approved by Finance after a "
                "7-day cooling-off, and the supplier's registered email gets told so they can cancel it if it wasn't them. Bank "
                "account numbers must be encrypted and shown masked.",
         "expect": {"features": ["sensitive", "change_approval", "cooling_off", "sign_up"]}},
    ]},
    {"key": "catering", "title": "Catering business (Maria, the beginner tester)", "turns": [
        {"say": "Hi, I run Maria's Kitchen, we do catering in Perth. Customers ring me or email me to book catering for their event, "
                "like weddings, birthdays or work lunches. I send them a quote with the menu items, how many guests and the price. "
                "If I give more than 10% discount I want to OK it myself before the quote goes out. The customer pays a 30% deposit "
                "to confirm the booking, then after the event I send an invoice for the rest plus GST. I need to know which of my "
                "staff are working at which event (I have 8 staff). I keep stock of ingredients and I want to know when something "
                "is running low so I can order from my suppliers. And I would like a list of what's coming up this week.",
         "expect": {"tables": [["customer", "client"], ["event", "booking"], ["quote"], ["menu", "item", "dish"], ["staff", "employee"],
                               ["ingredient", "stock"], ["supplier"]],
                    "features": ["line_items", "money_totals", "approval_threshold", "page"]}},
        {"say": "can you remind me 2 days before each event please, so I can get the food and the staff ready", "expect": {"features": ["schedule"]}},
    ]},
    {"key": "vague", "title": "Too vague to build", "turns": [
        {"say": "can u help me set up my business system", "expect": {"asks": True, "plain": True, "few_questions": 3}},
    ]},
    # Vague, the way beginners really ask: half a sentence, typos, a product they've heard of, "make it simpler",
    # "it's not working". Each is judged on what a good analyst would do with it (see analyst.score).
    {"key": "vague_shop", "title": "Vague: just a shop", "turns": [
        {"say": "i have a small shop and need a system for it", "expect": {"asks_or_small": 5, "plain": True, "few_questions": 3}},
    ]},
    {"key": "vague_then_answer", "title": "Vague, then a short answer", "turns": [
        {"say": "need help with my business, its a mess tbh", "expect": {"asks": True, "plain": True, "few_questions": 3}},
        {"say": "mobile car detailing, me and 2 guys. people book us, we go out to them, they pay on the spot or we invoice "
                "the businesses", "expect": {"builds": True, "tables": [["booking", "job", "appointment"], ["customer", "client"]],
                                              "max_tables": 7, "plain": True}},
    ]},
    {"key": "vague_slang", "title": "Vague: typos and shorthand", "turns": [
        {"say": "hey need sumthing 4 my cafe - orders n stock n staff hours pls. keep it easy",
         "expect": {"builds": True, "tables": [["order"], ["stock", "item", "product", "ingredient"], ["staff", "shift", "timesheet", "roster", "hour"]],
                    "max_tables": 8, "plain": True}},
    ]},
    {"key": "vague_like", "title": "Vague: like another product", "turns": [
        {"say": "i want something like xero but easier, for my plumbing business",
         "expect": {"builds": True, "tables": [["invoice"], ["customer", "client"]], "max_tables": 8, "plain": True}},
    ]},
    {"key": "vague_everything", "title": "Vague: everything", "turns": [
        {"say": "Everything! I want it to run my whole business. It's a bakery.",
         "expect": {"builds": True, "max_tables": 10, "plain": True, "few_questions": 3}},
    ]},
    {"key": "vague_what", "title": "Vague: what can you do", "turns": [
        {"say": "what can you do?", "expect": {"question": True, "plain": True}},
    ]},
    {"key": "vague_changes", "title": "Vague changes to an app that works", "turns": [
        {"say": "I'm a solo cleaner. I need my clients, when I clean for them, and invoices. Keep it simple.",
         "expect": {"builds": True, "max_tables": 5}},
        {"say": "make it simpler, its confusing", "expect": {"keeps": True, "plain": True, "few_questions": 2}},
        {"say": "the invoice thing isnt working", "expect": {"keeps": True, "plain": True, "few_questions": 2}},
        {"say": "can u make it do the usual stuff", "expect": {"keeps": True, "plain": True, "few_questions": 3}},
    ]},
    {"key": "spreadsheet", "title": "From the spreadsheet they use today", "file": "orders", "turns": [
        {"say": "Here's how we track things today. Turn it into a proper system and bring the data in.",
         "expect": {"tables": [["customer", "client"], ["order"]], "imports": True}},
    ]},
]
