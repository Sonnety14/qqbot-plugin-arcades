"""Chat-scoped nearby-arcade lookup and private administrator editing."""

import asyncio
import contextlib
import time
import threading
from datetime import datetime, timedelta, timezone

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .locations import from_components, parse_text
from .nearcade_sync import (
    ensure_source_ready,
    note_attempt,
    source_status_lines,
    sync_is_due,
    sync_nearcade,
)
from .snapshot_store import RANKABLE_ORIGINS, SOURCE_MANUAL, SOURCE_NEARCADE, SnapshotStore
from .sources import Client, monthly_sync
from .store import Store

CHINA = timezone(timedelta(hours=8))
GAMES = {"舞萌": "maimai", "中二": "chuni"}

#: 首次交互时展示的数据来源与免责说明。
#: 条目由玩家自行编辑整理，无法逐条及时验证，因此需在提示中如实说明，
#: 并提醒出行前自行核实店铺现况。
DATA_NOTICE = """该插件数据来源 nearcade 全国音游地图。
条目由玩家自行编辑整理，我们并无法及时对所有的内容进行验证，敬请谅解。
※ 如有您想要补充的信息，请联系开发者。
※ 请您务必在出行前多方面了解店铺现况，避免无用出行，祝您游玩愉快！
——————————————————————
"""

#: 回复里展示的来源名称。新增来源时在此登记，避免出现「旧来源（停用）」这类兜底文案。
SOURCE_LABELS = {
    SOURCE_MANUAL: "管理员新增",
    SOURCE_NEARCADE: "nearcade 全国音游地图",
}


def origin_label(origin):
    return SOURCE_LABELS.get(origin, "旧来源（停用）")


@register("astrbot_plugin_arcades", "Sonnety14", "Nearby arcade locations", "0.6.3")
class Arcades(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        root = StarTools.get_data_dir("astrbot_plugin_arcades")
        self.store = Store(root / "arcades.sqlite3")
        self.snapshot_store = SnapshotStore(root / "nearcade.sqlite3")
        self.secrets_file = root / "credentials.json"
        self.sessions = {}
        self.task = None
        self.busy = set()
        self.stop = threading.Event()
        ensure_source_ready(self.snapshot_store)

    async def initialize(self):
        self.task = asyncio.create_task(self.scheduler())

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1500)
    async def handle(self, event: AstrMessageEvent):
        platform, user = event.get_platform_id(), str(event.get_sender_id())
        configured_platform = str(self.config.get("platform_id", "")).strip()
        if not configured_platform or platform != configured_platform or user == str(
            event.get_self_id()
        ):
            return
        text = event.message_str.strip().removeprefix('/').strip()
        commands = {a + g: (a, g) for a in ("查", "增加", "删除") for g in GAMES}
        if not event.is_private_chat():
            if (text in commands and commands[text][0] != "查") or text in ("恢复音游同步", "音游地图状态", "确认添加") or text.startswith("确认删除 "):
                event.stop_event()
                await event.send(event.plain_result("管理操作请在私聊中使用"))
                return
        scope = 'private' if event.is_private_chat() else 'group:' + str(event.get_group_id())
        key = (platform, scope, user)
        now = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
        if text in commands:
            event.stop_event()
            action, game = commands[text]
            if action != "查" and not event.is_admin():
                await event.send(
                    event.plain_result(
                        "请联系管理员" + ("添加" if action == "增加" else "删除")
                    )
                )
                return
            if key not in self.sessions and len(self.sessions) >= 2000:
                await event.send(event.plain_result("当前请求较多，请稍后再试"))
                return
            self.sessions[key] = dict(
                action=action, game=GAMES[game], label=game, expires=now + 180
            )
            prompt = DATA_NOTICE
            prompt += "发送位置信息" if action == "查" else "发送位置信息或文字信息"
            prompt += "\n请在 3 分钟内在当前聊天发送 QQ“位置”卡片；发送“取消音游操作”退出"
            if action == "增加":
                prompt += "\n文字格式：店名 | 完整地址 | 纬度,经度\n也可省略坐标，通过高德解析后确认添加"
            elif action == "删除":
                prompt += "\n文字可填点位编号或店名；会先列出候选，再确认删除"
            await event.send(event.plain_result(prompt))
            return
        if text == "取消音游操作":
            event.stop_event()
            self.sessions.pop(key, None)
            await event.send(event.plain_result("已取消音游操作"))
            return
        if text == "恢复音游同步":
            event.stop_event()
            if not event.is_admin():
                await event.send(event.plain_result("请联系管理员恢复同步"))
                return
            # 按来源恢复：只解除 nearcade 的阻断，不动旧源的状态。
            self.snapshot_store.set_source_state(SOURCE_NEARCADE, "blocked_reason", "")
            self.snapshot_store.set_source_state(SOURCE_NEARCADE, "enabled", "true")
            self.snapshot_store.set_source_state(SOURCE_NEARCADE, "status", "idle")
            self.store.meta("attempt_day_nearcade", "")
            await event.send(
                event.plain_result("已解除 nearcade 来源的暂停，下一次定时检查时重试")
            )
            return
        if text == "音游地图状态":
            event.stop_event()
            lines = []
            for label, game in GAMES.items():
                points = [
                    p for p in self.store.all(game) if p["origin"] in RANKABLE_ORIGINS
                ]
                valid = sum(p["lat"] is not None for p in points)
                updated = self.store.meta("updated:" + game) or "尚未同步"
                lines.append(
                    f"{label}：{valid}/{len(points)} 个点位参与排序；来源更新：{updated}"
                )
                error = self.store.meta("error:" + game)
                if error:
                    lines.append(error)
            # 按来源展示状态；不再用单一 captcha_required 开关
            lines.extend(source_status_lines(self.store, self.snapshot_store))
            outcome = self.store.meta("nearcade:last_outcome")
            if outcome:
                lines.append(outcome)
            await event.send(event.plain_result("\n".join(lines)))
            return
        state = self.sessions.get(key)
        if not state and not event.is_private_chat():
            return
        try:
            location = from_components(event.get_messages())
        except ValueError as exc:
            event.stop_event()
            await event.send(event.plain_result(str(exc)))
            return
        if not state:
            if location:
                event.stop_event()
                await event.send(
                    event.plain_result("请先发送“查舞萌”或“查中二”，再私聊发送位置")
                )
            return
        event.stop_event()
        if state["action"] != "查" and not event.is_admin():
            self.sessions.pop(key, None)
            await event.send(
                event.plain_result(
                    "请联系管理员" + ("添加" if state["action"] == "增加" else "删除")
                )
            )
            return
        if key in self.busy:
            await event.send(event.plain_result("正在解析，请稍候"))
            return
        self.busy.add(key)
        try:
            answer, done = await self.process(state, text, location)
            if done:
                self.sessions.pop(key, None)
        except ValueError as exc:
            answer = str(exc)
        except Exception as exc:
            logger.warning("Arcade command failed: %s", type(exc).__name__)
            answer = "点位操作失败，请稍后重试"
        finally:
            self.busy.discard(key)
        await event.send(event.plain_result(answer))

    async def process(self, state, text, location):
        game, label = state["game"], state["label"]
        if state["action"] == "查":
            if not location:
                raise ValueError("请发送 QQ“位置”卡片，不是定位截图或实时位置共享")
            rows = self.store.nearest(game, location)
            all_points = [
                p for p in self.store.all(game) if p["origin"] in RANKABLE_ORIGINS
            ]
            missing = sum(p["lat"] is None for p in all_points)
            lines = [f"附近{label} · 按直线距离排序（最多 5 个）"]
            if self.store.meta(f"catalog_version:{game}") != "2":
                lines.append("全国分类名单正在重建，当前结果只覆盖已核对的部分店铺")
            if not rows:
                lines.append(
                    "本地暂时没有可计算距离的点位，请联系管理员检查数据或添加点位"
                )
            for i, p in enumerate(rows, 1):
                source = origin_label(p["origin"])
                lines.append(
                    f"{i}. {p['name']} · 约 {p['distance']:.2f} 公里\n地址：{p['address']}\n编号：{p['id']}（{source}）"
                )
            if missing:
                lines.append(
                    f"另有 {missing} 个来源点位缺少可靠坐标，未参与排序；结果仅针对已有坐标的数据"
                )
            lines.append(
                "来源更新："
                + (
                    self.store.meta("updated:" + game)
                    if self.store.meta("catalog_version:" + game) == "2"
                    else "全国名单核对未完成"
                )
                + "\n直线距离不是步行/驾车距离，出发前请核实营业与机台情况"
            )
            return "\n\n".join(lines), True
        if state["action"] == "增加":
            if text == "确认添加" and state.get("preview"):
                point = state["preview"]
            else:
                point = location or parse_text(text)
                if not point["name"]:
                    raise ValueError(
                        "位置卡片没有地点名称，请改用“店名 | 地址 | 纬度,经度”"
                    )
                if not point["address"]:
                    point["address"] = point["name"]
                if point["lat"] is None:
                    result = await asyncio.to_thread(
                        Client(self.secrets_file).geocode,
                        point["address"] + " " + point["name"],
                        self.store,
                    )
                    if state["expires"] <= time.monotonic():
                        raise ValueError("操作已超时，请重新发送增加命令")
                    if result.get("missing"):
                        raise ValueError(
                            "地址未解析到唯一具体地点，请补充完整地址，或发送 QQ 位置 / 文字经纬度"
                        )
                    point.update(lat=result["lat"], lng=result["lng"])
                    state["preview"] = point
                    return (
                        f"定位预览：{point['name']}\n{point['address']}\n纬度 {point['lat']}，经度 {point['lng']}（GCJ-02）\n核对后发送“确认添加”，或重新提供位置信息",
                        False,
                    )
            ident = self.store.add(game, point)
            return (
                f"已增加{label}：{point['name']}\n编号：{ident}\n本地新增点位不会被月度同步覆盖",
                True,
            )
        if text.startswith("确认删除 "):
            ident = text.removeprefix("确认删除 ").strip()
            if ident not in state.get("candidates", []):
                raise ValueError("请使用本次候选列表中的完整编号")
            if not self.store.remove(game, ident):
                raise ValueError("点位不存在或已被其他管理员删除")
            return (
                f"已从本地{label}查询中删除 {ident}；来源点位会持续屏蔽，不修改原网站",
                True,
            )
        if location:
            rows = [
                p for p in self.store.nearest(game, location) if p["distance"] <= 0.2
            ]
        else:
            if not text or len(text) > 200:
                raise ValueError("请输入点位编号或店名")
            points = self.store.all(game)
            rows = [p for p in points if p["id"] == text or p["name"] == text]
            if not rows:
                rows = [
                    p
                    for p in points
                    if text.casefold() in (p["name"] + " " + p["address"]).casefold()
                ]
        if not rows:
            raise ValueError(
                "未找到匹配点位；定位仅匹配 200 米内点位，可改发店名或查询结果中的编号"
            )
        if len(rows) > 5:
            raise ValueError("匹配超过 5 个点位，请提供更完整店名或编号")
        state["candidates"] = [p["id"] for p in rows]
        lines = [f"{p['name']}\n地址：{p['address']}\n编号：{p['id']}" for p in rows]
        return "\n\n".join(
            lines
        ) + "\n\n请发送“确认删除 完整编号”选择一条；不会批量删除", False

    async def scheduler(self):
        """每日调度。

        数据源已切换为 nearcade：**不再跑旧的 BEMANICN 抓取**，
        也不再做全国高德补坐标（上游记录自带坐标）。
        ``legacy_scrape_enabled`` 默认关闭，保留开关只为应急回滚。
        """
        while True:
            if self.config.get("sync_enabled", True):
                try:
                    now = datetime.now(CHINA)
                    stamp = now.isoformat(timespec="seconds")
                    if self.config.get("nearcade_enabled", True) and sync_is_due(
                        self.store
                    ):
                        note_attempt(self.store)
                        outcome = await asyncio.to_thread(
                            sync_nearcade, self.store, self.snapshot_store
                        )
                        self.store.meta("nearcade:last_outcome", outcome.describe())
                        logger.info("nearcade: %s", outcome.describe())
                    if self.config.get("legacy_scrape_enabled", False):
                        # 仅应急回滚用；默认关闭，避免旧爬虫被重新拉起。
                        await asyncio.to_thread(
                            monthly_sync,
                            self.store,
                            self.secrets_file,
                            stamp,
                            self.stop,
                        )
                except Exception as exc:
                    logger.warning("Arcade sync failed: %s", type(exc).__name__)
            await asyncio.sleep(3600)

    async def terminate(self):
        self.stop.set()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
