"""nearcade 快照同步：把新流水线接到插件的原有存储与调度上。

一条同步的完整顺序（任一步失败都保留旧数据）：

1. 读取按来源的状态；来源未启用或已阻断 -> 直接返回，不做任何请求；
2. 分页抓取 + 完整性校验（``nearcade_snapshot.stage``）；
3. 导入独立快照库并登记为 staging；
4. 校验通过才 ``publish`` 切换活动快照；
5. 同步写入旧 ``points`` 表，供现有 ``store.nearest()`` 排序。

与旧 ``sources.monthly_sync`` 的区别：

* 不再有登录、Cookie、人机验证绕行——接口是公开只读的；
* 不再需要高德补坐标——上游记录自带坐标；
* 状态按来源隔离，**不会去动 ``captcha_required``**，
  因此旧爬虫不会因为新源上线而复活。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .snapshot_store import (
    GAME_TITLE_IDS,
    SOURCE_NEARCADE,
    SnapshotStore,
    bootstrap_source_states,
    import_result_summary,
    sync_to_legacy_points,
)
from .nearcade_client import NearcadeClient, NearcadeError
from .nearcade_snapshot import (
    SnapshotValidationError,
    make_snapshot_id,
    stage,
)

LOGGER = logging.getLogger(__name__)

#: 单个游戏在旧表里视为「最近未同步」的阈值。超过则视为过期，需要重跑。
DEFAULT_MIN_INTERVAL_DAYS = 25


@dataclass
class SyncOutcome:
    """一次同步的结果摘要，供状态命令与日志使用。"""

    status: str          # ok / skipped / blocked / failed
    reason: str = ""
    snapshot_id: str = ""
    total: int = 0
    games: Mapping[str, int] | None = None
    page_count: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def describe(self) -> str:
        if self.status == "ok":
            games = ", ".join(
                "%s=%d" % (k, v) for k, v in sorted((self.games or {}).items())
            )
            return "同步完成：快照 %s，%s，合计 %d（%d 页）" % (
                self.snapshot_id, games, self.total, self.page_count,
            )
        return "同步未完成（%s）：%s" % (self.status, self.reason)


def ensure_source_ready(snapshot_store: SnapshotStore) -> None:
    """初始化来源状态。

    只补写缺失键，**绝不覆盖已有值**——尤其是旧源的启用/阻断状态，
    否则旧爬虫会随着新源上线一起复活。
    """
    bootstrap_source_states(snapshot_store)


def sync_nearcade(
    store: Any,
    snapshot_store: SnapshotStore,
    *,
    client: NearcadeClient | None = None,
    force: bool = False,
    stamp: str | None = None,
) -> SyncOutcome:
    """执行一次 nearcade 同步。``store`` 为插件的旧 ``Store`` 实例。

    ``force=False`` 时，若来源未启用则直接跳过（默认未启用，
    避免部署即抓取）。
    """
    ensure_source_ready(snapshot_store)

    # 阻断检查必须排在启用检查之前：block_source 会把 enabled 置 false，
    # 若先判 enabled 就只会得到「未启用」，掩盖真正的阻断原因。
    blocked = snapshot_store.source_state(SOURCE_NEARCADE, "blocked_reason")
    if blocked and not force:
        return SyncOutcome(status="blocked", reason="来源已被阻断：%s" % blocked)

    if not force and not snapshot_store.is_source_enabled(SOURCE_NEARCADE):
        return SyncOutcome(
            status="skipped",
            reason="来源未启用（source:nearcade:enabled）",
        )

    client = client or NearcadeClient()
    snapshot_id = make_snapshot_id(SOURCE_NEARCADE)

    # 与上一份活动快照比较条目数，骤降则判定为待核对。
    previous_count = None
    active = snapshot_store.active_snapshot(SOURCE_NEARCADE)
    if active:
        previous_count = snapshot_store.snapshot_count(active) or None

    try:
        snapshot = stage(
            client, source=SOURCE_NEARCADE, snapshot_id=snapshot_id,
            previous_count=previous_count,
        )
    except SnapshotValidationError as exc:
        snapshot_store.set_source_state(SOURCE_NEARCADE, "status", "failed")
        snapshot_store.set_source_state(SOURCE_NEARCADE, "last_error", str(exc))
        LOGGER.warning("nearcade 快照未通过校验，保留旧数据：%s", exc)
        return SyncOutcome(status="failed", reason=str(exc))
    except NearcadeError as exc:
        snapshot_store.set_source_state(SOURCE_NEARCADE, "status", "failed")
        snapshot_store.set_source_state(SOURCE_NEARCADE, "last_error", str(exc))
        LOGGER.warning("nearcade 抓取失败，保留旧数据：%s", exc)
        return SyncOutcome(status="failed", reason=str(exc))

    result = snapshot_store.import_snapshot(
        snapshot, game_title_ids=GAME_TITLE_IDS, publish=True
    )
    # 注意：tombstone 与快照数据都在 snapshot_store 里，
    # 写入目标才是插件的旧 points 表（store.path）。两者不是同一个对象。
    written = sync_to_legacy_points(
        snapshot_store,
        store.path,
        snapshot_id=snapshot.snapshot_id,
        origin=SOURCE_NEARCADE,
    )

    snapshot_store.set_source_state(SOURCE_NEARCADE, "status", "ready")
    snapshot_store.set_source_state(SOURCE_NEARCADE, "last_error", "")
    store.meta("updated:maimai", stamp or snapshot.finished_at)
    store.meta("updated:chuni", stamp or snapshot.finished_at)
    store.meta("nearcade:snapshot", snapshot.snapshot_id)
    store.meta("nearcade:items", str(snapshot.item_count))
    store.meta("nearcade:digest", snapshot.digest())

    LOGGER.info("nearcade 同步完成：%s", import_result_summary(result))
    return SyncOutcome(
        status="ok",
        snapshot_id=snapshot.snapshot_id,
        total=snapshot.item_count,
        games=dict(written),
        page_count=snapshot.page_count,
    )


def sync_is_due(store: Any, *, days: int = DEFAULT_MIN_INTERVAL_DAYS) -> bool:
    """判断是否到了该同步的时候（用于每日调度，避免重复抓取）。

    依据 ``updated:<game>`` 与 ``attempt_day_v2``：同一自然日只尝试一次，
    且距上次成功同步不足 ``days`` 天时不重跑。
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if store.meta("attempt_day_nearcade") == today:
        return False
    for game in ("maimai", "chuni"):
        stamp = store.meta("updated:" + game)
        if not stamp:
            return True
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if (now - when).days >= days:
            return True
    return False


def note_attempt(store: Any) -> None:
    """记录本自然日已尝试过同步。"""
    from datetime import datetime, timezone

    store.meta("attempt_day_nearcade", datetime.now(timezone.utc).strftime("%Y-%m-%d"))


def source_status_lines(store: Any, snapshot_store: SnapshotStore) -> Sequence[str]:
    """给「音游地图状态」命令用的来源状态行。"""
    lines = []
    state = snapshot_store.all_source_state()
    for source in sorted(state):
        entry = state[source]
        enabled = entry.get("enabled", "false")
        bits = ["enabled=%s" % enabled]
        for key in ("status", "blocked_reason", "last_success", "last_error",
                    "active_snapshot"):
            if entry.get(key):
                bits.append("%s=%s" % (key, entry[key]))
        lines.append("来源 %s：%s" % (source, "，".join(bits)))
    digest = store.meta("nearcade:digest")
    if digest:
        lines.append(
            "nearcade 快照 %s：%s 条，摘要 %s"
            % (store.meta("nearcade:snapshot"), store.meta("nearcade:items"), digest[:16])
        )
    return lines
