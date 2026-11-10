"""What a blueprint is made of, how loosely-written pieces (from the AI) are tidied into it, and how it is checked.

A blueprint:
    {"name": "Eth Analytic", "theme": {"accent", "density"}, "modules": [{"key", "label", "icon"}],
     "tables": [{"key", "label", "singular", "module", "icon", "title_field", "embedded", "description",
                 "fields": [...], "views": [...], "sections": [...], "documents": [...], "public_form": {...}}],
     "pages": [{"key", "label", "module", "icon", "widgets": [...]}],
     "automations": [{"key", "name", "enabled", "trigger": {...}, "conditions": [...], "actions": [...]}]}

`normalize_*` accept the many ways a model (or a person) writes the same thing and return the one canonical form, or
raise SchemaError with a sentence saying what's wrong. `check(blueprint)` finds references that don't line up.
"""
import contextvars
import re
from datetime import date, timedelta

from .. import icons
from . import expr

FIELD_TYPES = ["text", "long_text", "rich_text", "number", "money", "percent", "duration", "rating", "date", "datetime",
               "time", "checkbox", "select", "multi_select", "tags", "email", "phone", "url", "address", "location", "barcode",
               "color", "json", "link", "user", "file", "image", "signature", "autonumber", "formula", "rollup", "lookup",
               "created_time", "modified_time", "created_by", "modified_by", "button"]
STORED = {"text", "long_text", "rich_text", "number", "money", "percent", "duration", "rating", "date", "datetime", "checkbox",
          "select", "multi_select", "email", "phone", "url", "address", "location", "barcode", "link", "user", "autonumber",
          "time", "tags", "color", "json"}
SYSTEM = {"created_time", "modified_time", "created_by", "modified_by"}  # kept by the platform on every record
DATES = {"date", "datetime"}
ROLE_NAMES = ["viewer", "editor", "admin", "owner"]  # as models.ROLES: each can do everything the ones before it can
WHO_WORDS = {"viewer", "editor", "admin", "owner", "creator", "portal", "everyone"}  # never a team's key
FILES = {"file", "image", "signature"}  # kept as File rows, not in the record's data
CALCULATED = {"formula", "rollup", "lookup", "autonumber", "created_time", "modified_time", "created_by", "modified_by", "button"}
AI_FILLABLE = {"text", "long_text", "rich_text", "number", "money", "percent", "date", "datetime", "checkbox", "select",
               "multi_select", "rating", "address", "link"}  # a link: the AI picks the matching record (the invoice a payment is for)
COLOURS_NAMED = ["red", "orange", "amber", "yellow", "green", "teal", "blue", "indigo", "violet", "pink", "gray"]
CHOICE_COLOURS = ["slate", "blue", "amber", "green", "violet", "red", "teal", "pink"]  # a choice's badge, set by hand
NUMERIC = {"number", "money", "percent"}
SENSITIVE_TYPES = {"text", "number", "phone", "email", "barcode"}  # what can be kept encrypted and shown masked
RESERVED = {"id", "created_at", "updated_at", "created_by", "record", "today", "now", "payload"}
TYPE_ALIASES = {
    "string": "text", "str": "text", "varchar": "text", "char": "text", "short_text": "text", "single_line": "text",
    "textarea": "long_text", "longtext": "long_text", "notes": "long_text", "paragraph": "long_text",
    "multiline": "long_text", "multi_line": "long_text", "description": "long_text",
    "markdown": "rich_text", "formatted_text": "rich_text", "html": "rich_text", "richtext": "rich_text", "rich": "rich_text",
    "multiselect": "multi_select", "multiple_select": "multi_select", "tags": "multi_select", "multiple_choice": "multi_select",
    "checklist": "multi_select", "labels": "multi_select", "multi_choice": "multi_select",
    "person": "user", "assignee": "user", "owner": "user", "member": "user", "staff": "user", "people": "user", "users": "user",
    "attachment": "file", "attachments": "file", "files": "file", "document": "file", "documents": "file", "upload": "file",
    "street_address": "address", "postal_address": "address", "addr": "address",
    "time_of_day": "time", "clock": "time", "hour": "time", "colour": "color", "hex": "color", "swatch": "color",
    "tag": "tags", "keywords": "tags", "free_tags": "tags", "object": "json", "data": "json", "raw": "json", "payload": "json",
    "created": "created_time", "created_on": "created_time", "date_created": "created_time", "added_on": "created_time",
    "modified": "modified_time", "updated": "modified_time", "last_modified": "modified_time", "updated_on": "modified_time",
    "last_updated": "modified_time", "created_by_user": "created_by", "author": "created_by", "added_by": "created_by",
    "modified_by_user": "modified_by", "updated_by": "modified_by", "last_modified_by": "modified_by",
    "action": "button", "action_button": "button", "run_button": "button",
    "gps": "location", "geo": "location", "coordinates": "location", "latlng": "location", "lat_lng": "location", "geolocation": "location",
    "sign": "signature", "signoff": "signature", "sign_off": "signature", "e_signature": "signature", "esignature": "signature",
    "barcode_field": "barcode", "qr": "barcode", "qr_code": "barcode", "ean": "barcode", "upc": "barcode", "scan": "barcode",
    "photo": "image", "picture": "image", "logo": "image", "photos": "image", "img": "image",
    "stars": "rating", "score": "rating", "rate": "rating",
    "time_spent": "duration", "length": "duration", "elapsed": "duration",
    "related": "lookup", "linked_field": "lookup", "lookup_field": "lookup",
    "int": "number", "integer": "number", "float": "number", "decimal": "number", "numeric": "number", "quantity": "number",
    "currency": "money", "price": "money", "amount": "money", "dollar": "money", "dollars": "money", "cost": "money",
    "percentage": "percent", "pct": "percent",
    "timestamp": "datetime", "time": "datetime", "date_time": "datetime", "date_and_time": "datetime", "appointment": "datetime",
    "due_date": "date",
    "bool": "checkbox", "boolean": "checkbox", "yes_no": "checkbox", "toggle": "checkbox", "flag": "checkbox",
    "dropdown": "select", "enum": "select", "choice": "select", "choices": "select", "status": "select",
    "single_select": "select", "category": "select", "picklist": "select", "option": "select", "options": "select",
    "relation": "link", "relationship": "link", "reference": "link", "foreign_key": "link",
    "belongs_to": "link", "many_to_one": "link", "fk": "link", "linked_record": "link", "link_to": "link", "ref": "link",
    "auto_number": "autonumber", "sequence": "autonumber", "serial": "autonumber", "auto_increment": "autonumber",
    "computed": "formula", "calculated": "formula", "calculation": "formula", "calc": "formula",
    "aggregate": "rollup", "summary": "rollup", "sum": "rollup", "count": "rollup",
    "phone_number": "phone", "tel": "phone", "telephone": "phone", "mobile": "phone",
    "website": "url", "web": "url", "link_url": "url", "hyperlink": "url",
    "e-mail": "email", "mail": "email", "email_address": "email",
}
FORMATS = ["number", "money", "percent", "date", "text", "checkbox"]
AGGREGATES = ["count", "sum", "avg", "min", "max", "median"]
AGG_ALIASES = {"average": "avg", "mean": "avg", "total": "sum", "number": "count", "minimum": "min", "maximum": "max", "middle": "median",
               "typical": "median"}

NOT_ONE_OF = ("not_one_of", "none_of", "not_in", "is_not_one_of", "neither", "not_any_of")  # said as "isn't this or that"
CONDITION_OPS = ["equals", "not_equals", "contains", "is_empty", "not_empty", "greater_than", "less_than", "at_least",
                 "at_most", "one_of", "before_today", "after_today", "within_next_days", "within_last_days",
                 "more_than_days_ago", "this_week", "last_week", "this_month", "last_month", "this_quarter", "last_quarter",
                 "this_year", "last_year", "this_financial_year", "last_financial_year",
                 "today", "tomorrow", "yesterday", "next_week", "next_month", "changed", "changed_to"]
PERIOD_OPS = ("this_week", "last_week", "this_month", "last_month", "this_quarter", "last_quarter", "this_year", "last_year",
              "this_financial_year", "last_financial_year", "today", "tomorrow", "yesterday", "next_week", "next_month")
OP_ALIASES = {
    "=": "equals", "==": "equals", "is": "equals", "eq": "equals", "equal": "equals", "equal_to": "equals",
    "!=": "not_equals", "<>": "not_equals", "is_not": "not_equals", "neq": "not_equals", "not": "not_equals",
    "not_equal": "not_equals", "ne": "not_equals",
    ">": "greater_than", "gt": "greater_than", "greater": "greater_than", "more_than": "greater_than", "after": "greater_than",
    "<": "less_than", "lt": "less_than", "less": "less_than", "fewer_than": "less_than", "before": "less_than",
    ">=": "at_least", "gte": "at_least", "greater_or_equal": "at_least", "<=": "at_most", "lte": "at_most", "less_or_equal": "at_most",
    "includes": "contains", "like": "contains", "has": "contains",
    "empty": "is_empty", "blank": "is_empty", "is_blank": "is_empty", "is_null": "is_empty", "null": "is_empty", "missing": "is_empty",
    "is_not_empty": "not_empty", "exists": "not_empty", "is_set": "not_empty", "not_blank": "not_empty", "has_value": "not_empty",
    "in": "one_of", "any_of": "one_of", "is_one_of": "one_of",
    "overdue": "before_today", "in_past": "before_today", "is_past": "before_today", "past": "before_today",
    "in_future": "after_today", "is_future": "after_today", "future": "after_today",
    "is_today": "today", "on_today": "today", "due_today": "today", "is_tomorrow": "tomorrow", "is_yesterday": "yesterday",
    "due_within": "within_next_days", "in_next_days": "within_next_days", "next_days": "within_next_days",
    "in_last_days": "within_last_days", "last_days": "within_last_days", "within_last": "within_last_days",
    "within_past_days": "within_last_days", "in_the_last_days": "within_last_days", "past_days": "within_last_days",
    "current_week": "this_week", "previous_week": "last_week", "prior_week": "last_week", "current_month": "this_month",
    "previous_month": "last_month", "prior_month": "last_month", "current_quarter": "this_quarter",
    "previous_quarter": "last_quarter", "current_year": "this_year", "previous_year": "last_year",
    "this_fy": "this_financial_year", "current_financial_year": "this_financial_year", "this_fiscal_year": "this_financial_year",
    "last_fy": "last_financial_year", "previous_financial_year": "last_financial_year", "last_fiscal_year": "last_financial_year",
    "older_than": "more_than_days_ago", "days_ago": "more_than_days_ago", "older_than_days": "more_than_days_ago",
    "was_changed": "changed", "is_changed": "changed", "changes": "changed", "modified": "changed",
    "becomes": "changed_to", "changes_to": "changed_to", "became": "changed_to", "set_to": "changed_to", "changed_into": "changed_to",
}
NO_VALUE = {"is_empty", "not_empty", "before_today", "after_today", "changed", *PERIOD_OPS}


def period(name, today, fy_start=7):
    """The first and last day of a today / this_week / last_month / this_quarter… period (weeks start on Monday), or None. The
    financial year starts in the month `fy_start` (July: Australia's)."""
    if name in ("today", "tomorrow", "yesterday"):
        day = today + timedelta(days={"today": 0, "tomorrow": 1, "yesterday": -1}[name])
        return day, day
    if name == "next_week":
        first = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
        return first, first + timedelta(days=6)
    if name == "next_month":
        first = expr._add_months(date(today.year, today.month, 1), 1)
        return first, expr._add_months(first, 1) - timedelta(days=1)
    if name in ("this_financial_year", "last_financial_year"):
        year = today.year if today.month >= fy_start else today.year - 1
        year -= name == "last_financial_year"
        first = date(year, fy_start, 1)
        return first, expr._add_months(first, 12) - timedelta(days=1)
    if name in ("this_week", "last_week"):
        first = today - timedelta(days=today.weekday(), weeks=name == "last_week")
        return first, first + timedelta(days=6)
    if name in ("this_month", "last_month", "this_quarter", "last_quarter"):
        span = 3 if name.endswith("quarter") else 1
        first = date(today.year, span * ((today.month - 1) // span) + 1, 1)
        if name.startswith("last"):
            first = expr._add_months(first, -span)
        return first, expr._add_months(first, span) - timedelta(days=1)
    if name in ("this_year", "last_year"):
        year = today.year - (name == "last_year")
        return date(year, 1, 1), date(year, 12, 31)
    return None


TRIGGERS = ["record_created", "record_updated", "record_created_or_updated", "record_deleted", "schedule", "button",
            "email_received", "file_added", "webhook_received"]
TRIGGER_ALIASES = {
    "created": "record_created", "on_create": "record_created", "new_record": "record_created", "record_added": "record_created",
    "create": "record_created", "on_created": "record_created", "record_create": "record_created", "insert": "record_created",
    "updated": "record_updated", "on_update": "record_updated", "record_changed": "record_updated", "field_changed": "record_updated",
    "update": "record_updated", "changed": "record_updated", "on_change": "record_updated", "record_update": "record_updated",
    "status_changed": "record_updated", "field_updated": "record_updated",
    "created_or_updated": "record_created_or_updated", "saved": "record_created_or_updated", "record_saved": "record_created_or_updated",
    "on_save": "record_created_or_updated", "record_created_or_changed": "record_created_or_updated",
    "scheduled": "schedule", "daily": "schedule", "weekly": "schedule", "hourly": "schedule", "time": "schedule", "cron": "schedule",
    "timer": "schedule", "every_day": "schedule", "recurring": "schedule",
    "manual": "button", "on_click": "button", "action": "button", "click": "button",
    "email": "email_received", "new_email": "email_received", "on_email": "email_received", "mail_received": "email_received",
    "email_arrives": "email_received", "inbox": "email_received", "incoming_email": "email_received",
    "deleted": "record_deleted", "on_delete": "record_deleted", "record_removed": "record_deleted", "removed": "record_deleted",
    "webhook": "webhook_received", "incoming_webhook": "webhook_received", "api": "webhook_received", "http": "webhook_received",
    "inbound_webhook": "webhook_received", "on_webhook": "webhook_received",
    "file": "file_added", "new_file": "file_added", "file_uploaded": "file_added", "onedrive_file": "file_added", "on_file": "file_added",
}
EVERY = ["hour", "day", "week", "month"]
ACTIONS = ["update_record", "create_record", "create_for_each", "update_each", "delete_record", "assign", "allocate", "wait", "notify",
           "send_email", "generate_document", "request_approval", "webhook", "ai_fill", "ai_extract", "ai_create_record",
           "branch", "task", "run_automation", "ai_check", "copy_record", "payment_link", "send_sms", "import_rows"]
APPROVAL_MODES = ["any", "all"]  # one of the approvers decides, or every one of them must approve
AI_STEPS = ("ai_fill", "ai_extract", "ai_create_record", "ai_check")
# Whether a person checks an AI step's work before it counts: every time, only when the AI isn't sure (and now and then
# at random, so its track record stays honest), or never (harmless enrichment: summaries, tags).
REVIEW_MODES = ["always", "when_unsure", "never"]
ACTION_ALIASES = {
    "update": "update_record", "set_field": "update_record", "set_fields": "update_record", "update_field": "update_record",
    "set_value": "update_record", "set": "update_record", "update_fields": "update_record", "edit_record": "update_record",
    "create": "create_record", "add_record": "create_record", "insert": "create_record", "new_record": "create_record",
    "create_records": "create_record", "insert_record": "create_record",
    "notification": "notify", "alert": "notify", "send_notification": "notify", "message": "notify", "remind": "notify",
    "reminder": "notify", "notify_user": "notify",
    "ai": "ai_fill", "ai_write": "ai_fill", "ai_generate": "ai_fill", "ai_classify": "ai_fill", "ai_summarize": "ai_fill",
    "ai_summarise": "ai_fill", "generate": "ai_fill", "ai_field": "ai_fill", "ai_update": "ai_fill", "ai_set": "ai_fill",
    "copy_lines": "create_for_each", "for_each": "create_for_each", "create_each": "create_for_each", "copy_records": "create_for_each",
    "email": "send_email", "send_mail": "send_email", "mail": "send_email", "email_customer": "send_email",
    "sms": "send_sms", "text": "send_sms", "send_text": "send_sms", "text_message": "send_sms", "send_text_message": "send_sms",
    "pdf": "generate_document", "create_pdf": "generate_document", "generate_pdf": "generate_document", "document": "generate_document",
    "make_document": "generate_document", "create_document": "generate_document",
    "approval": "request_approval", "approve": "request_approval", "ask_approval": "request_approval", "get_approval": "request_approval",
    "http": "webhook", "post": "webhook", "call_url": "webhook", "api_call": "webhook",
    "import": "import_rows", "import_file": "import_rows", "import_spreadsheet": "import_rows",
    "ai_read": "ai_extract", "extract": "ai_extract", "read_document": "ai_extract", "ai_read_file": "ai_extract",
    "ai_create": "ai_create_record", "create_from_email": "ai_create_record", "create_from_file": "ai_create_record",
    "ai_intake": "ai_create_record", "intake": "ai_create_record",
    "delete": "delete_record", "remove_record": "delete_record", "remove": "delete_record", "archive": "delete_record",
    "update_all": "update_each", "update_lines": "update_each", "update_related": "update_each", "for_each_update": "update_each",
    "round_robin": "assign", "assign_to": "assign", "assign_user": "assign",
    "delay": "wait", "pause": "wait", "sleep": "wait", "wait_until": "wait", "wait_for": "wait",
    "if": "branch", "if_else": "branch", "decision": "branch", "switch": "branch", "route": "branch", "split": "branch",
    "branches": "branch", "condition": "branch", "choose": "branch",
    "human_task": "task", "todo": "task", "to_do": "task", "assign_task": "task", "create_task": "task", "user_task": "task",
    "manual_step": "task", "ask_to_do": "task",
    "ai_verify": "ai_check", "ai_review": "ai_check", "verify": "ai_check", "check": "ai_check", "ai_compare": "ai_check",
    "ai_validate": "ai_check", "validate": "ai_check", "ai_audit": "ai_check", "compare": "ai_check",
    "call_automation": "run_automation", "start_automation": "run_automation", "trigger_automation": "run_automation",
    "sub_workflow": "run_automation", "subflow": "run_automation", "run_workflow": "run_automation",
    "duplicate": "copy_record", "duplicate_record": "copy_record", "copy": "copy_record", "revise": "copy_record",
    "new_revision": "copy_record", "new_version": "copy_record", "clone": "copy_record", "renew": "copy_record",
}
VIEW_TYPES = ["list", "board", "calendar", "gallery", "timeline", "map", "tree", "sheet", "inbox"]
VIEW_ALIASES = {"table": "list", "grid": "list", "kanban": "board", "pipeline": "board", "columns": "board",
                "queue": "inbox", "split": "inbox", "triage": "inbox", "work_through": "inbox", "master_detail": "inbox",
                "cal": "calendar", "schedule": "calendar", "agenda": "calendar", "cards": "gallery", "tiles": "gallery",
                "gantt": "timeline", "roadmap": "timeline", "plan": "timeline", "geo": "map", "locations": "map",
                "hierarchy": "tree", "outline": "tree", "nested": "tree", "org_chart": "tree", "spreadsheet": "sheet",
                "excel": "sheet", "edit_grid": "sheet", "bulk_edit": "sheet"}
WIDGETS = ["metric", "chart", "list", "goal", "text", "pivot", "form", "actions", "activity", "heading", "map", "embed", "insight", "my_work"]
WIDGET_ALIASES = {"my_tasks": "my_work", "inbox": "my_work", "waiting_for_me": "my_work", "my_approvals": "my_work", "work_queue": "my_work",
                  "todo_list": "my_work", "kpi": "metric", "stat": "metric", "number": "metric", "card": "metric", "count": "metric", "total": "metric",
                  "bar": "chart", "bar_chart": "chart", "graph": "chart", "pie": "chart", "pie_chart": "chart", "breakdown": "chart",
                  "line": "chart", "line_chart": "chart", "donut": "chart", "trend": "chart",
                  "table": "list", "records": "list", "recent": "list", "list_view": "list",
                  "progress": "goal", "target": "goal", "gauge": "goal", "note": "text", "markdown": "text",
                  "pivot_table": "pivot", "crosstab": "pivot", "matrix": "pivot", "report": "pivot", "summary_table": "pivot",
                  "quick_add": "form", "entry_form": "form", "add_form": "form", "input": "form",
                  "buttons": "actions", "shortcuts": "actions", "quick_actions": "actions", "links": "actions",
                  "feed": "activity", "recent_changes": "activity", "timeline_feed": "activity", "log": "activity",
                  "title": "heading", "section": "heading", "header": "heading", "divider": "heading",
                  "geo": "map", "locations": "map", "iframe": "embed", "external": "embed", "web_page": "embed", "powerbi": "embed",
                  "ai": "insight", "ai_insight": "insight", "commentary": "insight", "summary": "insight", "analysis": "insight"}
CHART_STYLES = ["bar", "line", "donut", "funnel"]
PERIODS = ["week", "month", "quarter", "year"]
DENSITIES = ["comfortable", "compact"]
APPROVER_ROLES = ["owner", "admin", "editor"]


class SchemaError(ValueError):
    pass


def slug(text, fallback=""):
    key = re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")
    if key and key[0].isdigit():
        key = f"f_{key}"
    return key[:48].rstrip("_") or fallback  # cut, then tidied: a key made again from itself is the same key


ACRONYMS = {"gst", "abn", "acn", "id", "po", "sku", "vat", "url", "pdf", "crm", "erp", "kpi", "ytd", "qty", "abn", "bsb", "eta",
            "hr", "it", "sla", "roi", "tfn", "rrp", "ndis", "pos"}


def label_of(key):
    words = str(key).replace("_", " ").strip().split()
    words = [w.upper() if w.lower() in ACRONYMS else w for w in words]
    text = " ".join(words)
    return text[:1].upper() + text[1:] if text else text


def _first(raw, *names, default=None):
    for name in names:
        if isinstance(raw, dict) and raw.get(name) not in (None, ""):
            return raw[name]
    return default


def _names(raw):
    """One name or several: "Draft", "Draft, Rejected" or ["Draft", "Rejected"]."""
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return [str(x) for x in _list(raw) if str(x).strip()]


def _options(raw):
    if isinstance(raw, str):
        raw = [o for o in re.split(r"[,;|/]", raw)]
    out = []
    for o in raw or []:
        if isinstance(o, dict):
            o = _first(o, "label", "value", "name")
        o = str(o or "").strip()
        if o and o.lower() not in [x.lower() for x in out]:
            out.append(o)
    return out


def field_type(raw_type):
    raw = str(raw_type or "text").strip().lower()
    t = raw if raw in FIELD_TYPES else TYPE_ALIASES.get(raw) or TYPE_ALIASES.get(slug(raw)) or slug(raw)  # a real type's own name wins
    if t not in FIELD_TYPES:
        raise SchemaError(f"“{raw_type}” isn't a field type; use one of: {', '.join(FIELD_TYPES)}")
    return t


def normalize_field(raw):
    """Returns the canonical field, or None for fields the platform already provides (id, created/updated times)."""
    if isinstance(raw, str):
        raw = {"label": raw}
    if not isinstance(raw, dict):
        raise SchemaError("a field must be an object with at least a label and a type")
    label = str(_first(raw, "label", "name", "title", "key", default="")).strip()
    key = slug(_first(raw, "key", "id", "name", default=label))
    if not key:
        raise SchemaError("a field needs a label")
    if key in RESERVED:
        return None
    if not label or label == key:
        label = label_of(key)
    ftype = field_type(_first(raw, "type", "kind", "field_type", default="text"))
    field = {"key": key, "label": label, "type": ftype}
    if raw.get("feature"):  # made by a feature of the Keel language (app/lang)
        field["feature"] = slug(raw["feature"])
    if raw.get("required") in (True, "true", "yes", 1):
        field["required"] = True
    help_text = _first(raw, "help", "description", "hint")
    if help_text:
        field["help"] = str(help_text)[:200]
    default = _first(raw, "default", "default_value", "initial")
    if default is not None and ftype not in CALCULATED | FILES:
        field["default"] = default
    if raw.get("unique") in (True, "true", "yes", 1) and ftype in ("text", "email", "phone", "url", "number", "barcode"):
        field["unique"] = True
    for bound in ("min", "max"):
        value = expr.to_number(raw.get(bound))
        if value is not None and ftype in NUMERIC | {"rating", "duration"}:
            field[bound] = value
    placeholder = _first(raw, "placeholder", "example")
    if placeholder and ftype in STORED:
        field["placeholder"] = str(placeholder)[:80]
    show_if = _first(raw, "show_if", "visible_if", "only_if", "show_when")
    if show_if:
        field["show_if"] = [normalize_condition(c) for c in _list(show_if)]
    hide_if = _first(raw, "hide_if", "hidden_if", "hide_when")
    if hide_if:  # hidden while these hold (shown otherwise)
        field["hide_if"] = [normalize_condition(c) for c in _list(hide_if)]
    readonly_if = _first(raw, "readonly_if", "read_only_if", "locked_if", "editable_unless")
    if readonly_if and ftype in STORED:  # can't be changed while these hold (the amount once it's approved)
        field["readonly_if"] = [normalize_condition(c) for c in _list(readonly_if)]
    ai_spec = _first(raw, "ai", "ai_fill", "ai_instructions")
    if ai_spec:
        if ftype not in AI_FILLABLE:
            raise SchemaError(f"the AI can't fill “{label}” ({ftype}); AI fields can be {', '.join(sorted(AI_FILLABLE))}")
        instructions = ai_spec if isinstance(ai_spec, str) else _first(ai_spec, "instructions", "prompt", "task", default="")
        if not str(instructions).strip():
            raise SchemaError(f"the AI field “{label}” needs instructions, e.g. \"ai\": \"Summarise the notes in one sentence\"")
        field["ai"] = {"instructions": str(instructions).strip()[:1000]}
        if (isinstance(ai_spec, dict) and _first(ai_spec, "use_knowledge", "knowledge", "rag") in (True, "true", "yes", 1)) \
                or raw.get("use_knowledge") in (True, "true", "yes", 1):
            field["ai"]["use_knowledge"] = True
        sources = ai_spec.get("from") if isinstance(ai_spec, dict) else None
        if sources:
            field["ai"]["from"] = [slug(x) for x in _list(sources)]
    if ftype in ("select", "multi_select"):
        options = _options(_first(raw, "options", "choices", "values", "enum", "items"))
        by = _first(raw, "options_by", "choices_by", "depends_on", "options_for")
        if not options and isinstance(by, dict) and isinstance(_first(by, "options", "choices", "map"), dict):
            options = list(dict.fromkeys(o for v in _first(by, "options", "choices", "map").values() for o in _options(v)))
        if not options:
            raise SchemaError(f"the {ftype.replace('_', '-')} field “{label}” needs its options, e.g. \"options\": [\"Open\", \"Done\"]")
        field["options"] = options
        if default is not None and ftype == "select" and str(default).lower() not in [o.lower() for o in options]:
            field.pop("default")
        if ftype == "multi_select" and "default" in field:
            field["default"] = [o for o in _options(default) if o.lower() in [x.lower() for x in options]]
        if isinstance(by, dict) and _first(by, "field", "on", "parent") and isinstance(_first(by, "options", "choices", "map"), dict):
            # the choices depend on another field: a Plumbing job's types are Leak and Blocked drain
            mapping = {str(k): _options(v) for k, v in _first(by, "options", "choices", "map").items() if _options(v)}
            if mapping:
                field["options_by"] = {"field": slug(_first(by, "field", "on", "parent")), "options": mapping}
                field["options"] = list(dict.fromkeys(field["options"] + [o for opts in mapping.values() for o in opts]))
    elif ftype == "link":
        target = _first(raw, "table", "target", "to", "references", "link_table", "linked_table", "table_key", "related_table")
        if not target:
            raise SchemaError(f"the link field “{label}” needs the table it links to, e.g. \"table\": \"clients\"")
        field["table"] = slug(target)
    if ftype in ("link", "user") and (raw.get("multiple") in (True, "true", "yes", 1) or str(raw.get("type", "")).lower() in (
            "links", "many_to_many", "has_many", "users", "people", "members", "multi_link", "multi_user")):
        field["multiple"] = True
    elif ftype == "formula":
        source = _first(raw, "expression", "formula", "expr", "calculation", "value")
        if not source:
            raise SchemaError(f"the formula field “{label}” needs an expression, e.g. \"expression\": \"hours * rate\"")
        try:
            expr.parse(str(source))
        except expr.ExprError as exc:
            raise SchemaError(f"the formula for “{label}”: {exc}") from exc
        field["expression"] = str(source)
        fmt = slug(_first(raw, "format", "result_type", "output", default="number"))
        fmt = {"currency": "money", "dollar": "money", "string": "text", "boolean": "checkbox", "bool": "checkbox",
               "percentage": "percent", "datetime": "date"}.get(fmt, fmt)
        field["format"] = fmt if fmt in FORMATS else "number"
    elif ftype == "rollup":
        source = _first(raw, "table", "source_table", "of", "from", "child_table", "related_table")
        via = _first(raw, "via", "link_field", "through", "by", "foreign_key", "on")
        filters = _first(raw, "filters", "where", "conditions")
        if not source or not (via or filters):
            raise SchemaError(f"the rollup field “{label}” needs \"table\" (the records to total) and \"via\" (their link field to this "
                              f"table), or \"where\" (conditions on {{{{record.x}}}}: every matching record of the table, like SUMIFS)")
        via = via or ""  # no link: the whole table, filtered (this requester's other requests this week)
        aggregate = slug(_first(raw, "aggregate", "function", "agg", "operation", default="sum" if _first(raw, "field", "sum_field", "of_field") else "count"))
        aggregate = AGG_ALIASES.get(aggregate, aggregate)
        if aggregate not in AGGREGATES:
            raise SchemaError(f"the rollup “{label}” can count, sum, avg, min or max, not “{aggregate}”")
        field.update(table=slug(source), via=".".join(slug(part) for part in str(via).split(".") if slug(part)), aggregate=aggregate)
        of = _first(raw, "field", "sum_field", "of_field", "target_field", "column")
        if of:
            field["field"] = slug(of)
        elif aggregate != "count":
            raise SchemaError(f"the rollup “{label}” needs the \"field\" to {aggregate}")
        if filters:
            field["filters"] = [normalize_condition(c) for c in _list(filters)]
    elif ftype == "autonumber":
        field["prefix"] = str(_first(raw, "prefix", default=""))[:12]
        digits = expr.to_number(_first(raw, "digits", "padding", "length", default=4))
        field["digits"] = int(min(max(digits or 4, 1), 9))
    elif ftype == "lookup" and _first(raw, "where", "find", "filters", "matching", "conditions"):
        # found by conditions rather than through a link, like XLOOKUP: the price in force on the order date, the
        # approver whose limit covers the amount, the tax rate for the region
        source = _first(raw, "from", "table", "in", "source_table", "find_in")
        of = _first(raw, "field", "of", "lookup_field", "show", "return", default="")  # left out: filled in once the tables are known
        if not source:
            raise SchemaError(f"the lookup “{label}” finds a record by conditions: it needs \"table\" (where to look), \"where\" "
                              f"(conditions on {{{{record.x}}}}) and \"field\" (the found record's field to show)")
        field.update(table=slug(source), field=".".join(slug(part) for part in str(of).split(".") if slug(part)),
                     where=[normalize_condition(c) for c in _list(_first(raw, "where", "find", "filters", "matching", "conditions"))])
        sort = _first(raw, "sort", "order_by", "pick", "take")
        if isinstance(sort, str):  # "limit" (lowest first), "-effective_from" or "effective_from desc" (latest first)
            word = sort.strip()
            sort = {"field": word.lstrip("-").split()[0], "dir": "desc" if word.startswith("-") or word.lower().endswith(" desc") else "asc"}
        if isinstance(sort, dict) and _first(sort, "field", "key", "by"):
            direction = str(_first(sort, "dir", "direction", "order", default="asc")).lower()
            field["sort"] = {"field": slug(_first(sort, "field", "key", "by")), "dir": "desc" if direction.startswith(("desc", "high", "late", "big")) else "asc"}
    elif ftype == "lookup":
        link = _first(raw, "link", "via", "through", "from", "link_field", "table")
        of = _first(raw, "field", "of", "lookup_field", "show")
        if not link and not of:
            raise SchemaError(f"the lookup “{label}” needs \"link\" (a link field of this table) and \"field\" (the linked record's field to show)")
        # "field" may go on through the linked record's links: a rent payment's lease → "property.owner"
        field.update(link=slug(link or ""), field=".".join(slug(part) for part in str(of or "").split(".")))  # one left out is filled in once the tables are known
    elif ftype == "rating":
        field["max"] = int(min(max(expr.to_number(_first(raw, "max", "out_of", "scale", default=5)) or 5, 3), 10))
    elif ftype == "file":
        field["multiple"] = raw.get("multiple", True) not in (False, "false", 0)
    elif ftype == "button":
        automation = _first(raw, "automation", "run", "action", "does")
        if not automation:
            raise SchemaError(f"the button “{label}” needs \"automation\": the key of a button automation on this table")
        field["automation"] = slug(automation)
    if ftype == "link":
        match = _first(raw, "match", "depends_on", "filter_by", "only_matching")
        if isinstance(match, dict) and match:  # {"customer": "customer"}: only contacts of this record's customer
            field["match"] = {slug(k): slug(v) for k, v in match.items()}
    if raw.get("readonly") in (True, "true", "yes", 1) or raw.get("read_only") in (True, "true", "yes", 1):
        field["readonly"] = True
    pattern = _first(raw, "pattern", "regex", "format_rule")
    if pattern and ftype in ("text", "phone", "barcode", "email", "url"):
        try:
            re.compile(str(pattern))
        except re.error as exc:
            raise SchemaError(f"the pattern for “{label}” isn't a valid regular expression: {exc}") from exc
        field["pattern"] = str(pattern)[:200]
        field["pattern_message"] = str(_first(raw, "pattern_message", "format_message", "invalid_message", default="") or
                                       f"{label} isn't in the right format")[:200]
    required_if = _first(raw, "required_if", "required_when", "mandatory_if")
    if required_if:
        field["required_if"] = [normalize_condition(c) for c in _list(required_if)]
        if all(c.get("field") == key and c.get("op") == "is_empty" for c in field["required_if"]):
            field["required"] = True  # “required when it's empty” is required
            del field["required_if"]
    decimals = expr.to_number(_first(raw, "decimals", "precision", "decimal_places"))
    if decimals is not None and ftype in ("number", "percent", "formula", "rollup"):
        field["decimals"] = int(min(max(decimals, 0), 6))
    unit = _first(raw, "unit", "suffix", "units")
    if unit and ftype in ("number", "formula", "rollup", "duration"):
        field["unit"] = str(unit).strip()[:12]
    if ftype == "duration":  # kept in hours and shown as "30m" or "1h 30m": a default of 30 minutes is half an hour, never 30 hours
        unit = str(field.pop("unit", "") or "").lower()
        if field.get("default") not in (None, ""):
            field["default"] = _hours(field["default"], 1 / 60 if unit.startswith("min") else 1)
    if ftype == "percent":  # kept as a fraction: a default (or limit) of 10 is 10%, never 1000% filled in on every new form
        for key in ("default", "min", "max"):
            if field.get(key) not in (None, ""):
                field[key] = as_fraction(field[key])
    if slug(_first(raw, "display", "show_as", "style", default="")) in ("progress", "bar", "progress_bar") and ftype in ("percent", "formula", "rollup"):
        field["display"] = "progress"
    colours = _first(raw, "option_colors", "option_colours", "choice_colors", "choice_colours")
    if isinstance(colours, dict) and ftype in ("select", "multi_select"):  # a choice's badge colour, where it's set by hand
        named = {"grey": "slate", "gray": "slate", "purple": "violet", "orange": "amber", "yellow": "amber", "indigo": "blue"}
        kept = {str(o): named.get(slug(c), slug(c)) for o, c in colours.items() if str(o) in field["options"]}
        kept = {o: c for o, c in kept.items() if c in CHOICE_COLOURS}
        if kept:
            field["option_colors"] = kept
    for side in ("before", "after"):  # shown either side of the value: "ABN " before a number, " pcs" after one
        said = _first(raw, f"show_{side}", f"shown_{side}", side, "text_" + side)
        if said not in (None, "") and ftype in ("text", "number", "formula", "rollup", "barcode", "autonumber"):
            field[side] = str(said)[:12]
    if ftype == "checkbox":  # what yes and no say: In stock / Out of stock
        for side, names in (("yes_label", ("yes_label", "true_label", "yes", "on_label")), ("no_label", ("no_label", "false_label", "no", "off_label"))):
            said = _first(raw, *names)
            if isinstance(said, str) and said.strip():
                field[side] = said.strip()[:30]
    if ftype == "url" and _first(raw, "template", "link_template", "url_template", "builds_from"):
        # a link made from the record's own details (a parcel's tracking page from its tracking number): opened, not typed
        field["template"] = str(_first(raw, "template", "link_template", "url_template", "builds_from"))[:500]
    if ftype in ("text", "barcode", "number") and raw.get("unique") in (True, "true", "yes", 1):
        field["unique"] = True
    if ftype in ("money", "rollup") or (ftype == "formula" and field.get("format") == "money"):
        currency = _first(raw, "currency", "currency_code")
        currency_field = _first(raw, "currency_field", "currency_from")
        if currency_field:  # its own field of currency codes, or its order's ("po.currency" on a purchase order's lines)
            field["currency_field"] = ".".join(slug(part) for part in str(currency_field).split(".")[:2])
        elif currency and re.fullmatch(r"[A-Za-z]{3}", str(currency).strip()):
            field["currency"] = str(currency).strip().upper()
    if _yes(_first(raw, "sensitive", "secret", "masked", "encrypted", "confidential")) and ftype in SENSITIVE_TYPES:
        field["sensitive"] = True  # a bank account: kept encrypted, shown masked, never sent to the AI or in emails
        field.pop("unique", None)
    access = slug(_first(raw, "access", "visible_to", "only_for", "restricted_to", default=""))
    access = {"admins": "admin", "owners": "owner", "editors": "editor", "managers": "admin", "manager": "admin", "staff": "editor",
              "internal": "editor", "private": "editor", "staff_only": "editor", "buyer_only": "editor", "hidden": "editor"}.get(access, access)
    if access and ftype != "link" and (access in ROLE_NAMES[1:] or access not in ROLE_NAMES):
        field["access"] = access  # a role (admins and up) or a team's key (only Finance); hiding a link hides the relationship
    return field


def _list(value):
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    return list(value)


def _source(raw, table, via):
    """The related records a step goes through: {table, via: their link field, of: the link field of the triggering record
    they hang off when it isn't the record itself (the bill of materials of a work order's product), nested: the lines'
    link to a sub-assembly, whose own lines a line naming one stands for (true: the one such link)}."""
    source = {"table": slug(table), "via": slug(via)}
    of = _first(raw, "of", "parent", "through", "from_field")
    if of and slug(of) not in ("record", "this", "self", "trigger"):
        source["of"] = ".".join(slug(part) for part in str(of).split(".") if part.strip())  # a path through links keeps its dots
    nested = _first(raw, "nested", "sub_assembly", "sub_assemblies", "subassembly", "explode", "expand")
    if nested and str(nested).strip().lower() not in ("false", "no"):
        source["nested"] = True if nested is True or str(nested).strip().lower() in ("true", "yes") else slug(nested)
    return source


def nested_link(source, spec):
    """The link of a bill of materials' lines to a sub-assembly (a drawer unit in a cabinet's), for a step with "nested"."""
    via = field(source, spec["via"])
    if not spec.get("nested") or not via or via["type"] != "link":
        return None
    links = [f for f in source["fields"] if f["type"] == "link" and f["table"] == via["table"] and f["key"] != via["key"]
             and not f.get("multiple") and (spec["nested"] is True or f["key"] == spec["nested"])]
    return links[0] if len(links) == 1 else None


def _person(raw):
    """Who a step is for: owner, admin or editor (anyone with that role), "creator" (who added the record), a team (its
    key: anyone in Accounts payable), "portal" (the outside person the record is for: the supplier), a person field of
    the record, or one through its links ("cost_centre.manager")."""
    who = ".".join(slug(part) for part in str(raw or "admin").split(".") if slug(part)) or "admin"
    if who in _TEAM_KEYS.get():  # a team of the app's own named like one of these words (Requester) is that team
        return who
    if who in _PERSON_FIELDS.get() and who not in WHO_WORDS:  # the record's own person field (a contract's "manager"), never
        return who                                            # the word for an admin
    return {"admins": "admin", "manager": "admin", "managers": "admin", "owners": "owner", "editors": "editor", "anyone": "editor",
            "team": "editor", "everyone": "editor", "staff": "editor", "requester": "creator", "submitter": "creator",
            "created_by": "creator", "raised_by": "creator", "author": "creator", "outside_person": "portal",
            "portal_person": "portal", "portal_user": "portal", "external": "portal", "the_supplier": "portal",
            "the_customer": "portal", "the_seller": "portal"}.get(who, who)


def normalize_keep_for(raw, table=None):
    """How long a list's records are kept: {"years": 7, "from": "invoice_date", "unless": [conditions]}. Past it a record
    is deleted, unless a condition holds (a legal hold on it), and erased for good a while after (see app/engine/retention.py).
    "from" is a date of the record's own, or when it was added (created_at)."""
    if isinstance(raw, (int, float)):
        raw = {"years": raw}
    if not isinstance(raw, dict):
        raise SchemaError("keep_for looks like {\"years\": 7, \"from\": \"invoice_date\", \"unless\": [a legal hold]}")
    out = {}
    for unit in ("years", "months", "days"):
        n = _first(raw, unit, unit[:-1])
        if n not in (None, ""):
            try:
                out[unit] = max(int(float(n)), 0)
            except (TypeError, ValueError):  # a formula, worked out for each record (the buyer's own years for invoices)
                if not isinstance(n, str) or not n.strip():
                    raise SchemaError(f"keep_for {unit} is a number, or a formula giving one") from None
                out[unit] = n.strip()[:500]
    if not any(out.get(u) for u in ("years", "months", "days")):
        raise SchemaError("keep_for needs how long: years, months or days")
    since = slug(str(_first(raw, "from", "since", "after", "field", default="created_at")))
    fields = {f["key"]: f for f in (table or {}).get("fields", [])}
    if since not in ("created_at", "updated_at") and fields and (since not in fields or fields[since]["type"] not in ("date", "datetime")):
        raise SchemaError(f"keep_for counts from a date of the record's own, or created_at; “{since}” isn't one")
    out["from"] = since
    unless = _first(raw, "unless", "except_when", "not_while", "held_if")
    if unless:
        out["unless"] = [normalize_condition(c) for c in _list(unless)]
    return out


def _not_by(raw, fields=()):
    """Separation of duties: who may not do this (whoever raised it, whoever moved it to a stage, the person in a field):
    ["creator", "Recommended", "prepared_by"]. Stage names are kept as written; the rest as who-words, except a field of
    the record's own (a contract's "manager" is its manager, not the word for an admin)."""
    out = []
    for x in _names(_first(raw, "not_by", "not", "except_by", "separate_from", "segregate_from", "different_from", "not_same_as",
                           default=[])):
        if slug(x) in fields:
            out.append(slug(x))
            continue
        word = _person(x)
        out.append(word if word in ("creator", "portal") or re.fullmatch(r"[a-z0-9_.]+", str(x).strip()) else str(x).strip())
    return list(dict.fromkeys(o for o in out if o))[:6]


def _yes(value):
    return value in (True, "true", "yes", 1, "1")


def _follow_up(raw, action):
    """An approval's or a task's nudges: remind after so many days, then pass it up to someone else."""
    remind = expr.to_number(_first(raw, "remind_after_days", "remind_after", "reminder_days", "remind_days"))
    if remind and remind > 0:
        action["remind_after_days"] = remind
    escalate = expr.to_number(_first(raw, "escalate_after_days", "escalate_after", "escalation_days", "escalate_days"))
    if escalate and escalate > 0:
        action["escalate_after_days"] = escalate
        action["escalate_to"] = _person(_first(raw, "escalate_to", "escalation", "then_to", default="owner"))


def _branch(raw):
    """{"type": "branch", "paths": [{"label", "when": conditions, "steps": [...]}], "otherwise": [...]}: the first path
    whose conditions the record meets has its steps done (a big order goes to the owner, a small one straight through);
    when none does, the "otherwise" steps."""
    paths = _first(raw, "paths", "branches", "cases", "routes", "options")
    said = _first(raw, "if", "when", "condition", "conditions", "only_if")
    if not paths and said:  # if / then / else
        paths = [{"when": said, "steps": _first(raw, "then", "steps", "do", "actions", default=[])}]
    said_otherwise = _first(raw, "otherwise", "else", "default", "if_not")
    otherwise = [normalize_action(x) for x in _list(said_otherwise)] if said_otherwise else None
    out = []
    for p in _list(paths):
        if not isinstance(p, dict):
            raise SchemaError("each path of a branch looks like {\"label\": \"Over $5,000\", \"when\": [conditions], \"steps\": [...]}")
        when = _first(p, "when", "if", "conditions", "only_if", "condition")
        steps = [normalize_action(x) for x in _list(_first(p, "steps", "then", "actions", "do", default=[]))]
        if not when:  # a path without conditions is the rest
            if otherwise is None:
                otherwise = steps
                continue
            raise SchemaError("each path of a branch needs \"when\": the conditions that send a record down it")
        out.append({"label": str(_first(p, "label", "name", "title", default="")).strip()[:80], "when": [normalize_condition(c) for c in _list(when)],
                    "steps": steps})
    if not out:
        raise SchemaError("a branch needs \"paths\": [{\"when\": [conditions], \"steps\": [...]}], and \"otherwise\": [...] for the rest")
    action = {"type": "branch", "paths": out}
    if otherwise:
        action["otherwise"] = otherwise
    return action


def normalize_workflow(raw, label="", fields=()):
    """How a table's records move from stage to stage: {"field": the stage (a select), "start": the stage new records
    begin at, "transitions": [{"from": [stages] (none: any), "to": a stage, "label": the button, "who": [roles, person
    fields or "creator"], "requires": fields filled in first, "only_if": conditions}]}. People move records only these
    ways (the owner can move them anywhere); automations aren't held to them."""
    if isinstance(raw, list):
        raw = {"transitions": raw}
    if not isinstance(raw, dict):
        raise SchemaError(f"the workflow of {label} looks like {{\"field\": \"status\", \"transitions\": [{{\"from\": [\"Draft\"], "
                          f"\"to\": \"Submitted\", \"label\": \"Submit\"}}]}}")
    moves = []
    for m in _list(_first(raw, "transitions", "moves", "steps", "actions", default=[])):
        if not isinstance(m, dict) or _first(m, "to", "into", "target", "stage", "status") is None:
            raise SchemaError(f"each move of {label}'s workflow needs \"to\": the stage it moves a record to")
        start = _first(m, "from", "from_stage", "from_stages", "when_in", "in")
        move = {"from": [] if start in (None, "", "*", "any", "anywhere") else _names(start),
                "to": str(_first(m, "to", "into", "target", "stage", "status")),
                "label": str(_first(m, "label", "button", "name", "title", default="")).strip()[:60]}
        who = _first(m, "who", "by", "role", "roles", "allowed", "can")
        if who:
            move["who"] = [slug(x) if slug(x) in fields and slug(x) not in WHO_WORDS else _person(x) for x in _names(who)]
        if m.get("filled") is True:  # added by the platform so no stage is out of reach: worked out again as the design changes
            move["filled"] = True
        needs = _first(m, "requires", "required", "require", "needs", "required_fields")
        if needs:
            move["requires"] = [slug(f) for f in _names(needs)]
        only = _first(m, "only_if", "if", "when", "conditions")
        if only:
            move["only_if"] = [normalize_condition(c) for c in _list(only)]
        apart = _not_by(m, fields)
        if apart:  # separation of duties: whoever recommended an award can't approve it
            move["not_by"] = apart
        if _yes(_first(m, "step_up", "reauthenticate", "confirm_sign_in", "second_factor")):
            move["step_up"] = True  # a fresh sign-in code first (putting a contract in force, approving a payment run)
        if m.get("feature"):  # made by a feature of the Keel language (app/lang): an approval's own moves
            move["feature"] = slug(m["feature"])
        moves.append(move)
    if not moves:
        raise SchemaError(f"the workflow of {label} needs \"transitions\": the moves between stages, like {{\"from\": [\"Draft\"], "
                          f"\"to\": \"Submitted\", \"label\": \"Submit for approval\"}}")
    flow = {"field": slug(_first(raw, "field", "status_field", "stage_field", default="status")), "transitions": moves}
    if _first(raw, "start", "initial", "first"):
        flow["start"] = str(_first(raw, "start", "initial", "first"))
    return flow


def _one_expression(value):
    """“{{item.quantity}} * {{record.quantity}}” is one sum, not text: {{(item.quantity) * (record.quantity)}}."""
    if not isinstance(value, str) or "{{" not in value:
        return value
    rest = expr.TEMPLATE.sub("", value)
    if rest.strip() and re.fullmatch(r"[\s\d.+\-*/()]*", rest) and re.search(r"[+\-*/]", rest):
        return "{{" + expr.TEMPLATE.sub(lambda m: f"({m.group(1)})", value).strip() + "}}"
    return value


# every record has created_at and updated_at; these names for them never mean a field people fill in
SYSTEM_TIMES = {"created_time": "created_at", "modified_time": "updated_at", "updated_time": "updated_at", "last_modified": "updated_at",
                "modified_at": "updated_at"}


# a value worked out when the condition is tested, written without braces ("value": "now() - 48h"): it means
# {{now() - 48h}}, compared as any worked-out value is (a date field equal to a day, a time before a moment)
WORKED_OUT = re.compile(r"\b(today|now|days_since|days_until|hours_since|hours_until|add_days|add_hours)\s*\(", re.I)


def worked_out(value):
    """{{now() - 48h}} for a condition's value of now() - 48h, or None for a value as it is (or one in braces already)."""
    if isinstance(value, str) and "{{" not in value and WORKED_OUT.search(value):
        return "{{" + value.strip().removeprefix("=").strip() + "}}"
    return None


DATE_CALLS = {"today", "now", "add_days", "add_months", "add_hours", "workday", "add_workdays", "end_of_month", "date",
              "datetime"}
DATED_TYPES = ("date", "datetime", "created_time", "modified_time")


def dated_formula(t, f):
    """Whether a formula field works out a date (confirmed_at + 2h, add_days(sent_on, 14), today()) though it says it
    shows a number (as a formula left at its default does): it's a date, to show, compare and count from."""
    if not f or f.get("type") != "formula" or f.get("format") not in (None, "", "number"):
        return False
    dates = frozenset([x["key"] for x in (t or {}).get("fields", []) if x.get("type") in DATED_TYPES] + ["created_at", "updated_at"])
    return _gives_date(str(f.get("expression") or ""), dates)


_dated_cache = {}


def _gives_date(source, dates):
    if (source, dates) in _dated_cache:
        return _dated_cache[(source, dates)]
    try:
        tree = expr.parse(source)
    except expr.ExprError:
        tree = None

    def dated(n):
        if isinstance(n, expr.ast.Name):
            return n.id in dates
        if isinstance(n, expr.ast.Call) and isinstance(n.func, expr.ast.Name):
            name = expr.ALIASES.get(n.func.id.lower(), n.func.id.lower())
            if name in DATE_CALLS:
                return True
            if name == "iif":
                return any(dated(a) for a in n.args[1:3])
            if name in ("min", "max", "coalesce"):
                return bool(n.args) and all(dated(a) for a in n.args)
            return False
        if isinstance(n, expr.ast.BinOp) and isinstance(n.op, expr.ast.Add):
            return dated(n.left) != dated(n.right)
        if isinstance(n, expr.ast.BinOp) and isinstance(n.op, expr.ast.Sub):
            return dated(n.left) and not dated(n.right)
        if isinstance(n, expr.ast.IfExp):
            return dated(n.body) or dated(n.orelse)
        return False
    out = bool(tree is not None and dated(tree.body))
    if len(_dated_cache) < 10000:
        _dated_cache[(source, dates)] = out
    return out


PLAIN_PATH = re.compile(r"^\s*[A-Za-z_][\w ]*(\.[A-Za-z_][\w ]*)*\s*$")
FORMULA_SAID = {**{op: sym for op, sym in (("equals", "=="), ("not_equals", "!="), ("greater_than", ">"), ("less_than", "<"),
                                           ("at_least", ">="), ("at_most", "<="))}}


def as_formula(field, op, value):
    """A condition whose "field" is worked out (days_since(due_date), any(payments where …)), said as the formula it
    means: days_since(due_date) > 3. None when its field is a field (or the op has no formula to say it)."""
    import json as _json
    if not isinstance(field, str) or PLAIN_PATH.match(field) or not re.search(r"[()+\-*/<>=]", field):
        return None
    worked = f"({field.strip()})"
    if op in ("is_empty", "not_empty"):
        return f"{'' if op == 'is_empty' else 'not '}is_empty{worked}"
    if isinstance(value, str) and WORKED_OUT.search(value):
        said = f"({value.strip().removeprefix('{{').removesuffix('}}')})"
    elif isinstance(value, (bool, int, float)) or value is None:
        said = {True: "True", False: "False", None: "None"}.get(value, str(value)) if isinstance(value, bool) or value is None else str(value)
    elif isinstance(value, list):
        said = _json.dumps([str(v) for v in value], ensure_ascii=False)
    else:
        said = _json.dumps(str(value), ensure_ascii=False)
    if op == "one_of":
        return f"{worked} in {said if isinstance(value, list) else '[' + said + ']'}"
    if op == "contains":
        return f"{said} in {worked}"
    return f"{worked} {FORMULA_SAID[op]} {said}" if op in FORMULA_SAID else None


def normalize_condition(raw):
    """{"field", "op", "value"}, or a group: {"any": [conditions]} holds when one of them does (urgent or overdue),
    {"all": [conditions]} when every one does (inside an "any": over $5,000 and not yet approved)."""
    if isinstance(raw, list):
        raw = {"all": raw}
    if isinstance(raw, str) and raw.strip():  # a formula that must hold: count(client.invoices where …) == 0
        raw = {"formula": raw}
    if not isinstance(raw, dict):
        raise SchemaError("a condition must look like {\"field\": \"status\", \"op\": \"equals\", \"value\": \"Done\"}")
    said = _first(raw, "formula", "expression", "check")
    if isinstance(said, str) and said.strip() and not _first(raw, "field", "key", "column", "name"):
        return {"formula": said.strip()}
    if not _first(raw, "field", "key", "column", "name"):
        for kind, names in (("any", ("any", "or", "either", "one_of_these")), ("all", ("all", "and", "every", "both"))):
            inner = _first(raw, *names)
            if isinstance(inner, (list, dict)):
                parts = [normalize_condition(c) for c in _list(inner)]
                if not parts:
                    raise SchemaError(f"an “{kind}” group needs the conditions in it")
                return parts[0] if len(parts) == 1 else {kind: parts}
    field = _first(raw, "field", "key", "column", "name")
    if not field:
        raise SchemaError("a condition needs the field it checks")
    op_raw = str(_first(raw, "op", "operator", "comparison", "condition", "is", default="equals")).strip().lower()
    if slug(op_raw) in NOT_ONE_OF:  # "isn't Paid or Cancelled": each one it mustn't be
        values = raw.get("value", raw.get("values"))
        values = [v for v in (values if isinstance(values, list) else str(values or "").split(",")) if str(v).strip()]
        if not values:
            raise SchemaError("not_one_of needs the values it mustn't be")
        parts = [normalize_condition({"field": field, "op": "not_equals", "value": v.strip() if isinstance(v, str) else v}) for v in values]
        return parts[0] if len(parts) == 1 else {"all": parts}
    op = OP_ALIASES.get(op_raw, OP_ALIASES.get(slug(op_raw), slug(op_raw)))
    if op not in CONDITION_OPS:
        raise SchemaError(f"“{op_raw}” isn't a condition; use one of: {', '.join(CONDITION_OPS)}, not_one_of")
    if as_formula(field, op, raw.get("value", raw.get("values", raw.get("to")))):  # "field": "days_since(due_date)"
        return {"formula": as_formula(field, op, raw.get("value", raw.get("values", raw.get("to"))))}
    path = [SYSTEM_TIMES.get(slug(part), slug(part)) for part in str(field).split(".")[:2]]
    condition = {"field": ".".join(path), "op": op}  # customer.account_manager: through a link
    if op not in NO_VALUE:
        value = raw.get("value", raw.get("values", raw.get("to")))
        if value is None and op not in ("equals", "not_equals"):
            raise SchemaError(f"the condition on “{field}” ({op}) needs a value")
        if op == "one_of":
            value = _options(value) if not isinstance(value, list) else [str(v) for v in value]
        condition["value"] = worked_out(value) or value
    return condition


def normalize_view(raw, table=None):
    if not isinstance(raw, dict):
        raise SchemaError("a view must be an object")
    vtype = slug(_first(raw, "type", "kind", "layout", default="list"))
    vtype = VIEW_ALIASES.get(vtype, vtype)
    if vtype not in VIEW_TYPES:
        raise SchemaError(f"“{vtype}” isn't a view type; use list or board")
    label = str(_first(raw, "label", "name", "title", default="")).strip()
    key = slug(_first(raw, "key", "id", default=label or vtype))
    view = {"key": key, "label": label or label_of(key), "type": vtype}
    columns = _first(raw, "columns", "fields", "show")
    if columns:
        view["columns"] = [slug(c if not isinstance(c, dict) else _first(c, "field", "key", "name")) for c in _list(columns)]
    filters = _first(raw, "filters", "filter", "where", "conditions")
    if filters:
        view["filters"] = [normalize_condition(c) for c in _list(filters)]
    sort = _first(raw, "sort", "order_by", "sort_by")
    if sort:
        if isinstance(sort, str):
            desc = sort.startswith("-") or sort.lower().endswith(" desc")
            sort = {"field": sort.lstrip("-").split()[0], "dir": "desc" if desc else "asc"}
        elif isinstance(sort, list):
            sort = sort[0] if sort else None
        if isinstance(sort, dict) and _first(sort, "field", "key"):
            direction = str(_first(sort, "dir", "direction", "order", default="asc")).lower()
            view["sort"] = {"field": slug(_first(sort, "field", "key")), "dir": "desc" if direction.startswith("desc") else "asc"}
    if vtype == "board":
        group = _first(raw, "group_by", "group", "column_field", "stack_by", "status_field", "by")
        if not group:
            raise SchemaError(f"the board “{view['label']}” needs \"group_by\": a select field such as status")
        view["group_by"] = slug(group)
    elif vtype == "calendar":
        date = _first(raw, "date_field", "date", "start_field", "start", "field", "by")
        if not date:
            raise SchemaError(f"the calendar “{view['label']}” needs \"date_field\": the date each record goes on")
        view["date_field"] = slug(date)
        end = _first(raw, "end_field", "end", "until")
        if end:
            view["end_field"] = slug(end)
    elif vtype == "timeline":
        start = _first(raw, "start_field", "start", "date_field", "from")
        end = _first(raw, "end_field", "end", "until", "to")
        if not start:
            raise SchemaError(f"the timeline “{view['label']}” needs \"start_field\" (and \"end_field\"): the date fields each bar runs between")
        view["start_field"] = slug(start)
        if end:  # without one, the end date field is found by its name, or each bar is a day long
            view["end_field"] = slug(end)
    elif vtype == "gallery":
        image = _first(raw, "image_field", "image", "cover", "photo")
        if image:
            view["image_field"] = slug(image)
    elif vtype == "map":
        where = _first(raw, "location_field", "location", "field", "position", "gps")
        if not where:
            raise SchemaError(f"the map “{view['label']}” needs \"location_field\": a location field")
        view["location_field"] = slug(where)
    elif vtype == "tree":
        parent = _first(raw, "parent_field", "parent", "under", "reports_to", "field")
        if not parent:
            raise SchemaError(f"the tree “{view['label']}” needs \"parent_field\": the table's link to its own parent record")
        view["parent_field"] = slug(parent)
    if vtype in ("list", "timeline") and _first(raw, "group_by", "lanes", "lane_by", "swimlanes", "group"):
        view["group_by"] = slug(_first(raw, "group_by", "lanes", "lane_by", "swimlanes", "group"))
    if vtype == "calendar" and slug(_first(raw, "mode", "show", "range", default="")) in ("week", "weekly"):
        view["mode"] = "week"
    if vtype == "timeline":
        depends = _first(raw, "depends_field", "dependencies", "depends_on", "after", "predecessors")
        if depends:
            view["depends_field"] = slug(depends)
    colour = _first(raw, "color_by", "colour_by", "color", "colour")
    if colour and vtype in ("calendar", "timeline", "gallery"):
        view["color_by"] = slug(colour)
    return view


def normalize_table(raw):
    if not isinstance(raw, dict):
        raise SchemaError("a table must be an object")
    label = str(_first(raw, "label", "name", "title", "plural", default="")).strip()
    key = slug(_first(raw, "table", "key", "id", default=label))
    if not key:
        raise SchemaError("a table needs a name")
    if key in RESERVED:
        raise SchemaError(f"“{key}” can't be used as a table name")
    if not label or label == key:
        label = label_of(key)
    singular = str(_first(raw, "singular", "record_name", "item_label", default="")).strip() or _singular(label)
    table = {"key": key, "label": label, "singular": singular, "fields": [], "views": []}
    module = _first(raw, "module", "group", "section", "app")
    if module:
        table["module"] = slug(module)
    seen = set()
    for f in _list(_first(raw, "fields", "columns", default=[])):
        field = normalize_field(f)
        if field is None or field["key"] in seen:
            continue
        seen.add(field["key"])
        table["fields"].append(field)
    title = _first(raw, "title_field", "display_field", "primary_field", "name_field")
    if title and slug(title) in seen:
        table["title_field"] = slug(title)
    for v in _list(_first(raw, "views", default=[])):
        table["views"].append(normalize_view(v))
    table["icon"] = icons.resolve(raw.get("icon")) or icons.icon_for(label)
    if raw.get("embedded") in (True, "true", "yes", 1) or _first(raw, "line_items_of", "lines_of", "parent_table"):
        table["embedded"] = True
    combines = _first(raw, "combines", "union", "combined_from", "put_together_from")
    if combines:  # a list put together from others, kept by the platform (engine/combined.py)
        table["combines"] = [_combine_arm(a, label) for a in _list(combines)]
    description = _first(raw, "description", "about", "purpose")
    if description:
        table["description"] = str(description)[:300]
    sections = _first(raw, "sections", "groups") or (raw.get("layout") if isinstance(raw.get("layout"), list) else None)
    if sections:
        table["sections"] = [normalize_section(x) for x in _list(sections)]
    documents = _first(raw, "documents", "templates", "pdfs")
    if documents:
        table["documents"] = [normalize_document(d) for d in _list(documents)]
    form = _first(raw, "public_form", "form", "intake_form")
    if form:
        table["public_form"] = normalize_form(form)
    access = _first(raw, "access", "permissions")
    if isinstance(access, dict):
        clean = {}
        for action in ("view", "edit"):
            role = slug(_first(access, action, f"{action}_role", f"can_{action}", default=""))
            role = {"admins": "admin", "owners": "owner", "editors": "editor", "viewers": "viewer", "everyone": "viewer",
                    "managers": "admin", "manager": "admin"}.get(role, role)
            if role in ROLE_NAMES:
                clean[action] = role
        changers = [slug(x) for x in _names(_first(access, "edit_teams", "changed_by_teams", default=[])) if slug(x)]
        if changers:  # everyone with the view role sees it; only these teams (and admins) change it: the shared catalogue
            clean["edit_teams"] = list(dict.fromkeys(changers))[:8]
        teams = [slug(x) for x in _names(_first(access, "teams", "team", "only_teams", "groups", default=[])) if slug(x)]
        if teams:  # only these teams (and admins) see and change it: Payments for Finance
            clean["teams"] = list(dict.fromkeys(teams))[:8]
        if clean:
            table["access"] = clean
    rows = _first(raw, "row_access", "own_records", "only_own", "record_access")
    also = list(_list(_first(raw, "row_access_also", default=[])))
    if isinstance(rows, list):  # several, each of which must hold: an organisation's own records, and of them an evaluator's own scores
        rows, also = (rows[0] if rows else None), list(rows[1:]) + also
    if also:
        table["row_access_also"] = [normalize_table({"label": label, "fields": [], "row_access": x})["row_access"] for x in also[:4]]
    if rows:
        if isinstance(rows, str):
            rows = {"field": rows}
        if not isinstance(rows, dict) or not _first(rows, "field", "owner_field", "by"):
            raise SchemaError(f"row_access on {label} looks like {{\"field\": \"owner\", \"roles\": [\"editor\"]}}: people with those "
                              "roles see only records where that person field is them")
        roles = [r for r in (slug(x) for x in _list(_first(rows, "roles", "for", "applies_to", default=["viewer", "editor"]))) if r in ROLE_NAMES[:3]]
        path = ".".join(slug(part) for part in str(_first(rows, "field", "owner_field", "by")).split(".") if slug(part))
        table["row_access"] = {"field": path, "roles": roles or ["viewer", "editor"]}
        every = [slug(x) for x in _names(_first(rows, "teams", "except_teams", "all_for", "see_all", "except", default=[])) if slug(x)]
        if every:  # teams who see every record all the same (Finance doing the books, while each manager sees their own)
            table["row_access"]["teams"] = list(dict.fromkeys(every))[:8]
        if _yes(_first(rows, "shared_when_empty", "empty_is_shared", "everyones_when_empty")):
            table["row_access"]["shared_when_empty"] = True  # nobody in it: everyone's (the platform's standard clauses, beside a buyer's own)
    rules = _first(raw, "rules", "validations", "validation", "constraints", "checks")
    if rules:
        table["rules"] = [normalize_rule(r, label) for r in _list(rules)]
    colours = _first(raw, "colors", "colours", "color_rules", "colour_rules", "highlight", "highlights", "conditional_formatting")
    if colours:
        table["colors"] = [normalize_colour_rule(c, label) for c in _list(colours)]
    if raw.get("wizard") in (True, "true", "yes", 1) or raw.get("steps") in (True, "true", "yes", 1) or raw.get("step_by_step") in (True, "true", "yes", 1):
        table["wizard"] = True
    if slug(_first(raw, "layout", "record_layout", "page_layout", default="")) in ("tabs", "tabbed"):
        table["layout"] = "tabs"
    portal = _first(raw, "portal", "portal_access", "customer_portal")
    if isinstance(portal, dict) and _yes(_first(portal, "everyone", "all", "public", "for_everyone")):
        # every outside person sees these (a price list, figures published to every seller), those meeting show_if
        table["portal"] = {"via": "", "everyone": True, "people": slug(_first(portal, "people", "portal", "for", default="")),
                           "fields": [slug(f) for f in _list(_first(portal, "fields", "show", "columns", default=[]))], "can_add": False}
        show_if = _first(portal, "show_if", "only_if", "visible_if", "shown_if")
        if show_if:
            table["portal"]["show_if"] = [normalize_condition(c) for c in _list(show_if)]
        portal = None
    if portal:
        if isinstance(portal, str):
            portal = {"via": portal}
        if not isinstance(portal, dict) or not _first(portal, "via", "link", "field"):
            raise SchemaError(f"portal on {label} looks like {{\"via\": \"customer\"}}: the link to the portal's people "
                              "(each sees the records linked to them)")
        table["portal"] = {"via": ".".join(slug(p) for p in str(_first(portal, "via", "link", "field")).split(".")[:2] if slug(p)),
                           "fields": [slug(f) for f in _list(_first(portal, "fields", "show", "columns", default=[]))],
                           "can_add": _first(portal, "can_add", "create", "add") in (True, "true", "yes", 1)}
        edit = _first(portal, "edit", "can_edit", "editable", "they_fill_in", "they_can_change")
        if edit:  # what they may change on their records (a bid's prices, their own phone number)
            table["portal"]["edit"] = [slug(f) for f in _names(edit)] if not isinstance(edit, bool) else []
        edit_if = _first(portal, "edit_if", "editable_if", "edit_while", "can_edit_if")
        if edit_if:  # only while these hold (until the tender closes)
            table["portal"]["edit_if"] = [normalize_condition(c) for c in _list(edit_if)]
        show_if = _first(portal, "show_if", "only_if", "visible_if", "shown_if")
        if show_if:  # only the records that meet these reach them (published answers)
            table["portal"]["show_if"] = [normalize_condition(c) for c in _list(show_if)]
        for part in ("sign_up", "profile"):  # said on the people's own table: their portal's, moved there by ops
            said = _first(portal, part, "self_register" if part == "sign_up" else "own_fields")
            if said:
                table["portal"][part] = said if isinstance(said, (dict, list, bool)) else _names(said)
    keep = _first(raw, "keep_for", "retention", "retain_for", "keep")
    if keep:
        table["keep_for"] = normalize_keep_for(keep, table)
    lock = _first(raw, "lock_if", "locked_when", "lock_when", "read_only_if")
    if lock:
        table["lock_if"] = [normalize_condition(c) for c in _list(lock)]
        editable = _first(raw, "lock_except", "still_editable", "editable_when_locked")
        if editable:
            table["lock_except"] = [slug(x) for x in _list(editable)]
    flow = _first(raw, "workflow", "stages", "status_flow", "transitions", "stage_flow")
    if isinstance(flow, dict) or isinstance(flow, list) and flow and all(isinstance(x, dict) for x in flow):
        table["workflow"] = normalize_workflow(flow, label, {f["key"] for f in table["fields"]})  # (a plain list of stage names is just the select's options)
    unique = _first(raw, "unique_together", "unique", "no_duplicates", "unique_fields")
    if unique and not isinstance(unique, (bool, int, dict)):  # no two records alike in these fields together (a supplier's invoice number)
        groups = [unique] if isinstance(unique, str) or isinstance(unique, list) and unique and all(isinstance(x, str) for x in unique) else unique
        clean = [[slug(f) for f in _names(g)] for g in _list(groups) if isinstance(g, (str, list))]
        clean = [g for g in clean if len(g) >= 1]
        if clean:
            table["unique_together"] = clean[:4]
    limits = _first(raw, "limits", "capacity", "capacities", "no_overlap", "max_per", "booking_limits")
    if limits:
        table["limits"] = [normalize_limit(x, label) for x in _list(limits)][:6]
    held = _first(raw, "approve_changes", "changes_need_approval", "change_approval", "maker_checker", "four_eyes_changes")
    if held:
        table["approve_changes"] = normalize_change_approval(held, label)
    sealed = _first(raw, "sealed", "sealed_until", "seal", "blind_until")
    if sealed:
        if isinstance(sealed, str):
            sealed = {"until": sealed}
        if not isinstance(sealed, dict) or not _first(sealed, "until", "field", "opens", "open_at"):
            raise SchemaError(f"sealed on {label} looks like {{\"until\": \"tender.closes_at\"}}: the date and time (on the record or "
                              "through its link) until which nobody but its maker can see what's in it")
        table["sealed"] = {"until": ".".join(slug(p) for p in str(_first(sealed, "until", "field", "opens", "open_at")).split(".")[:3] if slug(p))}
        keep = _first(sealed, "keep_open", "editable_after", "still_editable", "then_editable")
        if keep:  # what the business fills in once it's opened (scores, notes)
            table["sealed"]["keep_open"] = [slug(f) for f in _names(keep)]
    if _yes(_first(raw, "audit_views", "log_views", "audit_reads", "log_reads")) or table.get("sealed"):
        table["audit_views"] = True  # every opening is kept in its history
    journey = _first(raw, "journey", "timeline", "progress", "thread", "status_timeline")
    if journey:
        table["journey"] = normalize_journey(journey, label)
    return table


def row_rules(t):
    """A table's rules for which of its records each person sees: its row_access, and any more that must hold too."""
    return [r for r in [(t or {}).get("row_access")] + list((t or {}).get("row_access_also") or []) if r]


def _own_names(source):
    """The names a formula reads of its own record: outside count(...), sum(...) and the like (whose names are the records
    they look at), and this.x inside them."""
    try:
        names = set(expr.outer_names(source))
    except expr.ExprError:
        return set(expr.names_in(source))
    return names | set(re.findall(r"\bthis\.(\w+)", source))


def _combine_arm(raw, label):
    """One list a combined list is put together from: {"table", "where": [conditions] (only those), "values": {this
    list's field: a value or {{record.x}} of the record it comes from}}."""
    if not isinstance(raw, dict) or not _first(raw, "table", "from", "source"):
        raise SchemaError(f"{label} is put together from lists like {{\"table\": \"stock_movements\", \"values\": {{\"quantity\": "
                          f"\"{{{{record.quantity}}}}\"}}}}: each needs its \"table\"")
    values = _first(raw, "values", "fields", "map", "set", default={})
    if not isinstance(values, dict) or not values:
        raise SchemaError(f"each list {label} is put together from needs \"values\": what goes in each of {label}'s fields")
    arm = {"table": slug(_first(raw, "table", "from", "source")), "values": {slug(k): _one_expression(v) for k, v in values.items() if k}}
    where = _first(raw, "where", "only", "filters", "conditions")
    if where:
        arm["where"] = [normalize_condition(c) for c in _list(where)]
    return arm


def normalize_change_approval(raw, label=""):
    """Changes to some fields of a record (a supplier's bank account, a price) wait for someone else to approve them:
    {"fields": [...] (none: any change), "approver": who, "wait_days": a cooling-off period before it can be approved,
    "tell": an email field told of the change with a link to cancel it, "step_up": the approver confirms their sign-in}."""
    if raw is True or isinstance(raw, str) or isinstance(raw, list):
        raw = {"fields": [] if raw is True else raw}
    if not isinstance(raw, dict):
        raise SchemaError(f"approve_changes on {label} looks like {{\"fields\": [\"bank_account\"], \"approver\": \"finance\"}}")
    out = {"fields": [slug(f) for f in _names(_first(raw, "fields", "of", "watch", default=[]))],
           "approver": _person(_first(raw, "approver", "approved_by", "who", "by", default="admin"))}
    wait = expr.to_number(_first(raw, "wait_days", "cooling_off_days", "cooling_off", "hold_days", "delay_days"))
    if wait and wait > 0:
        out["wait_days"] = min(wait, 60)
    tell = _first(raw, "tell", "notify_field", "confirm_with", "tell_contact", "email_field")
    if tell:  # an email field, or one through a link (the seller's registered contact's email)
        out["tell"] = ".".join(slug(p) for p in str(tell).split(".")[:2] if slug(p))
    if _yes(_first(raw, "step_up", "reauthenticate", "confirm_sign_in", "second_factor")):
        out["step_up"] = True
    if out.get("tell") and _yes(_first(raw, "told_first", "notice_first", "only_once_told", "require_notice")):
        out["told_first"] = True  # not approved until the notice has gone out (by email, or in the portal)
    return out


def normalize_journey(raw, label=""):
    """The path a record takes across the business, shown on it: its own stages and the records that follow from it
    (Submitted → Approved → Ordered → Delivered → Paid): [{"label", "when": conditions on the record}, or {"label", "table",
    "via": their link to it, "when": conditions on them}]."""
    steps = []
    for x in _list(raw):
        if isinstance(x, str):
            x = {"label": x}
        if not isinstance(x, dict) or not _first(x, "label", "name", "title"):
            raise SchemaError(f"each step of {label}'s journey looks like {{\"label\": \"Ordered\", \"table\": \"purchase_orders\", "
                              "\"via\": \"request\"}} or {\"label\": \"Approved\", \"when\": [conditions]}")
        step = {"label": str(_first(x, "label", "name", "title")).strip()[:40]}
        table = _first(x, "table", "records", "in")
        if table:
            step["table"] = slug(table)
            step["via"] = ".".join(slug(p) for p in str(_first(x, "via", "link", "through", default="")).split(".")[:3] if slug(p))
        when = _first(x, "when", "done_when", "if", "conditions")
        if when:
            step["when"] = [normalize_condition(c) for c in _list(when)]
        steps.append(step)
    return steps[:10]


def normalize_portal(raw):
    """The ERP's portal for outside people (customers, suppliers, subcontractors): who they are (a table with an email
    field) and what the portal says. Which records they see is set on each table with "portal": {"via": ...}."""
    if isinstance(raw, str):
        raw = {"table": raw}
    if not isinstance(raw, dict) or not _first(raw, "table", "people", "contacts", "for"):
        raise SchemaError("the portal looks like {\"table\": \"customers\", \"email_field\": \"email\", \"title\": \"Customer portal\"}")
    portal = {"table": slug(_first(raw, "table", "people", "contacts", "for")),
              "email_field": slug(_first(raw, "email_field", "email", "login_field", default="email")),
              "title": str(_first(raw, "title", "label", "name", default="Portal")).strip()[:80],
              "enabled": raw.get("enabled", True) not in (False, "false", 0)}
    welcome = _first(raw, "welcome", "intro", "message", "text")
    if welcome:
        portal["welcome"] = str(welcome)[:1000]
    profile = _first(raw, "profile", "own_fields", "they_keep_up_to_date", "self_service")
    if profile:  # what they see and keep up to date on their own record (their phone, their bank account: those changes
        portal["profile"] = [slug(f) for f in _names(profile)] if not isinstance(profile, bool) else []  # may need approval)
    join = _first(raw, "sign_up", "self_register", "registration", "join", "self_sign_up")
    if join:  # outside people can join by themselves (suppliers registering); their record starts at its first stage
        fields = _first(join, "fields", "ask", "questions", default=[]) if isinstance(join, dict) else []
        portal["sign_up"] = {"fields": [slug(f) for f in _names(fields)]}
    return portal


def normalize_space(raw):
    """A team's own menu (the Warehouse sees Receiving, Picking and Stock counts, not the books): {"key", "label", "for":
    [teams or roles], "groups": [{"label", "items": [{"table", "view"} | {"page"} | {"flow"}]}], "links": [{"label",
    "url"}] (their SOPs, a carrier's or supplier's site)}. People in it see it as their menu, and can switch to all."""
    if not isinstance(raw, dict):
        raise SchemaError("a space must be an object")
    label = str(_first(raw, "label", "name", "title", default="")).strip()
    key = slug(_first(raw, "key", "id", default=label or "space"))
    who = _first(raw, "for", "teams", "who", "team", "roles", default=[])
    space = {"key": key, "label": label or label_of(key), "for": [_person(x) for x in (_list(who) if not isinstance(who, str) else [who])][:8],
             "icon": icons.resolve(raw.get("icon")) or "layout-grid", "groups": [], "links": []}
    if not space["for"]:
        raise SchemaError(f"the space “{space['label']}” needs \"for\": the teams it's the menu of")
    groups = _first(raw, "groups", "sections", default=None)
    if groups is None and _first(raw, "items", "screens"):  # one plain group
        groups = [{"label": "", "items": _first(raw, "items", "screens")}]
    for g in _list(groups or []):
        if not isinstance(g, dict):
            continue
        items = []
        for item in _list(_first(g, "items", "screens", "lists", default=[])):
            item = {"table": item} if isinstance(item, str) else item
            if not isinstance(item, dict):
                continue
            if _first(item, "flow"):
                items.append({"flow": slug(_first(item, "flow"))})
            elif _first(item, "page", "dashboard"):
                items.append({"page": slug(_first(item, "page", "dashboard"))})
            elif _first(item, "table", "list"):
                entry = {"table": slug(_first(item, "table", "list"))}
                if _first(item, "view", "tab"):
                    entry["view"] = slug(_first(item, "view", "tab"))
                if _first(item, "label", "name", "title"):
                    entry["label"] = str(_first(item, "label", "name", "title"))[:60]
                items.append(entry)
        if items:
            space["groups"].append({"label": str(_first(g, "label", "name", "title", default="")).strip()[:60], "items": items[:20]})
    for link in _list(_first(raw, "links", "useful_links", "bookmarks", default=[])):
        if isinstance(link, dict) and str(_first(link, "url", "href", "link", default="")).startswith(("https://", "http://")):
            space["links"].append({"label": str(_first(link, "label", "name", "title", default=_first(link, "url", "href", "link")))[:60],
                                   "url": str(_first(link, "url", "href", "link"))[:500]})
    if not space["groups"]:
        raise SchemaError(f"the space “{space['label']}” needs \"groups\" of the screens it shows")
    return space


SYNC_EVERY = {"5": 5, "15": 15, "hour": 60, "hourly": 60, "60": 60, "day": 1440, "daily": 1440, "1440": 1440}


def normalize_sync(raw):
    """A connected app's items brought into a list on a schedule: {"key", "app", "pull" (orders, customers, products…),
    "into" (the table), "match" (the field kept with the app's id), "fields": {field: "detail.path"}, "lines": {"into",
    "via", "fields"} (an order's lines), "every": minutes}."""
    if not isinstance(raw, dict):
        raise SchemaError("a sync must be an object")
    app, pull = slug(str(_first(raw, "app", "connector", "from", default=""))), slug(str(_first(raw, "pull", "what", "brings", default="")))
    into = slug(str(_first(raw, "into", "table", "to", default="")))
    if not app or not pull or not into:
        raise SchemaError("a sync needs \"app\" (shopify, woocommerce, xero…), \"pull\" (orders, customers…) and \"into\" (the table)")
    fields = _first(raw, "fields", "map", "mapping", default={})
    if not isinstance(fields, dict) or not fields:
        raise SchemaError("a sync needs \"fields\": {field key: the app's detail, such as \"customer.email\"}")
    sync = {"key": slug(_first(raw, "key", "id", default=f"{app}_{pull}")), "app": app, "pull": pull, "into": into,
            "match": slug(str(_first(raw, "match", "id_field", "external_id", "match_on", default=f"{app}_id"))),
            "fields": {slug(k): str(v) for k, v in fields.items() if k},
            "every": SYNC_EVERY.get(slug(str(_first(raw, "every", "frequency", default="15"))).replace("_minutes", "").replace("minutes", ""), 15)}
    lines = _first(raw, "lines", "line_items", "items")
    if isinstance(lines, dict) and _first(lines, "into", "table"):
        sync["lines"] = {"into": slug(_first(lines, "into", "table")), "via": slug(str(_first(lines, "via", "link", default=""))),
                         "fields": {slug(k): str(v) for k, v in (_first(lines, "fields", "map", default={}) or {}).items()}}
    return sync


def syncs(bp):
    return list(bp.get("syncs") or [])


def spaces(bp):
    return list(bp.get("spaces") or [])


def normalize_team(raw):
    """A team of people (Accounts payable, Procurement, the evaluation panel): {"key", "name", "about"}. Who is in each
    is set under Settings › People and teams; steps, moves, approvals and tables name a team by its key."""
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict) or not _first(raw, "name", "label", "key", "title"):
        raise SchemaError("a team looks like {\"key\": \"accounts_payable\", \"name\": \"Accounts payable\"}")
    name = str(_first(raw, "name", "label", "title", default="")).strip() or label_of(slug(_first(raw, "key")))
    team = {"key": slug(_first(raw, "key", "id", default=name)), "name": name[:60]}
    about = _first(raw, "about", "description", "does", "purpose")
    if about:
        team["about"] = str(about)[:200]
    if team["key"] in WHO_WORDS:
        raise SchemaError(f"“{team['key']}” is already a word for who does things; give the team another name")
    return team


def teams(bp):
    return list(bp.get("teams") or [])


def portals(bp):
    """The ERP's portals, one for each kind of outside person (clients, subcontractors), as normalize_portal makes them.
    (A design from before there could be several has a single "portal".)"""
    return list(bp.get("portals") or ([bp["portal"]] if bp.get("portal") else []))


def portal_of(bp, table):
    """The portal whose people are the records of `table`, on or off, or None."""
    return next((p for p in portals(bp) if p["table"] == table), None)


def portal_shows(tables, t, people):
    """Whether a table's records reach the people of the portal for the table `people`: its "portal" link points at
    them, or at a record that links to them (a stage's project's client); or it's for everyone in a portal (the one
    named, or any)."""
    if (t.get("portal") or {}).get("everyone"):
        return not t["portal"].get("people") or t["portal"]["people"] == people
    head, _, hop = (t.get("portal") or {}).get("via", "").partition(".")
    via = field(t, head)
    if via is None or via["type"] != "link":
        return False
    if hop:  # through a named link of the record it links to: a clarification's tender's invited suppliers; or on to a
        # record that links to them (a verification document's case's seller, whose people they are)
        on = field(tables.get(via["table"]), hop)
        if not on or on["type"] != "link":
            return False
        return on["table"] == people or any(f["type"] == "link" and f["table"] == people for f in (tables.get(on["table"]) or {}).get("fields", []))
    return via["table"] == people or any(f["type"] == "link" and f["table"] == people for f in (tables.get(via["table"]) or {}).get("fields", []))


def portal_link(tables, t):
    """The portal's first link on a table's records ("tender" of "tender.invited")."""
    return field(t, (t.get("portal") or {}).get("via", "").partition(".")[0])


def normalize_colour_rule(raw, table_label=""):
    """{"when": conditions, "color": "red", "field": a field to colour (else the whole record)}: overdue invoices red,
    VIP customers violet, stock below its reorder level amber."""
    if not isinstance(raw, dict):
        raise SchemaError(f"a colour rule on {table_label} looks like {{\"when\": [conditions], \"color\": \"red\"}}")
    colour = slug(_first(raw, "color", "colour", "style", "tone", default=""))
    colour = {"grey": "gray", "purple": "violet", "danger": "red", "warning": "amber", "success": "green", "info": "blue",
              "good": "green", "bad": "red", "warn": "amber"}.get(colour, colour)
    if colour not in COLOURS_NAMED:
        raise SchemaError(f"a colour rule on {table_label} needs a \"color\": one of {', '.join(COLOURS_NAMED)}")
    when = _first(raw, "when", "if", "conditions", "where")
    if not when:
        raise SchemaError(f"a colour rule on {table_label} needs \"when\": the conditions for the colour")
    rule = {"when": [normalize_condition(c) for c in _list(when)], "color": colour}
    target = _first(raw, "field", "column", "on")
    if target:
        rule["field"] = slug(target)
    return rule


def normalize_rule(raw, table_label=""):
    """A rule every saved record must keep: {"check": "total_debit == total_credit", "message": "Debits and credits must
    balance", "when": [conditions], "field": where to show the message}."""
    if isinstance(raw, str):
        raw = {"check": raw}
    if not isinstance(raw, dict):
        raise SchemaError(f"a rule on {table_label} looks like {{\"check\": \"end_date >= start_date\", \"message\": \"...\"}}")
    holds = _first(raw, "holds", "must_hold")  # conditions that must all hold (a gate's only_if)
    check = _first(raw, "check", "expression", "formula", "must", "condition", "assert") or ("true" if holds else None)
    if not check:
        raise SchemaError(f"a rule on {table_label} needs \"check\": an expression that must be true")
    try:
        expr.parse(str(check))
    except expr.ExprError as exc:
        raise SchemaError(f"the rule “{check}” on {table_label}: {exc}") from exc
    rule = {"check": str(check), "message": str(_first(raw, "message", "error", "text", default="") or f"This must be true: {check}")[:300]}
    when = _first(raw, "when", "only_if", "if", "applies_when")
    if when:
        rule["when"] = [normalize_condition(c) for c in _list(when)]
    if holds:
        rule["holds"] = [normalize_condition(c) for c in _list(holds)]
    where = _first(raw, "field", "show_on", "on_field")
    if where:
        rule["field"] = slug(where)
    return rule


def normalize_limit(raw, table_label=""):
    """How many records may share something, or that their times may not overlap: {"per": ["event_date"], "max": 5}
    (five jobs a day; "max" can be a number field through links: "session.class.capacity"), or {"per": ["room"],
    "overlap": {"start": "check_in", "end": "check_out"}} (a room booked once at a time), each with "where": the records
    it counts (not cancelled ones) and "message"."""
    if not isinstance(raw, dict):
        raise SchemaError(f"a limit on {table_label} looks like {{\"per\": [\"event_date\"], \"max\": 5}} or "
                          f"{{\"per\": [\"room\"], \"overlap\": {{\"start\": \"check_in\", \"end\": \"check_out\"}}}}")
    limit = {"per": [slug(x) for x in _names(_first(raw, "per", "for_each", "same", "group_by", "by", default=[]))]}
    most = _first(raw, "max", "at_most", "most", "limit", "capacity", "max_count")
    overlap = _first(raw, "overlap", "no_overlap", "between", "range", "times")
    if isinstance(overlap, (list, tuple)) and overlap:
        overlap = {"start": overlap[0], "end": overlap[1] if len(overlap) > 1 else None}
    if isinstance(overlap, dict) and _first(overlap, "start", "from", "begins"):
        end = _first(overlap, "end", "to", "until", "ends")
        length = _first(overlap, "length", "duration", "takes")  # how long each takes (a duration field), when no end is kept
        limit["overlap"] = {"start": slug(_first(overlap, "start", "from", "begins")), **({"end": slug(end)} if end else {}),
                            **({"length": slug(length)} if length and not end else {})}
    elif most is not None and to_number_or_path(most) is not None:
        limit["max"] = to_number_or_path(most)
    else:
        raise SchemaError(f"a limit on {table_label} needs \"max\": how many may share {', '.join(limit['per']) or 'it'} (a number, "
                          f"or a number field like \"session.class.capacity\"), or \"overlap\": {{\"start\", \"end\"}}")
    where = _first(raw, "where", "only_if", "when", "counting", "if")
    if where:
        limit["where"] = [normalize_condition(c) for c in _list(where)]
    message = _first(raw, "message", "error", "text")
    if message:
        limit["message"] = str(message)[:300]
    return limit


def to_number_or_path(raw):
    """A limit's most: 5, "5", or a field ("capacity", "session.class.capacity")."""
    number = expr.to_number(raw)
    if number is not None:
        return number if number > 0 else None
    text = re.sub(r"^\{\{\s*|\s*\}\}$", "", str(raw).strip())
    text = re.sub(r"^record\.", "", text)
    parts = [slug(x) for x in text.split(".") if slug(x)]
    return ".".join(parts) or None


def normalize_section(raw):
    if not isinstance(raw, dict):
        raise SchemaError("a section looks like {\"label\": \"Billing\", \"fields\": [\"rate\", \"terms\"]}")
    fields = [slug(f) for f in _list(_first(raw, "fields", "columns", default=[]))]
    lines = _first(raw, "lines", "line_items", "items")
    if not fields and not lines:
        raise SchemaError("a section needs its fields, or the lines it is ({\"label\": \"What to price\", \"lines\": \"tender_lines\"})")
    section = {"label": str(_first(raw, "label", "name", "title", default="Details")).strip()[:60], "fields": fields}
    if lines:  # a step of the form that is the record's lines, where it stands among the others
        section["lines"] = slug(str(lines))
    show_if = _first(raw, "show_if", "visible_if", "only_if", "show_when")
    if show_if:  # the whole section shows only while these hold (Delivery details for delivered orders)
        section["show_if"] = [normalize_condition(c) for c in _list(show_if)]
    return section


def normalize_document(raw):
    """A PDF the table can produce (an invoice, a quote): which fields go where. The layout itself is the platform's."""
    if not isinstance(raw, dict):
        raise SchemaError("a document template must be an object")
    label = str(_first(raw, "label", "name", "title", default="")).strip()
    key = slug(_first(raw, "key", "id", default=label or "document"))
    fmt = slug(_first(raw, "format", "kind", "output", default="pdf"))
    if fmt in ("text", "txt", "csv", "aba", "bank_file", "file", "fixed_width", "export", "xml"):
        # a file in another system's layout (a bank's payment file, a CSV for the accountant): a header, a line per
        # record listed, a footer, each a template with {{record.x}} / {{line.x}} and pad(), lpad(), digits(), cents()
        lines = _first(raw, "lines", "line_items", "items", "rows")
        doc = {"key": key, "label": label or label_of(key), "format": "text",
               "extension": slug(_first(raw, "extension", "ext", "file_type", default="csv" if fmt == "csv" else "aba" if fmt in ("aba", "bank_file") else "txt"))[:8] or "txt",
               "header": str(_first(raw, "header", "first_line", "top", default=""))[:4000],
               "line": str(_first(raw, "line", "row", "each", "line_template", "row_template", default=""))[:4000],
               "footer": str(_first(raw, "footer", "last_line", "bottom", "trailer", default=""))[:4000]}
        if isinstance(lines, dict) and _first(lines, "table"):
            doc["lines"] = _listing(lines)
        if not doc["line"] and not doc["header"]:
            raise SchemaError(f"the file “{doc['label']}” needs \"line\" (a template for each line) or \"header\"")
        only = _first(raw, "only_if", "when", "show_if")
        if only:  # offered only while these hold (the bank file once the payment run is approved)
            doc["only_if"] = [normalize_condition(c) for c in _list(only)]
        return doc

    if fmt in ("label", "labels", "sticker", "stickers", "tag", "tags", "barcode_label", "shipping_label", "bin_label", "asset_tag"):
        # a label to stick on the thing (a product's barcode, a bin, an asset, a parcel): printed, never kept
        from ..documents.labels import size_of
        doc = {"key": key, "label": label or label_of(key), "format": "label",
               "size": size_of(str(_first(raw, "size", "paper", "stock", default=""))),
               "fields": [slug(f) for f in _list(_first(raw, "fields", "details", "lines", default=[]))][:5]}
        for name, keys in (("title_field", ("title_field", "title", "name_field")), ("code_field", ("code_field", "barcode_field", "barcode", "code_from")),
                           ("copies_field", ("copies_field", "copies", "quantity_field", "how_many"))):
            value = _first(raw, *keys)
            if isinstance(value, str) and value.strip():
                doc[name] = slug(value)
        code = slug(str(_first(raw, "code", "code_type", "symbology", default="")))
        if code in ("qr", "qr_code", "qrcode"):
            doc["code"] = "qr"
        elif code in ("barcode", "code128", "code_128", "bar"):
            doc["code"] = "barcode"
        return doc

    doc = {"key": key, "label": label or label_of(key), "title": str(_first(raw, "title", "heading", default=label or label_of(key))).strip()[:60]}
    number = _first(raw, "number_field", "number", "reference_field")
    if number:
        doc["number_field"] = slug(number)
    doc["fields"] = [slug(f) for f in _list(_first(raw, "fields", "meta", "header_fields", default=[]))]
    to = _first(raw, "to", "recipient", "bill_to", "customer")
    if isinstance(to, str):
        to = {"field": to}
    if isinstance(to, dict) and _first(to, "field", "link", "key"):
        doc["to"] = {"field": slug(_first(to, "field", "link", "key")), "label": str(_first(to, "label", default="To")),
                     "details": [slug(f) for f in _list(_first(to, "details", "fields", "lines", default=[]))]}
    doc["details"] = [slug(f) for f in _list(_first(raw, "details", "extra_fields", default=[]))]
    lines = _first(raw, "lines", "line_items", "items")
    if isinstance(lines, dict) and _first(lines, "table"):
        doc["lines"] = _listing(lines)
    sections = [_listing(x) for x in _list(_first(raw, "sections", "more_lines", "lists", default=[])) if isinstance(x, dict) and _first(x, "table")]
    if sections:  # more lists under their own headings: an owner statement's expenses after the rent collected
        doc["sections"] = sections
    doc["totals"] = [slug(f) for f in _list(_first(raw, "totals", "summary", default=[]))]
    net = _first(raw, "net", "net_label", "balance_label")
    if net:
        doc["net"] = str(net).strip()[:60]
    for key_ in ("notes", "footer"):
        text = _first(raw, key_)
        if text:
            doc[key_] = str(text)[:2000]
    only = _first(raw, "only_if", "show_if")
    if only:
        doc["only_if"] = [normalize_condition(c) for c in _list(only)]
    return doc


def _listing(raw):
    """A list of records on a document: {table, via: their link to the record (or through one more link:
    "property.owner"), columns, filters, total: the field summed under it, label, subtract: its total counts against}."""
    via = str(_first(raw, "via", "link", "link_field", default=""))
    out = {"table": slug(raw["table"]), "via": ".".join(slug(p) for p in via.split(".")),
           "columns": [slug(c) for c in _list(_first(raw, "columns", "fields", default=[]))]}
    only = _first(raw, "filters", "where", "only")  # a statement lists only what's still owed
    if only:
        out["filters"] = [normalize_condition(c) for c in _list(only)]
    total = _first(raw, "total", "sum", "total_field")
    if total:
        out["total"] = slug(total)
    label = _first(raw, "label", "heading", "title")
    if label:
        out["label"] = str(label).strip()[:60]
    if raw.get("subtract") in (True, "true", "yes", 1) or raw.get("less") in (True, "true", "yes", 1):
        out["subtract"] = True
    return out


def normalize_form(raw):
    """A form anyone with its link can fill in (an enquiry, a booking request); each answer becomes a record."""
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        raise SchemaError("a public form must be an object")
    return {"enabled": raw.get("enabled", True) not in (False, "false", 0),
            "title": str(_first(raw, "title", "heading", default="")).strip()[:120],
            "intro": str(_first(raw, "intro", "description", "text", default="")).strip()[:1000],
            "fields": [slug(f) for f in _list(_first(raw, "fields", "questions", default=[]))],
            "submit": str(_first(raw, "submit", "submit_label", "button", default="Send")).strip()[:40],
            "success": str(_first(raw, "success", "thanks", "confirmation", default="Thanks, we've received it.")).strip()[:300]}


COLOURS = {"blue": "#2563eb", "indigo": "#5b5bd6", "violet": "#7c3aed", "purple": "#9333ea", "green": "#16a34a",
           "emerald": "#059669", "teal": "#0d9488", "cyan": "#0891b2", "orange": "#ea580c", "amber": "#d97706",
           "red": "#dc2626", "rose": "#e11d48", "pink": "#db2777", "slate": "#475569", "black": "#111827", "navy": "#1e3a8a",
           "brown": "#92400e", "gold": "#b45309"}


def normalize_theme(raw):
    if isinstance(raw, str):
        raw = {"accent": raw}
    if not isinstance(raw, dict):
        raise SchemaError("a theme looks like {\"accent\": \"#0d9488\", \"density\": \"compact\"}")
    theme = {}
    accent = str(_first(raw, "accent", "color", "colour", "brand", "primary", default="")).strip().lower()
    if accent:
        accent = COLOURS.get(accent, accent)
        if not re.fullmatch(r"#[0-9a-f]{6}", accent):
            raise SchemaError(f"“{accent}” isn't a colour; give a hex code like #0d9488 or a name like teal")
        theme["accent"] = accent
    density = slug(_first(raw, "density", "spacing", default=""))
    if density:
        density = {"dense": "compact", "tight": "compact", "comfy": "comfortable", "spacious": "comfortable", "normal": "comfortable"}.get(density, density)
        if density not in DENSITIES:
            raise SchemaError("density is comfortable or compact")
        theme["density"] = density
    return theme


IRREGULAR = {"children": "child", "people": "person", "staff": "staff", "men": "man", "women": "woman", "feet": "foot",
             "teeth": "tooth", "geese": "goose", "mice": "mouse", "series": "series", "species": "species", "news": "news",
             "analyses": "analysis", "crises": "crisis", "diagnoses": "diagnosis", "criteria": "criterion"}


def _singular(label):
    words = label.split()
    if not words:
        return label
    last = words[-1]
    if last.lower() in IRREGULAR:
        one = IRREGULAR[last.lower()]
        last = one.capitalize() if last[:1].isupper() else one
    elif last.lower().endswith("ies"):
        last = last[:-3] + "y"
    elif last.lower().endswith(("sses", "ches", "shes", "xes")):
        last = last[:-2]
    elif last.lower().endswith("s") and not last.lower().endswith("ss"):
        last = last[:-1]
    return " ".join(words[:-1] + [last])


SYSTEM_KEYS = {"created_time": "created_at", "created": "created_at", "date_created": "created_at", "created_on": "created_at",
               "creation_date": "created_at", "added": "created_at", "modified_time": "updated_at", "updated_time": "updated_at",
               "last_modified": "updated_at", "modified": "updated_at", "updated": "updated_at", "last_updated": "updated_at"}


def system_key(key):
    """created_time, date_created... are the platform's own created_at (every record has it); likewise updated_at."""
    return SYSTEM_KEYS.get(key, key)


def normalize_widget(raw):
    if not isinstance(raw, dict):
        raise SchemaError("a widget must be an object")
    raw = {k: (system_key(slug(v)) if k in ("group_by", "date_field", "rows", "across", "stack_by", "by", "group", "x", "dimension") and isinstance(v, str) else v)
           for k, v in raw.items()}
    if isinstance(raw.get("compare"), dict) and isinstance(raw["compare"].get("date_field"), str):
        raw["compare"] = {**raw["compare"], "date_field": system_key(slug(raw["compare"]["date_field"]))}
    wtype = slug(_first(raw, "type", "kind", "widget", default="metric"))
    wtype = WIDGET_ALIASES.get(wtype, wtype)
    if wtype not in WIDGETS:
        raise SchemaError(f"“{wtype}” isn't a widget; use one of: {', '.join(WIDGETS)}")
    width = expr.to_number(_first(raw, "width", "span", "size", "cols"))
    width = {"small": 1, "quarter": 1, "half": 2, "medium": 2, "large": 3, "wide": 4, "full": 4}.get(str(_first(raw, "width", "span", "size", default="")).lower(), width)
    shaped = {"width": int(min(max(width, 1), 4))} if width else {}
    if wtype == "text":
        text = _first(raw, "text", "content", "body", "markdown")
        if not text and isinstance(raw.get("values"), dict):  # often written inside "values"
            text = _first(raw["values"], "text", "content", "body", "markdown")
        if not text:
            raise SchemaError("a text widget needs its \"text\"")
        return {"type": "text", "label": str(_first(raw, "label", "title", default="")).strip(), "text": str(text)[:4000], **shaped}
    if wtype == "my_work":  # what's waiting for whoever is looking: approvals, tasks, checks of the AI's work
        return {"type": "my_work", "label": str(_first(raw, "label", "title", default="Waiting for you")).strip()[:80], **shaped}
    if wtype == "heading":
        text = str(_first(raw, "label", "title", "text", default="")).strip()
        if not text:
            raise SchemaError("a heading needs its \"label\"")
        return {"type": "heading", "label": text[:120], "width": 4, **({"text": str(raw["text"])[:300]} if raw.get("text") and raw.get("label") else {})}
    if wtype == "actions":
        buttons = []
        for b in _list(_first(raw, "buttons", "actions", "items", "links", default=[])):
            if not isinstance(b, dict):
                continue
            kind = slug(_first(b, "kind", "type", "do", default="new"))
            kind = {"add": "new", "create": "new", "new_record": "new", "open": "view", "table": "view", "list": "view",
                    "dashboard": "page", "run": "automation", "start": "automation", "url": "link", "web": "link"}.get(kind, kind)
            if kind not in ("new", "view", "page", "automation", "link"):
                raise SchemaError(f"a button can be new (add a record), view (open a table), page, automation (run a scheduled "
                                  f"one now) or link, not “{kind}”")
            button = {"kind": kind, "label": str(_first(b, "label", "text", "title", default="")).strip()[:60]}
            for k in ("table", "view", "page", "automation"):
                if b.get(k):
                    button[k] = slug(b[k])
            if kind == "link":
                url = str(_first(b, "url", "href", default="")).strip()
                if not re.match(r"^https?://", url, re.I):
                    raise SchemaError("a link button needs a \"url\" starting with https://")
                button["url"] = url[:500]
            if isinstance(b.get("values"), dict):
                button["values"] = {slug(k): v for k, v in b["values"].items()}
            if kind in ("new", "view") and not button.get("table"):
                raise SchemaError(f"the {kind} button “{button['label']}” needs its \"table\"")
            if kind == "page" and not button.get("page"):
                raise SchemaError(f"the page button “{button['label']}” needs its \"page\"")
            if kind == "automation" and not button.get("automation"):
                raise SchemaError(f"the automation button “{button['label']}” needs its \"automation\" (key)")
            buttons.append(button)
        if not buttons:
            raise SchemaError("an actions widget needs \"buttons\": [{\"kind\": \"new\", \"table\": \"orders\", \"label\": \"New order\"}]")
        return {"type": "actions", "label": str(_first(raw, "label", "title", default="")).strip(), "buttons": buttons[:12], **shaped}
    if wtype == "embed":
        url = str(_first(raw, "url", "src", "link", default="")).strip()
        if not re.match(r"^https://", url, re.I):
            raise SchemaError("an embed widget needs a \"url\" starting with https:// (a Power BI, Google or Looker report, a video)")
        return {"type": "embed", "label": str(_first(raw, "label", "title", default="")).strip(), "url": url[:1000],
                "height": int(min(max(expr.to_number(_first(raw, "height", default=420)) or 420, 200), 1200)), "width": shaped.get("width", 4)}
    if wtype == "insight":
        return {"type": "insight", "label": str(_first(raw, "label", "title", default="")).strip() or "What the numbers say",
                **({"focus": str(raw["focus"])[:300]} if raw.get("focus") else {}), **shaped}
    if wtype == "activity":
        widget = {"type": "activity", "label": str(_first(raw, "label", "title", default="")).strip() or "Recent activity",
                  "limit": int(min(max(expr.to_number(_first(raw, "limit", "count", default=12)) or 12, 3), 50)), **shaped}
        tables = _first(raw, "tables", "table", "of")
        if tables:
            widget["tables"] = [slug(x) for x in _list(tables)]
        return widget
    table = _first(raw, "table", "source", "of", "from") or (_list(raw.get("tables")) or [None])[0]
    if not table:
        raise SchemaError(f"the {wtype} widget needs the table it shows")
    widget = {"type": wtype, "table": slug(table), "label": str(_first(raw, "label", "title", "name", default="")).strip()}
    filters = _first(raw, "filters", "filter", "where", "conditions")
    if filters:
        widget["filters"] = [normalize_condition(c) for c in _list(filters)]
    if wtype in ("metric", "chart", "goal", "pivot"):
        aggregate = slug(_first(raw, "aggregate", "function", "agg", "calculation", default="count"))
        aggregate = AGG_ALIASES.get(aggregate, aggregate)
        if aggregate not in AGGREGATES:
            raise SchemaError(f"a {wtype} can count, sum, avg, min or max, not “{aggregate}”")
        widget["aggregate"] = aggregate
        of = _first(raw, "field", "value_field", "sum_field", "measure")
        if of:
            widget["field"] = slug(of)
        elif aggregate != "count":
            raise SchemaError(f"the {wtype} “{widget['label']}” needs the \"field\" to {aggregate}")
    if wtype == "chart":
        group = _first(raw, "group_by", "group", "by", "category", "x", "dimension")
        listed = [slug(f) for f in _list(raw.get("fields")) if isinstance(f, str)]
        if not group and listed:  # "fields": ["campaign", "amount"]: by campaign, the amount summed
            group = listed[0]
            if len(listed) > 1 and not widget.get("field"):
                widget.update(field=listed[1], aggregate=widget["aggregate"] if widget.get("aggregate", "count") != "count" else "sum")
        group = group or _first(raw, "date_field")  # "Donations by month": its date, a bar a month
        if not group:
            raise SchemaError(f"the chart “{widget['label']}” needs \"group_by\": the field to break it down by")
        widget["group_by"] = slug(group)
        style = slug(_first(raw, "style", "chart", "kind", "chart_type", default=""))
        style = {"pie": "donut", "doughnut": "donut", "bars": "bar", "column": "bar", "columns": "bar", "trend": "line", "area": "line",
                 "stacked": "bar", "stacked_bar": "bar", "pipeline": "funnel", "conversion": "funnel"}.get(style, style)
        if not style:
            style = {"line": "line", "line_chart": "line", "trend": "line", "pie": "donut", "pie_chart": "donut", "donut": "donut"}.get(slug(_first(raw, "type", default="")), "bar")
        widget["style"] = style if style in CHART_STYLES else "bar"
    if wtype == "form":
        fields = _first(raw, "fields", "columns", "ask")
        if fields:
            widget["fields"] = [slug(f) for f in _list(fields)]
        widget["submit"] = str(_first(raw, "submit", "button", "submit_label", default="Add")).strip()[:40]
        values = _first(raw, "values", "defaults", "preset")
        if isinstance(values, dict):
            widget["values"] = {slug(k): v for k, v in values.items()}
    if wtype == "metric":
        compare = _first(raw, "compare", "compare_to", "trend", "vs")
        if compare:
            if isinstance(compare, str):
                compare = {"period": compare}
            period = slug(_first(compare, "period", "every", "by", default="month")) if isinstance(compare, dict) else "month"
            period = {"monthly": "month", "weekly": "week", "yearly": "year", "annual": "year", "quarterly": "quarter",
                      "previous_month": "month", "last_month": "month", "previous_period": "month"}.get(period, period)
            date_field = _first(compare, "date_field", "date", "field") if isinstance(compare, dict) else None
            widget["compare"] = {"period": period if period in PERIODS else "month", **({"date_field": slug(date_field)} if date_field else {})}
    if wtype == "chart":
        stack = _first(raw, "stack_by", "split_by", "series", "color_by", "stack")
        if stack:
            widget["stack_by"] = slug(stack)
    if wtype == "map":
        where = _first(raw, "location_field", "location", "field")
        if not where:
            raise SchemaError(f"the map “{widget['label']}” needs \"location_field\"")
        widget["location_field"] = slug(where)
    date_basis = _first(raw, "date_field", "by_date", "period_field")
    if date_basis and wtype in ("metric", "chart", "list", "goal", "pivot", "map"):
        widget["date_field"] = slug(date_basis)
    if wtype == "pivot":
        rows = _first(raw, "rows", "group_by", "row_field", "by", "group")
        cols = _first(raw, "across", "columns", "column_field", "split_by", "columns_by")
        cols = cols if not isinstance(cols, list) or len(cols) == 1 else None
        if not rows:
            raise SchemaError(f"the pivot “{widget['label']}” needs \"rows\": the field each row is (a customer, a month)")
        widget["rows"] = slug(rows if not isinstance(rows, list) else rows[0])
        if cols:
            widget["across"] = slug(cols if not isinstance(cols, list) else cols[0])
    if wtype == "goal":
        target = expr.to_number(_first(raw, "target", "goal", "max"))
        if not target:
            raise SchemaError(f"the goal “{widget['label']}” needs a \"target\" number")
        widget["target"] = target
    widget.update(shaped)
    if wtype == "list":
        widget["limit"] = int(min(max(expr.to_number(_first(raw, "limit", "count", "rows", default=5)) or 5, 1), 50))
        columns = _first(raw, "columns", "fields")
        if columns:
            widget["columns"] = [slug(c) for c in _list(columns)]
        sort = _first(raw, "sort", "order_by")
        if sort:
            view = normalize_view({"type": "list", "sort": sort})
            if view.get("sort"):
                widget["sort"] = view["sort"]
    return widget


PRIORITIES = {"low": 0, "normal": 1, "medium": 1, "high": 2, "important": 2, "urgent": 3, "critical": 3, "emergency": 3, "asap": 3}
FLOW_STEPS = ["info", "form", "scan", "pick", "photo", "signature", "decide", "print"]
FLOW_STEP_ALIASES = {"show": "info", "text": "info", "message": "info", "instructions": "info", "fields": "form", "fill": "form",
                     "inputs": "form", "ask": "form", "scan_items": "scan", "scanning": "scan", "barcode": "scan", "count": "pick",
                     "pick_list": "pick", "picklist": "pick", "check_off": "pick", "checklist": "pick", "picture": "photo",
                     "image": "photo", "camera": "photo", "sign": "signature", "signoff": "signature", "choice": "decide",
                     "choose": "decide", "branch": "decide", "buttons": "decide", "approve": "decide", "label": "print",
                     "print_label": "print", "print_labels": "print", "document": "print"}


def normalize_flow(raw):
    """A flow: work done step by step on a phone or at a bench (receiving a delivery, picking an order, a stock count,
    a return), each step one screen: read, fill in, scan, count off, take a photo, sign, decide, print. It's about one
    record of "table": the one it's started from ("start": "record", a button on it), or one it makes when it's done
    ("start": "new", from the menu). Nothing is saved until the last step; then the record gets what was filled in,
    scanned items become its lines, counts go on its lines, and "then" runs (rule steps, with {{flow.step.x}})."""
    if not isinstance(raw, dict):
        raise SchemaError("a flow must be an object")
    label = str(_first(raw, "label", "name", "title", default="")).strip()
    key = slug(_first(raw, "key", "id", default=label or "flow"))
    table = slug(_first(raw, "table", "about", "record", "on", default=""))
    if not table:
        raise SchemaError(f"the flow “{label or key}” needs the \"table\" whose record it works on")
    start = slug(str(_first(raw, "start", "starts", "from", "mode", default="record")))
    start = "new" if start in ("new", "create", "menu", "nav", "add", "creates", "makes") else "record"
    flow = {"key": key, "label": label or label_of(key), "table": table, "start": start,
            "icon": icons.resolve(raw.get("icon")) or "list-todo",
            "steps": [normalize_flow_step(s, n) for n, s in enumerate(_list(_first(raw, "steps", "screens", "pages", default=[])), start=1)]}
    if not flow["steps"]:
        raise SchemaError(f"the flow “{flow['label']}” needs \"steps\": what's done on each screen")
    keys_ = [s["key"] for s in flow["steps"]]
    for k in {k for k in keys_ if keys_.count(k) > 1}:
        raise SchemaError(f"the flow “{flow['label']}” has two steps called “{k}”")
    about = str(_first(raw, "description", "about", "help", default="")).strip()
    if about:
        flow["description"] = about[:300]
    when = _first(raw, "when", "only_if", "show_if", "conditions")
    if when and start == "record":  # the button shows only while these hold (Receive goods once the order is sent)
        flow["when"] = [normalize_condition(c) for c in _list(when)]
    who = _first(raw, "who", "for", "teams", "roles", "access")
    if who:
        flow["who"] = [_person(x) for x in (_list(who) if not isinstance(who, str) else [who])][:8]
    then = [normalize_action(a) for a in _list(_first(raw, "then", "on_finish", "finish", "after", "actions", default=[]))]
    if then:
        flow["then"] = then
    if _first(raw, "again", "repeat", "do_another", "restart") in (True, "true", "yes", 1):
        flow["again"] = True  # "Do another" when it's done: one delivery after another
    return flow


def normalize_flow_step(raw, n=1):
    if not isinstance(raw, dict):
        raise SchemaError("a flow's step must be an object")
    kind_raw = slug(str(_first(raw, "kind", "type", "step", "do", default="")))
    kind = FLOW_STEP_ALIASES.get(kind_raw, kind_raw)
    if kind not in FLOW_STEPS:
        raise SchemaError(f"“{kind_raw}” isn't a kind of flow step; use one of: {', '.join(FLOW_STEPS)}")
    label = str(_first(raw, "label", "title", "name", "heading", default="")).strip()
    step = {"key": slug(_first(raw, "key", "id", default=label or f"{kind}_{n}")), "kind": kind,
            "label": label or {"info": "Read this", "form": "Fill in", "scan": "Scan", "pick": "Count off", "photo": "Take a photo",
                               "signature": "Sign", "decide": "Decide", "print": "Print"}[kind]}
    text = _first(raw, "text", "help", "instructions", "body", "message")
    if text:
        step["text"] = str(text)[:2000]
    if kind == "form":
        step["fields"] = [slug(f if not isinstance(f, dict) else _first(f, "key", "field", "name"))
                          for f in (_names(_first(raw, "fields", "inputs", default=[])) if isinstance(_first(raw, "fields", "inputs"), str)
                                    else _list(_first(raw, "fields", "inputs", default=[])))][:12]
        if not step["fields"]:
            raise SchemaError(f"the flow step “{step['label']}” needs \"fields\" to fill in")
    elif kind == "scan":  # each code scanned finds a record ("find"), and they become lines of the flow's record
        into = _first(raw, "into", "lines", "makes", "creates", default={})
        if not isinstance(into, dict) or not _first(into, "table"):
            raise SchemaError(f"the flow step “{step['label']}” needs \"into\": {{table, via, item, quantity}}: the lines scanned items become")
        step["into"] = {"table": slug(_first(into, "table")), "via": slug(_first(into, "via", "link", "parent", default="")),
                        "item": slug(_first(into, "item", "field", "product", "link_to", default=""))}
        quantity = _first(into, "quantity", "qty", "count", "amount")
        if quantity:
            step["into"]["quantity"] = slug(quantity)
        same = slug(str(_first(raw, "duplicates", "same_twice", "repeat", default="count")))
        step["duplicates"] = {"reject": "reject", "no": "reject", "unique": "reject", "serials": "reject", "allow": "allow",
                              "separate": "allow"}.get(same, "count")  # scanning the same twice counts two of it
        least = to_number_safe(_first(raw, "min", "at_least", "minimum"))
        if least:
            step["min"] = int(least)
    elif kind == "pick":  # the record's lines, each to be counted off to a target, by scanning or by tapping
        lines = _first(raw, "lines", "items", "of", default={})
        if not isinstance(lines, dict) or not _first(lines, "table"):
            raise SchemaError(f"the flow step “{step['label']}” needs \"lines\": {{table, via}}: the lines to count off")
        step["lines"] = {"table": slug(_first(lines, "table")), "via": slug(_first(lines, "via", "link", "parent", default=""))}
        for name, keys in (("target", ("target", "quantity", "qty", "ordered", "expected")), ("count_into", ("count_into", "save_to", "into", "received", "counted")),
                           ("code", ("code", "barcode", "scan", "code_field", "match"))):
            value = _first(raw, *keys)
            if value:
                step[name] = ".".join(slug(p) for p in str(value).split("."))
        if not step.get("count_into"):
            raise SchemaError(f"the flow step “{step['label']}” needs \"count_into\": the number field of each line the count is saved in")
        short = slug(str(_first(raw, "short", "less", "under", default="ask")))
        step["short"] = {"block": "block", "no": "block", "never": "block", "allow": "allow", "yes": "allow"}.get(short, "ask")
    elif kind in ("photo", "signature"):
        f = _first(raw, "field", "save_to", "into", default="")
        if not f:
            raise SchemaError(f"the flow step “{step['label']}” needs \"field\": where the {kind} is kept")
        step["field"] = slug(f)
        if raw.get("optional") in (True, "true", "yes", 1) or raw.get("required") in (False, "false", "no", 0):
            step["optional"] = True
    elif kind == "decide":  # buttons: each sets a field ("decision": Accept / Reject) and may end the flow there
        step["field"] = slug(_first(raw, "field", "save_to", "sets", default=""))
        options = []
        for o in _list(_first(raw, "options", "buttons", "choices", default=[])):
            o = {"label": o} if isinstance(o, str) else o
            if isinstance(o, dict) and _first(o, "label", "value"):
                choice = {"label": str(_first(o, "label", "value"))[:60], "value": str(_first(o, "value", "label"))[:120]}
                if _first(o, "finish", "end", "done", "stop") in (True, "true", "yes", 1):
                    choice["finish"] = True
                options.append(choice)
        if len(options) < 2:
            raise SchemaError(f"the flow step “{step['label']}” needs two or more \"options\" (buttons)")
        step["options"] = options[:4]
    elif kind == "print":  # on the last screen, once everything's saved: the record's label or document, or each line's
        step["document"] = slug(_first(raw, "document", "label", "template", "doc", default=""))
        of = _first(raw, "of", "for", "lines", default="record")
        step["of"] = "record" if str(of).lower() in ("record", "it", "this", "") else slug(of)
        if _first(raw, "auto", "automatic", "by_itself", "straight_away") in (True, "true", "yes", 1):
            step["auto"] = True  # on the printer as soon as it's saved, where one is chosen
    return step


def to_number_safe(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def flows(bp):
    return list(bp.get("flows") or [])


def normalize_page(raw):
    if not isinstance(raw, dict):
        raise SchemaError("a page must be an object")
    label = str(_first(raw, "label", "name", "title", default="")).strip()
    key = slug(_first(raw, "key", "id", "page", default=label or "overview"))
    page = {"key": key, "label": label or label_of(key), "widgets": [normalize_widget(w) for w in _list(_first(raw, "widgets", "cards", "items", default=[]))]}
    module = _first(raw, "module", "group", "section")
    if module:
        page["module"] = slug(module)
    page["icon"] = icons.resolve(raw.get("icon")) or "layout-dashboard"
    home = _first(raw, "home_for", "home", "landing_for", "default_for")
    if home:  # roles, or teams' keys (Accounts payable's home page)
        roles = [r for r in (slug(x) for x in _list(home if not isinstance(home, str) else [home])) if r and r not in ("portal", "creator")]
        if roles:
            page["home_for"] = roles
    access = slug(_first(raw, "access", "visible_to", "only_for", default=""))
    access = {"admins": "admin", "owners": "owner", "editors": "editor", "managers": "admin", "manager": "admin"}.get(access, access)
    if access and (access in ROLE_NAMES[1:] or access not in ROLE_NAMES):
        page["access"] = access  # a role, or a team's key
    return page


WEEKDAY_NAMES = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri", "saturday": "sat",
                 "sunday": "sun"}


def _timing(text):
    """A schedule's timing written in words where it doesn't belong ("Friday 3pm", "daily at 08:00", "monthly")."""
    text = text.lower()
    said = {}
    day = next((short for name, short in WEEKDAY_NAMES.items() if re.search(rf"\b({name}|{short})s?\b", text)), None)
    if day:
        said.update(every="week", weekday=day)
    elif re.search(r"\b(monthly|every month|each month|month end|1st of)\b", text):
        said["every"] = "month"
    elif re.search(r"\b(hourly|every hour)\b", text):
        said["every"] = "hour"
    elif re.search(r"\b(daily|every day|each day|every morning|nightly)\b", text):
        said["every"] = "day"
    time = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b", text)
    if time:
        said["at"] = time.group(0).replace(" ", "")
    return said


def normalize_trigger(raw):
    if isinstance(raw, str):
        raw = {"type": raw}
    if not isinstance(raw, dict):
        raise SchemaError("a trigger must be an object like {\"type\": \"record_updated\", \"table\": \"projects\"}")
    ttype_raw = str(_first(raw, "type", "event", "on", "kind", "when", default="")).strip().lower()
    ttype = TRIGGER_ALIASES.get(slug(ttype_raw), slug(ttype_raw))
    if ttype not in TRIGGERS:
        raise SchemaError(f"“{ttype_raw}” isn't a trigger; use one of: {', '.join(TRIGGERS)}")
    trigger = {"type": ttype}
    table = _first(raw, "table", "on_table", "object", "entity")
    if table:
        trigger["table"] = slug(table)
    elif ttype not in ("schedule", "email_received", "file_added", "webhook_received"):
        raise SchemaError(f"the {ttype} trigger needs the table it watches")
    if ttype == "email_received":
        for key in ("from_contains", "subject_contains", "body_contains"):
            value = _first(raw, key, key.replace("_contains", ""))
            if value:
                trigger[key] = str(value)[:200]
        if raw.get("has_attachment") in (True, "true", "yes", 1) or raw.get("with_attachments") in (True, "true", "yes", 1):
            trigger["has_attachment"] = True
    if ttype == "file_added":
        folder = _first(raw, "folder", "path", "in", "onedrive_folder")
        if not folder:
            raise SchemaError("the file_added trigger needs the OneDrive \"folder\" to watch, e.g. \"Invoices/Incoming\"")
        trigger["folder"] = str(folder).strip("/ ")[:300]
    if ttype == "schedule":
        said = _timing(" ".join(str(raw.get(k) or "") for k in ("label", "name", "when", "description", "cron", "subject_contains",
                                                                "body_contains", "from_contains", "folder")))
        every = slug(_first(raw, "every", "frequency", "interval", "period", default=""))
        every = {"daily": "day", "days": "day", "weekly": "week", "weeks": "week", "hourly": "hour", "hours": "hour",
                 "monthly": "month", "months": "month"}.get(every, every) or said.get("every") or \
            {"weekly": "week", "hourly": "hour"}.get(ttype_raw, "day")
        if every not in EVERY:
            raise SchemaError(f"a schedule runs every hour, day, week or month, not “{every}”")
        trigger["every"] = every
        if said.get("weekday") and not _first(raw, "weekday", "day", "on_day"):
            raw = {**raw, "weekday": said["weekday"]}
        at = str(_first(raw, "at", "time", "hour", default=said.get("at") or "08:00"))
        match = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", at.strip().lower())
        hour, minute = (int(match.group(1)), int(match.group(2) or 0)) if match else (8, 0)
        if match and match.group(3) == "pm" and hour < 12:
            hour += 12
        trigger["at"] = f"{min(hour, 23):02d}:{min(minute, 59):02d}"
        if every == "week":
            day = slug(_first(raw, "weekday", "day", "on_day", default="monday"))[:3]
            trigger["weekday"] = day if day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun") else "mon"
    if ttype == "button":
        trigger["label"] = str(_first(raw, "label", "button", "text", "name", default="Run")).strip()[:40]
    return trigger


def normalize_action(raw):
    if not isinstance(raw, dict):
        raise SchemaError("an action must be an object like {\"type\": \"notify\", \"message\": \"...\"}")
    atype_raw = str(_first(raw, "type", "action", "do", "kind", default="")).strip().lower()
    atype = ACTION_ALIASES.get(slug(atype_raw), slug(atype_raw))
    if atype not in ACTIONS:
        raise SchemaError(f"“{atype_raw}” isn't an action; use one of: {', '.join(ACTIONS)}")
    if atype == "branch":
        return _branch(raw)
    action = {"type": atype}
    if atype in AI_STEPS:
        said = _first(raw, "review", "check", "human_review", "human_check", "checked_by_person", "approval")
        said = {True: "always", "true": "always", "yes": "always", False: "never", "false": "never", "no": "never"}.get(said, said)
        mode = slug(str(said or "never"))
        mode = {"unsure": "when_unsure", "if_unsure": "when_unsure", "when_not_sure": "when_unsure", "low_confidence": "when_unsure",
                "every_time": "always", "all": "always", "each": "always", "none": "never", "off": "never", "auto": "never"}.get(mode, mode)
        if mode not in REVIEW_MODES:
            raise SchemaError(f"an AI step's \"review\" is always, when_unsure or never, not “{said}”")
        if mode != "never":
            action["review"] = mode
            action["reviewer"] = _person(_first(raw, "reviewer", "checked_by", "check_by", "review_by", default="editor"))
    if atype in AI_STEPS:
        reads = []  # reference records it reads besides the record (the supplier's contract prices)
        for ref in _list(_first(raw, "reads", "reference", "references", "compare_with", "against", "look_at", "sources", default=[])):
            if isinstance(ref, str):
                ref = {"table": ref}
            if not isinstance(ref, dict) or not _first(ref, "table", "from"):
                raise SchemaError("an AI step's \"reads\" lists tables it looks at, like {\"table\": \"contract_prices\", \"where\": "
                                  "[{\"field\": \"supplier\", \"op\": \"equals\", \"value\": \"{{record.supplier}}\"}]}")
            one = {"table": slug(_first(ref, "table", "from"))}
            where = _first(ref, "where", "filters", "only")
            if where:
                one["where"] = [normalize_condition(c) for c in _list(where)]
            reads.append(one)
        if reads:
            action["reads"] = reads[:4]
        if _first(raw, "read_files", "read_attachments", "attachments", "files") in (True, "true", "yes", 1):
            action["read_files"] = True  # the record's attached files (a brief, a quote) as well as its fields
        if _first(raw, "learn", "learn_from_corrections") in (False, "false", "no", 0):
            action["learn"] = False  # it's shown people's past corrections of its work unless told not to
        if raw.get("manual") in (True, "true", "yes", 1) or raw.get("enabled") in (False, "false", "no", 0) \
                or str(raw.get("ai", "")).lower() in ("off", "false", "no"):
            action["manual"] = True  # switched off: a person does the job instead, as a task
    if atype in AI_STEPS and _first(raw, "use_knowledge", "knowledge", "search_knowledge",
                                                                         "use_documents", "rag") in (True, "true", "yes", 1):
        action["use_knowledge"] = True
    if atype in ("ai_fill", "ai_check"):  # how sure the AI is, 0 to 100, kept in a number field; below sure_above it isn't sure
        confidence = _first(raw, "confidence", "confidence_field", "score_field")
        if confidence:
            action["confidence"] = slug(str(confidence))
        above = _first(raw, "sure_above", "confidence_above", "threshold", "min_confidence")
        if above not in (None, ""):
            try:
                action["sure_above"] = min(max(int(float(above)), 0), 100)
            except (TypeError, ValueError):
                raise SchemaError("an AI step's sure_above is a number from 0 to 100") from None
    only_if = _first(raw, "only_if", "if", "conditions", "when")
    if only_if and isinstance(only_if, (list, dict)):
        action["only_if"] = [normalize_condition(c) for c in _list(only_if)]
    if atype == "delete_record":
        target = _first(raw, "target", "record", "of")
        if isinstance(target, str) and slug(target) not in ("record", "this", "self", "trigger", "current", "this_record"):
            action["target"] = target_of(target)
        return action
    if atype == "wait":
        until = _first(raw, "until", "until_field", "date_field")
        met = _first(raw, "until_met", "until_true", "until_condition", "until_conditions", "until_all")
        if isinstance(until, (list, dict)):  # "until": [conditions]: until the record meets them
            met, until = until, None
        amount = {k: expr.to_number(_first(raw, k, k[:-1], default=0)) or 0 for k in ("days", "hours", "minutes")}
        if met:  # until it's paid (or signed, or approved), at most "days" (then the steps after it go on regardless)
            action["until_met"] = [normalize_condition(c) for c in _list(met)]
            action.update({k: v for k, v in amount.items() if v})
            return action
        if until:
            action["until"] = slug(until)
        elif not any(amount.values()):
            raise SchemaError("wait needs how long (\"days\", \"hours\" or \"minutes\"), \"until\": a date field of the record, "
                          "or \"until_met\": conditions the record must meet (until it's paid)")
        else:
            action.update({k: v for k, v in amount.items() if v})
        return action
    if atype == "assign":
        target = _first(raw, "field", "to_field", "into", "assignee_field")
        if not target:
            raise SchemaError("assign needs \"field\": the person field to fill")
        action["field"] = slug(target)
        pool = slug(_first(raw, "from", "role", "among", "pool", default="editor"))
        action["from"] = {"admins": "admin", "editors": "editor", "everyone": "editor", "team": "editor"}.get(pool, pool)
        return action
    if atype == "allocate":  # a driver for the job, a room for the booking: records of the list a link field leads to, picked
        target = _first(raw, "field", "to_field", "into")
        if not target:
            raise SchemaError("allocate needs \"field\": the link field to fill with records picked from the list it links to")
        action["field"] = slug(target)
        count = to_number_safe(_first(raw, "count", "how_many", "number", default=1))
        action["count"] = max(int(count or 1), 1)
        where = _first(raw, "where", "among", "only_those")
        if where:
            action["where"] = [normalize_condition(c) for c in _list(where)]
        free = _first(raw, "free_on", "free_that_day", "day")
        if free:
            action["free_on"] = slug(free)
        return action
    if atype == "copy_record":  # a new revision of a contract or order, a renewal, a repeat order: the record and its lines
        values = _first(raw, "values", "set", "changes", "fields", default={})
        action["values"] = {slug(k): _one_expression(v) for k, v in (values.items() if isinstance(values, dict) else []) if k}
        back = _first(raw, "link_back", "previous", "previous_field", "link_to_original", "original_field")
        if back:  # the copy's link to the record it was copied from ("previous version")
            action["link_back"] = slug(back)
        if _first(raw, "lines", "with_lines", "copy_lines") in (False, "false", "no", 0):
            action["lines"] = False
        return action
    if atype in ("update_record", "create_record", "create_for_each", "update_each"):
        said = (raw.get(k) for k in ("values", "fields", "set", "data", "record", "field_values", "updates", "payload"))
        values = next((v for v in said if isinstance(v, dict) or isinstance(v, list) and any(isinstance(i, dict) for i in v)), None)
        if isinstance(values, list):  # [{"field": "status", "value": "Done"}]
            values = {_first(v, "field", "key", "name"): v.get("value") for v in values if isinstance(v, dict)}
        if values in (None, {}, []) and atype in ("create_record", "create_for_each"):
            values = {}  # nothing said: fields with the same name are copied across, and the links filled in (copy_matching)
        elif not isinstance(values, dict) or not values:
            raise SchemaError(f"the {atype} action needs \"values\": {{field: value}}")
        action["values"] = {slug(k): _one_expression(v) for k, v in values.items() if k}
        if atype in ("create_record", "create_for_each") and raw.get("copy_matching", True) not in (False, "false", 0):
            action["copy_matching"] = True
        if atype == "update_each":
            source = _first(raw, "source", "for_each", "records", "each")
            if isinstance(source, str):
                source = {"table": source}
            source = source or {}
            if not _first(source, "table") or not _first(source, "via", "link"):
                raise SchemaError("update_each needs \"source\": {\"table\": the related records, \"via\": their link to this record}")
            action["source"] = _source(source, _first(source, "table"), _first(source, "via", "link"))
            where = _first(raw, "where", "filters", "only_those")
            if where:
                action["where"] = [normalize_condition(c) for c in _list(where)]
        elif atype in ("create_record", "create_for_each"):
            table = _first(raw, "table", "in_table", "target_table", "into")
            if not table and atype == "create_record":
                raise SchemaError(f"the {atype} action needs the table to create records in")
            action["table"] = slug(table) if table else None  # create_for_each: found from the step before it, if it can be
            if atype == "create_for_each":
                source = _first(raw, "source", "for_each", "from", "each")
                if isinstance(source, str):
                    source = {"table": source}
                source = source or {}
                each_of = _first(source, "field", "each_of", "people") or _first(raw, "each_of", "for_each_in", "per")
                if each_of and not _first(source, "table"):  # one for each record or person in a field (each invited supplier)
                    action["source"] = {"field": slug(each_of)}
                    where = _first(raw, "where", "filters", "only_those")
                    if where:
                        action["where"] = [normalize_condition(c) for c in _list(where)]
                    return action
                source_table = _first(source, "table") or _first(raw, "source_table", "from_table")
                if source_table and str(_first(source, "all", "every", default="")).lower() in ("true", "1", "yes"):
                    # every record of a list that matches the rule's record (a delivery for each customer on the round's day)
                    action["source"] = {"table": slug(source_table), "all": True}
                    where = _first(raw, "where", "filters", "only_those")
                    if where:
                        action["where"] = [normalize_condition(c) for c in _list(where)]
                    return action
                via = _first(source, "via", "link", "link_field") or _first(raw, "source_via", "via")
                if not source_table or not via:
                    raise SchemaError("create_for_each needs \"source\": {\"table\": the records to go through, \"via\": their link field to the triggering record}")
                action["source"] = _source(source, source_table, via)
                where = _first(raw, "where", "filters", "only_those")
                if where:  # only the billable timesheet entries
                    action["where"] = [normalize_condition(c) for c in _list(where)]
        else:
            target = _first(raw, "target", "record", "on", "of")
            if isinstance(target, str) and slug(target) not in ("record", "this", "self", "trigger", "current", "this_record"):
                action["target"] = target_of(target)  # a link field: update the linked record instead
    elif atype == "notify":
        message = _first(raw, "message", "text", "body", "content", "title")
        if not message:
            raise SchemaError("the notify action needs a message")
        action["message"] = str(message)
        # "from" too: the grammar has "to" before "message", so a recipient written after the message lands there
        to = ".".join(slug(p) for p in str(_first(raw, "to", "who", "recipient", "notify", "from", default="everyone")).split(".") if slug(p)) or "everyone"
        action["to"] = {"all": "everyone", "team": "everyone", "admins": "admin", "owners": "owner", "editors": "editor", "requester": "creator",
                        "submitter": "creator", "created_by": "creator", "raised_by": "creator"}.get(to, to)
    elif atype == "send_email":
        to = _first(raw, "to", "recipient", "email", "address")
        subject = _first(raw, "subject", "title")
        body = _first(raw, "body", "message", "text", "content")
        if not to or not subject or not body:
            raise SchemaError("send_email needs \"to\" (an address or {{record.email}}), a \"subject\" and a \"body\"")
        action.update(to=str(to), subject=str(subject)[:300], body=str(body)[:10000])
        cc = _first(raw, "cc")
        if cc:
            action["cc"] = str(cc)
        attach = _first(raw, "attach", "attachments", "attach_document", "documents")
        if attach:
            action["attach"] = [f"page:{slug(str(a)[5:])}" if str(a).startswith("page:") else slug(a) for a in _list(attach)]
    elif atype == "send_sms":
        to = _first(raw, "to", "phone", "number", "recipient")
        message = _first(raw, "message", "body", "text", "content")
        if not to or not message:
            raise SchemaError("send_sms needs \"to\" (a phone number or {{record.phone}}) and a \"message\"")
        action.update(to=str(to), message=str(message)[:1600])
    elif atype == "generate_document":
        document = _first(raw, "document", "template", "doc", "key")
        if not document:
            raise SchemaError("generate_document needs the \"document\" (a template's key) to produce")
        action["document"] = slug(document)
        save_to = _first(raw, "field", "save_to", "into")
        if save_to:
            action["field"] = slug(save_to)
        target = _first(raw, "target", "record", "of", "for")  # "created": the record an earlier step made; or a link field
        if isinstance(target, str) and slug(target) not in ("record", "this", "self", "trigger", "current", "this_record"):
            action["target"] = target_of(target)
    elif atype == "request_approval":
        several = [_person(a) for a in _names(_first(raw, "approvers", "approved_by", "chain", default=[]))][:10]
        action["approver"] = several[0] if several else _person(_first(raw, "approver", "from", "who", "role", "assignee", default="admin"))
        if len(several) > 1:
            action["approvers"] = list(dict.fromkeys(several))
            mode = slug(_first(raw, "mode", "needs", "require", "approval", default="any"))
            action["mode"] = "all" if mode in ("all", "everyone", "each", "every", "unanimous", "all_of_them", "both") else "any"
        action["message"] = str(_first(raw, "message", "question", "text", "reason", default="Please approve this."))[:1000]
        _follow_up(raw, action)
        apart = _not_by(raw)
        if action["approver"] == "owner" and not action.get("approvers"):  # only the owner answers: kept from whoever raised
            apart = [x for x in apart if x != "creator"]  # it, what the owner adds would wait for ever, and staff can't anyway
        if apart:
            action["not_by"] = apart
        if _yes(_first(raw, "step_up", "reauthenticate", "confirm_sign_in", "second_factor")):
            action["step_up"] = True
        rejected = _first(raw, "if_rejected", "on_reject", "on_rejected", "when_rejected", "rejected", "else", "otherwise")
        if rejected:  # what happens on a no (say why, set it back to Draft); the run then stops
            action["if_rejected"] = [normalize_action(x) for x in _list(rejected)]
    elif atype == "task":
        action["to"] = _person(_first(raw, "to", "assignee", "who", "for", "assign_to", "approver", default="editor"))
        message = _first(raw, "message", "task", "instructions", "text", "title", "description")
        if not message:
            raise SchemaError("a task needs \"message\": what the person is asked to do")
        action["message"] = str(message)[:1000]
        priority = _first(raw, "priority", "urgency", "importance")
        if priority:  # low, normal, high, urgent; or a field of the record that says it (an Urgency choice)
            action["priority"] = slug(str(priority))
        flow = _first(raw, "flow", "with_flow", "done_with", "using")
        if flow:  # the task is done by doing this flow on the record (the stock count it asks for)
            action["flow"] = slug(str(flow))
        fields = _first(raw, "fields", "fill", "inputs", "ask_for", "collect")
        if fields:  # what they fill in to finish it (the tracking number, the inspection result)
            action["fields"] = [slug(f if not isinstance(f, dict) else _first(f, "key", "field", "name"))
                                for f in (_names(fields) if isinstance(fields, str) else _list(fields))][:12]
        _follow_up(raw, action)
        apart = _not_by(raw)
        if apart:  # four eyes: the person who prepared it can't also check it
            action["not_by"] = apart
    elif atype == "run_automation":
        key = _first(raw, "automation", "key", "workflow", "name", "run")
        if not key:
            raise SchemaError("run_automation needs \"automation\": the key of the automation to run")
        action["automation"] = slug(key)
        target = _first(raw, "target", "record", "for", "on")
        if isinstance(target, str) and slug(target) not in ("record", "this", "self", "trigger", "current", "this_record"):
            action["target"] = target_of(target)
    elif atype == "webhook":
        url = str(_first(raw, "url", "endpoint", "address", "path", default="")).strip()
        app = slug(str(_first(raw, "app", "connector", "service", "to_app", default="")))
        if app:  # a connected app (Xero, Shopify, EasyPost…): its address and sign-in are added, the url is its API's path
            action["app"] = app
            url = url if url.startswith("/") or re.match(r"^https?://", url, re.I) else "/" + url
        elif not re.match(r"^https?://", url, re.I) and not url.startswith("{{"):
            raise SchemaError("a webhook needs a \"url\" starting with http:// or https://, or an \"app\" and the path on it")
        hosts = _first(raw, "hosts", "allowed_hosts", "only_hosts", "allow")
        if hosts:  # the only hosts it may reach: a list, or a field holding one per line ({{integration.allowed_hosts}})
            action["hosts"] = str(hosts)[:300] if isinstance(hosts, str) else [str(h).strip().lower()[:200] for h in _list(hosts)][:20]
        elif url.startswith("{{") and not app:
            raise SchemaError("a webhook whose address comes from the record needs \"hosts\": the hosts it may reach, so a record can't "
                              "send it anywhere")
        headers = _first(raw, "headers", "header")
        if isinstance(headers, dict):
            action["headers"] = {str(k)[:100]: str(v)[:2000] for k, v in list(headers.items())[:20]}
        action["url"] = url[:500]
        payload = _first(raw, "payload", "body", "data", "json")
        if isinstance(payload, dict):
            action["payload"] = payload
        method = str(_first(raw, "method", "verb", default="POST")).upper()
        action["method"] = method if method in ("GET", "POST", "PUT", "PATCH") else "POST"
        save = _first(raw, "save", "save_to", "store", "response_to")
        if isinstance(save, dict) and save:  # {"field": "path.in.the.answer"}: keep what the other system answers
            action["save"] = {slug(k): str(v) for k, v in save.items()}
    elif atype == "import_rows":  # a spreadsheet in one of the record's file fields, brought into a list row by row
        source, into = _first(raw, "field", "from", "file", "source"), _first(raw, "table", "list", "into", "to")
        if not source or not into:
            raise SchemaError("import_rows needs \"field\" (the record's file field holding the spreadsheet) and \"table\" (the list its rows go into)")
        action["field"], action["table"] = slug(str(source)), slug(str(into))
        values = _first(raw, "values", "set", "fixed")
        if isinstance(values, dict):
            action["values"] = values
        columns = _first(raw, "columns", "mapping", "map")
        if isinstance(columns, dict):
            action["columns"] = {str(k)[:200]: slug(str(v)) for k, v in list(columns.items())[:200]}
        match = _first(raw, "match_on", "update_by", "match")
        if match:
            action["match_on"] = slug(str(match))
        result = _first(raw, "result", "outcome", "save_result_to")
        if result:
            action["result"] = slug(str(result))
    elif atype == "payment_link":  # a card payment page for the amount (Stripe); paying it sets "paid" on the record
        amount = _first(raw, "amount", "total", "value")
        if not amount:
            raise SchemaError("payment_link needs \"amount\": what's to be paid ({{record.total}})")
        action["amount"] = str(amount)[:200]
        action["description"] = str(_first(raw, "description", "for", "name", "label", default=""))[:200]  # the record's name otherwise
        currency = _first(raw, "currency", "currency_code")
        if currency:
            action["currency"] = str(currency)[:40]
        save = _first(raw, "save_to", "save", "field", "link_field")
        if not save:
            raise SchemaError("payment_link needs \"save_to\": the web address field the payment page goes in")
        action["save_to"] = slug(str(save))
        paid = _first(raw, "paid", "when_paid", "on_paid", "then_set")
        if isinstance(paid, dict) and paid:  # what's set when it's paid ({"status": "Paid"})
            action["paid"] = {slug(k): v for k, v in paid.items()}
    elif atype == "ai_extract":
        source = slug(_first(raw, "from", "source", "file_field", "field", default=""))
        source = "file" if source in ("files", "attachments", "attached_files", "its_files", "record_files") else source
        if not source:
            raise SchemaError("ai_extract needs \"from\": a file field of the record, or \"email\"/\"file\" for what started the automation")
        action["from"] = source
        fields = _first(raw, "fields", "into", "fill")
        if fields:
            action["fields"] = [slug(f) for f in _list(fields)]
        instructions = _first(raw, "instructions", "prompt")
        if instructions:
            action["instructions"] = str(instructions)[:1000]
    elif atype == "ai_create_record":
        table = _first(raw, "table", "into", "in_table")
        if not table:
            raise SchemaError("ai_create_record needs the \"table\" to create the record in")
        action["table"] = slug(table)
        instructions = _first(raw, "instructions", "prompt", "task")
        if instructions:
            action["instructions"] = str(instructions)[:1000]
        save_to = _first(raw, "attach_to", "file_field", "save_files_to")
        if save_to:
            action["attach_to"] = slug(save_to)
    elif atype == "ai_check":
        verdict = _first(raw, "field", "result_field", "verdict_field", "status_field", "into", "result")
        instructions = _first(raw, "instructions", "check", "criteria", "rules", "prompt", "task")
        if not verdict or not instructions:
            raise SchemaError("ai_check needs \"field\": a choice field for its verdict (Pass, Issues) and \"instructions\": what to "
                              "check the record against")
        action.update(field=slug(verdict), instructions=str(instructions)[:2000])
        notes = _first(raw, "notes_field", "findings_field", "details_field", "explain_in", "notes")
        if notes:
            action["notes_field"] = slug(notes)  # what it found, line by line
    elif atype == "ai_fill":
        field = _first(raw, "field", "target_field", "output_field", "into", "key")
        instructions = _first(raw, "instructions", "prompt", "task", "description")
        if not field or not instructions:
            raise SchemaError("the ai_fill action needs the \"field\" to fill and \"instructions\" for the AI")
        action.update(field=slug(field), instructions=str(instructions))
    return action


REPEATS_ARE_SLIPS = ("notify", "send_email", "task", "request_approval")


def normalize_automation(raw):
    if not isinstance(raw, dict):
        raise SchemaError("an automation must be an object")
    name = str(_first(raw, "name", "label", "title", "description", default="")).strip()
    key = slug(_first(raw, "key", "id", default=name or "automation"))
    trigger_raw = _first(raw, "trigger", "when", "on", "event")
    if trigger_raw is None and _first(raw, "trigger_type", "type"):
        trigger_raw = {"type": _first(raw, "trigger_type", "type"), "table": raw.get("table")}
    if isinstance(trigger_raw, dict) and not trigger_raw.get("table") and raw.get("table"):
        trigger_raw = {**trigger_raw, "table": raw["table"]}
    trigger = normalize_trigger(trigger_raw or {})
    conditions = _list(_first(raw, "conditions", "condition", "if", "filters", "where", default=[]))
    if isinstance(trigger_raw, dict):
        conditions = _list(_first(trigger_raw, "conditions", "condition", "filters", "where", default=[])) + conditions
        changed = _first(trigger_raw, "field", "watch_field", "changed_field")
        if changed and trigger["type"] in ("record_updated", "record_created_or_updated"):
            to = _first(trigger_raw, "to", "value", "changed_to")
            conditions.insert(0, {"field": changed, "op": "changed_to" if to is not None else "changed", **({"value": to} if to is not None else {})})
    about = (_BP_TABLES.get() or {}).get(trigger.get("table") or "") or {}
    people = _PERSON_FIELDS.set(frozenset(f["key"] for f in about.get("fields", []) if f.get("type") == "user"))
    try:
        actions = [normalize_action(a) for a in _list(_first(raw, "actions", "then", "do", "steps", default=[]))]
    finally:
        _PERSON_FIELDS.reset(people)
    # the same message, email or task twice in a row is a slip: everyone told twice
    actions = [a for i, a in enumerate(actions) if not (i and a["type"] in REPEATS_ARE_SLIPS and a == actions[i - 1])]
    if not actions:
        raise SchemaError(f"the automation “{name or key}” needs at least one action")
    out = {"key": key, "name": name or label_of(key), "enabled": raw.get("enabled", True) not in (False, "false", 0),
           "trigger": trigger, "conditions": [normalize_condition(c) for c in conditions], "actions": actions}
    if raw.get("feature"):  # made by a feature of the Keel language (app/lang): changed and removed with it
        out["feature"] = slug(raw["feature"])
    return out


# --- checking a whole blueprint -------------------------------------------------------------------------------------

def empty(name="My ERP"):  # "portals" are added by set_portal
    return {"name": name, "modules": [], "tables": [], "pages": [], "automations": []}


def table(bp, key):
    return next((t for t in bp.get("tables", []) if t["key"] == key), None)


def backlink(tables, t, name):
    """The records of another list that link to a record of t, named in a formula as that list (a client's invoices:
    client.invoices), or as that list and its link when it links to t more than once (invoices_by_billed_to): (that
    list, its link field), or None. A field of t by that name comes first; the caller looks there."""
    if not t or not name:
        return None
    said = str(name).strip().lower()
    found = []
    for other in (tables.values() if isinstance(tables, dict) else tables or []):
        links = [f for f in other.get("fields") or [] if f.get("type") == "link" and f.get("table") == t.get("key")]
        for f in links:
            if said == f"{other['key']}_by_{f['key']}".lower():
                return other, f
        if links and said in {other["key"].lower(), lc(other.get("label") or "").replace(" ", "_")}:
            found.append((other, links[0]))
    return found[0] if found else None


def list_named(tables, name):
    """A whole list named in a formula (bookings: all of them), or None."""
    said = str(name or "").strip().lower()
    for t in (tables.values() if isinstance(tables, dict) else tables or []):
        if said and said in {t["key"].lower(), lc(t.get("label") or "").replace(" ", "_")}:
            return t
    return None


def condition_leaves(conditions):
    """Every plain condition of a list ({"field", "op", "value"}), those inside "any" and "all" groups too. A formula
    ({"formula": …}) isn't one: formulas() gives those."""
    for c in conditions or []:
        if "any" in c or "all" in c:
            yield from condition_leaves(c.get("any") or c.get("all"))
        elif "formula" not in c:
            yield c


def formulas(conditions):
    """The formulas among conditions ({"formula": "count(client.invoices where …) == 0"}), in groups too."""
    for c in conditions or []:
        if "any" in c or "all" in c:
            yield from formulas(c.get("any") or c.get("all"))
        elif "formula" in c:
            yield c["formula"]


def step_at(actions, at):
    """The step at a place in an automation: "2", or "3.1.0" (a branch's second way), "3.o.0" (its otherwise), "3.r.0"
    (an approval's "if rejected")."""
    parts, steps, step = str(at).split("."), actions, None
    try:
        step = steps[int(parts[0])]
        for way, n in zip(parts[1::2], parts[2::2]):
            steps = step["otherwise"] if way == "o" else step["if_rejected"] if way == "r" else step["paths"][int(way)]["steps"]
            step = steps[int(n)]
    except (IndexError, KeyError, ValueError, TypeError):
        return None
    return step


def stages_set_by_rules(bp, t):
    """The stages automations, and flows' buttons, put a table's records at (lower case)."""
    key = (t.get("workflow") or {}).get("field") or "status"
    out = {str(o["value"]).strip().lower() for f in flows(bp) if f["table"] == t["key"]
           for s in f["steps"] if s["kind"] == "decide" and s.get("field") == key for o in s["options"]}
    tables = {x["key"]: x for x in bp.get("tables", [])}
    for a in bp.get("automations", []):
        for act in steps_of(a["actions"]):
            mine = (act["type"] == "update_record" and a["trigger"].get("table") == t["key"] and not act.get("target")) or \
                (act["type"] in ("create_record", "create_for_each", "update_each") and act.get("table") == t["key"]) or \
                (act["type"] == "update_record" and act.get("target") and _targets(tables, a["trigger"].get("table"), act["target"], t["key"]))
            value = (act.get("values") or {}).get(key)
            if mine and isinstance(value, str) and "{{" in value:  # set from what came in (a DocuSign event's status): any of them
                out |= {x.lower() for x in stages(t)}
            elif mine and isinstance(value, str):
                out.add(value.strip().lower())
    return out


def _targets(tables, trigger_table, target, table):
    """Whether a step that changes a linked record (its "target": a link of the rule's record, or "created") may change a
    record of `table`."""
    if target == "created" or trigger_table not in tables:
        return True  # what was made isn't known here: counted, to be safe
    here = tables[trigger_table]
    for part in str(target).split("."):
        f = field(here, part)
        if f is None or f["type"] != "link":
            return True
        here = tables.get(f["table"]) or {}
    return here.get("key") == table


def complete_moves(bp, t):
    """The stage buttons a list's workflow is missing, so no stage is out of reach and none with stages after it is a
    dead end (unless a rule moves records there or on): [move]. Each is a button to the stage after, in order; a stage
    that stops things (Cancelled) can be got to from any stage still going."""
    flow = t.get("workflow")
    names = stages(t) if flow else []
    if not flow or len(names) < 2:
        return []
    moves, added = list(flow.get("transitions") or []), []
    by_rules = stages_set_by_rules(bp, t)
    start = start_stage(t) or names[0]
    going = [x for x in names if not says_no(x)]

    def reached():
        got, grew = {start.lower()} | by_rules, True
        while grew:
            grew = False
            for mv in moves + added:
                if (not mv.get("from") or {x.lower() for x in mv["from"]} & got) and mv["to"].lower() not in got:
                    got.add(mv["to"].lower())
                    grew = True
        return got
    for i, stage in enumerate(names):
        if stage.lower() in reached():
            continue
        if says_no(stage):
            frm = [x for x in going if x.lower() in reached()][:-1] or [start]
        else:
            prev = next((x for x in reversed(names[:i]) if not says_no(x) and x.lower() in reached()), None)
            frm = [prev] if prev else []
        if frm:
            added.append({"from": frm, "to": stage, "label": f"Move to {stage}"[:60]})
    for i, stage in enumerate(going[:-1]):
        later = going[i + 1:]
        if any(not says_no(mv["to"]) and mv["to"] != stage and (not mv.get("from") or stage in mv["from"]) for mv in moves + added) \
                or {x.lower() for x in later} & by_rules or ENDED.search(stage):
            continue
        added.append({"from": [stage], "to": later[0], "label": f"Move to {later[0]}"[:60]})
    return added


def steps_of(actions):
    """Every step of an automation in order, those on a branch's paths and an approval's "if rejected" too."""
    for act in actions or []:
        yield act
        for path in act.get("paths") or []:
            yield from steps_of(path["steps"])
        yield from steps_of(act.get("otherwise"))
        yield from steps_of(act.get("if_rejected"))


HOPS = 3  # links a lookup, a rollup or a document's list follows at most (a rent payment → lease → property → owner)


def field(t, key):
    return next((f for f in (t or {}).get("fields", []) if f["key"] == key), None)


CODE_KEYS = ("code", "sku", "ref", "reference", "abn", "number", "no", "id_code")
NAME_KEYS = ("title", "name", "subject", "summary", "trading_name", "full_name", "item_name", "product_name")


def is_code(key):
    """A field that holds a code people look up, not what they call it (a category's 40141700, a cost centre's CC100)."""
    return bool(key) and (key in CODE_KEYS or key.endswith(("_code", "_number", "_no", "_ref")))


def name_field(t, title_key=None):
    """The field a record is called by beside its number or code (a request's title, a category's name), or None."""
    keys = {f["key"]: f for f in t["fields"] if f["type"] == "text" and f["key"] != title_key}
    return next((k for k in NAME_KEYS if k in keys), None) or next((k for k in keys if k.endswith(("_name", "_title"))), None)


def default_columns(t, most=7):
    """A list's columns when nobody chose them: what each record is called, where it is (its stage), then the rest in
    order (never long text, files or what's kept secret)."""
    shown = [f["key"] for f in t["fields"] if f["type"] not in ("long_text", "rich_text", "file", "image", "signature")
             and not f.get("sensitive")]
    first = [k for k in (title_field(t), (t.get("workflow") or {}).get("field")) if k in shown]
    return (list(dict.fromkeys(first)) + [k for k in shown if k not in first])[:most]


def title_field(t):
    kept = field(t, t["title_field"]) if t.get("title_field") else None
    if kept is not None and not (kept["type"] == "text" and kept.get("default") not in (None, "")):
        return t["title_field"]
    for f in t["fields"]:  # never one most records share (a currency that starts as AUD): it tells them apart from nothing
        if f["type"] in ("text", "autonumber", "email") and f.get("default") in (None, ""):
            return f["key"]
    person = next((f for f in t["fields"] if f["type"] == "user" and not f.get("multiple")), None)
    if person is not None:  # a limit, a panel seat, a score: whose it is
        return person["key"]
    return t["fields"][0]["key"] if t["fields"] else None


def _check_expression(source, allowed, where, problems, record_fields=None, tables=None, t=None):
    """tables, t: the design's lists and the one the expression is on, so it can name the records linking to its own
    (a client's invoices) and whole lists (bookings), as count(...), sum(...) and any(...) look at them."""
    try:
        names = expr.outer_names(source)
    except expr.ExprError as exc:
        problems.append(f"{where}: {exc}")
        return
    names = {n for n in names if not (tables and ((t and backlink(tables, t, n)) or list_named(tables, n)))} - {"this"}
    unknown = sorted(n for n in names if n not in allowed and n not in ("today", "now", "True", "False", "None"))
    if unknown:
        problems.append(f"{where}: “{', '.join(unknown)}” isn't a field here (fields: {', '.join(sorted(a for a in allowed if a != 'record')) or 'none'})")
    calls = expr.unknown_calls(source)
    if calls:  # it would fail every time it's worked out, and a rule that can't be worked out holds nothing back
        problems.append(f"{where}: there's no function {', '.join(f'{c}()' for c in calls)} (there are: "
                        f"{', '.join(sorted((expr.KNOWN - set(expr.ALIASES)) | {'today', 'now'}))})")
    if record_fields is not None:
        import ast
        for node in ast.walk(expr.parse(source)):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ("record", "this") \
                    and node.attr not in record_fields and node.attr not in ("id", "title", "created_at", "updated_at", "created_by"):
                problems.append(f"{where}: record.{node.attr} isn't a field of this table")


def links_on(t, path, tables, depth=0):
    """The link fields a dotted path from t goes through, each to one record, or None if it isn't such a path. A lookup
    of a link on the way is read as the path behind it (rent payments' "owner" = "lease.property.owner")."""
    out = []
    for part in path.split("."):
        f = field(t, part)
        if f and f["type"] == "lookup" and f.get("link") and depth < HOPS:
            behind = links_on(t, f"{f['link']}.{f['field']}", tables, depth + 1)
            if not behind:
                return None
            out += behind
        elif f and f["type"] == "link" and not f.get("multiple"):
            out.append(f)
        else:
            return None
        t = tables.get(out[-1]["table"])
        if t is None:
            return None
    return out if len(out) <= HOPS else None


def lookup_source(t, f, tables):
    """The field a lookup of t shows: its linked record's field, or the field at the end of a path through that record's
    links (a rent payment's lease → "property.owner"), or the field of the record it finds by its conditions."""
    if "where" in f:
        end = tables.get(f.get("table"))
        if end is None:
            return None
    else:
        link = field(t, f["link"])
        if not link or link["type"] != "link" or link["table"] not in tables:
            return None
        end = tables[link["table"]]
    through, _, key = f["field"].rpartition(".")
    if through:
        on = links_on(end, through, tables, depth=1)
        if not on or len(on) >= HOPS:
            return None
        end = tables.get(on[-1]["table"])
    return field(end, key)


def as_fraction(value):
    """A percentage as it's kept (15% is 0.15) from how people and the AI write it: "10", 10, "10%" → 0.1; 0.1 stays."""
    if isinstance(value, bool) or value in (None, ""):
        return value
    text = str(value).strip()
    n = expr.to_number(text.rstrip("%"))
    if n is None:
        return value
    return n / 100 if text.endswith("%") or abs(n) > 1 else n


def _is_percent(f):
    return f is not None and (f["type"] == "percent" or (f["type"] == "formula" and f.get("format") == "percent"))


def _percent_terms(t, tables):
    """Regexes for what reads as a percentage in a formula on t: its own percentage fields, one reached through its links
    (criterion.weight), and a total of a list's percentages (sum(criteria, weight))."""
    out = [rf"\b(?:record\.|this\.)?{re.escape(f['key'])}\b(?!\s*[.(])" for f in t["fields"] if _is_percent(f)]
    if not tables:
        return out
    ways = [([f["key"]], tables.get(f.get("table"))) for f in t["fields"] if f["type"] == "link" and f.get("table") in tables]
    for _ in range(3):  # through its links, up to three on: criterion.weight, invoice.buyer.price_tolerance_percent
        further = []
        for path, target in ways:
            for g in (target or {}).get("fields", []):
                if _is_percent(g):
                    out.append(r"\b(?:record\.|this\.)?" + r"\.".join(re.escape(k) for k in path + [g["key"]]) + r"\b(?!\s*[.(])")
                elif g["type"] == "link" and g.get("table") in tables:
                    further.append((path + [g["key"]], tables[g["table"]]))
        ways = further
    for other in tables.values():  # a list of records linking here: sum(criteria, weight), avg(...), min(...), max(...)
        if not any(g["type"] == "link" and g.get("table") == t["key"] for g in other.get("fields", [])):
            continue
        for g in other["fields"]:
            if _is_percent(g):
                out.append(rf"\b(?:sum|avg|average|min|max)\(\s*{re.escape(other['key'])}\b(?:\s+where\b[^,()]*(?:\([^()]*\)[^,()]*)*)?,"
                           rf"\s*{re.escape(g['key'])}\s*\)")
    return out


def percents_fixed(t, source, tables=None):
    """A formula with its percentages used as they're kept: "subtotal * discount_percent / 100" is
    "subtotal * discount_percent" (15% is already 0.15), and "discount_percent > 10" is "discount_percent > 0.1". With
    `tables`, the same for a linked record's percentage (criterion.weight > 50) and a list's total of them
    (sum(criteria, weight) == 100 is all of it: 1)."""
    if not t or not isinstance(source, str):
        return source
    key = (id(t), id(tables), source)
    kept = _PERCENTS_FIXED.get(key)
    if kept is not None and kept[0] is t and kept[1] is tables:  # (asked for every record a condition is checked on)
        return kept[2]
    out = source
    for term in _percent_terms(t, tables):
        out = re.sub(rf"({term})\s*/\s*100(?:\.0*)?(?![\d.%])", r"\1", out)
        out = re.sub(rf"\(\s*({term})\s*/\s*100(?:\.0*)?\s*\)", r"\1", out)
        # compared on its own (never a product or a sum it's part of: "price * discount_percent > 5" is $5)
        alone = r"(?<![*/+\-]\s)(?<![*/+\-])"
        out = re.sub(rf"{alone}(({term})\s*(?:>=|<=|==|!=|>|<)\s*)(\d+(?:\.\d+)?)(?![\d.%])(?!\s*[*/+\-])",
                     lambda m: m.group(1) + (f"{float(m.group(3)) / 100:g}" if float(m.group(3)) > 1 else m.group(3)), out)
        out = re.sub(rf"(?<![\w.*/+\-])(?<![*/+\-]\s)(\d+(?:\.\d+)?)(\s*(?:>=|<=|==|!=|>|<)\s*(?:{term}))(?!\s*[*/+\-])",
                     lambda m: (f"{float(m.group(1)) / 100:g}" if float(m.group(1)) > 1 else m.group(1)) + m.group(2), out)
    if len(_PERCENTS_FIXED) > 20000:
        _PERCENTS_FIXED.clear()
    _PERCENTS_FIXED[key] = (t, tables, out)
    return out


_PERCENTS_FIXED = {}


PRICE_WORDS = ("price", "rate", "cost", "fee", "charge")
NAME_WORDS = ("description", "item_name", "name", "title", "details", "product_name")


def fill_from(t, link, target):
    """What picking a record in a link fills in beside it, {field of t: field of the picked record}: fields of the same
    name (unit, GST code), its name into a description, its price into the line's price (a menu item on a quote line)."""
    if not t or not target or link["type"] != "link":
        return {}
    out = {}
    theirs = {f["key"]: f for f in target["fields"] if f["type"] in STORED and f["type"] not in ("autonumber",) and not f.get("sensitive")}
    title = title_field(target)
    for f in t["fields"]:
        if f["key"] == link["key"] or f["type"] not in STORED or f["type"] in ("autonumber", "link", "user"):
            continue
        if f["key"] in theirs and theirs[f["key"]]["type"] == f["type"]:
            out[f["key"]] = f["key"]
        elif any(w == f["key"] or f["key"].endswith("_" + w) for w in NAME_WORDS) and f["type"] in ("text", "long_text") and title in theirs:
            out[f["key"]] = title
        elif f["type"] == "money" and any(w in f["key"] for w in PRICE_WORDS):
            price = next((k for k, g in theirs.items() if g["type"] == "money" and any(w in k for w in PRICE_WORDS)), None)
            if price:
                out[f["key"]] = price
    return out


CODE_NAME = re.compile(r"(?<![\w@./:{-])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![\w@/}-])")


def named(bp, text):
    """Text with each list's or detail's code name (material_shortage) as its name (material shortage), for people."""
    names = {f["key"]: f["label"] for t in bp.get("tables", []) for f in t["fields"]} | {t["key"]: t["label"] for t in bp.get("tables", [])}
    return CODE_NAME.sub(lambda m: lc(names[m.group(0)]) if m.group(0) in names else m.group(0).replace("_", " "), text or "")


def today_words(day):
    """Today as the builder is told it: the weekday, this week's Monday to Sunday and the month, so worked examples about
    "this week" or "30 days later" start from the right days."""
    from datetime import timedelta
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    return (f"{day:%A} {day.day} {day:%B %Y} ({day.isoformat()}). This week: Monday {monday.day} {monday:%B} to Sunday "
            f"{sunday.day} {sunday:%B}. This month: {day:%B %Y}")


def converts(bp):
    """Whether the app turns amounts from other currencies into its own (convert()): it needs exchange rates set."""
    return any("convert(" in str(f.get("expression") or "") for t in bp.get("tables", []) for f in t["fields"]) \
        or any("convert(" in str(act.get("values") or "") for a in bp.get("automations", []) for act in steps_of(a["actions"]))


def person_path(t, who, tables):
    """Whether `who` names people on a record of t: a person field, a lookup of one, or a path through its links that
    ends at one (the cost centre's manager: "cost_centre.manager")."""
    if not t or not who:
        return False
    current = t
    parts = who.split(".")
    for i, part in enumerate(parts[:HOPS + 1]):
        f = field(current, part)
        if f is None:
            return False
        if i == len(parts) - 1:
            return f["type"] == "user" or f["type"] == "lookup" and (lookup_source(current, f, tables) or {}).get("type") == "user"
        if f["type"] != "link" or f["table"] not in tables:
            return False
        current = tables[f["table"]]
    return False


def link_path(t, path, tables):
    """The links a step's "target" goes through from t: one link field, or a path of them (a payment's
    "invoice.couple": its invoice's couple). None when a part isn't a link."""
    out, current = [], t
    for part in str(path or "").split(".") if path else []:
        f = field(current, part) if current else None
        if not f or f["type"] != "link" or f["table"] not in tables:
            return None
        out.append(f)
        current = tables[f["table"]]
    return out or None


def target_of(raw):
    """A step's "target" as written: a link field's key, a path of them ("invoice.couple"), or "created"."""
    text = re.sub(r"^\{\{\s*|\s*\}\}$", "", str(raw).strip())
    parts = [slug(x) for x in re.sub(r"^(record|this|trigger)\.", "", text, flags=re.I).split(".") if slug(x)]
    return ".".join(parts)


def _path_field(t, path, tables):
    """The field at the end of a dotted path from t through single links (a bid's "tender.closes_at"), or None."""
    current, parts = t, path.split(".")
    for part in parts[:-1]:
        f = field(current, part)
        if not f or f["type"] != "link" or f["table"] not in tables:
            return None
        current = tables[f["table"]]
    return field(current, parts[-1])


_TEAM_KEYS = contextvars.ContextVar("team_keys", default=frozenset())
_PERSON_FIELDS = contextvars.ContextVar("person_fields", default=frozenset())  # the record's person fields, while its rule or moves are read
_BP_TABLES = contextvars.ContextVar("bp_tables", default=None)  # {key: table} of the design being changed
_FLOW_KEYS = contextvars.ContextVar("flow_keys", default={})  # the design's flows while it's checked (a task done through one)


def who_fits(t, who, tables, teams_=None):
    """Whether a step, move or check can be for `who`: a role, the record's creator, a team, the outside person the record
    is for (its portal), or people named on the record."""
    teams_ = _TEAM_KEYS.get() if teams_ is None else teams_
    return who in APPROVER_ROLES or who in teams_ or bool(t) and (who == "creator" or who == "portal" and bool(t.get("portal"))
                                                                 or person_path(t, who, tables))


def _who_words(t):
    teams_ = sorted(_TEAM_KEYS.get())
    return ("owner, admin, editor, creator" + (", a team (" + ", ".join(teams_) + ")" if teams_ else "")
            + (", portal" if t and t.get("portal") else "") + (f" or a person field of {t['label']}" if t else ""))


def reach(t, source, via, tables):
    """How records of `source` reach a record of t, for a rollup or a document's list: (path, anchor), or None. The path
    is their link ("order"), or a path through their links ("property.owner"), a lookup of a link read as the path
    behind it (rent payments' "owner" = "lease.property.owner"). It reaches the record itself (anchor None) or, when it
    ends at a table t links to once, the record t links to there: a statement's owner (anchor "owner")."""
    links = links_on(source, via, tables) if via else None
    if not links:
        return None
    path, end = ".".join(f["key"] for f in links), links[-1]["table"]
    if end == t["key"]:
        return path, None
    anchors = [f["key"] for f in t["fields"] if f["type"] == "link" and f["table"] == end and not f.get("multiple")]
    return (path, anchors[0]) if len(anchors) == 1 else None


def _check_conditions(conditions, t, where, problems, tables):
    keys = {f["key"] for f in t["fields"]} | {"created_at", "updated_at", "created_by"}
    for source in formulas(conditions):
        _check_expression(source, keys | {"record", "id"}, where, problems, tables=tables, t=t)
    for c in condition_leaves(conditions):
        head, _, rest = c["field"].partition(".")
        if rest:  # through a link: an invoice's customer.account_manager
            link = field(t, head)
            linked = tables.get(link["table"]) if link and link["type"] == "link" else None
            if linked is None:
                problems.append(f"{where}: the condition checks “{c['field']}”, but “{head}” isn't a link field of {t['label']}")
            elif not field(linked, rest) and rest not in ("created_at", "updated_at", "created_by"):
                problems.append(f"{where}: the condition checks “{c['field']}”, but “{rest}” isn't a field of {linked['label']}")
        elif c["field"] not in keys:
            problems.append(f"{where}: the condition checks “{c['field']}”, which isn't a field of {t['label']}")


CONTEXT = {"record", "created", "item", "email", "file", "payload", "today", "now", "approver", "reason", "owner"}  # names automation values can use besides fields ("approver": who said yes; "reason": what they wrote answering)


_MORE_CONTEXT = contextvars.ContextVar("more_context", default=frozenset())  # {{flow.…}}, while a flow's "then" is checked
# the design's lists while a rule is checked: its values and messages can look at a whole list, as formulas do
# (first(people where login == record.created_by).default_site)
_TABLES = contextvars.ContextVar("tables", default=None)


def _check_template(text, where, problems, context_fields):
    for m in expr.TEMPLATE.finditer(text or ""):
        _check_expression(m.group(1), set(context_fields) | CONTEXT | _MORE_CONTEXT.get(), where, problems, record_fields=set(context_fields),
                          tables=_TABLES.get())


def _check_values(values, target, where, problems, context_fields):
    if target is None:
        return  # the step's table is missing: that's reported where the table is named
    keys = {f["key"]: f for f in target["fields"]}
    for k, v in values.items():
        f = keys.get(k)
        if f is None:
            problems.append(f"{where}: {target['label']} has no field “{k}” (fields: {', '.join(keys)})")
        elif f["type"] in CALCULATED:
            problems.append(f"{where}: “{k}” is worked out automatically and can't be set")
        elif f["type"] in FILES:
            problems.append(f"{where}: “{k}” holds files; files are added by generate_document, ai_create_record or by people")
        elif f["type"] == "user" and isinstance(v, str) and "{{" not in v and (v in WHO_WORDS or re.fullmatch(r"[a-z][a-z0-9_]*", v)):
            problems.append(f"{where}: {f['label']} holds a person, so it can't be set to “{v}” (a team or role isn't one person): "
                            f"use {{{{approver}}}} (who approved it) or a field that holds a person, or leave it out")
        if isinstance(v, str) and "{{" in v:
            _check_template(v, where, problems, context_fields)


def _check_table(t, tables, modules, problems):
    keys = {f["key"]: f for f in t["fields"]}
    if not t["fields"]:
        problems.append(f"the table {t['label']} has no fields: define each one in its \"fields\" (key, label, type and the type's "
                        f"settings); sections and views only arrange fields defined there")
    if t.get("module") and t["module"] not in modules:
        problems.append(f"the table {t['label']} is in module “{t['module']}”, which doesn't exist")
    if t.get("embedded") and not any(f["type"] == "link" for f in t["fields"]):
        problems.append(f"{t['label']} is shown inside another table's records, so it needs a link field to that table")
    for n, arm in enumerate(t.get("combines") or [], start=1):
        where = f"{t['label']} (put together from list {n})"
        source = tables.get(arm["table"])
        if source is None or arm["table"] == t["key"]:
            problems.append(f"{where}: “{arm['table']}” isn't another table")
            continue
        if source.get("combines"):
            problems.append(f"{where}: {source['label']} is put together itself: name the lists it comes from instead")
        _check_values(arm["values"], t, where, problems, [f["key"] for f in source["fields"]])
        _check_conditions(arm.get("where"), source, where, problems, tables)
        missing = [f["label"] for f in t["fields"] if f.get("required") and f["key"] not in arm["values"]]
        if missing:
            problems.append(f"{where}: it must fill in {', '.join(missing)}, which must be filled in on {t['label']} (or make them optional)")
    if t.get("combines") and (t.get("workflow") or t.get("public_form") or t.get("embedded")):
        problems.append(f"{t['label']} is put together from other lists, so people don't add to it: it can't have stages, a public form "
                        f"or be lines of another record")
    for f in t["fields"]:
        where = f"{t['label']} › {f['label']}"
        if f["type"] == "url" and f.get("template"):
            if not f["template"].startswith(("https://", "http://")):
                problems.append(f"{where}: its link must start with https:// (a web address), not “{f['template'][:40]}”")
            _check_template(f["template"], where, problems, list(keys))
        if f["type"] == "link" and f["table"] not in tables:
            problems.append(f"{where} links to “{f['table']}”, which isn't a table (tables: {', '.join(tables)})")
        elif f["type"] == "link" and f.get("match"):  # only a tenant of this request's property: both sides must be there
            other = tables[f["table"]]
            for theirs, ours in f["match"].items():
                if not field(other, theirs):
                    problems.append(f"{where} only offers {other['label'].lower()} whose “{theirs}” matches, but {other['label']} has no "
                                    f"field “{theirs}” (its fields: {', '.join(x['key'] for x in other['fields'])}), so it refuses every one: "
                                    f"match on a field it has, or leave \"match\" out")
                elif ours not in keys:
                    problems.append(f"{where} only offers {other['label'].lower()} matching this record's “{ours}”, which isn't one of its fields")
        if f["type"] == "formula":
            _check_expression(f["expression"], set(keys) | {"record", "id", "created_at", "updated_at", "created_by"}, where, problems, tables=tables, t=t)
            if f["key"] in _own_names(f["expression"]):  # its own name inside sum(…, amount) is the summed records' field
                problems.append(f"{where}: a formula can't use itself")
        if f["type"] == "lookup" and "where" in f:
            source = tables.get(f["table"])
            if not source:
                problems.append(f"{where} finds a record in “{f['table']}”, which isn't a table")
                continue
            if not f["field"]:
                problems.append(f"{where}: the lookup needs \"field\": which field of the {source['label']} it finds to show "
                                f"({', '.join(x['key'] for x in source['fields'][:12])})")
            elif not lookup_source(t, f, tables):
                problems.append(f"{where}: {source['label']} has no field “{f['field']}”")
            _check_conditions(f["where"], source, where, problems, tables)
            if f.get("sort") and not field(source, f["sort"]["field"]) and f["sort"]["field"] not in ("created_at", "updated_at"):
                problems.append(f"{where} picks by “{f['sort']['field']}”, which isn't a field of {source['label']}")
        elif f["type"] == "lookup":
            link = keys.get(f["link"])
            if link and link["type"] == "link" and not f["field"]:
                shown = [x["key"] for x in (tables.get(link["table"]) or {}).get("fields", [])]
                problems.append(f"{where}: the lookup needs \"field\": which field of {link['label']} to show" + (f" ({', '.join(shown[:12])})" if shown else ""))
            elif not link or link["type"] != "link":
                links = [x["key"] for x in t["fields"] if x["type"] == "link"]
                problems.append(f"{where}: " + (f"“{f['link']}” must be" if f["link"] else "the lookup needs \"link\":")
                                + f" a link field of {t['label']}" + (f" ({', '.join(links)})" if links else ""))
            elif link["table"] in tables and not lookup_source(t, f, tables):
                problems.append(f"{where}: {tables[link['table']]['label']} has no field “{f['field']}”"
                                + (f" (a path goes through links to one record each, {HOPS} links at most)" if "." in f["field"] else ""))
        if f["type"] == "rollup":
            source = tables.get(f["table"])
            if not source:
                problems.append(f"{where} totals “{f['table']}”, which isn't a table")
                continue
            if not f["via"] and not f.get("filters"):
                problems.append(f"{where}: a total over all of {source['label']} needs \"where\": conditions on {{{{record.x}}}} saying which")
            elif f["via"] and not reach(t, source, f["via"], tables):
                links = [x["key"] for x in source["fields"] if x["type"] == "link" and x["table"] == t["key"]]
                problems.append(f"{where}: “{f['via']}” must be the field of {source['label']} that links to {t['label']}"
                                + (f" (that's “{links[0]}”)" if links else f"; {source['label']} has no link to {t['label']} yet")
                                + ", or a path through their links (“property.owner”) to it or to the record it links to")
            if f.get("field"):
                of = field(source, f["field"])
                if not of:
                    problems.append(f"{where}: {source['label']} has no field “{f['field']}”")
                elif f["aggregate"] in ("sum", "avg") and of["type"] not in NUMERIC | {"duration", "rating", "rollup"} \
                        and of.get("format") not in NUMERIC:
                    problems.append(f"{where}: can't {f['aggregate']} “{of['label']}” because it isn't a number")
            _check_conditions(f.get("filters"), source, where, problems, tables)
        for kind in ("show_if", "hide_if", "readonly_if"):
            _check_conditions(f.get(kind), t, where, problems, tables)
        if f.get("options_by"):
            parent = field(t, f["options_by"]["field"])
            if not parent or parent["key"] == f["key"] or parent["type"] not in ("select", "text", "link", "checkbox"):
                problems.append(f"{where}: its choices depend on “{f['options_by']['field']}”, which must be another choice or text field of {t['label']}")
            elif parent["type"] == "select":
                unknown = [k for k in f["options_by"]["options"] if k not in parent["options"]]
                if unknown:
                    problems.append(f"{where}: its choices are given for {', '.join(unknown)}, which aren't options of {parent['label']} ({', '.join(parent['options'])})")
        for source in (f.get("ai") or {}).get("from", []):
            if source not in keys:
                problems.append(f"{where}: the AI reads “{source}”, which isn't a field of {t['label']}")
    for section in t.get("sections", []):
        missing = [k for k in section["fields"] if k not in keys]
        if missing:
            problems.append(f"the section {t['label']} › {section['label']} lists {', '.join(missing)}, which aren't fields")
        _check_conditions(section.get("show_if"), t, f"the section {t['label']} › {section['label']}", problems, tables)
        lines = tables.get(section.get("lines") or "")
        if section.get("lines") and (lines is None or not lines.get("embedded")
                                     or not any(f["type"] == "link" and f.get("table") == t["key"] for f in lines["fields"])):
            problems.append(f"the section {t['label']} › {section['label']} is the lines “{section['lines']}”, which aren't line items of {t['label']}")
    for v in t.get("views", []):
        where = f"the view {t['label']} › {v['label']}"
        needs = {"board": [("group_by", ("select",))], "calendar": [("date_field", ("date", "datetime")), ("end_field", ("date", "datetime"))],
                 "timeline": [("start_field", ("date", "datetime")), ("end_field", ("date", "datetime"))], "gallery": [("image_field", ("image",))]}
        for setting, kinds in needs.get(v["type"], []):
            if not v.get(setting):
                continue
            f = keys.get(v[setting])
            if not f or (f["type"] not in kinds and f.get("format") not in kinds):
                fitting = [x["key"] for x in t["fields"] if x["type"] in kinds or x.get("format") in kinds]
                problems.append(f"{where}: “{v[setting]}” must be a {' or '.join(kinds)} field" + (f" ({', '.join(fitting)})" if fitting else f"; {t['label']} has none"))
        if v["type"] == "map" and (keys.get(v["location_field"]) or {}).get("type") != "location":
            places = [x["key"] for x in t["fields"] if x["type"] == "location"]
            problems.append(f"{where}: “{v['location_field']}” must be a location field" + (f" ({', '.join(places)})" if places else f"; {t['label']} has none"))
        if v["type"] == "tree":
            parent = keys.get(v["parent_field"])
            if not parent or parent["type"] != "link" or parent["table"] != t["key"]:
                problems.append(f"{where}: “{v['parent_field']}” must be a link from {t['label']} to {t['label']} (each record's parent)")
        if v.get("depends_field"):
            dep = keys.get(v["depends_field"])
            if not dep or dep["type"] != "link" or dep["table"] != t["key"]:
                problems.append(f"{where}: “{v['depends_field']}” must be a link from {t['label']} to {t['label']} (what each depends on)")
        if v["type"] in ("list", "timeline") and v.get("group_by") and v["group_by"] not in keys:
            problems.append(f"{where} groups by “{v['group_by']}”, which isn't a field")
        if v.get("color_by") and v["color_by"] not in keys:
            problems.append(f"{where} colours by “{v['color_by']}”, which isn't a field")
        if v.get("sort") and v["sort"]["field"] not in keys and v["sort"]["field"] not in ("created_at", "updated_at"):
            problems.append(f"{where} sorts by “{v['sort']['field']}”, which isn't a field")
        _check_conditions(v.get("filters"), t, where, problems, tables)
    for doc in t.get("documents", []):
        where = f"the document {t['label']} › {doc['label']}"
        if doc.get("format") == "text":
            for part in ("header", "line", "footer"):
                try:
                    expr.TEMPLATE.sub("", doc.get(part) or "")
                    for m in expr.TEMPLATE.finditer(doc.get(part) or ""):
                        expr.parse(m.group(1))
                except expr.ExprError as exc:
                    problems.append(f"{where}: its {part} can't be read: {exc}")
            if doc.get("lines") and doc["lines"]["table"] not in tables:
                problems.append(f"{where} lists “{doc['lines']['table']}”, which isn't a table")
            elif doc.get("lines") and not reach(t, tables[doc["lines"]["table"]], doc["lines"]["via"], tables):
                problems.append(f"{where}: “{doc['lines']['via']}” must be the link of {tables[doc['lines']['table']]['label']} to {t['label']}")
            continue
        if doc.get("format") == "label":
            for k in doc.get("fields", []) + [doc[x] for x in ("title_field", "code_field", "copies_field") if doc.get(x)]:
                if k not in keys:
                    problems.append(f"{where} shows “{k}”, which isn't a field of {t['label']}")
            if doc.get("copies_field") in keys and keys[doc["copies_field"]]["type"] not in ("number", "formula", "rollup", "lookup"):
                problems.append(f"{where}: how many copies (“{doc['copies_field']}”) must be a number")
            continue
        for k in doc.get("fields", []) + doc.get("details", []) + doc.get("totals", []) + ([doc["number_field"]] if doc.get("number_field") else []):
            if k not in keys:
                problems.append(f"{where} shows “{k}”, which isn't a field of {t['label']}")
        to = doc.get("to")
        if to and to["field"] == "record":  # a statement to the customer or owner it's about
            for k in to.get("details", []):
                if k not in keys:
                    problems.append(f"{where}: {t['label']} has no field “{k}”")
        elif to:
            target = keys.get(to["field"])
            if not target:
                problems.append(f"{where} is addressed to “{to['field']}”, which isn't a field")
            elif to.get("details"):
                if target["type"] != "link" or target["table"] not in tables:
                    problems.append(f"{where}: details of the recipient need “{to['field']}” to be a link field")
                else:
                    for k in to["details"]:
                        if not field(tables[target["table"]], k):
                            problems.append(f"{where}: {tables[target['table']]['label']} has no field “{k}”")
        for lines in ([doc["lines"]] if doc.get("lines") else []) + doc.get("sections", []):
            source = tables.get(lines["table"])
            if not source:
                problems.append(f"{where} lists lines from “{lines['table']}”, which isn't a table")
                continue
            if not reach(t, source, lines["via"], tables):
                links = [x["key"] for x in source["fields"] if x["type"] == "link" and x["table"] == t["key"]]
                problems.append(f"{where}: lines need “via” to be {source['label']}'s link to {t['label']}, or a path through their links "
                                f"(“property.owner”) to it or to the record it links to" + (f" (“{links[0]}”)" if links else ""))
            for c in lines["columns"] + ([lines["total"]] if lines.get("total") else []):
                if not field(source, c):
                    problems.append(f"{where}: {source['label']} has no field “{c}”")
            _check_conditions(lines.get("filters"), source, where, problems, tables)
        for text in (doc.get("notes"), doc.get("footer")):
            _check_template(text, where, problems, list(keys))
    for rule in t.get("rules", []):
        where = f"the rule “{rule['message']}” on {t['label']}"
        _check_expression(rule["check"], set(keys) | {"record", "id", "created_at", "updated_at"}, where, problems, tables=tables, t=t)
        _check_conditions(rule.get("when"), t, where, problems, tables)
        _check_conditions(rule.get("holds"), t, where, problems, tables)
        if rule.get("field") and rule["field"] not in keys:
            problems.append(f"{where} shows its message on “{rule['field']}”, which isn't a field")
    _check_conditions(t.get("lock_if"), t, f"the lock on {t['label']}", problems, tables)
    _check_conditions((t.get("keep_for") or {}).get("unless"), t, f"what keeps {t['label']} past their time", problems, tables)
    for unit, amount in (t.get("keep_for") or {}).items():  # a length worked out for each record
        if unit in ("years", "months", "days") and isinstance(amount, str):
            _check_conditions([{"formula": f"({amount}) >= 0"}], t, f"how long {t['label']} are kept", problems, tables)
    for k in t.get("lock_except", []):
        if k not in keys:
            problems.append(f"the lock on {t['label']} leaves “{k}” editable, but that isn't a field")
    for rule in t.get("colors", []):
        _check_conditions(rule["when"], t, f"a colour rule on {t['label']}", problems, tables)
        if rule.get("field") and rule["field"] not in keys:
            problems.append(f"a colour rule on {t['label']} colours “{rule['field']}”, which isn't a field")
    portal = t.get("portal")
    if portal and portal.get("everyone"):
        for k in portal["fields"]:
            if k not in keys:
                problems.append(f"the portal on {t['label']} shows “{k}”, which isn't a field")
        _check_conditions(portal.get("show_if"), t, f"what the portal shows of {t['label']}", problems, tables)
    elif portal:
        head, _, hop = portal["via"].partition(".")
        via = keys.get(head)
        if not via or via["type"] != "link":
            problems.append(f"the portal on {t['label']} needs “{head}” to be a link field (to the portal's people, or to a record "
                            "that links to them)")
        elif hop and (field(tables.get(via["table"]), hop) or {}).get("type") != "link":
            problems.append(f"the portal on {t['label']} goes through “{portal['via']}”: “{hop}” must be a link field of "
                            f"{(tables.get(via['table']) or {}).get('label', via['table'])} to the portal's people")
        for k in portal["fields"] + portal.get("edit", []):
            if k not in keys:
                problems.append(f"the portal on {t['label']} shows “{k}”, which isn't a field")
            elif k in portal.get("edit", []) and keys[k]["type"] in CALCULATED:
                problems.append(f"the portal on {t['label']} lets them change “{keys[k]['label']}”, which is worked out, not filled in")
        flow = t.get("workflow")
        if flow and flow["field"] in portal.get("edit", []):
            problems.append(f"the portal on {t['label']} can't edit the stage “{flow['field']}” directly; give a workflow move "
                            f"\"who\": [\"portal\"] instead")
        for kind in ("edit_if", "show_if"):
            _check_conditions(portal.get(kind), t, f"the portal on {t['label']}", problems, tables)
    for group in t.get("unique_together", []):
        missing = [k for k in group if k not in keys]
        if missing:
            problems.append(f"{t['label']} can't have two records alike in {', '.join(missing)}: not fields of {t['label']}")
    for limit in t.get("limits", []):
        where = f"the limit on {t['label']}"
        for k in limit["per"]:
            if k not in keys:
                problems.append(f"{where} counts records with the same “{k}”, which isn't a field of {t['label']}")
        if isinstance(limit.get("max"), str):
            f = _path_field(t, limit["max"], tables)
            if f is None or f["type"] not in NUMERIC | {"formula", "rollup", "lookup"}:
                problems.append(f"{where}: its most, “{limit['max']}”, isn't a number field of {t['label']} (or through its links)")
        for end in ("start", "end"):
            k = (limit.get("overlap") or {}).get(end)
            if k and (k not in keys or keys[k]["type"] not in ("date", "datetime", "time") and keys[k].get("format") not in ("date", "datetime", "time")):
                problems.append(f"{where}: its {end}, “{k}”, isn't a date or time field of {t['label']}")
        k = (limit.get("overlap") or {}).get("length")
        if k and (k not in keys or keys[k]["type"] != "duration"):
            problems.append(f"{where}: how long each takes, “{k}”, isn't a duration field of {t['label']}")
        _check_conditions(limit.get("where"), t, where, problems, tables)
    held = t.get("approve_changes")
    if held:
        where = f"the change approval on {t['label']}"
        for k in held["fields"]:
            if k not in keys:
                problems.append(f"{where} watches “{k}”, which isn't a field")
            elif keys[k]["type"] not in STORED:
                problems.append(f"{where} watches “{keys[k]['label']}”, which is worked out, not filled in")
        if not who_fits(t, held["approver"], tables):
            problems.append(f"{where} asks “{held['approver']}” to approve: use {_who_words(t)}")
        head, _, rest = (held.get("tell") or "").partition(".")
        through = tables.get((keys.get(head) or {}).get("table") or "") if rest and (keys.get(head) or {}).get("type") == "link" else None
        if held.get("tell") and (keys.get(held["tell"]) or {}).get("type") != "email" and not person_path(t, held["tell"], tables) \
                and (field(through, rest) or {}).get("type") != "email":
            problems.append(f"{where} tells “{held['tell']}” about each change: it must be an email field of {t['label']}, or one through "
                            "a link (registered_contact.email)")
    sealed = t.get("sealed")
    if sealed:
        end = _path_field(t, sealed["until"], tables)
        if not end or engine_kind(end) not in DATES:
            problems.append(f"sealed on {t['label']}: “{sealed['until']}” must be a date or date-and-time field, on the record or "
                            "through its links (tender.closes_at)")
        for k in sealed.get("keep_open", []):
            if k not in keys:
                problems.append(f"sealed on {t['label']} keeps “{k}” open, which isn't a field")
    for step in t.get("journey", []):
        where = f"the journey of {t['label']} › {step['label']}"
        if step.get("table"):
            source = tables.get(step["table"])
            if not source:
                problems.append(f"{where} follows “{step['table']}”, which isn't a table")
                continue
            if not reach(t, source, step.get("via") or "", tables):
                links = [x["key"] for x in source["fields"] if x["type"] == "link" and x["table"] == t["key"]]
                problems.append(f"{where}: “{step.get('via')}” must be {source['label']}'s link to {t['label']}"
                                + (f" (that's “{links[0]}”)" if links else ", or a path through their links to it"))
            _check_conditions(step.get("when"), source, where, problems, tables)
        else:
            _check_conditions(step.get("when"), t, where, problems, tables)
    access = t.get("access") or {}
    for team in access.get("teams", []):
        if team not in _TEAM_KEYS.get():
            problems.append(f"{t['label']} is kept to the team “{team}”, which isn't a team (add it with set_team)")
    for f in t["fields"]:
        if f.get("access") and f["access"] not in ROLE_NAMES and f["access"] not in _TEAM_KEYS.get():
            problems.append(f"{t['label']} › {f['label']} is kept to “{f['access']}”: use admin, owner or a team")
    for rows in row_rules(t):
        for team in rows.get("teams") or []:
            if team not in _TEAM_KEYS.get():
                problems.append(f"row_access on {t['label']} lets the team “{team}” see every record, but it isn't a team (add it with set_team)")
        if rows["field"] != "created_by" and _person_path(t, rows["field"], tables) is None:
            people = [x["key"] for x in t["fields"] if x["type"] == "user"] + [
                f"{x['key']}.{y['key']}" for x in t["fields"] if x["type"] == "link" and x["table"] in tables
                for y in tables[x["table"]]["fields"] if y["type"] == "user"]
            problems.append(f"row_access on {t['label']} needs “{rows['field']}” to be a person (user) field, a linked record's "
                            f"person field such as customer.sales_rep, or created_by" + (f" ({', '.join(people)})" if people else ""))
    for f in t["fields"]:
        if not f.get("currency_field"):
            continue
        head, _, rest = f["currency_field"].partition(".")
        link = keys.get(head) if rest else None
        codes = field(tables.get(link["table"]), rest) if link and link["type"] == "link" else None if rest else keys.get(head)
        if (codes or {}).get("type") not in ("select", "text", "lookup", "formula"):
            problems.append(f"{t['label']} › {f['label']} takes its currency from “{f['currency_field']}”, which must be a select or text field of currency codes"
                            " (its own, or its linked record's: “po.currency”)")
    form = t.get("public_form")
    if form:
        for k in form["fields"]:
            f = keys.get(k)
            if not f:
                problems.append(f"the public form of {t['label']} asks for “{k}”, which isn't a field")
            elif f["type"] in CALCULATED | {"user", "link"}:
                problems.append(f"the public form of {t['label']} can't ask for “{f['label']}” ({f['type']})")


def engine_kind(f):
    """A field's kind as far as the blueprint can tell (a formula's format)."""
    if f is None:
        return None
    return f.get("format") if f["type"] == "formula" else f["type"]


def _person_path(t, path, tables):
    """The person field a row_access path ends at: "owner", "customer.sales_rep" (through a link), or a lookup of one."""
    parts = path.split(".")
    f = field(t, parts[0])
    if f is None:
        return None
    if len(parts) == 1:
        if f["type"] == "lookup":
            source = lookup_source(t, f, tables)
            return source if source and source["type"] == "user" else None
        return f if f["type"] == "user" else None
    here = t
    for part in parts[:-1]:  # through links, as many as it takes: an offer's round's buyer's members
        f = field(here, part)
        if f is None or f["type"] != "link" or f.get("table") not in tables:
            return None
        here = tables[f["table"]]
    target = field(here, parts[-1])
    return target if target and target["type"] == "user" else None


def link_reached(t, of, tables):
    """The link a step's "of" reaches records through: a link field of t, a lookup showing one (a work order's product,
    shown from its order), or a path through links ("order.product"). None when it's none of those."""
    f = _path_field(t, of, tables) if "." in of else field(t, of)
    if f and f["type"] == "lookup" and "where" not in f and "." not in of:
        f = lookup_source(t, f, tables)
    return f if f and f["type"] == "link" and f["table"] in tables else None


def _check_source(spec, source, t, where, problems, made=(), tables=None):
    """Whether a step's related records hang off the triggering record (or, with "of", off the record one of its links
    reaches, or off one made earlier in the run: the new invoice's lines)."""
    via = field(source, spec["via"])
    parent = t
    if spec.get("of") == "created":
        if not via or via["type"] != "link" or via["table"] not in made:
            problems.append(f"{where} goes through the {lc(source['label'])} of a record it created, but no earlier step creates "
                            f"the record “{spec['via']}” links to")
            return False
        return True
    if spec.get("of"):
        link = link_reached(t, spec["of"], tables or {}) if t else None
        if not link:
            problems.append(f"{where} goes through the {lc(source['label'])} of “{spec['of']}”, which isn't a link field of {t['label'] if t else 'the record'} "
                            f"(use one of its links, a lookup of one, or a path through them like “order.product”)")
            return False
        parent = {"key": link["table"], "label": link["label"]}
    if not parent or not via or via["type"] != "link" or via["table"] != parent["key"]:
        problems.append(f"{where}: “{spec['via']}” must be {source['label']}'s link to {parent['label'] if parent else 'the triggering record'}")
        return False
    if spec.get("nested") and not nested_link(source, spec):
        links = [f["key"] for f in source["fields"] if f["type"] == "link" and f["table"] == via["table"] and f["key"] != via["key"]]
        problems.append(f"{where}: “nested” must be the link of {source['label']} to the sub-assembly a line is made of (another of "
                        f"{parent['label']})" + (f": “{links[0]}”" if len(links) == 1 else f"; {source['label']} has none yet" if not links else ""))
        return False
    return True


def stages(t):
    """A table's stages in order (its workflow field's options), or []."""
    flow = t.get("workflow")
    f = field(t, flow["field"]) if flow else None
    return list(f.get("options") or []) if f and f["type"] == "select" else []


SAYS_NO = re.compile(r"\bnot\b|reject|declin|refus|lost|fail|unsuccess|cancel|void|withdrawn|abandon|turned down|sent back|returned\b", re.I)


ENDED = re.compile(r"clos|complet|\bdone\b|paid|finish|resolv|archiv", re.I)  # the work's end, not the next step of a new one
STOPS = re.compile(r"cancel|void|withdrawn|abandon", re.I)  # stopped by whoever raised it, not a decision


def lc(text):
    """A label in lower case for the middle of a sentence, its words in capitals kept ("Edit PO lines", "your GST")."""
    return re.sub(r"\S+", lambda m: m.group(0) if re.fullmatch(r"[A-Z0-9&/]{2,}s?", m.group(0)) else m.group(0).lower(), str(text or ""))


def a_one(label, start=False):
    """“a quote”, “an adoption”: a label for the middle of a sentence with its article (`start`: “An adoption” to begin one)."""
    word = lc(label)
    out = f"{'an' if word[:1].lower() in 'aeiou' else 'a'} {word}"
    return out[:1].upper() + out[1:] if start else out


def says_no(stage):
    """Whether a stage is a "no" or a stop (Rejected, Not awarded, Cancelled) rather than a step onwards."""
    return bool(SAYS_NO.search(str(stage or "")))


def sending_on(bp, t):
    """The moves a new record's form may make as it's saved ("Save and send for approval"): onwards from where it starts,
    never a "no", the end of the work (Closed, Paid), or someone's decision (Approve beside Reject, or into the stage an
    approval rule sets once it's approved): those are for later, by whoever decides. [(index, move)]."""
    flow, start = t.get("workflow"), start_stage(t)
    if not flow or not start:
        return []
    key = flow["field"]
    out = [(i, m) for i, m in enumerate(flow["transitions"]) if (not m["from"] or start in m["from"]) and m["to"] != start]
    approved_into = set()
    for a in bp.get("automations", []):
        if (a.get("trigger") or {}).get("table") != t["key"]:
            continue
        asked = False
        for s in steps_of(a["actions"]):
            asked = asked or s.get("type") == "request_approval"
            if asked and s.get("type") == "update_record" and key in (s.get("values") or {}):
                approved_into.add(str(s["values"][key]).lower())
    deciders = [set(m.get("who") or []) for _, m in out if says_no(m["to"]) and not STOPS.search(m["to"]) and "creator" not in (m.get("who") or [])]

    def decision(m):
        who = set(m.get("who") or [])
        return m["to"].lower() in approved_into or "creator" in (m.get("not_by") or []) or \
            (who and "creator" not in who and any(who & d or not d for d in deciders))
    return [(i, m) for i, m in out if not says_no(m["to"]) and not ENDED.search(m["to"]) and not decision(m)]


def _hours(value, per=1):
    """A duration as hours: 30 with minutes as the unit (per = 1/60), "30 minutes", "1h 30m", "1:30"."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(value * per, 4)
    text = str(value).strip().lower()
    clock = re.fullmatch(r"(\d+):(\d{1,2})", text)
    if clock:
        return round(int(clock.group(1)) + int(clock.group(2)) / 60, 4)
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\b", text)
    if parts:
        return round(sum(float(n) / (60 if u.startswith("m") else 1) for n, u in parts), 4)
    n = expr.to_number(text)
    return round(n * per, 4) if n is not None else value


def start_stage(t):
    """The stage a new record of t begins at: the workflow's "start", the field's default, or its first option."""
    flow, names = t.get("workflow"), stages(t)
    if not names:
        return None
    f = field(t, flow["field"])
    for said in (flow.get("start"), f.get("default")):
        if said in names:
            return said
    return names[0]


def _check_workflow(t, tables, problems):
    flow = t["workflow"]
    where = f"the workflow of {t['label']}"
    f = field(t, flow["field"])
    if not f or f["type"] != "select":
        choices = [x["key"] for x in t["fields"] if x["type"] == "select"]
        problems.append(f"{where} moves records through “{flow['field']}”, which must be a choice (select) field of {t['label']}"
                        + (f" ({', '.join(choices)})" if choices else "; add one with the stages as its options"))
        return
    names = stages(t)
    if flow.get("start") and flow["start"] not in names:
        problems.append(f"{where} starts at “{flow['start']}”, which isn't one of {f['label']}'s options ({', '.join(names)})")
    for m in flow["transitions"]:
        for stage in m["from"] + [m["to"]]:
            if stage not in names:
                problems.append(f"{where} has a move with “{stage}”, which isn't one of {f['label']}'s options ({', '.join(names)})")
        for who in m.get("who", []):
            if not who_fits(t, who, tables):
                problems.append(f"{where}: “{who}” can't make a move; use {_who_words(t)}")
        for who in m.get("not_by", []):
            if not _apart_fits(t, who, tables):
                problems.append(f"{where}: the move to {m['to']} is kept from “{who}”; use creator, a stage of {f['label']} (whoever "
                                f"moved it there), a team or a person field of {t['label']}")
        for k in m.get("requires", []):
            if not field(t, k):
                problems.append(f"{where}: the move to {m['to']} needs “{k}” filled in, which isn't a field of {t['label']}")
        _check_conditions(m.get("only_if"), t, where, problems, tables)


def _apart_fits(t, who, tables):
    """A "not_by" entry: the creator, a stage (whoever moved the record there), a team, or people named on the record."""
    return who == "creator" or who in _TEAM_KEYS.get() or person_path(t, who, tables) \
        or str(who).lower() in [x.lower() for x in stages(t)]


def _check_automation(a, tables, problems, documents, bp_pages=(), automations=None):
    token = _TABLES.set(tables)
    try:
        _check_automation_in(a, tables, problems, documents, bp_pages, automations)
    finally:
        _TABLES.reset(token)


def _check_automation_in(a, tables, problems, documents, bp_pages=(), automations=None):
    where = f"the automation “{a['name']}”"
    trig = a["trigger"]
    t = tables.get(trig.get("table")) if trig.get("table") else None
    if trig.get("table") and not t:
        problems.append(f"{where} watches “{trig['table']}”, which isn't a table")
        return
    if t:
        _check_conditions(a["conditions"], t, where, problems, tables)
    elif a["conditions"]:
        problems.append(f"{where} has conditions but no table to check them on")
    context_fields = [f["key"] for f in t["fields"]] + [slug(t.get("singular") or "")] if t else []  # "client.name": the record

    def person(who, what):
        """Whether a step can be for `who`: a role, a team, the record's creator, its outside person, or people named on it."""
        if who_fits(t, who, tables):
            return True
        problems.append(f"{where} {what} “{who}”: use {_who_words(t)}" + ("" if t else " (it isn't about a record, so not a person field)"))
        return False

    def apart(act):
        for who in act.get("not_by", []):
            if not t or not _apart_fits(t, who, tables):
                problems.append(f"{where} keeps a step from “{who}”: use creator, a stage (whoever moved the record there), a team or a "
                                f"person field" + (f" of {t['label']}" if t else ""))

    def sure_field(act):  # how sure the AI is, kept in a number field of the record
        if act.get("confidence") and t is not None and (field(t, act["confidence"]) or {}).get("type") not in ("number", "percent"):
            problems.append(f"{where}: it keeps how sure the AI is in “{act['confidence']}”, which must be a number field of {t['label']}")

    def check_steps(steps, created_table, made):
        """Steps in order (a branch's paths and an approval's "if rejected" are checked the same way); returns the last
        record made by them and every table they made records in."""
        for act in steps or []:
            kind = act["type"]
            for ref in act.get("reads", []):
                if ref["table"] not in tables:
                    problems.append(f"{where}: the AI reads “{ref['table']}”, which isn't a table")
                else:
                    _check_conditions(ref.get("where"), tables[ref["table"]], where, problems, tables)
            if act.get("only_if"):
                if t:
                    _check_conditions(act["only_if"], t, where, problems, tables)
                else:
                    problems.append(f"{where}: a step has conditions but there's no record to check them on")
            if kind in ("update_record", "ai_fill", "ai_extract", "ai_check", "generate_document", "request_approval", "update_each", "assign",
                        "allocate", "branch") and not t \
                    and not (kind in ("update_record", "generate_document") and act.get("target") == "created" and created_table):
                problems.append(f"{where}: “{kind}” needs a record, but this automation isn't about one (add a table to the trigger)")
                continue
            if kind == "update_record":
                target = t
                if act.get("target") == "created":
                    target = tables.get(created_table) if created_table else None
                    if target is None:
                        problems.append(f"{where} updates the record it created, but no earlier step creates one")
                        continue
                elif act.get("target"):
                    links = link_path(t, act["target"], tables)
                    if not links:
                        problems.append(f"{where} updates “{act['target']}”, which isn't a link field of {t['label']}"
                                        + (" (or a path of them, as “invoice.couple”)" if "." in act["target"] else ""))
                        continue
                    target = tables.get(links[-1]["table"])
                _check_values(act["values"], target, where, problems, context_fields)
            elif kind in ("create_record", "create_for_each", "ai_create_record"):
                target = tables.get(act["table"])
                if not act["table"]:
                    problems.append(f"{where}: the {kind} action needs \"table\": the table the new records go in")
                    continue
                if not target:
                    problems.append(f"{where} creates records in “{act['table']}”, which isn't a table")
                    continue
                if kind != "create_for_each":  # "the record it created" stays the invoice while its lines are made
                    created_table = act["table"]
                    made.add(act["table"])
                if kind == "create_for_each" and act["source"].get("field"):  # one for each record or person in a field
                    each = field(t, act["source"]["field"]) if t else None
                    if not each or each["type"] not in ("link", "user"):
                        problems.append(f"{where} makes one record for each in “{act['source']['field']}”, which must be a link or "
                                        f"person field" + (f" of {t['label']}" if t else " of the record (there is none)"))
                        continue
                    source = tables.get(each["table"]) if each["type"] == "link" else None
                    if source is not None:
                        _check_conditions(act.get("where"), source, where, problems, tables)
                    _check_values(act["values"], target, where, problems, context_fields + [f["key"] for f in (source or {}).get("fields", [])])
                elif kind == "create_for_each":
                    source = tables.get(act["source"]["table"])
                    if not source:
                        problems.append(f"{where} goes through “{act['source']['table']}”, which isn't a table")
                    else:
                        if not act["source"].get("all"):
                            _check_source(act["source"], source, t, where, problems, made, tables)
                        _check_conditions(act.get("where"), source, where, problems, tables)
                    item_fields = [f["key"] for f in source["fields"]] if source else []
                    _check_values(act["values"], target, where, problems, context_fields + item_fields)
                elif kind == "create_record":
                    _check_values(act["values"], target, where, problems, context_fields)
                elif act.get("attach_to") and not (field(target, act["attach_to"]) or {}).get("type") in FILES:
                    problems.append(f"{where}: “{act['attach_to']}” must be a file field of {target['label']}")
                if kind == "ai_create_record" and act.get("reviewer") and act["reviewer"] not in APPROVER_ROLES \
                        and act["reviewer"] not in _TEAM_KEYS.get():
                    problems.append(f"{where} asks “{act['reviewer']}” to check what the AI made: use owner, admin or editor (the "
                                    f"new record has no person in it yet)")
            elif kind == "update_each":
                source = tables.get(act["source"]["table"])
                if not source:
                    problems.append(f"{where} updates “{act['source']['table']}”, which isn't a table")
                elif _check_source(act["source"], source, t, where, problems, made, tables):
                    _check_values(act["values"], source, where, problems, context_fields + [f["key"] for f in source["fields"]])
                    _check_conditions(act.get("where"), source, where, problems, tables)
            elif kind == "assign":
                if (field(t, act["field"]) or {}).get("type") != "user":
                    problems.append(f"{where} assigns “{act['field']}”, which must be a person field of {t['label']}")
                if act.get("from") not in (*APPROVER_ROLES, "viewer") and act.get("from") not in _TEAM_KEYS.get():
                    problems.append(f"{where} assigns among “{act.get('from')}”: use editor, admin or a team")
            elif kind == "allocate":
                f = field(t, act["field"])
                if (f or {}).get("type") != "link" or f.get("table") not in tables:
                    links = [x["key"] for x in t["fields"] if x["type"] == "link"]
                    problems.append(f"{where} allocates “{act['field']}”, which must be a link field of {t['label']} to the list it picks "
                                    f"from" + (f" (its links: {', '.join(links)})" if links else ": add one first"))
                else:
                    if act.get("count", 1) > 1 and not f.get("multiple"):
                        problems.append(f"{where} picks {act['count']} for {f['label']}, which holds one: pick 1, or let it hold several")
                    _check_conditions(act.get("where"), tables[f["table"]], where, problems, tables)
                if act.get("free_on") and engine_kind(field(t, act["free_on"])) not in DATES:
                    problems.append(f"{where} picks those free on “{act['free_on']}”, which must be a date field of {t['label']}")
            elif kind == "wait":
                if act.get("until") and (not t or engine_kind(field(t, act["until"])) not in DATES):
                    problems.append(f"{where} waits until “{act['until']}”, which must be a date field of the record")
                if act.get("until_met"):
                    if t:
                        _check_conditions(act["until_met"], t, where, problems, tables)
                    else:
                        problems.append(f"{where} waits for conditions but there's no record to check them on")
            elif kind == "delete_record":
                if not t and act.get("target") != "created":
                    problems.append(f"{where}: delete_record needs a record, but this automation isn't about one")
            elif kind == "notify":
                _check_template(act["message"], where, problems, context_fields)
                if act["to"] not in ("everyone", "viewer") and not who_fits(t, act["to"], tables):
                    problems.append(f"{where} notifies “{act['to']}”: use everyone, {_who_words(t)}")
            elif kind == "send_email":
                for text in (act["to"], act.get("cc"), act["subject"], act["body"]):
                    _check_template(text, where, problems, context_fields)
                pages_ = {p["key"] for p in bp_pages}
                for k in act.get("attach", []):
                    if k.startswith("page:"):
                        if k[5:] not in pages_:
                            problems.append(f"{where} attaches the page “{k[5:]}”, which doesn't exist")
                        continue
                    if k not in documents.get(t["key"] if t else "", {}) and k not in documents.get(created_table or "", {}) \
                            and (not t or (field(t, k) or {}).get("type") not in FILES):
                        problems.append(f"{where} attaches “{k}”, which isn't a document template or file field of {t['label'] if t else 'the record'}")
            elif kind == "generate_document":
                of = t
                if act.get("target") == "created":
                    of = tables.get(created_table) if created_table else None
                    if of is None:
                        problems.append(f"{where} produces a document for the record it created, but no earlier step creates one")
                        continue
                elif act.get("target"):
                    links = link_path(t, act["target"], tables)
                    if not links:
                        problems.append(f"{where} produces a document for “{act['target']}”, which isn't a link field of {t['label']}")
                        continue
                    of = tables.get(links[-1]["table"]) or t
                if act["document"] not in documents.get(of["key"], {}):
                    problems.append(f"{where} produces “{act['document']}”, which isn't a document template of {of['label']}"
                                    + (f" ({', '.join(documents.get(of['key'], {}))})" if documents.get(of["key"]) else "; add one with add_document"))
                if act.get("field") and (field(of, act["field"]) or {}).get("type") not in FILES:
                    problems.append(f"{where}: “{act['field']}” must be a file field of {of['label']} to save the document into")
            elif kind == "copy_record":
                if not t:
                    problems.append(f"{where}: copy_record copies the record it's about, but this automation isn't about one")
                    continue
                _check_values(act["values"], t, where, problems, context_fields)
                back = field(t, act.get("link_back", "")) if act.get("link_back") else None
                if act.get("link_back") and (not back or back["type"] != "link" or back["table"] != t["key"]):
                    problems.append(f"{where}: “{act['link_back']}” must be a link from {t['label']} to {t['label']} (the copy's original)")
                created_table = t["key"]
                made.add(t["key"])
            elif kind == "request_approval":
                apart(act)
                for who in act.get("approvers") or [act["approver"]]:
                    person(who, "asks")
                if act.get("escalate_to"):
                    person(act["escalate_to"], "passes overdue approvals to")
                _check_template(act["message"], where, problems, context_fields)
                check_steps(act.get("if_rejected"), created_table, set(made))
            elif kind == "webhook":
                def strings(value, line_fields=None):
                    if isinstance(value, dict) and "each" in value and "item" in value:
                        lines = tables.get(str(value["each"]))
                        if lines is None or not t or not any(f["type"] == "link" and f.get("table") == t["key"] for f in lines["fields"]):
                            problems.append(f"{where}: “each” names “{value['each']}”, which isn't a line table of {t['label'] if t else 'the record'}")
                            return
                        token = _MORE_CONTEXT.set(_MORE_CONTEXT.get() | {"line"})
                        try:
                            strings(value["item"])
                        finally:
                            _MORE_CONTEXT.reset(token)
                    elif isinstance(value, dict):
                        for v in value.values():
                            strings(v)
                    elif isinstance(value, list):
                        for v in value:
                            strings(v)
                    elif isinstance(value, str):
                        _check_template(value, where, problems, context_fields)
                strings(act.get("payload") or {})
                if act.get("app"):
                    from ..connectors import apps
                    if apps.get(act["app"]) is None:
                        problems.append(f"{where} sends to “{act['app']}”, which isn't an app ({', '.join(a.key for a in apps.all_apps())})")
                for k in (act.get("save") or {}):
                    if t and not field(t, k):
                        problems.append(f"{where}: it keeps the answer in “{k}”, which isn't a field of {t['label']}")
                for text in [act["url"], *(act.get("headers") or {}).values(), *([act["hosts"]] if isinstance(act.get("hosts"), str) else [])]:
                    _check_template(text, where, problems, context_fields)
            elif kind == "import_rows":
                into = tables.get(act["table"])
                if t is None or (field(t, act["field"]) or {}).get("type") not in FILES:
                    problems.append(f"{where}: it brings in the spreadsheet in “{act['field']}”, which must be a file field of "
                                    f"{t['label'] if t else 'the record'}")
                if into is None:
                    problems.append(f"{where}: its rows go into “{act['table']}”, which isn't a list")
                else:
                    for k in [*(act.get("values") or {}), *(act.get("columns") or {}).values(), *([act["match_on"]] if act.get("match_on") else [])]:
                        if not field(into, k):
                            problems.append(f"{where}: “{k}” isn't a field of {into['label']}")
                    for v in (act.get("values") or {}).values():
                        if isinstance(v, str):
                            _check_template(v, where, problems, context_fields)
                if act.get("result") and t and not field(t, act["result"]):
                    problems.append(f"{where}: what came of each row goes in “{act['result']}”, which isn't a field of {t['label']}")
            elif kind == "payment_link":
                if t is None:
                    problems.append(f"{where}: a payment link is for a record (an invoice)")
                else:
                    if (field(t, act["save_to"]) or {}).get("type") != "url":
                        problems.append(f"{where}: the payment page goes in “{act['save_to']}”, which must be a web address field of {t['label']}")
                    for k in (act.get("paid") or {}):
                        if not field(t, k):
                            problems.append(f"{where}: when it's paid it sets “{k}”, which isn't a field of {t['label']}")
                    for text in (act["amount"], act.get("description") or ""):
                        _check_template(text, where, problems, context_fields)
            elif kind == "ai_check":
                if act.get("reviewer"):
                    person(act["reviewer"], "asks to check the AI's work")
                verdict = field(t, act["field"])
                if not verdict or verdict["type"] not in ("select", "checkbox"):
                    choices = [x["key"] for x in t["fields"] if x["type"] == "select"]
                    problems.append(f"{where}: the AI's verdict goes in “{act['field']}”, which must be a choice field of {t['label']} "
                                    f"(Pass, Issues, Fail)" + (f" ({', '.join(choices)})" if choices else "; add one"))
                if act.get("notes_field") and (field(t, act["notes_field"]) or {}).get("type") not in ("text", "long_text", "rich_text"):
                    problems.append(f"{where}: what the AI found goes in “{act['notes_field']}”, which must be a long text field of {t['label']}")
                sure_field(act)
            elif kind == "ai_fill":
                sure_field(act)
                if act.get("reviewer"):
                    person(act["reviewer"], "asks to check the AI's work")
                f = field(t, act["field"])
                if not f:
                    problems.append(f"{where} has the AI fill “{act['field']}”, which isn't a field of {t['label']}")
                elif f["type"] not in AI_FILLABLE:
                    problems.append(f"{where}: the AI can't fill “{f['label']}” ({f['type']})")
            elif kind == "ai_extract":
                if act.get("reviewer"):
                    person(act["reviewer"], "asks to check the AI's work")
                if act["from"] not in ("email", "file") and (field(t, act["from"]) or {}).get("type") not in FILES:
                    problems.append(f"{where} reads “{act['from']}”, which isn't a file field of {t['label']} (or email/file)")
                for k in act.get("fields", []):
                    f = field(t, k)
                    if not f or f["type"] not in AI_FILLABLE | {"link", "email", "phone", "url"}:
                        problems.append(f"{where}: the AI can't fill “{k}” of {t['label']}")
            elif kind == "branch":
                for path in act["paths"]:
                    _check_conditions(path["when"], t, where, problems, tables)
                    check_steps(path["steps"], created_table, set(made))
                check_steps(act.get("otherwise"), created_table, set(made))
            elif kind == "task":
                apart(act)
                person(act["to"], "gives a task to")
                _check_template(act["message"], where, problems, context_fields)
                for k in act.get("fields", []):
                    f = field(t, k) if t else None
                    if not f:
                        problems.append(f"{where}: the task asks for “{k}”, which isn't a field of {t['label'] if t else 'the record (there is none)'}")
                    elif f["type"] in CALCULATED:
                        problems.append(f"{where}: the task asks for “{f['label']}”, which is worked out, not filled in")
                if act.get("priority") and act["priority"] not in PRIORITIES and not (t and field(t, act["priority"])):
                    problems.append(f"{where}: the task's priority “{act['priority']}” isn't low, normal, high, urgent or a field")
                if act.get("flow"):
                    flow = _FLOW_KEYS.get().get(act["flow"])
                    if flow is None:
                        problems.append(f"{where}: the task is done through “{act['flow']}”, which isn't a flow")
                    elif t is None or flow["table"] != t["key"] or flow["start"] != "record":
                        problems.append(f"{where}: the task's flow “{flow['label']}” must be one started from a "
                                        f"{t['singular'].lower() if t else 'record'} (start \"record\" on {t['key'] if t else 'its table'})")
            elif kind == "run_automation":
                if automations is not None and act["automation"] not in automations:
                    problems.append(f"{where} runs “{act['automation']}”, which isn't an automation"
                                    + (f" ({', '.join(sorted(automations))})" if automations else ""))
                elif act["automation"] == a["key"]:
                    problems.append(f"{where} runs itself")
                if act.get("target") == "created":
                    if not created_table:
                        problems.append(f"{where} runs “{act['automation']}” for the record it created, but no earlier step creates one")
                elif act.get("target") and not link_path(t, act["target"], tables):
                    problems.append(f"{where} runs “{act['automation']}” for “{act['target']}”, which isn't a link field of the record")
        return created_table, made

    check_steps(a["actions"], None, set())
    if trig["type"] == "webhook_received" and not trig.get("table") and not any(x["type"] in ("ai_create_record", "create_record", "notify", "webhook", "send_email")
                                                                                  for x in steps_of(a["actions"])):  # about a record: it may change it
        problems.append(f"{where} starts from a webhook but does nothing with what it's sent; use create_record with {{{{payload.field}}}} values, or ai_create_record")
    if trig["type"] in ("email_received", "file_added") and not any(x["type"] in ("ai_create_record", "notify", "webhook", "create_record")
                                                                     for x in steps_of(a["actions"])):
        problems.append(f"{where} starts from an email or file but does nothing with it; use ai_create_record to turn it into a record")


def check(bp):
    """Every reference in the blueprint that doesn't point at something real, as plain sentences."""
    token = _TEAM_KEYS.set(frozenset(tm["key"] for tm in teams(bp)))
    known = _FLOW_KEYS.set({f["key"]: f for f in flows(bp)})
    tables_ = _BP_TABLES.set({t["key"]: t for t in bp.get("tables", [])})
    try:
        return _check_all(bp)
    finally:
        _TEAM_KEYS.reset(token)
        _FLOW_KEYS.reset(known)
        _BP_TABLES.reset(tables_)


def _check_all(bp):
    problems = []
    keys_ = [tm["key"] for tm in teams(bp)]
    for key in {k for k in keys_ if keys_.count(k) > 1}:
        problems.append(f"there are two teams called “{key}”")
    tables = {t["key"]: t for t in bp.get("tables", [])}
    modules = {m["key"] for m in bp.get("modules", [])}
    documents = {t["key"]: {d["key"]: d for d in t.get("documents", [])} for t in bp.get("tables", [])}
    for t in bp.get("tables", []):
        _check_table(t, tables, modules, problems)
        if t.get("workflow"):
            _check_workflow(t, tables, problems)
    pages = {p["key"] for p in bp.get("pages", [])}
    automations = {a["key"] for a in bp.get("automations", [])}
    combined = {k for k, t in tables.items() if t.get("combines")}
    for a in bp.get("automations", []) if combined else []:  # a combined list follows its lists: nothing else writes to it
        on = a["trigger"].get("table")
        for step in steps_of(a["actions"]):
            into = step.get("table") if step["type"] in ("create_record", "create_for_each", "ai_create_record") else \
                on if step["type"] in ("update_record", "delete_record", "copy_record", "ai_fill", "ai_extract", "ai_check") and not step.get("target") else \
                (step.get("source") or {}).get("table") if step["type"] == "update_each" else None
            if into in combined:
                problems.append(f"the automation “{a['name']}” changes {tables[into]['label']}, which is put together from other lists "
                                f"by itself: change those lists instead")
    for portal in portals(bp):
        people = tables.get(portal["table"])
        if not people:
            problems.append(f"the {portal['title']} is for “{portal['table']}”, which isn't a table")
        elif (field(people, portal["email_field"]) or {}).get("type") != "email":
            problems.append(f"the {portal['title']} signs people in by “{portal['email_field']}”, which must be an email field of {people['label']}")
        else:
            for k in portal.get("profile", []):
                f = field(people, k)
                if k == (people.get("workflow") or {}).get("field"):
                    problems.append(f"the {portal['title']} can't let them change their own {f['label'] if f else k}: it's the "
                                    "stage the business moves them through (verified)")
                elif not f or f["type"] not in STORED or f["type"] in ("autonumber",):
                    problems.append(f"the {portal['title']} lets them keep “{k}” up to date, which must be a field of {people['label']} people fill in")
            for k in (portal.get("sign_up") or {}).get("fields", []):
                f = field(people, k)
                if not f or f["type"] not in STORED or f["type"] in ("link", "user", "autonumber"):
                    problems.append(f"the {portal['title']}'s sign-up asks for “{k}”, which must be a field of {people['label']} people fill in")
    everyone = sorted(p["table"] for p in portals(bp))
    for t in bp.get("tables", []) if everyone else []:
        via = field(t, (t.get("portal") or {}).get("via", ""))
        if t.get("portal") and via and not any(portal_shows(tables, t, people) for people in everyone):
            problems.append(f"the portal on {t['label']}: “{via['key']}” must link to {' or '.join(everyone)} (or to a record that does)")
    for p in bp.get("pages", []):
        if p.get("module") and p["module"] not in modules:
            problems.append(f"the page {p['label']} is in module “{p['module']}”, which doesn't exist")
        for who in p.get("home_for", []) + ([p["access"]] if p.get("access") else []):
            if who not in ROLE_NAMES and who not in _TEAM_KEYS.get():
                problems.append(f"the page {p['label']} is for “{who}”: use viewer, editor, admin, owner or a team")
        for w in p["widgets"]:
            if w["type"] in ("text", "heading", "embed", "insight", "my_work"):
                continue
            if w["type"] == "actions":
                for b in w["buttons"]:
                    if b.get("table") and b["table"] not in tables:
                        problems.append(f"the page {p['label']}: the button “{b['label']}” opens “{b['table']}”, which isn't a table")
                    if b.get("page") and b["page"] not in pages:
                        problems.append(f"the page {p['label']}: the button “{b['label']}” opens the page “{b['page']}”, which doesn't exist")
                    if b.get("automation") and b["automation"] not in automations:
                        problems.append(f"the page {p['label']}: the button “{b['label']}” runs “{b['automation']}”, which isn't an automation")
                    elif b.get("automation") and next(a for a in bp["automations"] if a["key"] == b["automation"])["trigger"]["type"] != "schedule":
                        problems.append(f"the page {p['label']}: the button “{b['label']}” can only run a scheduled automation "
                                        "(buttons on records are made with a button trigger)")
                continue
            if w["type"] == "activity":
                for k in w.get("tables", []):
                    if k not in tables:
                        problems.append(f"the page {p['label']}: the activity feed shows “{k}”, which isn't a table")
                continue
            where = f"the page {p['label']} › {w.get('label') or w['type']}"
            t = tables.get(w["table"])
            if not t:
                problems.append(f"{where} shows “{w['table']}”, which isn't a table")
                continue
            keys = {f["key"]: f for f in t["fields"]}
            if w.get("field") and w["field"] not in keys:
                problems.append(f"{where}: {t['label']} has no field “{w['field']}”")
            if w.get("group_by") and w["group_by"] not in keys and w["group_by"] not in ("created_at", "updated_at"):
                problems.append(f"{where}: {t['label']} has no field “{w['group_by']}” to group by")
            for k in (w.get("rows"), w.get("across"), w.get("stack_by"), (w.get("compare") or {}).get("date_field"), w.get("date_field"),
                      w.get("location_field")):
                if k and k not in keys and k not in ("created_at", "updated_at"):
                    problems.append(f"{where}: {t['label']} has no field “{k}” to break it down by")
            for k in w.get("fields", []) if w["type"] == "form" else []:
                if k not in keys or keys[k]["type"] not in STORED - {"autonumber"} | FILES:
                    problems.append(f"{where}: the form can't ask for “{k}” (it must be a field people fill in)")
            for c in w.get("columns", []):
                if c not in keys:
                    problems.append(f"{where}: {t['label']} has no column “{c}”")
            _check_conditions(w.get("filters"), t, where, problems, tables)
    for a in bp.get("automations", []):
        _check_automation(a, tables, problems, documents, bp.get("pages", []), automations)
    keys_ = [f["key"] for f in flows(bp)]
    for key in {k for k in keys_ if keys_.count(k) > 1}:
        problems.append(f"there are two flows called “{key}”")
    for flow in flows(bp):
        _check_flow(flow, tables, documents, problems, bp.get("pages", []), automations)
    for sync in syncs(bp):
        _check_sync(sync, tables, problems)
    flow_keys = {f["key"] for f in flows(bp)}
    for space in spaces(bp):
        where = f"the space “{space['label']}”"
        for who in space["for"]:
            if who not in ROLE_NAMES and who not in _TEAM_KEYS.get():
                problems.append(f"{where} is for “{who}”, which isn't a team or a role")
        for g in space["groups"]:
            for item in g["items"]:
                if item.get("table") and item["table"] not in tables:
                    problems.append(f"{where} shows “{item['table']}”, which isn't a table")
                elif item.get("table") and item.get("view") and item["view"] not in {v["key"] for v in tables[item["table"]].get("views", [])}:
                    problems.append(f"{where}: {tables[item['table']]['label']} has no view “{item['view']}”")
                elif item.get("page") and item["page"] not in pages:
                    problems.append(f"{where} shows the page “{item['page']}”, which isn't one")
                elif item.get("flow") and item["flow"] not in flow_keys:
                    problems.append(f"{where} shows the flow “{item['flow']}”, which isn't one")
    return problems


def _check_sync(sync, tables, problems):
    from ..connectors import apps
    where = f"the sync of {sync['app']} {sync['pull']}"
    app = apps.get(sync["app"])
    if app is None:
        problems.append(f"{where}: there's no app “{sync['app']}” ({', '.join(a.key for a in apps.all_apps())})")
        return
    if sync["pull"] not in app.pulls:
        problems.append(f"{where}: {app.name} can bring in {', '.join(app.pulls) or 'nothing'}, not “{sync['pull']}”")
    t = tables.get(sync["into"])
    if t is None:
        problems.append(f"{where} goes into “{sync['into']}”, which isn't a table")
        return
    keys = {f["key"]: f for f in t["fields"]}
    if (keys.get(sync["match"]) or {}).get("type") not in ("text", "number", "barcode"):
        problems.append(f"{where}: {t['label']} needs a text field “{sync['match']}” to keep {app.name}'s id in (to match records next time)")
    for k in sync["fields"]:
        if k not in keys or keys[k]["type"] not in STORED:
            problems.append(f"{where}: “{k}” isn't a field of {t['label']} it can fill")
    lines = sync.get("lines")
    if lines:
        lt = tables.get(lines["into"])
        if lt is None:
            problems.append(f"{where}: its lines go into “{lines['into']}”, which isn't a table")
            return
        lines["via"] = lines["via"] or _line_link(lt, t["key"]) or ""
        if (field(lt, lines["via"]) or {}).get("table") != t["key"]:
            problems.append(f"{where}: {lt['label']} needs a link to {t['label']} (\"via\")")
        for k in lines["fields"]:
            if not field(lt, k) or field(lt, k)["type"] not in STORED:
                problems.append(f"{where}: “{k}” isn't a field of {lt['label']} it can fill")


def _line_link(lines_table, parent):
    """The link of a line table to its parent, when there's only one."""
    links = [f["key"] for f in lines_table["fields"] if f["type"] == "link" and f.get("table") == parent]
    return links[0] if len(links) == 1 else None


def _check_flow(flow, tables, documents, problems, bp_pages=(), automations=None):
    where = f"the flow “{flow['label']}”"
    t = tables.get(flow["table"])
    if t is None:
        problems.append(f"{where} works on “{flow['table']}”, which isn't a table")
        return
    keys = {f["key"]: f for f in t["fields"]}
    for step in flow["steps"]:
        at = f"{where}, step “{step['label']}”"
        kind = step["kind"]
        if kind == "form":
            for k in step["fields"]:
                if k not in keys or keys[k]["type"] not in (STORED - {"autonumber"}) | FILES | {"signature"}:
                    problems.append(f"{at}: “{k}” isn't a field of {t['label']} people fill in")
        elif kind in ("scan", "pick"):
            spec = step["into"] if kind == "scan" else step["lines"]
            lines = tables.get(spec["table"])
            if lines is None:
                problems.append(f"{at}: “{spec['table']}” isn't a table")
                continue
            spec["via"] = spec["via"] or _line_link(lines, t["key"]) or ""
            via = field(lines, spec["via"])
            if via is None or via["type"] != "link" or via.get("table") != t["key"]:
                problems.append(f"{at}: {lines['label']} needs a link to {t['label']} (\"via\")")
            if kind == "scan":
                spec["item"] = spec["item"] or next((f["key"] for f in lines["fields"] if f["type"] == "link" and f["key"] != spec["via"]), "")
                item = field(lines, spec["item"])
                if item is None or item["type"] != "link" or item.get("table") not in tables:
                    problems.append(f"{at}: {lines['label']} needs a link to what's scanned (\"item\")")
                if spec.get("quantity") and (field(lines, spec["quantity"]) or {}).get("type") not in ("number",):
                    problems.append(f"{at}: the quantity “{spec['quantity']}” must be a number field of {lines['label']}")
            else:
                if step.get("target") and (field(lines, step["target"]) or {}).get("type") not in ("number", "formula", "rollup", "lookup"):
                    problems.append(f"{at}: the target “{step['target']}” must be a number field of {lines['label']}")
                if step.get("count_into") and (field(lines, step["count_into"]) or {}).get("type") != "number":
                    problems.append(f"{at}: the count is saved in “{step['count_into']}”, which must be a number field of {lines['label']}")
                if step.get("code"):
                    first, _, rest = step["code"].partition(".")
                    f = field(lines, first)
                    if f is None or (rest and (f["type"] != "link" or not field(tables.get(f.get("table")) or {"fields": []}, rest))):
                        problems.append(f"{at}: the code “{step['code']}” isn't a field of {lines['label']} (or of what it links to)")
        elif kind == "photo" and (keys.get(step["field"]) or {}).get("type") not in FILES:
            problems.append(f"{at}: the photo is kept in “{step['field']}”, which must be an image or file field of {t['label']}")
        elif kind == "signature" and (keys.get(step["field"]) or {}).get("type") != "signature":
            problems.append(f"{at}: the signature is kept in “{step['field']}”, which must be a signature field of {t['label']}")
        elif kind == "decide" and step.get("field"):
            f = keys.get(step["field"])
            if f is None or f["type"] not in ("select", "text", "checkbox"):
                problems.append(f"{at}: the choice is kept in “{step['field']}”, which must be a choice or text field of {t['label']}")
            elif f["type"] == "select":
                for o in step["options"]:
                    if o["value"] not in (f.get("options") or []):
                        problems.append(f"{at}: “{o['value']}” isn't a choice of {f['label']} ({', '.join(f.get('options') or [])})")
        elif kind == "print":
            owner = t["key"] if step["of"] == "record" else next(
                (s2.get("into", s2.get("lines", {})).get("table") for s2 in flow["steps"] if s2["key"] == step["of"]), step["of"])
            if step["document"] and step["document"] not in documents.get(owner, {}):
                problems.append(f"{at}: “{step['document']}” isn't a label or document of {(tables.get(owner) or {}).get('label', owner)}")
    if flow.get("when"):
        _check_conditions(flow["when"], t, where, problems, tables)
    if flow.get("then"):
        token = _MORE_CONTEXT.set(frozenset({"flow"}))
        try:
            _check_automation({"key": f"flow_{flow['key']}", "name": flow["label"], "trigger": {"type": "button", "table": t["key"]},
                               "conditions": [], "actions": flow["then"]}, tables, problems, documents, bp_pages, automations)
        finally:
            _MORE_CONTEXT.reset(token)
