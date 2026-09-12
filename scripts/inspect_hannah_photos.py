#!/usr/bin/env python3
"""Read-only: Hannah people + avatar files."""

from src.photos import avatars_dir, photo_exists, photo_file
from src.store import connect, db_path_from_config

conn = connect(db_path_from_config())
print("=== people Hannah* ===")
for row in conn.execute(
    """
    SELECT p.id, p.name, p.phone_id, p.location, p.in_contacts, p.lgs_lead_id,
           c.last_text, c.preview
    FROM people p
    LEFT JOIN chats c ON c.person_id = p.id
    WHERE p.name LIKE 'Hannah%' COLLATE NOCASE
    ORDER BY p.phone_id, p.name
    """
):
    print(dict(row))

print("=== photo_file ===")
for pid in ("toby", "archie"):
    path = photo_file("Hannah", pid)
    print(pid, path, "exists", photo_exists("Hannah", pid), "size", path.stat().st_size if path.is_file() else 0)

print("=== files matching hannah ===")
root = avatars_dir()
for path in sorted(root.rglob("*")):
    if path.is_file() and "hannah" in path.name.lower():
        print(path, path.stat().st_size)

print("=== phantom Hannah N files (toby/flat) ===")
for folder in (root, root / "toby", root / "archie"):
    if not folder.is_dir():
        continue
    for i in range(2, 12):
        path = folder / f"hannah-{i}.jpg"
        if path.is_file():
            print(path, path.stat().st_size)
