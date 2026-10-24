"""快照导入与按来源的同步状态（阶段 4/5）。

设计约束：

* 查询库与快照库独立保存；来源同步只更新对应来源，保留人工点位。
* **按来源管理状态**，不再用单一 ``captcha_required`` 开关。
  旧源（bemanicn）的禁用/阻断状态必须**保留**，只新增新源的启用状态——
  否则一旦清掉，旧的爬虫会恢复运行。
* **人工点位与删除屏蔽跨来源持久**：``manual`` 来源的行永不被来源同步覆盖；
  删除走 tombstone，月度更新不会让已删除的店「复活」。
* **坐标基准为 GCJ-02（已核验）**，因此导入时不转换；但每行仍显式记录
  ``raw_crs`` 与 ``coordinate_method``，便于将来复核。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .nearcade_snapshot import StagedSnapshot

#: 本插件认识的游戏 slug。阶段 4 要求「明确产品定义」，
#: 因此不认识的 slug 直接拒绝，而不是静默按 titleId 撞运气匹配
#: （否则 {"beatmania": (5,)} 会真的导入一批 beatmania 店铺）。
KNOWN_GAMES = frozenset({"maimai", "chuni"})

#: 每个 slug 允许的系列 ID。旧版舞萌(2)默认不纳入，改这里即可开启。
GAME_TITLE_IDS: dict[str, tuple[int, ...]] = {
    "maimai": (1,),
    "chuni": (3,),
}

#: 参与距离排序的来源白名单。
#
# 旧实现把 ``{"manual", "bemanicn-games"}`` 硬编码在 store.py 的 nearest() 里，
# 新来源入库的 4103 个点位会被静默过滤，查询只返回人工点位——
# 该缺陷在影子查询中才暴露。来源集合统一在此定义，其它模块一律引用这里。
RANKABLE_ORIGINS = frozenset({"manual", "nearcade"})

#: 需要保留本地已补坐标的来源：当上游记录坐标为空而地址未变时，
#: 不得用空值覆盖本地已解析出的坐标。
#: 旧实现把 ``'bemanicn-games'`` 写死在 SQL 里，换来源后会静默清空已补坐标。
COORDINATE_PRESERVING_ORIGINS = frozenset({"manual", "nearcade", "bemanicn-games"})

#: 已停用的历史来源。保留常量是为了让「旧数据不得混入排序」这条规则可读，
#: 同时便于迁移脚本识别需要改写的来源标记。
RETIRED_ORIGINS = frozenset({"bemanicn", "bemanicn-games", "wahlap"})

SOURCE_NEARCADE = "nearcade"
SOURCE_MANUAL = "manual"
SOURCE_BEMANICN = "bemanicn"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


DDL = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    status          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    item_count      INTEGER,
    reported_total  INTEGER,
    page_count      INTEGER,
    content_digest  TEXT,
    contract_note   TEXT,
    failure_reason  TEXT
);

CREATE TABLE IF NOT EXISTS source_state (
    source  TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (source, key)
);

-- 快照店铺：与插件查询用的 points 表分离，便于「影子查询」比对后再切换。
CREATE TABLE IF NOT EXISTS snapshot_shops (
    snapshot_id  TEXT NOT NULL,
    game         TEXT NOT NULL,
    shop_id      INTEGER NOT NULL,
    source_id    TEXT NOT NULL,
    name         TEXT NOT NULL,
    address      TEXT NOT NULL,
    lat          REAL,
    lng          REAL,
    raw_crs      TEXT NOT NULL DEFAULT 'gcj02',
    coordinate_method TEXT NOT NULL DEFAULT 'identity',
    machine_total INTEGER NOT NULL DEFAULT 0,
    is_closed    INTEGER NOT NULL DEFAULT 0,
    source_updated_at TEXT,
    fetched_at   TEXT,
    PRIMARY KEY (snapshot_id, game, shop_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_shops_lookup
    ON snapshot_shops (snapshot_id, game);

-- 删除屏蔽：按 game + source_id 持久化，跨快照与跨来源生效。
CREATE TABLE IF NOT EXISTS hidden_shops (
    game      TEXT NOT NULL,
    source_id TEXT NOT NULL,
    reason    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (game, source_id)
);
"""


class SnapshotStoreError(RuntimeError):
    """快照存储层错误。"""


@dataclass(frozen=True)
class ImportResult:
    snapshot_id: str
    source: str
    games: Mapping[str, int]
    total: int
    skipped_closed: int
    skipped_hidden: int
    without_coordinates: int
    replaced_previous: bool


class SnapshotStore:
    """独立的快照库。``path`` 可指向测试库，不影响生产 ``arcades.sqlite3``。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._connect() as db:
            db.executescript(DDL)

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        """打开一个会自动关闭的连接。

        必须显式关闭：``sqlite3.Connection.__exit__`` 只提交事务、不关闭句柄，
        句柄泄漏在 Windows 上表现为临时库无法删除（WinError 32）。
        """
        db = sqlite3.connect(self.path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return closing(db)

    # -- 来源状态（阶段 5）--------------------------------------------------
    def set_source_state(self, source: str, key: str, value: str) -> None:
        # 注意：此处用 closing() 关闭连接，而 Connection.__exit__ 的「提交事务」
        # 语义不再生效，因此所有写操作都必须显式 commit()，否则会静默回滚。
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO source_state VALUES (?,?,?)", (source, key, value)
            )
            db.commit()

    def source_state(self, source: str, key: str, default: str | None = None) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM source_state WHERE source=? AND key=?", (source, key)
            ).fetchone()
        return row["value"] if row else default

    def all_source_state(self) -> dict[str, dict[str, str]]:
        with self._connect() as db:
            rows = db.execute("SELECT source, key, value FROM source_state").fetchall()
        out: dict[str, dict[str, str]] = {}
        for row in rows:
            out.setdefault(row["source"], {})[row["key"]] = row["value"]
        return out

    def is_source_enabled(self, source: str) -> bool:
        """来源是否启用。**未配置视为未启用**，避免静默抓取。"""
        return self.source_state(source, "enabled", "false") == "true"

    def block_source(self, source: str, reason: str) -> None:
        self.set_source_state(source, "enabled", "false")
        self.set_source_state(source, "blocked_reason", reason)

    # -- 快照登记 -----------------------------------------------------------
    def register_snapshot(self, snapshot: StagedSnapshot, *, status: str = "staging") -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot.snapshot_id,
                    snapshot.source,
                    status,
                    snapshot.started_at,
                    snapshot.finished_at,
                    snapshot.item_count,
                    snapshot.reported_total,
                    snapshot.page_count,
                    snapshot.digest(),
                    json.dumps(dict(snapshot.contract_note), ensure_ascii=False),
                    None,
                ),
            )
            db.commit()

    def mark_failed(self, snapshot_id: str, reason: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE snapshots SET status='failed', failure_reason=? WHERE snapshot_id=?",
                (reason, snapshot_id),
            )
            db.commit()

    def snapshot_status(self, snapshot_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT status FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        return row["status"] if row else None

    def active_snapshot(self, source: str) -> str | None:
        return self.source_state(source, "active_snapshot")

    def snapshot_shops(
        self, snapshot_id: str, game: str | None = None
    ) -> list[dict[str, Any]]:
        """按快照（可选按游戏）读取已导入的店铺行。

        对外提供这个方法而不是让调用方去碰连接：读取快照是正常需求，
        不应该依赖私有实现。
        """
        sql = "SELECT * FROM snapshot_shops WHERE snapshot_id=?"
        params: list[Any] = [snapshot_id]
        if game is not None:
            sql += " AND game=?"
            params.append(game)
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_shops_without_coordinates(
        self, snapshot_id: str, game: str | None = None
    ) -> int:
        sql = (
            "SELECT COUNT(*) AS n FROM snapshot_shops "
            "WHERE snapshot_id=? AND (lat IS NULL OR lng IS NULL)"
        )
        params: list[Any] = [snapshot_id]
        if game is not None:
            sql += " AND game=?"
            params.append(game)
        with self._connect() as db:
            row = db.execute(sql, params).fetchone()
        return int(row["n"])

    def snapshot_count(self, snapshot_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM snapshot_shops WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        return int(row["n"])

    # -- 删除屏蔽（tombstone）------------------------------------------------
    def hide(self, game: str, source_id: str, reason: str = "") -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO hidden_shops VALUES (?,?,?,?)",
                (game, source_id, reason, _now()),
            )
            db.commit()

    def unhide(self, game: str, source_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM hidden_shops WHERE game=? AND source_id=?", (game, source_id)
            )
            db.commit()

    def hidden_ids(self, game: str) -> set[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT source_id FROM hidden_shops WHERE game=?", (game,)
            ).fetchall()
        return {row["source_id"] for row in rows}

    # -- 导入 ---------------------------------------------------------------
    def import_snapshot(
        self,
        snapshot: StagedSnapshot,
        *,
        game_title_ids: Mapping[str, Sequence[int]],
        publish: bool = False,
        include_closed: bool = False,
    ) -> ImportResult:
        """把快照导入独立表。

        ``publish=False``（默认）只登记数据，不切换活动快照——
        便于影子查询比对后再决定。
        """
        unknown = sorted(set(game_title_ids) - KNOWN_GAMES)
        if unknown:
            raise SnapshotStoreError(
                "未知游戏 slug：%s（已知：%s）"
                % (", ".join(unknown), ", ".join(sorted(KNOWN_GAMES)))
            )
        if not game_title_ids:
            raise SnapshotStoreError("game_title_ids 为空，无法导入")

        games = {name: tuple(ids) for name, ids in game_title_ids.items()}
        fetched_at = _now()

        # 先登记，状态保持 staging；任何异常都不会让它变成 ready。
        self.register_snapshot(snapshot, status="staging")

        counts: dict[str, int] = {}
        skipped_closed = 0
        skipped_hidden = 0
        without_coordinates = 0

        with self._connect() as db:
            for game, title_ids in games.items():
                wanted = set(title_ids)
                hidden = self.hidden_ids(game)
                rows = []
                for shop in snapshot.shops:
                    if not (wanted & set(shop.title_ids)):
                        continue
                    if shop.is_closed and not include_closed:
                        skipped_closed += 1
                        continue
                    if shop.source_id in hidden:
                        skipped_hidden += 1
                        continue
                    if not shop.has_coordinates:
                        without_coordinates += 1
                    rows.append(
                        (
                            snapshot.snapshot_id,
                            game,
                            shop.shop_id,
                            shop.source_id,
                            shop.name,
                            shop.address,
                            shop.lat,
                            shop.lng,
                            shop.crs,
                            "identity",
                            shop.machine_total,
                            1 if shop.is_closed else 0,
                            shop.updated_at,
                            fetched_at,
                        )
                    )
                db.executemany(
                    "INSERT OR REPLACE INTO snapshot_shops VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                counts[game] = len(rows)
            db.commit()

        # 未出现在任何游戏里说明快照与游戏目录不匹配，属契约问题。
        if not any(games.values()):
            raise SnapshotStoreError("游戏系列 ID 全为空，无法导入")

        if publish:
            self.publish(snapshot)
        return ImportResult(
            snapshot_id=snapshot.snapshot_id,
            source=snapshot.source,
            games=counts,
            total=sum(counts.values()),
            skipped_closed=skipped_closed,
            skipped_hidden=skipped_hidden,
            without_coordinates=without_coordinates,
            replaced_previous=self.active_snapshot(snapshot.source) is not None,
        )

    def publish(self, snapshot: StagedSnapshot) -> None:
        """原子切换活动快照。旧快照保留（标记 superseded），不删除数据。"""
        previous = self.active_snapshot(snapshot.source)
        with self._connect() as db:
            db.execute(
                "UPDATE snapshots SET status='ready' WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            )
            if previous and previous != snapshot.snapshot_id:
                db.execute(
                    "UPDATE snapshots SET status='superseded' WHERE snapshot_id=?",
                    (previous,),
                )
            db.commit()
        self.set_source_state(snapshot.source, "active_snapshot", snapshot.snapshot_id)
        self.set_source_state(snapshot.source, "enabled", "true")
        self.set_source_state(snapshot.source, "last_success", _now())
        self.set_source_state(snapshot.source, "status", "ready")

    # -- 查询（影子查询用）---------------------------------------------------
    def rankable_shops(
        self, game: str, snapshot_id: str | None = None, *, source: str = SOURCE_NEARCADE
    ) -> list[dict[str, Any]]:
        """返回可参与排序的点位。

        规则与 ``store.nearest()`` 保持一致：
        * 只接受白名单来源；
        * **坐标缺失的点位保留在库中但不参与排序**；
        * 已删除（tombstone）的点位不出现。
        """
        if source not in RANKABLE_ORIGINS:
            return []
        snapshot_id = snapshot_id or self.active_snapshot(source)
        if not snapshot_id:
            return []
        hidden = self.hidden_ids(game)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM snapshot_shops "
                "WHERE snapshot_id=? AND game=? AND lat IS NOT NULL AND lng IS NOT NULL",
                (snapshot_id, game),
            ).fetchall()
        return [dict(row) for row in rows if row["source_id"] not in hidden]


def import_result_summary(result: ImportResult) -> str:
    """给日志/状态命令用的一行摘要。"""
    parts = ", ".join("%s=%d" % (k, v) for k, v in sorted(result.games.items()))
    return (
        "快照 %s（%s）：%s，合计 %d；跳过闭店 %d、已屏蔽 %d；缺坐标 %d"
        % (
            result.snapshot_id,
            result.source,
            parts,
            result.total,
            result.skipped_closed,
            result.skipped_hidden,
            result.without_coordinates,
        )
    )


def sync_to_legacy_points(
    store: SnapshotStore,
    legacy_path: str | Path,
    *,
    snapshot_id: str,
    origin: str = SOURCE_NEARCADE,
    games: Iterable[str] = ("maimai", "chuni"),
) -> dict[str, int]:
    """把快照写入旧 ``points`` 表，供现有 ``store.nearest()`` 做影子查询。

    ``store`` 是**快照库**（数据与 tombstone 都在这里），
    ``legacy_path`` 才是插件的旧 ``points`` 表——两者通常不是同一个文件。

    只删除**同一来源**的行（``manual`` 与其它来源不受影响），
    并把删除屏蔽同步为旧表的 ``hidden``。
    """
    if origin not in RANKABLE_ORIGINS:
        raise SnapshotStoreError("来源 %r 不在可排序白名单内" % origin)
    written: dict[str, int] = {}
    with sqlite3.connect(str(legacy_path)) as db:
        db.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "points" not in tables:
            raise SnapshotStoreError("目标库缺少 points 表：%s" % legacy_path)
        for game in games:
            hidden = set(store.hidden_ids(game))
            # Administrator deletions live in the query database. Preserve them
            # even when the independent snapshot has no matching tombstone.
            if "hidden" in tables:
                hidden.update(row[0] for row in db.execute(
                    "SELECT id FROM hidden WHERE game=?", (game,)))
            rows = store.snapshot_shops(snapshot_id, game)
            # 写入排序表时就把被屏蔽与缺坐标的店排除掉，
            # 避免「先插入再删除」的顺序问题把屏蔽对象又带回来。
            legal = [
                row
                for row in rows
                if row["source_id"] not in hidden and row["lat"] is not None
            ]

            db.execute("DELETE FROM points WHERE game=? AND origin=?", (game, origin))
            db.executemany(
                "INSERT OR REPLACE INTO points (game,id,origin,name,address,lat,lng) "
                "VALUES (?,?,?,?,?,?,?)",
                [
                    (game, row["source_id"], origin, row["name"], row["address"],
                     row["lat"], row["lng"])
                    for row in legal
                ],
            )

            if "hidden" in tables:
                # Local administrator deletions must survive source refreshes.
                db.executemany(
                    "INSERT OR IGNORE INTO hidden VALUES (?,?)",
                    [(game, sid) for sid in sorted(hidden)],
                )
            db.commit()
            written[game] = len(legal)
    return written


def bootstrap_source_states(store: SnapshotStore) -> None:
    """按阶段 5 建立初始来源状态。

    **关键：不清除旧源的禁用/阻断状态。** 只补写缺失项，
    已存在的键一律不覆盖，避免把人工设置的状态冲掉。
    """
    defaults = [
        (SOURCE_BEMANICN, "enabled", "false"),
        (SOURCE_BEMANICN, "blocked_reason", "captcha"),
        (SOURCE_NEARCADE, "status", "idle"),
    ]
    for source, key, value in defaults:
        if store.source_state(source, key) is None:
            store.set_source_state(source, key, value)
