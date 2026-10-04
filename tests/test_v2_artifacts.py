import pytest
from dradar.v2.artifacts import Artifacts, ArtifactError

def test_snapshot_is_durable_and_independent_of_source(tmp_path):
    source = tmp_path / "source.patch"
    source.write_bytes(b"synthetic patch")
    store = Artifacts(tmp_path / "outputs")
    first = store.save("a", "e", {"patch": source})
    source.write_bytes(b"source later changes")
    assert store.inspect("a", "e") == first
    assert (tmp_path / "outputs/a/patch").read_bytes() == b"synthetic patch"
    assert store.save("a", "e", {"patch": source}) == first

def test_snapshot_ownership_is_immutable(tmp_path):
    source = tmp_path / "source"
    source.write_text("synthetic")
    store = Artifacts(tmp_path / "outputs")
    store.save("a", "e", {"patch": source})
    with pytest.raises(ArtifactError):
        store.save("a", "different", {"patch": source})

def test_corruption_is_explicit_not_replaced(tmp_path):
    source = tmp_path / "source"
    source.write_text("synthetic")
    store = Artifacts(tmp_path / "outputs")
    store.save("a", "e", {"patch": source})
    (tmp_path / "outputs/a/patch").write_text("corrupt")
    with pytest.raises(ArtifactError):
        store.inspect("a", "e")
    with pytest.raises(ArtifactError):
        store.save("a", "e", {"patch": source})

@pytest.mark.parametrize("name", ["../escape", "..", "manifest.json"])
def test_traversal_and_reserved_names_rejected(tmp_path, name):
    with pytest.raises(ArtifactError):
        Artifacts(tmp_path / "outputs").save("a", "e", {name: tmp_path / "source"})

def test_symlink_source_rejected(tmp_path):
    real = tmp_path / "source"
    real.write_text("synthetic")
    linked = tmp_path / "link"
    linked.symlink_to(real)
    with pytest.raises(ArtifactError):
        Artifacts(tmp_path / "outputs").save("a", "e", {"patch": linked})
    assert not (tmp_path / "outputs/a").exists()

def test_failed_copy_does_not_publish_partial_result(tmp_path):
    source = tmp_path / "source"
    source.write_text("synthetic")
    with pytest.raises(OSError):
        Artifacts(tmp_path / "outputs").save("a", "e", {"patch": source, "result": tmp_path / "missing"})
    assert not (tmp_path / "outputs/a").exists()
    assert not list((tmp_path / "outputs").glob(".saving-*"))

@pytest.mark.skipif(not hasattr(__import__("os"), "mkfifo"), reason="POSIX FIFO")
def test_nonregular_source_does_not_block(tmp_path):
    import os
    source = tmp_path / "fifo"
    os.mkfifo(source)
    with pytest.raises(ArtifactError):
        Artifacts(tmp_path / "outputs").save("a", "e", {"patch": source})
