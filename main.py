import json
import os
import asyncio
from datetime import datetime
import urllib3
import requests
from astrbot.api.all import *
from astrbot.api.message_components import Plain
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 禁用 requests 的 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@register("silicon_billing", "AstrBot", "SiliconCloud 账单监控与提醒插件", "1.0.0")
class SiliconBillingPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        # 读取 config.json 中的配置
        self.config = config

        # 本地数据文件路径 (用于持久化保存你的限额和绑定的QQ)
        self.data_dir = os.path.dirname(__file__)
        self.limits_file = os.path.join(self.data_dir, "limits.json")
        self.bind_file = os.path.join(self.data_dir, "bind.json")

        self.key_limits = self.load_json(self.limits_file, {})
        self.notify_binds = self.load_json(self.bind_file, {})

        # 启动定时任务
        self.scheduler = AsyncIOScheduler()
        self.setup_cron()
        self.scheduler.start()

    def load_json(self, filepath, default):
        """读取本地持久化数据"""
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        return default

    def save_json(self, filepath, data):
        """保存本地持久化数据"""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def setup_cron(self):
        """设置定时任务"""
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
        headers = {
            "accept": "application/json, text/plain, */*",
            "cookie": self.config.get("cookie", ""),
            "x-subject-id": self.config.get("x_subject_id", ""),
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
                response.raise_for_status()
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
                    print(f"账单获取失败: {data}")
                    break
            except Exception as e:
                print(f"网络请求出错: {e}")
                break
        return all_data

    async def generate_report(self):
        """生成文本报告和告警，并返回"""
        times = self.get_timestamps()

        # 使用 asyncio.to_thread 避免阻塞机器人的主事件循环
        month_data = await asyncio.to_thread(self.fetch_api_data_sync, times["month_start"], times["end_time"])
        today_data = await asyncio.to_thread(self.fetch_api_data_sync, times["today_start"], times["end_time"])

        if not month_data and not today_data:
            return "获取账单数据失败，请检查 Cookie 或网络状态。", []

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

        report_lines = []
        alerts = []

        for tail, stats in key_stats.items():
            limit = self.key_limits.get(tail)
            limit_str = f"{limit}" if limit is not None else "-"

            line = f"[{tail}]：▶ 本月累计消耗: {stats['monthly_total']:.4f} /{limit_str}  (今日新增: {stats['daily_total']:.4f} 元)"
            if stats['monthly_net'] > 0 or stats['daily_net'] > 0:
                line += f"\n  ▶ ⚠️ 注意：存在实际扣费！(本月实扣: {stats['monthly_net']:.4f} 元)"
            report_lines.append(line)

            if limit is not None and stats["monthly_total"] >= limit:
                alerts.append(
                    f"⚠️ 【超限提醒】[{tail}] 本月消耗 {stats['monthly_total']:.4f} /{limit}，已达上限！(今日新增: {stats['daily_total']:.4f} 元)")

        return "\n".join(report_lines) if report_lines else "当前无账单数据。", alerts

    # ==========================================
    # 机器人指令交互区域
    # ==========================================

    @filter.command("sc_bind")
    async def sc_bind(self, event: AstrMessageEvent):
        '''绑定当前会话为定时账单接收处'''
        # 记录下当前消息发送者所在的平台和 ID
        self.notify_binds = {
            "provider_id": event.get_provider_id(),
            "target_id": event.message_obj.sender_id  # 如果你想发到群里，这里可以改判断 group_id
        }
        self.save_json(self.bind_file, self.notify_binds)
        yield event.plain_result("✅ 绑定成功！之后的定时账单汇报将会发送到这里。")

    @filter.command("sc_check")
    async def sc_check(self, event: AstrMessageEvent):
        '''立刻检查一次账单情况'''
        yield event.plain_result("正在拉取最新账单数据，请稍候...")
        report, alerts = await self.generate_report()
        msg = f"📊 SiliconCloud 账单速报\n\n{report}"
        if alerts:
            msg += "\n\n" + "\n".join(alerts)
        yield event.plain_result(msg)

    @filter.command("sc_add")
    async def sc_add(self, event: AstrMessageEvent, tail: str, limit: float):
        '''添加/修改 Key 限额。用法: /sc_add [尾号] [金额]'''
        self.key_limits[tail] = float(limit)
        self.save_json(self.limits_file, self.key_limits)
        yield event.plain_result(f"✅ 成功设置 Key [{tail}] 的限额为 {limit} 元。")

    @filter.command("sc_del")
    async def sc_del(self, event: AstrMessageEvent, tail: str):
        '''删除 Key 限额。用法: /sc_del [尾号]'''
        if tail in self.key_limits:
            del self.key_limits[tail]
            self.save_json(self.limits_file, self.key_limits)
            yield event.plain_result(f"🗑️ 已删除 Key [{tail}] 的限额监控。")
        else:
            yield event.plain_result(f"❌ 找不到尾号为 [{tail}] 的监控记录。")

    @filter.command("sc_list")
    async def sc_list(self, event: AstrMessageEvent):
        '''查看当前设定的所有 Key 限额'''
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
        if not self.notify_binds:
            print("[SiliconCloud] 定时播报未触发：未绑定接收方。请对机器人发送 /sc_bind")
            return

        report, alerts = await self.generate_report()

        msg = f"🔔 定时账单播报\n\n{report}"
        if alerts:
            msg += "\n\n" + "\n".join(alerts)

        provider_id = self.notify_binds.get("provider_id")
        target_id = self.notify_binds.get("target_id")

        try:
            # 向绑定的 ID 发送消息
            chain = MessageChain().message([Plain(msg)])
            await self.context.send_message(provider_id, target_id, chain)
        except Exception as e:
            print(f"[SiliconCloud] 定时账单发送失败: {e}")