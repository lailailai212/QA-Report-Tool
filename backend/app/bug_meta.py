# -*- coding: utf-8 -*-
"""Static Feishu Bug field metadata for OBIS (cached; no MCP round-trip)."""
from __future__ import annotations

from typing import Any

from .config import settings

BUG_WORK_ITEM_TYPE = "67e61b9027f30a82566ed1cc"
BUG_WORK_ITEM_TYPE_NAME = "Bug"

FIELD_KEYS = {
    "name": "name",
    "description": "description",
    "priority": "priority",
    "template": "template",
    "severity": "field_e9df04",
    "bugEnvironment": "field_79f4fc",
    "issueStage": "field_246321",
    "componentVersions": "field_cc0205",
    "versionFound": "field_29cd1a",
    "issueType": "field_95b42b",
    "executionMethod": "field_93012c",
    "labels": "field_c55907",
    "sprint": "field_08c237",
    "roleOwners": "role_owners",
}

ROLE_KEYS = {
    "devOwner": "role_073f15",
    "qcOwner": "role_a94aa9",
    "reporter": "role_38c8ea",
}


def bug_meta() -> dict[str, Any]:
    return {
        "projectKey": settings.feishu_project_key,
        "simpleName": settings.feishu_simple_name,
        "workItemType": BUG_WORK_ITEM_TYPE,
        "workItemTypeName": BUG_WORK_ITEM_TYPE_NAME,
        "fieldKeys": FIELD_KEYS,
        "roleKeys": ROLE_KEYS,
        "feishuTemplates": [
            {"id": "2840575", "name": "Standard Workflow_V1"},
            {"id": "4362903", "name": "Standard Workflow_V2"},
        ],
        "priority": [
            {"id": "option_1", "name": "P0"},
            {"id": "option_2", "name": "P1"},
            {"id": "option_3", "name": "P2"},
            {"id": "uh67jkok0", "name": "P3"},
        ],
        "severity": [
            {"id": "9t3c88xg7", "name": "Critical"},
            {"id": "2", "name": "Major"},
            {"id": "3", "name": "Medium"},
            {"id": "4", "name": "Minor"},
            {"id": "e71vugh4a", "name": "Trivial"},
        ],
        "issueStage": [
            {"id": "stage_first", "name": "测试阶段"},
            {"id": "stage_smoke", "name": "冒烟测试"},
            {"id": "stage_regression", "name": "回归阶段"},
            {"id": "o1xn33w3n", "name": "验收阶段"},
            {"id": "stage_grey", "name": "灰度阶段"},
            {"id": "stage_online", "name": "线上阶段"},
        ],
        "bugEnvironment": [
            {"id": "hkrnplf2t", "name": "SIT"},
            {"id": "xu3bhhm0q", "name": "UAT"},
            {"id": "rsw18358z", "name": "PRE"},
            {"id": "_rme3lbye", "name": "PRD"},
        ],
        "issueType": [
            {"id": "1pxlw0t2u", "name": "功能问题"},
            {"id": "usy58rtau", "name": "数据缺陷"},
            {"id": "cy6u51h5a", "name": "UI 问题"},
            {"id": "06imsl7o3", "name": "性能问题"},
            {"id": "41t6ip8yp", "name": "兼容性问题"},
            {"id": "2niemismr", "name": "体验问题"},
            {"id": "otjee963d", "name": "安全问题"},
            {"id": "c2wp1382z", "name": "文档问题"},
            {"id": "hs82ieq2i", "name": "其他"},
        ],
        "executionMethod": [
            {"id": "wlht_94nl", "name": "Manual"},
            {"id": "5jvh_bwwt", "name": "Automated"},
            {"id": "b6fec2hey", "name": "Exploratory"},
        ],
        "labels": [
            {"id": "5662xtp2z", "name": "偶尔发生"},
            {"id": "稳定复现", "name": "稳定复现"},
            {"id": "2ufud3a4c", "name": "需求变更"},
            {"id": "k69y0kyir", "name": "历史遗留"},
        ],
        "roles": [
            {"key": "devOwner", "roleId": ROLE_KEYS["devOwner"], "name": "Dev Owner"},
            {"key": "qcOwner", "roleId": ROLE_KEYS["qcOwner"], "name": "QC Owner"},
            {"key": "reporter", "roleId": ROLE_KEYS["reporter"], "name": "Reporter"},
        ],
    }
