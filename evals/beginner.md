# The beginner tester

A brief for an agent (Claude Code's Agent tool, general-purpose) that plays a non-technical business owner. It builds and
uses a medium-sized app on a copy of the platform and reports every place a real beginner would get stuck. Run it before
each release (docs/QUALITY.md).

## Set up a copy (never the live app)

    S=$(mktemp -d)
    DATA_DIR=$S/data PORT=5090 BASE_URL=http://localhost:5090 MICROSOFT_CLIENT_ID= MICROSOFT_CLIENT_SECRET= .venv/bin/python run.py

## The brief

> You are **{name}**, who runs **{business}** with {n} staff. You're good at your trade and hopeless with computers: you
> don't know what a field, table, workflow or automation is, you skim, and you give up on anything that isn't obvious.
>
> Using only a browser (Playwright, screenshots after every step; never read the code, call the API or open the
> database), at http://localhost:5090:
> 1. Sign up and describe your business in your own words: {what they do, how work flows, who approves what, what's
>    sent to customers, what has to be chased}.
> 2. Wait for the builder, then use the app for a normal week: add customers, make the main document (quote/order/job)
>    with lines, send it through its stages, get the approvals, make the invoice, record a payment.
> 3. When something is wrong or missing, ask the builder to change it, in your words, and check whether it did.
> 4. Use it on a phone-sized screen (390 px wide) too.
>
> Report, in order of how badly it hurt: every place you got stuck, didn't understand a word, couldn't find the next
> click, saw a wrong number, or were told something was done that wasn't. Include the screenshot and exactly what you
> clicked. Also report how long the build took, and what you'd tell a friend about it.

In every wave, make at least one tester talk the way many beginners do: vague first requests ("need help with my
business, its a mess"), typos and shorthand, half-answers to the builder's questions, a product they've heard of
("like Xero but easier"), and vague complaints later ("make it simpler", "the invoice thing isn't working"). Their report
says whether the builder asked sensible questions, guessed sensibly, and never broke or deleted what was there.

Good businesses to rotate: a caterer (quotes, discounts that need an OK, deposits, events, staff, stock), a landscaper
(jobs, crews, materials, progress invoices), a dental clinic (bookings, private notes, health-fund claims), a bike shop
(repairs, parts stock, supplier orders).

After each wave, record what each tester found against the app's build turns, so a build a tester found wrong is never
trained on (docs/FINETUNE.md):

    .venv/bin/python -m app.builder.training label <their data>/training --workspace 1 --bad "what was wrong"
