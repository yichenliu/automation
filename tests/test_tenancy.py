"""One customer's data never reaches another's, and a small customer is served as well as a big one sharing the same
indexes."""
from sqlalchemy import text

from app import ai, knowledge
from app.engine.records import Engine
from app.extensions import db
from app.models import Chunk, Membership, User, Workspace


def _ws(name, owner):
    ws = Workspace(owner_id=owner.id, name=name, state={}, blueprint={"name": name, "tables": [], "pages": [], "automations": [], "modules": []})
    db.session.add(ws)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=owner.id, role="owner"))
    return ws


def test_a_small_workspace_finds_its_passages_by_meaning_in_an_index_a_big_one_fills(app):
    app.config["OLLAMA_EMBED_MODEL"] = "test-embed"
    owner = User(email="o@example.com", name="Olive")
    owner.set_password("x" * 12)
    db.session.add(owner)
    db.session.flush()
    big, small = _ws("Big", owner), _ws("Small", owner)
    near, far = [1.0, 0.0, 0.0], [0.6, 0.0, 0.8]
    knowledge._indexed.discard(3)
    knowledge._vector_index(3)
    for n in range(1000):  # the big workspace's passages are all far nearer the question than the small one's
        v = [1.0, (n % 37) / 400, (n % 41) / 400]
        db.session.add(Chunk(workspace_id=big.id, source="email", ref=f"m{n}", title=f"Big {n}", text="refunds", embedding=v, dims=3, model="test-embed"))
    db.session.add(Chunk(workspace_id=small.id, source="email", ref="s1", title="Small's refund note", text="refunds", embedding=far, dims=3,
                         model="test-embed"))
    db.session.commit()
    ai.set_embedder(lambda texts: [near for _ in texts])
    db.session.execute(text("SET enable_seqscan = off"))  # as in a big database: through the shared index
    member = Membership.query.filter_by(workspace_id=small.id, user_id=owner.id).first()
    hits = knowledge.search(Engine(small, by="Olive", member=member), "money back")
    assert "Small's refund note" in [h["title"] for h in hits]
