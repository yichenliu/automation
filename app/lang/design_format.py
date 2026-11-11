"""The platform's design format as a builder is told it: what a design_op patch can say. These are what no other patch
makes: a list's settings (who sees which records, what outside people see, sealed records, held changes), the portals,
teams' menus, pages, flows, syncs and the theme. Each line is said from the platform's own definition of that part
(the docstrings of app/blueprint/schema.py's normalizers), so the reference and what the platform does can't drift."""
import json

from ..blueprint import schema


def _doc(fn):
    """A normalizer's docstring, as one line."""
    return " ".join((fn.__doc__ or "").split())


# A list's settings that only design_op's update_table changes: key, shape, what it does (or the normalizer saying it).
LIST_SETTINGS = [
    ("portal", '{via, fields?, can_add?, edit?, edit_if?, show_if?} or {everyone: true, fields?, show_if?}',
     "Which records of the list outside people see in their portal: those linked to them through via: the link to the "
     "portal's people, or to a record that links to them or that their own records name (a certificate's seller: each "
     "person of that seller), or through one more link (\"case.seller\"); on the list their own records name, via is "
     "that link of theirs (a seller's profile: \"seller\"). fields: what they see; can_add: they add them; edit: "
     "the fields they may change; edit_if: only while these conditions hold (until the tender closes); show_if: only "
     "the records meeting these (published answers). everyone: every outside person sees them (a published price list)."),
    ("row_access", '{field, roles?, teams?, shared_when_empty?}, or a list of several that must all hold',
     "People with these roles (viewer, editor) see only the records where the person field is them, or through links "
     "(\"buyer.members\": the record's organisation's people); teams: who still see every record; shared_when_empty: a "
     "record with no one there is everyone's (the platform's standard ones, beside each organisation's own)."),
    ("sealed", '{until, keep_open?}',
     "Nobody but whoever made a record sees what's in it until a date and time, on it or through its link "
     "(\"tender.bids_close_at\"), not even administrators; after it, it can't be changed, except keep_open (the fields "
     "the business fills in once it's open). Every opening is kept in its history."),
    ("approve_changes", "{fields?, approver?, wait_days?, tell?, told_first?, step_up?}", schema.normalize_change_approval),
    ("journey", "[{label, when?} | {label, table, via, when?}]", schema.normalize_journey),
    ("keep_for", "{years | months | days, from?, unless?}", schema.normalize_keep_for),
    ("lock_if", "[conditions], with lock_except: [fields]",
     "Records meeting these can't be changed (except lock_except's fields): a sent order, a signed contract."),
    ("unique_together", "[[fields]]",
     "No two records alike in these fields together (a seller's invoice number: [[\"seller\", \"invoice_number\"]])."),
    ("sections", "[{label, fields} | {label, lines, show_if?}], with wizard: true", schema.normalize_section),
    ("layout", '"tabs"', "A record's page shows its sections as tabs."),
    ("colors", "[{when, color, field?}]", schema.normalize_colour_rule),
    ("limits", "[{per, max, where?, message?} | {per, overlap: {start, end}}]", schema.normalize_limit),
    ("audit_views", "true", "Every time someone opens one of its records is kept in its history."),
    ("public_form", "{enabled?, title?, fields?, ...}", schema.normalize_form),
]

OPS = [
    ("set_portal", "{table, email_field?, title?, welcome?, profile?, sign_up?}", schema.normalize_portal,
     {"op": "set_portal", "table": "seller_contacts", "email_field": "email", "title": "Seller portal", "sign_up": {"fields": ["name", "seller"]}}),
    ("set_space", "{key, label, for, groups, links?}", schema.normalize_space,
     {"op": "set_space", "key": "receiving", "label": "Receiving", "for": ["receivers"], "groups": [{"label": "Today", "items": [{"table": "goods_receipts"}]}]}),
    ("add_page | update_page", "{key, label, widgets, home_for?, access?, module?}", schema.normalize_page,
     {"op": "add_page", "page": {"key": "ap_home", "label": "Accounts payable", "home_for": ["accounts_payable"],
                                 "widgets": [{"type": "my_work"}, {"type": "list", "table": "invoices", "label": "Exceptions",
                                              "filters": [{"field": "status", "op": "equals", "value": "Exception"}]}]}}),
    ("add_flow | update_flow", "{key, label, table, start, steps, then?}", schema.normalize_flow, None),
    ("set_sync", "{key, app, pull, into, match, fields, lines?, every?}", schema.normalize_sync, None),
    ("set_theme", "{accent?, density?}", "The app's accent colour and how tightly it's laid out.", None),
    ("rename_app", "{name}", "The app's name.", None),
]


def reference():
    """The DESIGN block of the catalog."""
    lines = ["DESIGN OPS (the \"op\" of a design_op patch; only for what no patch above makes):",
             "- update_table {table, then any of these settings of the list, each by its key}:"]
    for key, shape, said in LIST_SETTINGS:
        lines.append(f"  - {key} {shape}: {said if isinstance(said, str) else _doc(said)}")
    for name, shape, said, example in OPS:
        line = f"- {name} {shape}: {said if isinstance(said, str) else _doc(said)}"
        if example:
            line += f" e.g. {json.dumps(example, ensure_ascii=False)}"
        lines.append(line)
    lines.append("  Widgets (a page's, by \"type\"): " + _doc(schema.normalize_widget))
    return "\n".join(lines)
