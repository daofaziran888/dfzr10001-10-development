"""数据解析模板接口：维护解析模板，覆盖提交校验、启用配置、停用配置等动作。

另提供字段级试解析诊断：诊断记录按模板版本留档，支持同版本去重、版本冲突标记与单字段重跑。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.diagnosis import MODE_FORMAL, MODE_TRIAL, diagnosis_service
from app.services.parser import ParserService

router = APIRouter(prefix="/api/parser", tags=["数据解析模板"])

service = ParserService()

LIST_FIELDS = ["模板编号", "输入格式", "字段映射", "类型转换", "默认值", "校验正则", "输出格式", "模板版本"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤数据解析模板列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据解析模板清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "parser", "total": total, "items": items}


@router.get("/diagnoses/overview")
def diagnosis_overview() -> dict[str, Any]:
    """诊断概览：按模板版本排列，组内含每条诊断的结果摘要与版本冲突标记。"""
    return {"module": "parser", "groups": diagnosis_service.grouped_overview()}


@router.get("/{entry_id}/diagnoses")
def list_template_diagnoses(entry_id: int) -> dict[str, Any]:
    """列出单个模板的全部历史诊断（含版本快照解释），按版本分组。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析模板 {entry_id} 不存在或已归档")
    return {
        "模板编号": entry.get("模板编号"),
        "当前启用版本": diagnosis_service.resolve_spec(entry).get("模板版本"),
        "groups": diagnosis_service.grouped_overview(template_id=entry_id),
    }


@router.post("/{entry_id}/diagnoses", response_model=ActionResult)
def create_diagnosis(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对样本做一次字段级试解析。

    values 支持：sample（样本原文，必填）、run_mode（试运行/正式采集，默认试运行）、
    related_task（关联任务编号，用于任务工作台生成修复待办）。
    """
    values = payload.values
    sample = str(values.get("sample") or "")
    run_mode = str(values.get("run_mode") or MODE_TRIAL).strip()
    if run_mode not in (MODE_TRIAL, MODE_FORMAL):
        return ActionResult(ok=False, message="run_mode 仅支持「试运行」或「正式采集」")
    related_task = values.get("related_task")
    related_task = str(related_task).strip() if related_task else None
    record, message, reused = diagnosis_service.create_diagnosis(
        entry_id, sample, run_mode=run_mode, related_task=related_task,
    )
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=True,
        message=("已复用首个诊断：" if reused else "") + message,
        entry=record,
    )


@router.get("/diagnoses/{diagnosis_id}", response_model=dict)
def get_diagnosis(diagnosis_id: str) -> dict[str, Any]:
    """读取单条诊断详情：逐字段给出映射、转换前后值、默认值来源与校验结果。"""
    record = diagnosis_service.get_diagnosis(diagnosis_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"诊断 {diagnosis_id} 不存在或已归档")
    return record


@router.post("/diagnoses/{diagnosis_id}/rerun-field", response_model=ActionResult)
def rerun_diagnosis_field(diagnosis_id: str, payload: EntryPayload) -> ActionResult:
    """只重跑诊断里的单个字段；可携带修正后的 raw_value，其余字段与版本快照保持不变。"""
    values = payload.values
    field_name = str(values.get("field") or "").strip()
    if not field_name:
        return ActionResult(ok=False, message="缺少 field 参数，无法定位要重跑的字段")
    raw_value = values.get("raw_value")
    raw_value = str(raw_value) if raw_value is not None else None
    record, message = diagnosis_service.rerun_field(diagnosis_id, field_name, raw_value=raw_value)
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条解析模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析模板 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条解析模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="解析模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条解析模板执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    diagnosis_service.refresh_conflict_flags()
    return ActionResult(ok=True, message=message, entry=entry)
