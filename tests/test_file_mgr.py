from __future__ import annotations

import pytest
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from src.mgr.file_mgr import FileMgr


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


@pytest.fixture
def mgr(workdir):
    deps = MagicMock()
    return FileMgr(workdir=workdir, deps=deps)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── safe_path ──────────────────────────────────────────────

class TestSafePath:
    def test_normal_relative_path(self, mgr, workdir):
        p = mgr.safe_path("a/b.txt")
        assert p == workdir / "a" / "b.txt"

    def test_traversal_rejected(self, mgr):
        with pytest.raises(ValueError, match="Path escapes workspace"):
            mgr.safe_path("../../etc/passwd")

    def test_dot_resolved(self, mgr, workdir):
        p = mgr.safe_path("./a/../a/b.txt")
        assert p == workdir / "a" / "b.txt"

    def test_current_dir(self, mgr, workdir):
        p = mgr.safe_path(".")
        assert p == workdir


# ── persist_large_output ───────────────────────────────────

class TestPersistLargeOutput:
    def test_small_output_returned_as_is(self, mgr):
        result = run(mgr.persist_large_output("tid1", "short"))
        assert result == "short"

    def test_exactly_at_threshold(self, mgr):
        text = "x" * mgr.persist_threshold
        result = run(mgr.persist_large_output("tid2", text))
        assert result == text

    def test_large_output_persisted(self, mgr, workdir):
        text = "y" * (mgr.persist_threshold + 1)
        result = run(mgr.persist_large_output("tid3", text))
        assert "<persisted-output>" in result
        assert "Full output saved to:" in result
        stored = workdir / ".task_outputs" / "tool-results" / "tid3.txt"
        assert stored.exists()
        assert stored.read_text() == text

    def test_preview_length(self, mgr):
        text = "z" * (mgr.persist_threshold + 5000)
        result = run(mgr.persist_large_output("tid4", text))
        preview_line = [l for l in result.splitlines() if l.startswith("z")]
        assert len(preview_line[0]) == mgr.preview_chars

    def test_idempotent_no_overwrite(self, mgr, workdir):
        text1 = "a" * (mgr.persist_threshold + 1)
        text2 = "b" * (mgr.persist_threshold + 1)
        run(mgr.persist_large_output("tid5", text1))
        run(mgr.persist_large_output("tid5", text2))
        stored = workdir / ".task_outputs" / "tool-results" / "tid5.txt"
        assert stored.read_text() == text1


# ── read_file ──────────────────────────────────────────────

class TestReadFile:
    def test_read_entire_file(self, mgr, workdir):
        f = workdir / "hello.txt"
        f.write_text("line1\nline2\nline3\n")
        result = run(mgr.read_file("hello.txt", "r1"))
        assert "总行数: 4" in result or "总行数: 3" in result
        assert "line1" in result
        assert "line3" in result

    def test_read_with_offset_and_limit(self, mgr, workdir):
        f = workdir / "nums.txt"
        f.write_text("\n".join(str(i) for i in range(1, 11)))
        result = run(mgr.read_file("nums.txt", "r2", offset=3, limit=2))
        assert "3" in result
        assert "4" in result
        assert "跳过前 2 行" in result
        assert "剩余" in result

    def test_read_nonexistent(self, mgr):
        result = run(mgr.read_file("no_such_file.txt", "r3"))
        assert "Error" in result

    def test_read_path_escape(self, mgr):
        result = run(mgr.read_file("../../etc/passwd", "r4"))
        assert "Error" in result

    def test_offset_beyond_file(self, mgr, workdir):
        f = workdir / "short.txt"
        f.write_text("one\ntwo\n")
        result = run(mgr.read_file("short.txt", "r5", offset=100))
        assert "总行数:" in result

    def test_limit_none_reads_all(self, mgr, workdir):
        content = "\n".join(f"line{i}" for i in range(50))
        (workdir / "big.txt").write_text(content)
        result = run(mgr.read_file("big.txt", "r6"))
        assert "line49" in result


# ── write_file ─────────────────────────────────────────────

class TestWriteFile:
    def test_write_new_file(self, mgr, workdir):
        result = run(mgr.write_file("new.txt", "hello world"))
        assert "写入" in result
        assert (workdir / "new.txt").read_text() == "hello world"

    def test_overwrite_existing(self, mgr, workdir):
        (workdir / "exist.txt").write_text("old")
        run(mgr.write_file("exist.txt", "new"))
        assert (workdir / "exist.txt").read_text() == "new"

    def test_append_mode(self, mgr, workdir):
        (workdir / "app.txt").write_text("first")
        result = run(mgr.write_file("app.txt", "second", append=True))
        assert "追加" in result
        assert (workdir / "app.txt").read_text() == "firstsecond"

    def test_creates_parent_dirs(self, mgr, workdir):
        run(mgr.write_file("a/b/c/deep.txt", "content"))
        assert (workdir / "a" / "b" / "c" / "deep.txt").read_text() == "content"

    def test_chunked_write_first(self, mgr, workdir):
        result = run(mgr.write_file("chunked.txt", "part1", chunk_index=1, total_chunks=3))
        assert "分块 1/3" in result
        assert "等待下一分块" in result

    def test_chunked_write_middle(self, mgr, workdir):
        (workdir / "chunked2.txt").write_text("part1")
        result = run(mgr.write_file("chunked2.txt", "part2", chunk_index=2, total_chunks=3))
        assert "分块 2/3" in result
        assert (workdir / "chunked2.txt").read_text() == "part1part2"

    def test_chunked_write_last(self, mgr, workdir):
        (workdir / "chunked3.txt").write_text("part1part2")
        result = run(mgr.write_file("chunked3.txt", "part3", chunk_index=3, total_chunks=3))
        assert "最后分块 3/3" in result
        assert "写入完成" in result
        assert (workdir / "chunked3.txt").read_text() == "part1part2part3"

    def test_path_escape(self, mgr):
        result = run(mgr.write_file("../../evil.txt", "hack"))
        assert "Error" in result


# ── edit_file ──────────────────────────────────────────────

class TestEditFileReplace:
    def test_replace_single(self, mgr, workdir):
        (workdir / "r.txt").write_text("aaa bbb aaa")
        result = run(mgr.edit_file("r.txt", mode="replace", old_text="aaa", new_text="ccc", count=1))
        assert "已替换 1 处" in result
        assert (workdir / "r.txt").read_text() == "ccc bbb aaa"

    def test_replace_all(self, mgr, workdir):
        (workdir / "ra.txt").write_text("aaa bbb aaa")
        result = run(mgr.edit_file("ra.txt", mode="replace", old_text="aaa", new_text="ccc", count=0))
        assert "已替换 2 处" in result
        assert (workdir / "ra.txt").read_text() == "ccc bbb ccc"

    def test_replace_no_old_text(self, mgr, workdir):
        (workdir / "rn.txt").write_text("content")
        result = run(mgr.edit_file("rn.txt", mode="replace"))
        assert "Error" in result
        assert "old_text" in result

    def test_replace_not_found(self, mgr, workdir):
        (workdir / "rnf.txt").write_text("content")
        result = run(mgr.edit_file("rnf.txt", mode="replace", old_text="xyz"))
        assert "Error" in result
        assert "未找到" in result

    def test_replace_with_empty(self, mgr, workdir):
        (workdir / "re.txt").write_text("hello world hello")
        run(mgr.edit_file("re.txt", mode="replace", old_text="hello", new_text="", count=0))
        assert (workdir / "re.txt").read_text() == " world "


class TestEditFileRangeReplace:
    def test_range_replace_normal(self, mgr, workdir):
        (workdir / "rr.txt").write_text("line1\nline2\nline3\nline4\n")
        result = run(mgr.edit_file("rr.txt", mode="range_replace",
                                   start_line=2, end_line=3, new_text="new2\nnew3"))
        assert "已替换第 2-3 行" in result
        content = (workdir / "rr.txt").read_text()
        assert "new2\n" in content
        assert "new3\n" in content

    def test_range_replace_missing_params(self, mgr, workdir):
        (workdir / "rrm.txt").write_text("line1\n")
        result = run(mgr.edit_file("rrm.txt", mode="range_replace", start_line=1))
        assert "Error" in result

    def test_range_replace_invalid_range(self, mgr, workdir):
        (workdir / "rri.txt").write_text("line1\nline2\n")
        result = run(mgr.edit_file("rri.txt", mode="range_replace",
                                   start_line=3, end_line=5))
        assert "Error" in result
        assert "行号范围无效" in result

    def test_range_replace_start_gt_end(self, mgr, workdir):
        (workdir / "rrs.txt").write_text("line1\nline2\nline3\n")
        result = run(mgr.edit_file("rrs.txt", mode="range_replace",
                                   start_line=3, end_line=1))
        assert "Error" in result


class TestEditFileInsert:
    def test_insert_at_beginning(self, mgr, workdir):
        (workdir / "ib.txt").write_text("line1\nline2\n")
        result = run(mgr.edit_file("ib.txt", mode="insert",
                                   start_line=1, new_text="new0"))
        assert "插入" in result
        content = (workdir / "ib.txt").read_text()
        assert content.startswith("new0\n")

    def test_insert_in_middle(self, mgr, workdir):
        (workdir / "im.txt").write_text("line1\nline2\nline3\n")
        run(mgr.edit_file("im.txt", mode="insert",
                          start_line=2, new_text="inserted"))
        lines = (workdir / "im.txt").read_text().splitlines()
        assert lines[1] == "inserted"
        assert lines[2] == "line2"

    def test_insert_at_end(self, mgr, workdir):
        (workdir / "ie.txt").write_text("line1\nline2\n")
        total = len((workdir / "ie.txt").read_text().splitlines())
        run(mgr.edit_file("ie.txt", mode="insert",
                          start_line=total + 1, new_text="last"))
        content = (workdir / "ie.txt").read_text()
        assert content.rstrip().endswith("last")

    def test_insert_missing_start_line(self, mgr, workdir):
        (workdir / "ims.txt").write_text("content\n")
        result = run(mgr.edit_file("ims.txt", mode="insert"))
        assert "Error" in result

    def test_insert_invalid_line(self, mgr, workdir):
        (workdir / "iil.txt").write_text("line1\n")
        result = run(mgr.edit_file("iil.txt", mode="insert", start_line=0))
        assert "Error" in result
        result2 = run(mgr.edit_file("iil.txt", mode="insert", start_line=100))
        assert "Error" in result2


class TestEditFileDelete:
    def test_delete_lines(self, mgr, workdir):
        (workdir / "d.txt").write_text("line1\nline2\nline3\nline4\n")
        result = run(mgr.edit_file("d.txt", mode="delete",
                                   start_line=2, end_line=3))
        assert "已删除第 2-3 行" in result
        assert "2 行" in result
        lines = (workdir / "d.txt").read_text().splitlines()
        assert lines == ["line1", "line4"]

    def test_delete_single_line(self, mgr, workdir):
        (workdir / "ds.txt").write_text("line1\nline2\nline3\n")
        run(mgr.edit_file("ds.txt", mode="delete",
                          start_line=2, end_line=2))
        lines = (workdir / "ds.txt").read_text().splitlines()
        assert lines == ["line1", "line3"]

    def test_delete_missing_params(self, mgr, workdir):
        (workdir / "dm.txt").write_text("content\n")
        result = run(mgr.edit_file("dm.txt", mode="delete", start_line=1))
        assert "Error" in result

    def test_delete_invalid_range(self, mgr, workdir):
        (workdir / "di.txt").write_text("line1\n")
        result = run(mgr.edit_file("di.txt", mode="delete",
                                   start_line=0, end_line=1))
        assert "Error" in result


class TestEditFileUnknownMode:
    def test_unknown_mode(self, mgr, workdir):
        (workdir / "u.txt").write_text("content")
        result = run(mgr.edit_file("u.txt", mode="bogus"))
        assert "Error" in result
        assert "未知模式" in result

    def test_nonexistent_file(self, mgr):
        result = run(mgr.edit_file("nope.txt", mode="replace", old_text="a", new_text="b"))
        assert "Error" in result


# ── get_file_info ──────────────────────────────────────────

class TestGetFileInfo:
    def test_file_info(self, mgr, workdir):
        f = workdir / "info.txt"
        f.write_text("hello\nworld\n")
        result = run(mgr.get_file_info("info.txt"))
        assert "文件" in result
        assert "行数: 2" in result
        assert "大小:" in result
        assert "权限:" in result

    def test_dir_info(self, mgr, workdir):
        d = workdir / "mydir"
        d.mkdir()
        (d / "a.txt").write_text("a")
        (d / "sub").mkdir()
        result = run(mgr.get_file_info("mydir"))
        assert "目录" in result
        assert "1 个目录" in result
        assert "1 个文件" in result

    def test_nonexistent(self, mgr):
        result = run(mgr.get_file_info("ghost"))
        assert "Error" in result
        assert "不存在" in result

    def test_file_extension(self, mgr, workdir):
        (workdir / "test.py").write_text("pass\n")
        result = run(mgr.get_file_info("test.py"))
        assert ".py" in result

    def test_file_no_extension(self, mgr, workdir):
        (workdir / "Makefile").write_text("all:\n")
        result = run(mgr.get_file_info("Makefile"))
        assert "无" in result


# ── _format_size ───────────────────────────────────────────

class TestFormatSize:
    def test_bytes(self, mgr):
        assert mgr._format_size(0) == "0B"
        assert mgr._format_size(500) == "500B"
        assert mgr._format_size(1023) == "1023B"

    def test_kilobytes(self, mgr):
        assert mgr._format_size(1024) == "1.0KB"
        assert mgr._format_size(1536) == "1.5KB"

    def test_megabytes(self, mgr):
        assert mgr._format_size(1024 * 1024) == "1.0MB"
        assert mgr._format_size(int(1.5 * 1024 * 1024)) == "1.5MB"


# ── list_directory ─────────────────────────────────────────

class TestListDirectory:
    def test_list_flat(self, mgr, workdir):
        (workdir / "a.txt").write_text("a")
        (workdir / "b.txt").write_text("b")
        (workdir / "sub").mkdir()
        result = run(mgr.list_directory(".", "l1"))
        assert "a.txt" in result
        assert "b.txt" in result
        assert "[DIR]" in result

    def test_list_recursive(self, mgr, workdir):
        (workdir / "d1").mkdir()
        (workdir / "d1" / "d2").mkdir()
        (workdir / "d1" / "d2" / "deep.txt").write_text("x")
        result = run(mgr.list_directory(".", "l2", recursive=True))
        assert "deep.txt" in result

    def test_max_depth(self, mgr, workdir):
        (workdir / "a").mkdir()
        (workdir / "a" / "b").mkdir()
        (workdir / "a" / "b" / "c").mkdir()
        (workdir / "a" / "b" / "c" / "d.txt").write_text("x")
        result = run(mgr.list_directory(".", "l3", recursive=True, max_depth=1))
        assert "未展开" in result

    def test_nonexistent_dir(self, mgr):
        result = run(mgr.list_directory("nope", "l4"))
        assert "Error" in result
        assert "不存在" in result

    def test_not_a_dir(self, mgr, workdir):
        (workdir / "f.txt").write_text("x")
        result = run(mgr.list_directory("f.txt", "l5"))
        assert "Error" in result
        assert "不是目录" in result

    def test_empty_dir(self, mgr, workdir):
        (workdir / "empty").mkdir()
        result = run(mgr.list_directory("empty", "l6"))
        assert "0 个目录" in result
        assert "0 个文件" in result


# ── create_directory ───────────────────────────────────────

class TestCreateDirectory:
    def test_create_new(self, mgr, workdir):
        result = run(mgr.create_directory("newdir"))
        assert "已创建" in result
        assert (workdir / "newdir").is_dir()

    def test_create_nested(self, mgr, workdir):
        result = run(mgr.create_directory("a/b/c"))
        assert "已创建" in result
        assert (workdir / "a" / "b" / "c").is_dir()

    def test_already_exists(self, mgr, workdir):
        (workdir / "exist").mkdir()
        result = run(mgr.create_directory("exist"))
        assert "已存在" in result

    def test_path_escape(self, mgr):
        result = run(mgr.create_directory("../../evil"))
        assert "Error" in result


# ── move_file ──────────────────────────────────────────────

class TestMoveFile:
    def test_move_file(self, mgr, workdir):
        (workdir / "src.txt").write_text("data")
        result = run(mgr.move_file("src.txt", "dst.txt"))
        assert "已移动" in result
        assert not (workdir / "src.txt").exists()
        assert (workdir / "dst.txt").read_text() == "data"

    def test_move_into_directory(self, mgr, workdir):
        (workdir / "file.txt").write_text("data")
        (workdir / "target_dir").mkdir()
        result = run(mgr.move_file("file.txt", "target_dir"))
        assert "已移动" in result
        assert (workdir / "target_dir" / "file.txt").read_text() == "data"

    def test_move_creates_parent_dirs(self, mgr, workdir):
        (workdir / "orig.txt").write_text("data")
        run(mgr.move_file("orig.txt", "x/y/z/moved.txt"))
        assert (workdir / "x" / "y" / "z" / "moved.txt").read_text() == "data"

    def test_move_source_not_found(self, mgr):
        result = run(mgr.move_file("ghost.txt", "dst.txt"))
        assert "Error" in result
        assert "不存在" in result

    def test_move_directory(self, mgr, workdir):
        d = workdir / "mydir"
        d.mkdir()
        (d / "inner.txt").write_text("x")
        result = run(mgr.move_file("mydir", "renamed"))
        assert "目录" in result
        assert (workdir / "renamed" / "inner.txt").read_text() == "x"

    def test_move_path_escape(self, mgr, workdir):
        (workdir / "ok.txt").write_text("data")
        result = run(mgr.move_file("ok.txt", "../../evil.txt"))
        assert "Error" in result


# ── find_files ─────────────────────────────────────────────

class TestFindFiles:
    def test_find_by_extension(self, mgr, workdir):
        (workdir / "a.py").write_text("")
        (workdir / "b.py").write_text("")
        (workdir / "c.txt").write_text("")
        result = run(mgr.find_files("*.py", "f1"))
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result
        assert "找到 2 个文件" in result

    def test_find_recursive(self, mgr, workdir):
        (workdir / "sub").mkdir()
        (workdir / "sub" / "deep.py").write_text("")
        result = run(mgr.find_files("**/*.py", "f2"))
        assert "deep.py" in result

    def test_find_no_matches(self, mgr, workdir):
        result = run(mgr.find_files("*.xyz", "f3"))
        assert "找到 0 个文件" in result

    def test_find_nonexistent_path(self, mgr):
        result = run(mgr.find_files("*", "f4", path="ghost"))
        assert "Error" in result

    def test_find_not_a_dir(self, mgr, workdir):
        (workdir / "f.txt").write_text("")
        result = run(mgr.find_files("*", "f5", path="f.txt"))
        assert "Error" in result

    def test_find_in_subdir(self, mgr, workdir):
        (workdir / "sub").mkdir()
        (workdir / "sub" / "target.log").write_text("")
        (workdir / "other.log").write_text("")
        result = run(mgr.find_files("*.log", "f6", path="sub"))
        assert "target.log" in result
        assert "other.log" not in result


# ── tool_results_dir ───────────────────────────────────────

class TestPostInit:
    def test_tool_results_dir_set(self, mgr, workdir):
        assert mgr.tool_results_dir == workdir / ".task_outputs" / "tool-results"
