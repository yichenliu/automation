"""Businesses the builder has never seen: a trade picked from a long list, its owner's description written new each run by
the model. They're scored only on what holds for any business (right when handed over, what trying it out finds wrong,
the builder's own worked examples, the owner's own checks), so nothing about the fixed scenarios can be tuned for and pass for quality. Their
build turns are what the model is trained on (the fixed scenarios stay the exam, never the lessons).

Like real owners, a third of them start vague ("need sumthing to run my biz its a mess"), and every one answers the
builder's questions from how their business really works. Each owner also writes up to three checks of their own with
numbers, from what they told the builder ("a $6,000 order waits for the manager's OK"): tried on the app afterwards,
they're the independent check that makes a build turn a verified lesson (app/builder/training.py).
"""
import random
import re

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
    # more, so later rounds meet businesses no round has built
    "glass and glazing contractor", "tree surgery and arborist service", "tattoo studio with four artists", "funeral home",
    "driving school", "optometrist with two stores", "mobile coffee van fleet", "costume hire shop", "auto electrician",
    "pallet supplier and recycler", "commercial laundry for hotels", "drone survey company", "piano tuning and repairs",
    "occupational therapy clinic", "cattle station selling to feedlots", "orchard selling at markets and wholesale",
    "coffee roaster supplying cafés", "website design agency", "translation and interpreting service", "locksmith",
    "mobile vet for farm animals", "trophy and engraving shop", "party supplies and balloon decorator", "tiling contractor",
    "home builder doing six houses a year", "secondhand furniture store", "seafood wholesaler", "art framing shop",
    "vending machine operator", "self-storage facility",
    # and more again (round 36 used the last of the above)
    "mobile hairdresser for aged care homes", "commercial fishing boat selling to co-ops", "gym equipment repairer",
    "hearing clinic", "bookkeeper for tradies", "caravan park", "nursery growing plants for landscapers", "car rental with 30 cars",
    "solar battery retailer", "mobile tyre fitter", "farm machinery dealer", "swimming school", "cake decorator taking orders",
    "dance studio", "IT recruitment contractor agency", "office fit-out company", "electrician doing test and tag",
    "small winery with a cellar door", "boat hire on a lake", "podiatry clinic", "courier company with eight vans",
    "home cleaning franchise", "lawn mowing round", "corporate catering kitchen", "bicycle courier", "audio-visual hire for events",
    "school holiday camp operator", "charity op shop with volunteers", "ceramics studio running classes", "sign language interpreters",
    "food truck", "real estate sales agency", "conveyancing firm", "chimney sweep", "window cleaning company",
    "hydraulic hose repair van", "cattle vet practice", "pet boarding kennels", "removal of asbestos", "uniform embroidery shop",
]

STYLES = ("plain", "plain", "vague")

VAGUE = """Write the first message the owner of a {trade} types into a chat with an assistant that builds business systems,
the way many busy owners do: one short line like a text message, lowercase, a typo or shorthand, saying almost nothing
specific (like "need sumthing to run my biz its a mess tbh" or "can u set up a system 4 my shop"). Just the message."""

CHECKS = """This is everything the owner of a {trade} has told the assistant building their new system:

{said}

Write up to 3 things this owner would check in the system to know it does what they told it: each one sentence in their
own words, about what the system does with a record (a sum it works out, an OK something must wait for, a reminder or
alert after so many days, a stage something moves to, who may see or do what), with real numbers and the right answer
worked out from what they said. Only what they told it above: nothing they didn't say, no volumes, speeds or staffing
("forty orders a day"), and nothing about what you don't know. If they said too little to check, write fewer, or none.
Answer in JSON: {{"checks": ["..."]}}"""

META = re.compile(r"\b(I )?(didn'?t|did not|don'?t|do not|never) (mention|have|know|say|give)\b|\bcan'?t (confirm|verify|check)\b"
                  r"|\bcannot (confirm|verify|check)\b|\bnot (sure|mentioned)\b", re.I)

ANSWER = """You are the owner of a {trade}. This is how your business really works:

{said}

The assistant building your system just said:

{asked}

Answer like a busy owner texting back: short, plain words, only what it asked, using only the facts above (if it asks
something they don't cover, say "whatever's normal"). Just your reply."""

EXPLAIN = """You are the owner of a {trade}. You told the assistant building your system:

{said}

You asked it to fix what wasn't right in your app, and it replied:

{reply}

What still isn't right, as your app's Checks page shows it:
{still}

Reply like a busy owner texting back: a sentence or two of plain words saying what should happen instead (what you'd
press, and what you'd expect to see then), using only what you told it above. If it asks you something, answer from
that; if it isn't covered, say "whatever's normal". Just your reply."""

ASK = """Write what the owner of a {trade} in Australia says when asked how their business works and what they need from a
system. 4 to 6 sentences, in their own plain words, the way they'd say it to a friend: who their customers are, what
happens from first contact to getting paid, who does what (staff, roles), what's sent to customers or suppliers, what
needs someone's OK or gets chased, and one or two real numbers (a limit, a percentage, a deadline, how many staff).
No headings, no lists, no technical words."""


def scenarios(n, seed=None, avoid=()):
    """n new businesses: [{"key", "title", "fresh": trade, "style", "turns": []}] (the owner's words are written when the
    run starts), none of the trades in `avoid` (ones earlier rounds built)."""
    rng = random.Random(seed)
    left = [t for t in TRADES if t not in set(avoid)]
    picked = rng.sample(left, min(n, len(left)))
    return [{"key": f"fresh{i + 1}", "title": f"Fresh: {trade}", "fresh": trade, "style": rng.choice(STYLES), "turns": []}
            for i, trade in enumerate(picked)]


def used(sources):
    """The trades earlier rounds built: from their run folders (results.jsonl) or text files, one trade a line."""
    import json
    from pathlib import Path
    out = []
    for source in sources or []:
        path = Path(source)
        if path.is_dir() and (path / "results.jsonl").exists():
            out += [r["title"].removeprefix("Fresh: ") for r in map(json.loads, (path / "results.jsonl").open())
                    if r.get("title", "").startswith("Fresh: ")]
        elif path.is_file():
            out += [line.strip() for line in path.open() if line.strip()]
    return out


def owner(trade, style="plain"):
    """(what the owner says first, how their business really works): the same, unless they start vague."""
    from app import ai
    said = describe(trade)
    if style != "vague":
        return said, said
    content, _ = ai.chat([{"role": "user", "content": VAGUE.format(trade=trade)}], think=False)
    return content.strip().strip('"'), said


def checks(trade, said):
    """The owner's own checks, written from what they told the builder (`said`: their messages), before it's built."""
    from app import ai
    content, _ = ai.chat([{"role": "user", "content": CHECKS.format(trade=trade, said=said)}], think=False,
                         schema={"type": "object", "properties": {"checks": {"type": "array", "items": {"type": "string"}}}, "required": ["checks"]})
    found = ai.parse_json(content).get("checks") or []
    return [str(c).strip() for c in found if str(c).strip() and not META.search(str(c))][:3]  # “…but I didn't mention it” isn't a check


def answer(trade, said, asked):
    """The owner's reply to the builder's questions."""
    from app import ai
    content, _ = ai.chat([{"role": "user", "content": ANSWER.format(trade=trade, said=said, asked=asked[:3000])}], think=False)
    return content.strip().strip('"')


def explain(trade, said, reply, still):
    """The owner saying in their own words what should happen, when pressing “fix” didn't put it right."""
    from app import ai
    content, _ = ai.chat([{"role": "user", "content": EXPLAIN.format(trade=trade, said=said[:3000], reply=reply[:2000],
                                                                     still="\n".join(f"- {x}" for x in still[:6]))}], think=False)
    return content.strip().strip('"')


def describe(trade):
    """The owner's description, written by the model (in the run's app context)."""
    from app import ai
    content, _ = ai.chat([{"role": "user", "content": ASK.format(trade=trade)}], think=False)
    return content.strip().strip('"')
