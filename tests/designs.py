"""A consultancy's ERP, written the loose way a model might write it."""
CONSULTANCY = [
    {"op": "create_table", "table": "clients", "label": "Clients", "module": "sales", "fields": [
        {"key": "id", "label": "ID", "type": "number"},
        {"key": "name", "label": "Name", "type": "string", "required": True},
        {"label": "Status", "type": "dropdown", "options": "Lead, Active, Past", "default": "Lead"},
        {"key": "email", "type": "e-mail"},
        {"key": "payment_terms", "label": "Payment terms (days)", "type": "integer", "default": 30}]},
    {"op": "create_table", "table": "projects", "module": "projects", "fields": [
        {"key": "name", "type": "text", "required": True},
        {"key": "client", "type": "relation", "to": "clients"},
        {"key": "status", "type": "status", "options": ["Planned", "Active", "Done"], "default": "Planned"},
        {"key": "hours", "type": "number"}, {"key": "rate", "type": "money"},
        {"key": "value", "type": "computed", "formula": "IF({hours} > 0, hours * rate, 0)", "format": "currency"},
        {"key": "client_terms", "type": "formula", "expression": "client.payment_terms"}]},
    {"op": "create_table", "table": "invoices", "module": "finance", "fields": [
        {"key": "number", "type": "autonumber", "prefix": "INV-"},
        {"key": "project", "type": "link", "table": "projects", "required": True},
        {"key": "amount", "type": "money"},
        {"key": "status", "type": "select", "options": ["Draft", "Sent", "Paid"], "default": "Draft"},
        {"key": "due_date", "type": "date"},
        {"key": "days_overdue", "type": "formula", "expression": "iif(status != 'Paid' and due_date < today(), days_between(due_date, today()), 0)"}]},
    {"op": "add_field", "table": "projects", "field": {"key": "invoiced", "type": "sum", "table": "invoices", "via": "project", "field": "amount"}},
    {"op": "add_field", "table": "clients", "field": {"key": "projects", "label": "Projects", "type": "rollup", "table": "projects", "via": "client", "aggregate": "count"}},
    {"op": "add_automation", "automation": {"name": "Invoice when project done",
        "trigger": {"type": "field_changed", "table": "projects", "field": "status", "to": "Done"},
        "actions": [{"type": "create", "table": "invoices", "values": {"project": "{{record}}", "amount": "{{record.value}}",
                                                                      "due_date": "{{add_days(today(), record.client_terms)}}"}},
                    {"type": "alert", "message": "Invoice drafted for {{record.name}}"}]}},
    {"op": "add_page", "page": {"label": "Overview", "widgets": [
        {"type": "kpi", "label": "Active clients", "table": "clients", "filters": [{"field": "status", "op": "=", "value": "Active"}]},
        {"type": "metric", "label": "Unpaid", "table": "invoices", "aggregate": "sum", "field": "amount", "filters": [{"field": "status", "op": "not_equals", "value": "Paid"}]},
        {"type": "bar", "label": "Projects by status", "table": "projects", "group_by": "status"},
        {"type": "list", "label": "Overdue invoices", "table": "invoices", "filters": [{"field": "due_date", "op": "before_today"}, {"field": "status", "op": "not_equals", "value": "Paid"}]}]}},
]
