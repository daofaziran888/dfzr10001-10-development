"""解析模板字段级诊断。

设计口径：
- 每个字段按「字段映射 → 类型转换 → 默认值 → 校验」四个阶段逐步诊断，任一阶段失败都保留
  现场值，错误字段的转换前值/转换后值成对出现，方便从出错字段继续调整。
- 诊断创建时对模板规格做快照留档；模板后续变更不影响历史诊断的解释，启动时若快照版本与
  当前启用版本不一致，只打「版本冲突」标记，原版本解释原样保留。
- 同一模板同一版本连续提交时只保留首个诊断，重复提交直接返回首条记录。
- 试运行（trial）诊断只落在诊断台账里，绝不写正式采集日志；正式采集（formal）才追加日志。
"""
from __future__ import annotations

import csv
import io
import json
import re
from copy import deepcopy
from datetime import datetime
from typing import Any

from app.store import store

PARSER_MODULE = "parser"
LOG_MODULE = "log"

MODE_TRIAL = "试运行"
MODE_FORMAL = "正式采集"

STATUS_PASS = "通过"
STATUS_FAIL = "失败"
STATUS_WARN = "警告"

DEFAULT_SOURCE_NONE = "无默认"
DEFAULT_SOURCE_TEMPLATE = "模板默认"
DEFAULT_SOURCE_SYSTEM = "系统兜底"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DiagnosisService:
    """诊断台账与修复待办都收在服务内存里，不与正式业务表混放。"""

    def __init__(self) -> None:
        self._diagnoses: list[dict[str, Any]] = []
        self._todos: list[dict[str, Any]] = []
        self._diag_seq = 0
        self._todo_seq = 0
        self._seed_archived_diagnoses()
        self.refresh_conflict_flags()

    # ------------------------------------------------------------------ 台账读取
    def list_diagnoses(self, *, template_id: int | None = None) -> list[dict[str, Any]]:
        rows = self._diagnoses
        if template_id is not None:
            rows = [row for row in rows if row["模板ID"] == template_id]
        return rows

    def grouped_overview(self, *, template_id: int | None = None) -> list[dict[str, Any]]:
        """概览按模板版本排列：同版本的诊断归到一组，组内按创建时间倒序。"""
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in self.list_diagnoses(template_id=template_id):
            groups.setdefault(row["模板版本快照"], []).append(row)
        result: list[dict[str, Any]] = []
        for version, rows in groups.items():
            rows = sorted(rows, key=lambda item: item["创建时间"], reverse=True)
            failed = sum(item["失败字段数"] for item in rows)
            result.append({
                "模板版本": version,
                "诊断条数": len(rows),
                "累计失败字段": failed,
                "存在版本冲突": any(item["版本冲突"] for item in rows),
                "诊断列表": [self._overview_item(item) for item in rows],
            })
        return sorted(result, key=lambda item: item["模板版本"])

    def get_diagnosis(self, diagnosis_id: str) -> dict[str, Any] | None:
        for row in self._diagnoses:
            if row["id"] == diagnosis_id:
                return row
        return None

    # ------------------------------------------------------------------ 诊断创建
    def create_diagnosis(
        self,
        template_id: int,
        sample_text: str,
        *,
        run_mode: str = MODE_TRIAL,
        related_task: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """对模板跑一次样本试解析。

        返回（诊断记录、提示信息、是否复用了历史诊断）。同版本连续提交时复用首个诊断。
        """
        entry = store.find(PARSER_MODULE, template_id)
        if entry is None:
            return None, f"解析模板 {template_id} 不存在或已归档", False
        if not str(sample_text or "").strip():
            return None, "样本内容为空，无法试解析", False
        if run_mode not in (MODE_TRIAL, MODE_FORMAL):
            return None, f"运行模式「{run_mode}」不支持，仅允许试运行/正式采集", False

        spec = self.resolve_spec(entry)
        version = str(spec.get("模板版本") or "未标注")

        existing = next(
            (row for row in self._diagnoses
             if row["模板ID"] == template_id and row["模板版本快照"] == version),
            None,
        )
        if existing is not None:
            return existing, f"模板 {version} 已存在诊断，同版本连续提交仅保留首个诊断", True

        record = self._build_record(
            entry=entry,
            spec=deepcopy(spec),
            sample_text=sample_text,
            run_mode=run_mode,
            related_task=related_task,
        )
        self._diagnoses.append(record)
        self._spawn_todos(record)
        if run_mode == MODE_FORMAL:
            self._append_formal_log(record)
        self.refresh_conflict_flags()
        return record, "诊断完成", False

    def rerun_field(
        self,
        diagnosis_id: str,
        field_name: str,
        *,
        raw_value: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """只重跑诊断里的单个字段，其余字段结果保持不变；解释口径沿用诊断时的版本快照。"""
        record = self.get_diagnosis(diagnosis_id)
        if record is None:
            return None, f"诊断 {diagnosis_id} 不存在或已归档"
        spec = record["模板快照"]
        field_spec = next((item for item in spec.get("字段定义", []) if item["字段名"] == field_name), None)
        if field_spec is None:
            return None, f"字段「{field_name}」不在该诊断的模板快照里"

        sample_record = self._first_record(record["样本原文"], spec.get("输入格式", "CSV"))
        if raw_value is not None:
            sample_record[field_spec.get("源字段", field_name)] = raw_value
        new_result = self._run_field(field_spec, sample_record)

        replaced = False
        for index, field_result in enumerate(record["字段结果"]):
            if field_result["字段名"] == field_name:
                record["字段结果"][index] = new_result
                replaced = True
                break
        if not replaced:
            record["字段结果"].append(new_result)

        self._recompute_summary(record)
        self._sync_todos_after_rerun(record, field_name, new_result)
        return record, f"字段「{field_name}」已按模板 {record['模板版本快照']} 快照重新诊断"

    # ------------------------------------------------------------------ 修复待办
    def list_todos(self, *, task_code: str | None = None) -> list[dict[str, Any]]:
        rows = self._todos
        if task_code:
            rows = [row for row in rows if row.get("关联任务") == task_code]
        return rows

    def todo_summary(self) -> dict[str, Any]:
        todos = self._todos
        return {
            "待修复": sum(1 for row in todos if row["状态"] == "待修复"),
            "已修复": sum(1 for row in todos if row["状态"] == "已修复"),
            "关联诊断数": len({row["诊断ID"] for row in todos}),
        }

    def run_todo_action(self, todo_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        todo = next((row for row in self._todos if row["id"] == todo_id), None)
        if todo is None:
            return None, f"修复待办 {todo_id} 不存在或已归档"
        if action == "标记已修复":
            todo["状态"] = "已修复"
        elif action == "重新打开":
            todo["状态"] = "待修复"
        else:
            return None, f"动作「{action}」不属于修复待办可执行范围"
        return todo, f"修复待办已{action}"

    # ------------------------------------------------------------------ 版本冲突
    def refresh_conflict_flags(self) -> None:
        """启动/新增诊断后对齐一次：快照版本与当前启用版本冲突时保留原版本解释，只打标记。"""
        for record in self._diagnoses:
            entry = store.find(PARSER_MODULE, record["模板ID"])
            current = self.resolve_spec(entry).get("模板版本") if entry else None
            record["当前启用版本"] = current
            record["版本冲突"] = bool(current and record["模板版本快照"] != current)

    # ------------------------------------------------------------------ 模板规格
    def resolve_spec(self, entry: dict[str, Any]) -> dict[str, Any]:
        spec = entry.get("spec")
        if isinstance(spec, dict) and spec.get("字段定义"):
            return spec
        # 旧模板没有结构化规格：按当时模板文本给一个兜底解释，不阻断诊断。
        return {
            "模板版本": entry.get("模板版本") or "未标注",
            "输入格式": entry.get("输入格式") or "文本",
            "输出格式": entry.get("输出格式") or "文本",
            "字段定义": [{
                "字段名": "原始记录",
                "源字段": "_raw",
                "目标类型": "string",
                "必填": False,
                "默认值": None,
                "校验正则": None,
                "校验规则": "旧版文本模板无结构化字段，按原样透传",
                "判定阈值": "无",
                "范围": "无",
                "标准": "旧模板留档口径",
            }],
        }

    # ------------------------------------------------------------------ 诊断管线
    def _run_field(self, field_spec: dict[str, Any], sample_record: dict[str, Any]) -> dict[str, Any]:
        name = field_spec["字段名"]
        source_key = field_spec.get("源字段", name)
        target_type = field_spec.get("目标类型", "string")
        found = source_key in sample_record and str(sample_record.get(source_key, "")).strip() != ""
        raw_value = sample_record.get(source_key) if found else None

        mapping_stage = {
            "源字段": source_key,
            "找到": found,
            "转换前值": raw_value,
        }

        default_stage = self._apply_default(field_spec, found, raw_value)
        effective_raw = raw_value if found else default_stage["生效值"]

        conversion_stage = self._convert(effective_raw, target_type, field_spec)
        validation_stage = self._validate(field_spec, conversion_stage, default_stage)

        stages = [mapping_stage, conversion_stage, validation_stage]
        if any(not stage.get("通过", True) for stage in stages):
            status = STATUS_FAIL
        elif default_stage["是否生效"] and default_stage["来源"] == DEFAULT_SOURCE_SYSTEM:
            status = STATUS_WARN
        else:
            status = STATUS_PASS

        return {
            "字段名": name,
            "源字段": source_key,
            "目标类型": target_type,
            "必填": bool(field_spec.get("必填", False)),
            "映射": mapping_stage,
            "转换": conversion_stage,
            "默认值": default_stage,
            "校验": validation_stage,
            "状态": status,
        }

    def _apply_default(self, field_spec: dict[str, Any], found: bool, raw_value: Any) -> dict[str, Any]:
        template_default = field_spec.get("默认值")
        stage = {
            "来源": DEFAULT_SOURCE_NONE,
            "默认值": template_default,
            "是否生效": False,
            "生效值": raw_value,
            "说明": "源字段存在，未触发默认值",
        }
        if found:
            return stage
        if template_default not in (None, ""):
            stage.update({
                "来源": DEFAULT_SOURCE_TEMPLATE,
                "是否生效": True,
                "生效值": template_default,
                "说明": f"源字段缺失，采用模板配置默认值 {template_default!r}",
            })
            return stage
        if field_spec.get("必填"):
            stage.update({
                "来源": DEFAULT_SOURCE_NONE,
                "说明": "必填字段缺失且模板未配置默认值",
            })
            return stage
        stage.update({
            "来源": DEFAULT_SOURCE_SYSTEM,
            "是否生效": True,
            "生效值": None,
            "说明": "可选字段缺失，系统兜底输出 null",
        })
        return stage

    def _convert(self, raw_value: Any, target_type: str, field_spec: dict[str, Any]) -> dict[str, Any]:
        stage = {
            "通过": True,
            "转换前值": raw_value,
            "转换后值": None,
            "目标类型": target_type,
            "错误信息": None,
        }
        if raw_value is None:
            if field_spec.get("必填"):
                stage["错误信息"] = "必填字段无可用输入值，类型转换未执行"
                stage["通过"] = False
            else:
                stage["通过"] = True
                stage["转换后值"] = None
                stage["错误信息"] = "可选字段无值，类型转换跳过，输出 null"
            return stage
        text = str(raw_value)
        try:
            if target_type == "string":
                stage["转换后值"] = text
            elif target_type == "int":
                stage["转换后值"] = int(text)
            elif target_type == "float":
                stage["转换后值"] = float(text)
            elif target_type == "boolean":
                if text.lower() in ("true", "1", "是", "y", "yes"):
                    stage["转换后值"] = True
                elif text.lower() in ("false", "0", "否", "n", "no"):
                    stage["转换后值"] = False
                else:
                    raise ValueError("布尔值仅接受 true/false、是/否、1/0")
            elif target_type == "date":
                fmt = field_spec.get("日期格式", "%Y-%m-%d")
                stage["转换后值"] = datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            else:
                raise ValueError(f"不支持的目标类型 {target_type}")
        except (ValueError, TypeError) as exc:
            stage["通过"] = False
            stage["转换后值"] = None
            stage["错误信息"] = (
                f"类型转换失败：原值 {text!r} 无法转换为目标类型 {target_type}（{exc}）"
            )
        return stage

    def _validate(self, field_spec: dict[str, Any], conversion: dict[str, Any], default: dict[str, Any]) -> dict[str, Any]:
        pattern = field_spec.get("校验正则")
        rule_text = field_spec.get("校验规则") or ("正则匹配" if pattern else "无额外校验")
        threshold_text = field_spec.get("判定阈值") or self._describe_threshold(field_spec, pattern)
        stage = {
            "通过": True,
            "规则": rule_text,
            "范围": field_spec.get("范围", "无"),
            "标准": field_spec.get("标准", "无"),
            "判定阈值": threshold_text,
            "正则": pattern,
            "说明": "",
        }
        if not conversion["通过"]:
            stage["通过"] = False
            stage["说明"] = "类型转换失败，未进入校验"
            return stage
        value = conversion["转换后值"]
        if value is None:
            if field_spec.get("必填"):
                stage["通过"] = False
                stage["说明"] = "必填字段为空，判定阈值：字段不得为 null"
            else:
                stage["说明"] = "可选字段为空，跳过校验"
            return stage

        if pattern:
            if not re.search(pattern, str(value)):
                stage["通过"] = False
                stage["说明"] = f"值 {value!r} 未匹配校验正则 {pattern}"
                return stage
        min_value = field_spec.get("最小值")
        max_value = field_spec.get("最大值")
        if min_value is not None or max_value is not None:
            numeric = value if isinstance(value, (int, float)) else self._to_number(value)
            if numeric is None:
                stage["通过"] = False
                stage["说明"] = f"值 {value!r} 无法按数值范围判定"
                return stage
            if min_value is not None and numeric < min_value:
                stage["通过"] = False
                stage["说明"] = f"值 {numeric} 低于下限阈值 {min_value}"
                return stage
            if max_value is not None and numeric > max_value:
                stage["通过"] = False
                stage["说明"] = f"值 {numeric} 超过上限阈值 {max_value}"
                return stage
        allowed = field_spec.get("允许值")
        if allowed and value not in allowed:
            stage["通过"] = False
            stage["说明"] = f"值 {value!r} 不在允许枚举 {allowed} 内"
            return stage
        date_min = field_spec.get("日期下限")
        date_max = field_spec.get("日期上限")
        if isinstance(value, str) and (date_min or date_max):
            if date_min and value < date_min:
                stage["通过"] = False
                stage["说明"] = f"日期 {value} 早于下限阈值 {date_min}"
                return stage
            if date_max and value > date_max:
                stage["通过"] = False
                stage["说明"] = f"日期 {value} 晚于上限阈值 {date_max}"
                return stage
        stage["说明"] = "校验通过"
        return stage

    @staticmethod
    def _describe_threshold(field_spec: dict[str, Any], pattern: Any) -> str:
        parts: list[str] = []
        if pattern:
            parts.append(f"正则 {pattern}")
        if field_spec.get("最小值") is not None or field_spec.get("最大值") is not None:
            parts.append(f"数值区间 [{field_spec.get('最小值', '-∞')}, {field_spec.get('最大值', '+∞')}]")
        if field_spec.get("允许值"):
            parts.append(f"允许枚举 {field_spec['允许值']}")
        if field_spec.get("日期下限") or field_spec.get("日期上限"):
            parts.append(f"日期区间 [{field_spec.get('日期下限', '-∞')}, {field_spec.get('日期上限', '+∞')}]")
        return "；".join(parts) or "无"

    @staticmethod
    def _to_number(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ 样本解析
    def _parse_input(self, sample_text: str, input_format: str) -> list[dict[str, Any]]:
        fmt = (input_format or "CSV").upper()
        if fmt == "JSON":
            data = json.loads(sample_text)
            if isinstance(data, dict):
                return [data]
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            raise ValueError("JSON 样本须为对象或对象数组")
        reader = csv.DictReader(io.StringIO(sample_text))
        return [dict(row) for row in reader]

    def _first_record(self, sample_text: str, input_format: str) -> dict[str, Any]:
        rows = self._parse_input(sample_text, input_format)
        if not rows:
            raise ValueError("样本里没有可解析的数据行")
        return rows[0]

    # ------------------------------------------------------------------ 记录组装
    def _build_record(
        self,
        *,
        entry: dict[str, Any],
        spec: dict[str, Any],
        sample_text: str,
        run_mode: str,
        related_task: str | None,
    ) -> dict[str, Any]:
        self._diag_seq += 1
        record = {
            "id": f"DIAG-{self._diag_seq:04d}",
            "模板ID": entry["id"],
            "模板编号": entry.get("模板编号"),
            "关联任务": related_task,
            "运行模式": run_mode,
            "样本原文": sample_text,
            "输入格式": spec.get("输入格式"),
            "输出格式": spec.get("输出格式"),
            "模板版本快照": spec.get("模板版本", "未标注"),
            "模板快照": spec,
            "当前启用版本": spec.get("模板版本", "未标注"),
            "版本冲突": False,
            "创建时间": _now(),
            "字段结果": [],
            "整体结果": STATUS_PASS,
            "失败字段数": 0,
            "摘要": "",
        }
        try:
            sample_record = self._first_record(sample_text, spec.get("输入格式", "CSV"))
            record["字段结果"] = [
                self._run_field(field_spec, sample_record)
                for field_spec in spec.get("字段定义", [])
            ]
        except (ValueError, json.JSONDecodeError, csv.Error) as exc:
            record["整体结果"] = STATUS_FAIL
            record["摘要"] = f"样本解析失败：{exc}"
            record["失败字段数"] = len(spec.get("字段定义", []))
            return record
        self._recompute_summary(record)
        return record

    def _recompute_summary(self, record: dict[str, Any]) -> None:
        results = record["字段结果"]
        failed = [item for item in results if item["状态"] == STATUS_FAIL]
        warned = [item for item in results if item["状态"] == STATUS_WARN]
        record["失败字段数"] = len(failed)
        if failed:
            record["整体结果"] = STATUS_FAIL
        elif warned:
            record["整体结果"] = STATUS_WARN
        else:
            record["整体结果"] = STATUS_PASS
        detail = "；".join(
            f"{item['字段名']}：{item['转换'].get('错误信息') or item['校验'].get('说明')}"
            for item in failed
        )
        record["摘要"] = (
            f"共 {len(results)} 个字段，失败 {len(failed)} 个，警告 {len(warned)} 个"
            + (f"。失败明细：{detail}" if detail else "")
        )

    def _overview_item(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record["id"],
            "模板编号": record["模板编号"],
            "运行模式": record["运行模式"],
            "模板版本快照": record["模板版本快照"],
            "当前启用版本": record["当前启用版本"],
            "版本冲突": record["版本冲突"],
            "整体结果": record["整体结果"],
            "失败字段数": record["失败字段数"],
            "摘要": record["摘要"],
            "创建时间": record["创建时间"],
        }

    # ------------------------------------------------------------------ 待办与日志
    def _spawn_todos(self, record: dict[str, Any]) -> None:
        for item in record["字段结果"]:
            if item["状态"] != STATUS_FAIL:
                continue
            self._todo_seq += 1
            self._todos.append({
                "id": self._todo_seq,
                "诊断ID": record["id"],
                "模板编号": record["模板编号"],
                "关联任务": record.get("关联任务"),
                "字段名": item["字段名"],
                "目标类型": item["目标类型"],
                "转换前值": item["转换"]["转换前值"],
                "转换后值": item["转换"]["转换后值"],
                "诊断摘要": record["摘要"],
                "状态": "待修复",
                "创建时间": record["创建时间"],
            })

    def _sync_todos_after_rerun(self, record: dict[str, Any], field_name: str, field_result: dict[str, Any]) -> None:
        for todo in self._todos:
            if todo["诊断ID"] != record["id"] or todo["字段名"] != field_name:
                continue
            todo["转换前值"] = field_result["转换"]["转换前值"]
            todo["转换后值"] = field_result["转换"]["转换后值"]
            if field_result["状态"] == STATUS_PASS:
                todo["状态"] = "已修复"
                todo["诊断摘要"] = f"{field_name} 重跑通过，原待办关闭"
            else:
                todo["状态"] = "待修复"
                todo["诊断摘要"] = field_result["转换"].get("错误信息") or field_result["校验"].get("说明", "")

    def _append_formal_log(self, record: dict[str, Any]) -> None:
        """正式采集诊断才落日志；试运行不允许走到这里。"""
        rows = store.rows(LOG_MODULE)
        log_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        total = len(record["字段结果"])
        failed = record["失败字段数"]
        status = "成功" if failed == 0 else ("完全失败" if failed == total else "部分失败")
        rows.append({
            "id": log_id,
            "status": status,
            "pending": status != "成功",
            "abnormal": failed > 0,
            "日志编号": f"LOG-DIAG-{log_id:04d}",
            "关联任务": record.get("关联任务") or record["模板编号"],
            "开始时间": record["创建时间"],
            "结束时间": _now(),
            "采集行数": 1,
            "成功条数": total - failed,
            "失败条数": failed,
            "错误信息": record["摘要"],
        })

    # ------------------------------------------------------------------ 启动留档
    def _seed_archived_diagnoses(self) -> None:
        """预置历史诊断：覆盖验收序列 J 的日期失败场景与旧版本留档场景。"""
        pars2 = store.find(PARSER_MODULE, 2)
        if pars2 is not None and pars2.get("spec"):
            sample = (
                "order_id,amount,order_date,status,remark\n"
                "ORD-1001,88.5,2026-13-40,已支付,"
            )
            record = self._build_record(
                entry=pars2,
                spec=deepcopy(pars2["spec"]),
                sample_text=sample,
                run_mode=MODE_TRIAL,
                related_task="TASK-0002",
            )
            self._diagnoses.append(record)
            self._spawn_todos(record)

        pars1 = store.find(PARSER_MODULE, 1)
        if pars1 is not None and pars1.get("spec"):
            old_spec = deepcopy(pars1["spec"])
            email_field = next(item for item in old_spec["字段定义"] if item["字段名"] == "email")
            email_field["校验正则"] = r"^\S+@\S+\.\S+$"
            email_field["判定阈值"] = r"正则 ^\S+@\S+\.\S+$（历史宽松口径）"
            sample = (
                "user_id,user_name,email,signup_date\n"
                "100861,,bad-email,2026-01-05"
            )
            record = self._build_record(
                entry=pars1,
                spec=old_spec,
                sample_text=sample,
                run_mode=MODE_TRIAL,
                related_task="TASK-0001",
            )
            self._diagnoses.append(record)
            self._spawn_todos(record)

        pars3 = store.find(PARSER_MODULE, 3)
        if pars3 is not None and pars3.get("spec"):
            old_spec = {
                "模板版本": "v2.9.0",
                "输入格式": "JSON",
                "输出格式": "JSON",
                "字段定义": [
                    {"字段名": "item_id", "源字段": "item_id", "目标类型": "string",
                     "必填": True, "默认值": None, "校验正则": r"^ITEM-\d+$",
                     "校验规则": "正则匹配", "范围": "无", "标准": "旧版编码标准",
                     "判定阈值": r"正则 ^ITEM-\d+$"},
                    {"字段名": "price", "源字段": "price", "目标类型": "float",
                     "必填": True, "默认值": None, "校验正则": None,
                     "最小值": 0, "最大值": 99999, "校验规则": "数值范围",
                     "范围": "0 ~ 99999", "标准": "旧版金额上限",
                     "判定阈值": "0 ≤ 值 ≤ 99999"},
                ],
            }
            sample = json.dumps([{"item_id": "ITEM-7", "price": 12.5}], ensure_ascii=False)
            record = self._build_record(
                entry=pars3,
                spec=old_spec,
                sample_text=sample,
                run_mode=MODE_FORMAL,
                related_task="TASK-0003",
            )
            self._diagnoses.append(record)


diagnosis_service = DiagnosisService()
