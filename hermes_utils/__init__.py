"""hermes_utils — hermes-task 的库依赖,跟 openclaw 的 utils/ 包并列共存。

之所以单独建一个包名而不是塞回 utils/, 是因为 hermes-agent 源码里有顶层
``utils.py``;如果我们这边也叫 utils,hermes-agent 内部的 ``from utils import ...``
会解析到我们的包,然后 ImportError。
"""
