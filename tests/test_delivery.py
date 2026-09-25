"""交付清单（入口包闭包）的自测（无测试框架依赖）：python -m tests.test_delivery

使用独立临时 SQLite 库验证只读性，不触碰 ./data；
核心逻辑只依赖 analyzer/cyclonedx/delivery，无需安装 fastapi 即可运行。
"""

from __future__ import annotations

import copy
import json
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


def _demo_manifest():
    # app / cli / island 为根包（无入边）。
    # app -> shared / app-only / cyc-a / ghost@9（未解析）；cli -> shared / mid / unknown-lic；
    # shared -> deep；mid -> shared；cyc-a <-> cyc-b 构成依赖环，cyc-b -> deep；
    # island -> unknown-lic / lost-ref（未解析），island 不被选为入口时其未解析不计入。
    return {
        "name": "delivery-demo",
        "packages": [
            {"name": "app", "version": "1", "license": "GPL-3.0",
             "dependencies": ["shared@1", "app-only@1", "cyc-a@1", "ghost@9"]},
            {"name": "cli", "version": "1", "license": "MIT",
             "dependencies": ["shared@1", "mid@1", "unknown-lic@1"]},
            {"name": "shared", "version": "1", "license": "Apache-2.0",
             "dependencies": ["deep@1"]},
            {"name": "deep", "version": "1", "license": "MIT"},
            {"name": "app-only", "version": "1", "license": "MIT"},
            {"name": "mid", "version": "1", "license": "MIT",
             "dependencies": ["shared@1"]},
            {"name": "cyc-a", "version": "1", "license": "MIT",
             "dependencies": ["cyc-b@1"]},
            {"name": "cyc-b", "version": "1", "license": "MIT",
             "dependencies": ["cyc-a@1", "deep@1"]},
            {"name": "unknown-lic", "version": "1", "license": "OFL-1.1"},
            {"name": "island", "version": "1", "license": "MIT",
             "dependencies": ["unknown-lic@1", "lost-ref"]},
        ],
    }


def _demo_result():
    return analyzer.analyze_manifest(_demo_manifest())


def _expect_error(name: str, fn, needle: str) -> None:
    try:
        fn()
    except ValueError as exc:
        _check(name, needle in str(exc), "message=%r" % str(exc))
        return
    raise AssertionError("%s 未抛出 ValueError" % name)


def test_preview_lists_packages_only() -> None:
    result = _demo_result()
    report = delivery.build_delivery(result, [])
    _check("preview not generated", report["generated"] is False)
    _check("preview read only", report["read_only"] is True)
    _check("preview deterministic", report["deterministic"] is True)
    _check("preview summary empty", report["summary"] is None)
    _check("preview pending empty", report["pending"] is None)
    _check("preview no items", report["items"] == [])
    ids = [p["id"] for p in report["packages"]]
    _check(
        "preview lists all packages in declaration order",
        ids == ["app@1", "cli@1", "shared@1", "deep@1", "app-only@1", "mid@1",
                "cyc-a@1", "cyc-b@1", "unknown-lic@1", "island@1"],
        str(ids),
    )
    brief = {p["id"]: p for p in report["packages"]}
    # app 可达：app, shared, app-only, cyc-a, cyc-b, deep（环安全终止）
    _check("app reachable count", brief["app@1"]["reachable"] == 6, str(brief["app@1"]))
    _check("island reachable count", brief["island@1"]["reachable"] == 2)
    _check("preview brief carries license", brief["app@1"]["license_display"] == "GPL-3.0")


def test_single_entry_closure() -> None:
    result = _demo_result()
    report = delivery.build_delivery(result, ["app@1"])
    _check("generated flag", report["generated"] is True)
    _check("entries echoed", [e["id"] for e in report["entries"]] == ["app@1"])
    _check("entry reachable count", report["entries"][0]["reachable"] == 6)
    ids = [item["id"] for item in report["items"]]
    _check(
        "closure follows declaration order",
        ids == ["app@1", "shared@1", "deep@1", "app-only@1", "cyc-a@1", "cyc-b@1"],
        str(ids),
    )
    summary = report["summary"]
    _check("summary packages", summary["packages"] == 6, str(summary))
    _check("summary entries", summary["entries"] == 1)
    _check("summary entry packages", summary["entry_packages"] == 1)
    _check("summary shared packages", summary["shared_packages"] == 0)
    item_map = {item["id"]: item for item in report["items"]}
    _check("entry flagged", item_map["app@1"]["entry"] is True)
    _check("non-entry not flagged", item_map["deep@1"]["entry"] is False)
    # 入口到自身的路径：距离 0
    self_path = item_map["app@1"]["paths"][0]
    _check("entry self path", self_path["path"] == ["app@1"] and self_path["distance"] == 0)
    # deep 的最短路径：app -> shared -> deep（2 跳），而不是经依赖环的 3 跳
    deep_path = item_map["deep@1"]["paths"][0]
    _check(
        "shortest path avoids cycle",
        deep_path["path"] == ["app@1", "shared@1", "deep@1"] and deep_path["distance"] == 2,
        str(deep_path),
    )
    # 依赖环上的包：cyc-b 到 cyc-a 的最短路径不绕圈
    cyc_path = item_map["cyc-a@1"]["paths"][0]
    _check("cycle path direct", cyc_path["path"] == ["app@1", "cyc-a@1"], str(cyc_path))
    # 待补充项：app 的未解析依赖 ghost@9 在闭包内可达
    pending = report["pending"]
    _check("pending unresolved", [u["reference"] for u in pending["unresolved_dependencies"]] == ["ghost@9"],
           str(pending["unresolved_dependencies"]))
    _check("pending no unknown license", pending["unknown_license_packages"] == [])
    _check("manifest incomplete", pending["complete"] is False and summary["complete"] is False)
    _check("pending total", summary["pending_total"] == 1, str(summary))


def test_multi_entry_shared_once_with_attribution() -> None:
    result = _demo_result()
    report = delivery.build_delivery(result, ["app@1", "cli@1"])
    ids = [item["id"] for item in report["items"]]
    _check(
        "closure union in declaration order",
        ids == ["app@1", "cli@1", "shared@1", "deep@1", "app-only@1", "mid@1",
                "cyc-a@1", "cyc-b@1", "unknown-lic@1"],
        str(ids),
    )
    # 共享包只列一次
    _check("shared listed once", ids.count("shared@1") == 1 and ids.count("deep@1") == 1)
    summary = report["summary"]
    _check("shared count", summary["shared_packages"] == 2, str(summary))
    _check("total packages", summary["packages"] == 9)
    item_map = {item["id"]: item for item in report["items"]}
    shared = item_map["shared@1"]
    _check("shared flagged", shared["shared"] is True)
    # 入口归属：两个入口都列出，按声明顺序（app 先于 cli），与请求顺序无关
    _check("shared attribution", shared["reached_by"] == ["app@1", "cli@1"], str(shared["reached_by"]))
    # 每个入口各保留一条最短路径
    paths = {p["entry"]: p for p in shared["paths"]}
    _check("shared path from app", paths["app@1"]["path"] == ["app@1", "shared@1"])
    _check("shared path from cli", paths["cli@1"]["path"] == ["cli@1", "shared@1"])
    deep = item_map["deep@1"]
    deep_paths = {p["entry"]: p for p in deep["paths"]}
    _check("deep shortest via app", deep_paths["app@1"]["path"] == ["app@1", "shared@1", "deep@1"])
    _check("deep shortest via cli", deep_paths["cli@1"]["path"] == ["cli@1", "shared@1", "deep@1"])
    # 未知许可证包在闭包内 -> 待补充项
    pending = report["pending"]
    _check(
        "unknown license pending",
        [n["id"] for n in pending["unknown_license_packages"]] == ["unknown-lic@1"],
        str(pending["unknown_license_packages"]),
    )
    _check("unknown license attribution",
           pending["unknown_license_packages"][0]["reached_by"] == ["cli@1"])
    _check("unresolved still pending",
           [u["source"] for u in pending["unresolved_dependencies"]] == ["app@1"])
    _check("incomplete with both kinds", summary["complete"] is False
           and summary["pending_total"] == 2, str(summary))
    # island 未被选为入口：其未解析依赖 lost-ref 不计入待补充项
    refs = [u["reference"] for u in pending["unresolved_dependencies"]]
    _check("unreachable unresolved excluded", "lost-ref" not in refs, str(refs))


def test_complete_manifest() -> None:
    result = _demo_result()
    report = delivery.build_delivery(result, ["mid@1"])
    ids = [item["id"] for item in report["items"]]
    _check("mid closure", ids == ["shared@1", "deep@1", "mid@1"], str(ids))
    summary = report["summary"]
    _check("complete flag", summary["complete"] is True, str(summary))
    _check("no pending", summary["pending_total"] == 0)
    _check("pending note says complete", "完整" in report["pending"]["note"])
    _check("pending lists empty",
           report["pending"]["unresolved_dependencies"] == []
           and report["pending"]["unknown_license_packages"] == [])


def test_deterministic_output() -> None:
    result = _demo_result()
    first = delivery.build_delivery(result, ["app@1", "cli@1"])
    second = delivery.build_delivery(result, ["app@1", "cli@1"])
    _check(
        "same input same output",
        json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True),
    )
    # 入口请求顺序不同：清单条目与归属不变（按声明顺序固定）
    swapped = delivery.build_delivery(result, ["cli@1", "app@1"])
    _check("items independent of request order", swapped["items"] == first["items"])
    _check("pending independent of request order", swapped["pending"] == first["pending"])
    _check("summary independent of request order", swapped["summary"] == first["summary"])
    _check("entries echo request order",
           [e["id"] for e in swapped["entries"]] == ["cli@1", "app@1"])


def test_pure_cycle_terminates() -> None:
    # 纯环图：没有根包也能选入口，BFS 必须稳定终止且只列一次
    result = analyzer.analyze_manifest(
        {"packages": [
            {"name": "a", "version": "1", "license": "MIT", "dependencies": ["b@1"]},
            {"name": "b", "version": "1", "license": "MIT", "dependencies": ["a@1"]},
        ]}
    )
    report = delivery.build_delivery(result, ["a@1"])
    ids = [item["id"] for item in report["items"]]
    _check("pure cycle closure", ids == ["a@1", "b@1"], str(ids))
    item_map = {item["id"]: item for item in report["items"]}
    _check("cycle shortest path", item_map["b@1"]["paths"][0]["path"] == ["a@1", "b@1"])
    _check("pure cycle complete", report["summary"]["complete"] is True)


def test_validation_errors() -> None:
    result = _demo_result()
    _expect_error(
        "duplicate entry rejected",
        lambda: delivery.build_delivery(result, ["app@1", "app@1"]),
        "重复选择交付入口包：app@1",
    )
    _expect_error(
        "unknown package rejected",
        lambda: delivery.build_delivery(result, ["ghost@9"]),
        "审计中不存在以下包：ghost@9",
    )
    # 校验优先级：重复先于不存在
    _expect_error(
        "duplicate checked first",
        lambda: delivery.build_delivery(result, ["ghost@9", "ghost@9"]),
        "重复选择",
    )


def test_cyclonedx_audit_supported() -> None:
    bom = {
        "bomFormat": "CycloneDX",
        "components": [
            {"bom-ref": "app", "name": "app", "version": "1",
             "licenses": [{"license": {"id": "GPL-3.0"}}]},
            {"bom-ref": "lib", "name": "lib", "version": "1",
             "licenses": [{"license": {"id": "Proprietary"}}]},
            {"bom-ref": "other", "name": "other", "version": "1",
             "licenses": [{"license": {"id": "MIT"}}]},
        ],
        "dependencies": [
            {"ref": "app", "dependsOn": ["lib"]},
            {"ref": "other", "dependsOn": ["lib"]},
        ],
    }
    result = cyclonedx.analyze_bom(bom)
    report = delivery.build_delivery(result, ["app@1", "other@1"])
    ids = [item["id"] for item in report["items"]]
    _check("cyclonedx closure", ids == ["app@1", "lib@1", "other@1"], str(ids))
    item_map = {item["id"]: item for item in report["items"]}
    _check("cyclonedx shared once", ids.count("lib@1") == 1)
    _check("cyclonedx attribution", item_map["lib@1"]["reached_by"] == ["app@1", "other@1"])
    _check("cyclonedx complete", report["summary"]["complete"] is True)


def test_build_is_read_only() -> None:
    manifest = _demo_manifest()
    result = analyzer.analyze_manifest(manifest)
    audit_id = database.save_audit(result, manifest)

    stored_before = database.get_audit(audit_id)
    audits_before = database.list_audits(limit=500)
    reviews_before = database.list_reviews(limit=500)

    snapshot = copy.deepcopy(stored_before)
    report = delivery.build_delivery(stored_before, ["app@1", "cli@1"])
    _check("report produced", report["generated"] is True)
    _check("input result not mutated", stored_before == snapshot)
    _check("no new audit created", database.list_audits(limit=500) == audits_before)
    _check("stored audit unchanged", database.get_audit(audit_id) == snapshot)
    _check("reviews untouched", database.list_reviews(limit=500) == reviews_before)
    _check("missing audit stays missing", database.get_audit(999999) is None)


def main() -> None:
    test_preview_lists_packages_only()
    test_single_entry_closure()
    test_multi_entry_shared_once_with_attribution()
    test_complete_manifest()
    test_deterministic_output()
    test_pure_cycle_terminates()
    test_validation_errors()
    test_cyclonedx_audit_supported()
    test_build_is_read_only()
    print("\nall delivery tests passed.")


if __name__ == "__main__":
    main()
