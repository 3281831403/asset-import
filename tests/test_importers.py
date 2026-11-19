"""assetpipe.importers 的验收规格（15 个测试，初始全红）。

这些测试就是题目本身，不可修改：每条测试都直接调用 assetpipe/importers.py 里的桩函数，
当前 8 个函数都只有 raise NotImplementedError，所以 15 条全部 failed。
契约细节见各函数的 docstring。

约定：
* 文件树一律用内联 dict ``{相对路径: 内容}`` 表达，只有 import_batch 落到 ``tmp_path``；
* 不使用随机数、不联网、不依赖真实仓库内容。
"""

import pytest

from assetpipe.importers import (
    AssetError,
    cache_key,
    check_missing_refs,
    convert_axes,
    convert_units,
    dedupe_materials,
    import_batch,
    normalize_path,
    resolve_dependencies,
)

# --------------------------------------------------------------------------
# 缺陷 1：路径归一化只做 replace("..", "")，..;/ 与多重编码照样越界
# --------------------------------------------------------------------------


def test_normalize_path_rejects_traversal_variants():
    # 正常相对路径：统一分隔符、丢掉 "."、消解内部 ".."
    assert normalize_path("./tex/./a.png") == "tex/a.png"
    assert normalize_path("tex\\sub\\a.png") == "tex/sub/a.png"
    assert normalize_path("tex/../models/a.glb") == "models/a.glb"
    # NFC：e + 组合重音 与 预组合字符必须同一个键
    assert normalize_path("cafe\u0301/a.png") == "caf\u00e9/a.png"
    # 合法文件名里的连续点不许被吃掉（replace("..", "") 会把它改成 assets/ab.png）
    assert normalize_path("assets/a..b.png") == "assets/a..b.png"
    # 默认大小写敏感，原样保留
    assert normalize_path("Tex/Sub/A.PNG") == "Tex/Sub/A.PNG"
    # 大小写不敏感时大小写不同的同一路径归一
    assert normalize_path("Tex/Sub/A.PNG", case_insensitive=True) == "tex/sub/a.png"

    # 这些都必须被拒绝，而不是被"消毒"成根目录内的路径
    for bad in (
        "../escape.png",
        "tex/../../escape.png",
        "..;/..;/secret.png",
        "%2e%2e/secret.png",
        "%252e%252e/secret.png",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "./",  # 归一化后为空
        "",
    ):
        with pytest.raises(AssetError):
            normalize_path(bad)


# --------------------------------------------------------------------------
# 缺陷 2：单位换算按固定系数硬编码，mm→inch 差 0.4%
# --------------------------------------------------------------------------


def test_convert_units_uses_exact_factors():
    assert convert_units(25.4, "mm", "inch") == pytest.approx(1.0, abs=1e-9)
    assert convert_units(1.0, "inch", "mm") == pytest.approx(25.4, abs=1e-9)
    assert convert_units(2.54, "cm", "inch") == pytest.approx(1.0, abs=1e-9)
    # 单位名大小写不敏感
    assert convert_units(1.0, "M", "CM") == pytest.approx(100.0, abs=1e-9)
    # 同单位直接返回 float
    assert convert_units(3, "m", "m") == 3.0
    assert isinstance(convert_units(3, "m", "m"), float)
    with pytest.raises(AssetError):
        convert_units(1.0, "m", "ft")


# --------------------------------------------------------------------------
# 额外 1：1 m == 100 cm == 1000 mm（1e-9）
# --------------------------------------------------------------------------


def test_units_metric_chain_is_exact():
    assert convert_units(1.0, "m", "cm") == pytest.approx(100.0, abs=1e-9)
    assert convert_units(1.0, "m", "mm") == pytest.approx(1000.0, abs=1e-9)
    assert convert_units(100.0, "cm", "m") == pytest.approx(1.0, abs=1e-9)
    assert convert_units(1000.0, "mm", "cm") == pytest.approx(100.0, abs=1e-9)
    assert convert_units(1000.0, "mm", "m") == pytest.approx(1.0, abs=1e-9)
    assert convert_units(100.0, "cm", "mm") == pytest.approx(1000.0, abs=1e-9)


# --------------------------------------------------------------------------
# 缺陷 3：轴转换只翻 Z，绕序不修正导致法线朝内
# --------------------------------------------------------------------------


def test_convert_axes_flips_winding_on_handedness_change():
    mesh = {
        "vertices": [(1.0, 2.0, 3.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
        "faces": [(0, 1, 2)],
        "name": "tri",
    }

    # z-up 右手 -> y-up 右手：纯旋转，det = +1，(x, y, z) -> (x, z, -y)，绕序不变
    z2y = convert_axes(mesh, "z-up", "y-up")
    assert z2y["vertices"][0] == pytest.approx((1.0, 3.0, -2.0))
    assert z2y["vertices"][1] == pytest.approx((0.0, 0.0, -1.0))
    assert z2y["vertices"][2] == pytest.approx((0.0, 1.0, 0.0))
    assert [tuple(f) for f in z2y["faces"]] == [(0, 1, 2)]
    assert z2y["name"] == "tri"  # 其他键原样带出

    # 回到原坐标系：顶点与绕序都要还原
    back = convert_axes(z2y, "y-up", "z-up")
    for got, want in zip(back["vertices"], mesh["vertices"]):
        assert got == pytest.approx(want)
    assert [tuple(f) for f in back["faces"]] == [(0, 1, 2)]

    # 左右手互换：det = -1，Z 取反且三角形绕序必须取反（否则法线朝内）
    y2y_left = convert_axes(mesh, "y-up", "y-up-left")
    assert y2y_left["vertices"][0] == pytest.approx((1.0, 2.0, -3.0))
    assert y2y_left["vertices"][1] == pytest.approx((0.0, 1.0, 0.0))
    assert [tuple(f) for f in y2y_left["faces"]] == [(0, 2, 1)]

    # 非法朝向标记
    with pytest.raises(AssetError):
        convert_axes(mesh, "y-up", "w-up")


# --------------------------------------------------------------------------
# 缺陷 4：材质按名字去重，同名不同内容被合并
# --------------------------------------------------------------------------


def test_dedupe_materials_keys_on_content_not_name():
    materials = {
        # 名字只差大小写，内容不同：不能合并，两边都得活下来
        "Steel": {"color": [1, 0, 0], "rough": 0.2},
        "steel": {"color": [0, 1, 0], "rough": 0.2},
        # 与 Steel 内容相同（键序不同）：必须合并到 Steel
        "steel_dup": {"rough": 0.2, "color": [1, 0, 0]},
    }

    out = dedupe_materials(materials)

    assert [m["name"] for m in out["unique"]] == ["Steel", "steel"]
    assert out["mapping"] == {
        "Steel": "Steel",
        "steel": "steel",
        "steel_dup": "Steel",
    }
    assert out["unique"][0]["sources"] == ["Steel", "steel_dup"]
    assert out["unique"][1]["sources"] == ["steel"]
    # 内容哈希是 sha256 小写十六进制
    assert out["unique"][0]["hash"] != out["unique"][1]["hash"]
    assert all(len(m["hash"]) == 64 for m in out["unique"])

    with pytest.raises(AssetError):
        dedupe_materials({"bad": {"oops": {1, 2, 3}}})


# --------------------------------------------------------------------------
# 缺陷 5：循环引用直接递归爆栈，没有环检测
# --------------------------------------------------------------------------


def test_resolve_dependencies_detects_cycle_without_recursion():
    n = 3000
    manifest = {"n0": [f"n{n - 1}"]}
    for i in range(1, n):
        manifest[f"n{i}"] = [f"n{i - 1}"]
    # 整张图是一个 3000 节点的大环：递归实现必然 RecursionError

    out = resolve_dependencies(manifest)

    assert out["order"] == []
    assert len(out["cycles"]) == 1
    cycle = out["cycles"][0]
    # 从 manifest 里最早的 n0 开始，沿依赖边走：n0 -> n2999 -> ... -> n1
    assert cycle == ["n0"] + [f"n{i}" for i in range(n - 1, 0, -1)]
    assert set(cycle) == set(manifest)


# --------------------------------------------------------------------------
# 额外 2：空 manifest 返回空 order
# --------------------------------------------------------------------------


def test_resolve_dependencies_empty_manifest():
    assert resolve_dependencies({}) == {"cycles": [], "order": []}
    assert resolve_dependencies({"solo": []}) == {"cycles": [], "order": ["solo"]}
    # 稳定拓扑序：依赖在前，同层按 manifest 出现顺序（不是字典哈希顺序）
    plan = resolve_dependencies({"app": ["lib"], "lib": [], "zzz": []})
    assert plan["cycles"] == []
    assert plan["order"] == ["lib", "app", "zzz"]


# --------------------------------------------------------------------------
# 额外 3：循环引用返回而非抛异常
# --------------------------------------------------------------------------


def test_resolve_dependencies_returns_cycle_instead_of_raising():
    out = resolve_dependencies({"a": ["b"], "b": ["c"], "c": ["a"], "ok": []})
    assert out["cycles"] == [["a", "b", "c"]]
    assert out["order"] == ["ok"]
    # 环上的节点一个都不进 order
    assert all(node not in out["order"] for node in ("a", "b", "c"))


# --------------------------------------------------------------------------
# 缺陷 6：缺失引用不报告，导入后运行时才崩
# --------------------------------------------------------------------------


def test_check_missing_refs_reports_sorted_unique():
    files = ["tex/a.png", "tex/b.png", "models/hero.glb"]
    refs = [
        "tex\\b.png",  # 反斜杠，同一文件
        "tex/z.png",
        "models/hero.glb",
        "tex/a.png",
        "tex/z.png",  # 重复
        "audio/none.wav",
    ]
    assert check_missing_refs(files, refs) == ["audio/none.wav", "tex/z.png"]
    assert check_missing_refs(files, ["tex/a.png", "./models/hero.glb"]) == []
    with pytest.raises(AssetError):
        check_missing_refs(files, ["../outside.png"])


# --------------------------------------------------------------------------
# 缺陷 7：缓存键只含文件名，改设置仍命中旧缓存
# --------------------------------------------------------------------------


def test_cache_key_covers_settings_and_file_contents():
    base = cache_key({"quality": "high", "lod": 0}, {"a.png": "sha-a"})
    assert isinstance(base, str) and len(base) == 64

    # 改设置必须换键
    assert base != cache_key({"quality": "low", "lod": 0}, {"a.png": "sha-a"})
    assert base != cache_key({"quality": "high", "lod": 1}, {"a.png": "sha-a"})
    # 新增设置字段必须换键
    assert base != cache_key(
        {"quality": "high", "lod": 0, "pack": True}, {"a.png": "sha-a"}
    )
    # 文件内容摘要变化必须换键
    assert base != cache_key({"quality": "high", "lod": 0}, {"a.png": "sha-b"})
    # 相同输入必须同键
    assert base == cache_key({"quality": "high", "lod": 0}, {"a.png": "sha-a"})

    with pytest.raises(AssetError):
        cache_key({"bad": {1, 2}}, {"a.png": "sha-a"})


# --------------------------------------------------------------------------
# 额外 4：两种字典插入顺序 cache_key 相同
# --------------------------------------------------------------------------


def test_cache_key_is_insertion_order_independent():
    settings_a = {"quality": "high", "lod": 2, "pack": "dlc"}
    settings_b = {"pack": "dlc", "lod": 2, "quality": "high"}
    files_a = {"tex/b.png": "2", "tex/a.png": "1"}
    files_b = {"tex/a.png": "1", "tex/b.png": "2"}

    key = cache_key(settings_a, files_a)
    assert key == cache_key(settings_b, files_a)
    assert key == cache_key(settings_a, files_b)
    assert key == cache_key(settings_b, files_b)
    assert key == cache_key(dict(reversed(list(settings_a.items()))), files_b)


# --------------------------------------------------------------------------
# 缺陷 8：批量导入边写边落盘，失败留半成品
# --------------------------------------------------------------------------


def test_import_batch_rolls_back_whole_batch_on_failure(tmp_path):
    keep = tmp_path / "tex" / "keep.png"
    keep.parent.mkdir(parents=True)
    keep.write_text("原有内容", encoding="utf-8")

    jobs = [
        {"name": "good-a", "files": {"tex/a.png": "A"}},
        {"name": "bad-b", "files": {"tex/b.png": None}},  # 内容不是 str -> 校验失败
        {"name": "good-c", "files": {"tex/c.png": "C"}},
    ]

    res = import_batch(jobs, tmp_path)

    assert res["ok"] is False
    assert res["written"] == []
    assert res["order"] == []
    assert [f["job"] for f in res["failed"]] == ["bad-b"]
    assert isinstance(res["failed"][0]["error"], str) and res["failed"][0]["error"]

    # 磁盘上不能留半成品：既没有 a.png / c.png，也没有新目录，原有文件不被覆盖
    assert not (tmp_path / "tex" / "a.png").exists()
    assert not (tmp_path / "tex" / "c.png").exists()
    assert keep.read_text(encoding="utf-8") == "原有内容"
    assert sorted(p.name for p in (tmp_path / "tex").iterdir()) == ["keep.png"]
    assert [p.name for p in tmp_path.iterdir()] == ["tex"]


# --------------------------------------------------------------------------
# 缺陷 9：大小写不敏感文件系统上 A.png 与 a.png 互相覆盖
# --------------------------------------------------------------------------


def test_import_batch_rejects_case_insensitive_collision(tmp_path):
    jobs = [
        {"name": "one", "files": {"Tex/A.png": "UPPER"}},
        {"name": "two", "files": {"tex/a.png": "lower"}},
    ]

    res = import_batch(jobs, tmp_path)

    assert res["ok"] is False
    assert res["written"] == []
    assert {f["job"] for f in res["failed"]} == {"one", "two"}
    # 报错而不是静默覆盖；磁盘上什么都不写
    assert list(tmp_path.rglob("*")) == []


# --------------------------------------------------------------------------
# 缺陷 10：导入顺序依赖 dict 迭代顺序，两次结果不同
# --------------------------------------------------------------------------


def test_import_batch_deterministic_across_dict_order(tmp_path):
    def build(reverse):
        files_tex = {"tex/sky.png": "S", "tex/rock.png": "R"}
        settings = {"quality": "high", "lod": 0}
        if reverse:
            files_tex = {"tex/rock.png": "R", "tex/sky.png": "S"}
            settings = {"lod": 0, "quality": "high"}
        return [
            {"name": "ui", "deps": ["tex"], "files": {"ui/hud.png": "H"}},
            {"name": "tex", "files": files_tex, "settings": settings},
        ]

    r1 = import_batch(build(False), tmp_path / "r1")
    r2 = import_batch(build(True), tmp_path / "r2")

    assert r1["ok"] is True and r2["ok"] is True
    # 依赖在前：tex 先写，ui 后写（和 jobs 的书写顺序无关）
    assert r1["order"] == ["tex", "ui"]
    assert r2["order"] == ["tex", "ui"]
    # 键的顺序不影响落盘集合，written 永远按字符串升序
    assert r1["written"] == r2["written"] == ["tex/rock.png", "tex/sky.png", "ui/hud.png"]
    for rel in r1["written"]:
        assert (tmp_path / "r1" / rel).read_bytes() == (tmp_path / "r2" / rel).read_bytes()
    assert (tmp_path / "r2" / "ui" / "hud.png").read_text(encoding="utf-8") == "H"


# --------------------------------------------------------------------------
# 额外 5：批量全成功时 order 与输入顺序一致
# --------------------------------------------------------------------------


def test_import_batch_all_success_order_matches_input(tmp_path):
    jobs = [
        {"name": "c", "files": {"out/c.txt": "c"}},
        {"name": "a", "files": {"out/a.txt": "a"}},
        {"name": "b", "files": {"out/b.txt": "b"}},
    ]

    res = import_batch(jobs, tmp_path)

    assert res["ok"] is True
    assert res["failed"] == []
    assert res["order"] == ["c", "a", "b"]  # 输入顺序，不是字典/集合顺序
    assert res["written"] == ["out/a.txt", "out/b.txt", "out/c.txt"]
    for name in ("a", "b", "c"):
        assert (tmp_path / "out" / f"{name}.txt").read_text(encoding="utf-8") == name
