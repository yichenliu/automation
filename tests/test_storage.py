"""Files in S3-compatible object storage: kept in the bucket, read through a cache named by their contents."""
import io

from app import storage


class Bucket:
    def __init__(self):
        self.objects, self.gets = {}, 0

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        self.gets += 1
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def test_a_file_kept_in_object_storage_is_read_through_a_cache_and_fetched_once(app, owner, monkeypatch):
    _, ws = owner
    bucket = Bucket()
    app.config.update(FILES_BACKEND="s3", S3_BUCKET="keel-files", S3_PREFIX="tenants")
    store = storage.ObjectStore(app.config, client=bucket)
    monkeypatch.setattr(storage, "backend", lambda: store)
    f = storage.save(ws.id, b"quote for the Smiths", "quote.txt")
    assert [k for _, k in bucket.objects] == [f"tenants/{f.path}"]
    assert storage.path_of(f).read_bytes() == b"quote for the Smiths"
    assert storage.path_of(f).read_bytes() == b"quote for the Smiths"
    assert bucket.gets == 1  # the second read is the cache's
