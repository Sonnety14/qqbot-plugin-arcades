"""nearcade 快照暂存与完整性校验。

职责：把一个来源的抓取结果先写入**暂存区**，全部校验通过后才允许切换为
**活动快照**；任何失败都保留上一份成功快照，绝不发布半批数据。

本模块不联网（分页抓取由 ``nearcade_client`` 负责），也不写生产库：
``stage()`` 产出一个自包含的 :class:`StagedSnapshot`，由调用方决定是否发布。

校验项（对应交接文档阶段 3）：

* 页码连续、无重复 ID、每页顺序正确、``hasNextPage`` 正确收束；
* 唯一条目数与上游报告的 ``totalCount`` 一致；
* 总数在分页过程中变化 -> 判定为「待核对」，不发布；
* 坐标顺序/范围、游戏数组、闭店状态通过校验；
* 数据量大幅减少（相对上一份活动快照）-> 待核对，不删空旧库。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .nearcade_client import (
    NearcadeClient,
    NearcadeError,
    ShopPoint,
    ShopPage,
)

#: 相对上一份活动快照，条目数跌破此比例即判定为「待核对」。
#: 上游确实可能撤店，但一次掉这么多更可能是抓到半批或接口异常。
MIN_RETAINED_RATIO = 0.80

#: 单次快照允许的最大条目数，防止异常响应把库写爆。
MAX_ITEMS = 20000

SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class SnapshotValidationError(NearcadeError):
    """快照未通过完整性校验；调用方不得发布该快照。"""


class _Accumulator:
    """跨页累加器：把两个入口（联网 / 离线）的校验逻辑收敛到同一处。

    这样任何一条校验规则都不会只在其中一个入口生效——
    早期版本正是因为在两处各写了一遍，导致「空页却声称有下一页」只在联网路径被检查。
    """

    def __init__(self) -> None:
        self.shops: list[ShopPoint] = []
        self._seen: set[int] = set()
        self.reported_total: int | None = None

    def add_page(self, page: ShopPage, page_no: int) -> None:
        if page.current_page != page_no:
            raise SnapshotValidationError(
                "分页错位：请求第 %d 页，返回第 %d 页" % (page_no, page.current_page)
            )

        if self.reported_total is None:
            self.reported_total = page.total_count
        elif page.total_count != self.reported_total:
            raise SnapshotValidationError(
                "总数在分页过程中变化（第 %d 页 %d，首屏 %d）：判定为待核对，不发布"
                % (page_no, page.total_count, self.reported_total)
            )

        if not page.shops and page.has_next_page:
            raise SnapshotValidationError("第 %d 页为空但仍声明有下一页" % page_no)

        for shop in page.shops:
            if shop.shop_id in self._seen:
                raise SnapshotValidationError(
                    "重复店铺 id=%d（第 %d 页）" % (shop.shop_id, page_no)
                )
            self._seen.add(shop.shop_id)
            validate_shop(shop)
            self.shops.append(shop)

        if len(self.shops) > MAX_ITEMS:
            raise SnapshotValidationError("条目数超过上限 %d" % MAX_ITEMS)

    def finish(self, *, page_count: int, previous_count: int | None) -> int:
        """收尾校验，返回报告的 totalCount。"""
        if self.reported_total is None:
            raise SnapshotValidationError("未取到任何分页")
        if not self.shops:
            raise SnapshotValidationError(
                "快照为空（上游报告 %d 条）：判定为待核对，不删除旧库" % self.reported_total
            )
        if len(self.shops) != self.reported_total:
            raise SnapshotValidationError(
                "唯一条目数 %d 与上游报告总数 %d 不一致" % (len(self.shops), self.reported_total)
            )
        if previous_count:
            retained = len(self.shops) / float(previous_count)
            if retained < MIN_RETAINED_RATIO:
                raise SnapshotValidationError(
                    "条目数相对上一份快照大幅减少（%d -> %d，保留 %.0f%%）："
                    "判定为待核对，不删除旧库"
                    % (previous_count, len(self.shops), retained * 100)
                )
        return self.reported_total


@dataclass
class StagedSnapshot:
    """一份通过校验、但尚未发布的快照。"""

    snapshot_id: str
    source: str
    shops: list[ShopPoint]
    reported_total: int
    page_count: int
    started_at: str
    finished_at: str
    contract_note: Mapping[str, Any] = field(default_factory=dict)

    @property
    def item_count(self) -> int:
        return len(self.shops)

    def digest(self) -> str:
        """内容摘要，用于「同一快照重复导入结果一致」的幂等性检查。"""
        payload = [
            [shop.shop_id, shop.name, shop.address, shop.lat, shop.lng,
             list(shop.title_ids), shop.machine_total, shop.is_closed]
            for shop in sorted(self.shops, key=lambda s: s.shop_id)
        ]
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def for_game(self, title_ids: Iterable[int]) -> list[ShopPoint]:
        wanted = set(title_ids)
        return [shop for shop in self.shops if wanted & set(shop.title_ids)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def make_snapshot_id(source: str, when: datetime | None = None) -> str:
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return "%s-%s" % (source, stamp)


def validate_shop(shop: ShopPoint) -> None:
    """单条记录的字段校验；不通过即视为快照不可信。"""
    if not SNAPSHOT_ID_RE.match(shop.source_id):
        raise SnapshotValidationError("来源 ID 形状异常：%r" % shop.source_id)
    if not shop.name:
        raise SnapshotValidationError("店铺 id=%d 名称为空" % shop.shop_id)
    if shop.lat is None or shop.lng is None:
        # 坐标缺失是允许的（保留点位但不参与排序）；
        # 但「一个有一个没有」属于解析异常，不能放过。
        if shop.lat is None and shop.lng is None:
            return
        raise SnapshotValidationError("店铺 id=%d 坐标半缺失" % shop.shop_id)
    if not (-90.0 <= shop.lat <= 90.0 and -180.0 <= shop.lng <= 180.0):
        raise SnapshotValidationError("店铺 id=%d 坐标越界" % shop.shop_id)
    if shop.machine_total < 0:
        raise SnapshotValidationError("店铺 id=%d 机台数为负" % shop.shop_id)


def stage(
    client: NearcadeClient,
    *,
    source: str = "nearcade",
    previous_count: int | None = None,
    snapshot_id: str | None = None,
    sort: str = "id_asc",
) -> StagedSnapshot:
    """抓取全部分页并校验，返回一份待发布快照。

    任何校验失败都抛 :class:`SnapshotValidationError`，调用方应保留旧快照。
    """
    started = _now()
    snapshot_id = snapshot_id or make_snapshot_id(source)
    acc = _Accumulator()
    page_count = 0
    page_no = 1

    while True:
        page: ShopPage = client.fetch_page(page_no, sort=sort)
        page_count += 1
        acc.add_page(page, page_no)
        if not page.has_next_page:
            break
        page_no += 1

    acc.finish(page_count=page_count, previous_count=previous_count)

    return StagedSnapshot(
        snapshot_id=snapshot_id,
        source=source,
        shops=acc.shops,
        reported_total=acc.reported_total or 0,
        page_count=page_count,
        started_at=started,
        finished_at=_now(),
        contract_note={
            "sort": sort,
            "page_size": client.page_size,
            "base_url": client.base_url,
            "source": source,
        },
    )


def stage_from_pages(
    pages: Sequence[Any],
    *,
    source: str = "nearcade",
    snapshot_id: str = "test-snapshot",
    previous_count: int | None = None,
) -> StagedSnapshot:
    """离线/测试入口：直接喂入分页响应体，不联网。

    与 :func:`stage` 共用同一套校验规则。
    """
    from .nearcade_client import parse_shop_page

    started = _now()
    acc = _Accumulator()
    page_count = 0

    for index, payload in enumerate(pages, start=1):
        page_count += 1
        acc.add_page(parse_shop_page(payload, expect_page=index), index)

    acc.finish(page_count=page_count, previous_count=previous_count)

    return StagedSnapshot(
        snapshot_id=snapshot_id,
        source=source,
        shops=acc.shops,
        reported_total=acc.reported_total or 0,
        page_count=page_count,
        started_at=started,
        finished_at=_now(),
        contract_note={"offline": True, "source": source},
    )
