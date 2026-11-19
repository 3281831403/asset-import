"""assetpipe.importers —— 第三方美术资产包导入流水线（8 个待实现的桩）。

本模块是这道题的核心：8 个函数全部只有 ``raise NotImplementedError``，
契约（参数、返回形状、边界规则、异常）写在各自的 docstring 里。
``tests/test_importers.py`` 的 15 个测试完全按这些契约写，初始 15 个全红；
把函数体实现出来之后应当全绿。

全局约定
--------
* 只允许标准库：os、pathlib、hashlib、json、re（NFC 归一化用标准库 unicodedata），
  外加 pytest（仅测试用）。离线、无随机、不读 time/uuid/环境变量，
  同一输入必须永远得到同一输出。
* 文件树一律用纯 dict 表达：``{相对路径: 文本内容}``，不碰真实磁盘；
  只有 ``import_batch`` 落盘，且落盘根目录由调用方传入（测试里是 ``tmp_path``）。
* 所有失败都用 :class:`AssetError` 表达。不要用内置 ``ImportError`` 当异常名 ——
  自定义类一律叫 ``AssetError``，避免与内置同名冲突。
* 批量结果里 ``failed`` 的统一形状：
  ``[{"job": <job 名，拿不到名字时为 None>, "error": <错误说明 str>}, ...]``，按 jobs 输入顺序排列。
"""

import hashlib
import heapq
import json
import re
import unicodedata
from pathlib import Path

__all__ = [
    "AssetError",
    "normalize_path",
    "convert_units",
    "convert_axes",
    "dedupe_materials",
    "resolve_dependencies",
    "check_missing_refs",
    "cache_key",
    "import_batch",
]


class AssetError(Exception):
    """导入流水线对外暴露的唯一异常类型。

    任何"输入不合法 / 路径越界 / 单位未知 / 内容无法哈希 / 依赖成环但调用方要求顺序"
    的情形都抛它，不要把 ValueError、KeyError、RecursionError 泄漏给调用方。
    """


_PERCENT_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_PERCENT_DECODE = {
    "2e": ".",
    "2f": "/",
    "5c": "\\",
    "25": "%",
}


def _decode_percent(text):
    """把 %2e / %2f / %5c / %25（大小写不敏感）解码一轮，其他转义原样保留。"""

    def repl(match):
        return _PERCENT_DECODE.get(match.group(1).lower(), match.group(0))

    return _PERCENT_RE.sub(repl, text)


def _json_dumps_canonical(value):
    """题面固定的规范 JSON 序列化；无法序列化时统一抛 AssetError。"""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    except (TypeError, ValueError) as exc:
        raise AssetError(f"内容无法 JSON 序列化: {exc}") from exc


_AXIS_MARKERS = {
    "y-up": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    "z-up": ((1, 0, 0), (0, 0, 1), (0, -1, 0)),
    "x-up": ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
}
_LEFT_FLIP = ((1, 0, 0), (0, 1, 0), (0, 0, -1))


def _matmul(a, b):
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )


def _transpose(m):
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


def _matvec(m, v):
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


def _det3(m):
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def _axis_matrix(marker):
    """朝向标记 -> 该坐标系到规范系（y-up 右手）的变换矩阵。"""
    if not isinstance(marker, str):
        raise AssetError("朝向标记必须是 str")
    key = marker.strip().lower()
    if key in _AXIS_MARKERS:
        return _AXIS_MARKERS[key]
    if key.endswith("-left") and key[: -len("-left")] in _AXIS_MARKERS:
        return _matmul(_LEFT_FLIP, _AXIS_MARKERS[key[: -len("-left")]])
    raise AssetError(f"非法朝向标记: {marker!r}")


def _strongly_connected(edges):
    """迭代版 Tarjan；返回强连通分量列表（每项是节点列表）。"""
    index = {}
    low = {}
    on_stack = set()
    stack = []
    result = []
    counter = [0]

    for root in edges:
        if root in index:
            continue
        # work 栈元素：(节点, 邻接迭代器)
        work = [(root, iter(edges[root]))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(edges[nxt])))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    comp.append(member)
                    if member == node:
                        break
                result.append(comp)
    return result


def _stable_topo_order(nodes, edges):
    """按 nodes 给出的出现顺序做稳定拓扑排序（依赖在前）。"""
    position = {node: i for i, node in enumerate(nodes)}
    indegree = {node: 0 for node in nodes}
    children = {node: [] for node in nodes}
    for node in nodes:
        for dep in edges[node]:
            if dep in indegree:
                indegree[node] += 1
                children[dep].append(node)
    ready = [(position[n], n) for n in nodes if indegree[n] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        _, node = heapq.heappop(ready)
        order.append(node)
        for child in children[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, (position[child], child))
    return order


def normalize_path(path, case_insensitive=False):
    """把第三方资产包里的资源路径规范成"仓库内唯一、可比较、不可越界"的键。

    参数
    ----
    path : str
        原始相对路径，可能来自 zip 条目、manifest 清单、Windows 复制粘贴。
    case_insensitive : bool, 默认 False
        为 True 时把结果折叠成大小写不敏感形式（用于在大小写不敏感的文件系统上判冲突）。

    返回
    ----
    str
        规范化后的相对路径：始终以 ``/`` 分隔、不含 ``.`` 段、不含空段、
        不以 ``/`` 开头、不含 ``..`` 的越界残留。

    规则（建议按此顺序实现）
    ------------------------
    1. ``path`` 不是 str，或去掉空白后为空 → ``AssetError``。
    2. 反斜杠（Windows 分隔符）一律换成 ``/``。
    3. **反复解码百分号转义直到字符串不再变化**（最多 5 轮，仍变化 → ``AssetError``，
       视为多重编码绕过）：``%2e`` → ``.``、``%2f`` → ``/``、``%5c`` → 反斜杠、
       ``%25`` → ``%``（十六进制大小写不敏感）。每轮解码后重新执行第 2 步。
       注意 ``%252e%252e`` 这种双重编码必须解码两轮后才能识别出 ``..``。
    4. Unicode NFC 归一化（``unicodedata.normalize("NFC", ...)``），让
       ``cafe`` + 组合重音 和 ``café`` 得到同一个键。
    5. 以 ``/`` 开头，或首段形如盘符（如 ``C:``）→ ``AssetError``（绝对路径一律拒绝，
       不允许悄悄改成相对路径）。
    6. 任一段含 NUL 字符（``chr(0)``）→ ``AssetError``。
    7. 丢弃空段和单点段 ``.``。
    8. "点段变体"判定：某一段非空、每个字符都在 ``{".", ";", " "}`` 里、且至少含 2 个 ``.``
       → 视作 ``..``：弹出一段并继续；此时栈已空（写盘会落到根目录之外）→ ``AssetError``。
       只含 1 个点的变体（例如 ``.;``）视作 ``.``，丢弃。
       **普通文件名里的连续点不是点段变体**：``assets/a..b.png`` 必须原样保留。
    9. 结果为空串 → ``AssetError``。
    10. ``case_insensitive`` 为 True 时对整串做 ``str.casefold()``；否则保留原大小写。

    严禁用 ``path.replace("..", "")`` 之类的字符串替换来"消毒"：它既会吃掉合法文件名，
    又放过 ``..;/..;/secret.png`` 与多重编码。
    """
    if not isinstance(path, str):
        raise AssetError("path 必须是 str")
    text = path.strip()
    if not text:
        raise AssetError("path 不能为空")

    # 2/3. 反复解码百分号转义，直到稳定（最多 5 轮）。
    for _ in range(5):
        text = text.replace("\\", "/")
        decoded = _decode_percent(text)
        if decoded == text:
            break
        text = decoded
    else:
        # 5 轮之后仍在变化：多重编码绕过。
        if _decode_percent(text.replace("\\", "/")) != text.replace("\\", "/"):
            raise AssetError("路径存在多重百分号编码")
    text = text.replace("\\", "/")

    # 4. Unicode NFC 归一化
    text = unicodedata.normalize("NFC", text)

    # 5. 绝对路径 / 盘符一律拒绝
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise AssetError("不允许绝对路径")

    # 6. 任一段含 NUL 字符即拒绝
    if chr(0) in text:
        raise AssetError("路径含 NUL 字符")

    # 7/8. 逐段消解
    parts = []
    for segment in text.split("/"):
        if segment == "" or segment == ".":
            continue
        # 点段变体：非空、只由 {'.', ';', ' '} 组成、且至少 2 个点
        if (
            segment
            and all(ch in ".; " for ch in segment)
            and segment.count(".") >= 2
        ):
            if not parts:
                raise AssetError("路径越界")
            parts.pop()
            continue
        # 单点变体（如 ".;"）视作 "."，丢弃
        if segment and all(ch in ".; " for ch in segment) and segment.count(".") == 1:
            continue
        parts.append(segment)

    if not parts:
        raise AssetError("路径归一化后为空")

    result = "/".join(parts)
    if case_insensitive:
        result = result.casefold()
    return result



def convert_units(value, from_unit, to_unit):
    """在 m / cm / mm / inch 四种长度单位之间换算，返回 float。

    参数
    ----
    value : int | float  长度值
    from_unit, to_unit : str  单位名，大小写不敏感、忽略首尾空白

    返回
    ----
    float 换算结果。

    规则
    ----
    * **只允许一张以米为基准的精确系数表**，由它推导任意两单位之间的换算：
      ``m = 1.0``、``cm = 0.01``、``mm = 0.001``、``inch = 0.0254``
      （于是 1 inch 恰好等于 25.4 mm）。
      不允许硬编码"两两系数"——历史实现把 mm→inch 写成近似系数，误差 0.4%。
    * 实现上直接用 ``float(value) * TABLE[from] / TABLE[to]``，误差不超过 1e-9（相对/绝对都远小于该值）。
    * ``from_unit == to_unit`` 时返回 ``float(value)``。
    * 未知单位 → ``AssetError``。
    """
    table = {"m": 1.0, "cm": 0.01, "mm": 0.001, "inch": 0.0254}

    if not isinstance(from_unit, str) or not isinstance(to_unit, str):
        raise AssetError("单位名必须是 str")
    src = from_unit.strip().lower()
    dst = to_unit.strip().lower()
    if src not in table or dst not in table:
        raise AssetError(f"未知单位: {from_unit!r} -> {to_unit!r}")
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise AssetError(f"长度值不是数值: {value!r}") from exc
    if src == dst:
        return amount
    return amount * table[src] / table[dst]


def convert_axes(mesh, from_axis, to_axis):
    """转换网格的坐标系朝向（up 轴 / 左右手系），必要时修正三角形绕序。

    参数
    ----
    mesh : dict
        形如 ``{"vertices": [(x, y, z), ...], "faces": [(i, j, k), ...]}`` 的普通 dict。
        允许携带其他键（例如 ``"name"``），必须原样带进结果。
    from_axis, to_axis : str
        朝向标记，合法取值共 6 个：
        ``"x-up"``、``"y-up"``、``"z-up"``、``"x-up-left"``、``"y-up-left"``、``"z-up-left"``。
        后缀 ``-left`` 表示左手系，没有后缀是右手系。其他取值 → ``AssetError``。

    返回
    ----
    dict
        ``{"vertices": [(x, y, z), ...], "faces": [(i, j, k), ...], <其余键原样>}``；
        vertices 的元素是 float 三元组，faces 的元素是 int 三元组。

    换算规则
    --------
    先把源坐标系转到规范系（y-up 右手系），再把规范系转到目标坐标系，即
    ``M = M_to^-1 @ M_from``（矩阵都是带符号的置换矩阵，正交，逆即转置）：

    * ``y-up``:        (x, y, z) -> (x, y, z)            恒等，det = +1
    * ``z-up``:        (x, y, z) -> (x, z, -y)           绕 X 转 -90°，把 +Z 抬成新 +Y，det = +1
    * ``x-up``:        (x, y, z) -> (-y, x, z)           绕 Z 转 90°，把 +X 抬成新 +Y，det = +1
    * 左手系（``-left``）：在上面结果的基础上再取负 Z 分量，即 (x, y, z) -> (x, y, -z)，det = -1

    * 整体变换 ``det(M) < 0``（左右手互换）时，**三角形绕序必须取反**：
      ``(i, j, k) -> (i, k, j)``。否则几何法线朝内、背面剔除和光照全错。
      ``det(M) > 0`` 时绕序保持不变。
    * 同一个转换做两次（A→B→A）必须回到原始顶点与原始绕序。

    其他
    ----
    * 顶点必须是长度 3 的数值序列、面必须是长度 3 的整数索引，否则 ``AssetError``。
    * 不要原地修改传入的 mesh（返回新对象）。
    """
    from_mat = _axis_matrix(from_axis)
    to_mat = _axis_matrix(to_axis)
    # M = M_to^-1 @ M_from，置换矩阵的逆即转置。
    matrix = _matmul(_transpose(to_mat), from_mat)
    flip_winding = _det3(matrix) < 0

    if not isinstance(mesh, dict):
        raise AssetError("mesh 必须是 dict")
    if "vertices" not in mesh or "faces" not in mesh:
        raise AssetError("mesh 缺少 vertices / faces")

    new_vertices = []
    for vertex in mesh["vertices"]:
        if not isinstance(vertex, (list, tuple)) or len(vertex) != 3:
            raise AssetError("顶点必须是长度 3 的数值序列")
        coords = []
        for comp in vertex:
            if isinstance(comp, bool) or not isinstance(comp, (int, float)):
                raise AssetError("顶点坐标必须是数值")
            coords.append(float(comp))
        new_vertices.append(tuple(_matvec(matrix, coords)))

    new_faces = []
    for face in mesh["faces"]:
        if not isinstance(face, (list, tuple)) or len(face) != 3:
            raise AssetError("面必须是长度 3 的整数索引")
        indices = []
        for idx in face:
            if isinstance(idx, bool) or not isinstance(idx, int):
                raise AssetError("面索引必须是整数")
            indices.append(idx)
        if flip_winding:
            indices = [indices[0], indices[2], indices[1]]
        new_faces.append(tuple(indices))

    result = dict(mesh)
    result["vertices"] = new_vertices
    result["faces"] = new_faces
    return result


def dedupe_materials(materials):
    """按**内容**去重材质（不是按名字），并给出旧名到新名的映射。

    参数
    ----
    materials : dict
        ``{旧名: 内容}``，内容为 JSON 可序列化对象（dict/list/str/数字/布尔/None）。
        旧名是 str，可能只有大小写或空白不同。

    返回
    ----
    dict
        ``{"unique": [...], "mapping": {旧名: 新名}}``：
        ``unique`` 的每一项是 ``{"name": 新名, "hash": 内容哈希, "sources": [旧名, ...]}``，
        按"该内容第一次出现的旧名在 materials 里的顺序"排列。

    规则
    ----
    * 内容哈希算法必须是：
      ``sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
      .encode("utf-8")).hexdigest()``，即 64 位小写十六进制。
      ``sort_keys=True`` 保证同样的内容不管键序如何都得到同一哈希。
    * 去重只看内容哈希，**不看名字**：``"Steel"`` 与 ``"steel"`` 内容不同 → 两个 unique 条目，
      两条 mapping 分别映射到自己，谁都不许被合并掉。
    * 内容相同、名字不同 → 只保留一个 unique 条目，新名取"该内容第一次出现时用的旧名"，
      ``mapping`` 里所有同内容的旧名都指向这个新名。
    * ``sources`` 列出映射到该内容的全部旧名，顺序与 materials 的迭代顺序一致。
    * 内容无法 JSON 序列化（例如 set、自定义对象）→ ``AssetError``。
    * materials 不是 dict → ``AssetError``。
    """
    if not isinstance(materials, dict):
        raise AssetError("materials 必须是 dict")

    unique = []
    by_hash = {}
    mapping = {}
    for old_name, content in materials.items():
        if not isinstance(old_name, str):
            raise AssetError("材质名必须是 str")
        digest = hashlib.sha256(
            _json_dumps_canonical(content).encode("utf-8")
        ).hexdigest()
        entry = by_hash.get(digest)
        if entry is None:
            entry = {"name": old_name, "hash": digest, "sources": []}
            by_hash[digest] = entry
            unique.append(entry)
        entry["sources"].append(old_name)
        mapping[old_name] = entry["name"]

    return {"unique": unique, "mapping": mapping}


def resolve_dependencies(manifest):
    """解析资产清单里的依赖，给出稳定拓扑序，并把环检测出来（不递归、不爆栈）。

    参数
    ----
    manifest : dict
        ``{节点: [依赖节点, ...]}``；值为空列表或 None 表示没有依赖。
        只描述 manifest 内部的依赖关系；引用到 manifest 之外的名字视为"外部依赖"，
        直接忽略（既不进 order，也不报环）。
        节点名重复不可能出现（dict 键唯一）；依赖列表允许重复，按出现顺序去重。

    返回
    ----
    dict
        ``{"cycles": [[节点, ...], ...], "order": [节点, ...]}``，
        空 manifest 必须返回 ``{"cycles": [], "order": []}``。

    规则
    ----
    * ``order`` 是稳定的拓扑序：**依赖在前、被依赖者在前**；同一时刻有多个可选节点时，
      取在 manifest 里出现更早的那个（这就是"稳定"的含义，和 dict 的哈希顺序无关）。
      历史实现直接按 dict 迭代顺序推，两次运行结果会不一样。
    * ``order`` 只包含不在任何环上的节点；纯环里的节点一个都不进 order。
    * ``cycles`` 每一项是一个环：从该环中在 manifest 里出现最早的节点开始，
      然后**沿依赖边走**（``a`` 依赖 ``b``，则 ``a`` 后面跟 ``b``），首节点不重复。
      例如 ``{"a": ["b"], "b": ["c"], "c": ["a"]}`` → ``[["a", "b", "c"]]``。
      多个环之间按各自首节点在 manifest 中的先后排序；节点集合相同的环只报一次。
    * **必须是迭代实现**：``a -> b -> ... -> a`` 这种链再长也只能体现在 ``cycles`` 里，
      绝不能抛 RecursionError（几千个节点的环也必须正常返回）。
    * manifest 不是 dict、依赖列表里混入非 str → ``AssetError``。
    """
    if not isinstance(manifest, dict):
        raise AssetError("manifest 必须是 dict")

    order_index = {}
    edges = {}
    for node, deps in manifest.items():
        if not isinstance(node, str):
            raise AssetError("manifest 的节点名必须是 str")
        order_index[node] = len(order_index)
        if deps is None:
            deps = []
        if not isinstance(deps, (list, tuple)):
            raise AssetError("依赖必须是列表")
        seen = []
        seen_set = set()
        for dep in deps:
            if not isinstance(dep, str):
                raise AssetError("依赖节点名必须是 str")
            if dep not in seen_set:
                seen_set.add(dep)
                seen.append(dep)
        # 外部依赖直接忽略；只保留 manifest 内部边。
        edges[node] = [d for d in seen if d in manifest]

    # Tarjan 强连通分量（迭代实现，避免深链爆栈）。
    components = _strongly_connected(edges)
    node_component = {}
    for comp_idx, comp in enumerate(components):
        for node in comp:
            node_component[node] = comp_idx

    # 环：大小 > 1，或单节点自环（节点依赖自身）。
    cyclic_nodes = set()
    raw_cycles = []
    for comp in components:
        if len(comp) > 1:
            cyclic_nodes.update(comp)
            raw_cycles.append(comp)
        elif comp[0] in edges[comp[0]]:
            cyclic_nodes.add(comp[0])
            raw_cycles.append(comp)

    # 每个环从 manifest 中出现最早的节点起步，沿依赖边走出首尾相接的路径。
    cycles = []
    for comp in raw_cycles:
        member = set(comp)
        start = min(comp, key=lambda n: order_index[n])
        path = [start]
        visited = {start}
        current = start
        while True:
            candidates = [d for d in edges[current] if d in member]
            nxt = min(candidates, key=lambda n: order_index[n])
            if nxt == start:
                break
            if nxt in visited:
                # 复杂强连通分量：取一条不重复节点的闭合路径。
                break
            path.append(nxt)
            visited.add(nxt)
            current = nxt
        cycles.append(path)
    cycles.sort(key=lambda cyc: order_index[cyc[0]])

    # 稳定拓扑序：Kahn，每次取 manifest 中出现最早的就绪节点。
    acyclic_nodes = [node for node in manifest if node not in cyclic_nodes]
    order = _stable_topo_order(acyclic_nodes, edges)

    return {"cycles": cycles, "order": order}


def check_missing_refs(files, refs):
    """检查引用到的资源是否都真的存在；返回缺失引用列表。

    参数
    ----
    files : Iterable[str]  实际存在的文件路径
    refs : Iterable[str]   被引用到的路径，允许重复

    返回
    ----
    list[str]
        缺失引用（已规范化、去重、按字符串升序排列）。全部命中时返回 ``[]``。

    规则
    ----
    * 两侧的路径都先各自规范化（``normalize_path(path, case_insensitive=False)``）再比较，
      所以 ``tex\\b.png`` 与 ``tex/b.png``、``./tex/b.png`` 视为同一个文件。
    * 本函数按**大小写敏感**比较（大小写折叠只用于 normalize_path 的
      case_insensitive=True 与 import_batch 的冲突检测）。
    * 任一侧出现非法路径（normalize_path 抛 AssetError，例如越界）→ 直接抛 ``AssetError``，
      不要静默跳过。
    * 历史实现根本不检查，缺失引用要到运行时才炸。
    """
    available = {normalize_path(f, case_insensitive=False) for f in files}
    missing = set()
    for ref in refs:
        norm = normalize_path(ref, case_insensitive=False)
        if norm not in available:
            missing.add(norm)
    return sorted(missing)


def cache_key(settings, files):
    """计算导入缓存的键：必须覆盖所有影响输出的输入，且与 dict 插入顺序无关。

    参数
    ----
    settings : dict
        ``{设置名: JSON 可序列化值}``，例如 ``{"quality": "high", "lod": 0}``。
    files : dict
        ``{路径或逻辑名: 内容摘要}``，例如 ``{"tex/rock.png": "<sha256>"}``。

    返回
    ----
    str
        64 位小写十六进制 sha256。

    规则
    ----
    * 计算方式固定：把 ``{"v": 1, "settings": settings, "files": files}`` 用
      ``json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`` 序列化，
      再 UTF-8 编码后取 sha256。
    * **与字典插入顺序无关**：``{"a": 1, "b": 2}`` 和 ``{"b": 2, "a": 1}`` 必须同键，
      ``files`` 同理。历史实现只把文件名拼进键里，改设置仍然命中旧缓存。
    * 任何影响输出的字段变化、新增字段、文件内容摘要变化都必须改变键；
      相同输入必须得到相同键。
    * settings 或 files 无法 JSON 序列化 → ``AssetError``。
    """
    if not isinstance(settings, dict) or not isinstance(files, dict):
        raise AssetError("settings 与 files 必须是 dict")
    payload = _json_dumps_canonical({"v": 1, "settings": settings, "files": files})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def import_batch(jobs, root):
    """批量导入：要么整批成功，要么一个字节都不留在磁盘上（回滚）。

    参数
    ----
    jobs : list[dict]
        每个 job 形如::

            {
                "name": "hero",                        # 必填，非空 str，批内唯一
                "files": {"tex/hero.png": "内容文本"},  # 必填，dict{相对路径: str}
                "deps": ["base"],                      # 可选，依赖的 job 名
                "settings": {"quality": "high"},       # 可选，JSON 可序列化
            }

    root : str | os.PathLike
        落盘根目录（通常是 ``tmp_path``）。不存在则按需创建（``parents=True``）。

    返回
    ----
    dict
        成功：``{"ok": True, "failed": [], "order": [job 名, ...], "written": [相对路径, ...]}``
        失败：``{"ok": False, "failed": [{"job": ..., "error": ...}, ...], "order": [], "written": []}``

    规则
    ----
    * **先全量校验、后落盘**。校验阶段任何一条不通过，磁盘都不许动：
      ``ok=False``、``written=[]``、``failed`` 按 jobs 输入顺序给出。
      失败的判定至少包括：
        - job 不是 dict、``name`` 缺失/为空/非 str、批内重名；
        - ``files`` 不是 dict，键无法 normalize_path（越界/绝对路径等），内容不是 str；
        - ``deps`` 不是 str 列表，或指向批内不存在的 job；
        - job 之间（以及同一 job 内）出现"仅大小写不同"的落盘路径冲突 —— 也就是说
          目标路径按 ``normalize_path(p, case_insensitive=True)`` 判重，冲突涉及的每个 job 都算失败。
          大小写不敏感文件系统上 ``Tex/A.png`` 与 ``tex/a.png`` 会互相覆盖，必须报错而不是静默覆盖；
        - ``settings`` 无法 JSON 序列化；
        - job 依赖成环（环上的每个 job 都算失败）。
    * 落地路径 = ``root / normalize_path(原始键, case_insensitive=False)``（保留原大小写），
      内容以 UTF-8 文本写入。
    * 写入顺序：job 之间用与 :func:`resolve_dependencies` 相同的稳定拓扑序
      （依赖先写，同层按 jobs 输入顺序）；同一 job 内按规范化路径升序写。
      ``order`` 就是这个顺序；**全部成功且没有 deps 时 order 必须与 jobs 输入顺序一致**
      （不许依赖 dict 迭代顺序 / set 顺序，两次运行结果必须完全相同）。
    * ``written`` 是全部落盘文件的相对路径列表，``/`` 分隔，按字符串升序。
    * 写入阶段遇到 OSError（磁盘满、权限等）同样整体回滚：
      删掉本批新建的文件、删掉本批新建的空目录、被覆盖的已有文件恢复原内容，
      然后返回 ``ok=False``，``failed`` 里带上出错的 job。
    * 返回前磁盘状态必须等于"什么都没导入"（成功时则等于"整批都在"），
      不允许留半成品文件或空目录。
    """
    errors_by_index = {}

    def fail(index, name, message):
        """每个 job 只保留一条失败记录（第一条错误信息最贴近根因）。"""
        errors_by_index.setdefault(index, ({"job": name, "error": message},))

    if not isinstance(jobs, (list, tuple)):
        return {
            "ok": False,
            "failed": [{"job": None, "error": "jobs 必须是 list"}],
            "order": [],
            "written": [],
        }

    # ---------------- 校验阶段：只读，绝不碰磁盘 ----------------
    name_positions = {}
    entries = []  # 每个 job 的解析结果（校验通过时）

    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            fail(index, None, "job 必须是 dict")
            entries.append(None)
            continue
        name = job.get("name")
        if not isinstance(name, str) or not name.strip():
            fail(index, None, "name 缺失或不是非空 str")
            entries.append(None)
            continue
        if name in name_positions:
            fail(index, name, "批内 job 重名")
            entries.append(None)
            continue
        name_positions[name] = index
        entries.append({"name": name})

    # 逐 job 校验 files / deps / settings
    job_files = {}
    job_deps = {}
    case_keys = {}  # casefold 路径 -> [(case-sensitive 路径, job 名)]
    for index, job in enumerate(jobs):
        if not isinstance(job, dict) or entries[index] is None:
            continue
        name = entries[index]["name"]
        errors = []

        files = job.get("files")
        normalized = {}
        if not isinstance(files, dict):
            errors.append("files 必须是 dict")
        else:
            for raw_path, content in files.items():
                if not isinstance(content, str):
                    errors.append(f"文件内容必须是 str: {raw_path!r}")
                    continue
                try:
                    norm = normalize_path(raw_path, case_insensitive=False)
                except AssetError as exc:
                    errors.append(f"非法路径 {raw_path!r}: {exc}")
                    continue
                normalized[norm] = content
                ci = normalize_path(raw_path, case_insensitive=True)
                case_keys.setdefault(ci, []).append((norm, name))
        job_files[name] = normalized

        deps = job.get("deps", [])
        if deps is None:
            deps = []
        if not isinstance(deps, (list, tuple)) or any(
            not isinstance(d, str) for d in deps
        ):
            errors.append("deps 必须是 str 列表")
            deps = []
        job_deps[name] = list(deps)

        if "settings" in job:
            try:
                _json_dumps_canonical(job["settings"])
            except AssetError as exc:
                errors.append(f"settings 无法序列化: {exc}")

        if errors:
            entries[index] = None
            for message in errors:
                fail(index, name, message)

    # 依赖指向批内不存在的 job
    for index, job in enumerate(jobs):
        if entries[index] is None:
            continue
        name = entries[index]["name"]
        missing = [d for d in job_deps.get(name, []) if d not in name_positions]
        if missing:
            fail(index, name, f"依赖的 job 不存在: {missing}")
            entries[index] = None

    # 大小写不敏感落盘路径冲突：冲突涉及的每个 job 都算失败
    collision_jobs = set()
    for ci, holders in case_keys.items():
        distinct_case = {norm for norm, _ in holders}
        distinct_jobs = {name for _, name in holders}
        if len(distinct_case) > 1 or len(distinct_jobs) > 1:
            collision_jobs.update(distinct_jobs)
    for index, job in enumerate(jobs):
        if entries[index] is None:
            continue
        name = entries[index]["name"]
        if name in collision_jobs:
            fail(index, name, "大小写不敏感的落盘路径冲突")
            entries[index] = None

    # 依赖成环：环上的每个 job 都算失败
    valid_names = {n for n, i in name_positions.items() if entries[i] is not None}
    graph = {
        e["name"]: [d for d in job_deps.get(e["name"], []) if d in valid_names]
        for e in entries
        if e is not None
    }
    plan = resolve_dependencies(graph)
    cyclic = {node for cyc in plan["cycles"] for node in cyc}
    for index, job in enumerate(jobs):
        if entries[index] is None:
            continue
        name = entries[index]["name"]
        if name in cyclic:
            fail(index, name, "依赖成环")
            entries[index] = None

    if errors_by_index:
        return {
            "ok": False,
            "failed": [errors_by_index[i][0] for i in sorted(errors_by_index)],
            "order": [],
            "written": [],
        }

    # ---------------- 落盘阶段：先备份、失败整体回滚 ----------------
    graph = {e["name"]: list(job_deps[e["name"]]) for e in entries}
    order = resolve_dependencies(graph)["order"]

    root_path = Path(root)
    created_dirs = []
    backups = {}  # 目标路径 -> 原内容（None 表示原先不存在）
    written_rel = []

    def ensure_dir(directory):
        """创建目录，记录本批新建的每一级，供回滚删除。"""
        missing = []
        probe = directory
        while not probe.exists():
            missing.append(probe)
            if probe.parent == probe:
                break
            probe = probe.parent
        for target in reversed(missing):
            target.mkdir()
            created_dirs.append(target)

    def rollback():
        for rel in written_rel:
            try:
                (root_path / rel).unlink()
            except OSError:
                pass
        for path, original in backups.items():
            if original is None:
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(original, encoding="utf-8")
            except OSError:
                pass
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass

    failed_job = None
    try:
        for name in order:
            failed_job = name
            normalized = job_files[name]
            for rel in sorted(normalized):
                target = root_path / rel
                if target not in backups:
                    backups[target] = (
                        target.read_text(encoding="utf-8") if target.exists() else None
                    )
                ensure_dir(target.parent)
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write(normalized[rel])
                written_rel.append(rel)
    except (OSError, ValueError) as exc:
        rollback()
        return {
            "ok": False,
            "failed": [{"job": failed_job, "error": f"写入失败: {exc}"}],
            "order": [],
            "written": [],
        }

    return {
        "ok": True,
        "failed": [],
        "order": order,
        "written": sorted(written_rel),
    }
