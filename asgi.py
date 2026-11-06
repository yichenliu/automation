"""The platform as an ASGI app (Keel v2): `uvicorn asgi:app --workers 4`. One app serves the pages, the v1 API, MCP
and, in front of them, the v2 API (FastAPI, app/api); the pages are native FastAPI routes (app/web/http.py) or, until
each is converted (docs/WEB.md), the platform's old web kit (app/webkit)."""
from app.api import create

app = create()
