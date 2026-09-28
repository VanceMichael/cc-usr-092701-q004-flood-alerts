"""行政区划目录、值守角色与权限规则。

目录是一棵“省—县—乡镇”三级树。权限只按层级与辖区判定：

* 乡镇值守人员只能处置本乡镇辖区；
* 县级可处置本县（含下属乡镇）；
* 省级可处置省内任意区域，且只有省级能发起跨县/跨乡镇的联动转移；
* 预警在某区域无人确认时，沿目录树逐级上交（乡镇→县→省）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

PROVINCE = "province"
COUNTY = "county"
TOWNSHIP = "township"
LEVELS = (PROVINCE, COUNTY, TOWNSHIP)
_LEVEL_RANK = {TOWNSHIP: 1, COUNTY: 2, PROVINCE: 3}


@dataclass(frozen=True)
class Area:
    code: str
    name: str
    level: str
    parent: str | None = None


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    level: str
    area_code: str  # 任职机构所在行政区

    @property
    def is_province(self) -> bool:
        return self.level == PROVINCE


@dataclass
class Catalog:
    areas: dict[str, Area] = field(default_factory=dict)
    users: dict[str, User] = field(default_factory=dict)

    def add_area(self, code: str, name: str, level: str, parent: str | None = None) -> None:
        if level not in LEVELS:
            raise ValueError(f"未知层级：{level}")
        if parent is not None and parent not in self.areas:
            raise ValueError(f"上级行政区不存在：{parent}")
        self.areas[code] = Area(code, name, level, parent)

    def add_user(self, user_id: str, name: str, level: str, area_code: str) -> None:
        if level not in LEVELS:
            raise ValueError(f"未知角色层级：{level}")
        if area_code not in self.areas:
            raise ValueError(f"任职行政区不存在：{area_code}")
        self.users[user_id] = User(user_id, name, level, area_code)

    # ---- 辖区关系 ----
    def contains(self, ancestor: str, descendant: str) -> bool:
        """ancestor 行政区是否包含 descendant（含相等）。"""
        if ancestor == descendant:
            return True
        node = self.areas.get(descendant)
        while node is not None and node.parent is not None:
            if node.parent == ancestor:
                return True
            node = self.areas.get(node.parent)
        return False

    def children(self, code: str) -> list[str]:
        return [a.code for a in self.areas.values() if a.parent == code]

    def parent(self, code: str) -> str | None:
        node = self.areas.get(code)
        return node.parent if node else None

    def can_command(self, user: User, area_code: str) -> bool:
        """该用户能否处置该区域：机构层级不低于区域层级，且辖区覆盖该区域。"""
        area = self.areas.get(area_code)
        if area is None:
            return False
        if _LEVEL_RANK[user.level] < _LEVEL_RANK[area.level]:
            return False
        return self.contains(user.area_code, area_code)

    def can_cross_region(self, user: User) -> bool:
        return user.is_province

    def handoff_target(self, area_code: str) -> str | None:
        """区域确认超时后的上一级行政区；省级仍无人确认时返回 None。"""
        area = self.areas.get(area_code)
        if area is None or area.level == PROVINCE:
            return None
        return area.parent


def demo_catalog() -> Catalog:
    """构造与 fixtures 事实资料一致的演示目录（鄂西北两片相邻山区）。"""
    cat = Catalog()
    cat.add_area("P42", "湖北省", PROVINCE)
    cat.add_area("C420322", "郧西县", COUNTY, "P42")
    cat.add_area("C420324", "竹溪县", COUNTY, "P42")
    cat.add_area("T42032201", "关防乡", TOWNSHIP, "C420322")
    cat.add_area("T42032202", "湖北口乡", TOWNSHIP, "C420322")
    cat.add_area("T42032401", "丰溪镇", TOWNSHIP, "C420324")
    cat.add_area("T42032402", "向坝乡", TOWNSHIP, "C420324")

    cat.add_user("u-prov", "省防汛值班员", PROVINCE, "P42")
    cat.add_user("u-yx", "郧西县指挥员", COUNTY, "C420322")
    cat.add_user("u-zx", "竹溪县指挥员", COUNTY, "C420324")
    cat.add_user("u-gf", "关防乡值守员", TOWNSHIP, "T42032201")
    cat.add_user("u-hbk", "湖北口乡值守员", TOWNSHIP, "T42032202")
    cat.add_user("u-fx", "丰溪镇值守员", TOWNSHIP, "T42032401")
    cat.add_user("u-xb", "向坝乡值守员", TOWNSHIP, "T42032402")
    return cat
