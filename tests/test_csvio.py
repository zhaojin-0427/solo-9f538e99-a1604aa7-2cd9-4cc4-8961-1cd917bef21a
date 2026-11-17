"""CSV 解析与跨文件校验测试。"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.csvio import parse_snp_csv, validate_pair

HEADER = "sample_id,SNP1,SNP2,SNP3,SNP4,SNP5,SNP6\n"


def test_parse_ok_with_missing():
    raw = (HEADER + "S01,0,1,2,,0,1\nS02,2,2,2,0,0,\n").encode("utf-8")
    parsed, errors = parse_snp_csv(raw, "左侧")
    assert not errors
    assert parsed.sample_ids == ["S01", "S02"]
    assert parsed.loci == [f"SNP{i}" for i in range(1, 7)]
    assert parsed.genotypes[0] == [0, 1, 2, None, 0, 1]
    assert parsed.genotypes[1] == [2, 2, 2, 0, 0, None]


def test_parse_utf8_bom():
    raw = ("﻿" + HEADER + "S01,0,0,0,0,0,0\n").encode("utf-8")
    parsed, errors = parse_snp_csv(raw, "左侧")
    assert not errors and parsed.sample_ids == ["S01"]


def test_parse_bad_encoding():
    parsed, errors = parse_snp_csv("样本号,0\n".encode("gbk"), "左侧")
    assert parsed is None and any("UTF-8" in e for e in errors)


def test_parse_illegal_value():
    raw = (HEADER + "S01,0,3,0,0,0,0\n").encode()
    parsed, errors = parse_snp_csv(raw, "左侧")
    assert parsed is None and any("非法" in e for e in errors)


def test_parse_duplicate_id():
    raw = (HEADER + "S01,0,0,0,0,0,0\nS01,1,1,1,1,1,1\n").encode()
    parsed, errors = parse_snp_csv(raw, "左侧")
    assert any("重复" in e for e in errors)


def test_parse_wrong_locus_count():
    raw = ("sample_id,SNP1,SNP2,SNP3,SNP4,SNP5\nS01,0,0,0,0,0\n").encode()
    parsed, errors = parse_snp_csv(raw, "左侧")
    assert any("6 个 SNP" in e for e in errors)


def test_parse_sample_limits():
    raw = HEADER.encode()
    parsed, errors = parse_snp_csv(raw, "左侧")
    assert any("1～12" in e for e in errors)
    raw = HEADER + "".join(f"S{i:02d},0,0,0,0,0,0\n" for i in range(13))
    parsed, errors = parse_snp_csv(raw.encode(), "左侧")
    assert any("1～12" in e for e in errors)


def test_parse_ragged_row():
    raw = (HEADER + "S01,0,0,0\n").encode()
    parsed, errors = parse_snp_csv(raw, "左侧")
    assert any("列数" in e for e in errors)


def test_validate_pair_loci_mismatch():
    a, _ = parse_snp_csv((HEADER + "S01,0,0,0,0,0,0\n").encode(), "左")
    h2 = "sample_id,A,B,C,D,E,F\nS01,0,0,0,0,0,0\n"
    b, _ = parse_snp_csv(h2.encode(), "右")
    _, _, errors = validate_pair(a, b)
    assert any("位点" in e for e in errors)


def test_validate_pair_count_mismatch():
    a, _ = parse_snp_csv((HEADER + "S01,0,0,0,0,0,0\n").encode(), "左")
    b, _ = parse_snp_csv(
        (HEADER + "T01,0,0,0,0,0,0\nT02,1,1,1,1,1,1\n").encode(), "右")
    _, _, errors = validate_pair(a, b)
    assert any("样本数不一致" in e for e in errors)


def test_validate_pair_overlap_ids():
    a, _ = parse_snp_csv((HEADER + "S01,0,0,0,0,0,0\n").encode(), "左")
    b, _ = parse_snp_csv((HEADER + "S01,1,1,1,1,1,1\n").encode(), "右")
    _, _, errors = validate_pair(a, b)
    assert any("相同样本号" in e for e in errors)
