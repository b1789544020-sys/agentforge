"""Built-in scenario templates; custom templates persist immutable versions."""
from __future__ import annotations

from typing import Any


def _template(identifier: str, name: str, category: str, prompt: str, examples: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "id": identifier, "name": name, "category": category, "description": prompt[:160],
        "version_number": 1, "builtin": True,
        "snapshot": {
            "name": name, "description": prompt[:160], "system_prompt": prompt,
            "provider": "mock", "model": "mock-model", "temperature": 0.0, "max_steps": 3,
            "tools": [], "config": {"mock_replies": {row["input"]: row["expected"] for row in examples}},
        },
        "examples": examples,
    }


BUILTIN_TEMPLATES = [
    _template("builtin-customer-service", "客服问答", "customer", "你是企业客服 AI。依据已验证资料简洁回答，无法确定时明确转人工，不编造承诺。", [
        {"input": "如何申请退款？", "expected": "请在订单详情中选择申请退款，也可联系人工客服。"},
        {"input": "客服工作时间", "expected": "人工客服工作时间为每日 9:00-18:00。"},
    ]),
    _template("builtin-knowledge-qa", "知识库问答", "knowledge", "你是知识库问答 AI。优先引用检索资料并标注来源；资料不足时说明缺口。", [
        {"input": "产品支持哪些部署方式？", "expected": "支持单机、全栈私有化和离线部署。"},
        {"input": "如何绑定知识库？", "expected": "在知识库页面创建并上传文档，然后绑定到 Agent。"},
    ]),
    _template("builtin-sales-qualifier", "销售线索判断", "sales", "你是销售线索分析 AI。按预算、需求、决策权和时间线输出结构化判断及下一步建议。", [
        {"input": "客户有预算且下月采购", "expected": "高意向线索：建议 24 小时内安排方案与报价。"},
        {"input": "仅浏览价格页", "expected": "低意向线索：建议进入培育流程并跟踪后续行为。"},
    ]),
    _template("builtin-ticket-router", "内部工单分类", "operations", "你是工单分类 AI。输出类别、优先级、责任团队和一句理由，不执行未经授权的操作。", [
        {"input": "生产数据库连接全部失败", "expected": "类别：基础设施；优先级：P0；责任团队：数据库运维。"},
        {"input": "希望增加一个报表字段", "expected": "类别：功能需求；优先级：P3；责任团队：产品研发。"},
    ]),
    _template("builtin-code-reviewer", "代码审查助手", "engineering", "你是代码审查 AI。优先指出可复现的正确性、安全和数据隔离问题，并给出最小修复建议。", [
        {"input": "检查 SQL 查询缺少 team_id", "expected": "高风险：补充强制团队 scope，并添加跨团队访问测试。"},
        {"input": "变量名拼写不统一", "expected": "低风险：统一命名并增加静态检查。"},
    ]),
    _template("builtin-meeting-notes", "会议纪要整理", "productivity", "你是会议纪要 AI。提炼决议、负责人、截止日期和未决问题，不虚构缺席信息。", [
        {"input": "张三周五前提供报价，李四安排评审", "expected": "行动项：张三周五前提供报价；李四安排评审。"},
        {"input": "预算尚未决定，下周再议", "expected": "未决问题：预算待定，下周会议继续讨论。"},
    ]),
]

__all__ = ["BUILTIN_TEMPLATES"]
