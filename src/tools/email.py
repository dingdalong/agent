from . import tool
from pydantic import BaseModel, Field, EmailStr
from typing import List

class EmailArgs(BaseModel):
    recipients: List[EmailStr] = Field(description="收件人邮箱列表")
    subject: str = Field(description="邮件主题")
    body: str = Field(description="邮件正文")

@tool(model=EmailArgs, description="发送邮件（模拟）", sensitive=True,
      confirm_template="发送邮件给 {recipients}，主题：{subject}")
async def send_email(recipients: List[str], subject: str, body: str) -> str:
    # 模拟发送
    print(f"\n📧 模拟发送邮件给 {recipients}，主题：{subject}，正文：{body}")
    return f"邮件已发送给 {len(recipients)} 个收件人"
