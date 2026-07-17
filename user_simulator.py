import json
import logging
from datetime import datetime
from pathlib import Path
import httpx
from openai import OpenAI


# 配置 api_use.log，每次调用追加一行 JSON
api_logger = logging.getLogger("api_use")
api_logger.setLevel(logging.INFO)
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)
_handler = logging.FileHandler(log_dir / "api_use.log", encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(message)s"))
api_logger.addHandler(_handler)

PROMPT_FILE = Path(__file__).parent / "system_prompt.md"


class User_simulator:
    def __init__(
        self,
        origin_query: str,
        user_profile: str = "",
        user_directory: str = "",
        prompt_file: str | Path = PROMPT_FILE,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        proxy: str | None = None
    ):
        """
        Args:
            origin_query: 用户的原始任务描述
            user_profile: 用户画像，描述用户身份、偏好等特征
            user_directory: 用户文件目录，描述用户可访问的文件结构
            prompt_file: system prompt 模板文件路径，默认读取同目录下的 system_prompt.md
            model: 使用的 OpenAI 模型名称
            api_key: OpenAI API key，为 None 时从环境变量 OPENAI_API_KEY 读取
            base_url: 自定义 API base URL，为 None 时使用默认值
        """
        self._template = Path(prompt_file).read_text(encoding="utf-8")

        # 用户画像由装配层(openclaw_automation.create_simulator)按 user_dir.profile_file 读好后传入;
        # simulator 只认传入值,不再自读固定文件名 user_profile.json(消除双读/覆盖与文件名写死)。
        self._user_profile = user_profile
        self._user_directory = user_directory
        self._current_origin_query = origin_query
        # 当前时间:__init__ 取一次并缓存,整场会话复用(见 design D5)。
        # 供 simulator 判定/追问涉及时间相对语义(最近/今年/本周等)时以此为"现在"。
        self._current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.model = model
        self.messages: list[dict] = []  # 记录对话历史，用于拼入 system prompt
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=httpx.Client(verify=False, proxy=proxy),
        )

    def _render(
        self,
        origin_query: str,
        conversation_history: str = "",
        evaluator_feedback: str = "",
    ) -> str:
        return (
            self._template
            .replace("{origin_query}", origin_query)
            .replace("{user_profile}", self._user_profile)
            .replace("{user_directory}", self._user_directory)
            .replace("{current_time}", self._current_time)
            .replace("{conversation_history}", conversation_history)
            .replace("{evaluator_feedback}", evaluator_feedback or "（本轮无第三方评估）")
        )

    def update_origin_query(self, origin_query: str) -> None:
        """更新 Origin_query，保留现有对话历史。"""
        self._current_origin_query = origin_query

    def _build_history_str(self) -> str:
        if not self.messages:
            return "（暂无对话历史）"
        lines = []
        for msg in self.messages:
            role = "用户" if msg["role"] == "assistant" else "Agent"
            lines.append(f"[{role}]: {msg['content']}")
        return "\n".join(lines)

    def chat(self, query: str, evaluator_feedback: str | None = None) -> str:
        """
        接收 agent 发来的消息（query），返回模拟用户的回复。

        Args:
            query: 当前轮 agent 发出的内容
            evaluator_feedback: 第三方 Evaluator 对本轮的证据化评估反馈，
                注入 system prompt 供 simulator 参考决策；None 表示本轮无评估。

        Returns:
            模拟用户的回复文本
        """
        # 将对话历史嵌入 system prompt，每次调用都是单轮（system + 单条 user）
        history_str = self._build_history_str()
        current_system = self._render(
            self._current_origin_query, history_str, evaluator_feedback or ""
        )

        full_messages = [
            {"role": "system", "content": current_system},
            {"role": "user", "content": query},
        ]

        # 返回值健壮性: reasoning 类模型经 OpenAI 兼容接口时,token 可能全进
        # reasoning 通道、主 content 为空。取值兜底顺序:主 content → reasoning 字段;
        # 仍空则重试;重试(3 次)仍空则判为 simulator 自身失败,返回【Task_Failed】,
        # chat() 永不返回空串。(连续吐不出内容=simulator 坏了,判失败比判完成诚实,
        # 避免虚高成功率、掩盖故障。)
        last_exc = None
        response = None
        reply = ""
        for attempt in range(3):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=full_messages,
                )
            except Exception as e:
                last_exc = e
                logging.warning(f"API call failed (attempt {attempt + 1}/3): {e}")
                continue

            reply = self._extract_reply(response)
            if reply:
                break
            logging.warning(
                f"simulator 返回空内容 (attempt {attempt + 1}/3): "
                "主 content 与 reasoning 通道均为空,重试"
            )
        else:
            if last_exc is not None and response is None:
                raise last_exc
            logging.warning("simulator 重试 3 次仍为空,判为 simulator 失败,返回 Task_Failed")
            reply = "【Task_Failed】"

        # 记录本轮对话到历史（user=agent说的，assistant=simulator回复）
        self.messages.append({"role": "user", "content": query})
        self.messages.append({"role": "assistant", "content": reply})

        usage = getattr(response, "usage", None) if response is not None else None
        self._log_api_call(full_messages, reply, usage)

        return reply

    @staticmethod
    def _extract_reply(response) -> str:
        """从模型响应取回复文本,主 content 为空则回退 reasoning 文本通道。

        reasoning 类模型经 OpenAI 兼容接口时,最终文本可能落在
        message.reasoning_content / message.reasoning 等非标准字段。宽松读取,
        字段缺失一律视为空,不抛错。返回去除首尾空白后的文本(可能为空串)。
        """
        try:
            message = response.choices[0].message
        except (AttributeError, IndexError, TypeError):
            return ""

        def _read(field: str) -> str:
            val = getattr(message, field, None)
            if val is None and isinstance(message, dict):
                val = message.get(field)
            return val.strip() if isinstance(val, str) else ""

        return _read("content") or _read("reasoning_content") or _read("reasoning")

    def reset(self):
        """清空对话历史，开始新一轮对话。"""
        self.messages = []

    def _log_api_call(self, input_messages: list[dict], output: str, usage) -> None:
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "model": self.model,
            "input": input_messages,
            "output": output,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
        }
        api_logger.info(json.dumps(record, ensure_ascii=False))
