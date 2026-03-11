import json
import logging
from datetime import datetime
from pathlib import Path
import httpx
from openai import OpenAI


# 配置 api_use.log，每次调用追加一行 JSON
api_logger = logging.getLogger("api_use")
api_logger.setLevel(logging.INFO)
_handler = logging.FileHandler("api_use.log", encoding="utf-8")
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

        # 尝试从 user_directory 对应的目录下读取 user_profile.json
        if user_directory:
            profile_path = Path(user_directory) / "user_profile.json"
            if profile_path.exists():
                profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
                self._user_profile = json.dumps(profile_data, ensure_ascii=False, indent=2)
            else:
                self._user_profile = user_profile
        else:
            self._user_profile = user_profile
        self._user_directory = user_directory
        self._current_origin_query = origin_query
        self.model = model
        self.messages: list[dict] = []  # 记录对话历史，用于拼入 system prompt
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=httpx.Client(verify=False, proxy=proxy),
        )

    def _render(self, origin_query: str, conversation_history: str = "") -> str:
        return (
            self._template
            .replace("{origin_query}", origin_query)
            .replace("{user_profile}", self._user_profile)
            .replace("{user_directory}", self._user_directory)
            .replace("{conversation_history}", conversation_history)
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

    def chat(self, query: str) -> str:
        """
        接收 agent 发来的消息（query），返回模拟用户的回复。

        Args:
            query: 当前轮 agent 发出的内容

        Returns:
            模拟用户的回复文本
        """
        # 将对话历史嵌入 system prompt，每次调用都是单轮（system + 单条 user）
        history_str = self._build_history_str()
        current_system = self._render(self._current_origin_query, history_str)

        full_messages = [
            {"role": "system", "content": current_system},
            {"role": "user", "content": query},
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
        )

        reply = response.choices[0].message.content

        # 记录本轮对话到历史（user=agent说的，assistant=simulator回复）
        self.messages.append({"role": "user", "content": query})
        self.messages.append({"role": "assistant", "content": reply})

        self._log_api_call(full_messages, reply, response.usage)

        return reply

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
