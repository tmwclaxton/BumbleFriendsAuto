#!/usr/bin/env python3
"""High-risk namesake contamination: rich chat vs empty/expired stub."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from src.photos import (
    _FACE_DIFFER,
    _FACE_MATCH,
    face_distance,
    load_photo,
    photo_exists,
    photo_file,
)
from src.store import base_person_name, connect, db_path_from_config


def main() -> int:
    conn = connect(db_path_from_config())
    rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT p.id, p.name, p.phone_id,
                   c.status, c.preview, c.last_text, c.archived,
                   (SELECT COUNT(*) FROM messages m WHERE m.person_id=p.id) AS n,
                   (SELECT COUNT(*) FROM messages m WHERE m.person_id=p.id AND m.side='them') AS them_n
            FROM people p
            LEFT JOIN chats c ON c.person_id = p.id
            """
        )
    ]
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["phone_id"] or "toby", base_person_name(row["name"]))].append(row)

    print("=== HIGH RISK: rich thread (them>=2) + empty/expired sibling ===")
    risky = []
    for (pid, base), people in sorted(groups.items()):
        if len(people) < 2:
            continue
        rich = [p for p in people if int(p["them_n"] or 0) >= 2]
        stubs = []
        for p in people:
            blob = f"{p['preview'] or ''} {p['last_text'] or ''} {p['status'] or ''}".lower()
            if int(p["them_n"] or 0) == 0 or "expired" in blob or (p["status"] or "") == "expired":
                stubs.append(p)
        if not rich or not stubs:
            continue
        for r in rich:
            for s in stubs:
                if r["id"] == s["id"]:
                    continue
                rp = photo_exists(r["name"], pid)
                sp = photo_exists(s["name"], pid)
                dist = None
                flag = "one-missing-photo"
                if rp and sp:
                    la, lb = load_photo(r["name"]), load_photo(s["name"])
                    if la is not None and lb is not None:
                        dist = face_distance(la, lb)
                        if dist <= _FACE_MATCH:
                            flag = "SAME-FACE (possible shared wrong crop)"
                        elif dist >= _FACE_DIFFER:
                            flag = "DIFF (split looks ok)"
                        else:
                            flag = "maybe"
                rpath = photo_file(r["name"], pid) if rp else None
                spath = photo_file(s["name"], pid) if sp else None
                rm = (
                    datetime.fromtimestamp(rpath.stat().st_mtime, timezone.utc).strftime("%m-%d %H:%M")
                    if rpath and rpath.is_file()
                    else "-"
                )
                sm = (
                    datetime.fromtimestamp(spath.stat().st_mtime, timezone.utc).strftime("%m-%d %H:%M")
                    if spath and spath.is_file()
                    else "-"
                )
                risky.append((pid, base, r, s, flag, dist, rm, sm, rp, sp))

    for pid, base, r, s, flag, dist, rm, sm, rp, sp in risky:
        dtxt = f"d={dist:.1f}" if dist is not None else "d=-"
        print(
            f"[{pid}] {base}: RICH {r['name']} them={r['them_n']} photo={rp}@{rm} "
            f"| STUB {s['name']} them={s['them_n']} status={s['status']} "
            f"preview={(s['preview'] or '')[:28]!r} photo={sp}@{sm} | {dtxt} {flag}"
        )

    print(f"\ntotal high-risk pairs={len(risky)}")
    print("\n=== SINGLETON rich chats (them>=5) — possible absorbed expired twin ===")
    for row in sorted(rows, key=lambda r: (-int(r["them_n"] or 0), r["name"])):
        if int(row["them_n"] or 0) < 5:
            continue
        pid = row["phone_id"] or "toby"
        base = base_person_name(row["name"])
        siblings = groups[(pid, base)]
        if len(siblings) != 1:
            continue
        has = photo_exists(row["name"], pid)
        when = "-"
        size = 0
        if has:
            path = photo_file(row["name"], pid)
            when = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
            size = path.stat().st_size
        print(
            f"  [{pid}] {row['name']} them={row['them_n']} photo={has} size={size} "
            f"mtime={when} status={row['status']} preview={(row['preview'] or '')[:36]!r}"
        )

    print("\n=== Hannah / Ru / Josh / David / Max status ===")
    for want in ("Hannah", "Ru", "Josh", "David", "Max", "Alex", "Sam", "Harry", "Tommy"):
        for row in rows:
            if base_person_name(row["name"]).casefold() != want.casefold():
                continue
            pid = row["phone_id"] or "toby"
            print(
                f"  [{pid}] {row['name']}: them={row['them_n']} msgs={row['n']} "
                f"status={row['status']} photo={photo_exists(row['name'], pid)} "
                f"preview={(row['preview'] or '')[:40]!r}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
