import pytest

from src.schemas import DevNoteContent, validate_dev_content


QUESTION_ID = "2b6f5f5f-90d6-43ef-a6b4-c14fd319e6db"


def valid_dev_note_content(kind: str) -> dict:
    content = {
        "kind": kind,
        "body": "Reviewed the implementation notes.",
    }
    if kind == "answer":
        content["answers_ref"] = QUESTION_ID
    if kind == "attachment":
        content["url"] = "https://example.com/design.md"
    if kind == "review":
        content["verdict"] = "approve"
    return content


@pytest.mark.parametrize(
    "kind",
    ["comment", "question", "answer", "status", "attachment", "review"],
)
def test_dev_note_schema_accepts_each_valid_kind(kind):
    validate_dev_content("note", valid_dev_note_content(kind))


def test_dev_note_schema_defaults_question_blocking_to_false():
    note = DevNoteContent.model_validate(valid_dev_note_content("question"))

    assert note.blocking is False


def test_dev_note_schema_accepts_blocking_question():
    content = valid_dev_note_content("question")
    content["blocking"] = True

    validate_dev_content("note", content)


def test_dev_note_schema_rejects_answer_without_answers_ref():
    content = valid_dev_note_content("answer")
    del content["answers_ref"]

    with pytest.raises(Exception):
        validate_dev_content("note", content)


def test_dev_note_schema_rejects_attachment_without_url():
    content = valid_dev_note_content("attachment")
    del content["url"]

    with pytest.raises(Exception):
        validate_dev_content("note", content)


def test_dev_note_schema_rejects_review_without_verdict():
    content = valid_dev_note_content("review")
    del content["verdict"]

    with pytest.raises(Exception):
        validate_dev_content("note", content)


@pytest.mark.parametrize(
    "field,value",
    [
        ("answers_ref", QUESTION_ID),
        ("answers_ref", None),
        ("url", "https://example.com/design.md"),
        ("url", None),
        ("verdict", "request-changes"),
        ("verdict", None),
    ],
)
def test_dev_note_schema_rejects_kind_specific_field_on_wrong_kind(field, value):
    content = valid_dev_note_content("comment")
    content[field] = value

    with pytest.raises(Exception):
        validate_dev_content("note", content)


def test_dev_note_schema_rejects_blocking_true_on_non_question():
    content = valid_dev_note_content("status")
    content["blocking"] = True

    with pytest.raises(Exception):
        validate_dev_content("note", content)


def test_dev_note_schema_rejects_blocking_field_on_non_question():
    content = valid_dev_note_content("status")
    content["blocking"] = False

    with pytest.raises(Exception):
        validate_dev_content("note", content)


@pytest.mark.parametrize("body", ["", "   ", "\n\t"])
def test_dev_note_schema_rejects_blank_body(body):
    content = valid_dev_note_content("comment")
    content["body"] = body

    with pytest.raises(Exception):
        validate_dev_content("note", content)
