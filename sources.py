"""Bounded, authenticated monthly downloads from fixed official endpoints."""

import html
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

from .locations import clean, coordinates
from .snapshot_store import COORDINATE_PRESERVING_ORIGINS, SOURCE_NEARCADE

HOSTS = {
    "map.bemanicn.com",
    "bemanicn.com",
    "restapi.amap.com",
    "nearca.de",
    "nearcade.cn",
}
MAX_BYTES = 12 * 1024 * 1024


class HumanVerificationError(ValueError):
    pass


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urllib.parse.urlsplit(newurl)
        if (
            url.scheme != "https"
            or url.hostname not in HOSTS
            or url.port not in (None, 443)
        ):
            raise ValueError("同步跳转到未授权域名，已停止")
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if result and urllib.parse.urlsplit(req.full_url).hostname != url.hostname:
            result.remove_header("X-xsrf-token")
        return result


def page_data(raw):
    match = re.search(r'data-page="([^"]+)"', raw)
    if not match:
        raise ValueError("地图页面结构变化或需要人工验证")
    page = json.loads(html.unescape(match[1]))
    if not isinstance(page.get("props"), dict):
        raise ValueError("地图页面缺少数据")
    return page


class Client:
    def __init__(self, secrets_file):
        self.secrets_file = Path(secrets_file)
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            SafeRedirect(),
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.ProxyHandler({}),
        )

    def secrets(self):
        if not self.secrets_file.exists():
            return {}
        return json.loads(self.secrets_file.read_text(encoding="utf-8"))

    def read(self, url, data=None, headers=None):
        target = urllib.parse.urlsplit(url)
        if (
            target.scheme != "https"
            or target.hostname not in HOSTS
            or target.port not in (None, 443)
        ):
            raise ValueError("仅支持预设数据来源")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": "AstrBot arcades directory (monthly sync)",
                **(headers or {}),
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"数据源返回 HTTP {exc.code}，保留旧数据") from None
        except (OSError, urllib.error.URLError):
            raise ValueError("数据源连接失败，保留旧数据") from None
        if len(raw) > MAX_BYTES:
            raise ValueError("数据源响应过大")
        text = raw.decode("utf-8")
        if (
            'data-page="' in text
            and page_data(text).get("component") == "Captcha/Verify"
        ):
            raise HumanVerificationError(
                "全国音游地图要求当前会话完成人机验证，已暂停同步；保留已核对数据"
            )
        return text

    def login(self):
        credentials = self.secrets()
        if not credentials.get("email") or not credentials.get("password"):
            raise ValueError("尚未配置全国音游地图登录信息")
        self.read("https://map.bemanicn.com/oauth/redirect")
        token = next(
            (
                urllib.parse.unquote(c.value)
                for c in self.jar
                if c.name == "XSRF-TOKEN" and c.domain == "bemanicn.com"
            ),
            None,
        )
        if token:
            body = urllib.parse.urlencode(
                {
                    "email": credentials["email"],
                    "password": credentials["password"],
                    "remember": "0",
                }
            ).encode()
            result = page_data(
                self.read(
                    "https://bemanicn.com/login",
                    body,
                    {
                        "X-XSRF-TOKEN": token,
                        "Referer": "https://bemanicn.com/login",
                        "Accept": "text/html",
                    },
                )
            )
            if not result["props"].get("auth", {}).get("user"):
                raise ValueError(
                    "地图登录未完成，可能需要验证邮箱、验证码或重新配置密码"
                )

    def download_chuni(self):
        return self.download_game("chuni")

    def download_maimai(self):
        return self.download_game("maimai")

    def catalogue_page(self, path, component):
        """Cache only public source fields, never authentication/session props."""
        key = "catalog:" + self.month + ":" + path
        cached = self.store.meta(key)
        if cached:
            return json.loads(cached)
        if self.stop and self.stop.is_set():
            raise ValueError("同步已中断，下次从缓存继续")
        for attempt in range(3):
            time.sleep(2 if attempt == 0 else 5)
            page = page_data(self.read("https://map.bemanicn.com" + path))
            if page.get("component") == component:
                break
        else:
            raise ValueError(f"全国音游地图页面未返回预期分类数据（{path}）")
        props = {
            k: v
            for k, v in page["props"].items()
            if k
            in {
                "id",
                "name",
                "cities",
                "provinces",
                "arcades",
                "arcade_count",
                "city",
                "shop",
            }
        }
        if "shop" in props:
            shop = props["shop"]
            props = {
                "shop": {
                    k: shop[k]
                    for k in (
                        "id",
                        "name",
                        "address",
                        "province_code",
                        "city_code",
                        "county_code",
                        "arcades",
                    )
                }
            }
        self.store.meta(key, json.dumps(props, ensure_ascii=False))
        return props

    def download_game(self, game):
        if not getattr(self, "logged_in", False):
            self.login()
            self.logged_in = True
        title = {"maimai": 1, "chuni": 3}[game]
        root = self.catalogue_page(f"/games/{title}", "Games/Show")
        expected = int(root["arcades"]["total"])
        points, row_ids = {}, set()
        cities = list(root["cities"])
        cities.sort(key=str)
        for index, city_code in enumerate(cities):
            self.store.meta(
                "catalog_progress", f"{game}：核对城市 {index + 1}/{len(cities)}"
            )
            page = self.catalogue_page(f"/games/{title}?city={city_code}", "Games/Show")
            listing = page["arcades"]
            rows = listing["data"]
            total = int(listing["total"])
            if not total:
                continue
            region = self.catalogue_page(f"/region/city/{city_code}", "Region/City")[
                "city"
            ]
            shops = {int(s["id"]): s for s in region["shops"]}
            if int(listing["last_page"]) > 1:
                # The site's next-page URL currently redirects to Games/Index.
                # Read the same site's shop details instead of accepting a partial list.
                rows = []
                for ident in shops:
                    detail = self.catalogue_page(f"/s/{ident}", "Shop/Show")["shop"]
                    rows.extend(
                        r for r in detail["arcades"] if int(r["title_id"]) == title
                    )
                if len(rows) != total:
                    raise ValueError(
                        f"{region['name']}分类条目数量变化或不完整，保留已验证数据，稍后重试"
                    )
            elif len(rows) != total:
                raise ValueError("分类分页条目不完整")
            city_points = normalize_category(rows, title, shops, region)
            points.update({p["id"]: p for p in city_points})
            row_ids.update(int(r["id"]) for r in rows)
            # Publish only validated category members while a first national scan runs.
            self.store.merge_verified(game, city_points)
        if len(row_ids) != expected:
            raise ValueError(
                f"全国分类完整性核对未通过：{len(row_ids)}/{expected} 条，不标记完整同步"
            )
        self.store.meta("catalog_progress", "分类名单核对完成")
        return list(points.values())

    def geocode(self, address, store):
        cached = store.cache(address)
        if cached is not None:
            return cached
        key = self.secrets().get("amap_key", "").strip()
        if not key:
            raise ValueError(
                "尚未配置高德 Web 服务 Key；可直接发送 QQ 位置或文字经纬度"
            )
        day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        daily_limit = int(store.meta("geocode_limit:" + day) or "1000")
        if not store.geocode_slot(day, daily_limit):
            raise ValueError(f"已达到插件当日 {daily_limit} 次地址解析上限，明天自动继续")
        params = urllib.parse.urlencode(
            {"key": key, "address": address, "output": "JSON"}
        )
        result = json.loads(
            self.read("https://restapi.amap.com/v3/geocode/geo?" + params)
        )
        if result.get("status") != "1":
            code = str(result.get("infocode", "unknown"))
            if code == "30001":
                result = {"missing": True, "reason": "address_engine_error"}
                store.cache(address, result)
                return result
            safe_code = code if code.isdigit() else "unknown"
            raise ValueError(
                f"高德地理编码失败（代码 {safe_code}），请检查 Key 和服务额度"
            )
        geocodes = result.get("geocodes", [])
        # Never treat a city/district center as an arcade's precise location.
        if len(geocodes) != 1 or geocodes[0].get("level") not in {
            "门牌号",
            "门址",
            "兴趣点",
        }:
            result = {"missing": True}
        else:
            lng, lat = geocodes[0]["location"].split(",")
            lat, lng = coordinates(lat, lng)
            result = {"lat": lat, "lng": lng, "level": geocodes[0]["level"]}
        store.cache(address, result)
        return result


def normalize_category(rows, title, shops, region):
    """Game membership comes exclusively from Games/Show or Shop/Show arcade rows."""
    points = {}
    counties = {str(c["county_code"]): c["name"] for c in region.get("counties", [])}
    for row in rows:
        if int(row["title_id"]) != title:
            raise ValueError("游戏分类混入其他游戏，拒绝同步")
        ident = int(row["shop_id"])
        shop = shops.get(ident)
        if shop is None:
            # Deleted shops have null shop in the website's own list and are not displayed.
            if "shop" in row and row["shop"] is None:
                continue
            raise ValueError("分类店铺不在城市地址目录内")
        name, address = clean(shop["name"], 100), clean(shop["address"])
        if not name or not address:
            raise ValueError("店铺缺少名称或地址")
        prefix = region["province"]["name"] + region["name"]
        if region["province"]["name"] == region["name"]:
            prefix = region["name"]
        if not address.startswith(region["province"]["name"]):
            if not address.startswith(region["name"]):
                county = counties.get(str(shop.get("county_code")), "")
                prefix += "" if address.startswith(county) else county
            else:
                prefix = region["province"]["name"]
            address = prefix + address
        points[ident] = dict(
            id=f"bcn-{ident}",
            origin=SOURCE_NEARCADE,
            name=name,
            address=address,
            lat=None,
            lng=None,
        )
    return list(points.values())


def monthly_sync(store, secrets_file, stamp, stop=None):
    """Keep independent snapshots; a source failure cannot erase either manual data or old snapshots."""
    if store.meta("captcha_required") == "1":
        return
    client = Client(secrets_file)
    month = stamp[:7]
    client.store, client.month, client.stop = store, month, stop
    for game in ("maimai", "chuni"):
        if stop and stop.is_set():
            return
        if store.meta(f"catalog_version:{game}") == "2" and store.meta(
            f"updated:{game}"
        ).startswith(month):
            continue
        try:
            points = (
                client.download_maimai()
                if game == "maimai"
                else client.download_chuni()
            )
            old = {p["id"]: p for p in store.all(game)}
            for p in points:
                previous = old.get(p["id"])
                if (
                    p["lat"] is None
                    and previous
                    and (p["name"], p["address"])
                    == (previous["name"], previous["address"])
                ):
                    p["lat"], p["lng"] = previous["lat"], previous["lng"]
            store.replace_source(game, points, stamp)
            store.meta(f"catalog_version:{game}", "2")
            store.meta(f"error:{game}", "")
        except HumanVerificationError as exc:
            store.meta("captcha_required", "1")
            store.meta("error:" + game, str(exc))
            store.meta("catalog_progress", str(exc))
            return
        except Exception as exc:
            store.meta(
                f"error:{game}",
                str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
            )


def address_candidates(address):
    # Indoor floor/unit suffixes are not separate geographic locations.
    # Preserve street numbers and mall/building names.
    shorter = re.sub(
        r"(?:[ABCD]馆[- ]?)?(?:-?\d+[F层楼]|[BF]\d+).*$", "", address, flags=re.I
    ).strip()
    return list(dict.fromkeys(a for a in (address, shorter) if a))


def enrich(store, secrets_file, limit=300, stop=None):
    client = Client(secrets_file)
    if not client.secrets().get("amap_key"):
        return
    pending = [
        (game, p)
        for game in ("maimai", "chuni")
        for p in store.all(game)
        if p["lat"] is None and p["origin"] in COORDINATE_PRESERVING_ORIGINS
    ]
    count = 0
    for game, point in pending:
        if stop and stop.is_set():
            return
        candidates = address_candidates(point["address"])
        if all((store.cache(a) or {}).get("missing") for a in candidates):
            continue
        if count >= limit:
            break
        try:
            for address in candidates:
                result = client.geocode(address, store)
                if not result.get("missing"):
                    break
        except ValueError as exc:
            store.meta("error:geocode", str(exc))
            return
        if not result.get("missing"):
            store.fill_coordinates(game, point["id"], point["address"], result)
        count += 1
        time.sleep(1)
    store.meta("error:geocode", "")
