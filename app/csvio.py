"""CSV 上传文件解析与校验。

文件格式（UTF-8，带表头，逗号分隔）：
    sample_id,SNP1,SNP2,...,SNP6
    S01,0,1,2,,0,1
    ...

约束：
- 1～12 个样本；样本号非空且在文件内唯一；
- 恰好 6 个 SNP 位点列，两份文件位点名称与顺序完全相同；
- 位点取值只能是 0、1、2 或空（空字符串/纯空格，表示缺失）；
- 两份文件样本数必须相同。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import List, Optional, Tuple

LOCI_COUNT = 6
MIN_SAMPLES = 1
MAX_SAMPLES = 12
VALID_GENOTYPES = {"0", "1", "2"}


@dataclass
class ParsedCSV:
    sample_ids: List[str]
    loci: List[str]
    # 与 sample_ids 对齐；每个元素是长度 6 的列表，元素为 0/1/2 或 None
    genotypes: List[List[Optional[int]]]


def parse_snp_csv(raw: bytes, side_label: str) -> Tuple[Optional[ParsedCSV], List[str]]:
    """解析一份 SNP CSV。

    返回 (ParsedCSV 或 None, 错误信息列表)。有错时 ParsedCSV 为 None。
    """
    errors: List[str] = []
    if raw is None or len(raw) == 0:
        return None, [f"{side_label}：文件为空"]

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, [f"{side_label}：文件不是有效的 UTF-8 编码"]

    reader = csv.reader(io.StringIO(text))
    try:
        rows = [row for row in reader]
    except csv.Error as exc:
        return None, [f"{side_label}：CSV 解析失败（{exc}）"]

    # 去掉完全空白的行
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        return None, [f"{side_label}：文件只有空白内容"]

    header = [c.strip() for c in rows[0]]
    if len(header) < 2:
        return None, [f"{side_label}：表头至少需要 sample_id 列和 SNP 位点列"]

    id_col = header[0].lower()
    if id_col not in ("sample_id", "sampleid", "id", "样本号", "样本编号"):
        errors.append(
            f"{side_label}：表头第一列应为 sample_id，实际为 '{header[0]}'"
        )
    loci = header[1:]
    if len(loci) != LOCI_COUNT:
        errors.append(
            f"{side_label}：需要恰好 {LOCI_COUNT} 个 SNP 位点列，实际为 {len(loci)} 列"
        )

    data_rows = rows[1:]
    if not (MIN_SAMPLES <= len(data_rows) <= MAX_SAMPLES):
        errors.append(
            f"{side_label}：样本数须在 {MIN_SAMPLES}～{MAX_SAMPLES} 之间，实际为 {len(data_rows)}"
        )

    sample_ids: List[str] = []
    genotypes: List[List[Optional[int]]] = []
    seen_ids = set()
    expected_width = len(header)
    for line_no, row in enumerate(data_rows, start=2):
        if len(row) != expected_width:
            errors.append(
                f"{side_label}：第 {line_no} 行列数为 {len(row)}，与表头 {expected_width} 列不一致"
            )
            continue
        sid = row[0].strip()
        if not sid:
            errors.append(f"{side_label}：第 {line_no} 行样本号为空")
            continue
        if sid in seen_ids:
            errors.append(f"{side_label}：样本号 '{sid}' 重复，文件内样本号必须唯一")
            continue
        seen_ids.add(sid)

        vals: List[Optional[int]] = []
        for k, cell in enumerate(row[1:], start=1):
            token = cell.strip()
            if token == "":
                vals.append(None)
            elif token in VALID_GENOTYPES:
                vals.append(int(token))
            else:
                errors.append(
                    f"{side_label}：样本 '{sid}' 的第 {k} 个位点值 '{token}' 非法，"
                    "只允许 0、1、2 或空"
                )
                vals.append(None)
        if len(loci) == LOCI_COUNT:
            sample_ids.append(sid)
            genotypes.append(vals)

    if errors:
        return None, errors
    return ParsedCSV(sample_ids=sample_ids, loci=loci, genotypes=genotypes), []


def validate_pair(left: ParsedCSV, right: ParsedCSV
                   ) -> Tuple[List[Tuple[str, List[Optional[int]]]],
                              List[Tuple[str, List[Optional[int]]]],
                              List[str]]:
    """对两份已解析文件做跨文件校验，返回引擎所需的样本元组与错误列表。"""
    errors: List[str] = []
    if left.loci != right.loci:
        errors.append(
            "两份文件的 SNP 位点名称或顺序不一致：左侧 %s；右侧 %s"
            % (",".join(left.loci), ",".join(right.loci))
        )
    if len(left.sample_ids) != len(right.sample_ids):
        errors.append(
            "两份文件样本数不一致：左侧 %d 个，右侧 %d 个"
            % (len(left.sample_ids), len(right.sample_ids))
        )
    overlap = set(left.sample_ids) & set(right.sample_ids)
    if overlap:
        errors.append(
            "两份文件出现相同样本号，匿名侧与院内侧编号空间应不同：" + "、".join(sorted(overlap))
        )
    left_samples = list(zip(left.sample_ids, left.genotypes))
    right_samples = list(zip(right.sample_ids, right.genotypes))
    return left_samples, right_samples, errors
