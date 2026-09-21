"""OWL-lite 本体模型 + SHACL 式约束。

为什么不用 owlready2 / rdflib：本体的**演化**才是本项目的核心，演化需要对
公理做细粒度的增删与一致性回滚，重量级推理机在这条路径上是负担且不可控。
这里实现的是受限但完整的公理集——类层级、互斥类、属性 domain/range、
基数约束——足以支撑 UNK 候选概念的合法性判定，且每一步都可解释、可回滚。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from govfin.errors import ConstraintViolation, OntologyError, ValidationError


@dataclass
class OwlProperty:
    """对象属性（连接两个个体）或数据属性（连到字面量）。"""

    name: str
    domain: str
    range: str
    kind: str = "object"  # object | datatype
    definition: str = ""
    inverse: str | None = None
    functional: bool = False
    min_cardinality: int | None = None
    max_cardinality: int | None = None
    aliases: list[str] = field(default_factory=list)
    rdfs_range: str | None = None  # datatype 属性用: string/integer/date/float/boolean

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OwlClass:
    name: str
    parent: str | None = None
    definition: str = ""
    aliases: list[str] = field(default_factory=list)
    # 该类的实例必须/允许携带的属性名。用于生成 SHACL shape。
    required_properties: list[str] = field(default_factory=list)
    optional_properties: list[str] = field(default_factory=list)
    disjoint_with: list[str] = field(default_factory=list)
    layer: int | None = None
    origin: str = "seed"  # seed | evolved
    introduced_in_version: str = "0.1.0"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ShapeViolation:
    focus_node: str
    property_name: str
    message: str
    severity: str = "Violation"

    def to_dict(self) -> dict:
        return asdict(self)


class Ontology:
    """本体 + 版本号 + 公理一致性检查。

    不可变语义：所有变更走 ``apply_edits``，成功则版本号递增，失败则整批回滚
    —— 半途生效的本体是最难排查的一类脏数据。
    """

    def __init__(
        self,
        *,
        version: str = "0.1.0",
        classes: Iterable[OwlClass] = (),
        properties: Iterable[OwlProperty] = (),
        provenance: dict | None = None,
    ) -> None:
        self.version = version
        self.classes: dict[str, OwlClass] = {c.name: c for c in classes}
        self.properties: dict[str, OwlProperty] = {p.name: p for p in properties}
        self.history: list[dict] = []
        self.provenance = provenance or {}
        self._validate_axioms()

    # ---------- 查询 ----------

    def get_class(self, name: str) -> OwlClass | None:
        return self.classes.get(name)

    def resolve_alias(self, name: str) -> str | None:
        """按名称或别名找类。UNK 提及常常是别名，这一步是聚类对齐的前提。"""
        if name in self.classes:
            return name
        for cls in self.classes.values():
            if name in cls.aliases:
                return cls.name
        return None

    def resolve_property_alias(self, name: str) -> str | None:
        """按名称或别名找属性（对象属性与数据属性通吃）。"""
        if name in self.properties:
            return name
        for prop in self.properties.values():
            if name in prop.aliases:
                return prop.name
        return None

    def ancestors(self, name: str) -> list[str]:
        chain: list[str] = []
        seen: set[str] = set()
        cur = self.classes.get(name)
        while cur is not None and cur.parent is not None and cur.parent not in seen:
            seen.add(cur.parent)
            chain.append(cur.parent)
            cur = self.classes.get(cur.parent)
        return chain

    def is_subclass_of(self, child: str, ancestor: str) -> bool:
        if child == ancestor:
            return True
        return ancestor in self.ancestors(child)

    def descendants(self, name: str) -> list[str]:
        return [c.name for c in self.classes.values() if self.is_subclass_of(c.name, name)]

    def leaf_classes(self) -> list[str]:
        parents = {c.parent for c in self.classes.values() if c.parent}
        return [c.name for c in self.classes.values() if c.name not in parents]

    def node_types(self, layer: int | None = None) -> list[str]:
        out = [c for c in self.classes.values() if layer is None or c.layer == layer]
        return sorted(c.name for c in out)

    # ---------- 公理校验 ----------

    def _validate_axioms(self) -> list[ShapeViolation]:
        """检查类层级、互斥、属性 domain/range。构造与变更后都会跑。"""
        problems: list[ShapeViolation] = []

        for name, cls in self.classes.items():
            if cls.parent is not None and cls.parent not in self.classes:
                problems.append(
                    ShapeViolation(name, "rdfs:subClassOf", f"父类 '{cls.parent}' 未在本体中定义")
                )
            if cls.parent == name:
                problems.append(ShapeViolation(name, "rdfs:subClassOf", "类不能是自己的父类"))

        for name in self.classes:
            if self._has_cycle_from(name):
                problems.append(ShapeViolation(name, "rdfs:subClassOf", f"类层级在 '{name}' 上成环"))

        for name, cls in self.classes.items():
            for other in cls.disjoint_with:
                if other not in self.classes:
                    problems.append(
                        ShapeViolation(name, "owl:disjointWith", f"互斥类 '{other}' 未定义")
                    )
                elif self.is_subclass_of(name, other):
                    problems.append(
                        ShapeViolation(
                            name,
                            "owl:disjointWith",
                            f"'{name}' 既继承又互斥于 '{other}'，本体不一致",
                        )
                    )

        for pname, prop in self.properties.items():
            if prop.domain not in self.classes:
                problems.append(ShapeViolation(pname, "rdfs:domain", f"domain 类 '{prop.domain}' 未定义"))
            if prop.kind == "object" and prop.range not in self.classes:
                problems.append(ShapeViolation(pname, "rdfs:range", f"range 类 '{prop.range}' 未定义"))

        if problems:
            raise ConstraintViolation(
                f"本体公理校验失败，共 {len(problems)} 个问题",
                violations=[p.to_dict() for p in problems],
            )
        return problems

    def _has_cycle_from(self, start: str) -> bool:
        seen: set[str] = set()
        cur: str | None = start
        while cur is not None:
            if cur in seen:
                return True
            seen.add(cur)
            node = self.classes.get(cur)
            cur = node.parent if node else None
        return False

    def check_conflicts(self, candidate: OwlClass) -> list[dict]:
        """判断一个候选类能否安全加入。

        这是 UNK 演化管道的闸门：宁可拒绝一个合法候选（人工仲裁可放行），
        也不能放过一个会让图谱结构崩坏的候选。
        """
        conflicts: list[dict] = []

        existing = self.resolve_alias(candidate.name)
        if existing is not None and existing != candidate.name:
            conflicts.append(
                {
                    "type": "alias_collision",
                    "message": f"候选名 '{candidate.name}' 与已有类 '{existing}' 的别名冲突",
                    "severity": "warning",
                }
            )
        if candidate.name in self.classes:
            conflicts.append(
                {
                    "type": "duplicate_class",
                    "message": f"类 '{candidate.name}' 已存在",
                    "severity": "error",
                }
            )

        if candidate.parent is not None and candidate.parent not in self.classes:
            conflicts.append(
                {
                    "type": "missing_parent",
                    "message": f"父类 '{candidate.parent}' 不存在，需先定义或改挂已有父类",
                    "severity": "error",
                }
            )

        # 互斥检查：候选的父类不能与任何互斥类存在继承关系
        if candidate.parent and candidate.parent in self.classes:
            parent_cls = self.classes[candidate.parent]
            for disjoint in parent_cls.disjoint_with:
                if self.is_subclass_of(candidate.name, disjoint):
                    conflicts.append(
                        {
                            "type": "disjoint_violation",
                            "message": f"'{candidate.name}' 同时继承互斥类 '{candidate.parent}' 与 '{disjoint}'",
                            "severity": "error",
                        }
                    )

        for prop in candidate.required_properties:
            if prop not in self.properties:
                conflicts.append(
                    {
                        "type": "unknown_property",
                        "message": f"必需属性 '{prop}' 未在本体中定义",
                        "severity": "warning",
                    }
                )

        # 自环：候选若声明为某属性的 domain 与 range 同一个未定义类，容易制造推理环路
        for cls in candidate.disjoint_with:
            if cls == candidate.name:
                conflicts.append(
                    {"type": "self_disjoint", "message": "类不能与自己互斥", "severity": "error"}
                )

        return conflicts

    def has_blocking_conflicts(self, candidate: OwlClass) -> bool:
        return any(c["severity"] == "error" for c in self.check_conflicts(candidate))

    # ---------- SHACL 式实例校验 ----------

    def inherited_required(self, node_type: str) -> list[str]:
        """本类及其所有祖先声明的必需属性。

        必需属性必须沿继承链传递，否则"政务记录必须有记录编号"对
        ``工商登记`` 这类子类就失效了——而子类恰恰是抽取器真正产出的类型。
        """
        names: list[str] = []
        for name in [node_type, *self.ancestors(node_type)]:
            cls = self.classes.get(name)
            if cls is None:
                continue
            for prop in cls.required_properties:
                if prop not in names:
                    names.append(prop)
        return names

    def declared_properties(self, node_type: str) -> list[str]:
        out = self.inherited_required(node_type)
        for name in [node_type, *self.ancestors(node_type)]:
            cls = self.classes.get(name)
            if cls is None:
                continue
            for prop in cls.optional_properties:
                if prop not in out:
                    out.append(prop)
        return out

    def validate_instance(self, node_type: str, props: dict) -> list[ShapeViolation]:
        """校验一个图节点是否满足其类型对应 shape 的约束。"""
        cls = self.classes.get(node_type)
        if cls is None:
            return [ShapeViolation(str(props.get("id", "?")), "rdf:type", f"未定义类型 '{node_type}'")]

        violations: list[ShapeViolation] = []
        node_id = str(props.get("id", props.get("名称", "?")))

        for pname in self.inherited_required(node_type):
            if props.get(pname) in (None, "", [], {}):
                violations.append(
                    ShapeViolation(node_id, pname, f"必需属性 '{pname}' 缺失", severity="Violation")
                )

        for pname, value in props.items():
            prop = self.properties.get(pname)
            if prop is None or value in (None, "", [], {}):
                continue
            if prop.domain != node_type and not self.is_subclass_of(node_type, prop.domain):
                violations.append(
                    ShapeViolation(
                        node_id,
                        pname,
                        f"属性 '{pname}' 的 domain 是 '{prop.domain}'，不适合类型 '{node_type}'",
                        severity="Warning",
                    )
                )
            if prop.kind == "datatype" and prop.rdfs_range:
                if not _datatype_ok(value, prop.rdfs_range):
                    violations.append(
                        ShapeViolation(
                            node_id,
                            pname,
                            f"属性 '{pname}' 期望 {prop.rdfs_range}，实际 {type(value).__name__}={value!r}",
                            severity="Violation",
                        )
                    )

        return violations

    def cardinality_violations(self, node_type: str, props: dict) -> list[ShapeViolation]:
        """一对多/一对一约束：值可以是列表，检查 min/max 基数。"""
        cls = self.classes.get(node_type)
        if cls is None:
            return []
        out: list[ShapeViolation] = []
        node_id = str(props.get("id", "?"))
        for pname in self.declared_properties(node_type):
            prop = self.properties.get(pname)
            if prop is None:
                continue
            raw = props.get(pname)
            if raw is None:
                count = 0
            elif isinstance(raw, (list, tuple, set)):
                count = len(raw)
            else:
                count = 1
            if prop.min_cardinality is not None and count < prop.min_cardinality:
                out.append(
                    ShapeViolation(
                        node_id, pname, f"基数 {count} 小于要求的最小值 {prop.min_cardinality}"
                    )
                )
            if prop.max_cardinality is not None and count > prop.max_cardinality:
                out.append(
                    ShapeViolation(
                        node_id, pname, f"基数 {count} 超过允许的最大值 {prop.max_cardinality}"
                    )
                )
        return out

    # ---------- 变更 ----------

    def apply_edits(self, edits: list[dict], *, new_version: str, rationale: str = "", actor: str = "arbiter") -> dict:
        """原子地应用一批本体编辑。

        先快照，再逐条应用，任何一条失败就整体回滚并抛出约束异常。
        """
        snapshot = self.snapshot()
        applied: list[str] = []
        try:
            for edit in edits:
                self._apply_one(edit)
                applied.append(edit.get("action", "?"))
            self._validate_axioms()
        except Exception as exc:
            self._restore(snapshot)
            raise ConstraintViolation(
                f"本体编辑批次失败并已回滚：{exc}",
                violations=[{"applied_before_failure": applied, "error": str(exc)}],
            ) from exc

        previous = self.version
        self.version = new_version
        # 提案在生成时还不知道自己会落在哪个版本上（多提案合批时更是如此），
        # 这里统一回填。留一个 "next" 在版本历史里是没有意义的占位符。
        for cls in self.classes.values():
            if cls.introduced_in_version in ("next", ""):
                cls.introduced_in_version = new_version
        record = {
            "from_version": previous,
            "to_version": new_version,
            "edits": edits,
            "rationale": rationale,
            "actor": actor,
        }
        self.history.append(record)
        return record

    def _apply_one(self, edit: dict) -> None:
        action = edit.get("action")
        if action == "add_class":
            cls = OwlClass(
                name=edit["class_name"],
                parent=edit.get("parent_class"),
                definition=edit.get("definition", ""),
                aliases=list(edit.get("aliases", [])),
                required_properties=list(edit.get("required_properties", [])),
                optional_properties=list(edit.get("optional_properties", [])),
                disjoint_with=list(edit.get("disjoint_with", [])),
                layer=edit.get("layer"),
                origin="evolved",
                introduced_in_version=edit.get("introduced_in_version", "next"),
            )
            if cls.name in self.classes:
                raise OntologyError(f"类 '{cls.name}' 已存在，不能重复新增")
            self.classes[cls.name] = cls

        elif action == "add_property":
            prop = OwlProperty(
                name=edit["property_name"],
                domain=edit["domain"],
                range=edit["range"],
                kind=edit.get("kind", "object"),
                definition=edit.get("definition", ""),
                inverse=edit.get("inverse"),
                functional=bool(edit.get("functional", False)),
                min_cardinality=edit.get("min_cardinality"),
                max_cardinality=edit.get("max_cardinality"),
                aliases=list(edit.get("aliases", [])),
                rdfs_range=edit.get("rdfs_range"),
            )
            if prop.name in self.properties:
                raise OntologyError(f"属性 '{prop.name}' 已存在")
            self.properties[prop.name] = prop

        elif action == "add_alias":
            target = edit["class_name"]
            if target not in self.classes:
                raise OntologyError(f"类 '{target}' 不存在，无法加别名")
            alias = edit["alias"]
            clash = self.resolve_alias(alias)
            if clash is not None and clash != target:
                raise OntologyError(f"别名 '{alias}' 已被 '{clash}' 占用")
            cls = self.classes[target]
            if alias not in cls.aliases:
                cls.aliases.append(alias)

        elif action == "add_property_alias":
            # 对象属性的别名不是装饰：同一关系的不同措辞（"触发" / "触发风险信号"）
            # 若不归并，抽取器会把它们写成两条不同的边类型，路径搜索就得同时
            # 枚举两种写法才能找全路径，跨文档多跳直接漏边。
            target = edit["property_name"]
            if target not in self.properties:
                raise OntologyError(f"属性 '{target}' 不存在，无法加别名")
            alias = edit["alias"]
            if not alias or alias == target:
                raise OntologyError(f"属性别名非法: {alias!r}")
            existing = self.resolve_property_alias(alias)
            if existing is not None and existing != target:
                raise OntologyError(f"属性别名 '{alias}' 已被 '{existing}' 占用")
            prop = self.properties[target]
            if alias not in prop.aliases:
                prop.aliases.append(alias)

        elif action == "set_parent":
            target = edit["class_name"]
            if target not in self.classes:
                raise OntologyError(f"类 '{target}' 不存在")
            self.classes[target].parent = edit["parent_class"]

        elif action == "add_disjoint":
            a, b = edit["class_a"], edit["class_b"]
            if a == b:
                raise OntologyError("类不能与自己互斥")
            for name in (a, b):
                if name not in self.classes:
                    raise OntologyError(f"类 '{name}' 不存在，无法建立互斥")
                other = b if name == a else a
                if other not in self.classes[name].disjoint_with:
                    self.classes[name].disjoint_with.append(other)

        elif action == "remove_class":
            name = edit["class_name"]
            if name not in self.classes:
                raise OntologyError(f"类 '{name}' 不存在")
            if edit.get("reassign_children_to"):
                new_parent = edit["reassign_children_to"]
                for cls in self.classes.values():
                    if cls.parent == name:
                        cls.parent = new_parent
            else:
                for cls in self.classes.values():
                    if cls.parent == name:
                        raise OntologyError(
                            f"类 '{name}' 仍被 '{cls.name}' 继承，需指定 reassign_children_to"
                        )
            del self.classes[name]

        else:
            raise OntologyError(f"未知的本体编辑动作: {action!r}")

    # ---------- 快照 / 序列化 ----------

    def snapshot(self) -> dict:
        return {
            "version": self.version,
            "classes": {n: c.to_dict() for n, c in self.classes.items()},
            "properties": {n: p.to_dict() for n, p in self.properties.items()},
            "history_len": len(self.history),
        }

    def _restore(self, snapshot: dict) -> None:
        self.version = snapshot["version"]
        self.classes = {n: OwlClass(**d) for n, d in snapshot["classes"].items()}
        self.properties = {n: OwlProperty(**d) for n, d in snapshot["properties"].items()}
        del self.history[snapshot["history_len"] :]

    def restore_from(self, snapshot: dict) -> None:
        """从快照恢复（仲裁回滚用）。"""
        self._restore(snapshot)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "classes": [c.to_dict() for c in self.classes.values()],
            "properties": [p.to_dict() for p in self.properties.values()],
            "history": self.history,
            "provenance": self.provenance,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "Ontology":
        ont = cls(
            version=data.get("version", "0.1.0"),
            classes=[OwlClass(**c) for c in data.get("classes", [])],
            properties=[OwlProperty(**p) for p in data.get("properties", [])],
            provenance=data.get("provenance", {}),
        )
        ont.history = list(data.get("history", []))
        return ont


def _datatype_ok(value: Any, expected: str) -> bool:
    if isinstance(value, list):
        return all(_datatype_ok(v, expected) for v in value)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "date":
        if not isinstance(value, str):
            return False
        import re

        return bool(re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", value))
    return True


def bump_version(version: str, *, kind: str = "minor") -> str:
    """语义化版本递增。主版本留给人工重大重构，演化管道默认走 minor。"""
    try:
        major, minor, patch = (int(x) for x in version.split("."))
    except ValueError as exc:
        raise ValidationError(f"非法版本号: {version!r}") from exc
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor + 1}.0"
