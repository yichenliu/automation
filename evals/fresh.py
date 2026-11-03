"""Businesses the builder has never seen: a trade picked from a long list, its owner's description written new each run by
the model. They're scored only on what holds for any business (right when handed over, what trying it out finds wrong,
the builder's own worked examples, the owner's own checks), so nothing about the fixed scenarios can be tuned for and pass for quality. Their
build turns are what the model is trained on (the fixed scenarios stay the exam, never the lessons).

Like real owners, a third of them start vague ("need sumthing to run my biz its a mess"), and every one answers the
builder's questions from how their business really works. Before anything is built, each owner also writes three checks
of their own with numbers ("a $6,000 order waits for the manager's OK"): tried on the app afterwards, they're the
independent check that makes a build turn a verified lesson (app/builder/training.py).
"""
import random

TRADES = [
    "landscaping business with three crews", "dental clinic with four dentists", "bike repair and sales shop", "electrical contractor",
    "commercial cleaning company", "event hire company (marquees, tables, chairs)", "small brewery selling to bars", "dog grooming salon",
    "sports coaching academy", "printing shop", "car detailing service", "boutique hotel with 20 rooms",
    "removalist company", "IT support company for small offices", "pest control business", "tutoring centre",
    "florist with weddings and daily orders", "hairdressing salon with five stylists", "architecture practice", "roofing contractor",
    "furniture removal and storage", "veterinary clinic", "café with catering", "fitness studio with memberships",
    "solar panel installer", "accounting practice", "recruitment agency", "wedding photography studio",
    "commercial kitchen equipment supplier", "pool maintenance service", "childcare centre", "medical equipment hire",
    "craft distillery", "property maintenance company for strata buildings", "mobile mechanic", "concrete contractor",
    "music school", "community garden co-op", "custom cabinet maker", "freight forwarding agent",
    "food wholesaler with two warehouses", "furniture manufacturer making to order from parts", "property manager for 40 rentals",
    "NDIS disability support provider", "plumbing supplies distributor", "scaffolding hire company", "commercial painting contractor",
    "mobile physiotherapy service", "fencing contractor", "aged care home care provider", "bakery supplying cafés",
    "marine boat servicing yard", "signage and printing manufacturer", "irrigation installer for farms", "skip bin hire",
    "language school with visas", "bridal boutique", "medical practice with five GPs", "steel fabrication workshop",
    "air conditioning installer and servicer", "small accounting firm doing tax returns", "wine distributor to restaurants",
    "event planning agency", "security guard company", "school uniform shop", "photography studio selling prints",
    "kitchen renovation company", "horse riding school", "computer repair shop", "landscape supplies yard",
    "carpet cleaning service", "chartered fishing tours", "mobile dog wash", "rural fuel delivery", "building inspector",
    "town planning consultancy",
]

STYLES = ("plain", "plain", "vague")

VAGUE = """Write the first message the owner of a {trade} types into a chat with an assistant that builds business systems,
the way many busy owners do: one short line like a text message, lowercase, a typo or shorthand, saying almost nothing
specific (like "need sumthing to run my biz its a mess tbh" or "can u set up a system 4 my shop"). Just the message."""

CHECKS = """This is what the owner of a {trade} said about their business:

{said}

Write 3 things this owner would check in their new system to know it works: each one sentence in their own words, with
real numbers and the right answer worked out from what they said ("A quote for 10 hours at $90 comes to $990 with GST",
"A $6,000 order waits for the manager's OK before it goes out"). Only what they said: invent nothing.
Answer in JSON: {{"checks": ["...", "...", "..."]}}"""

ANSWER = """You are the owner of a {trade}. This is how your business really works:

{said}

The assistant building your system just said:

{asked}

Answer like a busy owner texting back: short, plain words, only what it asked, using only the facts above (if it asks
something they don't cover, say "whatever's normal"). Just your reply."""

ASK = """Write what the owner of a {trade} in Australia says when asked how their business works and what they need from a
system. 4 to 6 sentences, in their own plain words, the way they'd say it to a friend: who their customers are, what
happens from first contact to getting paid, who does what (staff, roles), what's sent to customers or suppliers, what
needs someone's OK or gets chased, and one or two real numbers (a limit, a percentage, a deadline, how many staff).
No headings, no lists, no technical words."""


def scenarios(n, seed=None):
    """n new businesses: [{"key", "title", "fresh": trade, "style", "turns": []}] (the owner's words are written when the
    run starts)."""
    rng = random.Random(seed)
    picked = rng.sample(TRADES, min(n, len(TRADES)))
    return [{"key": f"fresh{i + 1}", "title": f"Fresh: {trade}", "fresh": trade, "style": rng.choice(STYLES), "turns": []}
            for i, trade in enumerate(picked)]


def owner(trade, style="plain"):
    """(what the owner says first, how their business really works): the same, unless they start vague."""
    from app import ai
    said = describe(trade)
    if style != "vague":
        return said, said
    content, _ = ai.chat([{"role": "user", "content": VAGUE.format(trade=trade)}], think=False)
    return content.strip().strip('"'), said


def checks(trade, said):
    """The owner's own checks, written from what they said before anything is built."""
    from app import ai
    content, _ = ai.chat([{"role": "user", "content": CHECKS.format(trade=trade, said=said)}], think=False,
                         schema={"type": "object", "properties": {"checks": {"type": "array", "items": {"type": "string"}}}, "required": ["checks"]})
    found = ai.parse_json(content).get("checks") or []
    return [str(c).strip() for c in found if str(c).strip()][:3]


def answer(trade, said, asked):
    """The owner's reply to the builder's questions."""
    from app import ai
    content, _ = ai.chat([{"role": "user", "content": ANSWER.format(trade=trade, said=said, asked=asked[:3000])}], think=False)
    return content.strip().strip('"')


def describe(trade):
    """The owner's description, written by the model (in the run's app context)."""
    from app import ai
    content, _ = ai.chat([{"role": "user", "content": ASK.format(trade=trade)}], think=False)
    return content.strip().strip('"')
