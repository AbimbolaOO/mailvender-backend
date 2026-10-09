import json
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
    for collection in ("pages", "components", "brand", "saved_pages", "workspace"):
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
        ["pages", "components", "brand", "saved_pages", "workspace"])
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



def upload_content(session, storage, record_id, body: bytes, size=None):  # type: ignore[no-untyped-def]
    """Asks for a content upload and stores `body` the way the browser's presigned POST would."""
    response = session.post(f"/v1/sync/datasets/{record_id}/content-uploads", json={"size": size or len(body)})
    assert response.status_code == 201, response.text
    created = response.json()
    storage.objects[created["upload"]["fields"]["key"]] = (body, "application/json")
    return created["upload_id"], created["upload"]["fields"]["key"]


def put_dataset(session, record_id, revision, upload_id, summary=None):  # type: ignore[no-untyped-def]
    return session.put(f"/v1/sync/datasets/{record_id}", json={
        "expected_revision": revision, "data": summary or {"localId": "d1", "name": "Leads"},
        "content_upload_id": upload_id})


def test_dataset_content_lives_in_object_storage_not_postgres(make_user, storage, db) -> None:  # type: ignore[no-untyped-def]
    from app import models as m

    user = make_user()
    dataset = str(uuid.uuid4())
    content = json.dumps({"localId": "d1", "dataset": {"rows": [["a@example.com"]]}}).encode()
    upload_id, key = upload_content(user, storage, dataset, content)
    assert key.startswith(f"mailvender/sheets/{user.user_id}/{dataset}/") and key.endswith(".json")
    assert uuid.UUID(key.rsplit("/", 1)[-1].removesuffix(".json"))  # ids only, never the file's name
    assert storage.presigned[-1]["content_type"] == "application/json"

    created = put_dataset(user, dataset, 0, upload_id).json()
    assert created["data"] == {"localId": "d1", "name": "Leads"}
    assert created["content_size"] == len(content) and key in created["content_url"]
    row = db.session.get(m.SyncRecord, (uuid.UUID(user.account_id), "datasets", uuid.UUID(dataset)))
    assert row.object_key == key and "dataset" not in row.data
    assert db.session.query(m.SyncUpload).count() == 0
    changes = user.get("/v1/sync/changes", params={"since": 0}).json()["records"]
    assert key in changes[0]["content_url"]
    assert key in user.get("/v1/sync/snapshot").json()["records"][0]["content_url"]

    # An update stores a new object and removes the old one once committed.
    upload_id, new_key = upload_content(user, storage, dataset, b'{"localId":"d1","dataset":{"rows":[]}}')
    assert put_dataset(user, dataset, 1, upload_id).json()["revision"] == 2
    assert new_key in storage.objects and key not in storage.objects

    # Deleting removes the content too.
    deleted = user.delete(f"/v1/sync/datasets/{dataset}?expected_revision=2").json()
    assert deleted["deleted"] and deleted["content_url"] is None and new_key not in storage.objects


def test_dataset_writes_need_a_valid_upload(make_user, storage) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    dataset = str(uuid.uuid4())
    missing = user.put(f"/v1/sync/datasets/{dataset}", json={"expected_revision": 0, "data": {"localId": "d1"}})
    assert missing.status_code == 422 and missing.json()["error"]["fields"][0]["field"] == "content_upload_id"

    upload_id, key = upload_content(user, storage, dataset, b"{}")
    other = put_dataset(user, str(uuid.uuid4()), 0, upload_id)  # an upload is bound to its record
    assert other.status_code == 409 and other.json()["error"]["code"] == "content_upload_invalid"
    del storage.objects[key]  # the browser's upload never arrived
    assert put_dataset(user, dataset, 0, upload_id).json()["error"]["code"] == "content_upload_invalid"

    big = {"localId": "d1", "rows": ["x" * 100] * 1000}
    upload_id, _ = upload_content(user, storage, dataset, b"{}")
    assert put_dataset(user, dataset, 0, upload_id, summary=big).status_code == 422

    too_large = user.post(f"/v1/sync/datasets/{dataset}/content-uploads", json={"size": 101 * 1024 * 1024})
    assert too_large.status_code == 422
    assert user.post(f"/v1/sync/pages/{dataset}/content-uploads", json={"size": 10}).status_code == 422
    stranger = make_user()
    assert put_dataset(stranger, dataset, 0, upload_id).json()["error"]["code"] == "content_upload_invalid"


def test_a_conflicting_dataset_write_keeps_its_upload_for_the_retry(make_user, storage) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    dataset = str(uuid.uuid4())
    first, _ = upload_content(user, storage, dataset, b'{"v":1}')
    assert put_dataset(user, dataset, 0, first).status_code == 200
    second, key = upload_content(user, storage, dataset, b'{"v":2}')
    stale = put_dataset(user, dataset, 0, second)
    assert stale.status_code == 409 and stale.json()["error"]["details"]["current"]["revision"] == 1
    assert put_dataset(user, dataset, 1, second).status_code == 200 and key in storage.objects


def test_expired_uploads_are_cleaned_up(make_user, storage, db) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from app import models as m
    from app.security import utcnow
    from app.services.sync import SyncService

    user = make_user()
    _, key = upload_content(user, storage, str(uuid.uuid4()), b"{}")
    db.session.query(m.SyncUpload).update({"expires_at": utcnow() - timedelta(hours=2)})
    db.commit()
    assert SyncService(db, storage).cleanup_expired_uploads() == 1
    db.commit()
    assert key not in storage.objects and db.session.query(m.SyncUpload).count() == 0


def test_inline_datasets_move_to_object_storage(make_user, storage, db) -> None:  # type: ignore[no-untyped-def]
    from app import models as m
    from app.security import utcnow
    from app.services.sync import SyncService

    user = make_user()
    record_id = uuid.uuid4()
    data = {"localId": "d1", "dataset": {"name": "Old", "sourceFileName": "My leads (2024).csv",
                                         "rows": [["a"], ["b"]]}}
    db.session.add(m.SyncRecord(account_id=uuid.UUID(user.account_id), collection="datasets", id=record_id,
                                revision=3, data=data, deleted=False, seq=db.sync.next_seq(),
                                updated_at=utcnow()))
    db.commit()
    assert SyncService(db, storage).move_inline_content() == 1
    record = user.get(f"/v1/sync/datasets/{record_id}").json()
    assert record["data"] == {"localId": "d1", "name": "Old", "sourceFileName": "My leads (2024).csv",
                              "rowCount": 2} and record["revision"] == 3
    key = next(k for k in storage.objects if f"/sheets/{user.account_id}/{record_id}/" in k)
    assert json.loads(storage.objects[key][0]) == data and key in record["content_url"]
    assert SyncService(db, storage).move_inline_content() == 0


def test_clients_can_report_sync_problems(make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    report = {"kind": "sync_stuck", "status": "other-tab", "pending": 3, "seconds": 600}
    assert user.post("/v1/client-events", json=report).status_code == 204
    assert user.post("/v1/client-events", json={**report, "kind": "other"}).status_code == 422
