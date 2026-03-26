import pytest
import os
from src.tools.builtin.file import (
    write_file, read_file, delete_file, list_files,
    append_file, search_file, find_files, insert_lines, delete_lines,
    replace_lines, find_replace, _safe_path, _read_lines,
    _validate_line_range,
)

WORKSPACE = os.path.abspath("./workspace")


@pytest.fixture(autouse=True)
def _ensure_workspace(workspace_dir):
    """Delegate to root conftest workspace_dir fixture."""
    yield


# ===================== 辅助函数测试 =====================

class TestSafePath:
    def test_normal_path(self):
        path, err = _safe_path("hello.txt")
        assert err is None
        assert path == os.path.join(WORKSPACE, "hello.txt")

    def test_subdir_path(self):
        path, err = _safe_path("sub/hello.txt")
        assert err is None
        assert path.endswith("sub/hello.txt")

    def test_path_traversal_blocked(self):
        path, err = _safe_path("../../etc/passwd")
        assert err is not None
        assert "路径遍历" in err

    def test_absolute_path_blocked(self):
        path, err = _safe_path("/etc/passwd")
        assert err is not None
        assert "路径遍历" in err


class TestValidateLineRange:
    def test_valid_range(self):
        lines = ["a\n", "b\n", "c\n"]
        assert _validate_line_range(lines, 1, 3) is None

    def test_single_line(self):
        lines = ["a\n", "b\n"]
        assert _validate_line_range(lines, 2, 2) is None

    def test_start_zero(self):
        lines = ["a\n"]
        err = _validate_line_range(lines, 0, 1)
        assert err is not None

    def test_end_exceeds(self):
        lines = ["a\n", "b\n"]
        err = _validate_line_range(lines, 1, 3)
        assert "超出范围" in err

    def test_start_greater_than_end(self):
        lines = ["a\n", "b\n", "c\n"]
        err = _validate_line_range(lines, 3, 1)
        assert "不能大于" in err


# ===================== write_file 测试 =====================

class TestWriteFile:
    @pytest.mark.asyncio
    async def test_write_basic(self):
        result = await write_file("test_write.txt", "hello world")
        assert "已保存" in result
        with open(os.path.join(WORKSPACE, "test_write.txt")) as f:
            assert f.read() == "hello world"

    @pytest.mark.asyncio
    async def test_write_overwrite(self):
        await write_file("test_overwrite.txt", "old content")
        await write_file("test_overwrite.txt", "new content")
        with open(os.path.join(WORKSPACE, "test_overwrite.txt")) as f:
            assert f.read() == "new content"

    @pytest.mark.asyncio
    async def test_write_subdirectory(self):
        result = await write_file("test_subdir/hello.txt", "sub content")
        assert "已保存" in result
        path = os.path.join(WORKSPACE, "test_subdir/hello.txt")
        assert os.path.isfile(path)

    @pytest.mark.asyncio
    async def test_write_path_traversal(self):
        result = await write_file("../../evil.txt", "bad")
        assert "路径遍历" in result

    @pytest.mark.asyncio
    async def test_write_empty_content(self):
        result = await write_file("test_empty.txt", "")
        assert "已保存" in result

    @pytest.mark.asyncio
    async def test_write_chinese_content(self):
        result = await write_file("test_chinese.txt", "你好世界")
        assert "已保存" in result
        with open(os.path.join(WORKSPACE, "test_chinese.txt"), encoding="utf-8") as f:
            assert f.read() == "你好世界"


# ===================== read_file 测试 =====================

class TestReadFile:
    @pytest.mark.asyncio
    async def test_read_basic(self):
        await write_file("test_read.txt", "hello\nworld\n")
        result = await read_file("test_read.txt")
        assert "hello" in result
        assert "world" in result

    @pytest.mark.asyncio
    async def test_read_nonexistent(self):
        result = await read_file("test_nonexistent.txt")
        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_read_empty_file(self):
        await write_file("test_empty_read.txt", "")
        result = await read_file("test_empty_read.txt")
        assert "为空" in result

    @pytest.mark.asyncio
    async def test_read_with_line_numbers(self):
        await write_file("test_ln.txt", "aaa\nbbb\nccc\n")
        result = await read_file("test_ln.txt", show_line_numbers=True)
        assert "1: aaa" in result
        assert "2: bbb" in result
        assert "3: ccc" in result

    @pytest.mark.asyncio
    async def test_read_line_range(self):
        await write_file("test_range.txt", "L1\nL2\nL3\nL4\nL5\n")
        result = await read_file("test_range.txt", show_line_numbers=True, start_line=2, end_line=4)
        assert "1: L1" not in result
        assert "2: L2" in result
        assert "4: L4" in result
        assert "5: L5" not in result

    @pytest.mark.asyncio
    async def test_read_single_line(self):
        await write_file("test_single.txt", "L1\nL2\nL3\n")
        result = await read_file("test_single.txt", show_line_numbers=True, start_line=2, end_line=2)
        assert "2: L2" in result
        assert "L1" not in result
        assert "L3" not in result

    @pytest.mark.asyncio
    async def test_read_invalid_range(self):
        await write_file("test_bad_range.txt", "L1\nL2\n")
        result = await read_file("test_bad_range.txt", start_line=1, end_line=5)
        assert "超出范围" in result

    @pytest.mark.asyncio
    async def test_read_path_traversal(self):
        result = await read_file("../../etc/passwd")
        assert "路径遍历" in result


# ===================== delete_file 测试 =====================

class TestDeleteFile:
    @pytest.mark.asyncio
    async def test_delete_basic(self):
        await write_file("test_del.txt", "to delete")
        result = await delete_file("test_del.txt")
        assert "已删除" in result
        assert not os.path.exists(os.path.join(WORKSPACE, "test_del.txt"))

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        result = await delete_file("test_no_such_file.txt")
        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_delete_directory_rejected(self):
        os.makedirs(os.path.join(WORKSPACE, "test_dir_del"), exist_ok=True)
        result = await delete_file("test_dir_del")
        assert "目录" in result
        # 清理
        os.rmdir(os.path.join(WORKSPACE, "test_dir_del"))


# ===================== list_files 测试 =====================

class TestListFiles:
    @pytest.mark.asyncio
    async def test_list_basic(self):
        await write_file("test_list_a.txt", "a")
        await write_file("test_list_b.txt", "b")
        result = await list_files()
        assert "test_list_a.txt" in result
        assert "test_list_b.txt" in result

    @pytest.mark.asyncio
    async def test_list_shows_dirs(self):
        os.makedirs(os.path.join(WORKSPACE, "test_list_dir"), exist_ok=True)
        result = await list_files()
        assert "test_list_dir/" in result
        os.rmdir(os.path.join(WORKSPACE, "test_list_dir"))

    @pytest.mark.asyncio
    async def test_list_subdir(self):
        await write_file("test_sub_list/file.txt", "content")
        result = await list_files("test_sub_list")
        assert "file.txt" in result

    @pytest.mark.asyncio
    async def test_list_nonexistent_dir(self):
        result = await list_files("test_no_dir_999")
        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_list_path_traversal(self):
        result = await list_files("../../")
        assert "超出" in result


# ===================== append_file 测试 =====================

class TestAppendFile:
    @pytest.mark.asyncio
    async def test_append_basic(self):
        await write_file("test_append.txt", "line1\n")
        result = await append_file("test_append.txt", "line2\n")
        assert "已追加" in result
        with open(os.path.join(WORKSPACE, "test_append.txt")) as f:
            assert f.read() == "line1\nline2\n"

    @pytest.mark.asyncio
    async def test_append_nonexistent(self):
        result = await append_file("test_no_append.txt", "data")
        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_append_multiple(self):
        await write_file("test_multi_append.txt", "A")
        await append_file("test_multi_append.txt", "B")
        await append_file("test_multi_append.txt", "C")
        with open(os.path.join(WORKSPACE, "test_multi_append.txt")) as f:
            assert f.read() == "ABC"


# ===================== search_file 测试 =====================

class TestSearchFile:
    @pytest.mark.asyncio
    async def test_search_in_file(self):
        await write_file("test_search.txt", "hello world\nfoo bar\nhello again\n")
        result = await search_file("hello", "test_search.txt")
        assert "test_search.txt:1:" in result
        assert "test_search.txt:3:" in result

    @pytest.mark.asyncio
    async def test_search_not_found(self):
        await write_file("test_search_empty.txt", "nothing here\n")
        result = await search_file("xyz", "test_search_empty.txt")
        assert "未找到" in result

    @pytest.mark.asyncio
    async def test_search_all_files(self):
        await write_file("test_s1.txt", "apple\n")
        await write_file("test_s2.txt", "banana\napple pie\n")
        result = await search_file("apple")
        assert "test_s1.txt" in result
        assert "test_s2.txt" in result

    @pytest.mark.asyncio
    async def test_search_nonexistent_file(self):
        result = await search_file("x", "test_no_file.txt")
        assert "不存在" in result


# ===================== insert_lines 测试 =====================

class TestInsertLines:
    @pytest.mark.asyncio
    async def test_insert_at_beginning(self):
        await write_file("test_ins.txt", "B\nC\n")
        result = await insert_lines("test_ins.txt", 1, "A")
        assert "插入" in result
        content = await read_file("test_ins.txt")
        assert content.startswith("A\n")

    @pytest.mark.asyncio
    async def test_insert_in_middle(self):
        await write_file("test_ins_mid.txt", "A\nC\n")
        await insert_lines("test_ins_mid.txt", 2, "B")
        result = await read_file("test_ins_mid.txt", show_line_numbers=True)
        assert "1: A" in result
        assert "2: B" in result
        assert "3: C" in result

    @pytest.mark.asyncio
    async def test_insert_at_end(self):
        await write_file("test_ins_end.txt", "A\nB\n")
        await insert_lines("test_ins_end.txt", 3, "C")
        result = await read_file("test_ins_end.txt", show_line_numbers=True)
        assert "3: C" in result

    @pytest.mark.asyncio
    async def test_insert_multiline(self):
        await write_file("test_ins_multi.txt", "A\nD\n")
        await insert_lines("test_ins_multi.txt", 2, "B\nC")
        result = await read_file("test_ins_multi.txt", show_line_numbers=True)
        assert "2: B" in result
        assert "3: C" in result
        assert "4: D" in result

    @pytest.mark.asyncio
    async def test_insert_invalid_line_zero(self):
        await write_file("test_ins_bad.txt", "A\n")
        result = await insert_lines("test_ins_bad.txt", 0, "X")
        assert "超出范围" in result

    @pytest.mark.asyncio
    async def test_insert_invalid_line_too_large(self):
        await write_file("test_ins_big.txt", "A\n")
        result = await insert_lines("test_ins_big.txt", 5, "X")
        assert "超出范围" in result

    @pytest.mark.asyncio
    async def test_insert_nonexistent_file(self):
        result = await insert_lines("test_no_ins.txt", 1, "X")
        assert "不存在" in result


# ===================== delete_lines 测试 =====================

class TestDeleteLines:
    @pytest.mark.asyncio
    async def test_delete_single_line(self):
        await write_file("test_dl.txt", "A\nB\nC\n")
        result = await delete_lines("test_dl.txt", 2, 2)
        assert "已删除" in result
        content = await read_file("test_dl.txt", show_line_numbers=True)
        assert "1: A" in content
        assert "2: C" in content
        assert "B" not in content

    @pytest.mark.asyncio
    async def test_delete_range(self):
        await write_file("test_dl_range.txt", "A\nB\nC\nD\nE\n")
        await delete_lines("test_dl_range.txt", 2, 4)
        result = await read_file("test_dl_range.txt", show_line_numbers=True)
        assert "1: A" in result
        assert "2: E" in result

    @pytest.mark.asyncio
    async def test_delete_all_lines(self):
        await write_file("test_dl_all.txt", "A\nB\n")
        await delete_lines("test_dl_all.txt", 1, 2)
        result = await read_file("test_dl_all.txt")
        assert "为空" in result

    @pytest.mark.asyncio
    async def test_delete_first_line(self):
        await write_file("test_dl_first.txt", "A\nB\nC\n")
        await delete_lines("test_dl_first.txt", 1, 1)
        content = await read_file("test_dl_first.txt")
        assert content.startswith("B\n")

    @pytest.mark.asyncio
    async def test_delete_last_line(self):
        await write_file("test_dl_last.txt", "A\nB\nC\n")
        await delete_lines("test_dl_last.txt", 3, 3)
        content = await read_file("test_dl_last.txt", show_line_numbers=True)
        assert "C" not in content

    @pytest.mark.asyncio
    async def test_delete_invalid_range(self):
        await write_file("test_dl_bad.txt", "A\nB\nC\n")
        result = await delete_lines("test_dl_bad.txt", 3, 1)
        assert "不能大于" in result

    @pytest.mark.asyncio
    async def test_delete_out_of_bounds(self):
        await write_file("test_dl_oob.txt", "A\n")
        result = await delete_lines("test_dl_oob.txt", 1, 5)
        assert "超出范围" in result

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        result = await delete_lines("test_no_dl.txt", 1, 1)
        assert "不存在" in result


# ===================== replace_lines 测试 =====================

class TestReplaceLines:
    @pytest.mark.asyncio
    async def test_replace_single_line(self):
        await write_file("test_rl.txt", "A\nB\nC\n")
        result = await replace_lines("test_rl.txt", 2, 2, "X")
        assert "已替换" in result
        content = await read_file("test_rl.txt", show_line_numbers=True)
        assert "2: X" in content

    @pytest.mark.asyncio
    async def test_replace_range_with_fewer_lines(self):
        await write_file("test_rl_less.txt", "A\nB\nC\nD\n")
        await replace_lines("test_rl_less.txt", 2, 3, "X")
        content = await read_file("test_rl_less.txt", show_line_numbers=True)
        assert "1: A" in content
        assert "2: X" in content
        assert "3: D" in content

    @pytest.mark.asyncio
    async def test_replace_range_with_more_lines(self):
        await write_file("test_rl_more.txt", "A\nB\nC\n")
        await replace_lines("test_rl_more.txt", 2, 2, "X\nY\nZ")
        content = await read_file("test_rl_more.txt", show_line_numbers=True)
        assert "2: X" in content
        assert "3: Y" in content
        assert "4: Z" in content
        assert "5: C" in content

    @pytest.mark.asyncio
    async def test_replace_all_lines(self):
        await write_file("test_rl_all.txt", "old1\nold2\n")
        await replace_lines("test_rl_all.txt", 1, 2, "new1\nnew2\nnew3")
        content = await read_file("test_rl_all.txt", show_line_numbers=True)
        assert "1: new1" in content
        assert "3: new3" in content

    @pytest.mark.asyncio
    async def test_replace_invalid_range(self):
        await write_file("test_rl_bad.txt", "A\n")
        result = await replace_lines("test_rl_bad.txt", 1, 5, "X")
        assert "超出范围" in result

    @pytest.mark.asyncio
    async def test_replace_nonexistent(self):
        result = await replace_lines("test_no_rl.txt", 1, 1, "X")
        assert "不存在" in result


# ===================== find_replace 测试 =====================

class TestFindReplace:
    @pytest.mark.asyncio
    async def test_replace_first_occurrence(self):
        await write_file("test_fr.txt", "foo bar foo baz\n")
        result = await find_replace("test_fr.txt", "foo", "qux")
        assert "1 处" in result
        with open(os.path.join(WORKSPACE, "test_fr.txt")) as f:
            content = f.read()
        assert content == "qux bar foo baz\n"

    @pytest.mark.asyncio
    async def test_replace_all_occurrences(self):
        await write_file("test_fr_all.txt", "foo bar foo baz foo\n")
        result = await find_replace("test_fr_all.txt", "foo", "qux", replace_all=True)
        assert "3 处" in result
        with open(os.path.join(WORKSPACE, "test_fr_all.txt")) as f:
            content = f.read()
        assert "foo" not in content
        assert content.count("qux") == 3

    @pytest.mark.asyncio
    async def test_replace_not_found(self):
        await write_file("test_fr_nf.txt", "hello world\n")
        result = await find_replace("test_fr_nf.txt", "xyz", "abc")
        assert "未找到" in result

    @pytest.mark.asyncio
    async def test_replace_multiline_text(self):
        await write_file("test_fr_ml.txt", "line1\nline2\nline3\n")
        await find_replace("test_fr_ml.txt", "line2\nline3", "new2\nnew3")
        with open(os.path.join(WORKSPACE, "test_fr_ml.txt")) as f:
            content = f.read()
        assert "new2\nnew3" in content

    @pytest.mark.asyncio
    async def test_replace_with_empty_string(self):
        await write_file("test_fr_empty.txt", "hello world\n")
        await find_replace("test_fr_empty.txt", " world", "")
        with open(os.path.join(WORKSPACE, "test_fr_empty.txt")) as f:
            assert f.read() == "hello\n"

    @pytest.mark.asyncio
    async def test_replace_nonexistent_file(self):
        result = await find_replace("test_no_fr.txt", "a", "b")
        assert "不存在" in result


# ===================== search_file 正则测试 =====================

class TestSearchFileRegex:
    @pytest.mark.asyncio
    async def test_regex_search_in_file(self):
        await write_file("test_regex.txt", "foo123\nbar456\nfoo789\n")
        result = await search_file(r"foo\d+", "test_regex.txt", use_regex=True)
        assert "test_regex.txt:1:" in result
        assert "test_regex.txt:3:" in result
        assert "bar" not in result

    @pytest.mark.asyncio
    async def test_regex_search_all_files(self):
        await write_file("test_rx1.txt", "hello world\n")
        await write_file("test_rx2.txt", "HELLO WORLD\n")
        result = await search_file(r"[Hh]ello", use_regex=True)
        assert "test_rx1.txt" in result
        assert "test_rx2.txt" not in result  # 大写 HELLO 不匹配 [Hh]ello

    @pytest.mark.asyncio
    async def test_regex_case_insensitive(self):
        await write_file("test_rxi.txt", "Hello World\nhello world\n")
        result = await search_file(r"(?i)hello", "test_rxi.txt", use_regex=True)
        assert "test_rxi.txt:1:" in result
        assert "test_rxi.txt:2:" in result

    @pytest.mark.asyncio
    async def test_regex_invalid_pattern(self):
        result = await search_file(r"[invalid", "test_regex.txt", use_regex=True)
        assert "正则表达式无效" in result

    @pytest.mark.asyncio
    async def test_regex_not_found(self):
        await write_file("test_rx_nf.txt", "hello world\n")
        result = await search_file(r"^\d+$", "test_rx_nf.txt", use_regex=True)
        assert "未找到" in result

    @pytest.mark.asyncio
    async def test_plain_search_still_works(self):
        """确认不传 use_regex 时行为不变"""
        await write_file("test_plain.txt", "foo.bar\nfoo-bar\n")
        result = await search_file("foo.bar", "test_plain.txt")
        # 纯文本匹配，"foo.bar" 只匹配第 1 行
        assert "test_plain.txt:1:" in result


# ===================== find_files 测试 =====================

class TestFindFiles:
    @pytest.mark.asyncio
    async def test_find_by_extension(self):
        await write_file("test_ff_a.py", "# python file")
        await write_file("test_ff_b.txt", "text file")
        result = await find_files("test_ff_*.py")
        assert "test_ff_a.py" in result
        assert "test_ff_b.txt" not in result

    @pytest.mark.asyncio
    async def test_find_with_keyword_filter(self):
        await write_file("test_fk_a.txt", "apple banana")
        await write_file("test_fk_b.txt", "cherry date")
        result = await find_files("test_fk_*.txt", keyword="apple")
        assert "test_fk_a.txt" in result
        assert "test_fk_b.txt" not in result

    @pytest.mark.asyncio
    async def test_find_no_match(self):
        result = await find_files("test_nonexistent_pattern_*.xyz")
        assert "未找到" in result

    @pytest.mark.asyncio
    async def test_find_recursive(self):
        await write_file("test_ffdir/nested.txt", "nested content")
        result = await find_files("**/*.txt")
        assert "nested.txt" in result


# ===================== list_files 递归测试 =====================

class TestListFilesRecursive:
    @pytest.mark.asyncio
    async def test_recursive_list(self):
        await write_file("test_recdir/sub/deep.txt", "deep content")
        await write_file("test_recdir/top.txt", "top content")
        result = await list_files("test_recdir", recursive=True)
        assert "deep.txt" in result
        assert "top.txt" in result

    @pytest.mark.asyncio
    async def test_non_recursive_default(self):
        await write_file("test_nrdir/sub/deep.txt", "deep")
        await write_file("test_nrdir/top.txt", "top")
        result = await list_files("test_nrdir")
        assert "top.txt" in result
        assert "sub/" in result
        assert "deep.txt" not in result


# ===================== 端到端流程测试 =====================

class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_full_edit_workflow(self):
        """模拟完整的代码编辑流程"""
        # 1. 创建文件
        await write_file("test_e2e.py", "def hello():\n    print('hi')\n\ndef main():\n    hello()\n")

        # 2. 带行号读取，确认内容
        result = await read_file("test_e2e.py", show_line_numbers=True)
        assert "1: def hello():" in result
        assert "5:     hello()" in result

        # 3. 在第 3 行插入新函数
        await insert_lines("test_e2e.py", 3, "\ndef goodbye():\n    print('bye')\n")

        # 4. 确认插入后的状态
        result = await read_file("test_e2e.py", show_line_numbers=True)
        assert "goodbye" in result

        # 5. 用 find_replace 修改函数内容
        await find_replace("test_e2e.py", "print('hi')", "print('hello!')")

        # 6. 删除空行（第 3 行）
        result = await read_file("test_e2e.py", show_line_numbers=True, start_line=3, end_line=3)
        if result.strip().endswith(":") is False:
            await delete_lines("test_e2e.py", 3, 3)

        # 7. 搜索确认修改
        result = await search_file("hello!", "test_e2e.py")
        assert "hello!" in result

        # 8. 最终读取验证
        final = await read_file("test_e2e.py")
        assert "hello!" in final
        assert "goodbye" in final
        assert "main" in final

    @pytest.mark.asyncio
    async def test_list_then_read(self):
        """列出文件后读取"""
        await write_file("test_lr_a.txt", "content A")
        await write_file("test_lr_b.txt", "content B")
        listing = await list_files()
        assert "test_lr_a.txt" in listing
        content = await read_file("test_lr_a.txt")
        assert content == "content A"
