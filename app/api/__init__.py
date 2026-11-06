"""The FastAPI front (Keel v2, D9): the platform as an ASGI app (`app.asgi`), serving many connections without holding a
thread for each one that's waiting on the model.

    /api/v2/language              GET   what can be written in the Keel language
    /api/v2/app                   GET   the app, in the language's terms, and its version
    /api/v2/app/what-if           POST  {patches}: what they'd do, what's refused and why, every check acted out
    /api/v2/app/apply             POST  {patches, version}: the same, saved as a new version
    /api/v2/app/test              POST  every check the app has, acted out now
    /api/v2/app/undo              POST  {version}: back to an earlier version
    /api/v2/builder/{id}/events   GET   a builder turn's progress as it happens (server-sent events)
    the pages                     every screen, the v1 API (/api/v1), MCP, webhooks and /static: the page routers of app/web
                                  (app/web/http.py, docs/WEB.md)

Each call is checked against an API key (Authorization: Bearer keel_…). Changing the app needs an admin's key. The
tools are the same ones the builder and MCP use (app/lang/tools.py).
"""
import asyncio
import hashlib
import hmac
import json

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


class Caller(BaseModel):
    workspace_id: int
    role: str
    name: str


def create(app=None):
    """The platform (made here if not given): an ASGI app, handing each call to its FastAPI app (`app.asgi`)."""
    if app is None:
        from .. import create_app
        app = create_app()
    return app


def front(platform):
    """The FastAPI app: the v2 routes, then the platform's pages (app/web)."""
    from ..lang import tools

    api = FastAPI(title=platform.config.get("PLATFORM_NAME") or "Keel", version="2",
                  description="Build and change a business app in the Keel language, with every change checked and proven.")

    def within(fn, *args, **kwargs):
        """Core work runs in the platform's own context (its database session and settings)."""
        with platform.app_context():
            from ..extensions import db
            try:
                return fn(*args, **kwargs)
            finally:
                db.session.remove()

    def caller(authorization: str = Header(default="")) -> Caller:
        plain = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        parts = plain.split("_")
        if len(parts) < 3 or parts[0] != "keel":
            raise HTTPException(401, "Send your API key as: Authorization: Bearer keel_...")

        def check():
            from ..models import ApiKey
            key = ApiKey.query.filter_by(prefix=parts[1], revoked_at=None).first()
            if key is None or not hmac.compare_digest(key.hash, hashlib.sha256(plain.encode()).hexdigest()):
                return None
            return Caller(workspace_id=key.workspace_id, role=key.role, name=key.name)
        found = within(check)
        if found is None:
            raise HTTPException(401, "That API key isn't valid (it may have been revoked).")
        return found

    def designer(who: Caller = Depends(caller)) -> Caller:
        if who.role != "admin":
            raise HTTPException(403, "Changing the app itself needs an admin's API key")
        return who

    def on_app(who, fn, *args):
        def run():
            from ..core.actor import acting
            from ..extensions import db
            from ..models import Workspace
            ws = db.session.get(Workspace, who.workspace_id)
            with acting(workspace_id=ws.id):  # the AI's turns are this app's, the audit log says it was this key
                return fn(ws, *args)
        return within(run)

    class Patches(BaseModel):
        patches: list[dict]

    class Applying(Patches):
        version: int

    class Undoing(BaseModel):
        version: int

    @api.get("/api/v2/language", tags=["language"], summary=" ".join((tools.language.__doc__ or "").split()))
    def language():
        return tools.language()

    @api.get("/api/v2/app", tags=["app"], summary=" ".join((tools.describe.__doc__ or "").split()))
    def describe(who: Caller = Depends(caller)):
        return on_app(who, tools.describe)

    @api.post("/api/v2/app/what-if", tags=["app"], summary=" ".join((tools.what_if.__doc__ or "").split()))
    def what_if(body: Patches, who: Caller = Depends(designer)):
        return on_app(who, tools.what_if, body.patches)

    @api.post("/api/v2/app/apply", tags=["app"], summary=" ".join((tools.apply.__doc__ or "").split()))
    def apply(body: Applying, who: Caller = Depends(designer)):
        return on_app(who, lambda ws: tools.apply(ws, body.patches, body.version, by=f"API key “{who.name}”"))

    @api.post("/api/v2/app/test", tags=["app"], summary=" ".join((tools.test.__doc__ or "").split()))
    def test(who: Caller = Depends(caller)):
        return on_app(who, tools.test)

    class Explaining(BaseModel):
        record: int | None = None
        check: str | None = None

    @api.post("/api/v2/app/explain", tags=["app"], summary=" ".join((tools.explain.__doc__ or "").split()))
    def explain(body: Explaining, who: Caller = Depends(caller)):
        return on_app(who, lambda ws: tools.explain(ws, body.record, body.check))

    @api.post("/api/v2/app/undo", tags=["app"], summary=" ".join((tools.undo.__doc__ or "").split()))
    def undo(body: Undoing, who: Caller = Depends(designer)):
        return on_app(who, lambda ws: tools.undo(ws, body.version, by=f"API key “{who.name}”"))

    @api.get("/api/v2/builder/{message_id}/events", tags=["builder"],
             summary="A builder turn's progress as it happens: each new line, then its reply when it's done")
    async def events(message_id: int, who: Caller = Depends(caller)):
        def look():
            from ..extensions import db
            from ..models import Message
            m = db.session.get(Message, message_id)
            if m is None or m.workspace_id != who.workspace_id:
                return None
            return {"status": m.status, "progress": list(m.progress or []), "text": m.text or "", "error": m.error or ""}

        if within(look) is None:
            raise HTTPException(404, "There's no such builder turn")

        async def stream():
            said = 0
            while True:
                now = await asyncio.to_thread(within, look)  # the wait is a timer, not a thread held for each watcher
                for line in now["progress"][said:]:
                    yield f"event: progress\ndata: {json.dumps(line, ensure_ascii=False)}\n\n"
                said = len(now["progress"])
                if now["status"] in ("done", "failed"):
                    yield f"event: {now['status']}\ndata: {json.dumps({'text': now['text'], 'error': now['error']}, ensure_ascii=False)}\n\n"
                    return
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    # the pages (app/web/http.py; docs/WEB.md): one module a line, each router a line below
    from ..web import http
    from ..web import auth, account, workspaces
    from ..web import erp_records
    from ..web import erp_flows
    from ..web import erp_learn
    from ..web import erp_pages
    from ..web import erp_studio
    from ..web import erp_ai
    from ..web import settings
    from ..web import builder
    from ..web import forms
    from ..web import connect
    from ..web import connect_apps
    from ..web import api as v1
    from ..web import mcp  # noqa: F401 (adds /api/v1/mcp and /api/v1/openapi.json to api.router)
    from ..web import portal
    from ..web import static
    http.install(api, platform, [
        auth.router,
        account.router,
        workspaces.router,
        erp_records.router,
        erp_flows.router,
        erp_learn.router,
        erp_pages.router,
        erp_studio.router,
        erp_ai.router,
        settings.router,
        builder.router,
        forms.router,
        connect.router,
        connect_apps.router,
        connect_apps.stripe,
        v1.router,
        v1.hooks,
        portal.router,
        portal.join,
        static.router,
    ])
    api.state.platform = platform
    return api
