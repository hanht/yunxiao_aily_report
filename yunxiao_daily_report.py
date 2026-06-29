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
import socket
import subprocess
from dotenv import load_dotenv

# DNS解析修复补丁：应对某些网络环境下 openapi-rdc.aliyuncs.com 域名解析失败的问题
_original_getaddrinfo = socket.getaddrinfo
_resolved_ips_cache = {}
_FALLBACK_IPS = [
    "118.178.223.77",
    "47.111.202.119",
    "118.178.223.76",
    "118.178.223.82",
    "118.178.223.120",
    "118.178.223.119",
    "47.111.202.125",
    "47.111.202.85"
]

def _custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host == 'openapi-rdc.aliyuncs.com':
        # Tier 1: Try system DNS first
        try:
            addr_list = _original_getaddrinfo(host, port, family, type, proto, flags)
            if addr_list:
                return addr_list
        except Exception:
            pass

        # Tier 2: Try public DNS via nslookup
        if host not in _resolved_ips_cache:
            for dns in ["223.6.6.6", "114.114.114.114"]:
                try:
                    res = subprocess.run(
                        ["nslookup", host, dns],
                        capture_output=True,
                        text=True,
                        timeout=3
                    )
                    if res.returncode == 0:
                        ips = []
                        for line in res.stdout.splitlines():
                            line = line.strip()
                            if "Address:" in line and "#" not in line:
                                parts = line.split("Address:")
                                if len(parts) > 1:
                                    ip = parts[1].strip()
                                    if ip != dns:
                                        ips.append(ip)
                        if ips:
                            _resolved_ips_cache[host] = ips[0]
                            break
                except Exception:
                    pass

        # Tier 3: Use hardcoded stable IPs
        if host not in _resolved_ips_cache:
            _resolved_ips_cache[host] = _FALLBACK_IPS[0]

        if host in _resolved_ips_cache:
            return _original_getaddrinfo(_resolved_ips_cache[host], port, family, type, proto, flags)

    return _original_getaddrinfo(host, port, family, type, proto, flags)

socket.getaddrinfo = _custom_getaddrinfo

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

def get_status_update_date(item):
    """获取状态更新的日期"""
    val = item.get("updateStatusAt")
    if not val:
        return None
    if isinstance(val, (int, float)):
        ts = val / 1000.0
    elif isinstance(val, str) and val.isdigit():
        ts = int(val) / 1000.0
    else:
        try:
            clean_str = val.replace('T', ' ').replace('Z', '')
            if '.' in clean_str:
                clean_str = clean_str.split('.')[0]
            dt = datetime.datetime.strptime(clean_str.strip(), "%Y-%m-%d %H:%M:%S")
            if 'Z' in val:
                dt = dt + datetime.timedelta(hours=8)
            return dt.date()
        except Exception:
            return None
    try:
        return datetime.datetime.fromtimestamp(ts).date()
    except Exception:
        return None

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

def get_status_on_date(item, target_date):
    """根据目标日期，动态计算工作项在当时的合理状态"""
    status_val = item.get("status")
    status_name = status_val.get("name") if isinstance(status_val, dict) else str(status_val)
    
    if status_name == "已完成":
        completion_date = get_status_update_date(item)
        if completion_date:
            if target_date == completion_date:
                return "已完成"
            elif target_date < completion_date:
                # 目标日期在完成日期之前，说明当时尚未完成
                # 根据计划开始时间来决定是"处理中"还是"待处理"
                start_date, _ = get_planned_dates(item)
                if start_date and target_date < start_date:
                    return "待处理"
                return "处理中"
            else:
                # 目标日期在完成日期之后
                return "已完成"
    return status_name

def is_active_on_date(item, target_date):
    """判断工作项在目标日期是否处于活动（计划）范围"""
    status_val = item.get("status")
    status_name = status_val.get("name") if isinstance(status_val, dict) else str(status_val)
    
    if status_name == "已完成":
        completion_date = get_status_update_date(item)
        if completion_date:
            if target_date == completion_date:
                return True
            elif target_date < completion_date:
                # 目标日期在完成日期之前，我们需要看计划范围
                start_date, end_date = get_planned_dates(item)
                if start_date or end_date:
                    if start_date and end_date:
                        return start_date <= target_date <= end_date
                    elif start_date:
                        return start_date <= target_date
                    else:
                        return target_date <= end_date
                # 如果没有计划时间，则默认不在之前的日期活跃
                return False
            else:
                # 目标日期在完成日期之后，已归档/不再活跃
                return False
        # 兜底：如果没有获取到完成时间，则按更新时间判断
        return is_status_updated_on_date(item, target_date)
        
    # 如果当前是"待处理"或"处理中"等未完成状态，判断目标日期是否在计划开始和结束时间之间
    start_date, end_date = get_planned_dates(item)
    if start_date or end_date:
        if start_date and end_date:
            return start_date <= target_date <= end_date
        elif start_date:
            return start_date <= target_date
        else:  # end_date
            return target_date <= end_date
            
    # 如果未完成状态且既没有计划开始也没有计划结束，回退到判断目标日期是否有修改
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
    url = f"https://openapi-rdc.aliyuncs.com/oapi/v1/projex/organizations/{YUNXIAO_ORG_ID}/workitems:search?perPage=200"
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

def build_markdown_report(grouped_items, query_date):
    """根据分组好的工作项生成日报 Markdown 内容"""
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



def is_china_workday(target_date):
    """判断是否为中国的工作日（包含调休补班，排除节假日和周末）"""
    # 1. 优先尝试使用公共节假日 API (timor.tech)
    date_str = target_date.strftime("%Y-%m-%d")
    url = f"https://timor.tech/api/holiday/info/{date_str}"
    try:
        res = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        if res.status_code == 200:
            data = res.json()
            if data.get("code") == 0:
                type_data = data.get("type", {})
                type_val = type_data.get("type")
                # 0: 工作日, 3: 调休 (属于补班工作日)
                if type_val == 0 or type_val == 3:
                    return True
                else:
                    return False
    except Exception as e:
        print(f"⚠️ 调用节假日 API 失败: {e}，将降级使用标准周末规则进行判断。")
        
    # 2. 降级方案：以标准周末（周六、周日）进行简单排除
    is_weekend = target_date.weekday() >= 5
    return not is_weekend

def main():
    target_date = local_today
    is_manual = False
    
    if len(sys.argv) > 1:
        is_manual = True
        try:
            target_date = datetime.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
            print(f"📌 使用命令行参数指定的日期: {target_date.strftime('%Y-%m-%d')}")
        except Exception:
            print(f"⚠️ 无效的日期参数: {sys.argv[1]}，将默认使用今日日期: {local_today.strftime('%Y-%m-%d')}")
            
    # 校验工作日：定时任务触发（或非手动指定日期的后台调度）时，自动跳过节假日/周末
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    is_schedule = (event_name == "schedule")
    is_github_manual = (event_name != "" and not is_schedule)
    
    if not is_manual and not is_github_manual:
        print("🔍 正在进行工作日属性校验...")
        if not is_china_workday(target_date):
            print(f"😴 目标日期 {target_date.strftime('%Y-%m-%d')} 为法定节假日或双休日，跳过日报生成与推送。")
            return
        print("📅 校验通过：今天是工作日/补班日，开始执行日报统计。")
            
    print(f"⏰ 开始执行日报统计任务, 目标日期: {target_date.strftime('%Y-%m-%d')}")
    
    # 1. 获取所有的工作项
    all_items = fetch_work_items()
    
    # 2. 筛选在目标日期活跃的工作项
    active_items = []
    for item in all_items:
        if is_active_on_date(item, target_date):
            active_items.append(item)
            
    # 3. 并行获取这些活跃工作项的工时日志
    effort_records_map = {}
    if active_items:
        headers = {
            "x-yunxiao-token": YUNXIAO_PAT,
            "Content-Type": "application/json"
        }
        import concurrent.futures
        def fetch_records(item_id):
            u = f"https://openapi-rdc.aliyuncs.com/oapi/v1/projex/organizations/{YUNXIAO_ORG_ID}/workitems/{item_id}/effortRecords"
            try:
                r = requests.get(u, headers=headers, timeout=10)
                if r.status_code == 200:
                    return item_id, r.json()
            except Exception:
                pass
            return item_id, []
            
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            futures = [executor.submit(fetch_records, item["id"]) for item in active_items]
            effort_records_map = dict(f.result() for f in concurrent.futures.as_completed(futures))
            
    # 4. 根据实际录入的工时或指派人进行人员分组
    grouped_items = {}
    total_hours = 0.0
    
    for item in active_items:
        item_id = item["id"]
        subject = item.get("subject", "无标题")
        status_name = get_status_on_date(item, target_date)
        
        # 提取当前类别名
        category = item.get("category")
        category_zh = category_map.get(category, category or "工作项")
        
        # 查找当天登记的工时记录
        records = effort_records_map.get(item_id, [])
        day_records = []
        for rec in records:
            gmt_start = rec.get("gmtStart")
            if gmt_start:
                rec_date = datetime.datetime.fromtimestamp(gmt_start / 1000.0).date()
                if rec_date == target_date:
                    day_records.append(rec)
                    
        if day_records:
            # 有当天登记的工时日志，按登记人分配工时
            for rec in day_records:
                owner_name = rec.get("owner", {}).get("name") or rec.get("creator", {}).get("name") or "未指派"
                hours = float(rec.get("actualTime", 0.0))
                total_hours += hours
                
                if owner_name not in grouped_items:
                    grouped_items[owner_name] = []
                    
                existing = None
                for existing_item in grouped_items[owner_name]:
                    if existing_item["id"] == item_id:
                        existing = existing_item
                        break
                if existing:
                    existing["hours"] += hours
                else:
                    grouped_items[owner_name].append({
                        "subject": subject,
                        "status": status_name,
                        "hours": hours,
                        "category_zh": category_zh,
                        "id": item_id
                    })
        else:
            # 没有当天登记的工时日志，则归属于任务的当前指派人，工时为 0h
            assigned_to = item.get("assignedTo")
            assignee_name = assigned_to.get("name") if isinstance(assigned_to, dict) else "未指派"
            if not assignee_name:
                assignee_name = "未指派"
                
            if assignee_name not in grouped_items:
                grouped_items[assignee_name] = []
                
            existing = None
            for existing_item in grouped_items[assignee_name]:
                if existing_item["id"] == item_id:
                    existing = existing_item
                    break
            if not existing:
                grouped_items[assignee_name].append({
                    "subject": subject,
                    "status": status_name,
                    "hours": 0.0,
                    "category_zh": category_zh,
                    "id": item_id
                })
                
    print(f"🔍 筛选出目标日期 ({target_date.strftime('%Y-%m-%d')}) 活跃工作项共 {len(active_items)} 个，登记总工时: {total_hours}h")
    
    # 5. 生成日报 Markdown 内容
    markdown_content = build_markdown_report(grouped_items, target_date)
    
    # 6. 推送至钉钉群
    send_to_dingtalk(markdown_content)

if __name__ == "__main__":
    main()
