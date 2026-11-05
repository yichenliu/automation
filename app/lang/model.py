"""The Keel language: what a builder writes to make or change an app (see app/lang/__init__.py).

Every shape is a Pydantic model, which is its single definition:
- the JSON schema that constrains the AI's output comes from it, one exact shape per kind of patch;
- validation from it says exactly where something is wrong;
- the catalog in the AI's instructions is generated from its docstrings.

Names are explicit everywhere:
- a condition or formula in a rule reads the record the rule is about, and reaches linked records through dots
  (order.status);
- a step names the record it changes ("record", or a link such as "order");
- a stage is reached however it's reached.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from .registry import register, union

Scalar = Union[str, float, int, bool, None]


def _words(text):
    """A name people see is words: one written as a key (feedlot_buyer) is made into them (Feedlot buyer)."""
    if text and "_" in text and " " not in text.strip():
        text = " ".join(text.split("_")).strip()
        return text[:1].upper() + text[1:]
    return text


Words = Annotated[str, AfterValidator(_words)]  # a label, a singular: what people read on the screen


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- fields ---------------------------------------------------------------------------------------------------------

PLAIN_TYPES = ("text", "long_text", "rich_text", "email", "phone", "url", "address", "barcode", "tags", "number", "money", "percent",
               "duration", "rating", "date", "datetime", "time", "yes_no", "person", "file", "image", "signature")


@register("field", {"type": "date", "key": "due_date", "label": "Due date", "required": True})
class Plain(Base):
    """A field people fill in. visible_to: only this role (admin, owner) or team sees it (prices kept from the makers);
    sensitive: kept encrypted and shown masked (a bank account, a tax number)."""
    type: Literal["text", "long_text", "rich_text", "email", "phone", "url", "address", "barcode", "tags", "number", "money", "percent",
                  "duration", "rating", "date", "datetime", "time", "yes_no", "person", "file", "image", "signature"]
    key: str
    label: Words
    required: bool = False
    default: Scalar = None
    help: str | None = None
    unique: bool = False
    visible_to: str | None = None
    sensitive: bool = False
    show_if: list["Condition | str"] = Field(default_factory=list, description="shown only while these hold (a reason, when declined)")
    required_if: list["Condition | str"] = Field(default_factory=list, description="must be filled in while these hold")


@register("field", {"type": "choice", "key": "priority", "label": "Priority", "options": ["Standard", "Urgent"]})
class Choice(Base):
    """A field with choices: "choice" (one) or "choices" (several). The choices are the owner's own words."""
    type: Literal["choice", "choices"]
    key: str
    label: Words
    options: list[str]
    required: bool = False
    default: str | None = None
    help: str | None = None
    visible_to: str | None = None
    show_if: list["Condition | str"] = Field(default_factory=list)
    required_if: list["Condition | str"] = Field(default_factory=list)


@register("field", {"type": "link", "key": "customer", "label": "Customer", "to": "customers", "required": True})
class Link(Base):
    """A link to a record of another list (an invoice's customer). many: several records (a job's crew)."""
    type: Literal["link"]
    key: str
    label: Words
    to: str = Field(description="the list it links to")
    many: bool = False
    required: bool = False
    help: str | None = None
    show_if: list["Condition | str"] = Field(default_factory=list)
    required_if: list["Condition | str"] = Field(default_factory=list)


@register("field", {"type": "auto_number", "key": "number", "label": "Invoice number", "prefix": "INV-"})
class AutoNumber(Base):
    """A number the app gives each new record (INV-0001)."""
    type: Literal["auto_number"]
    key: str
    label: Words
    prefix: str = ""


@register("field", {"type": "formula", "key": "amount", "label": "Amount", "formula": "quantity * unit_price", "shows": "money"})
class Formula(Base):
    """Worked out from the record's own fields, its links' fields, and the records linking to it: "quantity *
    unit_price", "round(subtotal * 0.1, 2)", "customer.discount_percent", "sum(payments, amount)" (its payments'
    amounts), "count(invoices where status = 'Unpaid')". shows: how the result looks."""
    type: Literal["formula"]
    key: str
    label: Words
    formula: str
    shows: Literal["number", "money", "percent", "date", "text", "yes_no"] = "number"


@register("field", {"type": "total", "key": "subtotal", "label": "Subtotal", "of": "invoice_lines", "link": "invoice", "how": "sum", "field": "amount"})
class Total(Base):
    """A total over the records of another list that link to this one: an invoice's subtotal is the sum of
    invoice_lines.amount, where invoice_lines.invoice links to the invoice. how: sum, count, avg, min, max."""
    type: Literal["total"]
    key: str
    label: Words
    of: str = Field(description="the list whose records are totalled")
    link: str = Field(description="that list's link field pointing at this list")
    how: Literal["sum", "count", "avg", "min", "max"] = "sum"
    field: str | None = Field(None, description="the field totalled (not for count)")
    only: list["Condition | str"] = Field(default_factory=list, description="only the records that match these")


@register("field", {"type": "lookup", "key": "customer_email", "label": "Customer email", "path": "customer.email"})
class Lookup(Base):
    """A linked record's field shown on this one, e.g. path "customer.email"."""
    type: Literal["lookup"]
    key: str
    label: Words
    path: str


FieldDef = union("field")


# --- stages ---------------------------------------------------------------------------------------------------------

class Move(Base):
    """A way people move a record on: a button from some stages to another. who: only these may (roles owner, admin,
    editor, or a team's key); needs: fields that must be filled in first."""
    from_: list[str] = Field(default_factory=list, alias="from", description="the stages it moves from (empty: any)")
    to: str
    button: str
    who: list[str] = Field(default_factory=list)
    needs: list[str] = Field(default_factory=list)
    only_if: list["Condition | str"] = Field(default_factory=list)
    not_by: str | None = Field(None, description="never by this person (a person field, or creator): someone else checks it")
    step_up: bool = Field(False, description="the person confirms their sign-in first (a payment run)")


class Stages(Base):
    """The stages a record goes through, in order, and the moves between them. With no moves, each stage gets a button
    to the next one."""
    field: str = "status"
    stages: list[str]
    start: str | None = None
    moves: list[Move] = Field(default_factory=list)


# --- conditions and values ------------------------------------------------------------------------------------------

OPS = ("equals", "not_equals", "contains", "is_empty", "not_empty", "greater_than", "less_than", "at_least", "at_most",
       "one_of", "before_today", "after_today", "within_next_days", "within_last_days", "more_than_days_ago",
       "today", "this_week", "this_month", "changed", "changed_to")


class Condition(Base):
    """field: a field of the record, or a linked record's through dots (order.status). value: what it's compared with
    (a choice in the owner's words, a number, a date; days for within_… and more_than_days_ago; a list for one_of).
    Or, as text, a formula that must hold, which can look at other records: 'count(client.invoices where status =
    "Unpaid" and days_since(due_date) > 30) == 0'."""
    field: str
    op: Literal["equals", "not_equals", "contains", "is_empty", "not_empty", "greater_than", "less_than", "at_least",
                "at_most", "one_of", "before_today", "after_today", "within_next_days", "within_last_days",
                "more_than_days_ago", "today", "this_week", "this_month", "changed", "changed_to"]
    value: Union[str, float, int, bool, list[str], None] = None


class Value(Base):
    """One field a step fills in: "to" a value as it is ("Paid", 0, true), or "formula" worked out from the record the
    rule is about (today(), "customer", "record" for that record itself, "created" for the one the step before made,
    "item.x" for a line being copied)."""
    field: str
    to: Scalar = None
    formula: str | None = None


# --- rules ----------------------------------------------------------------------------------------------------------

@register("when", {"on": "added", "list": "jobs"})
class Added(Base):
    """When a record is added to the list."""
    on: Literal["added"]
    list: str


@register("when", {"on": "changed", "list": "jobs", "field": "assigned_to"})
class Changed(Base):
    """When a record of the list is changed: any field, or only this field (and only to this value)."""
    on: Literal["changed"]
    list: str
    field: str | None = None
    to: Scalar = None


@register("when", {"on": "added_or_changed", "list": "jobs"})
class AddedOrChanged(Base):
    """When a record of the list is added or changed."""
    on: Literal["added_or_changed"]
    list: str


@register("when", {"on": "reaches", "list": "jobs", "stage": "Done"})
class Reaches(Base):
    """When a record of the list gets to a stage, however it gets there (a button, the board, another rule)."""
    on: Literal["reaches"]
    list: str
    stage: str


@register("when", {"on": "button", "list": "quotes", "label": "Send quote"})
class Button(Base):
    """A button on each record of the list; only_if of the rule says when it can be pressed (until then it's refused,
    saying what it waits for: "can't send specs until the design is approved" is a button with only_if)."""
    on: Literal["button"]
    list: str
    label: Words


@register("when", {"on": "every", "list": "invoices", "period": "day", "at": "08:00"})
class Every(Base):
    """On a schedule, for each record of the list that matches the rule's only_if."""
    on: Literal["every"]
    list: str
    period: Literal["hour", "day", "week", "month"] = "day"
    at: str = "08:00"
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] | None = None


@register("when", {"on": "deleted", "list": "jobs"})
class Deleted(Base):
    """When a record of the list is deleted."""
    on: Literal["deleted"]
    list: str


@register("when", {"on": "email", "subject_contains": "invoice", "with_attachment": True})
class EmailIn(Base):
    """When an email arrives in the connected mailbox (from_contains, subject_contains, with_attachment narrow it):
    its steps read it as email.subject, email.body, email.sender."""
    on: Literal["email"]
    from_contains: str | None = None
    subject_contains: str | None = None
    with_attachment: bool = False


@register("when", {"on": "file", "folder": "Invoices/Incoming"})
class FileIn(Base):
    """When a file arrives in a connected OneDrive folder: its steps read it as file.name."""
    on: Literal["file"]
    folder: str


When = union("when")


@register("step", {"do": "set", "on": "order", "values": [{"field": "status", "to": "Ready"}], "only_if": [{"field": "order.status", "op": "equals", "value": "In fitting"}]})
class SetValues(Base):
    """Change a record: on "record" (the one the rule is about) or a link of it ("order", "job.customer")."""
    do: Literal["set"]
    on: str = "record"
    values: list[Value]
    only_if: list[Condition | str] = Field(default_factory=list)


@register("step", {"do": "add", "list": "invoices", "values": [{"field": "job", "formula": "record"}, {"field": "issue_date", "formula": "today()"}]})
class AddRecord(Base):
    """Add a record to a list. Fields with the same names as the rule's record are copied unless copy_same is false."""
    do: Literal["add"]
    list: str
    values: list[Value] = Field(default_factory=list)
    copy_same: bool = True
    only_if: list[Condition | str] = Field(default_factory=list)


@register("step", {"do": "add_each", "list": "invoice_lines", "lines": "job_lines", "link": "job", "values": [{"field": "invoice", "formula": "created"}]})
class AddEach(Base):
    """Add a record for each line of the rule's record (an invoice line for each job line): lines is the list of lines,
    link is their link field to the rule's record; values read the line as item.x and the record made before as
    created."""
    do: Literal["add_each"]
    list: str
    lines: str
    link: str
    values: list[Value] = Field(default_factory=list)
    copy_same: bool = True
    only_if: list[Condition | str] = Field(default_factory=list)
    lines_where: list[Condition | str] = Field(default_factory=list, description="only the lines that match these")


@register("step", {"do": "notify", "who": "owner", "message": "{{number}} is ready to invoice"})
class Notify(Base):
    """Tell people in the app: who is owner, admin, editor, everyone, a team's key, or a person field (assigned_to)."""
    do: Literal["notify"]
    who: str = "owner"
    message: str
    only_if: list[Condition | str] = Field(default_factory=list)


@register("step", {"do": "email", "to": "customer.email", "subject": "Your invoice {{number}}", "body": "Hi {{customer.name}}, your invoice is attached.", "attach": ["invoice"]})
class Email(Base):
    """Send an email: to is an email field or a linked one (customer.email). attach: document keys."""
    do: Literal["email"]
    to: str
    subject: str
    body: str
    attach: list[str] = Field(default_factory=list)
    only_if: list[Condition | str] = Field(default_factory=list)


@register("step", {"do": "text", "to": "customer.phone", "message": "Hi {{customer.name}}, your car is ready to collect."})
class Text(Base):
    """Send a text message (SMS): to is a phone field or a linked one (customer.phone)."""
    do: Literal["text"]
    to: str
    message: str
    only_if: list[Condition | str] = Field(default_factory=list)


@register("step", {"do": "document", "document": "invoice", "save_to": "pdf"})
class MakeDocument(Base):
    """Make the record's document (a PDF; a standard one is made for the list if it has none by that name), kept in a
    file field if save_to is given."""
    do: Literal["document"]
    document: str
    save_to: str | None = None
    only_if: list[Condition | str] = Field(default_factory=list)


@register("step", {"do": "task", "who": "assigned_to", "message": "Call {{customer.name}} about the delay"})
class Task(Base):
    """Give someone a task about the record."""
    do: Literal["task"]
    who: str = "editor"
    message: str
    only_if: list[Condition | str] = Field(default_factory=list)


@register("step", {"do": "approval", "who": "owner", "message": "Approve this refund?"})
class Approval(Base):
    """Ask someone to approve the record; the rule waits for their answer."""
    do: Literal["approval"]
    who: str = "owner"
    message: str = "Please approve this."
    only_if: list[Condition | str] = Field(default_factory=list)


@register("step", {"do": "wait", "days": 3})
class Wait(Base):
    """Wait some days before the next step."""
    do: Literal["wait"]
    days: int


@register("step", {"do": "payment_link", "amount": "balance", "save_to": "payment_link"})
class PaymentLink(Base):
    """A card payment page for an amount (a formula), its link kept in a url field."""
    do: Literal["payment_link"]
    amount: str
    save_to: str


REVIEW = Literal["never", "when_unsure", "always"]


@register("step", {"do": "ai_fill", "field": "summary", "instructions": "Summarise the job in one sentence for the crew.", "review": "never"})
class AiFill(Base):
    """The AI fills in one field of the record from what the record (and, with read_files, its attached files) says.
    review: whether a person checks its work first (never, when_unsure, always)."""
    do: Literal["ai_fill"]
    field: str
    instructions: str
    review: REVIEW = "never"
    reviewer: str | None = Field(None, description="who checks its work when it's checked: a role or a team (left out: the team)")
    read_files: bool = False
    use_knowledge: bool = False
    only_if: list[Condition | str] = Field(default_factory=list)


@register("step", {"do": "ai_extract", "source": "email", "fields": ["supplier", "amount", "due_date"]})
class AiExtract(Base):
    """The AI reads a document (a file field of the record, or "email"/"file" for what started the rule) and fills in
    these fields from it."""
    do: Literal["ai_extract"]
    source: str
    fields: list[str]
    instructions: str | None = None
    review: REVIEW = "never"


@register("step", {"do": "ai_create", "list": "bills", "instructions": "A bill from the supplier's email: supplier, amount, due date.",
                   "review": "when_unsure"})
class AiCreate(Base):
    """The AI makes a record of a list from what started the rule (an email, a file), filling in what it can;
    attach_to keeps the email's files on it."""
    do: Literal["ai_create"]
    list: str
    instructions: str
    attach_to: str | None = None
    review: REVIEW = "never"


@register("step", {"do": "ai_check", "field": "check_result", "instructions": "Pass if the quote has a price for every line.",
                   "notes_field": "check_notes"})
class AiCheck(Base):
    """The AI checks the record against instructions and puts its verdict in a choice field (and what it found in a
    notes field)."""
    do: Literal["ai_check"]
    field: str
    instructions: str
    notes_field: str | None = None
    review: REVIEW = "never"


@register("step", {"do": "assign", "field": "assigned_to", "among": "editor"})
class Assign(Base):
    """Give the record to someone, taking turns among a role or a team (fills a person field)."""
    do: Literal["assign"]
    field: str
    among: str = "editor"


@register("step", {"do": "update_each", "list": "job_lines", "link": "job", "values": [{"field": "description", "formula": "description + ' (done)'"}]})
class UpdateEach(Base):
    """Change each record of a list that links to the rule's record (every line of a job); values read the record
    being changed as item.x."""
    do: Literal["update_each"]
    list: str
    link: str
    values: list[Value]
    only_if: list[Condition | str] = Field(default_factory=list)


@register("step", {"do": "copy", "values": [{"field": "status", "to": "Draft"}], "lines": True})
class Copy(Base):
    """A copy of the record (and its lines): a new revision, a repeat order; values change what's different."""
    do: Literal["copy"]
    values: list[Value] = Field(default_factory=list)
    lines: bool = True


@register("step", {"do": "delete"})
class Delete(Base):
    """Delete the record the rule is about."""
    do: Literal["delete"]


@register("step", {"do": "run", "rule": "send_invoice"})
class RunRule(Base):
    """Run another rule on this record."""
    do: Literal["run"]
    rule: str


@register("step", {"do": "webhook", "url": "https://hooks.example.com/new-job", "method": "POST"})
class Webhook(Base):
    """Send the record to another system's web address."""
    do: Literal["webhook"]
    url: str
    method: Literal["POST", "PUT", "PATCH", "GET"] = "POST"


Step = union("step")


class Rule(Base):
    """What the app does by itself: when something happens (and only_if these hold), these steps."""
    key: str
    name: str
    when: When
    only_if: list[Condition | str] = Field(default_factory=list)
    steps: list[Step]


# --- views ----------------------------------------------------------------------------------------------------------

class View(Base):
    """Another way to see a list: a board (by stage), a calendar (by a date field), a timeline (date_field to
    end_field), a gallery (of image_field), a filtered list. A list with group_by shows its records under each value
    of that field, with counts and totals: unpaid invoices by customer (group_by customer), jobs by driver."""
    type: Literal["list", "board", "calendar", "gallery", "timeline", "map", "inbox"]
    label: Words
    filters: list[Condition | str] = Field(default_factory=list)
    date_field: str | None = None
    end_field: str | None = None
    image_field: str | None = None
    group_by: str | None = None


# --- patches: the changes a builder sends -----------------------------------------------------------------------------

@register("patch", {"do": "add_list", "key": "jobs", "label": "Jobs", "singular": "Job", "fields": [{"type": "link", "key": "customer", "label": "Customer", "to": "customers"}, {"type": "date", "key": "scheduled_for", "label": "Scheduled for"}], "stages": {"stages": ["Booked", "In progress", "Done"]}}, group="data")
class AddList(Base):
    """Add a list (a table): its fields and, if its records go through stages, its stages. inside: the list whose
    records hold these as lines (invoice lines inside an invoice; needs a link field to it)."""
    do: Literal["add_list"]
    key: str
    label: Words
    singular: Words
    fields: list[FieldDef]
    stages: Stages | None = None
    inside: str | None = None


@register("patch", {"do": "change_list", "list": "jobs", "label": "Work orders", "singular": "Work order"}, group="data")
class ChangeList(Base):
    """Rename a list."""
    do: Literal["change_list"]
    list: str
    label: Words | None = None
    singular: Words | None = None
    module: str | None = Field(None, description="the section of the menu it's in")
    icon: str | None = None
    title_field: str | None = Field(None, description="the field that names each record")


@register("patch", {"do": "remove_list", "list": "notes"}, group="data")
class RemoveList(Base):
    """Remove a list (its records are kept, hidden)."""
    do: Literal["remove_list"]
    list: str


@register("patch", {"do": "add_field", "list": "jobs", "field": {"type": "money", "key": "call_out_fee", "label": "Call-out fee"}}, group="data")
class AddField(Base):
    """Add a field to a list. Sent for one it has already, that field is made as sent."""
    do: Literal["add_field"]
    list: str
    field: FieldDef


@register("patch", {"do": "change_field", "list": "jobs", "field": {"type": "choice", "key": "priority", "label": "Priority", "options": ["Standard", "Urgent"]}}, group="data")
class ChangeField(Base):
    """Change a field: send it whole, with the same key."""
    do: Literal["change_field"]
    list: str
    field: FieldDef


@register("patch", {"do": "remove_field", "list": "jobs", "field": "fax"}, group="data")
class RemoveField(Base):
    """Remove a field (its values are kept, hidden)."""
    do: Literal["remove_field"]
    list: str
    field: str


@register("patch", {"do": "rename_choice", "list": "jobs", "field": "status", "old": "Done", "new": "Completed"}, group="data")
class RenameChoice(Base):
    """Rename a choice or a stage everywhere (records follow)."""
    do: Literal["rename_choice"]
    list: str
    field: str
    old: str
    new: str


@register("patch", {"do": "set_stages", "list": "quotes", "stages": {"stages": ["Draft", "Sent", "Accepted", "Declined"], "moves": [{"from": ["Draft"], "to": "Sent", "button": "Send"}, {"from": ["Sent"], "to": "Accepted", "button": "Accepted"}, {"from": ["Sent"], "to": "Declined", "button": "Declined"}]}}, group="data")
class SetStages(Base):
    """Set a list's stages and moves."""
    do: Literal["set_stages"]
    list: str
    stages: Stages


@register("patch", {"do": "add_rule", "rule": {"key": "job_done_invoice", "name": "Invoice a finished job", "when": {"on": "reaches", "list": "jobs", "stage": "Done"}, "steps": [{"do": "add", "list": "invoices", "values": [{"field": "job", "formula": "record"}]}, {"do": "notify", "who": "owner", "message": "{{record.title}} is done and invoiced"}]}}, group="behaviour")
class AddRule(Base):
    """Add a rule no feature covers (or replace one: same key)."""
    do: Literal["add_rule"]
    rule: Rule


@register("patch", {"do": "change_rule", "rule": "job_done_invoice", "only_if": [{"field": "billable", "op": "equals", "value": True}]}, group="behaviour")
class ChangeRule(Base):
    """Change part of a rule: only the parts sent are replaced; the rest stays as it is."""
    do: Literal["change_rule"]
    rule: str
    name: str | None = None
    when: When | None = None
    only_if: list[Condition | str] | None = None
    steps: list[Step] | None = None
    enabled: bool | None = Field(None, description="false switches the rule off (it's kept), true back on")


@register("patch", {"do": "remove_rule", "rule": "job_done_invoice"}, group="behaviour")
class RemoveRule(Base):
    """Remove a rule."""
    do: Literal["remove_rule"]
    rule: str


@register("patch", {"do": "remove_feature", "feature": "alert_invoices_paid"}, group="behaviour")
class RemoveFeature(Base):
    """Remove a feature and what it made."""
    do: Literal["remove_feature"]
    feature: str


@register("patch", {"do": "add_stage", "list": "jobs", "stage": "On hold", "after": "Booked", "button": "Put on hold"}, "data")
class AddStage(Base):
    """Add one stage to a list's stages, after another: a button moves a record from that stage into it, and from it a
    record moves on wherever it could from that stage. Everything else about the stages stays as it is."""
    do: Literal["add_stage"]
    list: str
    stage: str
    after: str
    button: str | None = None


# --- small changes: one setting of a field, one stage, one move, one step of a rule -----------------------------------

@register("patch", {"do": "set_field", "list": "jobs", "field": "fax", "settings": {"required": False, "help": "Only some customers have one"}}, group="data")
class SetField(Base):
    """Change some of a field's settings, leaving the rest as they are; null takes a setting away. Settings: label,
    help, required, unique, default, placeholder, options, multiple, sensitive, readonly, decimals, unit, currency,
    prefix, digits, min, max, visible_to (roles or teams who may see it), option_colors, before, after, yes_label,
    no_label."""
    do: Literal["set_field"]
    list: str
    field: str
    settings: dict[str, Any]


@register("patch", {"do": "order_fields", "list": "jobs", "fields": ["title", "customer", "scheduled_for"]}, group="data")
class OrderFields(Base):
    """Put a list's fields in this order on its forms and pages (any not named keep their places after them)."""
    do: Literal["order_fields"]
    list: str
    fields: list[str]


@register("patch", {"do": "remove_stage", "list": "jobs", "stage": "In progress"}, group="data")
class RemoveStage(Base):
    """Take one stage away (refused while records are at it); its moves go with it."""
    do: Literal["remove_stage"]
    list: str
    stage: str


@register("patch", {"do": "order_stages", "list": "jobs", "stages": ["Booked", "In progress", "Done"], "start": "Booked"}, group="data")
class OrderStages(Base):
    """Put a list's stages in this order (every one of them), and say which a new record starts at."""
    do: Literal["order_stages"]
    list: str
    stages: list[str]
    start: str | None = None


@register("patch", {"do": "add_move", "list": "quotes", "move": {"from": ["Sent"], "to": "Draft", "button": "Back to draft"}}, group="data")
class AddMove(Base):
    """Add one way people move a record on: a button from some stages to another."""
    do: Literal["add_move"]
    list: str
    move: Move


@register("patch", {"do": "change_move", "list": "quotes", "button": "Send", "move": {"from": ["Draft"], "to": "Sent", "button": "Send to customer", "who": ["owner"]}}, group="data")
class ChangeMove(Base):
    """Change one move, named by its button: sent whole as it should be."""
    do: Literal["change_move"]
    list: str
    button: str
    move: Move


@register("patch", {"do": "remove_move", "list": "quotes", "button": "Declined"}, group="data")
class RemoveMove(Base):
    """Take one move away, named by its button."""
    do: Literal["remove_move"]
    list: str
    button: str


@register("patch", {"do": "remove_stages", "list": "orders"}, group="data")
class RemoveStages(Base):
    """A list no longer goes through stages (its stage field stays, as a plain choice)."""
    do: Literal["remove_stages"]
    list: str


@register("patch", {"do": "add_step", "rule": "job_done_invoice", "step": {"do": "notify", "who": "owner", "message": "An invoice was made for {{record.title}}"}}, group="behaviour")
class AddStep(Base):
    """Add one step to a rule, at a place (1 is first; left out, last)."""
    do: Literal["add_step"]
    rule: str
    step: Step
    at: int | None = None


@register("patch", {"do": "change_step", "rule": "job_done_invoice", "at": 1, "step": {"do": "add", "list": "invoices", "values": [{"field": "job", "formula": "record"}, {"field": "due_date", "formula": "today() + 14"}]}}, group="behaviour")
class ChangeStep(Base):
    """Change one step of a rule, by its place (1 is first): sent whole as it should be."""
    do: Literal["change_step"]
    rule: str
    at: int
    step: Step


@register("patch", {"do": "remove_step", "rule": "job_done_invoice", "at": 2}, group="behaviour")
class RemoveStep(Base):
    """Take one step away from a rule, by its place (1 is first)."""
    do: Literal["remove_step"]
    rule: str
    at: int


@register("patch", {"do": "move_step", "rule": "job_done_invoice", "at": 2, "to": 1}, group="behaviour")
class MoveStep(Base):
    """Move one step of a rule to another place (1 is first)."""
    do: Literal["move_step"]
    rule: str
    at: int
    to: int


@register("patch", {"do": "add_view", "list": "jobs", "view": {"type": "calendar", "label": "Job calendar", "date_field": "scheduled_for"}}, group="data")
class AddView(Base):
    """Add a view to a list."""
    do: Literal["add_view"]
    list: str
    view: View


@register("patch", {"do": "add_records", "list": "services", "records": [{"name": "Gutter clean", "price": 180}]}, group="data")
class AddRecords(Base):
    """Add records to a list (starting data the owner gave: their staff, their services)."""
    do: Literal["add_records"]
    list: str
    records: list[dict]


@register("patch", {"do": "add_validation", "list": "payments", "check": "amount <= invoice.total",
                     "message": "A payment can't be more than its invoice's total"}, "behaviour")
class AddValidation(Base):
    """A rule every record of a list must keep when it's added or changed, whoever does it (the owner, staff, the app's
    own rules), so what breaks it is refused ("can't book unless…", "never more than…", "not while…"): check is a
    formula that must be true: its own fields (end_date >= start_date), a linked record's (student.deposit_paid >= 50),
    or other records, those linking to it or a whole list, counted or tested (count(client.invoices where status =
    "Unpaid" and days_since(due_date) > 30) == 0; not any(bookings where room == this.room and id != this.id and
    overlaps(starts_on, ends_on, this.starts_on, this.ends_on))); message what people are told when it isn't; only_if:
    only while these hold. Sent again with the same check, it replaces that rule. Holding a record back from a stage is a
    gate."""
    do: Literal["add_validation"]
    list: str
    check: str
    message: str
    only_if: list[Condition | str] = Field(default_factory=list)


@register("patch", {"do": "remove_validation", "list": "payments", "check": "amount <= invoice.total"}, "behaviour")
class RemoveValidation(Base):
    """Take away a rule a list's records must keep (one add_validation made), named by its check or its message, as
    the app shows it."""
    do: Literal["remove_validation"]
    list: str
    check: str


@register("patch", {"do": "set_team", "key": "finance", "name": "Finance", "about": "Pays the bills and chases what's owed"}, "data")
class SetTeam(Base):
    """A team people can be put in (Finance, Makers): lists, fields, moves and tasks can be for a team."""
    do: Literal["set_team"]
    key: str
    name: str
    about: str | None = None


@register("patch", {"do": "set_access", "list": "invoices", "view": "finance", "edit": "finance"}, "data")
class SetAccess(Base):
    """Who may see and change a list's records: view and edit are a role (viewer, editor, admin, owner) or a team's
    key (only Finance). Admins and the owner always may."""
    do: Literal["set_access"]
    list: str
    view: str | None = None
    edit: str | None = None


@register("patch", {"do": "own_records", "list": "jobs", "field": "assigned_to", "roles": ["editor"], "see_all": ["office"]}, "data")
class OwnRecords(Base):
    """People with these roles see only the records where a person field is them (each salesperson their own deals);
    see_all: teams who still see every record."""
    do: Literal["own_records"]
    list: str
    field: str
    roles: list[Literal["viewer", "editor", "admin"]] = Field(default_factory=lambda: ["viewer", "editor"])
    see_all: list[str] = Field(default_factory=list)


@register("patch", {"do": "add_document", "list": "jobs", "key": "job_report", "label": "Job report", "title": "Job report"}, "data")
class AddDocument(Base):
    """A PDF each record of a list can be made into (a quote, an invoice, a report, a certificate), laid out by the
    platform from the record's fields and lines. A rule's document step or a send_document feature makes and sends it."""
    do: Literal["add_document"]
    list: str
    key: str
    label: Words
    title: str | None = None


@register("patch", {"do": "import_rows", "file": 12, "sheet": "Customers", "list": "customers", "columns": {"Customer name": "name", "E-mail": "email"}}, "data")
class ImportRows(Base):
    """Bring an attached spreadsheet's rows into a list: each column heading to the field it fills in (a linked
    record's field as customer.email, a line's as order_lines.quantity). Only when the owner wants their rows in."""
    do: Literal["import_rows"]
    file: int = Field(description="the attached file's id")
    list: str
    columns: dict[str, str]
    sheet: str | None = None


@register("patch", {"do": "design_op", "op": {"op": "add_page", "page": {"key": "today", "label": "Today", "widgets": []}}}, group="behaviour")
class DesignOp(Base):
    """Only for what no other patch makes: an op of the platform's design format for pages, documents, forms,
    portals, flows, syncs, spaces, teams, the theme or the app's name; or update_table for who may see and change a
    list's records (access, row_access, rules, unique_together). Lists, fields, stages and rules are never made here."""
    do: Literal["design_op"]
    op: dict



Total.model_rebuild()
Move.model_rebuild()
Plain.model_rebuild()
Choice.model_rebuild()
Link.model_rebuild()
