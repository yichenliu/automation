"""The platform as an ASGI app (Keel v2): `uvicorn asgi:app --workers 4`. FastAPI serves the v2 API and streams;
the Flask app is mounted under it while its pages move over (app/api/__init__.py)."""
from app.api import create

app = create()
