import os
import shutil
import sqlite3
import tempfile
import pytest


def _create_online_sqlite_backup(src_path: str, dst_path: str) -> None:
    """Matches the deploy_prod.sh online SQLite backup mechanism."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30.0)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst, pages=100)
        finally:
            dst.close()
    finally:
        src.close()

    chk = sqlite3.connect(dst_path)
    try:
        res = chk.execute("PRAGMA integrity_check;").fetchall()
        if res != [("ok",)]:
            raise RuntimeError(f"Backup integrity check failed: {res}")
    finally:
        chk.close()


def test_sqlite_online_backup_wal_consistency():
    """Verify that SQLite online backup captures committed WAL transactions and verifies integrity."""
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "database.db")
        backup_path = os.path.join(td, "backup.db")

        # 1. Setup DB with WAL mode
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, balance REAL);")
        conn.execute("INSERT INTO users VALUES (1, 100.0), (2, 250.0);")
        conn.commit()

        # Add more transactions without explicit checkpoint
        conn.execute("INSERT INTO users VALUES (3, 500.0);")
        conn.commit()

        # 2. Execute online backup while connection is still open
        _create_online_sqlite_backup(db_path, backup_path)
        conn.close()

        # 3. Verify backup database file
        assert os.path.exists(backup_path)
        chk = sqlite3.connect(backup_path)
        try:
            res = chk.execute("PRAGMA integrity_check;").fetchall()
            assert res == [("ok",)]
            rows = chk.execute("SELECT id, balance FROM users ORDER BY id").fetchall()
            assert rows == [(1, 100.0), (2, 250.0), (3, 500.0)]
        finally:
            chk.close()


def test_sqlite_online_backup_detects_corruption():
    """Verify that corrupt databases fail integrity check and abort backup."""
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "corrupt.db")
        backup_path = os.path.join(td, "backup.db")

        # Create pseudo-corrupt file
        with open(db_path, "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\xff" * 1000)

        with pytest.raises(Exception):
            _create_online_sqlite_backup(db_path, backup_path)


def test_rollback_preserves_database_and_cleans_new_files():
    """
    Verify the deployment rollback contract:
    - Restores old application files.
    - Removes newly introduced files.
    - Does NOT automatically revert the live SQLite database (prevents data destruction).
    - Preserves pre-deploy database backup intact in backup directory.
    """
    with tempfile.TemporaryDirectory() as td:
        remote_dir = os.path.join(td, "app")
        backup_dir = os.path.join(td, "backup")
        os.makedirs(remote_dir, exist_ok=True)
        os.makedirs(backup_dir, exist_ok=True)

        # 1. Initial production state
        old_main = os.path.join(remote_dir, "main.py")
        db_file = os.path.join(remote_dir, "database.db")
        rev_file = os.path.join(remote_dir, "REVISION")

        with open(old_main, "w", encoding="utf-8") as f:
            f.write("# Version 1.0\nprint('old')\n")
        with open(rev_file, "w", encoding="utf-8") as f:
            f.write("rev-1.0\n")

        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, note TEXT);")
        conn.execute("INSERT INTO users VALUES (1, 'initial_user');")
        conn.commit()
        conn.close()

        # 2. Simulate pre-deploy backup creation
        shutil.copy2(old_main, os.path.join(backup_dir, "main.py"))
        _create_online_sqlite_backup(db_file, os.path.join(backup_dir, "database.db"))
        new_files_list = os.path.join(backup_dir, "new_files.txt")
        with open(new_files_list, "w", encoding="utf-8") as f:
            f.write("new_feature.py\n")

        # 3. Simulate failed release: new file introduced, main updated, and new transactions occur
        new_feature = os.path.join(remote_dir, "new_feature.py")
        with open(new_feature, "w", encoding="utf-8") as f:
            f.write("# New feature file introduced in release\n")

        with open(old_main, "w", encoding="utf-8") as f:
            f.write("# Version 2.0 (broken)\nraise RuntimeError('crash')\n")

        # New user registered right before or during deployment
        conn = sqlite3.connect(db_file)
        conn.execute("INSERT INTO users VALUES (2, 'in_flight_user');")
        conn.commit()
        conn.close()

        # 4. Simulate deploy_prod.sh rollback() execution
        # Revert saved code files
        shutil.copy2(os.path.join(backup_dir, "main.py"), remote_dir)

        # Clean up newly introduced files listed in new_files.txt
        if os.path.exists(new_files_list):
            with open(new_files_list, "r", encoding="utf-8") as f:
                for line in f:
                    name = line.strip()
                    if name:
                        target = os.path.join(remote_dir, name)
                        if os.path.exists(target):
                            os.remove(target)

        # Restore REVISION
        with open(rev_file, "w", encoding="utf-8") as f:
            f.write("rev-1.0\n")

        # NOTE: Database is deliberately NOT overwritten

        # 5. Assertions on post-rollback state
        # Code was restored
        with open(old_main, "r", encoding="utf-8") as f:
            assert "Version 1.0" in f.read()

        # Newly introduced file was purged
        assert not os.path.exists(new_feature), "Newly introduced file must be removed during rollback"

        # Revision was preserved
        with open(rev_file, "r", encoding="utf-8") as f:
            assert f.read().strip() == "rev-1.0"

        # Database was NOT reverted; newer user transaction is preserved
        conn = sqlite3.connect(db_file)
        rows = conn.execute("SELECT id, note FROM users ORDER BY id").fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[1] == (2, "in_flight_user"), "In-flight user transaction must not be destroyed by rollback"

        # Backup file remains available in backup directory for explicit manual restore
        assert os.path.exists(os.path.join(backup_dir, "database.db"))
