import os
import logging
import asyncio
from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAI

load_dotenv()

# 纯异步客户端（替换现有的同步客户端）
async_client = AsyncOpenAI(
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=30.0,
    max_retries=2,
)

# 同步客户端（用于向后兼容）
client = OpenAI(
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=30.0,
    max_retries=2,
)

# 模型名称（供其他模块使用）
MODEL_NAME = os.getenv("OPENAI_MODEL")
USER_ID = os.getenv("USER_ID")

# 并发控制配置
DEFAULT_CONCURRENCY = 5
request_semaphore = asyncio.Semaphore(DEFAULT_CONCURRENCY)

# Plan 模块配置
PLAN_MAX_ADJUSTMENTS = 3
PLAN_MAX_RAW_RESPONSE_LENGTH = 500
PLAN_DEFAULT_TIMEOUT = 30.0
PLAN_GENERATION_TIMEOUT = 60.0
PLAN_MAX_VARIABLE_DEPTH = 10

# 性能监控日志配置
# 避免重复配置日志
if not logging.getLogger().hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('performance.log'),
            logging.StreamHandler()
        ]
    )
