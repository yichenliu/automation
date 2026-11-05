"""What the AI knows about the business beyond its tables' numbers: its documents (policies, price lists, contracts,
manuals, anything uploaded or attached), its email, and every record written out as text. The assistant, the AI steps
of automations and the analyst look things up here before they answer (retrieval-augmented generation), and say which
sources they used.

- Indexing: anything that changes is marked (`touch`) and the worker reads it in batches. Its text is cut into passages
  (Chunk rows); each passage gets a vector from the embedding model, and its words go into a full-text index.
- Finding (`search`): by words (PostgreSQL full-text search: stemmed, titles weighted above text, ranked by cover
  density) and by meaning (pgvector: cosine distance, through an HNSW index for each vector length), the two rankings
  combined by reciprocal rank fusion, then limited to what the person asking is allowed to see. If the embedding model
  can't be reached, finding by words still works and the vectors are added when it's back.
"""
import json
import logging
import re
from datetime import timedelta

import numpy as np
from sqlalchemy import func
from sqlalchemy import text as sql

from . import ai, jobs
from .blueprint import schema
from .extensions import db
from .models import Chunk, Comment, File, IndexTask, Record, Workspace, utcnow
from .core.settings import settings

log = logging.getLogger("knowledge")
PASSAGE = 1400  # characters per passage
OVERLAP = 200
BATCH = 24  # things indexed per pass of the worker
EMBED_BATCH = 16
MIN_SIMILARITY = 0.42  # a passage found only by meaning, less similar than this, isn't really about the question
CLOSE_TO_BEST = 0.08  # nor is one much less similar than the best match (bge-m3 scores loosely related text ~0.45-0.5)
FUSION_K = 60
SKIP_FILES = {"generated"}  # PDFs the platform made from records: the records themselves are indexed
STOP = set("a an and are as at be by can do does for from has have how i in is it its me my of on or our so that the "
           "their them there these this to was we what when where which who why will with you your".split())
_indexed = set()  # the vector lengths with an HNSW index made (each made once, when passages of that length come)


# --- marking things to index -------------------------------------------------------------------------------------------

def touch(workspace_id, source, refs, data=None):
    """Mark things to (re)index: records that changed, a file that arrived, an email (its text in `data`, since email
    isn't kept anywhere else). Marking twice is harmless: each thing is indexed once, as it is when the worker gets to
    it."""
    refs = [str(r) for r in (refs if isinstance(refs, (list, set, tuple)) else [refs]) if r is not None]
    if not refs or workspace_id is None:
        return
    from sqlalchemy.dialects.postgresql import insert
    now = utcnow()
    for ref in refs:
        statement = insert(IndexTask.__table__).values(workspace_id=workspace_id, source=source, ref=ref, data=data, attempts=0,
                                                       run_after=now, created_at=now)
        db.session.execute(statement.on_conflict_do_update(index_elements=["workspace_id", "source", "ref"],
                                                           set_={"created_at": now, "run_after": now, "attempts": 0, "data": data}))
    db.session.commit()


def _reads(t):
    """What decides how a table's records read in the index: its names, and each field's name, kind, choices and who may
    see it."""
    return json.dumps({"label": t["label"], "singular": t["singular"], "title": t.get("title_field"),
                       "fields": [[f["key"], f["label"], f["type"], f.get("options"), f.get("access"), f.get("sensitive"), f.get("before"),
                                   f.get("after"), f.get("yes_label"), f.get("no_label")] for f in t["fields"]]}, sort_keys=True, default=str)


def _kept_from(t):
    return {f["key"]: (f.get("access"), bool(f.get("sensitive"))) for f in t["fields"]}


def redesigned(workspace, before, after):
    """A new version of the design: the records it reads differently (a field renamed, a choice renamed, a field added or
    kept from some people) are read again, so the AI answers in the app's words as they are now. A field newly kept from
    some people takes its table's passages out at once, before they're read again without it. Returns how many."""
    was = {t["key"]: t for t in (before or {}).get("tables", [])}
    now = {t["key"]: t for t in (after or {}).get("tables", [])}
    changed = [k for k, t in now.items() if k in was and _reads(was[k]) != _reads(t)]
    if not changed:
        return 0
    targets = set()
    for key in changed:
        t = now[key]
        parent = next((f["table"] for f in t["fields"] if f["type"] == "link"), None) if t.get("embedded") else None
        targets.add(parent if parent in now else key)  # a line reads as part of its parent
        hidden = {k for k, v in _kept_from(t).items() if v != _kept_from(was[key]).get(k, v) and (v[0] or v[1])}
        if hidden:
            Chunk.query.filter(Chunk.workspace_id == workspace.id, Chunk.source == "record", Chunk.table.in_({key, parent} - {None})
                               ).delete(synchronize_session=False)
    ids = [rid for (rid,) in db.session.query(Record.id).filter(Record.workspace_id == workspace.id, Record.table.in_(targets),
                                                                 Record.deleted_at.is_(None))]
    for start in range(0, len(ids), 500):
        touch(workspace.id, "record", ids[start:start + 500])
    db.session.commit()
    return len(ids)


def rebuild(workspace):
    """Read everything again: every record and every kept file (after an upgrade, or when asked to)."""
    tables = {t["key"] for t in (workspace.blueprint or {}).get("tables", []) if not t.get("embedded")}
    ids = [rid for rid, table in db.session.query(Record.id, Record.table).filter(Record.workspace_id == workspace.id,
                                                                                    Record.deleted_at.is_(None)) if table in tables]
    for start in range(0, len(ids), 500):
        touch(workspace.id, "record", ids[start:start + 500])
    files = [f.id for f in File.query.filter(File.workspace_id == workspace.id, File.deleted_at.is_(None),
                                             File.source.notin_(SKIP_FILES))]
    touch(workspace.id, "file", files)
    return len(ids), len(files)


# --- what each thing says --------------------------------------------------------------------------------------------

def _shareable(engine, f):
    """Fields that go into a record's text: not files, and not fields kept from some people (those never reach the
    index, so they can't come back in someone else's answer)."""
    if f["type"] in schema.FILES or f.get("access"):
        return False
    return f["type"] != "lookup" or not (engine.source(f) or {}).get("access")


def record_text(engine, record):
    """A record written out for reading: its name, its fields, its lines and the conversation about it."""
    t = engine.tables[record.table]
    facts = []
    for f in t["fields"]:
        if not _shareable(engine, f):
            continue
        value = engine.value(record, f["key"])
        if value in (None, "", []):
            continue
        shown = str(value) if f["type"] in ("long_text", "rich_text") else engine.display(f, value, record)
        facts.append(f"{f['label']}: {shown}")
    parts = [" | ".join(facts)]
    for child in engine.bp.get("tables", []):
        link = next((f for f in child["fields"] if f["type"] == "link" and f["table"] == t["key"]), None)
        if not child.get("embedded") or link is None:
            continue
        rows = engine.children(child["key"], link["key"], record.id)
        if rows:
            parts.append(f"{child['label']}:")
            for r in rows[:100]:
                parts.append("- " + "; ".join(f"{f['label']} {engine.display(f, engine.value(r, f['key']), r)}" for f in child["fields"]
                                              if f["key"] != link["key"] and _shareable(engine, f)
                                              and engine.value(r, f["key"]) not in (None, "", [])))
    comments = [] if engine.sealed_from_me(record) else Comment.query.filter_by(record_id=record.id).order_by(Comment.id.desc()).limit(20).all()
    if comments:
        parts.append("Comments:")
        parts += [f"- {c.user.display if c.user else 'someone'} ({c.created_at:%d %b %Y}): {c.text}" for c in reversed(comments)]
    return f"{t['singular']} {engine.title(record)}", "\n".join(parts)


def passages(text, size=PASSAGE, overlap=OVERLAP):
    """Text cut into passages of about `size` characters, at paragraph or sentence ends, overlapping a little so a
    sentence cut in two is whole in one of them."""
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", (text or "").strip()))
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            floor = start + size // 2
            cut = max(text.rfind("\n\n", floor, end), text.rfind("\n", floor, end), text.rfind(". ", floor, end))
            if cut > start:
                end = cut + 1
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in out if p]


def _documents(engines, task):
    """[{title, text, link, table, record_id}] for one indexing task; [] when the thing is gone (its passages go too)."""
    if task.source == "record":
        record = db.session.get(Record, int(task.ref))
        if record is None or record.deleted_at is not None:
            return []
        engine = engines(record.workspace_id)
        t = engine.tables.get(record.table)
        if t is None or t.get("embedded"):
            return []
        title, text = record_text(engine, record)
        link = f"table:{record.table}:record:{record.id}"
        return [{"title": title, "text": p, "link": link, "table": record.table, "record_id": record.id} for p in passages(text)]
    if task.source == "file":
        from .files import readers
        from .storage import path_of
        f = db.session.get(File, int(task.ref))
        if f is None or f.deleted_at is not None or f.source in SKIP_FILES:
            return []
        content = readers.read(path_of(f), f.name, max_chars=400_000, max_rows=2000, max_images=0)
        record = db.session.get(Record, f.record_id) if f.record_id else None
        return [{"title": f.name, "text": p, "link": f"file:{f.id}", "table": record.table if record else None,
                 "record_id": f.record_id} for p in passages(content.text)]
    if task.source == "email":
        mail = task.data or {}
        head = f"From: {mail.get('from_name') or ''} <{mail.get('from') or ''}>\nReceived: {(mail.get('received') or '')[:16]}\n" \
               f"Subject: {mail.get('subject') or ''}\n\n"
        return [{"title": mail.get("subject") or "(no subject)", "text": p, "link": mail.get("link")}
                for p in passages(head + (mail.get("body") or ""))]
    return []


def _unit(vectors):
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return matrix / np.where(norms == 0, 1, norms)


def _embed(texts):
    out = []
    for start in range(0, len(texts), EMBED_BATCH):
        out.extend(_unit(ai.embed(texts[start:start + EMBED_BATCH])))
    return out


def _vector_index(dims):
    """An HNSW index for passages of this vector length (the embedding model's), made the first time they're stored."""
    if dims in _indexed:
        return
    db.session.execute(sql(f"CREATE INDEX IF NOT EXISTS ix_chunk_embedding_{int(dims)} ON chunk USING hnsw "
                           f"((embedding::vector({int(dims)})) vector_cosine_ops) WHERE dims = {int(dims)}"))
    _indexed.add(dims)


def _store(workspace_id, source, ref, docs, vectors, model):
    Chunk.query.filter_by(workspace_id=workspace_id, source=source, ref=ref).delete(synchronize_session=False)
    for i, doc in enumerate(docs):
        vector = vectors[i] if vectors is not None else None
        db.session.add(Chunk(workspace_id=workspace_id, source=source, ref=ref, table=doc.get("table"), record_id=doc.get("record_id"),
                             title=(doc["title"] or "")[:300], text=doc["text"], position=i, link=(doc.get("link") or "")[:500] or None,
                             embedding=[float(x) for x in vector] if vector is not None else None,
                             dims=len(vector) if vector is not None else None, model=model if vector is not None else None))
    if vectors is not None and len(vectors):
        _vector_index(len(vectors[0]))
    db.session.flush()


def work(limit=BATCH):
    """Index a batch of what's waiting. Returns whether there was anything to do."""
    from .engine.records import Engine
    tasks = IndexTask.query.filter(IndexTask.run_after <= utcnow()).order_by(IndexTask.id).limit(limit).all()
    if not tasks:
        return False
    made = {}

    def engines(workspace_id):
        if workspace_id not in made:
            made[workspace_id] = Engine(db.session.get(Workspace, workspace_id), by="AI index")
        return made[workspace_id]

    prepared = []
    for task in tasks:
        try:
            prepared.append((task, task.created_at, _documents(engines, task)))
        except Exception as exc:  # one unreadable thing doesn't hold up the rest
            log.warning("Couldn't index %s %s: %s", task.source, task.ref, exc)
            db.session.rollback()
            task = db.session.get(IndexTask, task.id)
            task.attempts = (task.attempts or 0) + 1
            task.run_after = utcnow() + timedelta(minutes=5 * task.attempts)
            if task.attempts >= 5:
                db.session.delete(task)
            db.session.commit()
    texts = [f"{doc['title']}\n{doc['text']}" for _, _, docs in prepared for doc in docs]
    model = settings().get("OLLAMA_EMBED_MODEL") or None
    vectors, waiting = None, False
    if texts and model:
        try:
            vectors = _embed(texts)
        except ai.AIUnavailable as exc:
            log.info("Indexing by words only for now: %s", exc)
            waiting = True
    elif texts:
        model = None
    position = 0
    for task, seen, docs in prepared:
        mine = vectors[position:position + len(docs)] if vectors is not None else None
        position += len(docs)
        _store(task.workspace_id, task.source, task.ref, docs, mine, model)
        if waiting:  # passages are findable by words now; come back for their vectors
            task.run_after, task.attempts = utcnow() + timedelta(minutes=2), (task.attempts or 0) + 1
        else:
            IndexTask.query.filter(IndexTask.id == task.id, IndexTask.created_at == seen).delete(synchronize_session=False)
    db.session.commit()
    return not waiting


# --- finding -----------------------------------------------------------------------------------------------------------

def _match_query(text):
    """The question's words for PostgreSQL's full-text search: any of them (OR), each stemmed by the search itself."""
    words = [w for w in re.findall(r"\w+", text.lower()) if (len(w) > 1 or not w.isascii()) and w not in STOP]
    return " OR ".join(dict.fromkeys(words[:24]))


def _may_see(engine, chunk):
    if chunk.source == "email":
        return engine._at_least("editor")
    if chunk.source == "file":
        f = db.session.get(File, int(chunk.ref))
        if f is None or f.deleted_at is not None:
            return False
        if f.record_id:
            owner = engine.get(f.record_id)
            field = engine.field(owner.table, f.field) if owner is not None and f.field else None
            return owner is not None and engine.allowed(owner) and (field is None or engine.sees(field))
        if f.source == "knowledge":
            return True
        if f.source in ("email", "onedrive"):
            return engine._at_least("editor")
        return engine._at_least("admin") or (f.uploaded_by_id is not None and f.uploaded_by_id == engine.user_id)
    if chunk.source == "record":
        return engine.allowed(engine.get(chunk.record_id))
    return False


def search(engine, query, k=6, sources=None):
    """The passages most relevant to `query` that this engine's person may see: [{n, source, title, text, link, score}].
    `found_by` says whether meaning was used (False when the embedding model couldn't be reached)."""
    query = (query or "").strip()
    if not query:
        return []
    ws_id = engine.ws.id
    rankings = []
    words = _match_query(query)
    if words:  # PostgreSQL full-text search: stemmed words, titles counted above text, a short passage on it before a long one
        rows = db.session.execute(sql("SELECT id FROM chunk, websearch_to_tsquery('english', :q) AS q WHERE workspace_id = :ws "
                                      "AND words @@ q ORDER BY ts_rank_cd(words, q, 1) DESC, id LIMIT 80"), {"q": words, "ws": ws_id}).fetchall()
        rankings.append([r[0] for r in rows])
    model = settings().get("OLLAMA_EMBED_MODEL")
    similarity = {}
    if model and db.session.query(Chunk.id).filter(Chunk.workspace_id == ws_id, Chunk.model == model).first() is not None:
        try:
            q = [float(x) for x in _unit(ai.embed([query]))[0]]
            dims = len(q)
            # pgvector, through the HNSW index for this length: 1 - cosine distance is the similarity. The index is
            # shared by every workspace, so it's scanned on until this one's nearest are found (an iterative scan),
            # never the nearest of all filtered afterwards (which leaves a small workspace in a big index with none)
            db.session.execute(sql("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
            rows = db.session.execute(sql(f"SELECT id, sim FROM (SELECT id, 1 - ((embedding::vector({dims})) <=> CAST(:q AS vector({dims}))) AS sim "
                                          f"FROM chunk WHERE workspace_id = :ws AND model = :model AND dims = {dims} "
                                          f"ORDER BY (embedding::vector({dims})) <=> CAST(:q AS vector({dims})) LIMIT 80) found ORDER BY sim DESC"),
                                      {"q": str(q), "ws": ws_id, "model": model}).fetchall()
            if rows:
                floor = max(MIN_SIMILARITY, float(rows[0][1]) - CLOSE_TO_BEST)
                similarity = {int(cid): float(sim) for cid, sim in rows if sim >= floor}
                rankings.append(list(similarity))
        except ai.AIUnavailable as exc:
            log.info("Searching by words only: %s", exc)
    scores = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (FUSION_K + rank + 1)
    if not scores:
        return []
    chunks = {c.id: c for c in Chunk.query.filter(Chunk.id.in_(list(scores)), Chunk.workspace_id == ws_id)}
    hits, per_thing = [], {}
    for cid in sorted(scores, key=lambda x: -scores[x]):
        chunk = chunks.get(cid)
        if chunk is None or (sources and chunk.source not in sources):
            continue
        thing = (chunk.source, chunk.ref)
        if per_thing.get(thing, 0) >= 2 or not _may_see(engine, chunk):
            continue
        per_thing[thing] = per_thing.get(thing, 0) + 1
        hits.append({"n": len(hits) + 1, "source": chunk.source, "title": chunk.title, "text": chunk.text, "link": chunk.link,
                     "score": round(scores[cid], 4), "similarity": round(similarity.get(cid, 0.0), 3)})
        if len(hits) >= k:
            break
    return hits


KIND = {"record": "Record", "file": "Document", "email": "Email"}


def as_context(hits, limit=1200):
    """Passages for a prompt, numbered so the AI can cite them as [1], [2]."""
    return "\n\n".join(f"[{h['n']}] {KIND.get(h['source'], h['source'])}: {h['title']}\n{h['text'][:limit]}" for h in hits)


def status(workspace):
    counts = dict(db.session.query(Chunk.source, func.count(func.distinct(Chunk.ref))).filter(Chunk.workspace_id == workspace.id)
                  .group_by(Chunk.source).all())
    passages_ = db.session.query(func.count(Chunk.id)).filter(Chunk.workspace_id == workspace.id).scalar() or 0
    without = db.session.query(func.count(Chunk.id)).filter(Chunk.workspace_id == workspace.id, Chunk.embedding.is_(None)).scalar() or 0
    waiting = db.session.query(func.count(IndexTask.id)).filter(IndexTask.workspace_id == workspace.id).scalar() or 0
    return {"records": counts.get("record", 0), "files": counts.get("file", 0), "emails": counts.get("email", 0),
            "passages": passages_, "by_words_only": without, "waiting": waiting,
            "model": settings().get("OLLAMA_EMBED_MODEL") or None}


# --- learning from Microsoft 365 -----------------------------------------------------------------------------------------

def mail_item(email):
    return {k: email.get(k) for k in ("subject", "from", "from_name", "received", "body", "link")}


@jobs.handler("knowledge_rebuild")
def _rebuild(job):
    workspace = db.session.get(Workspace, job.workspace_id)
    records, files = rebuild(workspace)
    return {"records": records, "files": files}


@jobs.handler("knowledge_mail")
def _learn_mail(job):
    """Read the last months of mail into what the AI knows."""
    from .connectors import microsoft
    workspace = db.session.get(Workspace, job.workspace_id)
    days = int(job.payload.get("days") or 180)
    jobs.stage(job, f"Reading the last {days} days of mail", 10)
    emails = microsoft.recent_messages(workspace, days=days, limit=int(job.payload.get("limit") or 500))
    for email in emails:
        touch(workspace.id, "email", email["id"], data=mail_item(email))
    return {"emails": len(emails)}


DOCUMENT_KINDS = {"pdf", "document", "spreadsheet", "text", "email"}


@jobs.handler("knowledge_onedrive")
def _learn_onedrive(job):
    """Bring the documents in a OneDrive folder into what the AI knows (each once; changed files again)."""
    from .connectors import microsoft
    from .files.readers import kind_of
    workspace = db.session.get(Workspace, job.workspace_id)
    folder = job.payload.get("folder") or ""
    jobs.stage(job, f"Looking through {folder or 'OneDrive'}", 5)
    items = [i for i in microsoft.walk(workspace, folder, limit=int(job.payload.get("limit") or 300))
             if kind_of(i["name"]) in DOCUMENT_KINDS and (i.get("size") or 0) < settings()["MAX_UPLOAD_MB"] * 1024 * 1024]
    conn = microsoft.connection(workspace)
    known = dict((conn.state or {}).get("knowledge_items") or {})
    added = 0
    for n, item in enumerate(items, start=1):
        stamp = item.get("modified") or item.get("etag") or ""
        if known.get(item["id"]) == stamp:
            continue
        jobs.stage(job, f"Reading {item['name']}", 5 + int(90 * n / max(len(items), 1)))
        microsoft.download(workspace, item["id"], item["name"], by="OneDrive (knowledge)")
        known[item["id"]] = stamp
        added += 1
        conn = microsoft.connection(workspace)
        conn.state = {**(conn.state or {}), "knowledge_items": known}
        db.session.commit()
    return {"documents": added, "looked_at": len(items)}
