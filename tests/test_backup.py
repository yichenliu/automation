"""The daily backup: a whole copy even though the tenant tables keep each workspace's rows to themselves, and nothing
that looks like a backup when one fails (an empty zip would push a real one out of the ones kept)."""
import subprocess

import pytest

from app import backup


def _settings(tmp_path):
    return lambda: {"DATA_DIR": str(tmp_path), "BACKUP_KEEP": 2, "SQLALCHEMY_DATABASE_URI": "postgresql+psycopg://keel:pw@localhost:5433/keel"}


def test_the_copy_reads_the_tenant_tables_with_row_security_on(tmp_path, monkeypatch):
    """pg_dump refuses a table whose row security even its owner keeps to, unless told to read it with that on; with no
    workspace set, the policy shows every row."""
    said = {}

    def run(args, **kw):
        said["args"] = args
        open(args[args.index("-f") + 1], "wb").write(b"dump")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(backup, "settings", _settings(tmp_path))
    monkeypatch.setattr(backup.subprocess, "run", run)
    made = backup.make()
    assert "--enable-row-security" in said["args"] and made.exists() and made.stat().st_size > 22


def test_a_backup_that_fails_leaves_nothing_and_keeps_the_ones_before(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "settings", _settings(tmp_path))
    good = tmp_path / "backups" / "backup-20261101-080000.zip"
    good.parent.mkdir()
    good.write_bytes(b"a real one")
    monkeypatch.setattr(backup.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(
        args, 1, "", 'pg_dump: error: query would be affected by row-level security policy for table "approval"'))
    with pytest.raises(backup.BackupFailed) as failed:
        backup.make()
    assert "row-level security" in str(failed.value)
    assert sorted(p.name for p in good.parent.iterdir()) == [good.name]
