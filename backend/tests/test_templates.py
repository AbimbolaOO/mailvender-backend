import json
from pathlib import Path

import pytest

from app.cli import main as cli
from app.services.templates import FOLDER, TemplateService, parse_template, read_folder
from tests.conftest import sql_uow


def template(template_id: str, order: int = 0, **changes) -> dict:  # type: ignore[no-untyped-def]
    return {
        "id": template_id,
        "name": template_id.replace("-", " ").title(),
        "category": "Promotion",
        "description": "A test template.",
        "tags": ["test"],
        "order": order,
        "doc": {"version": 3, "settings": {"title": "Hi"}, "children": []},
        **changes,
    }


def write(folder: Path, *templates: dict) -> Path:  # type: ignore[type-arg]
    folder.mkdir(exist_ok=True)
    for old in folder.glob("*.json"):
        old.unlink()
    for item in templates:
        (folder / f"{item['id']}.json").write_text(json.dumps(item))
    return folder


def test_sync_adds_updates_and_removes(tmp_path: Path) -> None:
    folder = tmp_path / "templates"
    with sql_uow() as uow:
        result = TemplateService(uow).sync(read_folder(write(folder, template("a", 1), template("b", 0))))
        assert (result.added, result.updated, result.removed) == (["a", "b"], [], [])
        assert [row.id for row in TemplateService(uow).list_all()] == ["b", "a"]  # gallery order
        etag = TemplateService(uow).catalog_version()

        result = TemplateService(uow).sync(read_folder(folder))
        assert (result.added, result.updated, result.removed, result.unchanged) == ([], [], [], 2)
        assert TemplateService(uow).catalog_version() == etag

        result = TemplateService(uow).sync(read_folder(write(folder, template("a", 1, name="New name"), template("c", 2))))
        assert (result.added, result.updated, result.removed) == (["c"], ["a"], ["b"])
        assert TemplateService(uow).catalog_version() != etag
        assert [(row.id, row.name) for row in TemplateService(uow).list_all()] == [("a", "New name"), ("c", "C")]


@pytest.mark.parametrize(
    ("file_name", "data", "problem"),
    [
        ("a.json", template("a", doc={"version": 2, "settings": {}, "children": []}), "version 3"),
        ("b.json", template("a"), "must be named a.json"),
        ("A b.json", template("A b"), "lowercase"),
        ("a.json", template("a", order="1"), "'order'"),
        ("a.json", template("a", tags="x"), "'tags'"),
        ("a.json", template("a", name=""), "'name'"),
    ],
)
def test_invalid_files_are_refused(file_name: str, data: dict, problem: str) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError, match=problem):
        parse_template(file_name, json.dumps(data).encode())


def test_cli_sync_leaves_templates_alone_when_a_file_is_broken(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    folder = write(tmp_path / "templates", template("a"))
    assert cli(["sync-templates", str(folder)]) == 0
    (folder / "b.json").write_text("{not json")
    assert cli(["sync-templates", str(folder)]) == 1
    assert "b.json: not valid JSON" in capsys.readouterr().err
    with sql_uow() as uow:
        assert [row.id for row in TemplateService(uow).list_all()] == ["a"]


def seed(tmp_path: Path, *templates: dict) -> None:  # type: ignore[type-arg]
    with sql_uow() as uow:
        TemplateService(uow).sync(read_folder(write(tmp_path / "t", *templates)))


def test_endpoints_need_sign_in(client) -> None:  # type: ignore[no-untyped-def]
    for path in ("/v1/templates", "/v1/templates/categories", "/v1/templates/hero"):
        assert client.get(path).status_code == 401


def test_list_is_summaries_paginated_in_gallery_order(make_user, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    seed(tmp_path, *[template(f"t{n:02}", order=n) for n in range(5)])
    session = make_user()

    first = session.get("/v1/templates", params={"limit": 2})
    assert first.status_code == 200, first.text
    body = first.json()
    assert [item["id"] for item in body["items"]] == ["t00", "t01"]
    assert "doc" not in body["items"][0]  # documents come one at a time
    assert body["items"][0]["version"] and body["catalog_version"]

    ids = [item["id"] for item in body["items"]]
    while body["next_cursor"]:
        body = session.get("/v1/templates", params={"limit": 2, "cursor": body["next_cursor"]}).json()
        ids += [item["id"] for item in body["items"]]
    assert ids == ["t00", "t01", "t02", "t03", "t04"]

    assert session.get("/v1/templates", params={"cursor": "nonsense"}).status_code == 422
    assert session.get("/v1/templates", params={"limit": 101}).status_code == 422


def test_list_filters_by_category_and_words(make_user, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    seed(tmp_path,
         template("jollof", 0, name="Jollof Friday", tags=["food", "dark"]),
         template("suya", 1, name="Suya sheet", category="Newsletter", tags=["food"]),
         template("orbit", 2, name="Seat to orbit", category="Concept", description="A 100% space ticket.", tags=["space"]))
    session = make_user()
    ids = lambda **params: [i["id"] for i in session.get("/v1/templates", params=params).json()["items"]]  # noqa: E731

    assert ids(category="Newsletter") == ["suya"]
    assert ids(q="food") == ["jollof", "suya"]
    assert ids(q="FOOD dark") == ["jollof"]  # every word, any case
    assert ids(q="food", category="Newsletter") == ["suya"]
    assert ids(q="100%") == ["orbit"]  # LIKE wildcards are matched literally
    assert ids(q="_") == []


def test_categories_and_one_template(make_user, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    seed(tmp_path, template("a", 0, category="Promotion"), template("b", 1, category="Concept"),
         template("c", 2, category="Promotion"))
    session = make_user()

    body = session.get("/v1/templates/categories").json()
    assert body["total"] == 3
    assert body["categories"] == [{"name": "Promotion", "count": 2}, {"name": "Concept", "count": 1}]

    one = session.get("/v1/templates/b")
    assert one.status_code == 200
    assert one.json()["doc"] == {"version": 3, "settings": {"title": "Hi"}, "children": []}
    summary = session.get("/v1/templates", params={"category": "Concept"}).json()["items"][0]
    assert one.json()["version"] == summary["version"]
    assert session.get("/v1/templates/missing").status_code == 404


def test_every_response_supports_etags(make_user, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    seed(tmp_path, template("hero", 0))
    session = make_user()
    for path in ("/v1/templates", "/v1/templates/categories", "/v1/templates/hero"):
        response = session.get(path)
        assert response.headers["cache-control"] == "private, no-cache"
        again = session.get(path, headers={"If-None-Match": response.headers["etag"]})
        assert again.status_code == 304 and again.content == b"", path
        assert session.get(path, headers={"If-None-Match": '"old"'}).status_code == 200

    # A change to any template makes the list and categories stale; the unchanged one stays valid.
    hero_etag = session.get("/v1/templates/hero").headers["etag"]
    list_etag = session.get("/v1/templates").headers["etag"]
    seed(tmp_path, template("hero", 0), template("new", 1))
    assert session.get("/v1/templates", headers={"If-None-Match": list_etag}).status_code == 200
    assert session.get("/v1/templates/hero", headers={"If-None-Match": hero_etag}).status_code == 304


def test_categories_is_a_reserved_id() -> None:
    with pytest.raises(ValueError, match="reserved"):
        parse_template("categories.json", json.dumps(template("categories")).encode())


def test_the_bundled_templates_are_valid() -> None:
    # Exported from the studio (npm run templates:export); the API syncs them on start.
    files = read_folder(FOLDER)
    assert len(files) >= 40
    assert len({file.order for file in files}) == len(files)


def test_large_responses_are_compressed(make_user) -> None:  # type: ignore[no-untyped-def]
    with sql_uow() as uow:
        TemplateService(uow).sync(read_folder(FOLDER))
    biggest = max(read_folder(FOLDER), key=lambda file: len(json.dumps(file.doc)))
    response = make_user().get(f"/v1/templates/{biggest.id}", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert int(response.headers["content-length"]) < len(json.dumps(biggest.doc)) / 4
