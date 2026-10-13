"""Everyone signs in with their name, and the site uses that person's own Notion (or their page in
the shared one, until they move)."""

import pytest

from backend.app import config, notion
from tests.conftest import wait_for_job
from tests.fakes import FakeAccounts, FakeNotion, H, adeola_workspace, efrain_workspace

SOCIOLOGY = "Today in sociology we look at Durkheim and social facts. " * 10


@pytest.fixture
def people(monkeypatch):
    """PEOPLE is set. Efrain and Adeola have moved to their own Notion; Arshaan and Ryan haven't."""
    monkeypatch.setenv("PEOPLE", "Arshaan, Efrain, Ryan, Adeola")
    monkeypatch.setenv("NOTION_TOKEN_EFRAIN", "efrain-key")
    monkeypatch.setenv("NOTION_TOKEN_ADEOLA", "adeola-key")
    monkeypatch.setattr(config, "APP_PASSWORD", "old-shared-password")
    accounts = FakeAccounts({"test": FakeNotion(), "efrain-key": efrain_workspace(), "adeola-key": adeola_workspace()})
    monkeypatch.setattr(notion, "_request", accounts.request)
    return accounts


def as_(name):
    return {"X-App-Password": name}


def test_everyone_signs_in_with_their_name(client, people):
    assert client.get("/api/auth/check", headers=as_("efrain")).json() == {"ok": True, "person": "Efrain"}
    assert client.get("/api/auth/check", headers=as_("  RYAN ")).json()["person"] == "Ryan"
    for wrong in ("old-shared-password", "Kamilah", ""):
        resp = client.get("/api/auth/check", headers=as_(wrong))
        assert resp.status_code == 401 and resp.json()["detail"] == "That name isn't on the list."
    cfg = client.get("/api/config").json()
    assert cfg["people_mode"] and cfg["password_required"] and cfg["notion_configured"]


def test_a_password_can_be_changed_from_the_name(client, people, monkeypatch):
    monkeypatch.setenv("PASSWORD_RYAN", "Tiger")
    assert client.get("/api/auth/check", headers=as_("ryan")).status_code == 401
    assert client.get("/api/auth/check", headers=as_("tiger")).json()["person"] == "Ryan"


def test_someone_who_moved_gets_their_own_notion_only(client, people, fake_ai):
    found = client.get("/api/notion/people", headers=as_("efrain")).json()["people"]
    assert found == [{"id": H("e_top"), "title": "Efrain", "icon": ""}]
    # Even if the page asks about someone else's page, it's Efrain's classes.
    classes = client.post("/api/notion/classes", headers=as_("efrain"), json={"person_id": H("arshaan")}).json()
    assert [c["title"] for c in classes["classes"]] == ["SOCSCI 1T03", "POLSC 1AA3"]


def test_someone_who_hasnt_moved_uses_their_page_in_the_shared_notion(client, people):
    assert client.get("/api/notion/people", headers=as_("ryan")).json()["people"][0]["id"] == H("ryan")


def test_no_page_and_no_key_says_what_to_do(client, monkeypatch, people):
    monkeypatch.delenv("NOTION_TOKEN_ADEOLA")
    resp = client.get("/api/notion/people", headers=as_("adeola"))
    assert resp.status_code == 400
    assert "Couldn't find a page called “Adeola”" in resp.json()["detail"] and "NOTION_TOKEN_ADEOLA" in resp.json()["detail"]


def test_several_top_pages_are_held_under_one_and_never_saved_to_directly(client, people, fake_ai):
    found = client.get("/api/notion/people", headers=as_("adeola")).json()["people"]
    assert found[0]["id"] == notion.virtual_root_id("Adeola") and found[0]["title"] == "Adeola"
    plan = client.post("/api/notion/plan", headers=as_("adeola"),
                       json={"person_id": found[0]["id"], "class_text": "psych", "summary": "psychology"}).json()
    targets = {c["target_id"] for c in plan["candidates"]}
    assert notion.virtual_root_id("Adeola") not in targets
    best = plan["candidates"][plan["best"]]
    assert best["target_id"] == H("a_uni") and best["kind"] == "page"  # next to her psych lectures


def test_each_persons_notion_is_cached_separately(client, people):
    first = client.get("/api/notion/people", headers=as_("efrain")).json()["people"][0]["id"]
    second = client.get("/api/notion/people", headers=as_("ryan")).json()["people"][0]["id"]
    third = client.get("/api/notion/people", headers=as_("efrain")).json()["people"][0]["id"]
    assert (first, second, third) == (H("e_top"), H("ryan"), H("e_top"))


def test_the_guess_is_only_ever_the_signed_in_persons_class(client, people, fake_ai):
    guess = client.post("/api/notion/guess", headers=as_("efrain"),
                        json={"summary": "A sociology lecture", "person_id": H("arshaan")}).json()
    assert guess["person_id"] == H("e_top") and guess["class_id"] == H("e_soc")
    assert "This lecture is Efrain's" in fake_ai.prompts[-1] and "Arshaan" not in fake_ai.prompts[-1]


def test_background_work_and_sending_use_the_signed_in_persons_notion(client, people, fake_ai):
    job = client.post("/api/jobs/text", headers=as_("efrain"), data={"transcript": SOCIOLOGY}).json()
    job = wait_for_job(client, job["id"], headers=as_("efrain"))
    assert job["status"] == "done", job["error"]
    assert job["placement"]["person_id"] == H("e_top") and job["placement"]["class_id"] == H("e_soc")
    place = job["placement"]["plan"]["candidates"][job["placement"]["plan"]["best"]]
    sent = client.post("/api/notion/export", headers=as_("efrain"), json={
        "place": {k: place[k] for k in ("kind", "target_id", "link_to")}, "title": "Durkheim",
        "notes": job["notes"], "transcript": job["transcript"]})
    assert sent.status_code == 200, sent.text
    assert people.workspaces["efrain-key"].created and not people.workspaces["test"].created


def test_a_moved_persons_class_page_is_suggested_not_their_class_list(client, people, fake_ai):
    plan = client.post("/api/notion/plan", headers=as_("efrain"),
                       json={"person_id": H("e_top"), "class_id": H("e_soc"), "summary": "sociology"}).json()
    best = plan["candidates"][plan["best"]]
    assert best["kind"] == "page" and best["target_id"] == H("e_soc")  # inside his SOCSCI 1T03 card
    assert H("e_courses") not in {c["target_id"] for c in plan["candidates"]}


def test_two_lists_of_the_same_classes_keep_the_one_holding_the_notes(client, people):
    from tests.fakes import D, page, table, text
    efrain = people.workspaces["efrain-key"]
    # A new "Classes" table with the same classes under longer names, and nothing inside them...
    efrain.search += [table("e_classes", "Classes", P_("e_acad")),
                      page("e_c1", "SOCSCI 1T03-C01LEC (3806)", D("e_classes")),
                      page("e_c2", "POLSCI 1AA3-C01LEC (3851)", D("e_classes"))]
    efrain.schemas[H("e_classes")] = {"Name": {"type": "title"}}
    # ...while his copied "Courses" cards hold his notes.
    efrain.children[notion.normalize_id(H("e_soc"))] = [text("paragraph", "Meeting notes"), text("child_page", "Lecture 2")]
    efrain.children[notion.normalize_id(H("e_pol"))] = [text("paragraph", "Political Science Lecture: …")]
    classes = client.post("/api/notion/classes", headers=as_("efrain"), json={"person_id": H("e_top")}).json()["classes"]
    assert [(c["group"], c["title"]) for c in classes] == [("Courses", "SOCSCI 1T03"), ("Courses", "POLSC 1AA3")]


def P_(i):
    return {"type": "page_id", "page_id": H(i)}
