"""交付清单（入口包闭包，只读）。

从一条**已保存审计**中选择一个或多个 **交付入口包**，沿 *使用方 -> 被依赖方*
的依赖边计算随入口交付的包闭包（BFS 可达集合），输出一份交付清单：

* ``items`` —— 闭包内的包，按审计节点声明顺序排列（**输出顺序固定**）；
  同一包被多个入口共享时**只列一次**，但保留**每个入口到它的最短路径**
  （``paths``，BFS 首次到达即最短）与**入口归属**（``reached_by``）；
* ``pending`` —— **待补充项**：入口可达的未解析依赖（源包在闭包内）与
  闭包内的未知许可证包分别单列，并给出 ``complete`` 标记，
  避免清单被误认为完整交付物；
* 依赖环安全：BFS 每个节点只访问一次，必然稳定终止。

清单是纯内存只读计算：不创建审计、不改动复核标记；
原生 manifest 与 CycloneDX 审计结果结构一致，均可直接生成。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional, Set

KIND = "delivery-manifest"

DIRECTION = "依赖边方向：使用方 -> 被依赖方；交付闭包从入口包沿边做 BFS 计算。"

NOTE = (
    "闭包按审计保存的依赖边做 BFS（每个节点只访问一次，依赖环安全终止）；"
    "清单条目按审计节点声明顺序排列，同一包被多个入口共享时只列一次，"
    "但保留每个入口到它的最短路径与入口归属；"
    "入口可达的未解析依赖与闭包内的未知许可证包单列为待补充项，"
    "存在待补充项时 complete=false，清单不能视为完整交付物。"
)


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
        "conflict_count": node.get("conflict_count", 0),
    }
    if extra:
        brief.update(extra)
    return brief


def _adjacency(result: Dict[str, Any]) -> Dict[str, List[str]]:
    adjacency: Dict[str, List[str]] = {}
    for edge in result.get("edges", []) or []:
        adjacency.setdefault(str(edge.get("source")), []).append(str(edge.get("target")))
    return adjacency


def _bfs_parents(adjacency: Dict[str, List[str]], seed: str) -> Dict[str, Optional[str]]:
    """从 seed 沿 使用方 -> 被依赖方 的边做 BFS，返回 {节点: 父节点}（含 seed）。

    BFS 首次到达即最短路径；每个节点只入队一次，依赖环自然终止。
    邻接顺序沿用审计中边的声明顺序，结果确定。
    """
    parents: Dict[str, Optional[str]] = {seed: None}
    queue: deque[str] = deque([seed])
    while queue:
        current = queue.popleft()
        for nxt in adjacency.get(current, []):
            if nxt in parents:
                continue  # 已访问（路径不更长）；环在此终止
            parents[nxt] = current
            queue.append(nxt)
    return parents


def _reconstruct_path(parents: Dict[str, Optional[str]], start: str, end: str) -> List[str]:
    path = [end]
    cur: Optional[str] = end
    while cur != start:
        cur = parents.get(cur)
        if cur is None:
            break
        path.append(cur)
    path.reverse()
    return path


def _duplicates(items: List[str]) -> List[str]:
    seen: Set[str] = set()
    duplicates: List[str] = []
    for item in items:
        if item in seen and item not in duplicates:
            duplicates.append(item)
        seen.add(item)
    return duplicates


def build_delivery(result: Dict[str, Any], entry_ids: List[str]) -> Dict[str, Any]:
    """对一份审计结果生成交付清单（只读，不改写输入）。

    ``entry_ids`` 为空时返回预览（``generated`` 为 False）：全部包节点及
    各自可达包数量，供页面勾选交付入口；否则生成清单。重复选择或选择
    审计中不存在的包会抛出 ``ValueError``（API 层转为 422）。
    """
    nodes = result.get("nodes", []) or []
    node_map = {str(n.get("id")): n for n in nodes}
    order = [str(n.get("id")) for n in nodes]
    position = {nid: index for index, nid in enumerate(order)}
    adjacency = _adjacency(result)

    requested = [str(e).strip() for e in (entry_ids or []) if str(e).strip()]

    report = {
        "kind": KIND,
        "read_only": True,
        "deterministic": True,
        "direction": DIRECTION,
        "method": NOTE,
        "audit": _audit_meta(result),
    }

    if not requested:
        # 预览模式：仅返回可选入口包列表（含各自可达包数量），不生成清单
        return report | {
            "generated": False,
            "packages": [
                _node_brief(
                    node_map[nid],
                    {"reachable": len(_bfs_parents(adjacency, nid))},
                )
                for nid in order
            ],
            "entries": [],
            "summary": None,
            "items": [],
            "pending": None,
        }

    # 校验：重复选择 -> 不存在的包
    duplicates = _duplicates(requested)
    if duplicates:
        raise ValueError(
            "重复选择交付入口包：%s。每个入口包只能选择一次。" % "、".join(duplicates)
        )
    unknown = [eid for eid in requested if eid not in node_map]
    if unknown:
        raise ValueError(
            "审计中不存在以下包：%s。交付清单只能选择该审计已声明的包节点作为入口。"
            % "、".join(unknown)
        )

    entries = list(dict.fromkeys(requested))  # 保持请求顺序
    entry_set = set(entries)

    # 每个入口各自 BFS：可达集合 + 最短路径父指针（环安全）
    parents_by_entry = {entry: _bfs_parents(adjacency, entry) for entry in entries}
    reach_by_entry = {entry: set(parents) for entry, parents in parents_by_entry.items()}

    closure: Set[str] = set()
    for entry in entries:
        closure |= reach_by_entry[entry]

    # 入口归属与最短路径按入口在审计中的声明顺序排列，与请求顺序无关，保证输出固定
    entries_by_decl = sorted(entries, key=lambda e: position[e])

    def reached_by(nid: str) -> List[str]:
        return [e for e in entries_by_decl if nid in reach_by_entry[e]]

    items: List[Dict[str, Any]] = []
    for nid in order:
        if nid not in closure:
            continue
        entries_of = reached_by(nid)
        paths = []
        for entry in entries_of:
            path = _reconstruct_path(parents_by_entry[entry], entry, nid)
            paths.append({"entry": entry, "distance": len(path) - 1, "path": path})
        items.append(
            _node_brief(
                node_map[nid],
                {
                    "entry": nid in entry_set,
                    "shared": len(entries_of) > 1,
                    "reached_by": entries_of,
                    "paths": paths,
                },
            )
        )

    # 待补充项 1：入口可达的未解析依赖（源包在闭包内），保持审计中原顺序
    pending_unresolved = [
        {
            "source": str(item.get("source")),
            "reference": item.get("reference"),
            "reason": item.get("reason"),
        }
        for item in result.get("unresolved", []) or []
        if str(item.get("source")) in closure
    ]
    # 待补充项 2：闭包内许可证未识别的包（按节点声明顺序）
    pending_unknown = [
        _node_brief(node_map[nid], {"reached_by": reached_by(nid)})
        for nid in order
        if nid in closure and not node_map[nid].get("license_known")
    ]
    complete = not pending_unresolved and not pending_unknown

    summary = {
        "entries": len(entries),
        "packages": len(items),
        "entry_packages": sum(1 for item in items if item["entry"]),
        "shared_packages": sum(1 for item in items if item["shared"]),
        "pending_unresolved": len(pending_unresolved),
        "pending_unknown_licenses": len(pending_unknown),
        "pending_total": len(pending_unresolved) + len(pending_unknown),
        "complete": complete,
    }

    note = (
        "清单完整：闭包内没有入口可达的未解析依赖，全部包的许可证均已识别。"
        if complete
        else "清单不完整：存在 %d 条入口可达的未解析依赖、%d 个未知许可证包，"
        "补齐前请勿将本清单视为完整交付物。"
        % (len(pending_unresolved), len(pending_unknown))
    )

    return report | {
        "generated": True,
        "entries": [
            _node_brief(node_map[entry], {"reachable": len(reach_by_entry[entry])})
            for entry in entries
        ],
        "summary": summary,
        "items": items,
        "pending": {
            "complete": complete,
            "note": note,
            "unresolved_dependencies": pending_unresolved,
            "unknown_license_packages": pending_unknown,
        },
    }
