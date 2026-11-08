import os
import time
import random
import re
import asyncio
import json
from datetime import datetime
from typing import Dict, Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import LLMResponse


@register(
    "enhanced_groupchat", "olozhika", "打破前缀限制的群聊强化深度互动插件", "1.0.0"
)
class EnhancedGroupChatPlugin(Star):
    def __init__(self, context: Context, config: Dict[str, Any] = None):
        super().__init__(context)
        self.context = context
        self.config = config if config else {}

        # 维护每个 session (群聊) 的窥屏与连击状态
        # key: session_id, value: dict
        self.session_states = {}

        logger.info("[APEG] 群聊强化插件已成功加载！")

    def _get_session_state(self, session_id: str) -> Dict[str, Any]:
        """安全地获取或初始化 session (群聊) 的状态"""
        if session_id not in self.session_states:
            self.session_states[session_id] = {
                "status": "probabilistic",  # "probabilistic" (1/n概率模式) 或 "peeping" (连击窥屏模式)
                "peep_start_time": 0.0,  # 刚进入连续窥屏状态的时间戳
                "last_ai_reply_time": 0.0,  # AI上一次回复的时间戳
                "is_llm_generating": False,  # 是否正在生成大模型回复
                "llm_start_time": 0.0,  # 本次大模型生成的起始时间戳
                "pending_messages": [],  # 大模型生成期间缓存的群友发言
                "just_exited_peeping": False,  # 是否刚退出窥屏状态
                "post_peep_count": 0,  # 退出窥屏后已记录的消息数
                "rolling_buffer": [],  # L条滚动缓存
                "has_discarded_messages": False,  # 是否有过丢弃行为
                "silent_countdown": 0,  # 静默倒计时，>0时跳过回复
                "last_flushed_buffer_snapshot": None,  # 上一次刷入历史的滚动缓存快照
                "undo_buffer_requested": False,  # 是否请求撤销缓存消息写入
                "time_before_messages": [],  # Time_before 等待期间收集的消息
                "time_before_collecting": False,  # 是否处于 time_before 收集中
                "time_after_end": 0.0,  # Time_after 静默期结束时间戳
            }
        return self.session_states[session_id]

    def _extract_image_files(self, event) -> list[str]:
        """从事件中提取图片文件的原始标记（file 字段），用于后续转述。"""
        from astrbot.core.message.components import Image

        return [
            comp.file
            for comp in event.message_obj.message
            if isinstance(comp, Image)
        ]

    async def _generate_image_caption(self, image_files: list[str]) -> str:
        """使用配置的图片转述 Provider 为图片生成文字描述。"""
        try:
            cfg = self.context.get_config()
            provider_id = cfg.get("default_image_caption_provider_id", "")
            if not provider_id:
                return ""
            prov = self.context.get_provider_by_id(provider_id)
            if not prov:
                return ""
            provider_settings = cfg.get("provider_settings", {})
            img_cap_prompt = provider_settings.get(
                "image_caption_prompt", "Please describe the image."
            )
            from astrbot.core.message.components import Image

            paths = []
            for spec in image_files:
                img = Image(file=spec)
                path = await img.convert_to_file_path()
                paths.append(path)
            from astrbot.core.utils.media_utils import compress_image

            compressed = []
            for p in paths:
                try:
                    cp = await compress_image(p)
                    compressed.append(cp)
                except Exception:
                    compressed.append(p)
            llm_resp = await prov.text_chat(
                prompt=img_cap_prompt,
                image_urls=compressed,
            )
            if llm_resp:
                return llm_resp.completion_text
            return ""
        except Exception as e:
            logger.error(f"[APEG] 图片转述失败: {e}")
            return ""

    async def _make_user_record(self, event, sender_id: str, sender_name: str) -> dict:
        """构造一条用户消息记录，包含身份标签，如有图片则生成转述一并注入。"""
        now_dt = datetime.now()
        now_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        content = [
            {"type": "text", "text": event.message_str},
            {
                "type": "text",
                "text": f"<system_reminder>User ID: {sender_id}, Nickname: {sender_name}\nCurrent datetime: {now_time_str} (UTC)</system_reminder>",
            },
        ]
        image_files = self._extract_image_files(event)
        if image_files:
            caption = await self._generate_image_caption(image_files)
            caption_text = f"<image_caption>{caption}</image_caption>" if caption else "[Image]"
            content.insert(-1, {"type": "text", "text": caption_text})
        return {"role": "user", "content": content}

    def _message_contains_keywords(self, content) -> bool:
        """检查消息内容是否包含保留关键词"""
        keep_keywords_str = self.config.get("keep_keywords", "").strip()
        if not keep_keywords_str:
            return False

        keywords = [
            k.strip().lower()
            for k in re.split(r"[\s,，]+", keep_keywords_str)
            if k.strip()
        ]
        if not keywords:
            return False

        # 提取文本
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    texts.append(str(item["text"]))
                elif isinstance(item, str):
                    texts.append(item)
            text = " ".join(texts)
        else:
            text = ""

        text_lower = text.lower()
        return any(kw in text_lower for kw in keywords)

    def _get_uni_nickname(self, sender_id: str) -> str | None:
        """从 uni_nickname 配置文件中查询发送者的统一昵称"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_config_path

            config_dir = get_astrbot_config_path()
        except Exception:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                config_dir = os.path.join(get_astrbot_data_path(), "config")
            except Exception:
                config_dir = "data/config"

        config_file = os.path.join(
            config_dir, "astrbot_plugin_uni_nickname_config.json"
        )
        if not os.path.exists(config_file):
            return None

        try:
            with open(config_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            mappings = data.get("nickname_mappings", [])
            sender_id_str = str(sender_id).strip()

            # 标准匹配：全匹配或者后半段/最后冒号分隔的部分匹配
            candidates = [sender_id_str]
            if ":" in sender_id_str:
                candidates.append(sender_id_str.split(":")[-1])
            if "_" in sender_id_str:
                candidates.append(sender_id_str.split("_")[-1])

            for item in mappings:
                if "," in item:
                    acc, nick = item.split(",", maxsplit=1)
                    acc = acc.strip()
                    nick = nick.strip()
                    if acc in candidates:
                        return nick
        except Exception as e:
            logger.warning(f"[APEG] 尝试读取/解析 uni_nickname 配置文件时遇到错误: {e}")
        return None

    def _get_group_n(self, group_id: str, session_id: str = None) -> int:
        """获取特定群聊的回复几率倒数 n"""
        n_config = self.config.get("n", 10)
        group_id_str = str(group_id).strip()

        # 1. 如果 n_config 本身是数字，直接返回
        if isinstance(n_config, (int, float)):
            return max(1, int(n_config))

        # 2. 如果 n_config 是字符串，并且是纯数字，直接返回数字
        if isinstance(n_config, str):
            n_config_trimmed = n_config.strip()
            if n_config_trimmed.isdigit():
                return max(1, int(n_config_trimmed))
            # 否则尝试 JSON 解析
            if n_config_trimmed.startswith("[") or n_config_trimmed.startswith("{"):
                try:
                    n_config = json.loads(n_config_trimmed)
                except Exception as e:
                    logger.warning(f"[APEG] 尝试将 n 字符串解析为 JSON 失败: {e}")
            else:
                return 10

        # 3. 如果是列表且长度非空
        if isinstance(n_config, list):
            # 获取所有可能的候选群号/会话标识进行匹配
            candidates = [group_id_str]
            if session_id:
                s_str = str(session_id).strip()
                candidates.append(s_str)
                if ":" in s_str:
                    candidates.append(s_str.split(":")[-1])
                if "_" in s_str:
                    candidates.append(s_str.split("_")[-1])
            if ":" in group_id_str:
                candidates.append(group_id_str.split(":")[-1])
            if "_" in group_id_str:
                candidates.append(group_id_str.split("_")[-1])

            for item in n_config:
                if isinstance(item, list) and len(item) >= 2:
                    cfg_group = str(item[0]).strip()
                    try:
                        cfg_val = int(item[1])
                    except (ValueError, TypeError):
                        continue

                    # 判断当前群号是否与配置里的群号匹配
                    cfg_group_candidates = [cfg_group]
                    if ":" in cfg_group:
                        cfg_group_candidates.append(cfg_group.split(":")[-1])
                    if "_" in cfg_group:
                        cfg_group_candidates.append(cfg_group.split("_")[-1])

                    # 如果有任何交集
                    if any(c in candidates for c in cfg_group_candidates):
                        return max(1, cfg_val)
                elif isinstance(item, str):
                    item = item.strip()
                    # 循环可能的分割符，从右至左 (rsplit) 分割，兼容包含冒号的复杂会话ID
                    cfg_group = None
                    cfg_val = None
                    for sep in [",", "，", ":", "："]:
                        if sep in item:
                            parts = item.rsplit(sep, 1)
                            try:
                                cfg_val_test = int(parts[1].strip())
                                cfg_group = parts[0].strip()
                                cfg_val = cfg_val_test
                                break  # 成功解析出一个尾部整数，直接终止分隔符循环
                            except (ValueError, TypeError):
                                continue

                    if cfg_group is None or cfg_val is None:
                        continue

                    cfg_group_candidates = [cfg_group]
                    if ":" in cfg_group:
                        cfg_group_candidates.append(cfg_group.split(":")[-1])
                    if "_" in cfg_group:
                        cfg_group_candidates.append(cfg_group.split("_")[-1])

                    if any(c in candidates for c in cfg_group_candidates):
                        return max(1, cfg_val)
        return 10

    async def _flush_pending_messages(self, event: AstrMessageEvent, session_id: str):
        """将生成期间缓存的群友发言按时间顺序插入到 AI 回复之前"""
        state = self._get_session_state(session_id)
        pending = state.get("pending_messages", [])
        if not pending:
            return

        logger.info(
            f"[APEG] 群聊 {session_id} 异步恢复：准备写入 LLM 生成期间暂存的的 {len(pending)} 条群友发言..."
        )
        try:
            session_curr_cid = (
                await self.context.conversation_manager.get_curr_conversation_id(
                    event.unified_msg_origin
                )
            )
            if not session_curr_cid:
                session_curr_cid = (
                    await self.context.conversation_manager.new_conversation(
                        event.unified_msg_origin
                    )
                )
            conv = await self.context.conversation_manager.get_conversation(
                event.unified_msg_origin,
                session_curr_cid,
            )
            if conv:
                history = []
                if conv.history:
                    try:
                        history = json.loads(conv.history)
                    except Exception:
                        history = []
                last_asst_idx = -1
                for i in range(len(history) - 1, -1, -1):
                    if history[i].get("role") == "assistant":
                        last_asst_idx = i
                        break
                if last_asst_idx >= 0:
                    history = (
                        history[:last_asst_idx] + pending + history[last_asst_idx:]
                    )
                else:
                    history.extend(pending)
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=event.unified_msg_origin,
                    conversation_id=conv.cid,
                    history=history,
                )
                logger.info(
                    f"[APEG] ✅ 成功将 {len(pending)} 条暂存群友发言合并归档至数据库！"
                )
        except Exception as e:
            logger.error(
                f"[APEG] 合并暂存发言到历史数据库时出错: {e}",
                exc_info=True,
            )
        finally:
            state["pending_messages"] = []

    async def _undo_buffer_flush(self, event: AstrMessageEvent, session_id: str):
        """撤销上一次滚动缓存写入历史数据库的操作"""
        state = self._get_session_state(session_id)
        snapshot = state.get("last_flushed_buffer_snapshot")
        if not snapshot:
            return
        try:
            session_curr_cid = (
                await self.context.conversation_manager.get_curr_conversation_id(
                    event.unified_msg_origin
                )
            )
            if not session_curr_cid:
                return
            conv = await self.context.conversation_manager.get_conversation(
                event.unified_msg_origin,
                session_curr_cid,
            )
            if not conv or not conv.history:
                return
            history = json.loads(conv.history)
            if not history:
                return
            snapshot_contents = frozenset(
                json.dumps(m.get("content"), sort_keys=True, ensure_ascii=False)
                for m in snapshot
            )
            filtered = [
                m
                for m in history
                if json.dumps(m.get("content"), sort_keys=True, ensure_ascii=False)
                not in snapshot_contents
            ]
            removed = len(history) - len(filtered)
            if removed > 0:
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=event.unified_msg_origin,
                    conversation_id=conv.cid,
                    history=filtered,
                )
                logger.info(f"[APEG] 🔙 已撤销 {removed} 条滚动缓存消息的历史写回。")
        except Exception as e:
            logger.error(f"[APEG] 撤销缓存写回时出错: {e}", exc_info=True)
        finally:
            state["undo_buffer_requested"] = False
            state["last_flushed_buffer_snapshot"] = None

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any):
        """监听 LLM 发起请求。无论是本插件唤醒还是原生前缀、@ 机器人唤醒，都对该群进行锁定标记，防止消息被覆盖丢失"""
        if not event.get_group_id():
            return

        session_id = event.session_id
        if session_id:
            state = self._get_session_state(session_id)
            now = time.time()

            # Time_before 收集中：@消息也收入 batch
            if state.get("time_before_collecting"):
                sender_id = (
                    event.get_sender_id()
                    if hasattr(event, "get_sender_id")
                    else "Unknown"
                )
                sender_name = self._get_uni_nickname(sender_id) or (
                    event.get_sender_name()
                    if hasattr(event, "get_sender_name")
                    else None
                ) or sender_id
                now_dt = datetime.now()
                now_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                record = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": event.message_str},
                        {
                            "type": "text",
                            "text": f"<system_reminder>User ID: {sender_id}, Nickname: {sender_name}\nCurrent datetime: {now_time_str} (UTC)</system_reminder>",
                        },
                    ],
                }
                state["time_before_messages"].append({
                    "record": record,
                    "text": event.message_str,
                })
                logger.info(
                    f"[APEG] ⏳ 群聊 {session_id} 收集中，@消息已入 batch（共 {len(state['time_before_messages'])} 条）"
                )
                event.stop_event()
                return

            # Time_after 静默期：记录消息到历史，然后停止 LLM 请求
            if state.get("time_after_end", 0) > now:
                sender_id = (
                    event.get_sender_id()
                    if hasattr(event, "get_sender_id")
                    else "Unknown"
                )
                sender_name = self._get_uni_nickname(sender_id) or (
                    event.get_sender_name()
                    if hasattr(event, "get_sender_name")
                    else None
                ) or sender_id
                now_dt = datetime.now()
                now_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                record = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": event.message_str},
                        {
                            "type": "text",
                            "text": f"<system_reminder>User ID: {sender_id}, Nickname: {sender_name}\nCurrent datetime: {now_time_str} (UTC)</system_reminder>",
                        },
                    ],
                }
                if req.conversation and req.conversation.history:
                    try:
                        history = json.loads(req.conversation.history)
                    except Exception:
                        history = []
                    history.append(record)
                    req.conversation.history = json.dumps(history, ensure_ascii=False)
                    await self.context.conversation_manager.update_conversation(
                        unified_msg_origin=event.unified_msg_origin,
                        conversation_id=req.conversation.cid,
                        history=history,
                    )
                logger.info(
                    f"[APEG] 🔇 群聊 {session_id} 回复后静默期，LLM 请求已被拦截，消息已记录。"
                )
                event.stop_event()
                return

            # 静默期拦截：n条内不响应任何LLM请求（包括@唤醒、管道原生唤起等）
            if state.get("silent_countdown", 0) > 0:
                state["silent_countdown"] -= 1
                sender_id = (
                    event.get_sender_id()
                    if hasattr(event, "get_sender_id")
                    else "Unknown"
                )
                sender_name = self._get_uni_nickname(sender_id) or (
                    event.get_sender_name()
                    if hasattr(event, "get_sender_name")
                    else None
                )
                if not sender_name:
                    sender_name = sender_id
                now_dt = datetime.now()
                now_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                record = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": event.message_str},
                        {
                            "type": "text",
                            "text": f"<system_reminder>User ID: {sender_id}, Nickname: {sender_name}\nCurrent datetime: {now_time_str} (UTC)</system_reminder>",
                        },
                    ],
                }
                if req.conversation and req.conversation.history:
                    try:
                        history = json.loads(req.conversation.history)
                    except Exception:
                        history = []
                    history.append(record)
                    req.conversation.history = json.dumps(history, ensure_ascii=False)
                    await self.context.conversation_manager.update_conversation(
                        unified_msg_origin=event.unified_msg_origin,
                        conversation_id=req.conversation.cid,
                        history=history,
                    )
                logger.info(
                    f"[APEG] 🔇 静默期拦截LLM请求（剩余 {state['silent_countdown']} 条），已记录但不回复。"
                )
                event.stop_event()
                return

            # 只有在非生成状态（即真正首次进入锁定）时，才清空暂存。
            # 如果已经在 generating 状态（可能我们在 on_message 里提前锁定了），则保留当前的 pending_messages
            if not state.get("is_llm_generating"):
                state["pending_messages"] = []
                state["is_llm_generating"] = True
            state["llm_start_time"] = time.time()

            # 注入退出窥屏工具（群聊 LLM 请求均可见）
            from astrbot.core.agent.tool import FunctionTool, ToolSet

            n_val = self._get_group_n(event.get_group_id(), session_id=session_id)
            session_id_local = session_id

            async def _exit_peeping_handler(ev: AstrMessageEvent, **kwargs):
                s = self._get_session_state(session_id_local)
                reason = kwargs.get("reason", "")
                s["silent_countdown"] = n_val
                s["status"] = "probabilistic"
                s["just_exited_peeping"] = False
                s["post_peep_count"] = 5
                s["undo_buffer_requested"] = True
                s["rolling_buffer"] = []
                s["has_discarded_messages"] = False
                log_suffix = f" 原因: {reason}" if reason else ""
                logger.info(
                    f"[APEG] 🔇 AI 选择退出追回复模式，接下来 {n_val} 条消息保持静默。{log_suffix}"
                )
                if reason:
                    return f"已退出追回复模式（{reason}）。接下来 {n_val} 条消息我不会发言，但会默默看大家聊天。"
                return f"已退出追回复模式。接下来 {n_val} 条消息我不会发言，但会默默看大家聊天。"

            tool = FunctionTool(
                name="exit_peeping_mode",
                description="退出当前的追回复模式，在接下来若干条消息中保持沉默不再自动回复。适用于对当前群聊内容不感兴趣、不想参与讨论或群友不礼貌等场景。",
                parameters={
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "退出原因（可选）",
                        },
                    },
                },
                handler=_exit_peeping_handler,
            )
            if req.func_tool is None:
                req.func_tool = ToolSet()
            req.func_tool.add_tool(tool)

            # 注入群聊发言人识别提示词
            speaker_note = (
                "\n\n【群聊提醒】"
                "你在一个有多人聊天的群聊里。当对话中连续出现多条 user 消息时，"
                "每条消息的格式如下："
                '{"role": "user", "content": [{"type": "text", "text": "消息内容"}, {"type": "text", "text": "<system_reminder>User ID: 群友ID, Nickname: 昵称\nGroup name: 群名(可能不显示)\nCurrent datetime: 时间 (CST)</system_reminder>"}]}'
                "请逐条留意，不要默认连续消息来自同一个人。"
                "准确知道每句话是谁说的，你才能自然地参与进来。"
            )
            if speaker_note not in req.system_prompt:
                req.system_prompt += speaker_note

            logger.info(
                f"[APEG] 🔒 检测到群聊 {session_id} 发起了 LLM 回答流，已锁定历史归档。当前时间群聊消息均进入高保真静默缓存块。"
            )

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """
        全天候监听所有消息：
        1. 过滤：非群聊、指令、@机器人的强唤醒消息。
        2. 归档：在当前会话的 conversation 历史记录中安全存下该群员的对话（[昵称]: 内容）。
        3. 回复调节：在『连续窥屏状态』100% 回复，或在『闲聊状态』以 1/n 的几率概率触发回复。
        """
        # 1. 过滤非群聊消息：只处理真实的群聊对话
        group_id = event.get_group_id()
        if not group_id:
            return

        session_id = event.session_id
        if not session_id:
            return

        # 1.5 过滤原生唤醒或命令消息：
        # 若本条群消息已经匹配了内置聊天前缀、唤醒词或 @ 机器人，
        # 则说明是由 AstrBot 默认会话流进行响应的，我们直接跳过，防止同一条消息触发两次 LLM 回复。
        if event.is_at_or_wake_command:
            return

        # 2. 检查群白名单过滤
        white_groups_str = self.config.get("white_groups", "").strip()
        if white_groups_str:
            white_list = [
                g.strip() for g in re.split(r"[\s,，]+", white_groups_str) if g.strip()
            ]
            if white_list and str(group_id) not in white_list:
                return

        message_str = event.message_str.strip() if event.message_str else ""
        if not message_str:
            from astrbot.core.message.components import Image

            if not any(
                isinstance(comp, Image) for comp in event.message_obj.message
            ):
                return

        # 3. 过滤典型命令：不插手正常的系统级指令消息
        if message_str.startswith(("/", "\\", "!", "！")):
            return

        # 4. 过滤包含 at 机器人的强唤醒消息：不干扰原生的 @ 消息，原生自有大模型进行处理
        has_at_bot = False
        if hasattr(event.message_obj, "has_at_bot") and event.message_obj.has_at_bot:
            has_at_bot = True
        else:
            # 安全遍历组件来判定是不是 at_bot
            from astrbot.core.message.components import At

            for comp in event.message_obj.message:
                if isinstance(comp, At) and str(comp.qq) == str(event.get_self_id()):
                    has_at_bot = True
                    break

        if has_at_bot:
            return

        # 获取发言人的群名片或昵称与用户 ID
        sender_id = (
            event.get_sender_id()
            if hasattr(event, "get_sender_id")
            else getattr(event, "user_id", "Unknown")
        )

        # 优先使用 uni_nickname 的昵称映射，若没有才使用原有获取到的昵称逻辑
        sender_name = self._get_uni_nickname(sender_id)
        if not sender_name:
            sender_name = (
                event.get_sender_name() if hasattr(event, "get_sender_name") else None
            )

            if not sender_name:
                if hasattr(event.message_obj, "sender") and event.message_obj.sender:
                    sender_name = (
                        event.message_obj.sender.nickname
                        or event.message_obj.sender.user_id
                    )

            if not sender_name:
                sender_name = sender_id

        formatted_msg = event.message_str

        state = self._get_session_state(session_id)
        now = time.time()

        # === Time_before 收集中：将本条消息追加到 batch 中 ===
        if state.get("time_before_collecting"):
            now_dt = datetime.now()
            now_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            batched_record = {
                "role": "user",
                "content": [
                    {"type": "text", "text": event.message_str},
                    {
                        "type": "text",
                        "text": f"<system_reminder>User ID: {sender_id}, Nickname: {sender_name}\nCurrent datetime: {now_time_str} (UTC)</system_reminder>",
                    },
                ],
            }
            state["time_before_messages"].append({
                "record": batched_record,
                "text": event.message_str,
            })
            logger.info(
                f"[APEG] ⏳ 群聊 {session_id} 正在收集中({len(state['time_before_messages'])}条)，消息已入 batch：{sender_name}：{event.message_str}"
            )
            if event.is_at_or_wake_command:
                event.stop_event()
            return

        # 1.6 消息去重（防止相同消息在短时间内因为网络重试/超时投递导致多次记录与重复回复）
        msg_id = getattr(event, "message_id", None) or (
            event.message_obj.message_id
            if hasattr(event.message_obj, "message_id")
            else None
        )
        msg_sig = f"{sender_id}:{message_str}"

        last_msg_id = state.get("last_msg_id")
        last_msg_sig = state.get("last_msg_sig")
        last_msg_time = state.get("last_msg_time", 0.0)

        is_duplicate = False
        if msg_id and last_msg_id and last_msg_id == msg_id:
            is_duplicate = True
        elif last_msg_sig == msg_sig and (now - last_msg_time < 12.0):
            is_duplicate = True

        if is_duplicate:
            logger.info(
                f"[APEG] 🚫 监测到过滤群聊重复重试请求 (Sender: {sender_name}, Content: {message_str})，正在静默拦截该事件归档与大模型请求，避免幽灵多回复。"
            )
            return

        # 更新去重缓存状态
        state["last_msg_id"] = msg_id
        state["last_msg_sig"] = msg_sig
        state["last_msg_time"] = now

        # 5.1 超时熔断与安全边界恢复校验
        # 假如因为异常或重启等原因导致 is_llm_generating 依然为 True，超过 60s 强制重置
        if (
            state.get("is_llm_generating")
            and now - state.get("llm_start_time", 0.0) > 60.0
        ):
            logger.warning(
                f"[APEG] ⚠️ 群聊 {session_id} LLM 响应处理流超时 (>60s)，启动安全重置，清空并暂存当前历史。"
            )
            state["is_llm_generating"] = False
            await self._flush_pending_messages(event, session_id)

        # 5.2 大模型生成期间的消息暂存流程 (重点解决 AI 组织语言期间其他群友消息丢失的问题)
        if state.get("is_llm_generating"):
            now_dt = datetime.now()
            now_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            record = {
                "role": "user",
                "content": [
                    {"type": "text", "text": event.message_str},
                    {
                        "type": "text",
                        "text": f"<system_reminder>User ID: {sender_id}, Nickname: {sender_name}\nCurrent datetime: {now_time_str} (UTC)</system_reminder>",
                    },
                ],
            }
            state["pending_messages"].append(record)
            logger.info(
                f"[APEG] 📥 [AI组织语言中] 群友 {sender_name} 发言，已安全加入高保真队列缓冲 (当前缓冲: {len(state['pending_messages'])}条)"
            )
            return

        # 获取或自动新建当前的 conversation
        session_curr_cid = (
            await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin,
            )
        )
        if not session_curr_cid:
            session_curr_cid = await self.context.conversation_manager.new_conversation(
                event.unified_msg_origin
            )
        conv = await self.context.conversation_manager.get_conversation(
            event.unified_msg_origin,
            session_curr_cid,
        )
        if not conv:
            logger.warning(f"[APEG] 无法读取当前群聊会话: {event.unified_msg_origin}")
            return

        M = self.config.get("M", 3.0)
        N = self.config.get("N", 5.0)
        n = self._get_group_n(group_id, session_id=session_id)

        # 检测当前的“连续窥屏”超时状态转移
        if state["status"] == "peeping":
            # 连续窥屏已经达到了最大可窥屏时长限制 N 分钟
            if now - state["peep_start_time"] >= N * 60:
                logger.info(
                    f"[APEG] 群聊 {session_id} 窥屏已达到 {N} 分钟上限，退出重击回复模式。"
                )
                state["status"] = "probabilistic"
                state["just_exited_peeping"] = True
                state["post_peep_count"] = 0
                state["rolling_buffer"] = []
                state["has_discarded_messages"] = False
            # AI 回复后已经超过了 M 分钟没有听到风吹草动
            elif now - state["last_ai_reply_time"] >= M * 60:
                logger.info(
                    f"[APEG] 群聊 {session_id} 的上次 AI 回复已寂寂无声超过 {M} 分钟，退出重击回复模式。"
                )
                state["status"] = "probabilistic"
                state["just_exited_peeping"] = True
                state["post_peep_count"] = 0
                state["rolling_buffer"] = []
                state["has_discarded_messages"] = False

        # 确定最后的回复机制
        is_reply = False
        if state["status"] == "peeping":
            is_reply = True
            logger.info(
                f"[APEG] 群聊 {session_id} 连击窥屏中，直接对以下消息进行跟贴回复：{formatted_msg}"
            )
        else:
            if n <= 1:
                is_reply = True
            else:
                is_reply = random.randint(1, n) == 1

            if is_reply:
                logger.info(
                    f"[APEG] 群聊 {session_id} 突破 1/{n} 机率触发，将积极加入闲聊：{formatted_msg}"
                )

        # 静默期检查：AI 主动退出后，n 条消息内不回复
        if state.get("silent_countdown", 0) > 0:
            is_reply = False
            state["silent_countdown"] -= 1
            logger.info(
                f"[APEG] 🔇 群聊 {session_id} 静默期剩余 {state['silent_countdown']} 条消息，跳过回复。"
            )

        # Time_after 静默期：AI 回复后的一段时间内不触发新回复
        time_after = self.config.get("time_after", 5.0)
        if is_reply and state.get("time_after_end", 0) > now:
            is_reply = False
            logger.info(
                f"[APEG] 🔇 群聊 {session_id} 处于回复后静默期(time_after={time_after}s)，跳过回复。"
            )

        # 6. 处理消息的归档与发送
        if is_reply:
            time_before = self.config.get("time_before", 3.0) or 0.0

            # 立即设置生成状态，锁定状态机，防止后续并发到达的消息再次出发大模型调用
            state["is_llm_generating"] = True
            state["llm_start_time"] = time.time() + time_before + 60
            state["pending_messages"] = []
            logger.info(
                f"[APEG] 🔒 [主动锁定] 群聊 {session_id} 决定回复消息，已提前设置 LLM 回答流状态，锁定后续消息归档。"
            )

            # 保存缓存快照，用于可能的回滚
            buffer = state.get("rolling_buffer", [])
            if not state.get("undo_buffer_requested"):
                state["last_flushed_buffer_snapshot"] = list(buffer) if buffer else None

            if time_before > 0:
                # === Time_before 批次收集模式 ===
                state["time_before_collecting"] = True
                state["time_before_messages"] = []
                now_dt = datetime.now()
                now_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                trigger_record = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": event.message_str},
                        {
                            "type": "text",
                            "text": f"<system_reminder>User ID: {sender_id}, Nickname: {sender_name}\nCurrent datetime: {now_time_str} (UTC)</system_reminder>",
                        },
                    ],
                }
                state["time_before_messages"].append({
                    "record": trigger_record,
                    "text": event.message_str,
                })
                logger.info(
                    f"[APEG] ⏳ 群聊 {session_id} Time_before 等待 {time_before}s，开始收集后续消息..."
                )
                await asyncio.sleep(time_before)

                # 收集所有在等待期间到达的消息
                batched_messages = list(state.get("time_before_messages", []))
                state["time_before_collecting"] = False
                state["time_before_messages"] = []

                logger.info(
                    f"[APEG] ⏳ 群聊 {session_id} Time_before 结束，共收集 {len(batched_messages)} 条消息，准备发送给 LLM。"
                )

                # 构建历史：flush 滚动缓存 + time_before 批次消息（最后一条除外）
                history = []
                if conv.history:
                    try:
                        history = json.loads(conv.history)
                    except Exception:
                        history = []

                if state.get("has_discarded_messages", False):
                    discard_reminder = {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "<system_reminder>由于消息太长，略去了一段不相关的聊天记录。</system_reminder>",
                            }
                        ],
                    }
                    history.append(discard_reminder)

                if buffer:
                    history.extend(buffer)

                # 除最后一条外的所有 batched 消息加入历史
                for m in batched_messages[:-1]:
                    history.append(m["record"])

                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=event.unified_msg_origin,
                    conversation_id=conv.cid,
                    history=history,
                )
                conv.history = json.dumps(history, ensure_ascii=False)
                state["rolling_buffer"] = []
                state["has_discarded_messages"] = False
                state["just_exited_peeping"] = False
                state["post_peep_count"] = 0

                # 用最后一条消息作为 prompt
                last_prompt = batched_messages[-1]["text"]
                state["llm_start_time"] = time.time()
                yield event.request_llm(prompt=last_prompt, conversation=conv)
                return

            # === Time_before=0，立即回复（原有路径） ===
            if buffer:
                history = []
                if conv.history:
                    try:
                        history = json.loads(conv.history)
                    except Exception:
                        history = []

                if state.get("has_discarded_messages", False):
                    discard_reminder = {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "<system_reminder>由于消息太长，略去了一段不相关的聊天记录。</system_reminder>",
                            }
                        ],
                    }
                    history.append(discard_reminder)

                history.extend(buffer)
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=event.unified_msg_origin,
                    conversation_id=conv.cid,
                    history=history,
                )
                conv.history = json.dumps(history, ensure_ascii=False)
                logger.info(
                    f"[APEG] 📤 群聊 {session_id} 唤醒前已将 {len(buffer)} 条缓存消息写入历史"
                )
                state["rolling_buffer"] = []
                state["has_discarded_messages"] = False

            state["just_exited_peeping"] = False
            state["post_peep_count"] = 0
            state["llm_start_time"] = time.time()

            yield event.request_llm(prompt=event.message_str, conversation=conv)
        else:
            # 如果不应回复该条消息，根据当前状态决定处理方式
            now_dt = datetime.now()
            now_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

            # 构造消息记录
            new_message = {
                "role": "user",
                "content": [
                    {"type": "text", "text": event.message_str},
                    {
                        "type": "text",
                        "text": f"<system_reminder>User ID: {sender_id}, Nickname: {sender_name}\nCurrent datetime: {now_time_str} (UTC)</system_reminder>",
                    },
                ],
            }

            # 窥屏中：保持原有逻辑，直接记录
            if state["status"] == "peeping":
                history = []
                if conv.history:
                    try:
                        history = json.loads(conv.history)
                    except Exception:
                        history = []
                history.append(new_message)
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=event.unified_msg_origin,
                    conversation_id=conv.cid,
                    history=history,
                )
            # 非窥屏状态：应用滚动缓存机制
            else:
                L_val = self.config.get("L", 15)
                try:
                    L_val = int(L_val)
                except (ValueError, TypeError):
                    L_val = 15
                L_val = max(1, L_val)

                # 情况1：刚退出窥屏，前5条无论如何记入
                if state["just_exited_peeping"] and state["post_peep_count"] < 5:
                    history = []
                    if conv.history:
                        try:
                            history = json.loads(conv.history)
                        except Exception:
                            history = []
                    history.append(new_message)
                    await self.context.conversation_manager.update_conversation(
                        unified_msg_origin=event.unified_msg_origin,
                        conversation_id=conv.cid,
                        history=history,
                    )
                    state["post_peep_count"] += 1
                    logger.info(
                        f"[APEG] 📝 群聊 {session_id} 退出窥屏后记录第 {state['post_peep_count']}/5 条消息"
                    )
                    if state["post_peep_count"] >= 5:
                        state["just_exited_peeping"] = False
                        logger.info(
                            f"[APEG] ✅ 群聊 {session_id} 退出窥屏后的5条消息已全部记录，切换到滚动缓存模式"
                        )
                # 情况2：进入滚动缓存模式
                else:
                    buffer = state["rolling_buffer"]
                    buffer.append(new_message)

                    # 如果缓存超过L条，移除最老的一条
                    if len(buffer) > L_val:
                        oldest = buffer.pop(0)
                        # 检查最老的消息是否包含关键词
                        if self._message_contains_keywords(oldest["content"]):
                            # 包含关键词，记录到历史
                            history = []
                            if conv.history:
                                try:
                                    history = json.loads(conv.history)
                                except Exception:
                                    history = []
                            history.append(oldest)
                            await self.context.conversation_manager.update_conversation(
                                unified_msg_origin=event.unified_msg_origin,
                                conversation_id=conv.cid,
                                history=history,
                            )
                            logger.info(
                                f"[APEG] 🔑 群聊 {session_id} 滚动缓存中最老的消息包含关键词，已记录到历史"
                            )
                        else:
                            state["has_discarded_messages"] = True
                            logger.info(
                                f"[APEG] 🗑️ 群聊 {session_id} 滚动缓存中最老的消息不包含关键词，已丢弃"
                            )

                    logger.info(
                        f"[APEG] 📥 群聊 {session_id} 消息已加入滚动缓存 (当前缓存: {len(buffer)}/{L_val})"
                    )

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """
        拦截所有的 AI 回复结果事件：
        只要本插件监听的群聊发现了 AI 的开口回复 (不管是 1/n 闲聊回复、窥屏重击或者是别的人格唤醒)，
        就立即启动或重置『连续窥屏』深度互动模式。
        """
        # 只在群聊中激活状态转换
        if not event.get_group_id():
            return

        session_id = event.session_id
        if not session_id:
            return

        state = self._get_session_state(session_id)

        # 模拟打字延迟：根据回复字数 / 3 * 系数 等待后发送
        coefficient = self.config.get("typing_speed_coefficient", 1.0)
        if coefficient > 0:
            try:
                coefficient = float(coefficient)
            except (ValueError, TypeError):
                coefficient = 1.0
            text = resp.completion_text
            delay = (len(text) / 3.0) * coefficient
            if delay > 0:
                logger.info(
                    f"[APEG] ⌨️ AI 回复共 {len(text)} 字，将在 {delay:.1f} 秒后发送（打字速度系数: {coefficient}）"
                )
                # 刷新 llm_start_time，防止 on_message 中的 60s 超时熔断误触发
                state["llm_start_time"] = time.time()
                await asyncio.sleep(delay)

        now = time.time()

        # 设置 Time_after 静默期：回复已发送到群，接下来 time_after 秒内不触发新回复
        time_after = self.config.get("time_after", 5.0)
        if time_after > 0:
            state["time_after_end"] = now + time_after
            logger.info(
                f"[APEG] 🔇 群聊 {session_id} 回复已发送，接下来 {time_after}s 内不触发新回复。"
            )

        # 1. 状态改变和连击维护
        if state.get("undo_buffer_requested"):
            logger.info(
                f"[APEG] 🔇 群聊 {session_id} AI 已请求退出模式，跳过进入连续窥屏追踪阶段。"
            )
        elif state["status"] == "probabilistic":
            state["status"] = "peeping"
            state["peep_start_time"] = now
            logger.info(
                f"[APEG] ✨ 群聊 {session_id} 进入『连续窥屏追踪阶段』。在此期间新发普通消息将 100% 连击回复。"
            )
        else:
            logger.info(f"[APEG] ✨ 重置群聊 {session_id} 连击窥屏时限。")

        state["last_ai_reply_time"] = now
        state["is_llm_generating"] = False

        # 2. 异步将生成的暂存消息追加合并入数据库
        # 这里使用 asyncio.create_task 并睡眠一段时间，等主程序的 _save_to_history 底层写入完全结束后再合并，完美防止覆盖。
        async def delayed_flush():
            await asyncio.sleep(0.5)
            await self._flush_pending_messages(event, session_id)
            # 撤销缓存消息写入
            if state.get("undo_buffer_requested") and state.get(
                "last_flushed_buffer_snapshot"
            ):
                await self._undo_buffer_flush(event, session_id)

        asyncio.create_task(delayed_flush())

    async def terminate(self):
        """插件卸载资源清理"""
        self.session_states.clear()
