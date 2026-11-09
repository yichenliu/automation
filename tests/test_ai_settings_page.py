"""Settings › AI: a platform admin chooses where the AI runs (on this computer, a server of their own, a cloud provider)
for each kind of work, tries it before saving, and saves it, in effect at once with no restart. The keys are kept
encrypted and never shown again; nobody else can change it."""
import pytest

from app import ai
from app import ai_providers as providers
from app.extensions import db
from app.models import Membership, PlatformSetting, User

from .conftest import login
from .test_ai_gateway import Fake, _fresh


@pytest.fixture
def page(app, owner, client, monkeypatch):
    for key, value in {"AI_PROVIDER": "ollama", "AI_BASE_URL": "", "AI_API_KEY": "", "AI_MODEL": "", "AI_DEADLINE": 30,
                       "AI_RETRIES": 0, "PLATFORM_ADMINS": "", "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""}.items():
        monkeypatch.setitem(app.config, key, value)
    for tag in ("_BUILDER", "_FAST", "_EMBED"):
        for name in ("AI_PROVIDER", "AI_BASE_URL", "AI_API_KEY"):
            monkeypatch.setitem(app.config, name + tag, "")
    _fresh()
    providers._listed.clear()
    login(client)
    user, ws = owner
    return f"/w/{ws.id}/settings/ai", ws


def _other_admin(ws):
    user = User(email="sam@example.com", name="Sam")
    user.set_password("password1234")
    db.session.add(user)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=user.id, role="admin"))
    db.session.commit()
    return user


def test_the_page_says_where_the_ai_runs_and_the_platform_admin_can_choose(app, page, client):
    url, ws = page
    html = client.get(url).text
    assert "Where it runs now" in html and "never leave your own machines" in html
    assert "Choose where it runs" in html and 'value="anthropic"' in html and 'value="openai"' in html and 'value="gemini"' in html


def test_saving_a_cloud_provider_keeps_the_key_encrypted_and_says_data_goes_there(app, page, client):
    url, ws = page
    done = client.post(url, data={"do": "save", "assistant_provider": "anthropic", "assistant_model": "claude-opus-5-5",
                                  "builder_provider": "", "fast_provider": "", "embed_provider": "", "key_anthropic": "sk-ant-secret-1234"})
    assert done.status_code in (302, 303)
    assert ai._route("assistant") == ai.Route("anthropic", "https://api.anthropic.com/v1", "sk-ant-secret-1234", "claude-opus-5-5")
    stored = db.session.get(PlatformSetting, "ai")
    assert "sk-ant-secret-1234" not in str(stored.value) and stored.updated_by == "ethan@example.com"
    html = client.get(url).text
    assert "sent to <b>Anthropic (Claude)</b>" in html and "saved, ending 1234" in html and "sk-ant-secret-1234" not in html
    # a key left blank stays; asked to, it's taken away
    client.post(url, data={"do": "save", "assistant_provider": "anthropic", "assistant_model": "claude-opus-5-5"})
    assert ai._route("assistant").key == "sk-ant-secret-1234"
    client.post(url, data={"do": "save", "assistant_provider": "", "forget_key_anthropic": "on"})
    assert ai._route("assistant").provider == "ollama" and not providers.saved_key("anthropic")


def test_trying_asks_each_one_with_whats_on_the_form_and_saves_nothing(app, page, client):
    url, ws = page
    fake = Fake(reply=lambda h, body: h.stream('{"greeting": "Hello, ready when you are."}'))
    try:
        html = client.post(url, data={"do": "try", "assistant_provider": "compatible", "assistant_model": "big",
                                      "url_compatible": fake.url, "embed_provider": "none"}).text
        assert "Hello, ready when you are." in html and "All of them answered" in html
        assert fake.seen[0]["body"]["model"] == "big"
        assert db.session.get(PlatformSetting, "ai") is None and ai._route("assistant").provider == "ollama"
        assert f'value="{fake.url}"' in html  # what was typed stays on the form, to save
    finally:
        fake.close()


def test_what_went_wrong_trying_is_said_plainly(app, page, client):
    url, ws = page
    fake = Fake(reply=lambda h, body: h.send_json(401, {"error": {"message": "Incorrect API key"}}))
    try:
        html = client.post(url, data={"do": "try", "assistant_provider": "compatible", "assistant_model": "big",
                                      "url_compatible": fake.url, "key_compatible": "wrong", "embed_provider": "none"}).text
        assert "turned the key away" in html and "Put right what didn't work" in html
    finally:
        fake.close()


def test_the_model_box_offers_the_models_the_key_can_use(app, page, client):
    url, ws = page
    fake = Fake()
    try:
        said = client.post(url + "/models", data={"provider": "compatible", "url": fake.url}).json
        assert said == {"models": ["big"], "best": ""}
    finally:
        fake.close()


def test_a_bad_address_is_refused_and_nothing_saved(app, page, client):
    url, ws = page
    response = client.post(url, data={"do": "save", "assistant_provider": "compatible", "assistant_model": "big", "url_compatible": "not an address"})
    assert response.status_code == 422 and db.session.get(PlatformSetting, "ai") is None
    response = client.post(url, data={"do": "save", "embed_provider": "openai"})  # which of its embedding models: it has several
    assert response.status_code == 422 and "say which of OpenAI" in response.text and db.session.get(PlatformSetting, "ai") is None
    client.post(url, data={"do": "save", "embed_provider": "ollama"})  # on this computer: its usual one
    assert ai.embed_model() == app.config["OLLAMA_EMBED_MODEL"] or ai.embed_model() == "bge-m3"


def test_only_the_platform_admin_chooses(app, page, client, monkeypatch):
    url, ws = page
    _other_admin(ws)
    client.post("/logout")
    client.post("/login", data={"email": "sam@example.com", "password": "password1234"})
    html = client.get(url).text  # a business's own admin sees where it runs, not how to change it
    assert "Where it runs now" in html and "Choose where it runs" not in html
    assert client.post(url, data={"do": "save", "assistant_provider": "openai"}).status_code == 403
    assert client.post(url + "/models", data={"provider": "openai"}).status_code == 403
    monkeypatch.setitem(app.config, "PLATFORM_ADMINS", "Sam@example.com")
    assert "Choose where it runs" in client.get(url).text


def test_a_model_written_another_way_is_saved_under_the_providers_own_name(app, page, client, monkeypatch):
    url, ws = page
    monkeypatch.setattr(providers, "_list", lambda p, address, key: ["claude-opus-5-5", "claude-sonnet-5-5"])
    done = client.post(url, data={"do": "save", "builder_provider": "anthropic", "builder_model": "claude-sonnet-5.5",
                                  "key_anthropic": "sk-ant-secret-1234"}, follow_redirects=True)
    assert providers.stored()["use"]["builder"] == {"provider": "anthropic", "model": "claude-sonnet-5-5"}
    assert "saved as that" in done.text
