"""交付清单计算（只读）。

从一条**已保存审计**中选择一个或多个**交付入口包**，沿审计保存的
*使用方 -> 被依赖方* 依赖边计算随入口一起交付的包闭包（传递闭包，含入口
自身）。多入口共享同一包时清单只列一次，但保留：

* ``entries`` —— 该包归属的入口（去重，按本次入口选择顺序）；
* ``attributed_via`` —— **每个入口到该包的最短路径**（BFS 首次到达，
  路径确定；未选择的入口不出现在其中）；
* ``distance`` —— 各入口最短路径跳数中的最小值。

BFS 节点只访问一次，遇到依赖环也会稳定终止；邻接顺序沿用审计中边的声明
顺序，故最短路径与整体输出顺序固定可复现。

入口可达但未解析为现有节点的依赖引用，以及闭包内**许可证未知**的包，
单列为 ``pending_unresolved`` / ``pending_unknown_license``（待补充项），
并在顶层给出 ``complete=False``，避免清单被误认为完整交付清单。

整个计算纯内存只读：复用现有审计读取，不创建审计、不改写审计结果与复核
标记；原生 manifest 与 CycloneDX 审计结果结构一致，均可直接使用。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple

KIND = "delivery-manifest"

DIRECTION = "依赖边方向：使用方 -> 被依赖方；包闭包从所选交付入口沿边做 BFS 传递闭包。"

METHOD = (
    "对每个所选交付入口沿 使用方→被依赖方 边做 BFS（节点只访问一次，环安全），"
    "首次到达即该入口到该包的最短路径；合并多入口闭包时同一包只列一次，"
    "保留每个入口到它的最短路径与入口归属；清单按包在审计中的声明顺序固定输出。"
    "入口可达的未解析依赖与闭包内未知许可证包单列为待补充项，清单不声称完整。"
)


# --- 审计元信息 ---------------------------------------------------------------


def _audit_meta(result: Dict[str, Any]) -> Dict[str, Any]:
    summary = result.get("summary") or {}
    return {
        "audit_id": result.get("audit_id"),
        "manifest_name": result.get("manifest_name"),
        "status": result.get("status"),
        "created_at": result.get("created_at"),
        "source_format": result.get("source_format"),
        "packages": summary.get("packages"),
        "edges": summary.get("edges"),
        "conflicts": summary.get("conflicts"),
        "license_warnings": summary.get("license_warnings"),
        "unresolved": summary.get("unresolved"),
    }


def _node_brief(node: Dict[str, Any], extra: Dict[str, Any] = None) -> Dict[str, Any]:
    brief = {
        "id": node.get("id"),
        "name": node.get("name"),
        "version": node.get("version"),
        "version_unspecified": node.get("version_unspecified", False),
        "license_raw": node.get("license_raw"),
        "license_display": node.get("license_display"),
        "license": node.get("license"),
        "license_known": node.get("license_known"),
        "license_category": node.get("license_category"),
        "license_multi": node.get("license_multi", False),
        "license_candidates": node.get("license_candidates", []),
        "license_tokens": node.get("license_tokens", []),
        "conflict_count": node.get("conflict_count", 0),
    }
    if extra:
        brief.update(extra)
    return brief


# --- 图读取 -------------------------------------------------------------------


def _edge_pairs(result: Dict[str, Any]) -> List[Tuple[str, str]]:
    """按审计中保存的顺序返回去重后的 (source, target) 边（保证结果确定）。"""
    pairs: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for edge in result.get("edges", []) or []:
        pair = (str(edge.get("source")), str(edge.get("target")))
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


def _adjacency(pairs: List[Tuple[str, str]]) -> Dict[str, List[str]]:
    adjacency: Dict[str, List[str]] = {}
    for source, target in pairs:
        adjacency.setdefault(source, []).append(target)
        adjacency.setdefault(target, [])
    return adjacency


def _bfs(
    adjacency: Dict[str, List[str]],
    start: str,
    node_order: List[str],
) -> Dict[str, List[str]]:
    """从 start 沿 使用方→被依赖方 边做 BFS，返回 {可达节点: 最短路径}（含 start）。

    邻接顺序沿用审计中边的声明顺序，BFS 唯一前驱使路径确定（环安全）。
    """
    parents: Dict[str, Optional[str]] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        for nxt in adjacency.get(current, []):
            if nxt in parents:
                continue  # BFS 已找到更短路径；同时天然处理依赖环
            parents[nxt] = current
            queue.append(nxt)

    paths: Dict[str, List[str]] = {}
    for nid in node_order:
        if nid not in parents:
            continue
        path = [nid]
        cur: Optional[str] = nid
        while cur != start:
            cur = parents.get(cur)
            if cur is None:
                break
            path.append(cur)
        path.reverse()
        paths[nid] = path
    return paths


def _duplicates(items: List[str]) -> List[str]:
    seen: Set[str] = set()
    duplicates: List[str] = []
    for item in items:
        if item in seen and item not in duplicates:
            duplicates.append(item)
        seen.add(item)
    return duplicates


def _resolve_entry(result: Dict[str, Any], entry: str) -> str:
    """把用户输入解析为审计中的节点 id：精确 id -> 大小写不敏感 id -> 唯一包名。

    找不到或包名对应多个版本时抛出 ``ValueError``（API 层转为 422）。
    """
    nodes = result.get("nodes", []) or []
    text = entry.strip()
    lowered = text.lower()
    for node in nodes:
        if str(node.get("id")) == text:
            return str(node.get("id"))
    for node in nodes:
        if str(node.get("id")).lower() == lowered:
            return str(node.get("id"))
    matches = [n for n in nodes if str(n.get("name") or "").lower() == lowered]
    if len(matches) == 1:
        return str(matches[0].get("id"))
    if len(matches) > 1:
        raise ValueError(
            "入口包名 %r 在该审计中对应多个版本：%s，请用 name@version 指定交付入口。"
            % (text, "、".join(sorted(str(n.get("id")) for n in matches)))
        )
    raise ValueError(
        "审计中不存在交付入口包：%s。交付清单只能选择该审计已声明的包节点。" % text
    )


# --- 主入口 ------------------------------------------------------------------


def list_entries(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """预览：按节点声明顺序返回可作为交付入口的全部包（含各自可达包数量）。"""
    nodes = result.get("nodes", []) or []
    node_order = [str(n.get("id")) for n in nodes]
    adjacency = _adjacency(_edge_pairs(result))
    entries: List[Dict[str, Any]] = []
    for node in nodes:
        nid = str(node.get("id"))
        reachable = _bfs(adjacency, nid, node_order)
        entries.append(
            _node_brief(node, {"is_entry": True, "reachable": len(reachable)})
        )
    return entries


def build_manifest(result: Dict[str, Any], entry_ids: List[str]) -> Dict[str, Any]:
    """对一份审计结果按所选入口计算交付清单，返回报告（纯只读，不改写输入）。

    ``entry_ids`` 为空时返回预览（仅可选入口列表，``generated`` 为 False），
    供页面列出可勾选的交付入口；否则执行闭包计算。重复选择、不存在的包
    （或包名对应多个版本）都会抛出 ``ValueError``（API 层转为 422）。
    """
    nodes = result.get("nodes", []) or []
    node_map = {str(n.get("id")): n for n in nodes}
    node_order = [str(n.get("id")) for n in nodes]
    adjacency = _adjacency(_edge_pairs(result))

    report: Dict[str, Any] = {
        "kind": KIND,
        "read_only": True,
        "deterministic": True,
        "direction": DIRECTION,
        "method": METHOD,
        "audit": _audit_meta(result),
        "entries": [],
    }

    requested = [str(e).strip() for e in (entry_ids or []) if str(e).strip()]
    if not requested:
        # 预览模式：仅返回可选入口列表，不计算清单
        return report | {
            "generated": False,
            "selected_entries": [],
            "summary": None,
            "complete": None,
            "packages": [],
            "pending_unresolved": [],
            "pending_unknown_license": [],
            "entry_candidates": list_entries(result),
        }

    # 校验：重复选择 -> 不存在/歧义的包
    duplicates = _duplicates(requested)
    if duplicates:
        raise ValueError(
            "重复选择交付入口包：%s。每个入口只能选择一次。" % "、".join(duplicates)
        )
    selected = [_resolve_entry(result, e) for e in requested]

    # 每个入口各自做 BFS（含入口自身）；同一包被多个入口共享时分别保留最短路径
    paths_by_entry: Dict[str, Dict[str, List[str]]] = {
        entry: _bfs(adjacency, entry, node_order) for entry in selected
    }

    # 合并闭包：同一包只列一次
    closure: Set[str] = set()
    for paths in paths_by_entry.values():
        closure.update(paths.keys())

    packages: List[Dict[str, Any]] = []
    unknown_count = 0
    selected_set = set(selected)
    for nid in node_order:  # 固定输出顺序：审计中的节点声明顺序
        if nid not in closure:
            continue
        node = node_map[nid]
        attributions: List[Dict[str, Any]] = []
        for entry in selected:  # 归属顺序：本次入口选择顺序
            path = paths_by_entry[entry].get(nid)
            if path is None:
                continue
            attributions.append(
                {"entry": entry, "distance": len(path) - 1, "path": path}
            )
        is_entry = nid in selected_set
        is_unknown = node.get("license_known") is False
        if is_unknown:
            unknown_count += 1
        packages.append(
            _node_brief(
                node,
                {
                    "is_entry": is_entry,
                    "entries": [a["entry"] for a in attributions],
                    "distance": min(a["distance"] for a in attributions),
                    "attributed_via": attributions,
                },
            )
        )

    # 入口可达的未解析依赖：源包在闭包内即随入口交付暴露（引用本身单列，
    # 不凭空造节点）；同一 (source, reference) 只列一次，但保留可达它的入口。
    pending_unresolved: List[Dict[str, Any]] = []
    seen_unresolved: Set[Tuple[str, str]] = set()
    unresolved_items = result.get("unresolved", []) or []
    # 固定顺序：审计 unresolved 列表顺序
    for item in unresolved_items:
        source = str(item.get("source"))
        reference = str(item.get("reference"))
        key = (source, reference)
        if key in seen_unresolved:
            continue
        via_entries = [
            entry
            for entry in selected
            if source in paths_by_entry[entry]
        ]
        if not via_entries:
            continue
        seen_unresolved.add(key)
        paths = [
            {"entry": entry, "path": paths_by_entry[entry][source]}
            for entry in selected
            if entry in via_entries
        ]
        pending_unresolved.append(
            {
                "kind": "unresolved-dependency",
                "source": source,
                "reference": reference,
                "reason": item.get("reason"),
                "entries": via_entries,
                "reachable_via": paths,
            }
        )

    pending_unknown = [
        pkg["id"]
        for pkg in packages
        if pkg.get("license_known") is False
    ]

    entry_briefs = [
        _node_brief(
            node_map[entry],
            {
                "is_entry": True,
                "reachable": len(paths_by_entry[entry]),
            },
        )
        for entry in selected
    ]

    shared_count = sum(1 for pkg in packages if len(pkg["entries"]) > 1)
    entry_only = sum(1 for pkg in packages if pkg["is_entry"])
    complete = not pending_unresolved and unknown_count == 0
    summary = {
        "entries": len(selected),
        "packages": len(packages),
        "entry_packages": entry_only,
        "transitive_packages": len(packages) - entry_only,
        "shared_packages": shared_count,
        "unknown_license_packages": unknown_count,
        "pending_unresolved": len(pending_unresolved),
        "pending_items": len(pending_unresolved) + unknown_count,
        "complete": complete,
    }

    return report | {
        "generated": True,
        "selected_entries": list(selected),
        "complete": complete,
        "summary": summary,
        "packages": packages,
        "pending_unresolved": pending_unresolved,
        "pending_unknown_license": [
            pkg for pkg in packages if pkg.get("license_known") is False
        ],
        "pending_unknown_license_ids": pending_unknown,
        "entry_candidates": list_entries(result),
    }
