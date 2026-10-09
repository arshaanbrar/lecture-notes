from backend.app import config, notion, placement
from tests.fakes import H


def names(people):
    return [p["title"] for p in people]


def test_people_are_the_top_level_pages(fake_notion):
    assert names(placement.people()) == ["Arshaan", "Efrain", "Ryan"]


def test_hidden_people_are_left_off_the_site(fake_notion, monkeypatch):
    # HIDDEN_PEOPLE=Ryan leaves him off the site; nothing in his Notion is changed.
    monkeypatch.setattr(config, "HIDDEN_PEOPLE", {"ryan"})
    assert names(placement.people()) == ["Arshaan", "Efrain"]
    assert fake_notion.created == [] and fake_notion.appended == []


def test_classes_come_from_course_tables(fake_notion, fake_ai):
    assert [c["title"] for c in placement.find_classes(H("arshaan"))] == [
        "The Future of Work", "Discrete Math for Computer Science"]
    assert [c["title"] for c in placement.find_classes(H("efrain"))] == ["SOCSCI 1T03"]
    # Ryan's table has no name, but its columns (Class code, Credits, Teacher…) show it's his classes.
    ryan = placement.find_classes(H("ryan"))
    assert [c["title"] for c in ryan] == ["CSC108", "Pshycology", "Intro to Management functions"]
    assert {c["group"] for c in ryan} == {"Classes"}


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


def test_timetables_are_not_classes_or_places_for_lectures(fake_notion, fake_ai):
    # Efrain's "class timetable" board says "class" but its rows are timetable slots, not classes.
    assert [c["title"] for c in placement.find_classes(H("efrain"))] == ["SOCSCI 1T03"]
    plan = placement.plan(H("efrain"), H("soc"), "", "Social Theory", "temporality")
    best = plan["candidates"][plan["best"]]
    # The notes go inside the class's card in the Courses gallery.
    assert best["kind"] == "page" and notion.same_id(best["target_id"], H("soc"))
    for unwanted in ("etimes", "eassess"):
        assert not any(notion.same_id(c["target_id"], H(unwanted)) for c in plan["candidates"])


def test_lectures_kept_on_their_own_page_stay_there(fake_notion, fake_ai):
    # Ryan keeps "csc lec 3", "csc lab 3", "mgm lec 4" right on his page, even though UNI has a
    # Classes table with a CSC108 card: new lectures go on his page too, without asking the AI.
    plan = placement.plan(H("ryan"), H("r_csc"), "", "Recursion", "recursion")
    best = plan["candidates"][plan["best"]]
    assert best["kind"] == "page" and notion.same_id(best["target_id"], H("ryan"))
    assert best["label"] == "New page in Ryan, next to your other lectures"
    assert not any("Places it could be saved" in p for p in fake_ai.prompts)


def test_people_with_a_class_list_are_guessed_from_their_classes(fake_notion, fake_ai):
    placement.guess_owner("Motivation and leadership", "Management lecture on leading teams")
    prompt = next(p for p in fake_ai.prompts if "These students share one Notion" in p)
    ryan = prompt.split(". Ryan", 1)[1].split("\nP", 1)[0]
    assert "Intro to Management functions" in ryan and "Pshycology" in ryan
    assert "no class list" not in ryan  # his loose page titles aren't listed on their own...
    assert "C3: CSC108 (recent lectures: “csc lec 3”" in ryan  # ...they show up under the class they belong to


def test_ryans_lectures_still_go_on_his_page_with_his_classes_known(fake_notion, fake_ai):
    plan = placement.plan(H("ryan"), H("r_mgm"), "", "Motivation and leadership", "management")
    best = plan["candidates"][plan["best"]]
    assert best["kind"] == "page" and notion.same_id(best["target_id"], H("ryan"))


def test_the_guess_sees_class_details_recent_lectures_timetable_and_time(fake_notion, fake_ai):
    placement.guess_owner("Social theory", "Durkheim", recorded_at="Monday, October 6 at 9:41 AM")
    prompt = next(p for p in fake_ai.prompts if "These students share one Notion" in p)
    assert "Recorded: Monday, October 6 at 9:41 AM" in prompt
    efrain = prompt.split(". Efrain", 1)[1].split("\nP", 1)[0]
    assert "C2: SOCSCI 1T03 [Semester: Fall; Status: In progress]" in efrain
    assert "their timetable: SocSci 1T03 timetable (Days: Monday; Time: 9:30 - 10:30)" in efrain
    arshaan = prompt.split(". Arshaan", 1)[1].split("\nP", 1)[0]
    # Lectures linked to a class in the Topics table count as that class's recent lectures.
    assert "Discrete Math for Computer Science (recent lectures: “Review Lecture" in arshaan


def test_when_the_device_knows_its_person_only_the_class_is_guessed(fake_notion, fake_ai):
    guess = placement.guess_owner("Motivation", "A management lecture on leading teams", only_person_id=H("ryan"))
    assert guess["person_id"] == H("ryan") and guess["class_id"] == H("r_mgm")
    assert guess["reason"] == "management lecture"
    prompt = fake_ai.prompts[-1]
    assert "This lecture is Ryan's" in prompt and "Arshaan" not in prompt and "Efrain" not in prompt


def test_the_device_person_is_kept_even_when_the_class_is_unclear(fake_notion, fake_ai):
    guess = placement.guess_owner("Team sync", "Budget planning", only_person_id=H("efrain"))
    assert guess["person_id"] == H("efrain") and not guess["class_id"]
