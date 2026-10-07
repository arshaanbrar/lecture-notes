from backend.app import check, config


def test_system_check_reports_each_part(client, fake_notion, fake_ai, monkeypatch, password):
    monkeypatch.setattr(config, "HIDDEN_PEOPLE", {"ryan"})
    monkeypatch.setattr(check, "_groq_models", lambda: {"openai/gpt-oss-120b", config.GROQ_WHISPER_MODEL})
    assert client.get("/api/check").status_code == 401  # password-protected
    data = client.get("/api/check", headers={"X-App-Password": password}).json()
    items = {i["name"]: i for i in data["items"]}
    assert items["Groq key"]["ok"] and items["AI for notes"]["ok"] and items["Transcription (Whisper)"]["ok"]
    assert items["Notion"]["ok"] and "Arshaan, Efrain (hidden: Ryan)" in items["Notion"]["detail"] 
    assert items["PDF reader"]["ok"] and items["PowerPoint reader"]["ok"]
    assert all(i["ok"] for i in data["items"] if i["required"]) == data["ok"]


def test_system_check_flags_a_missing_whisper_model_and_bad_key(client, fake_notion, fake_ai, monkeypatch):
    monkeypatch.setattr(check, "_groq_models", lambda: {"openai/gpt-oss-120b"})
    items = {i["name"]: i for i in client.get("/api/check").json()["items"]}
    assert not items["Transcription (Whisper)"]["ok"]

    def bad_key():
        raise check.AppError("Groq says the key is invalid.")
    monkeypatch.setattr(check, "_groq_models", bad_key)
    data = client.get("/api/check").json()
    assert not data["ok"] and "invalid" in {i["name"]: i for i in data["items"]}["Groq key"]["detail"]


def test_check_page_is_served(client):
    page = client.get("/check")
    assert page.status_code == 200 and "System check" in page.text
