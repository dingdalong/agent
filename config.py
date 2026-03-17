import os
import logging
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# 初始化 OpenAI 客户端
base_url = os.getenv("OPENAI_BASE_URL")
client = OpenAI(
    base_url=base_url,
    api_key=os.getenv("OPENAI_API_KEY")
)

# 模型名称（供其他模块使用）
MODEL_NAME = os.getenv("OPENAI_MODEL")
USER_ID = os.getenv("USER_ID")

# 性能监控日志配置
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('performance.log'),
        logging.StreamHandler()
    ]
)
