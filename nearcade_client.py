"""nearcade 公开接口只读客户端与响应规范化。

本模块只做两件事：按契约抓取 ``GET /api/shops`` 分页，以及把返回的店铺记录
规范化成插件内部使用的结构。它不写入任何数据库，也不做坐标转换。

坐标基准：当前按 GCJ-02 使用上游坐标，与 QQ 位置卡片统一；不做二次转换。
若上游改变坐标契约，需要重新核对。输出只表示直线距离，不表示导航路线。

请求约束：

* 单并发，请求间隔至少 ``min_interval`` 秒；
* ``429`` 尊重 ``Retry-After``，普通网络失败有限退避；
* ``401``/``403``/验证码页面立即暂停，不绕过；
* 限制最大页数、最大响应体，避免无限循环。

坐标语义：``location.coordinates`` 是 GeoJSON 的 ``[经度, 纬度]``。解析后统一存为
独立的 ``lng``/``lat`` 字段，避免轴顺序被隐式误解。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

DEFAULT_BASE_URL = "https://nearca.de"

#: `/api/shops` 的每页上限。实测 `limit=200` 返回 400，`limit=100` 正常。
#: 注意 `/api/discover` 的上限是 150，与这里不同，不要混用。
MAX_PAGE_SIZE = 100

#: 单个响应体的上限，防止在异常情况下把内存吃满。
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

#: 当前已知的游戏系列 ID -> 中文名。运行时以 ``fetch_game_titles()`` 的结果为准，
#: 这里只是离线兜底与可读性参考，不作为永久假设。
KNOWN_GAME_TITLES = {1: "舞萌DX", 2: "maimai", 3: "中二节奏"}

#: 舞萌 / 中二 的目标系列 ID。旧版 maimai(2) 是否纳入是产品决策，默认不纳入。
GAME_TITLE_IDS = {"maimai": (1,), "chuni": (3,)}


class NearcadeError(RuntimeError):
    """nearcade 访问失败的基类。"""


class UpstreamBlockedError(NearcadeError):
    """上游要求验证、拒绝访问或返回了非 JSON 内容；调用方应暂停并保留旧快照。"""


class ResponseShapeError(NearcadeError):
    """响应结构不符合契约。"""


@dataclass(frozen=True)
class ShopPoint:
    """一条规范化后的店铺记录。"""

    source_id: str            # 形如 nearcade-10672，不与旧 bcn-* 命名空间冲突
    shop_id: int
    name: str
    address: str
    lat: float | None
    lng: float | None
    title_ids: tuple[int, ...]  # 该店拥有的游戏系列（已去重、已剔除 0）
    machine_total: int          # 机台数量合计
    is_closed: bool
    updated_at: str | None
    crs: str = "gcj02"          # 已核验：nearcade 存储坐标即 GCJ-02，无需转换
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_coordinates(self) -> bool:
        return self.lat is not None and self.lng is not None


def _clean_text(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    text = str(value)
    # 去掉控制字符，避免把换行/制表符带进 QQ 消息
    text = "".join(" " if ch < " " or ch == "\x7f" else ch for ch in text)
    return " ".join(text.split())[:limit]


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / Inf
        return None
    return number


def _iter_date(value: Any) -> str | None:
    """接受 ISO 字符串与 Mongo 扩展 JSON 的 {"$date": ...} 两种形状。"""
    if isinstance(value, str):
        return value or None
    if isinstance(value, Mapping):
        inner = value.get("$date")
        if isinstance(inner, str):
            return inner or None
    return None


def parse_address(address: Any) -> str:
    """把地址规范成一行可读文本。

    ``general`` 与 ``detailed`` 在不同来源下可能重复、也可能一方为空，
    因此按段拼接后去重相邻重复段。
    """
    if not isinstance(address, Mapping):
        return ""
    parts: list[str] = []
    general = address.get("general")
    if isinstance(general, Sequence) and not isinstance(general, (str, bytes)):
        for item in general:
            text = _clean_text(item, 80)
            if text and (not parts or parts[-1] != text):
                parts.append(text)
    detailed = _clean_text(address.get("detailed"), 200)
    if detailed and (not parts or parts[-1] != detailed):
        parts.append(detailed)
    return _clean_text(" ".join(parts), 300)


def parse_region_ids(address: Any) -> tuple[str, ...]:
    """地区层级 ID。

    线上返回 ``[{"id": "CN-510117", "name": ...}]``，种子文件返回 ``["CN-510117"]``。
    两种形状都必须接受。
    """
    if not isinstance(address, Mapping):
        return ()
    region = address.get("region")
    if not isinstance(region, Sequence) or isinstance(region, (str, bytes)):
        return ()
    ids: list[str] = []
    for node in region:
        if isinstance(node, Mapping):
            candidate = node.get("id")
        else:
            candidate = node
        if isinstance(candidate, str) and candidate:
            ids.append(candidate)
    return tuple(ids)


def parse_coordinates(location: Any) -> tuple[float | None, float | None]:
    """从 GeoJSON Point 解析 ``(lat, lng)``。

    输入顺序是 ``[经度, 纬度]``；越界或形状不合法一律返回 ``(None, None)``，
    由调用方按「坐标缺失」处理，绝不猜测轴顺序。
    """
    if not isinstance(location, Mapping):
        return None, None
    coords = location.get("coordinates")
    if not isinstance(coords, Sequence) or isinstance(coords, (str, bytes)):
        return None, None
    if len(coords) != 2:
        return None, None
    lng = _as_float(coords[0])
    lat = _as_float(coords[1])
    if lat is None or lng is None:
        return None, None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return None, None
    return lat, lng


def _machine_total(games: Any) -> int:
    if not isinstance(games, Sequence) or isinstance(games, (str, bytes)):
        return 0
    total = 0
    for game in games:
        if not isinstance(game, Mapping):
            continue
        quantity = game.get("quantity")
        if isinstance(quantity, bool):
            continue
        if isinstance(quantity, int):
            total += max(0, quantity)
        elif isinstance(quantity, float) and quantity == quantity:
            total += max(0, int(quantity))
        elif isinstance(quantity, str):
            try:
                total += max(0, int(quantity.strip()))
            except ValueError:
                continue
    return total


def parse_title_ids(games: Any) -> tuple[int, ...]:
    """该店拥有的游戏系列 ID（升序去重）。

    ``titleId = 0`` 是脏数据（不在线上游戏目录中），直接剔除；
    非整数或负数同样剔除。同一系列的多条机台记录不会重复计入。
    """
    if not isinstance(games, Sequence) or isinstance(games, (str, bytes)):
        return ()
    found: set[int] = set()
    for game in games:
        if not isinstance(game, Mapping):
            continue
        raw = game.get("titleId")
        if isinstance(raw, bool) or not isinstance(raw, int):
            continue
        if raw > 0:
            found.add(raw)
    return tuple(sorted(found))


def normalize_shop(record: Mapping[str, Any]) -> ShopPoint:
    """把一条线上店铺记录规范化为 :class:`ShopPoint`。

    坐标按原值使用：nearcade 存储坐标即 GCJ-02（见模块文档），
    与现有插件的 QQ/高德查询层同基准，**不需要也不允许再做转换**。
    """
    if not isinstance(record, Mapping):
        raise ResponseShapeError("店铺记录不是对象")
    shop_id = record.get("id")
    if isinstance(shop_id, bool) or not isinstance(shop_id, int):
        raise ResponseShapeError("店铺记录缺少整数 id")
    lat, lng = parse_coordinates(record.get("location"))
    games = record.get("games")
    return ShopPoint(
        source_id="nearcade-%d" % shop_id,
        shop_id=shop_id,
        name=_clean_text(record.get("name"), 120),
        address=parse_address(record.get("address")),
        lat=lat,
        lng=lng,
        title_ids=parse_title_ids(games),
        machine_total=_machine_total(games),
        is_closed=bool(record.get("isClosed")),
        updated_at=_iter_date(record.get("updatedAt")),
        crs="gcj02",
        raw=record,
    )


@dataclass
class ShopPage:
    """一页店铺列表及分页状态。"""

    shops: list[ShopPoint]
    total_count: int
    current_page: int
    has_next_page: bool
    has_prev_page: bool


def parse_shop_page(payload: Any, expect_page: int | None = None) -> ShopPage:
    """解析 ``/api/shops`` 的响应体。

    契约要求 ``shops``/``totalCount``/``currentPage``/``hasNextPage`` 存在；
    缺失即视为结构错误而不是静默当成空页，避免把坏响应写成「全国 0 家店」。
    """
    if not isinstance(payload, Mapping):
        raise ResponseShapeError("响应不是 JSON 对象")
    required = ("shops", "totalCount", "currentPage", "hasNextPage", "hasPrevPage")
    missing = [key for key in required if key not in payload]
    if missing:
        raise ResponseShapeError("响应缺少字段：%s" % ", ".join(missing))
    raw_shops = payload["shops"]
    if not isinstance(raw_shops, Sequence) or isinstance(raw_shops, (str, bytes)):
        raise ResponseShapeError("shops 字段不是数组")
    shops = [normalize_shop(item) for item in raw_shops]

    current = payload["currentPage"]
    if isinstance(current, bool) or not isinstance(current, int):
        raise ResponseShapeError("currentPage 不是整数")
    if expect_page is not None and current != expect_page:
        raise ResponseShapeError(
            "分页错位：请求第 %s 页，返回第 %s 页" % (expect_page, current)
        )
    total = payload["totalCount"]
    if isinstance(total, bool) or not isinstance(total, int):
        raise ResponseShapeError("totalCount 不是整数")
    return ShopPage(
        shops=shops,
        total_count=total,
        current_page=current,
        has_next_page=bool(payload["hasNextPage"]),
        has_prev_page=bool(payload["hasPrevPage"]),
    )


def decode_body(raw: bytes) -> Any:
    """把响应体解码为 JSON，并对验证页/HTML 给出可识别的错误。"""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise NearcadeError("响应体超过 %d 字节上限" % MAX_RESPONSE_BYTES)
    text = raw.decode("utf-8", errors="replace").lstrip()
    lowered = text[:400].lower()
    if lowered.startswith("<!doctype") or lowered.startswith("<html"):
        raise UpstreamBlockedError("上游返回 HTML 而非 JSON，可能要求人机验证，已暂停")
    for marker in ("just a moment", "cf-chl", "captcha", "challenge-platform"):
        if marker in lowered:
            raise UpstreamBlockedError("上游返回验证挑战页面，已暂停")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ResponseShapeError("响应不是合法 JSON：%s" % exc) from exc


class NearcadeClient:
    """只读分页客户端。

    ``fetcher`` 允许注入，便于离线测试与固定 fixture 回放：
    签名 ``fetcher(url: str, headers: dict) -> bytes``。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        page_size: int = MAX_PAGE_SIZE,
        min_interval: float = 1.5,
        max_pages: int = 200,
        timeout: float = 45.0,
        accept_language: str = "zh-CN,zh;q=0.9",
        fetcher: Callable[[str, Mapping[str, str]], bytes] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 4,
        backoff_base: float = 1.5,
        backoff_cap: float = 30.0,
    ) -> None:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError("page_size 必须在 1..%d 之间" % MAX_PAGE_SIZE)
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.min_interval = max(0.0, min_interval)
        self.max_pages = max_pages
        self.timeout = timeout
        self.accept_language = accept_language
        self._fetcher = fetcher or self._http_fetch
        self._sleep = sleep
        self._last_request_at = 0.0
        self.max_retries = max(0, max_retries)
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap

    # -- 传输层 ---------------------------------------------------------------
    def _http_fetch(self, url: str, headers: Mapping[str, str]) -> bytes:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise UpstreamBlockedError("上游返回 %s，已暂停：%s" % (exc.code, url)) from exc
            if exc.code == 429:
                # 尊重 Retry-After：等待指定秒数后交由上层有限重试，而不是立刻再打。
                retry_after = self._parse_retry_after(exc.headers.get("Retry-After"))
                if retry_after is not None:
                    self._sleep(min(retry_after, self.backoff_cap))
                raise NearcadeError("上游限流 429：%s" % url) from exc
            if 500 <= exc.code < 600:
                raise NearcadeError("上游 %s：%s" % (exc.code, url)) from exc
            raise NearcadeError("上游返回 %s：%s" % (exc.code, url)) from exc
        except urllib.error.URLError as exc:
            raise NearcadeError("网络失败：%s（%s）" % (exc.reason, url)) from exc
        except (TimeoutError, OSError) as exc:
            # 读超时/连接被重置不会包成 URLError，必须单独接住，
            # 否则一次瞬时抖动就会让整批快照失败。
            raise NearcadeError("网络超时或中断：%s（%s）" % (exc, url)) from exc

    @staticmethod
    def _parse_retry_after(value: Any) -> float | None:
        """解析 Retry-After（秒数或 HTTP 日期）；无法解析返回 None。"""
        if not value:
            return None
        text = str(value).strip()
        try:
            return max(0.0, float(text))
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(text)
            if when is None:
                return None
            import datetime as _dt

            now = _dt.datetime.now(when.tzinfo) if when.tzinfo else _dt.datetime.now()
            return max(0.0, (when - now).total_seconds())
        except (TypeError, ValueError):
            return None

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.min_interval:
            self._sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Language": self.accept_language,
            "User-Agent": "AstrBot-arcades/0.6.3 (+read-only snapshot)",
        }

    def _fetch_with_retry(self, url: str) -> bytes:
        """带有限退避的取回。

        只对**瞬时**失败重试（网络中断、读超时、5xx）。
        ``UpstreamBlockedError``（401/403/验证页）**立即抛出、绝不重试**——
        被拒绝后继续重试等于继续敲门。429 由 ``_http_fetch`` 先等待
        ``Retry-After``，再在此处做有限次退避。
        """
        attempt = 0
        while True:
            try:
                return self._fetcher(url, self._headers())
            except UpstreamBlockedError:
                raise
            except NearcadeError:
                if attempt >= self.max_retries:
                    raise
                delay = min(self.backoff_base * (2 ** attempt), self.backoff_cap)
                self._sleep(delay)
                attempt += 1

    # -- 公共接口 -------------------------------------------------------------
    def fetch_page(self, page: int = 1, *, sort: str = "id_asc") -> ShopPage:
        url = "%s/api/shops?page=%d&limit=%d&sort=%s&includeTimeInfo=false" % (
            self.base_url, page, self.page_size, sort,
        )
        self._throttle()
        payload = decode_body(self._fetch_with_retry(url))
        return parse_shop_page(payload, expect_page=page)

    def iter_shops(self, *, sort: str = "id_asc") -> Iterator[ShopPoint]:
        """按页产出店铺，直到 ``hasNextPage`` 收敛或达到 ``max_pages``。"""
        seen_ids: set[int] = set()
        first_total: int | None = None
        page = 1
        while page <= self.max_pages:
            current = self.fetch_page(page, sort=sort)
            if first_total is None:
                first_total = current.total_count
            elif current.total_count != first_total:
                raise ResponseShapeError(
                    "总数在分页过程中变化（第 %d 页：%s，首屏：%s），"
                    "本次快照不完整，已放弃" % (page, current.total_count, first_total)
                )
            if not current.shops and current.has_next_page:
                raise ResponseShapeError("第 %d 页为空但仍声明有下一页" % page)
            for shop in current.shops:
                if shop.shop_id in seen_ids:
                    raise ResponseShapeError("第 %d 页出现重复店铺 id=%d" % (page, shop.shop_id))
                seen_ids.add(shop.shop_id)
                yield shop
            if not current.has_next_page:
                return
            page += 1
        raise ResponseShapeError("超过 max_pages=%d 仍未收敛" % self.max_pages)

    def fetch_game_titles(self) -> dict[int, str]:
        """游戏目录：``titleId -> 名称``。"""
        url = "%s/api/game-titles" % self.base_url
        self._throttle()
        payload = decode_body(self._fetch_with_retry(url))
        if not isinstance(payload, Mapping) or not isinstance(payload.get("titles"), Sequence):
            raise ResponseShapeError("game-titles 响应结构不符")
        titles: dict[int, str] = {}
        for item in payload["titles"]:
            if isinstance(item, Mapping) and isinstance(item.get("id"), int):
                titles[int(item["id"])] = str(item.get("name") or "")
        return titles


def shops_for_game(shops: Iterable[ShopPoint], game: str) -> list[ShopPoint]:
    """按游戏 slug 过滤店铺，同一店铺只出现一次。

    ``game`` 取 ``"maimai"`` / ``"chuni"``；未知 slug 抛出 ``ValueError``。
    """
    try:
        wanted = GAME_TITLE_IDS[game]
    except KeyError as exc:
        raise ValueError("未知游戏：%s" % game) from exc
    return [shop for shop in shops if any(t in wanted for t in shop.title_ids)]
