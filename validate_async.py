#!/usr/bin/env python3
"""异步重构验证脚本"""

import asyncio
import sys

async def validate_imports():
    """验证所有模块导入正常"""
    print("1. 验证模块导入...")

    try:
        from src.core.async_api import call_model, parse_stream_response, parse_nonstream_response, execute_tool_calls
        print("  ✅ src.core.async_api 导入成功")

        from src.core.performance import async_time_function
        print("  ✅ src.core.performance 导入成功")

        from src.tools import tools, tool_executor
        print(f"  ✅ src.tools 导入成功: {len(tools)} 个工具")

        from src.memory import ConversationBuffer, MemoryStore
        print("  ✅ src.memory 导入成功")

        from config import async_client, client, MODEL_NAME, USER_ID
        print("  ✅ config 导入成功")

        print("  所有模块导入正常!")
        return True
    except Exception as e:
        print(f"  ❌ 导入错误: {e}")
        import traceback
        traceback.print_exc()
        return False

async def validate_tool_discovery():
    """验证工具自动发现机制"""
    print("\n2. 验证工具自动发现...")

    from src.tools import tools, tool_executor

    print(f"  发现工具数量: {len(tools)}")
    for i, tool in enumerate(tools, 1):
        name = tool['function']['name']
        print(f"  工具 {i}: {name}")

    print(f"  工具执行器数量: {len(tool_executor)}")
    for name, func in tool_executor.items():
        is_async = asyncio.iscoroutinefunction(func)
        print(f"    执行器 '{name}': {'异步' if is_async else '同步'}")

    return len(tools) > 0

async def validate_main_program():
    """验证主程序结构"""
    print("\n3. 验证主程序结构...")

    try:
        import main
        print("  ✅ main.py 导入成功")

        # 检查关键函数
        if hasattr(main, 'run_agent') and asyncio.iscoroutinefunction(main.run_agent):
            print("  ✅ run_agent 是异步函数")
        else:
            print("  ❌ run_agent 不是异步函数")
            return False

        if hasattr(main, 'main') and asyncio.iscoroutinefunction(main.main):
            print("  ✅ main 是异步函数")
        else:
            print("  ❌ main 不是异步函数")
            return False

        print("  主程序结构验证通过!")
        return True
    except Exception as e:
        print(f"  ❌ 验证错误: {e}")
        import traceback
        traceback.print_exc()
        return False

async def main():
    """主验证流程"""
    print("=" * 60)
    print("异步API重构验证")
    print("=" * 60)

    all_passed = True

    # 验证导入
    if not await validate_imports():
        all_passed = False

    # 验证工具发现
    if not await validate_tool_discovery():
        all_passed = False

    # 验证主程序
    if not await validate_main_program():
        all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("✅ 所有验证通过!")
        print("异步API重构完成，所有组件正常工作。")
    else:
        print("❌ 验证失败，请检查上述错误。")
        sys.exit(1)

    return all_passed

if __name__ == "__main__":
    asyncio.run(main())