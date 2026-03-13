import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# 初始化 OpenAI 客户端
client = OpenAI(
    base_url=os.getenv("OPENIA_BASE_URL"),   # 注意环境变量名
    api_key=os.getenv("OPENAI_API_KEY")
)

# 模型名称（供其他模块使用）
MODEL_NAME = os.getenv("OPENAI_MODEL")