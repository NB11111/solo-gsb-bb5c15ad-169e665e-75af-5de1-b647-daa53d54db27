"""交付清单计算的自测（无测试框架依赖）：python -m tests.test_delivery

使用独立临时 SQLite 库验证只读性，不触碰 ./data；
核心逻辑只依赖 analyzer/cyclonedx/delivery，无需安装 fastapi 即可运行。
"""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path

from app import analyzer, cyclonedx, database, delivery

# 隔离的临时数据库（仅用于只读性验证）
_TMP_DIR = tempfile.mkdtemp(prefix="delivery-test-")
database.DB_PATH = Path(_TMP_DIR) / "test.db"
database.init_db()


def _check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError("%s %s" % (name, detail))
    print("ok -", name)


def _expect_error(name: str, fn, needle: str) -> None:
    try:
        fn()
    except ValueError as exc:
        _check(name, needle in str(exc), "message=%r" % str(exc))
        return
    raise AssertionError("%s 未抛出 ValueError" % name)


def _demo_manifest():
    # 入口 app / cli；share 被两个入口共享（cli→share 直达 1 跳，
    # app→lib→share 2 跳）；deep 经 share 传递可达；
    # mystery 许可证未知（OFL-1.1）；app 有未解析引用 ghost；
    # cyc1/cyc2 构成依赖环（环安全 + 最短路径稳定）。
    return {
        "name": "delivery-demo",
        "packages": [
            {"name": "app", "version": "1", "license": "MIT",
             "dependencies": ["lib@1", "ghost"]},
            {"name": "cli", "version": "1", "license": "MIT",
             "dependencies": ["share@1", "cyc1@1"]},
            {"name": "lib", "version": "1", "license": "Apache-2.0",
             "dependencies": ["share@1"]},
            {"name": "share", "version": "1", "license": "MIT",
             "dependencies": ["deep@1", "mystery@1"]},
            {"name": "deep", "version": "1", "license": "ISC"},
            {"name": "mystery", "version": "1", "license": "OFL-1.1"},
            {"name": "cyc1", "version": "1", "license": "MIT",
             "dependencies": ["cyc2@1"]},
            {"name": "cyc2", "version": "1", "license": "MIT",
             "dependencies": ["cyc1@1"]},
        ],
    }


def _demo_result():
    return analyzer.analyze_manifest(_demo_manifest())


def test_preview_lists_all_entries() -> None:
    result = _demo_result()
    preview = delivery.build_manifest(result, [])
    _check("preview not generated", preview["generated"] is False)
    _check("preview has no summary", preview["summary"] is None)
    ids = [e["id"] for e in preview["entry_candidates"]]
    _check("preview lists every node as candidate", ids == [
        "app@1", "cli@1", "lib@1", "share@1", "deep@1",
        "mystery@1", "cyc1@1", "cyc2@1",
    ], str(ids))
    app_entry = preview["entry_candidates"][0]
    _check("candidate reachable count", app_entry["reachable"] == 5, str(app_entry))
    # list_entries 直接调用结果一致
    _check("list_entries same order", [e["id"] for e in delivery.list_entries(result)] == ids)


def test_single_entry_closure() -> None:
    result = _demo_result()
    report = delivery.build_manifest(result, ["app@1"])
    _check("generated", report["generated"] is True)
    _check("kind", report["kind"] == "delivery-manifest")
    _check("read_only", report["read_only"] is True)
    pkg_ids = [p["id"] for p in report["packages"]]
    _check("closure packages", pkg_ids == ["app@1", "lib@1", "share@1", "deep@1", "mystery@1"],
           str(pkg_ids))
    app_pkg = report["packages"][0]
    _check("entry marked", app_pkg["is_entry"] is True and app_pkg["distance"] == 0)
    share = next(p for p in report["packages"] if p["id"] == "share@1")
    _check("share shortest path from app", share["distance"] == 2
           and share["attributed_via"] == [
               {"entry": "app@1", "distance": 2,
                "path": ["app@1", "lib@1", "share@1"]}],
           str(share["attributed_via"]))
    _check("closure not complete (unknown + unresolved)", report["complete"] is False)
    _check("pending unknown ids", report["pending_unknown_license_ids"] == ["mystery@1"])
    pending = report["pending_unresolved"]
    _check("pending unresolved", [(u["source"], u["reference"]) for u in pending]
           == [("app@1", "ghost")], str(pending))
    _check("pending unresolved path", pending[0]["reachable_via"] == [
        {"entry": "app@1", "path": ["app@1"]}], str(pending[0]))
    s = report["summary"]
    _check("summary counts",
           (s["entries"], s["packages"], s["entry_packages"], s["transitive_packages"],
            s["shared_packages"], s["unknown_license_packages"],
            s["pending_unresolved"], s["complete"])
           == (1, 5, 1, 4, 0, 1, 1, False), str(s))


def test_multiple_entries_shared_once_with_attributions() -> None:
    result = _demo_result()
    report = delivery.build_manifest(result, ["app@1", "cli@1"])
    pkg_ids = [p["id"] for p in report["packages"]]
    # 合并闭包；cyc 环可达；顺序固定（审计节点声明顺序）
    _check("union closure order", pkg_ids == [
        "app@1", "cli@1", "lib@1", "share@1", "deep@1",
        "mystery@1", "cyc1@1", "cyc2@1",
    ], str(pkg_ids))
    share = next(p for p in report["packages"] if p["id"] == "share@1")
    _check("shared package listed once", pkg_ids.count("share@1") == 1)
    _check("shared entries attribution order", share["entries"] == ["app@1", "cli@1"])
    _check("per-entry shortest paths kept", share["attributed_via"] == [
        {"entry": "app@1", "distance": 2, "path": ["app@1", "lib@1", "share@1"]},
        {"entry": "cli@1", "distance": 1, "path": ["cli@1", "share@1"]},
    ], str(share["attributed_via"]))
    _check("distance is min across entries", share["distance"] == 1)
    # 环：稳定终止 + 最短路径（cli→cyc1 1 跳；cyc2 经 cyc1 2 跳）
    cyc1 = next(p for p in report["packages"] if p["id"] == "cyc1@1")
    cyc2 = next(p for p in report["packages"] if p["id"] == "cyc2@1")
    _check("cycle cyc1 distance", cyc1["distance"] == 1, str(cyc1["attributed_via"]))
    _check("cycle cyc2 shortest path", cyc2["attributed_via"] == [
        {"entry": "cli@1", "distance": 2, "path": ["cli@1", "cyc1@1", "cyc2@1"]}],
        str(cyc2["attributed_via"]))
    _check("shared count", report["summary"]["shared_packages"] == 3)  # share/deep/mystery
    # 未解析项只归属可达它的入口（ghost 仅 app）
    ghost = report["pending_unresolved"][0]
    _check("unresolved attributed only to reachable entry", ghost["entries"] == ["app@1"])


def test_entry_selection_order_stable_and_symmetric_paths() -> None:
    result = _demo_result()
    r1 = delivery.build_manifest(result, ["app@1", "cli@1"])
    r2 = delivery.build_manifest(result, ["cli@1", "app@1"])
    # 包清单顺序不随入口顺序变化（按审计节点声明顺序）
    _check("package order stable regardless of entry order",
           [p["id"] for p in r1["packages"]] == [p["id"] for p in r2["packages"]])
    share1 = next(p for p in r1["packages"] if p["id"] == "share@1")
    share2 = next(p for p in r2["packages"] if p["id"] == "share@1")
    # 归属顺序跟随入口选择顺序；路径内容不变
    _check("attribution order follows selection",
           [a["entry"] for a in share1["attributed_via"]] == ["app@1", "cli@1"]
           and [a["entry"] for a in share2["attributed_via"]] == ["cli@1", "app@1"])
    _check("shortest paths identical across selections",
           sorted((a["entry"], tuple(a["path"])) for a in share1["attributed_via"])
           == sorted((a["entry"], tuple(a["path"])) for a in share2["attributed_via"]))
    _check("selected_entries echoed in order",
           r1["selected_entries"] == ["app@1", "cli@1"]
           and r2["selected_entries"] == ["cli@1", "app@1"])
    # 重复两次运行输出完全一致（确定性）
    r3 = delivery.build_manifest(result, ["app@1", "cli@1"])
    _check("deterministic output", r1 == r3)


def test_complete_when_no_unknown_no_unresolved() -> None:
    manifest = {
        "name": "clean-delivery",
        "packages": [
            {"name": "app", "version": "1", "license": "MIT", "dependencies": ["lib@1"]},
            {"name": "lib", "version": "1", "license": "MIT"},
        ],
    }
    result = analyzer.analyze_manifest(manifest)
    report = delivery.build_manifest(result, ["app@1"])
    _check("complete manifest", report["complete"] is True)
    _check("no pending", report["summary"]["pending_items"] == 0)
    _check("unknown list empty", report["pending_unknown_license"] == [])
    # 入口自身单独选择
    only = delivery.build_manifest(result, ["lib@1"])
    _check("leaf closure is itself", [p["id"] for p in only["packages"]] == ["lib@1"])
    _check("leaf complete", only["complete"] is True)


def test_unreachable_unresolved_excluded() -> None:
    # ghost 仅 app 引用：从 lib 出发的清单不应列入
    result = _demo_result()
    report = delivery.build_manifest(result, ["lib@1"])
    _check("unresolved not reachable excluded", report["pending_unresolved"] == [])
    _check("closure from lib", [p["id"] for p in report["packages"]]
           == ["lib@1", "share@1", "deep@1", "mystery@1"])
    # mystery 仍未知 → 不完整
    _check("unknown still pending", report["complete"] is False
           and report["summary"]["unknown_license_packages"] == 1)


def test_validation_errors() -> None:
    result = _demo_result()
    _expect_error("duplicate entries",
                  lambda: delivery.build_manifest(result, ["app@1", "app@1"]),
                  "重复选择交付入口包")
    _expect_error("unknown entry",
                  lambda: delivery.build_manifest(result, ["ghost@9"]),
                  "审计中不存在交付入口包")
    amb = analyzer.analyze_manifest({
        "packages": [
            {"name": "u", "version": "1", "license": "MIT"},
            {"name": "u", "version": "2", "license": "MIT"},
        ],
    })
    _expect_error("ambiguous bare name",
                  lambda: delivery.build_manifest(amb, ["u"]),
                  "对应多个版本")
    # 唯一包名可只给包名
    resolved = delivery.build_manifest(result, ["deep"])
    _check("bare name resolved", resolved["selected_entries"] == ["deep@1"])


def test_cyclonedx_audit_supported() -> None:
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "components": [
            {"type": "application", "bom-ref": "pkg:app@1", "name": "app", "version": "1",
             "licenses": [{"license": {"id": "MIT"}}]},
            {"type": "library", "bom-ref": "pkg:lib@1", "name": "lib", "version": "1",
             "licenses": [{"license": {"id": "MIT"}}]},
        ],
        "dependencies": [
            {"ref": "pkg:app@1", "dependsOn": ["pkg:lib@1"]},
        ],
    }
    result = cyclonedx.analyze_bom(bom, "cdx-delivery")
    report = delivery.build_manifest(result, ["app@1"])
    _check("cyclonedx closure", [p["id"] for p in report["packages"]] == ["app@1", "lib@1"])
    _check("cyclonedx complete", report["complete"] is True)
    _check("cyclonedx source format meta", report["audit"].get("source_format") == "cyclonedx")


def test_build_is_read_only() -> None:
    manifest = _demo_manifest()
    result = analyzer.analyze_manifest(manifest)
    snapshot = copy.deepcopy(result)
    report = delivery.build_manifest(result, ["app@1", "cli@1"])
    _check("input result not mutated", result == snapshot)
    _check("report produced", report["generated"] is True)


def test_persisted_audit_read_only() -> None:
    manifest = _demo_manifest()
    result = analyzer.analyze_manifest(manifest)
    audit_id = database.save_audit(result, manifest)

    stored_before = database.get_audit(audit_id)
    audits_before = database.list_audits(limit=500)
    reviews_before = database.list_reviews(limit=500)
    snapshot = copy.deepcopy(stored_before)

    delivery.build_manifest(stored_before, ["app@1"])
    _check("no new audit created", database.list_audits(limit=500) == audits_before)
    _check("stored audit unchanged", database.get_audit(audit_id) == snapshot)
    _check("reviews untouched", database.list_reviews(limit=500) == reviews_before)


def main() -> None:
    test_preview_lists_all_entries()
    test_single_entry_closure()
    test_multiple_entries_shared_once_with_attributions()
    test_entry_selection_order_stable_and_symmetric_paths()
    test_complete_when_no_unknown_no_unresolved()
    test_unreachable_unresolved_excluded()
    test_validation_errors()
    test_cyclonedx_audit_supported()
    test_build_is_read_only()
    test_persisted_audit_read_only()
    print("\nall delivery tests passed.")


if __name__ == "__main__":
    main()
