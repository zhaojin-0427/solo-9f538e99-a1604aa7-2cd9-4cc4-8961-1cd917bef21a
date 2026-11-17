"""Flask 应用：SNP 指纹匿名样本核对网页。

路由
----
GET  /                     任务列表
GET  /upload               上传表单
POST /upload               上传两份 CSV，校验后建任务
GET  /datasets/<id>        核对详情（候选、锁定、修订号、供数记录）
POST /datasets/<id>/lock   按外部证据锁定候选（带修订号乐观并发控制）
POST /datasets/<id>/unlock 解除锁定
POST /datasets/<id>/deliver 按交接编号生成对照 CSV 并登记不可变供数记录
GET  /datasets/<id>/deliveries/<delivery_id>          查看某次供数的原始对照
GET  /datasets/<id>/deliveries/<delivery_id>/download 取回该次供数的原文件
GET  /datasets/<id>/download 旧链接：改为引导到交接编号登记，不直接供数
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, url_for)

from . import csvio
from .matching import all_resolved, analyze
from .storage import Store

MAX_HANDOVER_NO = 64


def build_mapping_csv(result) -> Tuple[List[Tuple[str, str, int, int]], bytes]:
    """把“全部唯一”的分析结果固化为对照行与 CSV 字节。"""
    rows: List[Tuple[str, str, int, int]] = []
    buf = io.StringIO()
    buf.write("left_sample_id,right_sample_id,mismatched_loci,shared_loci\n")
    for lid in result.left_ids:
        rid = result.candidates[lid][0]
        info = result.pair_infos[(lid, rid)]
        rows.append((lid, rid, info.mismatches, info.shared))
        buf.write("%s,%s,%d,%d\n" % (lid, rid, info.mismatches, info.shared))
    return rows, buf.getvalue().encode("utf-8-sig")


def delivery_status(mapping_json: str, current: Dict[str, str]) -> str:
    """按当前完整对应判定历史供数记录的状态。

    valid      当前仍有唯一完整对应，且与该记录完全一致
    recheck    当前没有唯一对应（存在歧义或无解），需人工复核
    withdraw   当前对应已唯一但与记录不同，旧文件应撤回
    """
    if not current:
        return "recheck"
    recorded = {row[0]: row[1] for row in json.loads(mapping_json)}
    if recorded == current:
        return "valid"
    return "withdraw"


def current_mapping(result) -> Dict[str, str]:
    """当前唯一完整对应；不唯一或无解时返回空字典。"""
    if not all_resolved(result):
        return {}
    return {lid: result.candidates[lid][0] for lid in result.left_ids}


def create_app(db_path: Optional[str] = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("APP_SECRET_KEY", "dev-only-change-me")
    # 每份 CSV 最多 12 行，2MB 足够且可防止超大上传耗尽内存
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    db_path = db_path or os.environ.get(
        "APP_DB_PATH", os.path.join(os.getcwd(), "data", "snp.db")
    )
    store = Store(db_path)
    app.extensions["store"] = store

    # ----------------------------------------------------------- 工具函数
    def load_context(dataset_id: int) -> Dict[str, Any]:
        # 只读快照：数据集、样本、锁定、供数记录与状态来自同一个已提交状态，
        # 页面不会出现“新锁定 + 旧供数记录”这类拼接。
        with store.read_snapshot() as conn:
            row = store.get_dataset_in(conn, dataset_id)
            if row is None:
                abort(404)
            left_samples = store.get_samples_in(conn, dataset_id, "left")
            right_samples = store.get_samples_in(conn, dataset_id, "right")
            locks = store.get_locks_in(conn, dataset_id)
            delivery_rows = store.list_deliveries_in(conn, dataset_id)
        loci = json.loads(row["loci_json"])
        result = analyze(left_samples, right_samples, loci, locks)
        current = current_mapping(result)
        deliveries = []
        for d in delivery_rows:
            deliveries.append({
                "row": d,
                "mapping": json.loads(d["mapping_json"]),
                "status": delivery_status(d["mapping_json"], current),
            })
        return {
            "row": row,
            "left_samples": left_samples,
            "right_samples": right_samples,
            "locks": locks,
            "loci": loci,
            "result": result,
            "all_resolved": all_resolved(result),
            "deliveries": deliveries,
        }

    def conflict(message: str, dataset_id: int):
        return render_template(
            "conflict.html", message=message, dataset_id=dataset_id
        ), 409

    # --------------------------------------------------------------- 首页
    @app.route("/")
    def index():
        return render_template("index.html", datasets=store.list_datasets())

    # --------------------------------------------------------------- 上传
    @app.route("/upload", methods=["GET"])
    def upload_form():
        return render_template("upload.html")

    @app.route("/upload", methods=["POST"])
    def upload_submit():
        left_file = request.files.get("left_csv")
        right_file = request.files.get("right_csv")
        left_label = (request.form.get("left_label") or "匿名侧（测序结果）").strip()
        right_label = (request.form.get("right_label") or "院内侧（库存编号）").strip()

        if left_file is None or right_file is None:
            flash("请同时选择两份 CSV 文件后再上传。", "error")
            return render_template("upload.html"), 400
        left_raw = left_file.read()
        right_raw = right_file.read()

        left_parsed, left_errors = csvio.parse_snp_csv(left_raw, left_label)
        right_parsed, right_errors = csvio.parse_snp_csv(right_raw, right_label)
        errors = left_errors + right_errors
        if errors:
            for msg in errors:
                flash(msg, "error")
            return render_template("upload.html"), 400

        left_samples, right_samples, pair_errors = csvio.validate_pair(
            left_parsed, right_parsed
        )
        if pair_errors:
            for msg in pair_errors:
                flash(msg, "error")
            return render_template("upload.html"), 400

        # 计算在数据库之外完成；任何异常都不会产生半成品数据
        result = analyze(left_samples, right_samples, left_parsed.loci)
        dataset_id = store.create_dataset(
            left_label=left_label,
            right_label=right_label,
            loci=left_parsed.loci,
            left_samples=left_samples,
            right_samples=right_samples,
            optimum=result.optimum,
            feasible=result.feasible,
            reason=result.reason,
        )
        if result.feasible:
            flash(
                "上传成功：最优不一致位点总数为 %d。请逐样本核对候选并按外部证据消歧。"
                % result.optimum,
                "info",
            )
        else:
            flash("上传成功，但该批数据无解：%s" % result.reason, "error")
        return redirect(url_for("dataset_detail", dataset_id=dataset_id))

    # --------------------------------------------------------------- 详情
    @app.route("/datasets/<int:dataset_id>")
    def dataset_detail(dataset_id: int):
        ctx = load_context(dataset_id)
        result = ctx["result"]
        # 供模板渲染的候选详情：left_id -> [{right_id, shared, mismatches, locked}]
        candidate_details: Dict[str, List[Dict[str, Any]]] = {}
        for lid in result.left_ids:
            items = []
            for rid in result.candidates.get(lid, []):
                info = result.pair_infos[(lid, rid)]
                items.append({
                    "right_id": rid,
                    "shared": info.shared,
                    "mismatches": info.mismatches,
                    "locked": ctx["locks"].get(lid) == rid,
                })
            candidate_details[lid] = items
        ctx["candidate_details"] = candidate_details
        return render_template("dataset.html", **ctx)

    # --------------------------------------------------------------- 锁定
    @app.route("/datasets/<int:dataset_id>/lock", methods=["POST"])
    def lock_candidate(dataset_id: int):
        left_id = (request.form.get("left_id") or "").strip()
        right_id = (request.form.get("right_id") or "").strip()
        try:
            seen_revision = int(request.form.get("revision", ""))
        except (TypeError, ValueError):
            return conflict("表单缺少修订号，请刷新页面后重试。", dataset_id)

        # 整个“校验 + 写入”放在同一个 IMMEDIATE 事务里，失败即回滚
        try:
            with store.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
                ).fetchone()
                if row is None:
                    abort(404)
                if row["revision"] != seen_revision:
                    return conflict(
                        "页面修订号已过期（您看到的是 r%d，当前为 r%d），"
                        "可能有其他人刚提交了锁定。请刷新页面查看最新判断后再操作。"
                        % (seen_revision, row["revision"]),
                        dataset_id,
                    )

                locks = {
                    r["left_id"]: r["right_id"]
                    for r in conn.execute(
                        "SELECT left_id, right_id FROM locks WHERE dataset_id = ?",
                        (dataset_id,),
                    ).fetchall()
                }
                left_samples = [
                    (r["sample_id"], json.loads(r["genotype_json"]))
                    for r in conn.execute(
                        "SELECT sample_id, genotype_json FROM samples "
                        "WHERE dataset_id=? AND side='left' ORDER BY idx",
                        (dataset_id,),
                    ).fetchall()
                ]
                right_samples = [
                    (r["sample_id"], json.loads(r["genotype_json"]))
                    for r in conn.execute(
                        "SELECT sample_id, genotype_json FROM samples "
                        "WHERE dataset_id=? AND side='right' ORDER BY idx",
                        (dataset_id,),
                    ).fetchall()
                ]
                loci = json.loads(row["loci_json"])

                left_ids = [s[0] for s in left_samples]
                right_ids = [s[0] for s in right_samples]
                if left_id not in left_ids or right_id not in right_ids:
                    flash("锁定失败：样本号不存在于本任务中。", "error")
                    return redirect(url_for("dataset_detail",
                                            dataset_id=dataset_id))
                if left_id in locks:
                    flash(
                        "锁定失败：%s 已锁定到 %s，请先解除原锁定再改选。"
                        % (left_id, locks[left_id]),
                        "error",
                    )
                    return redirect(url_for("dataset_detail",
                                            dataset_id=dataset_id))
                used_rights = set(locks.values())
                if right_id in used_rights:
                    flash(
                        "锁定失败：%s 已被其他样本锁定，一对一对应不允许共用。"
                        % right_id,
                        "error",
                    )
                    return redirect(url_for("dataset_detail",
                                            dataset_id=dataset_id))

                trial_locks = dict(locks)
                trial_locks[left_id] = right_id
                result = analyze(left_samples, right_samples, loci, trial_locks)
                base_optimum = row["optimum"]

                if not result.feasible or result.forced_total is None:
                    flash("锁定被拒绝：%s。已有锁定保持不变。"
                          % (result.reason or "无法构成合法匹配"), "error")
                    return redirect(url_for("dataset_detail",
                                            dataset_id=dataset_id))
                if result.forced_total != base_optimum:
                    flash(
                        "锁定被拒绝：该选择会使不一致位点总数偏离原始最优值"
                        "（最优 %d，当前选择将达 %d）。已有锁定保持不变。"
                        % (base_optimum, result.forced_total),
                        "error",
                    )
                    return redirect(url_for("dataset_detail",
                                            dataset_id=dataset_id))

                conn.execute(
                    "INSERT INTO locks (dataset_id, left_id, right_id, locked_at) "
                    "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S','now'))",
                    (dataset_id, left_id, right_id),
                )
                conn.execute(
                    "UPDATE datasets SET revision = revision + 1 WHERE id = ?",
                    (dataset_id,),
                )
        except sqlite3.Error as exc:
            flash("数据库保存失败，已有判断未被改变：%s" % exc, "error")
            return redirect(url_for("dataset_detail", dataset_id=dataset_id))

        flash("已锁定 %s ↔ %s，其余样本候选已按最优约束重新计算。"
              % (left_id, right_id), "info")
        return redirect(url_for("dataset_detail", dataset_id=dataset_id))

    # --------------------------------------------------------------- 解锁
    @app.route("/datasets/<int:dataset_id>/unlock", methods=["POST"])
    def unlock_candidate(dataset_id: int):
        left_id = (request.form.get("left_id") or "").strip()
        try:
            seen_revision = int(request.form.get("revision", ""))
        except (TypeError, ValueError):
            return conflict("表单缺少修订号，请刷新页面后重试。", dataset_id)

        try:
            with store.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
                ).fetchone()
                if row is None:
                    abort(404)
                if row["revision"] != seen_revision:
                    return conflict(
                        "页面修订号已过期（您看到的是 r%d，当前为 r%d），"
                        "请刷新页面后再解除锁定。"
                        % (seen_revision, row["revision"]),
                        dataset_id,
                    )
                cur = conn.execute(
                    "DELETE FROM locks WHERE dataset_id = ? AND left_id = ?",
                    (dataset_id, left_id),
                )
                if cur.rowcount == 0:
                    flash("该样本本就没有锁定，无需解除。", "error")
                    return redirect(url_for("dataset_detail",
                                            dataset_id=dataset_id))
                conn.execute(
                    "UPDATE datasets SET revision = revision + 1 WHERE id = ?",
                    (dataset_id,),
                )
        except sqlite3.Error as exc:
            flash("数据库保存失败，已有判断未被改变：%s" % exc, "error")
            return redirect(url_for("dataset_detail", dataset_id=dataset_id))

        flash("已解除 %s 的锁定，候选已重新计算。" % left_id, "info")
        return redirect(url_for("dataset_detail", dataset_id=dataset_id))

    # --------------------------------------------------------------- 下载
    @app.route("/datasets/<int:dataset_id>/download")
    def download_mapping(dataset_id: int):
        """旧版直链：供数必须经交接编号登记，这里只做引导，不产出文件。"""
        ctx = load_context(dataset_id)
        if not all_resolved(ctx["result"]):
            flash("尚未全部唯一，不能供数；请先完成消歧后按交接编号登记。", "error")
        else:
            flash("请填写交接编号并登记供数记录后，再取回对应文件。", "info")
        return redirect(url_for("dataset_detail", dataset_id=dataset_id))

    # --------------------------------------------------------------- 供数
    def delivery_filename(dataset_id: int, handover_no: str) -> str:
        safe = "".join(
            ch if (ch.isalnum() or ch in "-_.") else "_" for ch in handover_no
        )
        return "mapping_dataset_%d_%s.csv" % (dataset_id, safe)

    @app.route("/datasets/<int:dataset_id>/deliver", methods=["POST"])
    def deliver_mapping(dataset_id: int):
        handover_no = (request.form.get("handover_no") or "").strip()
        try:
            seen_revision = int(request.form.get("revision", ""))
        except (TypeError, ValueError):
            return conflict("表单缺少修订号，请刷新页面后重试。", dataset_id)

        if not handover_no:
            flash("供数失败：请填写交接编号。", "error")
            return redirect(url_for("dataset_detail", dataset_id=dataset_id))
        if len(handover_no) > MAX_HANDOVER_NO:
            flash("供数失败：交接编号不能超过 %d 个字符。" % MAX_HANDOVER_NO,
                  "error")
            return redirect(url_for("dataset_detail", dataset_id=dataset_id))

        # 读取当前状态、校验唯一性、固化快照、登记记录、推进修订号全部在
        # 同一个 IMMEDIATE 事务内完成：文件、记录与修订号来自同一已提交状态，
        # 期间任何锁定/解锁都无法插队。
        try:
            with store.transaction() as conn:
                row = store.get_dataset_in(conn, dataset_id)
                if row is None:
                    abort(404)

                # 幂等优先：同一任务内同一交接编号已登记时，无条件取回原记录，
                # 即使页面修订号已过期也不重新生成、不覆盖。
                existing = store.get_delivery_in(conn, dataset_id, handover_no)
                if existing is not None:
                    flash(
                        "交接编号 %s 已于 %s（基于修订号 r%d）登记，返回原记录与原文件，"
                        "未做任何覆盖。"
                        % (handover_no, existing["created_at"],
                           existing["revision"]),
                        "info",
                    )
                    return redirect(url_for(
                        "delivery_detail", dataset_id=dataset_id,
                        delivery_id=existing["id"],
                    ))

                if row["revision"] != seen_revision:
                    return conflict(
                        "页面修订号已过期（您看到的是 r%d，当前为 r%d），"
                        "可能有其他人刚提交了锁定或供数。请刷新页面确认当前对应后再登记。"
                        % (seen_revision, row["revision"]),
                        dataset_id,
                    )

                left_samples = store.get_samples_in(conn, dataset_id, "left")
                right_samples = store.get_samples_in(conn, dataset_id, "right")
                locks = store.get_locks_in(conn, dataset_id)
                result = analyze(left_samples, right_samples,
                                 json.loads(row["loci_json"]), locks)

                if not result.feasible:
                    flash("供数失败：任务当前无解（%s），未生成文件，也未登记记录。"
                          % result.reason, "error")
                    return redirect(url_for("dataset_detail",
                                            dataset_id=dataset_id))
                if not all_resolved(result):
                    ambiguous = [lid for lid, c in result.candidates.items()
                                 if len(c) != 1]
                    flash(
                        "供数失败：仍有 %d 个样本不唯一（%s），按规定不得供数；"
                        "未生成文件，也未登记记录。"
                        % (len(ambiguous), "、".join(ambiguous)),
                        "error",
                    )
                    return redirect(url_for("dataset_detail",
                                            dataset_id=dataset_id))

                mapping, csv_content = build_mapping_csv(result)
                delivery_id = store.add_delivery(
                    conn, dataset_id, handover_no, row["revision"],
                    mapping, csv_content,
                )
                store.bump_revision(conn, dataset_id)
        except sqlite3.IntegrityError:
            # 并发下同一交接编号被另一请求抢先登记：取回先提交的那份
            existing = store.get_delivery(dataset_id, handover_no)
            if existing is None:
                flash("供数登记失败，未生成文件；请刷新页面后重试。", "error")
                return redirect(url_for("dataset_detail", dataset_id=dataset_id))
            flash("交接编号 %s 已被并发登记，返回原记录与原文件。" % handover_no,
                  "info")
            return redirect(url_for(
                "delivery_detail", dataset_id=dataset_id,
                delivery_id=existing["id"],
            ))
        except sqlite3.Error as exc:
            flash("供数登记失败，未生成文件，已有判断与记录均未改变：%s" % exc,
                  "error")
            return redirect(url_for("dataset_detail", dataset_id=dataset_id))

        flash(
            "已按交接编号 %s 生成对照文件并登记供数记录（基于修订号 r%d）。"
            % (handover_no, seen_revision),
            "info",
        )
        return redirect(url_for(
            "delivery_detail", dataset_id=dataset_id, delivery_id=delivery_id,
        ))

    @app.route("/datasets/<int:dataset_id>/deliveries/<int:delivery_id>")
    def delivery_detail(dataset_id: int, delivery_id: int):
        ctx = load_context(dataset_id)
        rec = store.get_delivery_by_id(dataset_id, delivery_id)
        if rec is None:
            abort(404)
        mapping = json.loads(rec["mapping_json"])
        # 原始对照按记录固化内容展示，不受当前锁定变化影响
        rows = []
        for lid, rid, mismatches, shared in mapping:
            rows.append({
                "left_id": lid,
                "right_id": rid,
                "mismatches": mismatches,
                "shared": shared,
                "current": ctx["result"].candidates.get(lid, []),
            })
        return render_template(
            "delivery.html",
            row=ctx["row"],
            delivery=rec,
            mapping=rows,
            status=delivery_status(rec["mapping_json"],
                                   current_mapping(ctx["result"])),
            filename=delivery_filename(dataset_id, rec["handover_no"]),
        )

    @app.route("/datasets/<int:dataset_id>/deliveries/<int:delivery_id>/download")
    def delivery_download(dataset_id: int, delivery_id: int):
        rec = store.get_delivery_by_id(dataset_id, delivery_id)
        if rec is None:
            abort(404)
        return send_file(
            io.BytesIO(rec["csv_content"]),
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name=delivery_filename(dataset_id, rec["handover_no"]),
        )

    @app.errorhandler(404)
    def not_found(_err):
        return render_template("conflict.html",
                               message="任务不存在或已被删除。",
                               dataset_id=None), 404

    @app.errorhandler(413)
    def too_large(_err):
        flash("上传文件过大（单请求上限 2MB），请检查 CSV 内容。", "error")
        return render_template("upload.html"), 413

    return app
