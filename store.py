"""Independent source snapshots, manual additions, and persistent deletions."""

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from .locations import coordinates, distance
from .snapshot_store import (
    COORDINATE_PRESERVING_ORIGINS,
    RANKABLE_ORIGINS,
    SOURCE_MANUAL,
)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS points (
                    game TEXT NOT NULL, id TEXT NOT NULL, origin TEXT NOT NULL,
                    name TEXT NOT NULL, address TEXT NOT NULL, lat REAL, lng REAL,
                    PRIMARY KEY(game,id)
                );
                CREATE TABLE IF NOT EXISTS hidden (game TEXT, id TEXT, PRIMARY KEY(game,id));
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS geocache (address TEXT PRIMARY KEY, result TEXT NOT NULL);
            """)

    def meta(self, key, value=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            if value is not None:
                db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, str(value))
                )
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else ""

    def replace_source(self, game, points, stamp, origin=None):
        """整体替换某来源的点位。``origin=None`` 时替换除人工外的全部来源。

        换来源期间应显式传入 ``origin``：否则会把上一版来源的记录一并删掉，
        而按迁移方案旧记录应当保留在库中（只是不参与排序）。
        """
        if not points:
            raise ValueError("同步结果为空，保留旧数据")
        with closing(sqlite3.connect(self.path)) as db, db:
            if origin is None:
                db.execute(
                    "DELETE FROM points WHERE game=? AND origin!=?", (game, SOURCE_MANUAL)
                )
            else:
                db.execute(
                    "DELETE FROM points WHERE game=? AND origin=?", (game, origin)
                )
            db.executemany(
                "INSERT INTO points VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        game,
                        p["id"],
                        p["origin"],
                        p["name"],
                        p["address"],
                        p.get("lat"),
                        p.get("lng"),
                    )
                    for p in points
                ],
            )
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                (f"updated:{game}", stamp),
            )

    def all(self, game):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in db.execute(
                    """SELECT p.* FROM points p
                LEFT JOIN hidden h ON p.game=h.game AND p.id=h.id
                WHERE p.game=? AND h.id IS NULL""",
                    (game,),
                )
            ]

    def merge_verified(self, game, points):
        """Publish validated members without erasing manual rows or deletion markers."""
        preserving = tuple(sorted(COORDINATE_PRESERVING_ORIGINS))
        marks = ",".join("?" for _ in preserving)
        with closing(sqlite3.connect(self.path)) as db, db:
            for p in points:
                db.execute(
                    """INSERT INTO points VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(game,id) DO UPDATE SET origin=excluded.origin,
                    name=excluded.name,address=excluded.address,
                    lat=CASE WHEN points.address=excluded.address
                        AND points.origin IN (%s)
                        THEN points.lat ELSE excluded.lat END,
                    lng=CASE WHEN points.address=excluded.address
                        AND points.origin IN (%s)
                        THEN points.lng ELSE excluded.lng END
                    WHERE points.origin!='manual'""" % (marks, marks),
                    (
                        game,
                        p["id"],
                        p["origin"],
                        p["name"],
                        p["address"],
                        p["lat"],
                        p["lng"],
                    )
                    + preserving
                    + preserving,
                )

    def nearest(self, game, location, limit=5):
        found = []
        for p in self.all(game):
            if (
                p["origin"] in RANKABLE_ORIGINS
                and p["lat"] is not None
                and p["lng"] is not None
            ):
                p["distance"] = distance(
                    location["lat"], location["lng"], p["lat"], p["lng"]
                )
                found.append(p)
        return sorted(found, key=lambda p: (p["distance"], p["id"]))[:limit]

    def add(self, game, point):
        lat, lng = coordinates(point["lat"], point["lng"])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM points WHERE game=? AND name=? AND abs(lat-?)<0.00001 AND abs(lng-?)<0.00001",
                (game, point["name"], lat, lng),
            ).fetchone():
                raise ValueError("该点位已存在，请勿重复添加")
            ident = "local-" + uuid.uuid4().hex[:12]
            db.execute(
                "INSERT INTO points VALUES(?,?,'manual',?,?,?,?)",
                (game, ident, point["name"], point["address"], lat, lng),
            )
        return ident

    def remove(self, game, ident):
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute(
                "SELECT origin FROM points WHERE game=? AND id=?", (game, ident)
            ).fetchone()
            if not row:
                return False
            if row[0] == "manual":
                db.execute("DELETE FROM points WHERE game=? AND id=?", (game, ident))
            else:
                db.execute("INSERT OR IGNORE INTO hidden VALUES (?,?)", (game, ident))
            return True

    def cache(self, address, result=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            if result is not None:
                db.execute(
                    "INSERT OR REPLACE INTO geocache VALUES (?,?)",
                    (address, json.dumps(result)),
                )
            row = db.execute(
                "SELECT result FROM geocache WHERE address=?", (address,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def fill_coordinates(self, game, ident, address, result):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE points SET lat=?,lng=? WHERE game=? AND id=? AND address=? "
                "AND origin!=?",
                (result["lat"], result["lng"], game, ident, address, SOURCE_MANUAL),
            )

    def geocode_slot(self, day, limit=1000):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", ("geocode:" + day,)
            ).fetchone()
            used = int(row[0]) if row else 0
            if used >= limit:
                return False
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                ("geocode:" + day, str(used + 1)),
            )
            return True
