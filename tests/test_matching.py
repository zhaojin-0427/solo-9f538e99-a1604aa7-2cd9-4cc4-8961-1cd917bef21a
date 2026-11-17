"""配对引擎单元测试：规则、最优值、候选枚举、锁定与无解。"""

import itertools
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.matching import (FORBIDDEN, analyze, compare_pair, hungarian_min_cost,
                          all_resolved)

LOCI = [f"SNP{i}" for i in range(1, 7)]


def g(*vals):
    """把 'x' 记为缺失，数字转 int。"""
    return [None if v == "x" else int(v) for v in vals]


def brute_force(left_samples, right_samples, locks=None):
    """暴力枚举所有完美匹配，返回 (最优代价, 每个 left 的候选集合)。"""
    locks = locks or {}
    left_ids = [s[0] for s in left_samples]
    right_ids = [s[0] for s in right_samples]
    pairs = {}
    for lid, lv in left_samples:
        for rid, rv in right_samples:
            pairs[(lid, rid)] = compare_pair(lv, rv, lid, rid)
    best = None
    edge_in_best = {lid: set() for lid in left_ids}
    for perm in itertools.permutations(range(len(right_ids))):
        matching = {left_ids[i]: right_ids[perm[i]] for i in range(len(left_ids))}
        # 强制锁
        if any(matching.get(l) != r for l, r in locks.items()):
            continue
        cost = 0
        ok = True
        for lid, rid in matching.items():
            info = pairs[(lid, rid)]
            if not info.eligible:
                ok = False
                break
            cost += info.mismatches
        if not ok:
            continue
        if best is None or cost < best:
            best = cost
        if best is not None and cost == best:
            pass
    # 第二遍：在已知最优值下收集边
    edge_in_best = {lid: set() for lid in left_ids}
    for perm in itertools.permutations(range(len(right_ids))):
        matching = {left_ids[i]: right_ids[perm[i]] for i in range(len(left_ids))}
        if any(matching.get(l) != r for l, r in locks.items()):
            continue
        cost = 0
        ok = True
        for lid, rid in matching.items():
            info = pairs[(lid, rid)]
            if not info.eligible:
                ok = False
                break
            cost += info.mismatches
        if ok and cost == best:
            for lid, rid in matching.items():
                edge_in_best[lid].add(rid)
    return best, edge_in_best


def check_against_bruteforce(left_samples, right_samples, locks=None):
    result = analyze(left_samples, right_samples, LOCI, locks)
    best, edges = brute_force(left_samples, right_samples, locks)
    if best is None:
        assert not result.feasible
        return result
    assert result.feasible
    assert result.forced_total == best
    for lid, cands in result.candidates.items():
        assert set(cands) == edges[lid], (lid, cands, edges[lid])
    return result


# --------------------------------------------------------------- 基础规则
def test_compare_pair_rule():
    # 4 个共非空，1 个不一致 -> 可配
    info = compare_pair(g(0, 0, 1, 2, "x", 1), g(0, 0, 1, 1, 0, "x"), "L", "R")
    assert info.shared == 4
    assert info.matches == 3
    assert info.mismatches == 1
    assert info.eligible

    # 只有 2 个共非空 -> 不可配，即使完全一致
    info = compare_pair(g(0, 1, "x", "x", "x", "x"), g(0, 1, "x", "x", "x", "x"), "L", "R")
    assert info.shared == 2
    assert not info.eligible

    # 共非空 5 个但 2 个不一致 -> 不可配
    info = compare_pair(g(0, 0, 0, 0, 0, "x"), g(1, 1, 0, 0, 0, 0), "L", "R")
    assert info.shared == 5
    assert info.mismatches == 2
    assert not info.eligible

    # 边界：共 3 且异 1 -> 可配
    info = compare_pair(g(0, 1, 0, "x", "x", "x"), g(0, 1, 2, 0, 0, 0), "L", "R")
    assert info.shared == 3 and info.mismatches == 1 and info.eligible


def test_hungarian_known_case():
    cost = [[1, 2, 3], [3, 1, 2], [2, 3, 1]]
    total, assignment = hungarian_min_cost(cost)
    assert total == 3  # 对角线
    assert assignment == [0, 1, 2]
    # 含禁止边
    cost = [[0, FORBIDDEN, 1], [FORBIDDEN, 0, 1], [1, 1, 0]]
    total, assignment = hungarian_min_cost(cost)
    assert total == 0
    assert assignment == [0, 1, 2]


# --------------------------------------------------------------- 唯一解
def test_unique_mapping_n3():
    left = [("L1", g(0, 0, 0, 0, 0, 0)),
            ("L2", g(1, 1, 1, 1, 1, 1)),
            ("L3", g(2, 2, 2, 2, 2, 2))]
    right = [("R1", g(0, 0, 0, 0, 0, 0)),
             ("R2", g(1, 1, 1, 1, 1, 1)),
             ("R3", g(2, 2, 2, 2, 2, 2))]
    r = check_against_bruteforce(left, right)
    assert r.optimum == 0
    assert all_resolved(r)
    assert r.candidates == {"L1": ["R1"], "L2": ["R2"], "L3": ["R3"]}


# --------------------------------------------------------------- 歧义
def test_ambiguous_candidates():
    # R1/R2 对 L1/L2 指纹完全相同 -> 2 个最优匹配，L1、L2 各有两个候选
    left = [("L1", g(0, 0, 0, 0, 0, 0)),
            ("L2", g(0, 0, 0, 0, 0, 0)),
            ("L3", g(2, 2, 2, 2, 2, 2))]
    right = [("R1", g(0, 0, 0, 0, 0, 0)),
             ("R2", g(0, 0, 0, 0, 0, 0)),
             ("R3", g(2, 2, 2, 2, 2, 2))]
    r = check_against_bruteforce(left, right)
    assert not all_resolved(r)
    assert set(r.candidates["L1"]) == {"R1", "R2"}
    assert set(r.candidates["L2"]) == {"R1", "R2"}
    assert r.candidates["L3"] == ["R3"]


def test_lock_resolves_ambiguity_and_keeps_optimum():
    left = [("L1", g(0, 0, 0, 0, 0, 0)),
            ("L2", g(0, 0, 0, 0, 0, 0)),
            ("L3", g(2, 2, 2, 2, 2, 2))]
    right = [("R1", g(0, 0, 0, 0, 0, 0)),
             ("R2", g(0, 0, 0, 0, 0, 0)),
             ("R3", g(2, 2, 2, 2, 2, 2))]
    r = analyze(left, right, LOCI, {"L1": "R2"})
    assert r.feasible and r.forced_total == 0
    assert r.candidates["L1"] == ["R2"]
    assert r.candidates["L2"] == ["R1"]
    assert r.candidates["L3"] == ["R3"]
    assert all_resolved(r)


def test_lock_rejected_when_infeasible():
    # L2 与 R1 六位点全不同（不可配）；锁 L1-R2 后 L2 只剩 R1 -> 无解
    left = [("L1", g(0, 0, 0, 0, 0, 0)),
            ("L2", g(1, 1, 1, 1, 1, 1))]
    right = [("R1", g(0, 0, 0, 0, 0, 0)),
             ("R2", g(1, 1, 1, 1, 1, 0))]  # 与 L1 异1 可配；与 L2 异1 可配
    base = analyze(left, right, LOCI)
    assert base.optimum == 1  # L1-R1(0) + L2-R2(1)
    r = analyze(left, right, LOCI, {"L1": "R2"})
    assert not r.feasible
    assert "锁定" in r.reason


def test_lock_rejected_when_raises_cost():
    # 两个完美匹配都合法：L1-R1+L2-R2 代价 0；L1-R2+L2-R1 代价 2。
    # 锁 L1-R2 后仍有完美匹配，但最优值由 0 升到 2 -> 必须拒绝。
    # n=3：强制 L1-R2 后仍存在完美匹配，但总代价高于原始最优值。
    # n=2 且两条完美匹配都合法，但代价不同（1 vs 2）：
    # 边代价 L1-R1=1、L1-R2=0、L2-R1=1、L2-R2=1
    left = [("L1", g(0, "x", 1, "x", 2, "x")),
            ("L2", g("x", 1, 1, "x", 2, "x"))]
    right = [("R1", g(1, 0, 1, "x", 2, 2)),
             ("R2", g(0, 2, 1, "x", 2, 2))]
    base = analyze(left, right, LOCI)
    assert base.optimum == 1  # L1-R2(0)+L2-R1(1)
    assert base.candidates["L1"] == ["R2"]
    # 强制 L1-R1(1) 后仍有完美匹配 L2-R2(1)，总代价 2 > 1 -> 拒绝
    r = analyze(left, right, LOCI, {"L1": "R1"})
    assert not r.feasible
    assert "最优性" in r.reason
    assert "由最优值 1 上升为 2" in r.reason


def test_lock_conflict_two_left_one_right():
    left = [("L1", g(0, 0, 0, 0, 0, 0)),
            ("L2", g(0, 0, 0, 0, 0, 0))]
    right = [("R1", g(0, 0, 0, 0, 0, 0)),
             ("R2", g(0, 0, 0, 0, 0, 0))]
    r = analyze(left, right, LOCI, {"L1": "R1", "L2": "R1"})
    assert not r.feasible


# --------------------------------------------------------------- 无解
def test_no_solution_isolated_sample():
    left = [("L1", g(0, 0, 0, 0, 0, 0)),
            ("L2", g(1, 1, 1, 1, 1, 1))]
    right = [("R1", g(0, 0, 0, 0, 0, 0)),
             # R2 与 L1 完全相同、与 L2 六位点全不同 -> L2 无任何可配对象
             ("R2", g(0, 0, 0, 0, 0, 0))]
    r = analyze(left, right, LOCI)
    assert not r.feasible
    assert "L2" in r.reason
    assert r.blocked_left == ["L2"]


def test_no_solution_too_few_shared():
    # 每个右侧只与对应左侧共享 2 位点
    left = [("L1", g(0, 1, "x", "x", "x", "x")),
            ("L2", g(1, 0, "x", "x", "x", "x"))]
    right = [("R1", g(0, 1, "x", "x", "x", "x")),
             ("R2", g(1, 0, "x", "x", "x", "x"))]
    r = analyze(left, right, LOCI)
    assert not r.feasible


# --------------------------------------------------------------- 缺失值与最优值
def test_missing_values_still_match():
    left = [("L1", g(0, 0, 0, "x", "x", "x")),
            ("L2", g(2, 2, 2, 2, 2, 2))]
    right = [("R1", g(0, 0, 0, 1, 1, 1)),   # 共 3 位点全一致
             ("R2", g(2, 2, 2, 2, 2, 2))]
    r = analyze(left, right, LOCI)
    assert r.feasible and r.optimum == 0
    assert r.candidates["L1"] == ["R1"]


def test_minimum_mismatch_preferred():
    # 两个完美匹配都合法，一个代价 1 一个代价 2，候选只包含代价 1 匹配中的边
    left = [("L1", g(0, 0, 0, 0, 0, 0)),
            ("L2", g(1, 1, 1, 1, 1, 1))]
    right = [("R1", g(0, 0, 0, 0, 0, 1)),   # 与 L1 异1，与 L2 异5(不可配)
             ("R2", g(1, 1, 1, 1, 1, 1))]
    r = check_against_bruteforce(left, right)
    assert r.optimum == 1
    assert r.candidates["L1"] == ["R1"]
    assert r.candidates["L2"] == ["R2"]


# --------------------------------------------------------------- 随机对照
def test_random_cases_against_bruteforce():
    import random
    random.seed(20261117)
    alleles = [0, 1, 2, None]
    for trial in range(60):
        n = random.randint(1, 6)
        left = [(f"L{i}", [random.choice(alleles) for _ in range(6)])
                for i in range(n)]
        right = [(f"R{i}", [random.choice(alleles) for _ in range(6)])
                 for i in range(n)]
        check_against_bruteforce(left, right)
        # 随机加一把锁再对照
        if n >= 2:
            lid = random.choice(left)[0]
            rid = random.choice(right)[0]
            check_against_bruteforce(left, right, {lid: rid})
