"""One customer's data kept from another's by PostgreSQL itself (Keel v2, D10): row-level security, a second lock
behind the code's own workspace filters, so a query that forgets its filter still can't reach another workspace.

Each transaction is told whose it is. When the code runs for one workspace (a person's request in it, an API key's
call), `keel.workspace` is set at the transaction's start, and the tenant tables show and take only that workspace's
rows. Work that spans workspaces runs with it unset:
- the scheduler going through every app;
- a worker before it knows the job's workspace;
- making a test copy from its live app, inside `wide()`.
Policies are kept by migrate (policies()); TENANCY_RLS=false turns the setting off.
"""
import contextlib
import contextvars

from sqlalchemy import event, orm, text

TENANT_TABLES = ("record", "record_change", "file", "run", "approval", "chunk", "notification", "comment", "follow", "flow_run")
SETTING = "keel.workspace"
_wide = contextvars.ContextVar("keel_tenancy_wide", default=False)
MINE = (f"coalesce(current_setting('{SETTING}', true), '') = '' "
        f"OR workspace_id = nullif(current_setting('{SETTING}', true), '')::int")


def policies(tables=TENANT_TABLES):
    """The statements that keep the policies (idempotent: run on every start)."""
    out = []
    for t in tables:
        out.append(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = '{t}' AND policyname = 'keel_tenant') "
                   f"THEN CREATE POLICY keel_tenant ON \"{t}\" USING ({MINE}) WITH CHECK ({MINE}); END IF; END $$")
        out.append(f'ALTER TABLE "{t}" ENABLE ROW LEVEL SECURITY')
        out.append(f'ALTER TABLE "{t}" FORCE ROW LEVEL SECURITY')
    return out


def _on(connection):
    if connection.dialect.name != "postgresql" or _wide.get():
        return False
    try:
        from .settings import settings
        return str(settings().get("TENANCY_RLS", True)).lower() not in ("false", "0", "no")
    except RuntimeError:
        return False


@event.listens_for(orm.Session, "after_begin")
def _whose(session, transaction, connection):
    """At each transaction's start: the workspace it runs for, if it runs for one."""
    if not _on(connection):
        return
    from .actor import workspace_id
    ws = workspace_id()
    if ws:
        connection.execute(text("SELECT set_config(:name, :ws, true)"), {"name": SETTING, "ws": str(int(ws))})


@contextlib.contextmanager
def wide(session=None):
    """Work that rightly spans workspaces (a test copy made from its live app): no workspace set while it runs."""
    token = _wide.set(True)
    try:
        if session is not None and session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT set_config(:name, '', true)"), {"name": SETTING})
        yield
    finally:
        _wide.reset(token)


def forget(session):
    """The workspace this session's open transaction was told it's for, cleared (a test carrying on after a request in the
    same transaction, as no request)."""
    actual = session.registry() if hasattr(session, "registry") else session  # a scoped session's own
    if actual.in_transaction() and actual.get_bind().dialect.name == "postgresql":
        actual.execute(text("SELECT set_config(:name, '', true)"), {"name": SETTING})
