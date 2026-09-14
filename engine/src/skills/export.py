"""导出 Skill（C 阶段扩展）：将已定稿章节合并导出为 TXT / EPUB。

作为 Skill 扩展框架（T3.3）的零依赖示范：
- 章节来源：chapters/ 下 status=approved 的 MD（可配置纳入未定稿）。
- 顺序：按 (volume, chapter) frontmatter 升序。
- 书名：取 settings/outline.md 的 title，缺失回退 novel_id。
- TXT：书名 + 逐章标题 + 正文（剥离正文首个 Markdown 标题避免重复）。
- EPUB：仅用标准库 zipfile 构造合法 EPUB 2.0.1（mimetype/container/opf/ncx/xhtml），
  无第三方依赖。
- EPUB 增强（v2.0 P4-C）：封面（settings/cover.jpg|.png 自动探测或显式指定）、
  多卷两级 ncx 目录（卷名取大纲 frontmatter volumes 标题）、dc:creator/dc:date 元数据。
"""

from __future__ import annotations

import html
import re
import uuid
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, ClassVar, Optional

from src.skills.base import Skill, SkillContext, SkillSettings
from src.skills.registry import register
from src.utils.logger import get_logger

logger = get_logger(__name__)

_LEADING_HEADING_RE = re.compile(r"^\s*#{1,6}\s+.*(?:\n|$)")


class ExportSettings(SkillSettings):
    enabled: bool = True                 # 纯本地零成本，基线默认启用
    format: str = "txt"                  # 默认导出格式：txt / epub
    output_dir: Optional[str] = None     # 缺省为小说根目录下 exports/
    include_unapproved: bool = False     # 是否纳入未定稿章节
    cover_path: Optional[str] = None     # 封面图路径；缺省自动探测 settings/cover.jpg|.png
    auto_export_on_finale: bool = False  # 订阅记忆总线：末章定稿时自动全书导出


@register
class ExportSkill(Skill):
    name: ClassVar[str] = "export"
    SettingsModel: ClassVar[type[SkillSettings]] = ExportSettings

    # ---------- 数据收集 ----------

    def _novel_title(self) -> str:
        store = self.context.store
        if store.exists("settings/outline.md"):
            try:
                meta = store.read("settings/outline.md").metadata
                if meta.get("title"):
                    return str(meta["title"])
            except Exception:  # noqa: BLE001 - 大纲损坏时回退 id
                pass
        return self.context.novel_id

    def _outline_meta(self) -> dict:
        """读取大纲 frontmatter（author / volumes 卷名等），损坏或缺失返回空。"""
        store = self.context.store
        if store.exists("settings/outline.md"):
            try:
                return dict(store.read("settings/outline.md").metadata or {})
            except Exception:  # noqa: BLE001 - 大纲损坏时按无元数据处理
                pass
        return {}

    def _volume_titles(self) -> dict[int, str]:
        """卷号 -> 卷名映射（取大纲 volumes 数组的标题字段）。"""
        titles: dict[int, str] = {}
        for vol in self._outline_meta().get("volumes") or []:
            try:
                vid = int(vol.get("volume", 0) or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            name = str(vol.get("title", "") or "").strip()
            if vid and name:
                titles[vid] = name
        return titles

    def _find_cover(self) -> Optional[Path]:
        """封面探测：显式 cover_path 优先，否则 settings/cover.jpg|.png；无则 None。"""
        if self.settings.cover_path:
            p = Path(self.settings.cover_path)
            if not p.is_absolute():
                p = self.context.store.root / p
            return p if p.is_file() else None
        for name in ("cover.jpg", "cover.jpeg", "cover.png"):
            p = self.context.store.root / "settings" / name
            if p.is_file():
                return p
        return None

    def _collect_chapters(self, include_unapproved: bool) -> list[dict]:
        chapters: list[dict] = []
        for doc in self.context.store.list_chapters():
            meta = doc.metadata
            status = str(meta.get("status", ""))
            if not include_unapproved and status != "approved":
                continue
            chapters.append(
                {
                    "volume": int(meta.get("volume", 0) or 0),
                    "chapter": int(meta.get("chapter", 0) or 0),
                    "title": str(meta.get("title", "")).strip(),
                    "content": doc.content or "",
                }
            )
        chapters.sort(key=lambda c: (c["volume"], c["chapter"]))
        return chapters

    @staticmethod
    def _body(content: str) -> str:
        """剥离正文开头的 Markdown 标题（章节标题另行渲染，避免重复）。"""
        return _LEADING_HEADING_RE.sub("", content, count=1).strip()

    @staticmethod
    def _chapter_heading(ch: dict) -> str:
        title = ch["title"]
        return f"第{ch['chapter']}章 {title}" if title else f"第{ch['chapter']}章"

    # ---------- TXT ----------

    def _build_txt(self, title: str, chapters: list[dict]) -> str:
        parts = [title, "=" * 20, ""]
        for ch in chapters:
            parts.append(self._chapter_heading(ch))
            parts.append("")
            parts.append(self._body(ch["content"]))
            parts.append("\n")
        return "\n".join(parts).rstrip() + "\n"

    # ---------- EPUB ----------

    def _chapter_xhtml(self, ch: dict) -> str:
        heading = html.escape(self._chapter_heading(ch))
        paras = [
            f"<p>{html.escape(p.strip())}</p>"
            for p in re.split(r"\n\s*\n", self._body(ch["content"]))
            if p.strip()
        ]
        body = "\n".join(paras)
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n'
            f"<head><title>{heading}</title></head>\n"
            f"<body>\n<h2>{heading}</h2>\n{body}\n</body>\n</html>\n"
        )

    def _build_epub(self, path: Path, title: str, chapters: list[dict]) -> None:
        book_id = f"urn:uuid:{uuid.uuid4()}"
        safe_title = html.escape(title)
        files = [(f"chap{i:03d}.xhtml", ch) for i, ch in enumerate(chapters, 1)]

        # ---- 封面（可选）：图片 + cover.xhtml 首页，缺失则整体跳过 ----
        cover = self._find_cover()
        cover_manifest = cover_spine = cover_meta = ""
        cover_img_name = cover_media = ""
        if cover is not None:
            ext = cover.suffix.lower()
            cover_media = "image/png" if ext == ".png" else "image/jpeg"
            cover_img_name = f"images/cover{'.png' if ext == '.png' else '.jpg'}"
            cover_meta = '    <meta name="cover" content="cover-image"/>\n'
            cover_manifest = (
                f'    <item id="cover-image" href="{cover_img_name}" '
                f'media-type="{cover_media}"/>\n'
                '    <item id="cover-page" href="cover.xhtml" '
                'media-type="application/xhtml+xml"/>\n'
            )
            cover_spine = '    <itemref idref="cover-page"/>\n'

        # ---- 元数据：dc:creator（大纲 frontmatter author，缺省省略）+ dc:date ----
        outline_meta = self._outline_meta()
        author = str(outline_meta.get("author", "") or "").strip()
        creator_tag = f"    <dc:creator>{html.escape(author)}</dc:creator>\n" if author else ""
        date_tag = f"    <dc:date>{date.today().isoformat()}</dc:date>\n"

        manifest = "\n".join(
            f'    <item id="c{i}" href="{fn}" media-type="application/xhtml+xml"/>'
            for i, (fn, _) in enumerate(files, 1)
        )
        spine = "\n".join(f'    <itemref idref="c{i}"/>' for i in range(1, len(files) + 1))
        opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
            'unique-identifier="BookId">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:opf="http://www.idpf.org/2007/opf">\n'
            f"    <dc:title>{safe_title}</dc:title>\n"
            "    <dc:language>zh</dc:language>\n"
            f'    <dc:identifier id="BookId">{book_id}</dc:identifier>\n'
            f"{creator_tag}"
            f"{date_tag}"
            f"{cover_meta}"
            "  </metadata>\n"
            '  <manifest>\n'
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
            f"{cover_manifest}"
            f"{manifest}\n"
            "  </manifest>\n"
            '  <spine toc="ncx">\n'
            f"{cover_spine}"
            f"{spine}\n"
            "  </spine>\n"
            "</package>\n"
        )

        navmap = self._build_navmap(files)
        ncx = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            f'  <head><meta name="dtb:uid" content="{book_id}"/></head>\n'
            f"  <docTitle><text>{safe_title}</text></docTitle>\n"
            "  <navMap>\n"
            f"{navmap}\n"
            "  </navMap>\n"
            "</ncx>\n"
        )

        container = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/>\n'
            "  </rootfiles>\n"
            "</container>\n"
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            # mimetype 必须是首个条目且不压缩存储
            zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
            zf.writestr("META-INF/container.xml", container)
            zf.writestr("OEBPS/content.opf", opf)
            zf.writestr("OEBPS/toc.ncx", ncx)
            if cover is not None:
                zf.writestr(f"OEBPS/{cover_img_name}", cover.read_bytes())
                zf.writestr("OEBPS/cover.xhtml",
                            self._cover_xhtml(safe_title, cover_img_name))
            for fn, ch in files:
                zf.writestr(f"OEBPS/{fn}", self._chapter_xhtml(ch))

    @staticmethod
    def _cover_xhtml(safe_title: str, img_name: str) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n'
            f"<head><title>{safe_title}</title></head>\n"
            '<body style="margin:0;text-align:center">\n'
            f'<img src="{img_name}" alt="{safe_title}" '
            'style="max-width:100%;max-height:100%"/>\n'
            "</body>\n</html>\n"
        )

    def _build_navmap(self, files: list[tuple[str, dict]]) -> str:
        """生成 ncx navMap：多卷两级（卷节点 → 章节点），单卷维持平铺。"""
        vol_ids = {ch["volume"] for _, ch in files}
        if len(vol_ids) <= 1:
            return "\n".join(
                f'    <navPoint id="n{i}" playOrder="{i}">\n'
                f"      <navLabel><text>{html.escape(self._chapter_heading(ch))}</text></navLabel>\n"
                f'      <content src="{fn}"/>\n'
                "    </navPoint>"
                for i, (fn, ch) in enumerate(files, 1)
            )

        vol_titles = self._volume_titles()
        # 按卷分组（files 已按 (volume, chapter) 升序）
        groups: list[tuple[int, list[tuple[str, dict]]]] = []
        for fn, ch in files:
            if not groups or groups[-1][0] != ch["volume"]:
                groups.append((ch["volume"], []))
            groups[-1][1].append((fn, ch))

        order = 0
        parts: list[str] = []
        for vid, items in groups:
            order += 1
            vol_label = html.escape(vol_titles.get(vid, f"第{vid}卷"))
            children: list[str] = []
            vol_order = order
            for fn, ch in items:
                order += 1
                children.append(
                    f'      <navPoint id="n{order}" playOrder="{order}">\n'
                    f"        <navLabel><text>{html.escape(self._chapter_heading(ch))}</text></navLabel>\n"
                    f'        <content src="{fn}"/>\n'
                    "      </navPoint>"
                )
            parts.append(
                f'    <navPoint id="v{vid}" playOrder="{vol_order}">\n'
                f"      <navLabel><text>{vol_label}</text></navLabel>\n"
                f'      <content src="{items[0][0]}"/>\n'
                + "\n".join(children) + "\n"
                "    </navPoint>"
            )
        return "\n".join(parts)

    # ---------- 主逻辑 ----------

    def run(
        self,
        format: Optional[str] = None,
        output_path: Optional[str] = None,
        include_unapproved: Optional[bool] = None,
        **kwargs: Any,
    ) -> dict:
        fmt = (format or self.settings.format).lower()
        if fmt not in ("txt", "epub"):
            raise ValueError(f"不支持的导出格式: {fmt}（可选 txt / epub）")
        inc = self.settings.include_unapproved if include_unapproved is None else include_unapproved

        chapters = self._collect_chapters(inc)
        if not chapters:
            raise ValueError("没有可导出的章节（是否全部未定稿？可设 include_unapproved=True）")

        title = self._novel_title()
        if output_path:
            out = Path(output_path)
        else:
            base = Path(self.settings.output_dir) if self.settings.output_dir else (
                self.context.store.root / "exports"
            )
            out = base / f"{self.context.novel_id}.{fmt}"

        words = sum(len(self._body(c["content"])) for c in chapters)
        if fmt == "txt":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(self._build_txt(title, chapters), encoding="utf-8", newline="\n")
        else:
            self._build_epub(out, title, chapters)

        logger.info("导出完成：%s（%d 章 / %d 字）", out, len(chapters), words)
        return {
            "format": fmt,
            "path": str(out),
            "title": title,
            "chapters": len(chapters),
            "words": words,
        }

    # ---------- 记忆总线钩子（可选完本自动导出） ----------

    def subscribed_events(self) -> list[str]:
        """仅当 auto_export_on_finale 开启时才订阅，否则零开销。"""
        from src.skills.memory_bus import EVENT_CHAPTER_COMMITTED
        return [EVENT_CHAPTER_COMMITTED] if self.settings.auto_export_on_finale else []

    def handle_event(self, event: str, payload: dict) -> None:
        """末章定稿时自动全书导出（异常由记忆总线隔离，不影响主流程）。"""
        from src.skills.memory_bus import EVENT_CHAPTER_COMMITTED
        if event != EVENT_CHAPTER_COMMITTED or not payload.get("is_finale"):
            return
        logger.info("记忆总线触发全书自动导出（末章定稿），格式=%s", self.settings.format)
        self.run()
