"""The icons pages can use (Lucide, ISC licence; the sprite is app/static/icons.svg), and the words the AI may use
for them. `icon_for` picks a sensible icon for a table the AI didn't give one."""

NAMES = ['arrow-right', 'arrow-up-down', 'baby', 'book-open', 'code', 'webhook', 'badge-dollar-sign', 'bed', 'bell', 'bot', 'box', 'boxes', 'briefcase', 'building-2', 'calculator', 'calendar-check', 'calendar', 'camera', 'car', 'chart-column', 'chart-gantt', 'chart-line', 'chart-pie', 'check', 'chevron-down', 'chevron-left', 'chevron-right', 'circle-check', 'circle-dot', 'circle-help', 'clipboard-list', 'clock', 'cloud', 'coffee', 'copy', 'credit-card', 'dog', 'dollar-sign', 'download', 'droplets', 'dumbbell', 'ellipsis', 'external-link', 'eye', 'factory', 'file-check', 'file-down', 'file-plus', 'file-spreadsheet', 'file-text', 'filter', 'flag', 'folder-kanban', 'gavel', 'globe', 'graduation-cap', 'grip-vertical', 'hammer', 'hand-coins', 'handshake', 'hard-drive', 'hash', 'heart-pulse', 'history', 'hotel', 'house', 'image', 'inbox', 'info', 'key', 'landmark', 'layers', 'layout-dashboard', 'layout-grid', 'leaf', 'link', 'list-todo', 'list', 'lock', 'log-out', 'mail', 'map-pin', 'menu', 'message-square', 'moon', 'music', 'package', 'paperclip', 'pencil', 'percent', 'phone', 'piggy-bank', 'plane', 'play', 'plus', 'printer', 'receipt', 'refresh-cw', 'scale', 'scissors', 'search', 'send', 'settings', 'shield', 'shirt', 'shopping-cart', 'sparkles', 'square-check', 'square-kanban', 'star', 'stethoscope', 'store', 'sun', 'table', 'tag', 'tags', 'target', 'ticket', 'trash-2', 'triangle-alert', 'truck', 'type', 'undo-2', 'upload', 'user-plus', 'user', 'users', 'utensils', 'wallet', 'wand-sparkles', 'wrench', 'x', 'zap']

ALIASES = {
    "people": "users", "clients": "users", "customers": "users", "contacts": "users", "team": "users", "staff": "users",
    "employees": "users", "person": "user", "project": "folder-kanban", "projects": "folder-kanban", "time": "clock",
    "timesheet": "clock", "timesheets": "clock", "hours": "clock", "invoice": "receipt", "invoices": "receipt",
    "bill": "receipt", "bills": "receipt", "expense": "receipt", "expenses": "receipt", "document": "file-text",
    "documents": "file-text", "quote": "file-text", "quotes": "file-text", "contract": "file-check", "contracts": "file-check",
    "spreadsheet": "file-spreadsheet", "product": "package", "products": "package", "inventory": "boxes", "stock": "boxes",
    "delivery": "truck", "deliveries": "truck", "shipping": "truck", "order": "shopping-cart", "orders": "shopping-cart",
    "sales": "badge-dollar-sign", "shop": "store", "company": "building-2", "companies": "building-2", "supplier": "factory",
    "suppliers": "factory", "maintenance": "wrench", "job": "hammer", "jobs": "hammer", "work": "hammer",
    "schedule": "calendar", "bookings": "calendar-check", "booking": "calendar-check", "appointments": "calendar-check",
    "task": "square-check", "tasks": "list-todo", "todo": "list-todo", "email": "mail", "location": "map-pin",
    "property": "house", "properties": "house", "home": "house", "vehicle": "car", "vehicles": "car", "money": "dollar-sign",
    "payment": "credit-card", "payments": "credit-card", "finance": "landmark", "bank": "landmark", "budget": "piggy-bank",
    "report": "chart-column", "reports": "chart-column", "dashboard": "layout-dashboard", "overview": "layout-dashboard",
    "goal": "target", "goals": "target", "leads": "target", "category": "tag", "categories": "tags", "assets": "box",
    "checklist": "clipboard-list", "inspections": "clipboard-list", "training": "graduation-cap", "students": "graduation-cap",
    "patients": "heart-pulse", "health": "stethoscope", "menu": "utensils", "cafe": "coffee", "travel": "plane",
    "rooms": "bed", "keys": "key", "compliance": "shield", "safety": "shield", "reviews": "star", "issues": "flag",
    "tickets": "ticket", "deals": "handshake", "opportunities": "handshake", "legal": "gavel", "approvals": "circle-check",
    "settings": "settings", "notes": "message-square", "messages": "message-square", "files": "paperclip",
}


def resolve(name):
    """A known icon name for whatever the AI wrote, or None."""
    key = str(name or "").strip().lower().replace("_", "-").replace(" ", "-")
    if key in NAMES:
        return key
    return ALIASES.get(key) or ALIASES.get(key.rstrip("s")) or None


def icon_for(label):
    for word in str(label or "").lower().replace("_", " ").split():
        found = resolve(word)
        if found:
            return found
    return "table"
