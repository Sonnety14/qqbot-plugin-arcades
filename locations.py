"""Parse supported QQ cards and rank GCJ-02 locations locally."""

import json
import math
import re


def coordinates(lat, lng):
    if isinstance(lat, bool) or isinstance(lng, bool):
        raise ValueError("经纬度必须为数字")
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError) as exc:
        raise ValueError("经纬度必须为数字") from exc
    if not (
        math.isfinite(lat)
        and math.isfinite(lng)
        and -90 <= lat <= 90
        and -180 <= lng <= 180
    ):
        raise ValueError("经纬度超出范围")
    if lat == 0 and lng == 0:
        raise ValueError("定位坐标无效，请重新发送位置")
    return lat, lng


def clean(value, limit=300):
    return (
        re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
        .replace("[CQ:", "［CQ:")
        .strip()[:limit]
    )


def from_components(components):
    for component in components:
        if isinstance(component, dict):
            kind = component.get("type", "")
            data = component.get("data", {})
        else:
            kind = getattr(getattr(component, "type", None), "value", "")
            data = {
                key: getattr(component, key, None)
                for key in ("lat", "lon", "title", "content")
            }
            if str(kind).lower() == "json":
                data = {"data": component.data}
        if str(kind).lower() == "location":
            lat, lng = coordinates(data.get("lat"), data.get("lon", data.get("lng")))
            return dict(
                lat=lat,
                lng=lng,
                name=clean(data.get("title")),
                address=clean(data.get("content")),
            )
        if str(kind).lower() != "json":
            continue
        card = data.get("data", data)
        if isinstance(card, str):
            if len(card) > 32768:
                continue
            try:
                card = json.loads(card)
            except (ValueError, RecursionError):
                continue
        if not isinstance(card, dict) or card.get("app") != "com.tencent.map":
            continue
        meta = card.get("meta", {})
        location = meta.get("Location.Search") if isinstance(meta, dict) else None
        if not isinstance(location, dict):
            raise ValueError("未识别此定位卡片，请用 QQ 聊天框的“位置”功能重新发送")
        lat, lng = coordinates(location.get("lat"), location.get("lng"))
        return dict(
            lat=lat,
            lng=lng,
            name=clean(location.get("name")),
            address=clean(location.get("address")),
        )
    return None


def parse_text(text):
    """Parse administrator input; address-only entries need geocoding confirmation."""
    fields = [v.strip() for v in text.split("|")]
    if len(fields) not in (2, 3) or not all(fields[:2]) or len(text) > 1000:
        raise ValueError(
            "格式：店名 | 完整地址 | 纬度,经度\n也可只写：店名 | 完整地址（需要高德 Key，将先预览坐标）"
        )
    result = dict(
        name=clean(fields[0], 100), address=clean(fields[1]), lat=None, lng=None
    )
    if len(fields) == 3:
        parts = re.split(r"[,，\s]+", fields[2])
        if len(parts) != 2:
            raise ValueError(
                "坐标格式为 纬度,经度，例如 30.67,104.06；使用腾讯/高德 GCJ-02 坐标"
            )
        result["lat"], result["lng"] = coordinates(*parts)
    return result


def distance(lat1, lng1, lat2, lng2):
    a, b = math.radians(lat1), math.radians(lat2)
    dlat, dlng = b - a, math.radians(lng2 - lng1)
    v = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlng / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, v))))
