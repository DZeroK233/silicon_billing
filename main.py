import json
import os
import asyncio
from datetime import datetime
import urllib3
import requests
from astrbot.api.all import *
# 显式导入 filter 和 AstrMessageEvent，覆盖 Python 的内置 filter
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 禁用 requests 的 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@register("silicon_billing", "AstrBot", "SiliconCloud 账单监控与提醒插件", "1.0.0")
class SiliconBillingPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        # AstrBot V4 规范：配置由 _conf_schema.json 定义，并由框架自动注入到 config 参数
        self.config = config

        limits_raw = self.config.get("key_limits", "{}")
        if isinstance(limits_raw, dict):
            self.key_limits = limits_raw
        else:
            try:
                self.key_limits = json.loads(limits_raw)
            except Exception:
                self.key_limits = {}

        self.cached_stats = {}
        self.last_fetch_time = 0

        # 启动定时任务
        self.scheduler = AsyncIOScheduler()
        self.setup_cron()
        self.scheduler.start()

    def setup_cron(self):
        """设置定时任务"""
        # 从配置中获取定时时间，名称需与 _conf_schema.json 中的字段对应
        cron_time = self.config.get("cron_time", "10:00")
        try:
            hour, minute = map(int, cron_time.split(":"))
        except ValueError:
            hour, minute = 10, 0  # 解析失败默认 10:00

        # 每天指定时间执行 cron_task
        self.scheduler.add_job(
            self.cron_task,
            'cron',
            hour=hour,
            minute=minute,
            id="sc_billing_job",
            replace_existing=True
        )

    # ==========================================
    # API 核心请求逻辑 (封装为可在异步调用的方法)
    # ==========================================
    def get_timestamps(self):
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_time = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        return {
            "today_start": int(today_start.timestamp() * 1000),
            "month_start": int(month_start.timestamp() * 1000),
            "end_time": int(end_time.timestamp() * 1000)
        }

    def fetch_api_data_sync(self, start_time, end_time):
        url = "https://cloud.siliconflow.cn/panel-server/api/v1/bill/items/allocation_aggregate"
        # 这里的 key 必须与 _conf_schema.json 中定义的一致
        import logging
        logger = logging.getLogger("astrbot")

        headers = {
            "accept": "application/json, text/plain, */*",
            "cookie": self.config.get("cookie", "").strip(),
            "x-subject-id": self.config.get("x_subject_id", "").strip(),
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0"
        }

        all_data = []
        current_page = 1
        page_size = 10

        while True:
            params = {
                "aggregateByApiKey": "true",
                "aggregateByModelName": "false",
                "aggregateByUnit": "true",
                "current": str(current_page),
                "pageSize": str(page_size),
                "startTime": start_time,
                "endTime": end_time,
                "type": "3"
            }
            try:
                response = requests.get(url, headers=headers, params=params, verify=False, timeout=10)
                if response.status_code != 200:
                    logger.error(f"[SiliconCloud] 账单获取失败(HTTP {response.status_code}): {response.text}")
                    break
                data = response.json()

                if data.get("code") == 20000:
                    items = data.get("data", {}).get("list", [])
                    all_data.extend(items)
                    total = data.get("data", {}).get("pagination", {}).get("total", 0)
                    if current_page * page_size >= total:
                        break
                    else:
                        current_page += 1
                else:
                    logger.error(f"[SiliconCloud] 账单获取失败: {data}")
                    break
            except Exception as e:
                logger.error(f"[SiliconCloud] 网络请求出错: {e}")
                break
        return all_data

    async def get_cached_stats(self, force_refresh=False):
        now = datetime.now().timestamp()
        if not force_refresh and self.cached_stats and (now - self.last_fetch_time < 3600):
            return self.cached_stats
            
        times = self.get_timestamps()

        # 使用 asyncio.to_thread 避免阻塞机器人，因为 requests 是同步的
        month_data = await asyncio.to_thread(self.fetch_api_data_sync, times["month_start"], times["end_time"])
        today_data = await asyncio.to_thread(self.fetch_api_data_sync, times["today_start"], times["end_time"])

        if not month_data and not today_data:
            return None

        key_stats = {}

        # 统计本月
        for item in month_data:
            api_key = item.get("apiKey", "")
            if not api_key or api_key == "-": continue
            tail = api_key[-4:]
            deduct = float(item.get("deductAmount", 0))
            net = float(item.get("netAmount", 0))
            key_stats[tail] = {
                "monthly_total": deduct + net,
                "monthly_net": net,
                "daily_total": 0.0,
                "daily_net": 0.0
            }

        # 统计今日
        for item in today_data:
            api_key = item.get("apiKey", "")
            if not api_key or api_key == "-": continue
            tail = api_key[-4:]
            deduct = float(item.get("deductAmount", 0))
            net = float(item.get("netAmount", 0))
            if tail in key_stats:
                key_stats[tail]["daily_total"] = deduct + net
                key_stats[tail]["daily_net"] = net
                
        self.cached_stats = key_stats
        self.last_fetch_time = now
        return self.cached_stats

    async def generate_report(self, force_refresh=True):
        """生成文本报告和告警，并返回"""
        key_stats = await self.get_cached_stats(force_refresh=force_refresh)
        
        if key_stats is None:
            return "❌ 获取账单数据失败，请前往控制台查看详细的报错日志以便定位问题（可能是配置失效或无网络）。", []

        report_lines = []
        alerts = []

        for tail, stats in key_stats.items():
            limit = self.key_limits.get(tail)
            limit_str = f"{limit}" if limit is not None else "-"

            line = f"[{tail}]：▶ 本月累计消耗: {stats['monthly_total']:.4f} /{limit_str}  (今日新增: {stats['daily_total']:.4f} 元)"
            if stats['monthly_net'] > 0:
                line += f"\n  ▶ ⚠️ 注意：存在实际扣费！(本月实扣: {stats['monthly_net']:.4f} 元)"
            report_lines.append(line)

            if limit is not None and stats["monthly_total"] >= limit:
                alerts.append(
                    f"⚠️ 【超限提醒】[{tail}] 本月消耗 {stats['monthly_total']:.4f} /{limit}，已达上限！(今日新增: {stats['daily_total']:.4f} 元)")

        return "\n".join(report_lines) if report_lines else "当前无有效账单数据。", alerts

    # ==========================================
    # 机器人指令交互区域
    # ==========================================

    def check_admin(self, event: AstrMessageEvent):
        admin_ids = self.config.get("admin_ids", [])
        return event.unified_msg_origin in admin_ids

    @filter.command("sc")
    async def sc_cmd(self, event: AstrMessageEvent, arg: str = ""):
        '''查询指定尾号的账单。用法: /sc 尾号'''
        arg = arg.strip()
            
        # 缓存机制查询特定尾号
        tail = arg
        stats = await self.get_cached_stats(force_refresh=False)
        if stats is None:
            yield event.plain_result("❌ 获取账单数据失败，请提醒管理员检查配置。")
            return
            
        if tail not in stats:
            yield event.plain_result(f"❌ 找不到 [{tail}] 的账单数据。")
            return
            
        data = stats[tail]
        limit = self.key_limits.get(tail)
        limit_str = f"{limit}" if limit is not None else "-"
        
        msg = f"📊 SiliconCloud 账单速报 [{tail}]\n"
        
        # 判断是否超限并显示不同样式
        if limit is not None and data['monthly_total'] >= limit:
            msg += f"⚠️ 【已超限】本月消耗: {data['monthly_total']:.4f} /{limit} 元\n"
        else:
            msg += f"▶ 本月累计: {data['monthly_total']:.4f} /{limit_str} 元\n"
            
        msg += f"▶ 今日新增: {data['daily_total']:.4f} 元"
        
        yield event.plain_result(msg)



    @filter.command("sc_help")
    async def sc_help(self, event: AstrMessageEvent):
        '''查询用户唯一ID'''
        yield event.plain_result(f"💡 您的唯一 ID (unified_msg_origin) 为：\n{event.unified_msg_origin}\n\n如果您是管理员，请将其填入插件配置的 admin_ids 列表中以获取管理指令执行权限。")

    @filter.command("sc_check")
    async def sc_check(self, event: AstrMessageEvent):
        '''立刻检查一次总账单情况'''
        if not self.check_admin(event):
            yield event.plain_result("❌ 权限不足，只有管理员才可执行此操作。")
            return
        yield event.plain_result("正在拉取最新账单数据，请稍候...")
        report, alerts = await self.generate_report(force_refresh=True)
        msg = f"📊 SiliconCloud 账单速报\n\n{report}"
        if alerts:
            msg += "\n\n" + "\n".join(alerts)
        yield event.plain_result(msg)

    @filter.command("sc_add")
    async def sc_add(self, event: AstrMessageEvent, tail: str, limit: float):
        '''添加/修改 Key 限额。用法: /sc_add 尾号 金额'''
        if not self.check_admin(event):
            yield event.plain_result("❌ 权限不足。")
            return
        self.key_limits[tail] = float(limit)
        self.config["key_limits"] = json.dumps(self.key_limits, ensure_ascii=False, indent=2)
        self.config.save_config()
        yield event.plain_result(f"✅ 成功设置 Key [{tail}] 的限额为 {limit} 元。")

    @filter.command("sc_del")
    async def sc_del(self, event: AstrMessageEvent, tail: str):
        '''删除 Key 限额。用法: /sc_del 尾号'''
        if not self.check_admin(event):
            yield event.plain_result("❌ 权限不足。")
            return
        if tail in self.key_limits:
            del self.key_limits[tail]
            self.config["key_limits"] = json.dumps(self.key_limits, ensure_ascii=False, indent=2)
            self.config.save_config()
            yield event.plain_result(f"🗑️ 已删除 Key [{tail}] 的限额监控。")
        else:
            yield event.plain_result(f"❌ 找不到尾号为 [{tail}] 的监控记录。")

    @filter.command("sc_list")
    async def sc_list(self, event: AstrMessageEvent):
        '''查看当前设定的所有 Key 限额'''
        if not self.check_admin(event):
            yield event.plain_result("❌ 权限不足。")
            return
        if not self.key_limits:
            yield event.plain_result("📝 当前未设置任何限额监控，请使用 /sc_add 添加。")
            return

        msg = "📝 当前设定的监控名单:\n"
        for tail, limit in self.key_limits.items():
            msg += f"- [{tail}] : {limit} 元\n"
        yield event.plain_result(msg.strip())

    # ==========================================
    # 定时任务触发器
    # ==========================================
    async def cron_task(self):
        """定时任务执行主体"""
        if not self.config.get("enable_cron", True):
            return
            
        notify_qq = self.config.get("notify_qq", "")
        if not notify_qq:
            return

        report, alerts = await self.generate_report(force_refresh=True)

        msg = f"🔔 定时账单播报\n\n{report}"
        if alerts:
            msg += "\n\n" + "\n".join(alerts)

        try:
            chain = MessageChain().message(msg)
            sent = False
            for platform in self.context.platform_manager.platform_insts:
                # 统一 session 格式: platform_id:message_type:session_id
                msg_origin = f"{platform.meta().id}:FriendMessage:{notify_qq}"
                try:
                    success = await self.context.send_message(msg_origin, chain)
                    if success:
                        sent = True
                        break # 发送成功一个平台即可
                except Exception:
                    continue
            
            if not sent:
                print(f"[SiliconCloud] 定时账单发送失败，无法通过当前启用的平台发送到指定 QQ: {notify_qq}")
        except Exception as e:
            print(f"[SiliconCloud] 定时账单发送错误: {e}")