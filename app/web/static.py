"""The platform's own files (app/static): its styles, scripts, icons and the map's library, at /static/<file>. The
browser checks each again on every use (no-cache) and is told when it hasn't changed."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..core.context import current_app
from .http import PageRoute, send_file

router = APIRouter(route_class=PageRoute)


@router.api_route("/static/{filename:path}", methods=["GET"], name="static")
def static(request: Request, filename: str):
    root = Path(current_app.static_folder).resolve()
    target = (root / filename).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404)
    return send_file(request, target, max_age=current_app.config.get("SEND_FILE_MAX_AGE_DEFAULT"))
