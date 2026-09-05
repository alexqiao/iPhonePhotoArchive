from __future__ import annotations

from pathlib import Path


def test_photos_mutation_is_confined_to_the_library_adapter() -> None:
    mutation_tokens = [
        "PHAsset" + "ChangeRequest",
        "delete" + "Assets(",
        "perform" + "ChangesAndWait",
    ]
    roots = [Path("src"), Path("native/photos-helper/Sources")]
    sources = [
        path for root in roots for path in root.rglob("*") if path.suffix in {".py", ".swift"}
    ]
    for token in mutation_tokens:
        matches = [source.name for source in sources if token in source.read_text(encoding="utf-8")]
        assert matches == ["PhotoLibraryAdapter.swift"]
    for source in sources:
        text = source.read_text(encoding="utf-8").lower()
        assert "osascript" not in text
        assert "recentlydeleted" not in text


def test_phone_deletion_is_confined_to_the_device_adapter() -> None:
    token = "request" + "DeleteFiles"
    matches = []
    for source in Path("native/photos-helper/Sources").rglob("*.swift"):
        if token in source.read_text(encoding="utf-8"):
            matches.append(source.name)
    assert matches == ["PhoneDeviceAdapter.swift"]
    adapter = Path(
        "native/photos-helper/Sources/PhotosHelperCore/PhoneDeviceAdapter.swift"
    ).read_text(encoding="utf-8")
    assert ".deleteAfterSuccessfulDownload: false" in adapter


def test_tokens_and_upload_urls_are_not_database_or_yaml_fields() -> None:
    schema = "\n".join(path.read_text(encoding="utf-8") for path in Path("src").rglob("*.sql"))
    config = Path("config/example.yaml").read_text(encoding="utf-8")
    for token in ["access_token", "refresh_token", "client_secret", "upload_url"]:
        assert token not in schema.lower()
        assert token not in config.lower()


def test_production_uses_only_apple_frameworks_for_cloud_photos() -> None:
    project = Path("pyproject.toml").read_text(encoding="utf-8").lower()
    package = Path("native/photos-helper/Package.swift").read_text(encoding="utf-8")
    plist = Path("native/photos-helper/Info.plist").read_text(encoding="utf-8")
    for dependency in ["httpx", "msal", "keyring"]:
        assert dependency not in project
    assert package.count('.linkedFramework("Photos")') == 1
    assert '.linkedFramework("ImageCaptureCore")' in package
    assert "NSCameraUsageDescription" in plist
    assert "NSPhotoLibraryUsageDescription" in plist
