import uuid


def put(session, collection, record_id, revision, data, parent=None):  # type: ignore[no-untyped-def]
    body = {"expected_revision": revision, "data": data}
    if parent:
        body["parent_id"] = parent
    return session.put(f"/v1/sync/{collection}/{record_id}", json=body)


def test_records_have_revisions_and_conflicts_do_not_overwrite(make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    page = str(uuid.uuid4())
    created = put(user, "pages", page, 0, {"name": "Welcome", "doc": {"v": 1}}).json()
    assert created["revision"] == 1 and created["deleted"] is False and created["updated_at"]
    assert put(user, "pages", page, 0, {"name": "dup"}).status_code == 409
    updated = put(user, "pages", page, 1, {"name": "Welcome", "doc": {"v": 2}}).json()
    assert updated["revision"] == 2

    stale = put(user, "pages", page, 1, {"name": "Other device", "doc": {"v": 99}})
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "revision_conflict"
    assert error["details"]["current"]["revision"] == 2
    assert error["details"]["current"]["data"]["doc"] == {"v": 2}
    assert user.get(f"/v1/sync/pages/{page}").json()["data"]["doc"] == {"v": 2}

    # Last write wins when the client retries with the current revision.
    assert put(user, "pages", page, 2, {"name": "Other device", "doc": {"v": 99}}).json()["revision"] == 3


def test_deletes_leave_tombstones_in_the_change_feed(make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    put(user, "components", a, 0, {"name": "Header"})
    put(user, "components", b, 0, {"name": "Footer"})
    assert user.delete(f"/v1/sync/components/{a}?expected_revision=5").status_code == 409
    deleted = user.delete(f"/v1/sync/components/{a}?expected_revision=1").json()
    assert deleted["deleted"] is True and deleted["revision"] == 2 and deleted["data"] is None

    feed = user.get("/v1/sync/changes?since=0").json()
    assert [(r["id"], r["deleted"]) for r in feed["records"]] == [(b, False), (a, True)]
    assert feed["has_more"] is False
    later = user.get(f"/v1/sync/changes?since={feed['next_since']}").json()
    assert later["records"] == []
    snapshot = user.get("/v1/sync/snapshot").json()
    assert [r["id"] for r in snapshot["records"]] == [b]  # live records only
    assert snapshot["watermark"] == feed["next_since"]


def test_new_device_hydrates_from_snapshot_then_changes(make_user, client) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import login

    user = make_user()
    for collection in ("pages", "components", "brand", "datasets", "saved_pages", "workspace"):
        put(user, collection, str(uuid.uuid4()), 0, {"from": collection})
    second = login(client, user.email)  # another browser
    pages, cursor = [], None
    while True:
        url = "/v1/sync/snapshot?limit=2" + (f"&cursor={cursor}" if cursor else "")
        body = second.get(url).json()
        pages.extend(body["records"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert sorted(r["collection"] for r in pages) == sorted(
        ["pages", "components", "brand", "datasets", "saved_pages", "workspace"])
    put(user, "pages", str(uuid.uuid4()), 0, {"later": True})
    changes = second.get(f"/v1/sync/changes?since={body['watermark']}").json()["records"]
    assert [r["data"] for r in changes] == [{"later": True}]


def test_unknown_collections_are_rejected(make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    assert put(user, "secrets", str(uuid.uuid4()), 0, {}).status_code == 422


def test_version_history_and_restore(make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    page = str(uuid.uuid4())
    put(user, "pages", page, 0, {"localId": "page_a", "name": "P", "doc": {"text": "first"}})
    assert put(user, "versions", str(uuid.uuid4()), 0, {"doc": {}}).status_code == 422  # parent required
    version = str(uuid.uuid4())
    put(user, "versions", version, 0, {"pageId": "page_a", "name": "Draft 1", "auto": False,
                                       "doc": {"text": "first"}}, parent=page)
    put(user, "pages", page, 1, {"localId": "page_a", "name": "P", "doc": {"text": "second"}})

    versions = user.get(f"/v1/pages/{page}/versions").json()["items"]
    assert [v["data"]["name"] for v in versions] == ["Draft 1"]
    assert versions[0]["updated_by"] == user.user_id and versions[0]["created_at"]

    restored = user.post(f"/v1/pages/{page}/versions/{version}/restore").json()
    assert restored["data"]["doc"] == {"text": "first"} and restored["revision"] == 3
    versions = user.get(f"/v1/pages/{page}/versions").json()["items"]
    assert versions[0]["data"]["name"] == "Before restoring “Draft 1”"
    assert versions[0]["data"]["doc"] == {"text": "second"}  # current version kept
    assert len(versions) == 2
