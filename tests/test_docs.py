"""文档一致性测试。

文档漂移比代码 bug 更难发现 —— 用户照着文档敲命令、结果参数不存在。
这一层把"文档说的"和"实现做的"绑在一起:

1. `docs/使用手册.md` 的选项表必须与 `cli.build_parser()` 的选项集合**完全一致**
   （数量、短写法、长写法都对得上）;
2. 文档里的相对链接必须指向真实存在的文件（README ↔ docs 互相引用）;
3. README 引用的文档必须存在，且关键小节标题在位（链接锚点不会指空）。

不检查正文措辞（那属于人工维护），只检查"会让人敲错命令"的部分。
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: F401  (把 src/ 放进 sys.path)

from xtcli import cli

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
DOCS = REPO / "docs"
USER_MANUAL = DOCS / "使用手册.md"
MAINT_MANUAL = DOCS / "维护手册.md"

# 形如 `-X` 或 `--x-y` 的反引号选项
_OPTION_RE = re.compile(r"`(--?[A-Za-z][A-Za-z0-9-]*)`")


def _parser_options() -> set[str]:
    return {
        option
        for action in cli.build_parser()._actions
        for option in getattr(action, "option_strings", [])
    }


def _section(text: str, heading: str) -> str:
    """取某个 '### x.y 标题' 到下一个同级标题之间的正文。"""
    start = text.index(heading)
    rest = text[start + len(heading):]
    match = re.search(r"\n### ", rest)
    return rest if match is None else rest[: match.start()]


class TestDocsExist(unittest.TestCase):
    def test_readme_and_manuals_exist(self):
        for path in (README, USER_MANUAL, MAINT_MANUAL):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"缺少文档: {path}")
                self.assertGreater(len(path.read_text(encoding="utf-8")), 2000)

    def test_readme_links_to_both_manuals(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("docs/使用手册.md", text)
        self.assertIn("docs/维护手册.md", text)

    def test_manuals_link_to_each_other(self):
        self.assertIn("维护手册.md", USER_MANUAL.read_text(encoding="utf-8"))
        self.assertIn("使用手册.md", MAINT_MANUAL.read_text(encoding="utf-8"))


class TestOptionTableMatchesParser(unittest.TestCase):
    """使用手册的选项表 == 解析器选项集合（防止"文档里有个不存在的选项"）。"""

    def _documented(self) -> set[str]:
        body = _section(USER_MANUAL.read_text(encoding="utf-8"), "### 4.2 选项")
        documented: set[str] = set()
        for line in body.splitlines():
            if not line.startswith("|"):
                continue
            cell = line.split("|")[1]
            documented.update(_OPTION_RE.findall(cell))
        return documented

    def test_sets_are_identical(self):
        documented = self._documented()
        actual = _parser_options()
        missing_in_doc = sorted(actual - documented)
        extra_in_doc = sorted(documented - actual)
        self.assertEqual(
            missing_in_doc, [], f"解析器里有、文档里没写的选项: {missing_in_doc}"
        )
        self.assertEqual(
            extra_in_doc, [], f"文档里写了、解析器里没有的选项: {extra_in_doc}"
        )

    def test_counts_match(self):
        self.assertEqual(len(self._documented()), len(_parser_options()))


class TestVerbsAreDocumented(unittest.TestCase):
    def test_every_verb_appears_in_both_manuals(self):
        for path in (USER_MANUAL, MAINT_MANUAL):
            text = path.read_text(encoding="utf-8")
            for verb in cli.VERBS:
                with self.subTest(doc=path.name, verb=verb):
                    self.assertIn(verb, text)

    def test_help_text_mentions_all_verbs(self):
        """help 是用户的第一入口: 它必须列出全部动词。"""
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.show_help()
        help_text = buffer.getvalue()
        for verb in cli.VERBS:
            with self.subTest(verb=verb):
                self.assertIn(verb, help_text)


class TestManualLinksResolve(unittest.TestCase):
    _LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

    def test_local_links_point_to_existing_files(self):
        for path in (README, USER_MANUAL, MAINT_MANUAL):
            text = path.read_text(encoding="utf-8")
            for target in self._LINK_RE.findall(text):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                file_part = target.split("#", 1)[0]
                if not file_part:
                    continue
                with self.subTest(doc=path.name, link=target):
                    self.assertTrue(
                        (path.parent / file_part).is_file(),
                        f"{path.name} 里的链接指向不存在的文件: {target}",
                    )

    def test_anchors_exist_for_cross_document_links(self):
        """跨文档带锚点的链接: 锚点对应的小节标题必须存在。"""
        for path in (README, USER_MANUAL, MAINT_MANUAL):
            text = path.read_text(encoding="utf-8")
            for target in self._LINK_RE.findall(text):
                if "#" not in target or target.startswith(("http://", "https://")):
                    continue
                file_part, anchor = target.split("#", 1)
                if not file_part or not anchor:
                    continue
                target_path = path.parent / file_part
                with self.subTest(doc=path.name, link=target):
                    self.assertTrue(target_path.is_file(), f"链接文件不存在: {target}")
                    headings = self._anchors(target_path.read_text(encoding="utf-8"))
                    self.assertIn(anchor, headings, f"{target_path.name} 里没有锚点 #{anchor}")

    @staticmethod
    def _anchors(text: str) -> set[str]:
        """把 markdown 标题转成 GitHub 风格锚点（够用即可）。"""
        anchors: set[str] = set()
        for line in text.splitlines():
            if not line.startswith("#"):
                continue
            title = line.lstrip("#").strip()
            slug = re.sub(r"[^\w\u4e00-\u9fff -]", "", title).strip().lower()
            slug = slug.replace(" ", "-")
            anchors.add(slug)
            # 标题里带点的编号 (如 "4. 命令与选项总表") 会保留点号, 也接受去掉点的形式
            anchors.add(slug.replace(".", ""))
        return anchors


class TestManualCoversKeyTopics(unittest.TestCase):
    """手册必须覆盖的主题（缺了就等于没写）。"""

    TOPICS: ClassVar[dict[Path, list[str]]] = {
        USER_MANUAL: ["一分钟上手", "退出码", "故障排查", "工具会写哪些文件", "未验证"],
        MAINT_MANUAL: ["加一颗新芯片", "加一个新后端", "加一个新架构", "受管写入",
                       "测试体系", "发布前检查", "历史事故"],
    }

    def test_topics_present(self):
        for path, topics in self.TOPICS.items():
            text = path.read_text(encoding="utf-8")
            for topic in topics:
                with self.subTest(doc=path.name, topic=topic):
                    self.assertIn(topic, text)


if __name__ == "__main__":
    unittest.main()
