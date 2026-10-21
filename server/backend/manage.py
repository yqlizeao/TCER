"""Tiny admin CLI for the TCER server backend (pure stdlib).

Usage:
    python server/backend/manage.py adduser <username> <password>
    python server/backend/manage.py passwd  <username> <password>
    python server/backend/manage.py listusers
    python server/backend/manage.py setroles <username> [role ...]   # 概念模块角色（空=仅成员）
        角色：concept_owner reviewer manager admin
    python server/backend/manage.py import-session <jsonl-file> [--user USER] [--project PROJ] [--public]
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
    if cmd == "setroles" and len(argv) >= 2:
        from concepts import rbac
        try:
            rbac.set_roles(argv[1], argv[2:])
        except ValueError as e:
            print(e)
            return 1
        print(f"{argv[1]}: {', '.join(sorted(rbac.roles_of(argv[1])))}")
        return 0
    if cmd == "import-session" and len(argv) >= 2:
        import time
        file_path = Path(argv[1])
        if not file_path.is_file():
            print(f"File not found: {file_path}")
            return 1
        user = "admin"
        project = None
        is_public = False
        i = 2
        while i < len(argv):
            if argv[i] == "--user" and i + 1 < len(argv):
                user = argv[i + 1]
                i += 2
            elif argv[i] == "--project" and i + 1 < len(argv):
                project = argv[i + 1]
                i += 2
            elif argv[i] == "--public":
                is_public = True
                i += 1
            else:
                i += 1
        import workbuddy_reader
        row = workbuddy_reader.parse_session_file(file_path, user=user, project=project)
        actual_project = project or row.get("project") or "WorkBuddy"
        row["project"] = actual_project
        now_s = int(time.time())
        n = db.insert_records(
            uploaded_by=user,
            person=user,
            project=actual_project,
            aggregate=None,
            sessions=[row],
            generated_at=now_s,
            semantic_consent=True,
        )
        if is_public:
            conn = db.connect()
            try:
                conn.execute(
                    "UPDATE uploads SET visibility='public' WHERE session_id=?",
                    (row.get("session_id"),),
                )
                conn.commit()
            finally:
                conn.close()
        print(
            f"Imported session '{row.get('session_id')}' into project '{actual_project}' "
            f"(title: {row.get('title')}, visibility: {'public' if is_public else 'private'})"
        )
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))