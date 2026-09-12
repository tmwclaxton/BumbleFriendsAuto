#!/usr/bin/env python3
"""Read-only audit: same-name clusters + avatar contamination risk."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from src.photos import (
    _FACE_DIFFER,
    _FACE_MATCH,
    avatars_dir,
    face_distance,
    load_photo,
    photo_exists,
    photo_file,
    photo_slug,
)
from src.store import base_person_name, connect, db_path_from_config


def main() -> int:
    conn = connect(db_path_from_config())
    rows = list(
        conn.execute(
            """
            SELECT p.id, p.name, p.phone_id, p.location,
                   c.status, c.preview, c.last_text, c.last_from, c.message_until,
                   c.archived, c.in_group,
                   (SELECT COUNT(*) FROM messages m WHERE m.person_id=p.id) AS n,
                   (SELECT COUNT(*) FROM messages m WHERE m.person_id=p.id AND m.side='them') AS them_n
            FROM people p
            LEFT JOIN chats c ON c.person_id = p.id
            ORDER BY p.phone_id, p.name COLLATE NOCASE
            """
        )
    )

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        base = base_person_name(item["name"])
        groups[(item["phone_id"] or "toby", base)].append(item)

    clusters = {k: v for k, v in groups.items() if len(v) >= 2}
    print(f"people={len(rows)} clusters={len(clusters)}")
    print("=== same-name clusters (2+) ===")
    for (pid, base), people in sorted(clusters.items()):
        print(f"\n[{pid}] {base} ({len(people)})")
        for person in people:
            photo = "photo" if photo_exists(person["name"], pid) else "NO-PHOTO"
            preview = (person["preview"] or "")[:40]
            print(
                f"  {person['name']}: msgs={person['n']} them={person['them_n']} "
                f"status={person['status'] or '-'} arch={person['archived']} "
                f"{photo} preview={preview!r}"
            )

    print(f"\n=== face distance inside clusters (match<={_FACE_MATCH} differ>={_FACE_DIFFER}) ===")
    for (pid, base), people in sorted(clusters.items()):
        named = [p for p in people if photo_exists(p["name"], pid)]
        if len(named) < 2:
            continue
        for i, left in enumerate(named):
            la = load_photo(left["name"])
            if la is None:
                continue
            for right in named[i + 1 :]:
                lb = load_photo(right["name"])
                if lb is None:
                    continue
                try:
                    dist = face_distance(la, lb)
                except Exception as exc:
                    print(f"  [{pid}] {left['name']} vs {right['name']}: err {exc}")
                    continue
                if dist <= _FACE_MATCH:
                    flag = "SAME?"
                elif dist >= _FACE_DIFFER:
                    flag = "DIFF"
                else:
                    flag = "maybe"
                print(f"  [{pid}] {left['name']} vs {right['name']}: d={dist:.1f} {flag}")

    print("\n=== avatar files without people row ===")
    root = avatars_dir()
    known = {(str(r["phone_id"] or "toby"), photo_slug(r["name"])) for r in rows}
    phantoms: list[tuple[str, str, int]] = []
    for folder, pid in ((root / "toby", "toby"), (root / "archie", "archie"), (root, "toby")):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.jpg")):
            if path.name.startswith("_"):
                continue
            if folder == root and (root / "toby" / path.name).is_file():
                continue
            slug = path.stem
            if (pid, slug) in known:
                continue
            phantoms.append((pid, path.name, path.stat().st_size))
    print(f"phantoms={len(phantoms)}")
    for pid, name, size in phantoms[:50]:
        print(f"  {pid} {name} {size}")

    print("\n=== rich threads (them>=3) with a stored photo ===")
    rich = []
    for row in rows:
        if int(row["them_n"] or 0) < 3:
            continue
        pid = row["phone_id"] or "toby"
        if not photo_exists(row["name"], pid):
            continue
        path = photo_file(row["name"], pid)
        st = path.stat()
        when = datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M")
        rich.append((pid, row["name"], int(row["them_n"]), st.st_size, when, row["status"]))
    for item in rich:
        print(f"  [{item[0]}] {item[1]} them={item[2]} size={item[3]} mtime={item[4]} status={item[5]}")

    print("\n=== singleton bases that look expired-ish OR have no them-text ===")
    for (pid, base), people in sorted(groups.items()):
        if len(people) != 1:
            continue
        person = people[0]
        blob = f"{person['preview'] or ''} {person['last_text'] or ''} {person['status'] or ''}".lower()
        stub = int(person["them_n"] or 0) == 0
        expiredish = "expired" in blob or (person["status"] or "") == "expired"
        if not (stub or expiredish):
            continue
        photo = "photo" if photo_exists(person["name"], pid) else "NO-PHOTO"
        print(
            f"  [{pid}] {person['name']}: them={person['them_n']} msgs={person['n']} "
            f"status={person['status'] or '-'} {photo} preview={(person['preview'] or '')[:40]!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
