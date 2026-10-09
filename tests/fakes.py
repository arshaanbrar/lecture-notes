"""A fake Notion workspace and a stand-in AI, so tests run without real API keys."""

import hashlib
import json
import re

from backend.app import notion
from backend.app.utils import AppError


def H(name: str) -> str:
    """A stable, realistic-looking Notion ID for a name."""
    h = hashlib.md5(name.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


W = {"type": "workspace", "workspace": True}
P = lambda i: {"type": "page_id", "page_id": H(i)}            # noqa: E731
D = lambda i: {"type": "database_id", "database_id": H(i)}    # noqa: E731
B = lambda i: {"type": "block_id", "block_id": H(i)}          # noqa: E731


def page(i, title, parent, props=None):
    return {"object": "page", "id": H(i), "parent": parent, "icon": None,
            "properties": {"Name": {"type": "title", "title": [{"plain_text": title}]}, **(props or {})}}


def table(i, title, parent):
    return {"object": "database", "id": H(i), "parent": parent,
            "title": [{"plain_text": title}] if title else []}


def cells(**values):
    """Table cells in Notion's format: a list is a multi-select, text is a rich-text cell."""
    return {name: {"type": "multi_select", "multi_select": [{"name": v} for v in value]} if isinstance(value, list)
            else {"type": "rich_text", "rich_text": [{"plain_text": value}]} for name, value in values.items()}


def linked_to(*ids):
    return {"domain": {"type": "relation", "relation": [{"id": H(x)} for x in ids]}}


class FakeNotion:
    """Modelled on the real shared workspace: one top-level page per person, different templates."""

    def __init__(self):
        self.search = [
            page("arshaan", "Arshaan", W), page("ryan", "Ryan", W), page("efrain", "Efrain", W),
            # Arshaan: course/lecture template. "Courses" is a linked view of Domains; Topics links to Domains.
            page("uni", "University", P("arshaan")),
            table("coursesview", "", P("uni")),
            table("domains", "Domains", B("toggle")), table("topics", "Topics", B("toggle")),
            page("fow", "The Future of Work", D("domains")),
            page("dm", "Discrete Math for Computer Science", D("domains")),
            page("t1", "Review Lecture 6 — Discrete Math", D("topics"), linked_to("dm")),
            page("t2", "Review Lecture 7 — Discrete Math", D("topics"), linked_to("dm")),
            page("t3", "The Future of Work Week Three", D("topics"), linked_to("fow")),
            # Ryan: lectures are loose pages right on his page; UNI also has a Classes table.
            # The Classes table has no name (it sits under a "Classes" heading); its columns give it away.
            page("r_uni", "UNI", P("ryan")), table("r_classes", "", P("r_uni")),
            page("r_csc", "CSC108", D("r_classes")), page("r_psy", "Pshycology", D("r_classes")),
            page("r_mgm", "Intro to Management functions", D("r_classes")),
            page("r1", "csc lec 3", P("ryan")), page("r2", "csc lab 3", P("ryan")), page("r3", "mgm lec 4", P("ryan")),
            # Efrain: a plain Courses table (shown as a gallery), plus a timetable and other non-lecture tables.
            page("acad", "Academic", P("efrain")), table("ecourses", "Courses ", P("acad")),
            page("soc", "SOCSCI 1T03", D("ecourses"), cells(Semester=["Fall"], Status="In progress")),
            table("etimes", "class timetable", P("acad")),
            page("tt1", "SocSci 1T03 timetable", D("etimes"), cells(Days=["Monday"], Time="9:30 - 10:30")),
            page("tt2", "SocSci 1T03 TUTORIAL timetable", D("etimes")),
            table("eassess", "assesments", P("acad")), page("as1", "SOCSCI 1T03 essay notes", D("eassess")),
        ]
        self.schemas = {
            H("domains"): {"Name": {"type": "title"},
                           "Topics/Assignments": {"type": "relation", "relation": {"database_id": H("topics")}}},
            H("topics"): {"lecture/assignment": {"type": "title"},
                          "domain": {"type": "relation", "relation": {"database_id": H("domains")}},
                          "type": {"type": "select", "select": {"options": [{"name": "Studying"}, {"name": "lecture"}]}},
                          "date": {"type": "date"}, "due date": {"type": "formula"}},
            H("ecourses"): {"Name": {"type": "title"}},
            H("r_classes"): {"Name": {"type": "title"}, "Class code": {"type": "select", "select": {"options": []}},
                             "Credits": {"type": "number"}, "Teacher": {"type": "select", "select": {"options": []}},
                             "Grade": {"type": "number"}},
            H("etimes"): {"Name": {"type": "title"}, "Location": {"type": "rich_text"}},
            H("eassess"): {"Name": {"type": "title"}},
        }
        self.created: list[dict] = []    # bodies of POST /pages
        self.appended: list[tuple] = []  # (block id, children)

    def _find(self, raw):
        return next((o for o in self.search if notion.same_id(o["id"], raw)), None)

    def request(self, method, path, body=None, version=notion.VERSION):
        parts = path.split("?")[0].strip("/").split("/")
        if path == "/search":
            return {"results": self.search, "has_more": False}
        if parts[0] == "blocks" and len(parts) == 2:  # resolving a block's parent
            return {"parent": P("uni")} if notion.same_id(parts[1], H("toggle")) else {}
        if parts[0] == "blocks" and method == "PATCH":
            self.appended.append((parts[1], body["children"]))
            return {"results": [{"id": f"block{i}", "type": c["type"]} for i, c in enumerate(body["children"])]}
        if parts[0] == "pages" and method == "GET":
            obj = self._find(parts[1])
            if not obj:
                raise AppError("Notion error: not found")
            return {**obj, "url": f"https://notion.so/{parts[1]}"}
        if parts[0] == "pages" and method == "POST":
            self.created.append(body)
            return {"id": "newpage", "url": "https://notion.so/newpage"}
        if parts[0] == "databases":
            obj = self._find(parts[1])
            key = obj["id"] if obj else parts[1]
            if version == notion.DATA_SOURCES_VERSION:
                raise AppError("Notion error: does not contain any data sources accessible by this API bot")
            if key not in self.schemas:
                raise AppError("Notion error: linked databases are not supported")
            if method == "GET":
                return {"object": "database", "id": key, "title": obj["title"], "properties": self.schemas[key]}
            rows = [o for o in self.search if o["object"] == "page" and o["parent"].get("database_id") == key]
            f = (body or {}).get("filter")
            if f:
                want = f["relation"]["contains"]
                rows = [r for r in rows if any(notion.same_id(x["id"], want)
                                               for x in r["properties"].get(f["property"], {}).get("relation", []))]
            return {"results": rows, "has_more": False}
        raise AppError(f"unexpected {method} {path}")


EXTRAS = {
    "practice_questions": [{"q": "Two steps?", "a": "Base case, inductive step."}],
    "flashcards": [{"front": "Base case", "back": "P(1)"}],
    "quiz": [{"question": "First?", "options": ["a", "b", "c", "d"], "answer": 1, "explanation": "b"}],
    "cheat_sheet": ["P(1) and P(k)→P(k+1)"],
    "explanations": [{"topic": "Induction", "explanation": "Dominoes."}],
}


class FakeAI:
    """Stand-in for Groq: deterministic answers based on what the prompt asks for."""

    def __init__(self):
        self.prompts: list[str] = []
        self.fail_extras = False

    def chat_json(self, system, prompt, progress, fast=False, effort=None, max_wait=None):
        self.prompts.append(prompt)
        if "which student" in system:
            lecture = prompt.split("\nP0.")[0].lower()  # the lecture's part, before the list of people
            for subject, name, cls_start in (("discrete", "Arshaan", "Discrete"), ("management", "Ryan", "Intro to Management")):
                person = re.search(rf"^P(\d+)\. {name}", prompt, re.M)
                cls = re.search(rf"^   C(\d+): {cls_start}", prompt, re.M)
                if subject in lecture and person and cls:
                    return json.dumps({"person": f"P{person.group(1)}", "class": f"C{cls.group(1)}",
                                       "confident": True, "reason": f"{subject} lecture"})
            return json.dumps({"person": None, "confident": False})
        if "file lecture notes" in system:
            return json.dumps({"choice": 0, "reason": "That's where the other lectures are."})
        if "Here are notes from a lecture" in prompt:
            if self.fail_extras:
                raise AppError("busy")
            return json.dumps(EXTRAS)
        notes = {"title": "Proofs by Induction", "summary": "Induction.", "key_points": ["Base case first"],
                 "key_terms": [{"term": "Base case", "definition": "P(1)."}], "action_items": ["Read 5.1"]}
        asked = {k: v for k, v in EXTRAS.items() if f'"{k}":' in prompt}  # extras asked for with the notes
        if asked and self.fail_extras:
            raise AppError("busy")
        return json.dumps({**notes, **asked})

    def chat_text(self, system, messages, progress=None, fast=False, max_wait=None):
        self.prompts.append(system)
        return f"Answer to: {messages[-1]['content']}"
