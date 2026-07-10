"""Tiny admin CLI for the TCER web backend (pure stdlib).

Usage:
    python web/backend/manage.py adduser <username> <password>
    python web/backend/manage.py passwd  <username> <password>
    python web/backend/manage.py listusers
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402


def main(argv: list[str]) -> int:
    db.init_db()
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "adduser" and len(argv) == 3:
        ok = db.create_user(argv[1], argv[2])
        print("created" if ok else "username already exists")
        return 0 if ok else 1
    if cmd == "passwd" and len(argv) == 3:
        conn = db.connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE username=?", (argv[1],)
            ).fetchone()
        finally:
            conn.close()
        if not exists:
            print("no such user")
            return 1
        # Reset by delete + recreate (keeps hashing in one place).
        conn = db.connect()
        try:
            conn.execute("DELETE FROM users WHERE username=?", (argv[1],))
            conn.commit()
        finally:
            conn.close()
        db.create_user(argv[1], argv[2])
        print("password updated")
        return 0
    if cmd == "listusers":
        conn = db.connect()
        try:
            for r in conn.execute("SELECT username, created_at FROM users ORDER BY username"):
                print(f"{r['username']}\t{r['created_at']}")
        finally:
            conn.close()
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))