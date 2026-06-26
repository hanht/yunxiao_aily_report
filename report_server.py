#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import datetime
import hmac
import hashlib
import base64
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from dotenv import load_dotenv

# 加载环境变量配置文件
load_dotenv()

# 获取并清理配置项
def clean_env_var(name, default=None):
    val = os.getenv(name)
    if not val:
        return default
    return val.strip().strip('"').strip("'")

YUNXIAO_PAT = clean_env_var("YUNXIAO_PAT")
YUNXIAO_ORG_ID = clean_env_var("YUNXIAO_ORG_ID")
YUNXIAO_PROJECT_ID = clean_env_var("YUNXIAO_PROJECT_ID")
DINGTALK_WEBHOOK = clean_env_var("DINGTALK_WEBHOOK")
DINGTALK_KEYWORD = clean_env_var("DINGTALK_KEYWORD", "日报")
DINGTALK_SECRET = clean_env_var("DINGTALK_SECRET")

# 检查必要参数，但不要立即退出，以便在页面上显示错误
config_ok = all([YUNXIAO_PAT, YUNXIAO_ORG_ID, YUNXIAO_PROJECT_ID])

category_map = {
    'Req': '需求',
    'Task': '任务',
    'Bug': '缺陷'
}

def is_timestamp_on_date(timestamp_val, target_date):
    """判断给定的时间戳或日期字符串是否为指定日期"""
    if not timestamp_val:
        return False
    
    # 如果是毫秒时间戳
    if isinstance(timestamp_val, (int, float)):
        ts = timestamp_val / 1000.0
    elif isinstance(timestamp_val, str) and timestamp_val.isdigit():
        ts = int(timestamp_val) / 1000.0
    else:
        # 如果是 ISO-8601 字符串
        try:
            clean_str = timestamp_val.replace('T', ' ').replace('Z', '')
            if '.' in clean_str:
                clean_str = clean_str.split('.')[0]
            dt = datetime.datetime.strptime(clean_str.strip(), "%Y-%m-%d %H:%M:%S")
            if 'Z' in timestamp_val:
                # UTC 转东八区
                dt = dt + datetime.timedelta(hours=8)
            return dt.date() == target_date
        except Exception as e:
            print(f"⚠️ 解析日期格式失败: {timestamp_val}, 错误: {e}")
            return False
            
    try:
        # 默认以本地时区解析时间戳
        dt = datetime.datetime.fromtimestamp(ts)
        return dt.date() == target_date
    except Exception as e:
        print(f"⚠️ 转换时间戳失败: {ts}, 错误: {e}")
        return False

def is_modified_on_date(gmt_modified, target_date):
    """判断修改时间是否为目标日期 (target_date 为 datetime.date 对象)"""
    return is_timestamp_on_date(gmt_modified, target_date)

def is_status_updated_on_date(item, target_date):
    """判断状态更新时间是否为目标日期"""
    return is_timestamp_on_date(item.get("updateStatusAt"), target_date)

def parse_date_string(date_str):
    """解析日期字符串为 date 对象"""
    if not date_str:
        return None
    try:
        clean_str = date_str.strip()
        if ' ' in clean_str:
            clean_str = clean_str.split(' ')[0]
        elif 'T' in clean_str:
            clean_str = clean_str.split('T')[0]
        return datetime.datetime.strptime(clean_str, "%Y-%m-%d").date()
    except Exception as e:
        print(f"⚠️ 解析计划日期失败: {date_str}, 错误: {e}")
        return None

def get_planned_dates(item):
    """提取计划开始时间(fieldId: 79)和计划完成时间(fieldId: 80)"""
    start_date = None
    end_date = None
    for cf in item.get("customFieldValues", []):
        field_id = cf.get("fieldId")
        field_name = cf.get("fieldName")
        if field_id == "79" or field_name == "计划开始时间":
            values = cf.get("values", [])
            if values:
                start_val = values[0].get("displayValue") or values[0].get("value") or values[0].get("identifier")
                start_date = parse_date_string(start_val)
        elif field_id == "80" or field_name == "计划完成时间":
            values = cf.get("values", [])
            if values:
                end_val = values[0].get("displayValue") or values[0].get("value") or values[0].get("identifier")
                end_date = parse_date_string(end_val)
    return start_date, end_date

def is_active_on_date(item, target_date):
    """判断工作项在目标日期是否处于活动（计划）范围"""
    start_date, end_date = get_planned_dates(item)
    
    # 获取状态名称
    status_val = item.get("status")
    if isinstance(status_val, dict):
        status_name = status_val.get("name") or status_val.get("displayName") or str(status_val)
    else:
        status_name = str(status_val)
        
    # 如果已完成，我们只在状态更新时间为目标日期时展示它，避免展示以前完成的历史任务
    if status_name == "已完成":
        return is_status_updated_on_date(item, target_date)
        
    # 如果是待处理或处理中，使用计划时间判断
    if start_date or end_date:
        if start_date and end_date:
            return start_date <= target_date <= end_date
        elif start_date:
            return start_date <= target_date
        else:  # end_date
            return target_date <= end_date
            
    # 如果既没有计划开始也没有计划结束，回退到判断目标日期是否有修改
    return is_modified_on_date(item.get("gmtModified"), target_date)

def get_actual_hours(item):
    """提取实际工时"""
    for cf in item.get("customFieldValues", []):
        field_id = cf.get("fieldId")
        field_name = cf.get("fieldName")
        if field_id in ["101587", "sumActualLaborHour"] or field_name in ["实际工时", "实际工时汇总"]:
            values = cf.get("values", [])
            if values:
                try:
                    val_str = values[0].get("displayValue") or values[0].get("value") or "0"
                    return float(val_str)
                except Exception:
                    pass
    return 0.0

def fetch_work_items():
    """获取所有类型的工作项"""
    if not config_ok:
        raise ValueError("缺少云效配置环境变量，请检查 .env 文件。")
        
    url = f"https://openapi-rdc.aliyuncs.com/oapi/v1/projex/organizations/{YUNXIAO_ORG_ID}/workitems:search"
    headers = {
        "x-yunxiao-token": YUNXIAO_PAT,
        "Content-Type": "application/json"
    }
    
    work_items = []
    
    for cat in ['Req', 'Task', 'Bug']:
        payload = {
            "spaceId": YUNXIAO_PROJECT_ID,
            "spaceType": "Project",
            "category": cat,
            "conditions": "{\"conditionGroups\":[]}"
        }
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code != 200:
            raise RuntimeError(f"请求云效失败 HTTP {response.status_code}: {response.text}")
            
        items = response.json()
        if isinstance(items, list):
            work_items.extend(items)
            
    return work_items

def build_markdown_report(grouped_items, query_date):
    """根据特定日期生成日报 Markdown"""
    date_str = query_date.strftime("%Y-%m-%d")
    markdown_lines = [
        f"### 📋 云效项目今日日报汇总 ({date_str})",
        f"**项目ID**: `{YUNXIAO_PROJECT_ID}`",
        f"---"
    ]
    
    if not grouped_items:
        markdown_lines.append("该日期项目内没有工作项更新。")
    else:
        for person, items in grouped_items.items():
            in_progress = []
            todo = []
            completed = []
            person_total_hours = 0.0
            
            for it in items:
                subject = it.get("subject", "无标题")
                status_name = it.get("status")
                
                hours = it.get("hours", 0.0)
                person_total_hours += hours
                hours_str = f", 工时: {hours}h" if hours > 0 else ""
                bullet = f"- {subject} ({status_name}{hours_str})"
                
                if status_name == "已完成":
                    completed.append(bullet)
                elif status_name == "待处理":
                    todo.append(bullet)
                else:
                    in_progress.append(bullet)
            
            hours_header = f" (今日工时: {person_total_hours}h)" if person_total_hours > 0 else ""
            markdown_lines.append(f"\n👤 **{person}**{hours_header}")
            
            if in_progress:
                markdown_lines.append("  *   **处理中**：")
                for p in in_progress:
                    markdown_lines.append(f"      {p}")
            if todo:
                markdown_lines.append("  *   **待处理**：")
                for t in todo:
                    markdown_lines.append(f"      {t}")
            if completed:
                markdown_lines.append("  *   **已完成**：")
                for c in completed:
                    markdown_lines.append(f"      {c}")
                
    content = "\n".join(markdown_lines)
    if DINGTALK_KEYWORD not in content:
        content += f"\n\n*(通知类型: {DINGTALK_KEYWORD})*"
        
    return content

def send_to_dingtalk(text_content):
    """发送 Markdown 日报至钉钉群机器人 Webhook"""
    if not DINGTALK_WEBHOOK:
        raise ValueError("未配置 DINGTALK_WEBHOOK 环境变量。")
        
    timestamp = str(round(time.time() * 1000))
    url = DINGTALK_WEBHOOK
    
    if DINGTALK_SECRET:
        secret_enc = DINGTALK_SECRET.encode('utf-8')
        string_to_sign = f'{timestamp}\n{DINGTALK_SECRET}'
        string_to_sign_enc = string_to_sign.encode('utf-8')
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        url = f"{DINGTALK_WEBHOOK}&timestamp={timestamp}&sign={sign}"
        
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": DINGTALK_KEYWORD,
            "text": text_content
        }
    }
    
    response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"推送钉钉失败 HTTP {response.status_code}: {response.text}")
    return response.json()

# HTML 模版代码
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>云效项目工作日报看板</title>
    <!-- 引入 Outfit 和 Inter 字体 -->
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: rgba(17, 24, 39, 0.7);
            --card-border: rgba(255, 255, 255, 0.07);
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
            
            --primary: #6366f1;
            --primary-hover: #4f46e5;
            --primary-glow: rgba(99, 102, 241, 0.35);
            
            --success: #10b981;
            --success-bg: rgba(16, 185, 129, 0.15);
            
            --warning: #f59e0b;
            --warning-bg: rgba(245, 158, 11, 0.15);
            
            --info: #0ea5e9;
            --info-bg: rgba(14, 165, 233, 0.15);
            
            --danger: #ef4444;
            --danger-bg: rgba(239, 68, 68, 0.15);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(at 10% 20%, rgba(99, 102, 241, 0.15) 0px, transparent 50%),
                radial-gradient(at 90% 80%, rgba(14, 165, 233, 0.15) 0px, transparent 50%);
            background-attachment: fixed;
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            line-height: 1.5;
        }

        /* 顶部装饰条 */
        .top-gradient-bar {
            height: 5px;
            background: linear-gradient(90deg, var(--primary) 0%, #a855f7 50%, var(--info) 100%);
            width: 100%;
        }

        .container {
            max-width: 1300px;
            margin: 0 auto;
            padding: 30px 20px;
            width: 100%;
            flex-grow: 1;
            display: flex;
            flex-direction: column;
            gap: 25px;
        }

        /* 玻璃拟态 Card 基础样式 */
        .glass-card {
            background: var(--card-bg);
            backdrop-filter: blur(12px) saturate(180%);
            -webkit-backdrop-filter: blur(12px) saturate(180%);
            border: 1px solid var(--card-border);
            border-radius: 16px;
            box-shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.5);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        /* 顶部标题栏 */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 24px;
        }

        .header-title-section h1 {
            font-family: 'Outfit', sans-serif;
            font-size: 26px;
            font-weight: 700;
            background: linear-gradient(135deg, #ffffff 30%, #c7d2fe 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: -0.5px;
        }

        .header-title-section p {
            color: var(--text-muted);
            font-size: 13px;
            margin-top: 4px;
        }

        .header-badge {
            font-size: 12px;
            font-weight: 600;
            padding: 6px 14px;
            border-radius: 9999px;
            background: rgba(99, 102, 241, 0.1);
            border: 1px solid rgba(99, 102, 241, 0.2);
            color: #818cf8;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .pulse-dot {
            width: 8px;
            height: 8px;
            background-color: var(--success);
            border-radius: 50%;
            animation: pulse-animation 2s infinite;
        }

        @keyframes pulse-animation {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { transform: scale(1); box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        /* 控制面板 */
        .control-panel {
            padding: 20px;
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 15px;
        }

        .date-picker-group {
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }

        .date-picker-group label {
            font-size: 14px;
            font-weight: 500;
            color: var(--text-muted);
        }

        input[type="date"] {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.15);
            border-radius: 8px;
            color: var(--text-main);
            padding: 8px 12px;
            font-size: 14px;
            outline: none;
            cursor: pointer;
            transition: border-color 0.2s;
        }

        input[type="date"]:focus {
            border-color: var(--primary);
        }

        .shortcut-btn {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 8px;
            color: var(--text-main);
            padding: 8px 14px;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }

        .shortcut-btn:hover {
            background: rgba(255, 255, 255, 0.08);
            border-color: rgba(255, 255, 255, 0.15);
        }

        .shortcut-btn.active {
            background: var(--primary);
            border-color: var(--primary);
            box-shadow: 0 0 15px var(--primary-glow);
        }

        .action-group {
            display: flex;
            gap: 12px;
        }

        .btn {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 10px 20px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            border: none;
            transition: all 0.2s ease-in-out;
        }

        .btn-primary {
            background: var(--primary);
            color: white;
            box-shadow: 0 4px 12px var(--primary-glow);
        }

        .btn-primary:hover {
            background: var(--primary-hover);
            transform: translateY(-1px);
        }

        .btn-success {
            background: var(--success-bg);
            border: 1px solid rgba(16, 185, 129, 0.3);
            color: #34d399;
        }

        .btn-success:hover {
            background: var(--success);
            color: white;
            box-shadow: 0 4px 12px var(--success-glow);
            transform: translateY(-1px);
        }

        /* 汇总概览区 */
        .summary-section {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 20px;
        }

        .summary-card {
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .summary-card-title {
            font-size: 13px;
            color: var(--text-muted);
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .summary-card-value {
            font-family: 'Outfit', sans-serif;
            font-size: 28px;
            font-weight: 700;
            color: white;
        }

        /* 主体内容分栏 */
        .main-layout {
            display: grid;
            grid-template-columns: 1.6fr 1fr;
            gap: 25px;
            align-items: start;
        }

        @media (max-width: 1024px) {
            .main-layout {
                grid-template-columns: 1fr;
            }
        }

        /* 左侧：工作详情卡片列表 */
        .details-container {
            display: flex;
            flex-direction: column;
            gap: 20px;
        }

        .user-card {
            padding: 24px;
        }

        .user-card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            padding-bottom: 14px;
        }

        .user-name {
            font-size: 18px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .user-hours-badge {
            font-size: 12px;
            background: rgba(245, 158, 11, 0.15);
            border: 1px solid rgba(245, 158, 11, 0.25);
            color: #fbbf24;
            padding: 3px 8px;
            border-radius: 6px;
            font-weight: 500;
        }

        .user-task-sections {
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .task-list-title {
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .task-list-title.done { color: #34d399; }
        .task-list-title.todo { color: #f87171; }

        .task-items {
            list-style: none;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .task-item {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid rgba(255, 255, 255, 0.04);
            padding: 10px 14px;
            border-radius: 8px;
            font-size: 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
        }

        .task-subject {
            flex-grow: 1;
            color: var(--text-main);
        }

        .task-meta {
            display: flex;
            gap: 8px;
            flex-shrink: 0;
        }

        .badge {
            font-size: 11px;
            padding: 2px 8px;
            border-radius: 4px;
            font-weight: 600;
        }

        .badge-done { background: var(--success-bg); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.2); }
        .badge-progress { background: var(--info-bg); color: #38bdf8; border: 1px solid rgba(14, 165, 233, 0.2); }
        .badge-todo { background: var(--warning-bg); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.2); }

        .badge-category {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: var(--text-muted);
        }

        /* 右侧：Markdown 预览区 */
        .preview-card {
            padding: 24px;
            position: sticky;
            top: 30px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .preview-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            padding-bottom: 12px;
        }

        .preview-title {
            font-size: 16px;
            font-weight: 600;
        }

        .markdown-textarea {
            width: 100%;
            height: 400px;
            background: rgba(0, 0, 0, 0.2);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 8px;
            color: #d1d5db;
            font-family: 'Courier New', Courier, monospace;
            padding: 12px;
            font-size: 13px;
            line-height: 1.6;
            resize: vertical;
            outline: none;
        }

        .markdown-textarea:focus {
            border-color: rgba(99, 102, 241, 0.4);
        }

        /* 状态与加载指示器 */
        .loading-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(11, 15, 25, 0.7);
            backdrop-filter: blur(8px);
            z-index: 1000;
            align-items: center;
            justify-content: center;
            flex-direction: column;
            gap: 15px;
        }

        .spinner {
            width: 48px;
            height: 48px;
            border: 5px solid rgba(99, 102, 241, 0.1);
            border-top-color: var(--primary);
            border-radius: 50%;
            animation: spin 1s infinite linear;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        /* 弹窗 Toast */
        .toast {
            position: fixed;
            bottom: 30px;
            right: 30px;
            padding: 14px 24px;
            border-radius: 10px;
            background: #1f2937;
            border: 1px solid rgba(255, 255, 255, 0.1);
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            color: white;
            z-index: 2000;
            transform: translateY(100px);
            opacity: 0;
            transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 14px;
        }

        .toast.show {
            transform: translateY(0);
            opacity: 1;
        }

        .toast.success { border-color: rgba(16, 185, 129, 0.3); color: #34d399; }
        .toast.error { border-color: rgba(239, 68, 68, 0.3); color: #f87171; }

        /* 空状态 */
        .empty-state {
            padding: 60px 20px;
            text-align: center;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 15px;
        }

        .empty-icon {
            font-size: 48px;
            color: var(--text-muted);
        }

        /* 错误状态警告 */
        .config-error-banner {
            background: var(--danger-bg);
            border: 1px solid rgba(239, 68, 68, 0.2);
            border-radius: 12px;
            padding: 16px 20px;
            color: #f87171;
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 14px;
            margin-bottom: 5px;
        }

        footer {
            text-align: center;
            padding: 20px;
            color: var(--text-muted);
            font-size: 12px;
            border-top: 1px solid var(--card-border);
            margin-top: auto;
        }
    </style>
</head>
<body>
    <div class="top-gradient-bar"></div>
    <div class="container">
        
        <!-- 配置项错误提醒 -->
        <div id="error-banner" class="config-error-banner" style="display: none;">
            <span>⚠️</span>
            <div>
                <strong>服务配置缺失或不完整！</strong> 
                请检查该目录下的 <code>.env</code> 配置文件中是否设置了 <code>YUNXIAO_PAT</code>、<code>YUNXIAO_ORG_ID</code>、<code>YUNXIAO_PROJECT_ID</code>。
            </div>
        </div>

        <!-- 顶部标题栏 -->
        <header class="glass-card">
            <div class="header-title-section">
                <h1>云效项目每日工作日报看板</h1>
                <p>项目ID：<span id="proj-id">加载中...</span></p>
            </div>
            <div class="header-badge">
                <div class="pulse-dot"></div>
                云效 API 正常链接
            </div>
        </header>

        <!-- 控制面板 -->
        <section class="control-panel glass-card">
            <div class="date-picker-group">
                <label for="date-select">选择查看日期</label>
                <input type="date" id="date-select">
                <button class="shortcut-btn" id="btn-today">今天</button>
                <button class="shortcut-btn" id="btn-yesterday">昨天</button>
                <button class="shortcut-btn" id="btn-before-yesterday">前天</button>
            </div>
            <div class="action-group">
                <button class="btn btn-primary" id="btn-search">
                    <span>🔍</span> 查询数据
                </button>
                <button class="btn btn-success" id="btn-send">
                    <span>📤</span> 发送此日报至钉钉
                </button>
            </div>
        </section>

        <!-- 汇总区域 -->
        <section class="summary-section">
            <div class="summary-card glass-card">
                <span class="summary-card-title">今日更新任务项</span>
                <span class="summary-card-value" id="stat-total">0</span>
            </div>
            <div class="summary-card glass-card">
                <span class="summary-card-title">贡献成员数</span>
                <span class="summary-card-value" id="stat-people">0</span>
            </div>
            <div class="summary-card glass-card">
                <span class="summary-card-title">录入总工时</span>
                <span class="summary-card-value" id="stat-hours">0h</span>
            </div>
        </section>

        <!-- 主体布局 -->
        <div class="main-layout">
            <!-- 左边：工作详情 -->
            <section class="details-container" id="details-container">
                <div class="glass-card empty-state">
                    <div class="empty-icon">📂</div>
                    <h3>请选择日期并点击“查询数据”进行查询</h3>
                    <p style="color: var(--text-muted); font-size: 13px;">默认已为您选中了前天。</p>
                </div>
            </section>

            <!-- 右边：Markdown 文本与操作 -->
            <section class="preview-card glass-card">
                <div class="preview-header">
                    <span class="preview-title">Markdown 日报预览</span>
                    <button class="shortcut-btn" id="btn-copy">📋 复制内容</button>
                </div>
                <textarea class="markdown-textarea" id="markdown-preview" readonly placeholder="查询后将在此生成 Markdown 日报..."></textarea>
            </section>
        </div>
    </div>

    <!-- 加载层 -->
    <div class="loading-overlay" id="loading-overlay">
        <div class="spinner"></div>
        <p style="font-weight: 500;">正在连接云效并拉取数据，请稍候...</p>
    </div>

    <!-- Toast 弹窗 -->
    <div class="toast" id="toast-notify">
        <span id="toast-icon">✓</span>
        <span id="toast-msg">操作成功</span>
    </div>

    <footer>
        云效每日日报看板 - Antigravity Designed
    </footer>

    <script>
        const configOk = __CONFIG_OK__;
        const projectID = "__PROJECT_ID__";

        // 日期助手函数
        function getFormattedDate(daysOffset = 0) {
            const date = new Date();
            date.setDate(date.getDate() - daysOffset);
            const y = date.getFullYear();
            const m = String(date.getMonth() + 1).padStart(2, '0');
            const d = String(date.getDate()).padStart(2, '0');
            return `${y}-${m}-${d}`;
        }

        // 初始化日期：默认选择“前天”（偏移 2 天）
        const dateInput = document.getElementById('date-select');
        const defaultDate = getFormattedDate(2);
        dateInput.value = defaultDate;
        
        // 绑定状态
        document.getElementById('proj-id').innerText = projectID || '未配置';
        if (!configOk) {
            document.getElementById('error-banner').style.display = 'flex';
        }

        // 按钮快捷键绑定
        const btnToday = document.getElementById('btn-today');
        const btnYesterday = document.getElementById('btn-yesterday');
        const btnBeforeYesterday = document.getElementById('btn-before-yesterday');

        function clearActiveShortcuts() {
            btnToday.classList.remove('active');
            btnYesterday.classList.remove('active');
            btnBeforeYesterday.classList.remove('active');
        }

        btnToday.addEventListener('click', () => {
            clearActiveShortcuts();
            btnToday.classList.add('active');
            dateInput.value = getFormattedDate(0);
            queryData();
        });

        btnYesterday.addEventListener('click', () => {
            clearActiveShortcuts();
            btnYesterday.classList.add('active');
            dateInput.value = getFormattedDate(1);
            queryData();
        });

        btnBeforeYesterday.addEventListener('click', () => {
            clearActiveShortcuts();
            btnBeforeYesterday.classList.add('active');
            dateInput.value = getFormattedDate(2);
            queryData();
        });

        // 页面初始化时，高亮显示“前天”按钮，并自动加载数据
        btnBeforeYesterday.classList.add('active');

        // Toast 消息
        function showToast(msg, isSuccess = true) {
            const toast = document.getElementById('toast-notify');
            const icon = document.getElementById('toast-icon');
            const msgEl = document.getElementById('toast-msg');
            
            toast.className = 'toast ' + (isSuccess ? 'success' : 'error');
            icon.innerText = isSuccess ? '✓' : '❌';
            msgEl.innerText = msg;
            
            toast.classList.add('show');
            setTimeout(() => {
                toast.classList.remove('show');
            }, 3000);
        }

        // 页面加载或切换日期后，运行数据查询
        async function queryData() {
            const dateVal = dateInput.value;
            if (!dateVal) {
                showToast("请先选择日期！", false);
                return;
            }

            const loading = document.getElementById('loading-overlay');
            loading.style.display = 'flex';

            try {
                const response = await fetch(`/api/report?date=${dateVal}`);
                const res = await response.json();
                
                if (res.status === 'success') {
                    renderUI(res);
                    showToast(`${dateVal} 的日报加载成功`);
                } else {
                    showToast(res.message || '拉取数据失败', false);
                    renderError(res.message);
                }
            } catch (err) {
                console.error(err);
                showToast('网络连接错误，无法获取数据', false);
                renderError(err.message || '网络连接异常');
            } finally {
                loading.style.display = 'none';
            }
        }

        function renderError(errMsg) {
            const detailsContainer = document.getElementById('details-container');
            detailsContainer.innerHTML = `
                <div class="glass-card empty-state" style="border-color: rgba(239, 68, 68, 0.2);">
                    <div class="empty-icon">⚠️</div>
                    <h3 style="color: #f87171;">查询失败</h3>
                    <p style="color: var(--text-muted); font-size: 13px; max-width: 80%; margin: 0 auto; word-break: break-all;">
                        ${errMsg}
                    </p>
                </div>
            `;
            document.getElementById('markdown-preview').value = '';
            document.getElementById('stat-total').innerText = '0';
            document.getElementById('stat-people').innerText = '0';
            document.getElementById('stat-hours').innerText = '0h';
        }

        function renderUI(res) {
            // 1. 填充概览数据
            document.getElementById('stat-total').innerText = res.total_items;
            document.getElementById('stat-people').innerText = res.contributors;
            document.getElementById('stat-hours').innerText = res.total_hours + 'h';
            
            // 2. 填充 Markdown
            document.getElementById('markdown-preview').value = res.markdown;

            // 3. 填充详细列表
            const detailsContainer = document.getElementById('details-container');
            detailsContainer.innerHTML = '';

            const data = res.data;
            const users = Object.keys(data);

            if (users.length === 0) {
                detailsContainer.innerHTML = `
                    <div class="glass-card empty-state">
                        <div class="empty-icon">📁</div>
                        <h3>该日期下无任何工作项被更新</h3>
                        <p style="color: var(--text-muted); font-size: 13px;">请尝试切换到其他日期查询。</p>
                    </div>
                `;
                return;
            }

            users.forEach(username => {
                const items = data[username];
                
                // 计算今日该成员总工时
                let userTotalHours = 0;
                const inProgressItems = [];
                const todoItems = [];
                const completedItems = [];

                items.forEach(it => {
                    userTotalHours += it.hours;
                    
                    const itemHTML = `
                        <li class="task-item">
                            <span class="task-subject">${it.subject}</span>
                            <div class="task-meta">
                                <span class="badge badge-category">${it.category_zh}</span>
                                <span class="badge ${getStatusBadgeClass(it.status)}">${it.status}</span>
                                ${it.hours > 0 ? `<span class="badge" style="background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.2); color: #f59e0b;">${it.hours}h</span>` : ''}
                            </div>
                        </li>
                    `;

                    if (it.status === '已完成') {
                        completedItems.push(itemHTML);
                    } else if (it.status === '待处理') {
                        todoItems.push(itemHTML);
                    } else {
                        inProgressItems.push(itemHTML);
                    }
                });

                const userCard = document.createElement('div');
                userCard.className = 'glass-card user-card';

                let sectionsHTML = '';
                if (inProgressItems.length > 0) {
                    sectionsHTML += `
                        <div>
                            <div class="task-list-title" style="color: #38bdf8;"><span>⚡️</span> 处理中</div>
                            <ul class="task-items">${inProgressItems.join('')}</ul>
                        </div>
                    `;
                }
                if (todoItems.length > 0) {
                    sectionsHTML += `
                        <div>
                            <div class="task-list-title todo"><span>📋</span> 待处理</div>
                            <ul class="task-items">${todoItems.join('')}</ul>
                        </div>
                    `;
                }
                if (completedItems.length > 0) {
                    sectionsHTML += `
                        <div>
                            <div class="task-list-title done"><span>🎯</span> 已完成</div>
                            <ul class="task-items">${completedItems.join('')}</ul>
                        </div>
                    `;
                }

                userCard.innerHTML = `
                    <div class="user-card-header">
                        <div class="user-name">
                            <span>👤</span> ${username}
                        </div>
                        ${userTotalHours > 0 ? `<span class="user-hours-badge">本日录入工时: ${userTotalHours}h</span>` : ''}
                    </div>
                    <div class="user-task-sections">
                        ${sectionsHTML}
                    </div>
                `;

                detailsContainer.appendChild(userCard);
            });
        }

        function getStatusBadgeClass(status) {
            if (status === '已完成') return 'badge-done';
            if (status === '处理中') return 'badge-progress';
            return 'badge-todo';
        }

        // 按钮事件监听
        document.getElementById('btn-search').addEventListener('click', () => {
            clearActiveShortcuts();
            queryData();
        });

        // 复制 Markdown 日报
        document.getElementById('btn-copy').addEventListener('click', () => {
            const text = document.getElementById('markdown-preview').value;
            if (!text) {
                showToast("暂无可复制的日报内容！", false);
                return;
            }
            navigator.clipboard.writeText(text).then(() => {
                showToast("日报已复制到剪贴板！");
            }).catch(err => {
                showToast("复制失败，请手动选择复制", false);
            });
        });

        // 发送至钉钉
        document.getElementById('btn-send').addEventListener('click', async () => {
            const text = document.getElementById('markdown-preview').value;
            if (!text) {
                showToast("当前没有日报数据，无法发送！", false);
                return;
            }

            if (!confirm(`确定要将当前预览的日报发送到钉钉吗？`)) {
                return;
            }

            const loading = document.getElementById('loading-overlay');
            loading.style.display = 'flex';

            try {
                const response = await fetch('/api/send_dingtalk', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ markdown: text })
                });
                const res = await response.json();
                if (res.status === 'success') {
                    showToast("日报已成功推送至钉钉！");
                } else {
                    showToast(res.message || "推送失败", false);
                }
            } catch (err) {
                console.error(err);
                showToast("网络请求错误，推送失败", false);
            } finally {
                loading.style.display = 'none';
            }
        });

        // 初始化加载数据
        if (configOk) {
            queryData();
        } else {
            renderError("缺失配置：请确保当前目录下的 .env 文件中已配置 YUNXIAO_PAT、YUNXIAO_ORG_ID、YUNXIAO_PROJECT_ID。");
        }
    </script>
</body>
</html>
"""

class DashboardHTTPRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 屏蔽终端琐碎的访问日志，保持输出清洁
        return

    def do_GET(self):
        url_parsed = urllib.parse.urlparse(self.path)
        
        # 路由 1: 首页
        if url_parsed.path in ["", "/", "/index.html"]:
            html_content = HTML_TEMPLATE.replace(
                "__CONFIG_OK__", "true" if config_ok else "false"
            ).replace(
                "__PROJECT_ID__", YUNXIAO_PROJECT_ID or ""
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_content.encode("utf-8"))
            return
            
        # 路由 2: 查询数据 API
        elif url_parsed.path == "/api/report":
            query_params = urllib.parse.parse_qs(url_parsed.query)
            date_str = query_params.get("date", [None])[0]
            
            if not date_str:
                self.send_error_json(400, "参数缺少: date (格式: YYYY-MM-DD)")
                return
                
            try:
                target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                self.send_error_json(400, "无效的日期格式，必须为 YYYY-MM-DD")
                return

            if not config_ok:
                self.send_error_json(500, "配置缺失。请检查 .env 配置文件中是否填写了 YUNXIAO_PAT、YUNXIAO_ORG_ID、YUNXIAO_PROJECT_ID。")
                return

            try:
                # 1. 抓取工作项
                all_items = fetch_work_items()
                
                # 2. 筛选特定日期有更新的工作项并进行人员分组
                grouped_items = {}
                filtered_count = 0
                total_hours = 0.0
                
                for item in all_items:
                    if not is_active_on_date(item, target_date):
                        continue
                        
                    filtered_count += 1
                    
                    assigned_to = item.get("assignedTo")
                    if isinstance(assigned_to, dict):
                        assignee_name = assigned_to.get("name") or "未指派"
                    else:
                        assignee_name = "未指派"
                        
                    if assignee_name not in grouped_items:
                        grouped_items[assignee_name] = []
                        
                    hours = get_actual_hours(item)
                    total_hours += hours
                    
                    category = item.get("category")
                    category_zh = category_map.get(category, category or "工作项")
                    
                    status_val = item.get("status")
                    if isinstance(status_val, dict):
                        status_name = status_val.get("name") or status_val.get("displayName") or str(status_val)
                    else:
                        status_name = str(status_val)
                        
                    grouped_items[assignee_name].append({
                        "subject": item.get("subject", "无标题"),
                        "status": status_name,
                        "hours": hours,
                        "category_zh": category_zh,
                        "id": item.get("id")
                    })
                
                # 3. 自动生成对应的 Markdown 格式日报
                markdown_content = build_markdown_report(grouped_items, target_date)
                
                # 4. 返回 JSON 数据
                response_data = {
                    "status": "success",
                    "date": date_str,
                    "total_items": filtered_count,
                    "contributors": len(grouped_items),
                    "total_hours": round(total_hours, 1),
                    "data": grouped_items,
                    "markdown": markdown_content
                }
                self.send_json(200, response_data)
                
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_error_json(500, f"获取云效数据失败: {str(e)}")
            return
            
        # 未匹配路由
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def do_POST(self):
        url_parsed = urllib.parse.urlparse(self.path)
        
        if url_parsed.path == "/api/send_dingtalk":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            
            try:
                payload = json.loads(post_data.decode("utf-8"))
                markdown_content = payload.get("markdown")
                
                if not markdown_content:
                    self.send_error_json(400, "参数缺少: markdown")
                    return
                
                if not DINGTALK_WEBHOOK:
                    self.send_error_json(400, "未配置钉钉机器人的 DINGTALK_WEBHOOK 环境变量。")
                    return
                    
                send_to_dingtalk(markdown_content)
                self.send_json(200, {"status": "success", "message": "已成功推送到钉钉群！"})
            except Exception as e:
                self.send_error_json(500, f"发送到钉钉失败: {str(e)}")
            return
            
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def send_json(self, status_code, data):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def send_error_json(self, status_code, message):
        self.send_json(status_code, {"status": "error", "message": message})

def run_server(port=8000):
    server_address = ("", port)
    httpd = HTTPServer(server_address, DashboardHTTPRequestHandler)
    print(f"🚀 云效日报工作看板 Web 服务已成功启动！")
    print(f"🔗 本地访问地址: http://localhost:{port}")
    
    # 自动在浏览器中打开页面
    try:
        webbrowser.open(f"http://localhost:{port}")
    except Exception as e:
        print(f"⚠️ 无法自动打开浏览器，请手动访问: http://localhost:{port}")
        
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 正在关闭服务...")
        httpd.server_close()
        sys.exit(0)

if __name__ == "__main__":
    port_to_run = 8000
    if len(sys.argv) > 1:
        try:
            port_to_run = int(sys.argv[1])
        except ValueError:
            pass
    run_server(port_to_run)
