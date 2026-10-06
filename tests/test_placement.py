from backend.app import notion, placement
from tests.fakes import H


def names(people):
    return [p["title"] for p in people]


def test_people_are_the_top_level_pages(fake_notion):
    assert names(placement.people()) == ["Arshaan", "Efrain", "Ryan"]


def test_classes_come_from_course_tables(fake_notion, fake_ai):
    assert [c["title"] for c in placement.find_classes(H("arshaan"))] == [
        "The Future of Work", "Discrete Math for Computer Science"]
    assert [c["title"] for c in placement.find_classes(H("efrain"))] == ["SOCSCI 1T03"]
    assert placement.find_classes(H("ryan")) == []


def test_guess_owner_matches_the_lecture_to_a_persons_class(fake_notion, fake_ai):
    guess = placement.guess_owner("Proofs by Induction", "Discrete math lecture on induction")
    assert guess["person_id"] == H("arshaan") and guess["class_id"] == H("dm")


def test_guess_owner_gives_up_when_nothing_fits(fake_notion, fake_ai):
    assert placement.guess_owner("Team sync", "Budget planning")["person_id"] is None


def test_plan_prefers_the_linked_lectures_table(fake_notion, fake_ai):
    plan = placement.plan(H("arshaan"), H("dm"), "", "Proofs by Induction", "induction")
    best = plan["candidates"][plan["best"]]
    assert best["kind"] == "entry" and notion.same_id(best["target_id"], H("topics"))
    assert "linked to Discrete Math" in best["label"]
    # The classes table itself is never offered as a place for lectures.
    assert not any(notion.same_id(c["target_id"], H("domains")) for c in plan["candidates"])


def test_plan_for_someone_with_loose_lecture_pages(fake_notion, fake_ai):
    plan = placement.plan(H("ryan"), None, "csc", "Recursion", "recursion")
    best = plan["candidates"][plan["best"]]
    assert best["kind"] == "page" and notion.same_id(best["target_id"], H("ryan"))
    assert "csc lec 3" in best["examples"]


def test_sending_a_lecture_fills_in_the_tables_columns(fake_notion, fake_ai):
    plan = placement.plan(H("arshaan"), H("dm"), "", "Proofs by Induction", "induction")
    placement.send(plan["candidates"][plan["best"]], "Proofs by Induction", {"summary": "s"}, "t", "2026-10-05")
    props = fake_notion.created[-1]["properties"]
    assert props["lecture/assignment"]["title"][0]["text"]["content"] == "Proofs by Induction"
    assert notion.same_id(props["domain"]["relation"][0]["id"], H("dm"))
    assert props["type"] == {"select": {"name": "lecture"}}
    assert props["date"] == {"date": {"start": "2026-10-05"}}
