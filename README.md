# asset-import

第三方美术资产包（贴图、网格、材质、清单）批量导入流水线的最小复现仓库。纯标准库、离线、无随机，8 个函数全是未实现的桩，15 个测试初始全红。

## 题面

接入第三方美术资产包后，导入开始出问题：资源路径只做 `replace("..", "")` 消毒，`..;/` 和 `%252e%252e` 这类多重编码照样越界写到仓库外；长度单位换算按两两固定系数硬编码，`mm → inch` 差 0.4%；坐标轴转换只翻 Z 轴、不修正三角形绕序，导进来的模型法线朝内、背面剔除把正面剔没了；材质按名字去重，同名不同内容的两份材质被合并成一份；循环引用直接递归调用，深链一爆栈整批导入就崩，也没有环检测；缺失引用根本不检查，资产包少文件要等到运行时才崩；缓存键只拼了文件名，改了 quality / lod 还照旧命中旧缓存；批量导入边写边落盘，中途失败就留一堆半成品文件；大小写不敏感的文件系统上 `A.png` 和 `a.png` 互相覆盖、静默丢文件；导入顺序依赖 dict 迭代顺序，同一批资产两次跑出来的结果不一样。assetpipe/importers.py 是核心，tests/test_importers.py 15个红。要修的是：上述 10 个缺陷，契约已经写在 8 个桩函数的 docstring 里，只实现函数体，不要改签名和返回形状，修完这 15 个测试应当全绿。

## 运行方式

在仓库根目录执行：

```bash
python -m pytest tests -q
```

初始失败环境的预期输出（末尾一行）：

```text
15 failed in 0.12s
```

必须是**恰好 15 个 failed**，并且没有 collection error / import error / syntax error —— 也就是说桩函数里的 `NotImplementedError` 只能出现在测试体内部，不能出现在 fixture、conftest 或模块导入阶段。实现完成后同样的命令应当变成 `15 passed`。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 其他 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## tests 不可修改

`tests/test_importers.py` 就是验收规格：15 个测试逐条对应上面 10 个缺陷（每个缺陷 1 条）以及 5 条组合/边界约束（`1 m == 100 cm == 1000 mm`、两种字典插入顺序 `cache_key` 相同、空 manifest 返回空 `order`、循环引用返回而不是抛异常、批量全成功时 `order` 与输入顺序一致）。**测试文件不可修改**，也不要改 `conftest.py` 里把仓库根加进 `sys.path` 的那几行；要改的只有 `assetpipe/importers.py` 里 8 个函数的函数体。

## 仓库结构

```text
asset-import/
  README.md                 本文件
  conftest.py               把仓库根加进 sys.path（不定义 fixture）
  .gitignore                三行：__pycache__/、*.pyc、.pytest_cache/
  assetpipe/__init__.py     导出 8 个函数与 AssetError
  assetpipe/importers.py    8 个桩，契约写在 docstring 里
  tests/test_importers.py   15 个测试（不可修改）
```

## 缺陷与测试对照

| # | 缺陷 | 测试 |
| --- | --- | --- |
| 1 | 路径只做 `replace("..", "")`，`..;/` 与多重编码照样越界 | `test_normalize_path_rejects_traversal_variants` |
| 2 | 单位换算按固定系数硬编码，mm→inch 差 0.4% | `test_convert_units_uses_exact_factors` |
| 3 | 轴转换只翻 Z，绕序不修正导致法线朝内 | `test_convert_axes_flips_winding_on_handedness_change` |
| 4 | 材质按名字去重，同名不同内容被合并 | `test_dedupe_materials_keys_on_content_not_name` |
| 5 | 循环引用直接递归爆栈，没有环检测 | `test_resolve_dependencies_detects_cycle_without_recursion` |
| 6 | 缺失引用不报告，导入后运行时才崩 | `test_check_missing_refs_reports_sorted_unique` |
| 7 | 缓存键只含文件名，改设置仍命中旧缓存 | `test_cache_key_covers_settings_and_file_contents` |
| 8 | 批量导入边写边落盘，失败留半成品 | `test_import_batch_rolls_back_whole_batch_on_failure` |
| 9 | 大小写不敏感文件系统上 `A.png` 与 `a.png` 互相覆盖 | `test_import_batch_rejects_case_insensitive_collision` |
| 10 | 导入顺序依赖 dict 迭代顺序，两次结果不同 | `test_import_batch_deterministic_across_dict_order` |

组合/边界 5 条：`test_units_metric_chain_is_exact`、`test_cache_key_is_insertion_order_independent`、`test_resolve_dependencies_empty_manifest`、`test_resolve_dependencies_returns_cycle_instead_of_raising`、`test_import_batch_all_success_order_matches_input`。

## 约束

- 只允许标准库：`os`、`pathlib`、`hashlib`、`json`、`re`（NFC 归一化用标准库 `unicodedata`），外加 `pytest`；不引入第三方包。
- 离线、无随机、不读时间/环境变量：同一输入必须永远得到同一输出。
- 文件树一律用内联 dict `{相对路径: 内容}` 表示，落盘只发生在 `import_batch`，且根目录由调用方传入（测试里是 `tmp_path`）。
- 异常统一用 `AssetError`（不用内置 `ImportError` 当类名，避免与内置同名冲突）。
