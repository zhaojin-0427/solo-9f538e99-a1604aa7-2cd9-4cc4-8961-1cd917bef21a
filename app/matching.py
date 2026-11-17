"""SNP 指纹配对引擎（纯函数，不依赖 Web/数据库，便于单元测试）。

业务规则
--------
1. 两个样本在同一 SNP 位点上：
   - 任一侧为空（缺失）-> 该位点不参与一致性判断；
   - 两侧均非空且数值相同 -> 一致；
   - 两侧均非空且数值不同 -> 不一致。
2. 一对样本可成为配对的前提：两侧均非空的位点 >= 3 个，且不一致位点数 <= 1。
3. 在全部可配样本对构成的二分图上，求覆盖全部样本的一对一完美匹配，
   使全部分配的不一致位点数之和最小（最小权完美匹配）。
4. 对每个左侧样本，枚举其出现在 *任意一个* 最优完美匹配中的全部候选；
   候选数为 1 即该样本归属唯一，否则存在歧义。
5. 已锁定的配对按强制边处理；若强制后不存在合法完美匹配，或最小值高于
   未锁定时的原始最优值，则该锁定破坏最优性，必须拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# 禁止边使用的有限大代价。n <= 12，任何真实代价 <= 6，该值远大于任何
# 合法匹配的总代价，又避免了无穷大参与匈牙利算法的加减运算。
FORBIDDEN = 1_000_000
MIN_SHARED = 3
MAX_MISMATCH = 1


@dataclass(frozen=True)
class PairInfo:
    """一对样本之间的位点级核对结果。"""

    left_id: str
    right_id: str
    shared: int          # 两侧均非空的位点数
    mismatches: int      # 均非空但不一致的位点数
    matches: int         # 均非空且一致的位点数
    eligible: bool       # 是否满足配对前提（shared>=3 且 mismatches<=1）
    per_locus: Tuple[str, ...]  # 每个位点的判定：match/mismatch/missing


@dataclass
class AnalysisResult:
    """一次完整分析的结果。"""

    left_ids: List[str]
    right_ids: List[str]
    loci: List[str]
    pair_infos: Dict[Tuple[str, str], PairInfo]
    feasible: bool
    # feasible=True 时有意义
    optimum: Optional[int] = None
    # 施加锁定强制边后的最优值（无锁定时等于 optimum）
    forced_total: Optional[int] = None
    # 每个左侧样本 -> 候选右侧样本列表（按 right 顺序）
    candidates: Dict[str, List[str]] = field(default_factory=dict)
    # 一个最优匹配（left_id -> right_id），仅用于展示/调试
    one_solution: Dict[str, str] = field(default_factory=dict)
    # feasible=False 时的原因
    reason: Optional[str] = None
    # 不可配样本线索
    blocked_left: List[str] = field(default_factory=list)
    blocked_right: List[str] = field(default_factory=list)


def compare_pair(left_vals: Sequence[Optional[int]],
                 right_vals: Sequence[Optional[int]],
                 left_id: str, right_id: str) -> PairInfo:
    """逐位点比较两个样本，返回 PairInfo。"""
    per_locus: List[str] = []
    shared = mismatches = matches = 0
    for lv, rv in zip(left_vals, right_vals):
        if lv is None or rv is None:
            per_locus.append("missing")
            continue
        shared += 1
        if lv == rv:
            matches += 1
            per_locus.append("match")
        else:
            mismatches += 1
            per_locus.append("mismatch")
    eligible = shared >= MIN_SHARED and mismatches <= MAX_MISMATCH
    return PairInfo(
        left_id=left_id,
        right_id=right_id,
        shared=shared,
        mismatches=mismatches,
        matches=matches,
        eligible=eligible,
        per_locus=tuple(per_locus),
    )


def _build_cost_matrix(pairs: Dict[Tuple[str, str], PairInfo],
                       left_ids: Sequence[str],
                       right_ids: Sequence[str],
                       forced: Optional[Dict[str, str]] = None
                       ) -> List[List[int]]:
    """构造 n x n 代价矩阵；不可配对取 FORBIDDEN，forced 边强制约束。"""
    n = len(left_ids)
    forced = forced or {}
    right_pos = {rid: j for j, rid in enumerate(right_ids)}
    matrix: List[List[int]] = [[FORBIDDEN] * n for _ in range(n)]
    for i, lid in enumerate(left_ids):
        for j, rid in enumerate(right_ids):
            info = pairs[(lid, rid)]
            if info.eligible:
                matrix[i][j] = info.mismatches
    # 强制边：i 行只保留指向被锁定列的边；被锁定列只接受对应行。
    locked_cols = set()
    for lid, rid in forced.items():
        i = left_ids.index(lid)
        j = right_pos[rid]
        for k in range(n):
            matrix[i][k] = FORBIDDEN
            matrix[k][j] = FORBIDDEN
        info = pairs[(lid, rid)]
        matrix[i][j] = info.mismatches if info.eligible else FORBIDDEN
        locked_cols.add(j)
    return matrix


def hungarian_min_cost(cost: Sequence[Sequence[int]]) -> Tuple[int, List[int]]:
    """方阵最小权完美匹配（匈牙利算法，O(n^3)）。

    返回 (总代价, 分配)，分配 assignment[i] = 行 i 匹配的列。
    若不存在不经过 FORBIDDEN 边的完美匹配，总代价 >= FORBIDDEN // 2。
    1 基下标实现，u/p 为行势，v/q 为列势。
    """
    n = len(cost)
    inf = float("inf")
    u = [0] * (n + 1)
    v = [0] * (n + 1)
    p = [0] * (n + 1)  # p[j] = 与列 j 配对的行
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = 0
            row = cost[i0 - 1]
            for j in range(1, n + 1):
                if not used[j]:
                    cur = row[j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    assignment = [0] * n
    total = 0
    for j in range(1, n + 1):
        i = p[j]
        assignment[i - 1] = j - 1
        total += cost[i - 1][j - 1]
    return total, assignment


def _max_bipartite_matching(adj: Sequence[Sequence[int]], n: int
                            ) -> Tuple[int, List[int]]:
    """Kuhn 最大匹配，adj[i] 为行 i 可连的列。返回 (匹配数, 行->列)。"""
    match_col = [-1] * n

    def augment(i: int, seen: List[bool]) -> bool:
        for j in adj[i]:
            if seen[j]:
                continue
            seen[j] = True
            if match_col[j] == -1 or augment(match_col[j], seen):
                match_col[j] = i
                return True
        return False

    count = 0
    for i in range(n):
        seen = [False] * n
        if augment(i, seen):
            count += 1
    row_match = [-1] * n
    for j, i in enumerate(match_col):
        if i != -1:
            row_match[i] = j
    return count, row_match


def analyze(left_samples: Sequence[Tuple[str, Sequence[Optional[int]]]],
            right_samples: Sequence[Tuple[str, Sequence[Optional[int]]]],
            loci: Sequence[str],
            locks: Optional[Dict[str, str]] = None) -> AnalysisResult:
    """对两侧样本做完整分析。

    left_samples / right_samples: [(sample_id, [每个位点的值或 None]), ...]
    locks: 已锁定的 left_id -> right_id（作为强制边参与计算）。
    """
    locks = dict(locks or {})
    left_ids = [s[0] for s in left_samples]
    right_ids = [s[0] for s in right_samples]
    n = len(left_ids)

    pairs: Dict[Tuple[str, str], PairInfo] = {}
    adj: List[List[int]] = [[] for _ in range(n)]
    for i, (lid, lv) in enumerate(left_samples):
        for j, (rid, rv) in enumerate(right_samples):
            info = compare_pair(lv, rv, lid, rid)
            pairs[(lid, rid)] = info
            if info.eligible:
                adj[i].append(j)

    result = AnalysisResult(
        left_ids=list(left_ids),
        right_ids=list(right_ids),
        loci=list(loci),
        pair_infos=pairs,
        feasible=False,
    )

    # 1) 可配图上是否存在覆盖全部样本的完美匹配
    match_count, row_match = _max_bipartite_matching(adj, n)
    if match_count < n:
        result.blocked_left = [
            left_ids[i] for i in range(n) if not adj[i]
        ]
        col_has_edge = [False] * n
        for i in range(n):
            for j in adj[i]:
                col_has_edge[j] = True
        result.blocked_right = [
            right_ids[j] for j in range(n) if not col_has_edge[j]
        ]
        details = []
        if result.blocked_left:
            details.append(
                "左侧样本 " + "、".join(result.blocked_left)
                + " 没有任何满足条件的右侧样本"
            )
        if result.blocked_right:
            details.append(
                "右侧样本 " + "、".join(result.blocked_right)
                + " 没有任何满足条件的左侧样本"
            )
        if not details:
            details.append(
                "可配样本对构成的二分图不存在覆盖全部样本的一对一匹配"
            )
        result.reason = "；".join(details) + "（规则：共同非空位点数≥3 且不一致位点数≤1）"
        return result

    # 2) 无强制边的最小代价完美匹配 = 原始最优值
    base_matrix = _build_cost_matrix(pairs, left_ids, right_ids)
    optimum, assignment = hungarian_min_cost(base_matrix)
    result.optimum = optimum
    result.forced_total = optimum
    result.one_solution = {
        left_ids[i]: right_ids[assignment[i]] for i in range(n)
    }

    # 3) 带锁定强制边重新计算
    locked_matrix = _build_cost_matrix(pairs, left_ids, right_ids, locks)
    forced_total, forced_assignment = hungarian_min_cost(locked_matrix)

    if forced_total >= FORBIDDEN // 2:
        result.reason = "当前锁定的候选组合无法构成覆盖全部样本的合法一对一匹配"
        result.candidates = {lid: [] for lid in left_ids}
        return result
    if forced_total > optimum:
        result.reason = (
            "当前锁定会使不一致位点总数由最优值 %d 上升为 %d，破坏最优性"
            % (optimum, forced_total)
        )
        result.candidates = {lid: [] for lid in left_ids}
        return result

    # 4) 枚举每条边：强制该边后的最小值是否等于最优值，
    #    相等则该边至少出现在一个最优完美匹配中。
    candidates: Dict[str, List[str]] = {}
    for i, lid in enumerate(left_ids):
        if lid in locks:
            candidates[lid] = [locks[lid]]
            continue
        cand: List[str] = []
        for j, rid in enumerate(right_ids):
            if not pairs[(lid, rid)].eligible:
                continue
            trial = _build_cost_matrix(pairs, left_ids, right_ids, locks)
            for k in range(n):
                trial[i][k] = FORBIDDEN
                trial[k][j] = FORBIDDEN
            trial[i][j] = pairs[(lid, rid)].mismatches
            total, _ = hungarian_min_cost(trial)
            if total == forced_total:
                cand.append(rid)
        candidates[lid] = cand
    result.feasible = True
    result.forced_total = forced_total
    result.candidates = candidates
    # 带锁情况下的一个最优解（用于展示）
    result.one_solution = {
        left_ids[i]: right_ids[forced_assignment[i]] for i in range(n)
    }
    return result


def all_resolved(result: AnalysisResult) -> bool:
    """全部样本候选唯一时才允许下载。"""
    if not result.feasible:
        return False
    return all(len(c) == 1 for c in result.candidates.values())
