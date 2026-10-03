from tests.conftest import png_bytes


def request_upload(user, size=200, content_type="image/png", filename="Logo Final.png"):  # type: ignore[no-untyped-def]
    return user.post("/v1/assets/uploads", json={"filename": filename, "content_type": content_type, "size": size})


def test_upload_is_scoped_presigned_and_private(make_user, storage) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    response = request_upload(user)
    assert response.status_code == 201
    body = response.json()
    presigned = storage.presigned[-1]
    assert presigned["key"].startswith(f"mailvender/accounts/{user.account_id}/uploads/")
    assert presigned["content_type"] == "image/png"
    assert presigned["max_bytes"] == 200 and presigned["expires_in"] == 300
    assert body["upload"]["fields"]["key"] == presigned["key"]
    assert body["asset"]["status"] == "pending" and body["asset"]["public_url"] is None
    assert "aws" not in response.text.lower() or "x-amz-signature" in response.text  # only presigned fields


def test_upload_rejects_disallowed_types_and_sizes(make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    codes = {f["code"] for f in request_upload(user, content_type="image/svg+xml").json()["error"]["fields"]}
    assert codes == {"unsupported_type"}
    too_big = request_upload(user, size=5 * 1024 * 1024 + 1).json()["error"]["fields"][0]["code"]
    assert too_big == "too_large"


def test_finalize_validates_and_publishes(make_user, storage) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    body = request_upload(user, size=len(png_bytes(40, 20))).json()
    asset_id = body["asset"]["id"]
    missing = user.post(f"/v1/assets/{asset_id}/finalize")
    assert missing.status_code == 409 and missing.json()["error"]["code"] == "upload_missing"

    upload_key = storage.presigned[-1]["key"]
    storage.objects[upload_key] = (png_bytes(40, 20), "image/png")
    asset = user.post(f"/v1/assets/{asset_id}/finalize").json()
    assert asset["status"] == "ready"
    assert (asset["width"], asset["height"], asset["mime_type"]) == (40, 20, "image/png")
    public_key = asset["public_url"].removeprefix("https://cdn.test/")
    assert public_key.startswith(f"mailvender/accounts/{user.account_id}/public/")
    assert public_key.endswith("/Logo-Final.png")
    assert upload_key not in storage.objects and public_key in storage.objects
    assert user.get("/v1/assets").json()["items"][0]["id"] == asset_id

    assert user.delete(f"/v1/assets/{asset_id}").status_code == 204
    assert public_key not in storage.objects
    assert user.get("/v1/assets").json()["items"] == []


def test_finalize_rejects_unsafe_uploads(make_user, storage) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    cases = [
        (b"<svg onload=alert(1)>", "invalid_image", 100),
        (png_bytes(), "type_mismatch", 1000),  # declared as JPEG
        (b"x" * 600, "too_large", 500),  # larger than declared
    ]
    for content, code, declared in cases:
        content_type = "image/jpeg" if code == "type_mismatch" else "image/png"
        asset_id = request_upload(user, size=declared, content_type=content_type).json()["asset"]["id"]
        key = storage.presigned[-1]["key"]
        storage.objects[key] = (content, content_type)
        response = user.post(f"/v1/assets/{asset_id}/finalize")
        assert response.status_code == 422, code
        assert response.json()["error"]["code"] == code
        assert key not in storage.objects  # never published, upload removed
    assert user.get("/v1/assets").json()["items"] == []
