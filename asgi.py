"""The platform as an ASGI app (Keel v2): `uvicorn asgi:app --workers 4`. One FastAPI app serves the pages, the v1 API,
MCP and the v2 API (app/api; the pages are app/web's routers, docs/WEB.md)."""
from app.api import create

app = create().asgi
