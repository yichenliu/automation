"""Everything the platform stores.

A workspace's ERP design (its blueprint) is one JSON document, versioned. The records of every table the owner designs
live in one `record` table as JSON, so changing the design never touches the data: removing a field only hides it, and
restoring an earlier version brings it back. Files are stored on disk under the data folder and described here.
"""
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.types import JSON

from .core.passwords import check_password, hash_password
from .extensions import db

ROLES = ["portal", "viewer", "editor", "admin", "owner"]  # each can do everything the ones before it can
STAFF_ROLES = ROLES[1:]  # "portal": an outside person (a customer, a supplier) who sees only their own records in the portal


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SignsIn:
    """What a stored person needs to be signed in (app/web/http.py: current_user, login_user): an id, and being active
    (everyone is, here). Two of them are the same person when their ids are."""

    __hash__ = object.__hash__

    @property
    def is_active(self):
        return True

    @property
    def is_authenticated(self):
        return self.is_active

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)

    def __eq__(self, other):
        if isinstance(other, SignsIn):
            return self.get_id() == other.get_id()
        return NotImplemented

    def __ne__(self, other):
        equal = self.__eq__(other)
        return equal if equal is NotImplemented else not equal


class User(SignsIn, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(120), default="")
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    last_seen_at = db.Column(db.DateTime)
    totp_secret = db.Column(db.Text)  # encrypted; set once two-step sign-in is turned on
    ms_identity = db.Column(db.String(120), unique=True)  # "tenant id:object id" of the Microsoft account linked for sign-in
    sso_identity = db.Column(db.String(200), unique=True)  # the company identity provider's issuer|subject (single sign-on)
    notify_email = db.Column(db.Boolean, default=False)  # also email them their notifications (through Microsoft 365)

    def set_password(self, password):
        self.password_hash = hash_password(password)

    def check_password(self, password):
        return check_password(self.password_hash, password)

    @property
    def display(self):
        return self.name or self.email.split("@")[0]


class Workspace(db.Model):
    """One ERP: its current blueprint, the version it's at, its settings and small bits of running state."""
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    about = db.Column(db.Text, default="")
    blueprint = db.Column(db.JSON, nullable=False, default=dict)
    version = db.Column(db.Integer, nullable=False, default=0)
    timezone = db.Column(db.String(64))
    currency = db.Column(db.String(8))
    settings = db.Column(db.JSON, nullable=False, default=dict)  # business details, appearance, public form tokens
    state = db.Column(db.JSON, nullable=False, default=dict)  # schedules' last runs, mailbox positions
    created_at = db.Column(db.DateTime, default=utcnow)
    # When its schedules next need looking at (UTC), so the scheduler reads only the workspaces that are due instead of
    # every one every half minute. Empty: look now (a new workspace, or its design or time zone just changed).
    next_due_at = db.Column(db.DateTime, index=True)


ODD = "~odd"  # a value kept some other way (a map, a list of maps): never left out by what the index finds


def normalised(data):
    """A record's values as equality compares them: text stripped and lower-cased; a number (a link's id among them) in a
    list, so [12] finds it whether it's kept alone or among others; yes/no as it is; anything else as ODD, which every
    search through the index takes along (Python decides those)."""
    def one(v):
        if isinstance(v, str):
            return v.strip().lower()
        if v is None or isinstance(v, (bool, int, float)):
            return v
        return ODD
    out = {}
    for key, v in (data or {}).items():
        if str(key).startswith("_"):
            continue
        if isinstance(v, list):
            out[key] = [one(x) for x in v]
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = [v]
        else:
            out[key] = one(v)
    return out


@event.listens_for(Session, "before_flush")
def _keep_norm(session, context, instances):  # noqa: ARG001
    """Every record saved, however it's saved (the engine, an import, the platform's own fixes), keeps its values as
    they're compared: what a rule finds through the index is never out of date."""
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, Record):
            obj.norm = normalised(obj.data)


@event.listens_for(Workspace.blueprint, "set")
@event.listens_for(Workspace.timezone, "set")
def _schedules_look_again(target, value, old, initiator):
    """A new design (or time zone) can bring a schedule, or move one: the scheduler looks at the workspace again next time
    round instead of waiting for the time it worked out from the old one."""
    target.next_due_at = None


class Membership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    role = db.Column(db.String(16), nullable=False, default="editor")
    contact_id = db.Column(db.Integer)  # portal people: their own record (the customer or supplier they are)
    teams = db.Column(db.JSON)  # the keys of the design's teams they're in (Accounts payable)
    away_from = db.Column(db.Date)  # away (on leave): their approvals and tasks go to `cover_id` meanwhile
    away_until = db.Column(db.Date)
    cover_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=utcnow)
    user = db.relationship("User")
    __table_args__ = (db.UniqueConstraint("workspace_id", "user_id"),)

    def can(self, role):
        return ROLES.index(self.role) >= ROLES.index(role)


class Invite(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id"), nullable=False, index=True)
    email = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="editor")
    token = db.Column(db.String(64), unique=True, nullable=False)
    contact_id = db.Column(db.Integer)  # an invitation to the portal: whose records they'll see
    teams = db.Column(db.JSON)  # the teams they join (Finance), so what's meant for a team reaches them from the start
    invited_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    accepted_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)


class Version(db.Model):
    """A saved design. Every change, by chat, by hand or by going back, adds one; nothing is ever overwritten."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id"), nullable=False, index=True)
    number = db.Column(db.Integer, nullable=False)
    blueprint = db.Column(db.JSON, nullable=False)
    changes = db.Column(db.JSON, nullable=False, default=list)  # plain sentences: "Added table Clients (5 fields)"
    source = db.Column(db.String(40), default="chat")  # chat | restore | start | manual | discovery | import
    by = db.Column(db.String(200))
    message_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=utcnow)
    __table_args__ = (db.UniqueConstraint("workspace_id", "number"),)


class Feedback(db.Model):
    """Someone using an app said something isn't right: for the owner to see, and the builder to fix (and to test it on)."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    page = db.Column(db.String(300))  # where they were
    text = db.Column(db.Text, nullable=False)  # what they expected and what happened, in their words
    version = db.Column(db.Integer)  # the app's design when they said it
    status = db.Column(db.String(16), default="open")  # open | asked (the builder is on it) | done | dismissed
    message_id = db.Column(db.Integer)  # the builder turn asked to fix it
    created_at = db.Column(db.DateTime, default=utcnow)


class Message(db.Model):
    """One turn of a chat: the builder (designing the ERP) or the assistant (using it). Assistant turns are worked on
    in the background and their progress is shown live."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id"), nullable=False, index=True)
    channel = db.Column(db.String(16), nullable=False, default="builder")  # builder | assistant
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    role = db.Column(db.String(16), nullable=False)  # user | assistant
    text = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="done", index=True)  # queued | thinking | waiting_ai | done | failed
    page = db.Column(db.String(300))  # what the person was looking at when they asked
    files = db.Column(db.JSON, default=list)  # ids of files attached to a question
    thinking = db.Column(db.Text, default="")
    progress = db.Column(db.JSON, default=list)
    ops = db.Column(db.JSON, default=list)
    changes = db.Column(db.JSON, default=list)
    results = db.Column(db.JSON)  # what a search showed
    proposals = db.Column(db.JSON)  # assistant: changes to records it suggests, waiting for a click
    checks = db.Column(db.JSON)  # builder: what trying the app out showed ({"examples", "issues", "fixes"})
    navigate = db.Column(db.String(300))
    version_before = db.Column(db.Integer)
    version_after = db.Column(db.Integer)
    error = db.Column(db.Text)
    next_try = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    claimed_by = db.Column(db.String(120))  # the worker doing this turn (leases.py); nobody else takes it until lease_until
    lease_until = db.Column(db.DateTime)


class Record(db.Model):
    """One record of any table. `version` goes up with every save, and a save made from an out-of-date copy is refused
    (two people editing the same invoice can't silently overwrite each other)."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id"), nullable=False)
    table = db.Column(db.String(64), nullable=False)
    data = db.Column(JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict)  # JSONB on PostgreSQL (indexed, GIN); plain JSON in the simulator
    # its values as they're compared (text trimmed and lower-cased, a link's id always in a list): kept with every save
    # (normalised(), below), so a rule's "equals" finds them through one GIN index on PostgreSQL. None: not yet kept
    norm = db.Column(JSON().with_variant(JSONB(), "postgresql"))
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_by = db.Column(db.String(120))
    created_by_id = db.Column(db.Integer)  # the person who added it (for "only their own" access; names can change)
    updated_by = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow)
    deleted_at = db.Column(db.DateTime)
    __table_args__ = (db.Index("ix_record_ws_table", "workspace_id", "table", "deleted_at"),)
    __mapper_args__ = {"version_id_col": version}


class RecordChange(db.Model):
    """The history of every record: who or what changed which field, from what to what."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    record_id = db.Column(db.Integer, db.ForeignKey("record.id"), nullable=False, index=True)
    kind = db.Column(db.String(16), nullable=False)  # created | updated | deleted | restored
    changes = db.Column(db.JSON, default=dict)  # {field: [old, new]}
    by = db.Column(db.String(200))  # "Ethan", "automation “Draft invoice”", "builder chat", "public form"
    by_id = db.Column(db.Integer)  # the person, when it was one (who moved it to a stage, for separation of duties)
    created_at = db.Column(db.DateTime, default=utcnow)


class Counter(db.Model):
    """The last number handed out by an autonumber field (INV-0001, INV-0002...)."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False)
    table = db.Column(db.String(64), nullable=False)
    field = db.Column(db.String(64), nullable=False)
    value = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (db.UniqueConstraint("workspace_id", "table", "field"),)


class File(db.Model):
    """A file kept by the platform: attached to a record, generated (a PDF invoice), imported, or from Microsoft 365."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    record_id = db.Column(db.Integer, index=True)
    field = db.Column(db.String(64))  # the file field it belongs to ("" for general attachments)
    name = db.Column(db.String(255), nullable=False)
    mime = db.Column(db.String(120))
    size = db.Column(db.Integer, default=0)
    sha256 = db.Column(db.String(64))
    path = db.Column(db.String(500), nullable=False)  # relative to the data folder
    source = db.Column(db.String(30), default="upload")  # upload | generated | email | onedrive | chat | form | knowledge
    by = db.Column(db.String(200))
    uploaded_by_id = db.Column(db.Integer)  # who put it here (a file not attached to a record is theirs and the admins')
    created_at = db.Column(db.DateTime, default=utcnow)
    deleted_at = db.Column(db.DateTime)


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    record_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    text = db.Column(db.Text, nullable=False)
    shared = db.Column(db.Boolean, default=False)  # seen in the portal too (portal people's own messages always are)
    created_at = db.Column(db.DateTime, default=utcnow)
    user = db.relationship("User")


class Run(db.Model):
    """One run of an automation: what started it, each step it took, and how it ended."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    automation = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(200))
    record_id = db.Column(db.Integer)
    table = db.Column(db.String(64))
    trigger = db.Column(db.String(300))
    context = db.Column(db.JSON, default=dict)  # an email or file that started it; records created along the way
    status = db.Column(db.String(20), default="queued", index=True)  # queued | running | waiting_ai | waiting_approval | done | skipped | failed
    steps = db.Column(db.JSON, default=list)
    error = db.Column(db.Text)
    depth = db.Column(db.Integer, default=0)
    next_try = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)
    finished_at = db.Column(db.DateTime)
    claimed_by = db.Column(db.String(120))  # the worker (or the save) carrying it out now; nobody else does until lease_until
    lease_until = db.Column(db.DateTime)


class FlowRun(db.Model):
    """Someone part way through a flow (receiving a delivery, a stock count): where they've got to and what they've
    entered, scanned and counted so far. Nothing reaches the records until they finish."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    flow = db.Column(db.String(64), nullable=False)
    record_id = db.Column(db.Integer)  # the record it's about (none yet for one that makes a new record)
    user_id = db.Column(db.Integer, nullable=False)
    at = db.Column(db.Integer, default=0)  # the step they're on
    data = db.Column(db.JSON, default=dict)  # each step's answers, by the step's key
    status = db.Column(db.String(20), default="open")  # open | done | cancelled
    result_id = db.Column(db.Integer)  # the record it saved to, once done
    task_id = db.Column(db.Integer)  # the task (an Approval) doing this flow finishes
    run_id = db.Column(db.Integer)  # the rule run of its "then" steps
    created_at = db.Column(db.DateTime, default=utcnow)
    finished_at = db.Column(db.DateTime)


class Approval(db.Model):
    """A person's decision (or a task for them) an automation is waiting for."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    run_id = db.Column(db.Integer, db.ForeignKey("run.id"), nullable=False)
    record_id = db.Column(db.Integer)
    table = db.Column(db.String(64))
    message = db.Column(db.Text)
    assignee_id = db.Column(db.Integer, db.ForeignKey("user.id"))  # a person, or anyone with `role`
    role = db.Column(db.String(16))
    kind = db.Column(db.String(16), default="approval")  # approval | task | review (checking the AI's work)
    step = db.Column(db.String(40))  # which step of the run it's for: "2", or "3.0.1" on a branch's path
    fields = db.Column(db.JSON)  # a task: the record's fields the person fills in to finish it
    history = db.Column(db.JSON, default=list)  # [{"when", "what", "by"}]: reminded, passed up, passed on
    # a check of the AI's work: {"table", "record_id", "values", "why", "unsure", "created"}, and once checked its
    # "outcome" (right | corrected | wrong) with what was "changed"
    proposal = db.Column(db.JSON)
    # pending | approved | rejected | done (a task) | cancelled (someone else decided) | escalated (passed up, overdue)
    status = db.Column(db.String(16), default="pending")
    decided_by = db.Column(db.String(200))
    decided_by_id = db.Column(db.Integer)  # who answered (a rule fills an "Approved by" field with them)
    comment = db.Column(db.Text)
    decided_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)
    # a work queue: how pressing a task is (0 low … 3 urgent), "not now" until when, who in its team or role took it,
    # and the flow it's done through (a stock count that is the task)
    priority = db.Column(db.Integer, default=1)
    deferred_until = db.Column(db.DateTime)
    taken_by_id = db.Column(db.Integer)
    flow = db.Column(db.String(64))


class AuditEvent(db.Model):
    """One entry of the audit log (audit.py): who, what, when, from where; chained to the one before by fingerprints."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, index=True)  # none: the platform's own (a sign-in before a workspace is open)
    user_id = db.Column(db.Integer)
    by = db.Column(db.String(200))
    action = db.Column(db.String(32), nullable=False)
    detail = db.Column(db.Text)
    ip = db.Column(db.String(64))
    at = db.Column(db.DateTime, nullable=False)
    previous = db.Column(db.String(64))
    fingerprint = db.Column(db.String(64))


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer)  # who it's for; none = everyone in the workspace
    kind = db.Column(db.String(20), default="info")  # info | approval | mention | error
    text = db.Column(db.Text, nullable=False)
    link = db.Column(db.String(300))
    read_by = db.Column(db.JSON, default=list)  # user ids who've seen it
    emailed_at = db.Column(db.DateTime)  # when it was emailed to the people who asked for that (or found there was no one)
    created_at = db.Column(db.DateTime, default=utcnow)


class Connection(db.Model):
    """A signed-in outside system (Microsoft 365). Tokens are kept encrypted."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    kind = db.Column(db.String(30), nullable=False)
    account = db.Column(db.String(255))
    status = db.Column(db.String(20), default="not connected")  # not connected | connected | needs sign-in
    problem = db.Column(db.Text)
    secret = db.Column(db.Text)  # encrypted JSON: tokens
    state = db.Column(db.JSON, default=dict)  # mailbox and folder positions for triggers
    connected_by = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=utcnow)
    checked_at = db.Column(db.DateTime)
    __table_args__ = (db.UniqueConstraint("workspace_id", "kind"),)


class Job(db.Model):
    """Background work that can take a while: imports, reading a business's files, AI fields, document reading."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, index=True)
    kind = db.Column(db.String(40), nullable=False)
    payload = db.Column(db.JSON, default=dict)
    status = db.Column(db.String(20), default="queued", index=True)  # queued | running | waiting_ai | done | failed
    stage = db.Column(db.String(300))
    progress = db.Column(db.Integer, default=0)
    result = db.Column(db.JSON)
    error = db.Column(db.Text)
    attempts = db.Column(db.Integer, default=0)
    run_after = db.Column(db.DateTime, default=utcnow)
    dedupe = db.Column(db.String(120), index=True)
    created_by = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=utcnow)
    finished_at = db.Column(db.DateTime)
    claimed_by = db.Column(db.String(120))  # the worker doing it (leases.py); nobody else takes it until lease_until
    lease_until = db.Column(db.DateTime)


class ScheduleRun(db.Model):
    """A schedule's time that has come and been taken (8 am on 5 Nov for "every day at 8"). Only one row can exist for
    each time, so however many processes look, the one whose row goes in is the one that runs it."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False)
    automation_key = db.Column(db.String(64), nullable=False)
    slot = db.Column(db.DateTime, nullable=False)  # the schedule's own time, in the workspace's time zone
    created_at = db.Column(db.DateTime, default=utcnow)
    __table_args__ = (db.UniqueConstraint("workspace_id", "automation_key", "slot"),)


class Outbox(db.Model):
    """An email or text message a rule sends. The rule's step writes it here as it runs (in the same save), and the worker
    delivers it, trying again a little later each time if the mailbox can't be reached; what happened is kept here and on
    the run's step."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    run_id = db.Column(db.Integer, index=True)
    channel = db.Column(db.String(8), nullable=False, default="email")  # email | sms
    to = db.Column(db.Text, nullable=False)
    cc = db.Column(db.Text)
    subject = db.Column(db.Text)
    body = db.Column(db.Text)
    attachments = db.Column(db.JSON, default=list)  # [{"file": id, "name"}] or a report made for it: [{"name", "mime", "data"}]
    status = db.Column(db.String(12), nullable=False, default="queued", index=True)  # queued | sending | sent | failed
    attempts = db.Column(db.Integer, default=0)
    next_try = db.Column(db.DateTime, default=utcnow)
    error = db.Column(db.Text)
    result = db.Column(db.Text)  # what happened, in words, as the run's step says it
    reported = db.Column(db.Boolean, default=False)  # what happened has been written on the run's step
    claimed_by = db.Column(db.String(120))
    lease_until = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)
    sent_at = db.Column(db.DateTime)


class RateHit(db.Model):
    """Attempts counted for a rate limit (sign-ins, API calls), by short time slices, so every web process counts the
    same ones. `key` is a fingerprint of the limit and who it's for (no addresses or emails are kept)."""
    key = db.Column(db.String(64), primary_key=True)
    bucket = db.Column(db.BigInteger, primary_key=True)  # when the slice starts, in seconds since 1970
    count = db.Column(db.Integer, nullable=False, default=0)


class Chunk(db.Model):
    """A passage the AI can find by meaning or by words: part of a document, a record written out as text, an email.
    `embedding` is the passage's vector (pgvector, unit length) from the embedding model, `dims` its length (each length
    has its own HNSW index, as models differ); `words` is its full-text form (PostgreSQL's, kept up by the database)."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False)
    source = db.Column(db.String(16), nullable=False)  # file | record | email
    ref = db.Column(db.String(200), nullable=False)  # the file id, record id or message id it came from
    table = db.Column(db.String(64))  # for records, and files attached to records: whose permissions apply
    record_id = db.Column(db.Integer)
    title = db.Column(db.String(300), default="")
    text = db.Column(db.Text, nullable=False)
    position = db.Column(db.Integer, default=0)
    link = db.Column(db.String(500))  # where to see it: "table:invoices:record:7", "file:12" or a web address
    embedding = db.Column(Vector())
    dims = db.Column(db.Integer)
    model = db.Column(db.String(80))
    words = db.Column(TSVECTOR, db.Computed("setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
                                            "setweight(to_tsvector('english', coalesce(text, '')), 'B')", persisted=True))
    created_at = db.Column(db.DateTime, default=utcnow)
    __table_args__ = (db.Index("ix_chunk_ws_source_ref", "workspace_id", "source", "ref"),)


class IndexTask(db.Model):
    """Something to (re)index for the AI: a record that changed, a file that arrived, an email. One row per thing, so a
    record saved ten times is indexed once; the worker takes them in batches."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False)
    source = db.Column(db.String(16), nullable=False)
    ref = db.Column(db.String(200), nullable=False)
    data = db.Column(db.JSON)  # an email's text, which isn't kept anywhere else
    attempts = db.Column(db.Integer, default=0)
    run_after = db.Column(db.DateTime, default=utcnow)
    created_at = db.Column(db.DateTime, default=utcnow)
    __table_args__ = (db.UniqueConstraint("workspace_id", "source", "ref"),)


class ApiKey(db.Model):
    """A key another system uses to read and write this workspace's records through the API. Only a hash is kept; the
    key itself is shown once, when it's made."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    prefix = db.Column(db.String(16), nullable=False, index=True)
    hash = db.Column(db.String(64), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="editor")
    created_by = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=utcnow)
    last_used_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)


class Follow(db.Model):
    """Someone following a record: they're told when anyone else changes it."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, nullable=False)
    record_id = db.Column(db.Integer, nullable=False, index=True)
    __table_args__ = (db.UniqueConstraint("user_id", "record_id"),)


class SavedView(db.Model):
    """A person's own view of a table: the filters, search, sorting and grouping they keep coming back to."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    table = db.Column(db.String(64), nullable=False)
    label = db.Column(db.String(80), nullable=False)
    params = db.Column(db.String(1000), default="")  # the view's address after the ?


class Suggestion(db.Model):
    """An improvement the analyst found by watching how the business uses its ERP, with the evidence for it and the
    design changes that make it. Applying one saves a new design version (undoable); dismissing one means it isn't
    suggested again."""
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    kind = db.Column(db.String(30), nullable=False)  # unused_field | text_to_select | default_value | follow_up | stuck | failing | question | email | analyst | ai_trust
    signature = db.Column(db.String(200), nullable=False)  # the same finding found again is the same suggestion
    title = db.Column(db.String(300), nullable=False)
    why = db.Column(db.Text, default="")
    evidence = db.Column(db.JSON, default=list)  # sentences: what was seen
    proof = db.Column(db.JSON)  # replaying it on the business's history: {"summary", "fired", "matched", ...}
    ops = db.Column(db.JSON, default=list)
    changes = db.Column(db.JSON, default=list)  # what applying it changes, in plain sentences
    status = db.Column(db.String(16), default="open")  # open | applied | dismissed | stale
    version_after = db.Column(db.Integer)
    decided_by = db.Column(db.String(200))
    decided_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)
    __table_args__ = (db.UniqueConstraint("workspace_id", "signature"),)
