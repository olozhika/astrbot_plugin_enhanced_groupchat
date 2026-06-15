import time
import random
import re
import asyncio
from datetime import datetime
from typing import Dict, Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import LLMResponse


@register("enhanced_groupchat", "DeepMind", "打破前缀限制的群聊强化深度互动插件", "1.0.0")
class EnhancedGroupChatPlugin(Star):
    def __init__(self, context: Context, config: Dict[str, Any] = None):
        super().__init__(context)
        self.context = context
        self.config = config if config else {}
        
        # 维护每个 session (群聊) 的窥屏与连击状态
        # key: session_id, value: dict
        self.session_states = {}

        logger.info("[EnhancedGroupChat] 群聊强化插件已成功加载！")

    def _get_session_state(self, session_id: str) -> Dict[str, Any]:
        """安全地获取或初始化 session (群聊) 的状态"""
        if session_id not in self.session_states:
            self.session_states[session_id] = {
                "status": "probabilistic",   # "probabilistic" (1/n概率模式) 或 "peeping" (连击窥屏模式)
                "peep_start_time": 0.0,       # 刚进入连续窥屏状态的时间戳
                "last_ai_reply_time": 0.0,    # AI上一次回复的时间戳
                "is_llm_generating": False,  # 是否正在生成大模型回复
                "llm_start_time": 0.0,        # 本次大模型生成的起始时间戳
                "pending_messages": [],       # 大模型生成期间缓存的群友发言
            }
        return self.session_states[session_id]

    async def _flush_pending_messages(self, event: AstrMessageEvent, session_id: str):
        """将生成期间缓存的群友发言追加并合并写入当前会话的数据库中"""
        state = self._get_session_state(session_id)
        pending = state.get("pending_messages", [])
        if not pending:
            return
            
        logger.info(f"[EnhancedGroupChat] 群聊 {session_id} 异步恢复：准备写入 LLM 生成期间暂存的的 {len(pending)} 条群友发言...")
        try:
            session_curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if not session_curr_cid:
                session_curr_cid = await self.context.conversation_manager.new_conversation(
                    event.unified_msg_origin
                )
            conv = await self.context.conversation_manager.get_conversation(
                event.unified_msg_origin,
                session_curr_cid,
            )
            if conv:
                history = conv.content or []
                history.extend(pending)
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=event.unified_msg_origin,
                    conversation_id=conv.conversation_id,
                    history=history
                )
                logger.info(f"[EnhancedGroupChat] ✅ 成功将 {len(pending)} 条暂存群友发言合并归档至数据库！")
        except Exception as e:
            logger.error(f"[EnhancedGroupChat] 合并暂存发言到历史数据库时出错: {e}", exc_info=True)
        finally:
            state["pending_messages"] = []

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any):
        """监听 LLM 发起请求。无论是本插件唤醒还是原生前缀、@ 机器人唤醒，都对该群进行锁定标记，防止消息被覆盖丢失"""
        if not event.get_group_id():
            return
            
        session_id = event.session_id
        if session_id:
            state = self._get_session_state(session_id)
            state["is_llm_generating"] = True
            state["llm_start_time"] = time.time()
            state["pending_messages"] = []
            logger.info(f"[EnhancedGroupChat] 🔒 检测到群聊 {session_id} 发起了 LLM 回答流，已锁定历史归档。当前时间群聊消息均进入高保真静默缓存块。")

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
            white_list = [g.strip() for g in re.split(r'[\s,，]+', white_groups_str) if g.strip()]
            if white_list and str(group_id) not in white_list:
                return

        message_str = event.message_str.strip() if event.message_str else ""
        if not message_str:
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
                if isinstance(comp, At) and str(comp.target) == str(self.context.robot_id):
                    has_at_bot = True
                    break

        if has_at_bot:
            return

        # 获取发言人的群名片或昵称
        sender_name = event.sender_name or event.user_id
        if hasattr(event.message_obj, "sender") and event.message_obj.sender:
            sender_name = event.message_obj.sender.nickname or event.message_obj.sender.user_id or sender_name

        formatted_msg = f"[{sender_name}]: {event.message_str}"

        state = self._get_session_state(session_id)
        now = time.time()

        # 5.1 超时熔断与安全边界恢复校验
        # 假如因为异常或重启等原因导致 is_llm_generating 依然为 True，超过 60s 强制重置
        if state.get("is_llm_generating") and now - state.get("llm_start_time", 0.0) > 60.0:
            logger.warning(f"[EnhancedGroupChat] ⚠️ 群聊 {session_id} LLM 响应处理流超时 (>60s)，启动安全重置，清空并暂存当前历史。")
            state["is_llm_generating"] = False
            await self._flush_pending_messages(event, session_id)

        # 5.2 大模型生成期间的消息暂存流程 (重点解决 AI 组织语言期间其他群友消息丢失的问题)
        if state.get("is_llm_generating"):
            now_dt = datetime.now()
            now_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            record = {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"<system_reminder>User ID: {event.user_id}, Nickname: {sender_name}\nCurrent datetime: {now_time_str} (UTC)</system_reminder>"
                    },
                    {
                        "type": "text",
                        "text": formatted_msg
                    }
                ]
            }
            state["pending_messages"].append(record)
            logger.info(f"[EnhancedGroupChat] 📥 [AI组织语言中] 群友 {sender_name} 发言，已安全加入高保真队列缓冲 (当前缓冲: {len(state['pending_messages'])}条)")
            return

        # 获取或自动新建当前的 conversation
        session_curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
            event.unified_msg_origin,
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
            logger.warning(f"[EnhancedGroupChat] 无法读取当前群聊会话: {event.unified_msg_origin}")
            return

        M = self.config.get("M", 3.0)
        N = self.config.get("N", 5.0)
        n = self.config.get("n", 10)

        # 检测当前的“连续窥屏”超时状态转移
        if state["status"] == "peeping":
            # 连续窥屏已经达到了最大可窥屏时长限制 N 分钟
            if now - state["peep_start_time"] >= N * 60:
                logger.info(f"[EnhancedGroupChat] 群聊 {session_id} 窥屏已达到 {N} 分钟上限，退出重击回复模式。")
                state["status"] = "probabilistic"
            # AI 回复后已经超过了 M 分钟没有听到风吹草动
            elif now - state["last_ai_reply_time"] >= M * 60:
                logger.info(f"[EnhancedGroupChat] 群聊 {session_id} 的上次 AI 回复已寂寂无声超过 {M} 分钟，退出重击回复模式。")
                state["status"] = "probabilistic"

        # 确定最后的回复机制
        is_reply = False
        if state["status"] == "peeping":
            is_reply = True
            logger.info(f"[EnhancedGroupChat] 群聊 {session_id} 连击窥屏中，直接对以下消息进行跟贴回复：{formatted_msg}")
        else:
            if n <= 1:
                is_reply = True
            else:
                is_reply = (random.randint(1, n) == 1)
            
            if is_reply:
                logger.info(f"[EnhancedGroupChat] 群聊 {session_id} 突破 1/{n} 机率触发，将积极加入闲聊：{formatted_msg}")

        # 6. 处理消息的归档与发送
        if is_reply:
            # 如果触发了回复，我们将此格式化的消息直接当做此次 LLM 请求的最完美 Prompt
            # 原生在请求结束及返回回复时，会自动帮我们把该 prompt 和 AI 回复同步追加进 conversation 库中。
            yield event.request_llm(
                prompt=formatted_msg,
                conversation=conv
            )
        else:
            # 如果不应回复该条消息，我们必须在底层静默、默默无闻地将其记录至当前会话的对话历史，
            # 完美地让 AI 在随后的任何时机有充足的历史作为闲聊戏剧文本参考。
            # 构造附带优雅 <system_reminder> 的双 block 消息体，既符合 LLM 的系统前缀读取设计，
            # 也能让 local_reminiscence 的聊天记录导出提取器正确地提取发言用户的 Nickname、时间戳和完整的剧本格式内容。
            history = conv.content or []
            now_dt = datetime.now()
            now_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            
            history.append({
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"<system_reminder>User ID: {event.user_id}, Nickname: {sender_name}\nCurrent datetime: {now_time_str} (UTC)</system_reminder>"
                    },
                    {
                        "type": "text",
                        "text": formatted_msg
                    }
                ]
            })
            await self.context.conversation_manager.update_conversation(
                unified_msg_origin=event.unified_msg_origin,
                conversation_id=conv.conversation_id,
                history=history
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

        now = time.time()
        state = self._get_session_state(session_id)

        # 1. 状态改变和连击维护
        if state["status"] == "probabilistic":
            state["status"] = "peeping"
            state["peep_start_time"] = now
            logger.info(f"[EnhancedGroupChat] ✨ 群聊 {session_id} 进入『连续窥屏追踪阶段』。在此期间新发普通消息将 100% 连击回复。")
        else:
            logger.info(f"[EnhancedGroupChat] ✨ 重置群聊 {session_id} 连击窥屏时限。")
            
        state["last_ai_reply_time"] = now
        state["is_llm_generating"] = False

        # 2. 异步将生成的暂存消息追加合并入数据库
        # 这里使用 asyncio.create_task 并睡眠一段时间，等主程序的 _save_to_history 底层写入完全结束后再合并，完美防止覆盖。
        async def delayed_flush():
            await asyncio.sleep(0.5)
            await self._flush_pending_messages(event, session_id)

        asyncio.create_task(delayed_flush())

    def terminate(self):
        """插件卸载资源清理"""
        self.session_states.clear()
