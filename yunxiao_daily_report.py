#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import datetime
import hmac
import hashlib
import base64
import urllib.parse
import requests
from dotenv import load_dotenv

# 加载环境变量配置文件
load_dotenv()

# 获取并清理配置项（防止 GitHub Secrets 复制时多带了引号、空格或换行）
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

# 检查必要参数
if not all([YUNXIAO_PAT, YUNXIAO_ORG_ID, YUNXIAO_PROJECT_ID, DINGTALK_WEBHOOK]):
    print("❌ 错误: 缺少必要配置环境变量！请检查当前目录下的 .env 文件是否配置完整。")
    print(f"当前配置状态:")
    print(f"- YUNXIAO_PAT: {'已设置' if YUNXIAO_PAT else '未设置'}")
    print(f"- YUNXIAO_ORG_ID: {'已设置' if YUNXIAO_ORG_ID else '未设置'}")
    print(f"- YUNXIAO_PROJECT_ID: {'已设置' if YUNXIAO_PROJECT_ID else '未设置'}")
    print(f"- DINGTALK_WEBHOOK: {'已设置' if DINGTALK_WEBHOOK else '未设置'}")
    sys.exit(1)

# 初始化今日的起始时间（强制使用北京时间 UTC+8）
utc_now = datetime.datetime.utcnow()
beijing_now = utc_now + datetime.timedelta(hours=8)
local_today = beijing_now.date()

category_map = {
    'Req': '需求',
    'Task': '任务',
    'Bug': '缺陷'
}

def is_timestamp_on_date(timestamp_val, target_date):
    """判断给定的时间戳或日期字符串是否为指定日期"""
    if not timestamp_val:
        return False
    
    # 如果是毫秒时间戳（数字或全数字字符串）
    if isinstance(timestamp_val, (int, float)):
        ts = timestamp_val / 1000.0
    elif isinstance(timestamp_val, str) and timestamp_val.isdigit():
        ts = int(timestamp_val) / 1000.0
    else:
        # 如果是 ISO-8601 或其他格式的日期时间字符串
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
        dt = datetime.datetime.fromtimestamp(ts)
        return dt.date() == target_date
    except Exception as e:
        print(f"⚠️ 转换时间戳失败: {ts}, 错误: {e}")
        return False

def is_modified_on_date(gmt_modified, target_date):
    """判断修改时间是否为指定日期"""
    return is_timestamp_on_date(gmt_modified, target_date)

def is_status_updated_on_date(item, target_date):
    """判断状态更新时间是否为指定日期"""
    return is_timestamp_on_date(item.get("updateStatusAt"), target_date)

def is_modified_today(gmt_modified):
    """判断修改时间是否为今天"""
    return is_modified_on_date(gmt_modified, local_today)

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
    """从工作项自定义字段中提取实际工时"""
    for cf in item.get("customFieldValues", []):
        field_id = cf.get("fieldId")
        field_name = cf.get("fieldName")
        # 支持工时字段 ID 或名称匹配
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
    """使用 workitems:search 接口获取项目中的需求、任务和缺陷"""
    url = f"https://openapi-rdc.aliyuncs.com/oapi/v1/projex/organizations/{YUNXIAO_ORG_ID}/workitems:search"
    headers = {
        "x-yunxiao-token": YUNXIAO_PAT,
        "Content-Type": "application/json"
    }
    
    work_items = []
    
    for cat in ['Req', 'Task', 'Bug']:
        print(f"🔄 正在获取类型为 [{category_map.get(cat)}] 的工作项列表...")
        payload = {
            "spaceId": YUNXIAO_PROJECT_ID,
            "spaceType": "Project",
            "category": cat,
            "conditions": "{\"conditionGroups\":[]}"
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=15)
            if response.status_code != 200:
                print(f"❌ 请求失败 HTTP {response.status_code}: {response.text}")
                continue
                
            items = response.json()
            if isinstance(items, list):
                work_items.extend(items)
                print(f"✅ 成功获取到 {len(items)} 个 [{category_map.get(cat)}] 工作项")
            else:
                print(f"⚠️ 返回数据格式不正确: {items}")
        except Exception as e:
            print(f"❌ 请求接口发生异常: {e}")
            
    print(f"📊 项目中累计拉取到 {len(work_items)} 个工作项，准备进行今日更新时间筛选...")
    return work_items

def build_markdown_report(grouped_items):
    """根据分组好的工作项生成日报 Markdown 内容"""
    today_str = local_today.strftime("%Y-%m-%d")
    markdown_lines = [
        f"### 📋 云效项目今日日报汇总 ({today_str})",
        f"**项目ID**: `{YUNXIAO_PROJECT_ID}`",
        f"---"
    ]
    
    if not grouped_items:
        markdown_lines.append("今日项目内没有工作项更新。")
    else:
        for person, items in grouped_items.items():
            in_progress = []
            todo = []
            completed = []
            person_total_hours = 0.0
            
            for it in items:
                subject = it.get("subject", "无标题")
                
                # 解析状态名称
                status_val = it.get("status")
                if isinstance(status_val, dict):
                    status_name = status_val.get("name") or status_val.get("displayName") or str(status_val)
                else:
                    status_name = str(status_val)
                
                # 获取工时
                hours = get_actual_hours(it)
                person_total_hours += hours
                
                # 格式化工时显示
                hours_str = f", 工时: {hours}h" if hours > 0 else ""
                bullet = f"- {subject} ({status_name}{hours_str})"
                
                if status_name == "已完成":
                    completed.append(bullet)
                elif status_name == "待处理":
                    todo.append(bullet)
                else:
                    in_progress.append(bullet)
            
            # 拼接该成员段落
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
                
    # 强制包含机器人设定的“自定义关键词”，保证发送成功
    content = "\n".join(markdown_lines)
    if DINGTALK_KEYWORD not in content:
        content += f"\n\n*(通知类型: {DINGTALK_KEYWORD})*"
        
    return content

def send_to_dingtalk(text_content):
    """发送 Markdown 日报至钉钉群机器人 Webhook"""
    print("📤 开始推送日报至钉钉...")
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
    
    try:
        response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
        if response.status_code == 200:
            print("✅ 日报成功推送至钉钉群")
            print(response.json())
        else:
            print(f"❌ 日报推送失败 HTTP {response.status_code}: {response.text}")
    except Exception as e:
        print(f"❌ 发送至钉钉发生异常: {e}")



def main():
    print(f"⏰ 开始执行日报统计任务, 当前日期: {local_today.strftime('%Y-%m-%d')}")
    
    # 1. 获取所有的工作项
    all_items = fetch_work_items()
    
    # 2. 筛选今日计划活跃的工作项并进行人员分组
    grouped_items = {}
    filtered_count = 0
    
    for item in all_items:
        if not is_active_on_date(item, local_today):
            continue
            
        filtered_count += 1
        
        # 寻找对应的姓名 (直接从 assignedTo 对象获取)
        assigned_to = item.get("assignedTo")
        if isinstance(assigned_to, dict):
            assignee_name = assigned_to.get("name") or "未指派"
        else:
            assignee_name = "未指派"
            
        if assignee_name not in grouped_items:
            grouped_items[assignee_name] = []
        grouped_items[assignee_name].append(item)
        
    print(f"🔍 筛选出今日 ({local_today.strftime('%Y-%m-%d')}) 有更新的工作项共 {filtered_count} 个")
    
    # 3. 生成日报 Markdown 内容
    markdown_content = build_markdown_report(grouped_items)
    
    # 4. 模拟推送至钉钉群
    send_to_dingtalk(markdown_content)

if __name__ == "__main__":
    main()
