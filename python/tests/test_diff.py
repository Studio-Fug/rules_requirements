# SPDX-License-Identifier: AGPL-3.0-or-later
from rules_requirements import diff
from rules_requirements._vendor import yaml
from rules_requirements.model import parse_documents


def model(text):
    return parse_documents([("x", d) for d in yaml.safe_load_all(text) if d])[0]


def test_diff_models():
    a = model(
        "user_needs: [{id: UN-1, title: A}, {id: UN-2, title: B}]\nrequirements: [{id: REQ-1, title: R, satisfies: [UN-1]}]\n"
    )
    b = model(
        "user_needs: [{id: UN-1, title: A2}, {id: UN-3, title: C}]\nrequirements: [{id: REQ-1, title: R, satisfies: [UN-1, UN-3], notes: [x]}]\n"
    )
    changes = diff.diff_models(a, b)
    assert [(c.id, c.change) for c in changes] == [
        ("UN-1", "modified"),
        ("UN-2", "removed"),
        ("UN-3", "added"),
        ("REQ-1", "modified"),
    ]
    assert changes[0].fields == {"title": ("A", "A2")}
    assert changes[3].fields["satisfies"] == (["UN-1"], ["UN-1", "UN-3"])
    assert "notes" in changes[3].fields
    assert "notes" not in diff.diff_models(a, b, ignore=("notes",))[3].fields
    assert diff.summarize(changes) == {"added": 1, "removed": 1, "modified": 2}
    text = diff.render_text(changes)
    assert "~ UN-1 (user_need) A2\n    title: 'A' -> 'A2'\n" in text and "- UN-2" in text and "+ UN-3" in text
    assert changes[0].to_dict()["fields"] == {"title": {"old": "A", "new": "A2"}}
    assert diff.render_text([]) == ""
    assert diff.diff_models(a, a) == []
