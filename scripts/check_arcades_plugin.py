"""Offline AstrBot command tests with temporary data and no network calls."""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import importlib.util
spec = importlib.util.spec_from_file_location('astrbot_plugin_arcades', ROOT/'__init__.py', submodule_search_locations=[str(ROOT)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from astrbot.api.message_components import Json  # noqa: E402
from astrbot.api.star import StarTools  # noqa: E402
from astrbot_plugin_arcades.main import Arcades  # noqa: E402


class Event:
    def __init__(
        self,
        text="",
        user="42",
        admin=False,
        group="",
        components=None,
        platform="example-qq",
    ):
        self.message_str = text
        self.user, self.admin, self.group, self.platform = user, admin, group, platform
        self.components = components or []
        self.sent = []
        self.stopped = False

    def get_sender_id(self):
        return self.user

    def get_self_id(self):
        return "10000"

    def get_platform_id(self):
        return self.platform

    def get_group_id(self):
        return self.group

    def is_private_chat(self):
        return not self.group

    def is_admin(self):
        return self.admin

    def get_messages(self):
        return self.components

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    async def send(self, text):
        self.sent.append(text)


async def main():
    with tempfile.TemporaryDirectory() as folder:
        with patch.object(StarTools, "get_data_dir", return_value=Path(folder)):
            plugin = Arcades(SimpleNamespace(), {"sync_enabled": False, "platform_id": "example-qq"})
        with patch.object(StarTools, "get_data_dir", return_value=Path(folder)):
            unconfigured = Arcades(SimpleNamespace(), {"sync_enabled": False})
        probe = Event("查舞萌")
        await unconfigured.handle(probe)
        assert not probe.stopped and not probe.sent
        card = Json(
            data={
                "app": "com.tencent.map",
                "meta": {
                    "Location.Search": {
                        "lat": "30",
                        "lng": "104",
                        "name": "公共测试地点",
                        "address": "测试路1号",
                    }
                },
            }
        )

        async def send(text="", **kwargs):
            event = Event(text, **kwargs)
            await plugin.handle(event)
            return event

        assert (await send("增加舞萌")).sent == ["请联系管理员添加"]
        assert (await send("删除中二")).sent == ["请联系管理员删除"]
        # 音游插件本身就在群聊一律拒绝（与群白名单无关），
        # 这里断言它确实被拦下并给出转私聊提示。
        group_rejected = await send("查舞萌", group="999")
        assert group_rejected.stopped, "群聊里的音游命令必须被拦下"
        assert any("请在私聊中使用" in str(m) for m in group_rejected.sent), group_rejected.sent
        assert not (await send("查舞萌", platform="other")).stopped
        assert not (await send("你好")).stopped
        assert "请先发送" in (await send(components=[card])).sent[0]
        assert "发送位置信息" in (await send("查中二")).sent[0]
        assert "请先发送" in (await send(user="43", components=[card])).sent[0]
        assert "没有可计算" in (await send(components=[card])).sent[0]
        for i in range(7):
            plugin.store.add(
                "maimai", dict(name=f"店{i}", address="地址", lat=30 + i / 100, lng=104)
            )
        await send("查舞萌")
        result = (await send(components=[card])).sent[0]
        assert "5. 店4" in result and "店5" not in result and "直线距离" in result
        await send("增加中二", admin=True)
        assert "已增加中二" in (await send(admin=True, components=[card])).sent[0]
        await send("增加中二", admin=True)
        assert "请联系管理员添加" in (await send(components=[card])).sent[0]
        await send("增加中二", admin=True)
        assert (
            "已增加" in (await send("第二家 | 地址 | 30.001,104", admin=True)).sent[0]
        )
        await send("删除中二", admin=True)
        preview = (await send(admin=True, components=[card])).sent[0]
        assert "确认删除" in preview and len(plugin.store.all("chuni")) == 2
        state = plugin.sessions[("example-qq", "42")]
        ident = state["candidates"][0]
        assert "本次候选" in (await send("确认删除 fake", admin=True)).sent[0]
        assert "已从本地" in (await send("确认删除 " + ident, admin=True)).sent[0]
        assert len(plugin.store.all("chuni")) == 1
        await send("增加舞萌", admin=True)
        assert "格式" in (await send("bad input", admin=True)).sent[0]
        assert "取消" in (await send("取消音游操作")).sent[0]
        await send("查舞萌")
        plugin.sessions[("example-qq", "42")]["expires"] = time.monotonic() - 1
        assert "请先发送" in (await send(components=[card])).sent[0]
        assert "来源更新" in (await send("音游地图状态")).sent[0]
        await plugin.initialize()
        await asyncio.sleep(0)
        await plugin.terminate()
        assert plugin.task.done()
    print(
        "PASS: native QQ JSON card, top five, scopes, isolation, admin recheck, add/delete confirmation, timeout and lifecycle"
    )


if __name__ == "__main__":
    asyncio.run(main())
