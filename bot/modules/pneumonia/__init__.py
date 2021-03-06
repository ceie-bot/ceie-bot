import json
import re
import typing
import traceback
from telethon import TelegramClient, events, sync
from telethon import functions, utils
import telethon
import socks
import asyncio
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.job import Job
from apscheduler.jobstores.base import ConflictingIdError
from aiocqhttp.message import Message, MessageSegment

import datetime

from ... import log
from ... import bot_module
from ...bot_module import Interceptor, InputVarAttribute
from ... import const
from ... import util
from ... import db


REGEX_NEWS_EXTRACT_JSON = re.compile(r'try\s*\{\s*window\.getTimelineService\s*\=\s*(?P<json>[^\<]*)\}\s*catch\s*\(\s*e\s*\)\s*\{\s*\}')
REGEX_BYREGION_EXTRACT_JSON = re.compile(r'try\s*\{\s*window\.getAreaStat\s*\=\s*(?P<json>[^\<]*)\}\s*catch\s*\(\s*e\s*\)\s*\{\s*\}')
REGEX_TOTAL_EXTRACT_JSON = re.compile(r'try\s*\{\s*window\.getStatisticsService\s*\=\s*(?P<json>[^\<]*)\}\s*catch\s*\(\s*e\s*\)\s*\{\s*\}')
REGEX_SINA_EXTRACT_JSON = re.compile(r'window\.SM\s*=\s*(?P<json>[^\n\;]*)\;?\n')

class PneumoniaBotModule(bot_module.BotModule):
    @classmethod
    async def on_init(cls):
        pass

    @classmethod
    def news_obj_to_str(cls, obj):
        return "%s@%s：%s\n%s\n\nfrom%s %s" % (
            datetime.datetime.fromtimestamp(obj["modifyTime"] / 1000, tz=datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
            obj["provinceName"],
            obj["title"],
            obj["summary"],
            obj["infoSource"],
            obj["sourceUrl"],
        )

    @classmethod
    async def check_alert_update(cls, job_id, bot, context):
        try:
            data = await util.http_get("https://3g.dxy.cn/newh5/view/pneumonia", timeout_secs=3)
            data = REGEX_NEWS_EXTRACT_JSON.search(data).group("json")
            data = json.loads(data)

            history_var_name = "pneumonia_alerts_new2"
            
            history_alerts = json.loads(db.get_variable(util.get_identity(context, const.GROUP), history_var_name, "[" + str(data['result'][0]["id"]) + "]"))

            result = "有新的疫情动态"
            count = 0
            for news_obj in data['result']:
                alert = cls.news_obj_to_str(news_obj)
                alert_spoken = alert in history_alerts
                alert_is_old = news_obj["id"] < history_alerts[-1]

                if alert_is_old or count >= 5:
                    break

                if not alert_spoken:
                    history_alerts.insert(0, alert)
                    result += "\n\n" + alert
                    count += 1

            if count:
                await bot.send(context, result)
                
            db.set_variable(util.get_identity(context, const.GROUP), history_var_name, json.dumps(history_alerts))
            return
        except Exception:
            tb = traceback.format_exc().strip()
            await bot.send(context, "获取疫情动态失败")
            await log.error("获取疫情动态失败：错误在 Context " + repr(context) + "：\n" + tb)
            return

    @classmethod
    def alert_to_digest(cls, alert):
        if alert is None:
            return None
        alert_split = alert.split("】")
        return (alert_split[0] + ("】" if len(alert_split) > 1 else "")).replace("\n", " ")

    @classmethod
    async def check_alert_update_tg(cls, job_id, bot, context, digest=False):
        try:
            client = None # TODO: 需要建立一个 telethon 的 client
            await client.connect()
            messages = (await client(functions.messages.GetHistoryRequest(peer="nCoV2019", offset_id=0, offset_date=0, add_offset=0, limit=10, max_id=0, min_id=0, hash=0))).messages

            history_var_name = "pneumonia_alerts_new2_tg"

            
            history_alerts = json.loads(db.get_variable(util.get_identity(context, const.GROUP), history_var_name, "[" + str(messages[0].id) + "]"))

            result = "有新的疫情动态（Telegram）"
            count = 0
            for msg_obj in messages:
                if type(msg_obj) != telethon.tl.types.Message:
                    continue
                alert = msg_obj.message
                alert_spoken = alert in history_alerts or ("msgid" + str(msg_obj.id)) in history_alerts
                alert_is_old = msg_obj.id < history_alerts[-1]
                alert = "\n" + alert.replace("\n\n", "\n")
                if digest:
                    alert_digest = cls.alert_to_digest(alert)
                    if alert_digest in history_alerts:
                        alert_spoken = True

                if alert_is_old or count >= 5:
                    break

                if alert and not alert_spoken:
                    history_alerts.insert(0, "msgid" + str(msg_obj.id))
                    history_alerts.insert(0, alert)
                    if digest:
                        history_alerts.insert(0, alert_digest)
                        alert = alert_digest
                    date_toutc8 = msg_obj.date.astimezone(tz=datetime.timezone(datetime.timedelta(hours=8)))
                    result += "\n" + alert

                    has_url = False
                    for entity in msg_obj.entities:
                        if type(entity) == telethon.tl.types.MessageEntityTextUrl:
                            result += "\n - " + entity.url
                            has_url = True

                    if has_url:
                        result += "\n"
                    result += "@" + date_toutc8.strftime("%Y-%m-%d %H:%M:%S") + " " + date_toutc8.tzname()
                    count += 1

            history_alerts = history_alerts[:(99 if len(history_alerts) > 100 else len(history_alerts) - 1)] + [history_alerts[-1]]
            db.set_variable(util.get_identity(context, const.GROUP), history_var_name, json.dumps(history_alerts))

            # await log.warning("%d %d %d %d", count, messages[0].id, messages[1].id, history_alerts[-1])
            if count:
                await bot.send(context, result)
            return
        except Exception:
            tb = traceback.format_exc().strip()
            await log.warning("%s", tb)
            await bot.send(context, "获取疫情动态（Telegram）失败")
            await log.error("获取疫情动态（Telegram）失败：错误在 Context " + repr(context) + "：\n" + tb)
            return


    @classmethod
    async def all_state_intercept(cls, bot, context, msg, input_vars, update_vars, extras, **kwargs):
        if u'疫情' in msg and u'提醒' in msg:
            if u'立即' in msg:
                await cls.check_alert_update_tg("pneunomia_alert_tg/%s" % (util.get_identity(context, const.GROUP)), util.global_bot, context, False)
                return True

            if u'取消' in msg:
                if u'telegram' in msg.lower():
                    any = False
                    result = "取消了疫情动态（Telegram）。"
                    j: Job
                    for j in util.get_jobs():
                        real_id = j.id.split(":")[-1]
                        args = real_id.split("/")
                        if args[0] == "pneunomia_alert_tg" and args[1] == util.get_identity(context, const.GROUP):
                            j.remove()
                            any = True

                    if any:
                        await bot.send(context, result)
                    return True
                else:
                    any = False
                    result = "取消了疫情动态。"
                    j: Job
                    for j in util.get_jobs():
                        real_id = j.id.split(":")[-1]
                        args = real_id.split("/")
                        if args[0] == "pneunomia_alert" and args[1] == util.get_identity(context, const.GROUP):
                            j.remove()
                            any = True

                    if any:
                        await bot.send(context, result)
                    return True

            else:
                if u'telegram' in msg.lower():
                    digest = u'摘要' in msg
                    jobcall = util.make_jobcall(cls.check_alert_update_tg, context, digest)
                    job_id = "pneunomia_alert_tg/%s" % (util.get_identity(context, const.GROUP))
                    
                    try:
                        util.add_job(jobcall, trigger=IntervalTrigger(minutes=9), id=job_id)
                    except ConflictingIdError:
                        await bot.send(context, "设置开启疫情动态（Telegram）：失败：已有这个任务")
                        return True

                    await bot.send(context, "设置开启疫情动态（Telegram）：成功" + ("（摘要）" if digest else ""))
                    return True
                else:
                    jobcall = util.make_jobcall(cls.check_alert_update, context)
                    job_id = "pneunomia_alert/%s" % (util.get_identity(context, const.GROUP))
                    
                    try:
                        util.add_job(jobcall, trigger=IntervalTrigger(minutes=10), id=job_id)
                    except ConflictingIdError:
                        await bot.send(context, "设置开启疫情动态：失败：已有这个任务")
                        return True

                    await bot.send(context, "设置开启疫情动态：成功")
                    return True

        elif u'疫情' == msg.strip():
            data = await util.http_get("https://3g.dxy.cn/newh5/view/pneumonia")
            byregion_data = REGEX_BYREGION_EXTRACT_JSON.search(data).group("json")
            byregion_data = json.loads(byregion_data)

            news_data = REGEX_NEWS_EXTRACT_JSON.search(data).group("json")
            news_data = json.loads(news_data)

            result = ""

            for region in byregion_data:
                result += ("%s：" % (region["provinceShortName"]))
                result += ("确诊 %s 例" % (region["confirmedCount"]))
                if region["suspectedCount"]:
                    result += ("，疑似 %s 例" % (region["suspectedCount"]))
                if region["curedCount"]:
                    result += ("，治愈 %s 例" % (region["curedCount"]))
                if region["deadCount"]:
                    result += ("，死亡 %s 例" % (region["deadCount"]))
                result += "\n"

            result += "\n"

            total_data = REGEX_TOTAL_EXTRACT_JSON.search(data).group("json")
            total_data = json.loads(total_data)

            remarks = []
            notes = []
            for i in range(5):
                remark = total_data.get("remark" + str(i + 1), "").strip()
                if remark != "":
                    remarks.append(remark)

            for i in range(5):
                note = total_data.get("note" + str(i + 1), "").strip()
                if note != "":
                    notes.append(note)

            remarks = "\n".join(remarks)
            notes = "\n".join(notes)
            total_data["remarks"] = remarks
            total_data["notes"] = notes
            total_data["confirmedIncr"] = total_data.get("confirmedIncr", "???")
            total_data["suspectedIncr"] = total_data.get("suspectedIncr", "???")
            total_data["seriousIncr"] = total_data.get("seriousIncr", "???")
            total_data["curedIncr"] = total_data.get("curedIncr", "???")
            total_data["deadIncr"] = total_data.get("deadIncr", "???")

            result += "总计全国确诊 %(confirmedCount)s 例（较昨日+%(confirmedIncr)s），疑似 %(suspectedCount)s 例（较昨日+%(suspectedIncr)s），重症 %(seriousCount)s 例（较昨日+%(seriousIncr)s），治愈 %(curedCount)s 例（较昨日+%(curedIncr)s），死亡 %(deadCount)s 例（较昨日+%(deadIncr)s）\n\n%(notes)s\n%(remarks)s" % total_data

            result += "\n"

            for i, news_obj in enumerate(news_data[0:5]):
                result += "\n" + ("%d. " % (i+1)) + datetime.datetime.fromtimestamp(news_obj["modifyTime"] / 1000, tz=datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S") + " - " + news_obj["title"]

            await bot.send(context, Message(result))

            # data = await util.http_get("https://news.sina.cn/zt_d/yiqing0121")

            # sina_data = REGEX_SINA_EXTRACT_JSON.search(data).group("json")
            # sina_data = json.loads(sina_data)

            # pic_data_url = sina_data["data"]["apiRes"]["data"]["components"][0]["data"][0]["pic"]

            # await bot.send(context, MessageSegment(MessageSegment(type_='image', data={'file': pic_data_url})))

            # pic_data_url = total_data["imgUrl"]

            # await bot.send(context, MessageSegment(MessageSegment(type_='image', data={'file': pic_data_url})))

            pic_data = total_data["quanguoTrendChart"]

            for pic_data_obj in pic_data:
                await bot.send(context, Message(pic_data_obj["title"]) + MessageSegment(type_='image', data={'file': pic_data_obj["imgUrl"]}))

            return True

        return False

module_class = PneumoniaBotModule
