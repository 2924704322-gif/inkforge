"""文本分块器：EPUB（ebooklib）与 TXT 分块，支持按章/按字数两种策略。"""

from __future__ import annotations

import re
from pathlib import Path

from src.utils.logger import get_logger

logger = get_logger(__name__)

# 中文章节标题匹配：第X章/第X节/Chapter X/卷/部 等
_CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:第[零一二三四五六七八九十百千\d]+[章节回卷部]|Chapter\s*\d+|"
    r"#[#]?\s*(?:第[零一二三四五六七八九十百千\d]+[章节回卷部]|Chapter\s*\d+))",
    re.IGNORECASE,
)

# EPUB 章节分割点：一级/二级标题
_HEADING_SPLIT_RE = re.compile(r"^#{1,2}\s+", re.MULTILINE)


class Chunk:
    """单个文本块。"""

    def __init__(self, index: int, text: str, chapter_range: str = ""):
        self.index = index
        self.text = text
        self.chapter_range = chapter_range  # e.g. "1-3" or "4"

    def __repr__(self) -> str:
        return f"Chunk({self.index}, {self.chapter_range!r}, {len(self.text)} chars)"


def split_by_chapters(text: str) -> list[Chunk]:
    """按章节标题分割文本，每章一个 Chunk。

    识别 '第X章' / 'Chapter X' 等模式作为分割点。
    如果全文没有检测到章标题，整文作为一个 Chunk。
    """
    lines = text.split("\n")
    chunks: list[Chunk] = []
    current_lines: list[str] = []
    current_chapter = 1
    chapter_labels: list[str] = []

    for line in lines:
        if _CHAPTER_HEADING_RE.match(line):
            if current_lines:
                body = "\n".join(current_lines).strip()
                if body:
                    label = f"第{current_chapter}章" if not chapter_labels else chapter_labels[0]
                    chunks.append(Chunk(len(chunks) + 1, body, label))
                    chapter_labels = []
                current_lines = []
                current_chapter += 1
            chapter_labels.append(line.strip())
        current_lines.append(line)

    # 最后一个块
    if current_lines:
        body = "\n".join(current_lines).strip()
        if body:
            label = chapter_labels[0] if chapter_labels else f"第{current_chapter}章"
            chunks.append(Chunk(len(chunks) + 1, body, label))

    return chunks


def split_by_words(text: str, chunk_size: int = 20000) -> list[Chunk]:
    """按字数分块，尽量在段边界断开。

    chunk_size: 每块目标字数（中文字符数），实际在段边界断开，偏差 ±20%。
    """
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[Chunk] = []
    current: list[str] = []
    current_len = 0
    start_chunk = 1

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        para_len = len(para)
        if current_len + para_len > chunk_size and current:
            chunks.append(Chunk(
                len(chunks) + 1,
                "\n\n".join(current),
                f"块{start_chunk}-{start_chunk + len(current) - 1}" if len(current) > 1 else f"块{start_chunk}",
            ))
            current = []
            current_len = 0
            start_chunk = chunks[-1].index + 1
        current.append(para)
        current_len += para_len

    if current:
        chunks.append(Chunk(
            len(chunks) + 1,
            "\n\n".join(current),
            f"块{start_chunk}-{start_chunk + len(current) - 1}" if len(current) > 1 else f"块{start_chunk}",
        ))

    return chunks


def load_epub(file_path: Path) -> str:
    """读取 EPUB 文件，提取纯文本并保留章节结构。

    依赖 ebooklib（需已在 requirements.txt 中；轻量可选依赖，
    未安装时抛出明确提示）。
    """
    try:
        import ebooklib
        from ebooklib import epub
    except ImportError:
        raise ImportError(
            "EPUB 解析需要 ebooklib 库，请执行: pip install ebooklib"
        )

    book = epub.read_epub(str(file_path))
    parts: list[str] = []

    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        content = item.get_body_content().decode("utf-8", errors="replace")
        # 去除 HTML 标签，保留文本
        text = re.sub(r"<[^>]+>", "", content)
        text = re.sub(r"\n{3,}", "\n\n", text)
        if text.strip():
            parts.append(text.strip())

    return "\n\n".join(parts)


def load_txt(file_path: Path) -> str:
    """读取 TXT 文件，尝试常见编码。"""
    for enc in ("utf-8", "gbk", "gb2312", "utf-16"):
        try:
            return file_path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"无法识别文件编码: {file_path}")


def chunk_file(
    file_path: Path,
    strategy: str = "BY_CHAPTERS",
    chunk_size: int = 20000,
) -> tuple[list[Chunk], str]:
    """便捷入口：自动判断格式 → 分块。

    返回 (chunks, book_title)。
    book_title 从文件名推断（去扩展名）。
    """
    suffix = file_path.suffix.lower()
    if suffix == ".epub":
        text = load_epub(file_path)
    elif suffix in (".txt", ".text", ".md"):
        text = load_txt(file_path)
    else:
        raise ValueError(f"不支持的文件格式: {suffix}（支持 .epub / .txt / .md）")

    title = file_path.stem

    if strategy.upper() == "BY_CHAPTERS":
        chunks = split_by_chapters(text)
    elif strategy.upper() == "BY_WORDS":
        chunks = split_by_words(text, chunk_size=chunk_size)
    else:
        raise ValueError(f"未知分块策略: {strategy}（可选 BY_CHAPTERS / BY_WORDS）")

    if not chunks:
        raise ValueError("文件未提取到有效文本内容")

    logger.info(
        "分块完成：%s，策略=%s，共 %d 块，总字数 %d",
        file_path.name, strategy, len(chunks),
        sum(len(c.text) for c in chunks),
    )
    return chunks, title
