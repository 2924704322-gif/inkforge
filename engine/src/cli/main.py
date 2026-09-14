"""CLI 入口：项目创建 / 断点续跑 / 大纲确认 / 逐章审阅 / 探活 / 索引重建 / 进度指标。

用法示例：
  python -m src.cli.main probe
  python -m src.cli.main create my-novel --brief "东方玄幻，废柴逆袭" --chapters 10
  python -m src.cli.main create my-novel --brief "..." --chapters 30 --parallel
  python -m src.cli.main resume my-novel
  python -m src.cli.main status my-novel
  python -m src.cli.main rebuild-index my-novel
  python -m src.cli.main skills my-novel
  python -m src.cli.main skill-run my-novel image_gen --args '{"name": "林尘"}'
"""

from __future__ import annotations

import typer
from langgraph.types import Command
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from src.config.settings import get_settings, load_models_config
from src.utils.logger import setup_logging

app = typer.Typer(help="多 Agent 小说生成系统 v1.0（M1 CLI）", no_args_is_help=True)
console = Console()


def _thread_config(novel_id: str) -> dict:
    return {"configurable": {"thread_id": novel_id}, "recursion_limit": 500}


# ---------- 人审交互 ----------

def _handle_outline_review(payload: dict) -> dict:
    outline = payload["outline"]
    console.rule("[bold cyan]大纲人审")
    console.print(f"[bold]《{outline.get('book_title', '')}》[/bold]  主题：{outline.get('theme', '')}")
    table = Table(title="卷章结构", show_lines=False)
    table.add_column("章", justify="right", width=4)
    table.add_column("标题", width=20)
    table.add_column("大纲")
    for vol in outline.get("volumes", []):
        table.add_row(
            f"[bold cyan]卷{vol['volume']}[/bold cyan]", f"[bold]{vol['title']}[/bold]", ""
        )
        for ch in vol.get("chapters", []):
            table.add_row(str(ch["chapter"]), ch["title"], ch["outline"])
    console.print(table)
    fs = outline.get("foreshadowing", [])
    if fs:
        console.print("[bold]伏笔表：[/bold]")
        for f in fs:
            console.print(
                f"  [{f['id']}] {f['desc']}（第{f['planted_ch']}章埋设 → 第{f['resolve_ch']}章回收）"
            )
    choice = Prompt.ask(
        "\n大纲审阅", choices=["approve", "reject"], default="approve", console=console
    )
    if choice == "approve":
        return {"action": "approve"}
    feedback = Prompt.ask("请输入打回修改意见", console=console)
    return {"action": "reject", "feedback": feedback}


def _handle_chapter_review(payload: dict) -> dict:
    ch = payload["chapter"]
    review = payload.get("review", {})
    console.rule(f"[bold cyan]第 {ch} 章人审（第 {payload.get('attempt', 1)} 稿）")
    model = payload.get("model")
    if model:
        if payload.get("used_fallback"):
            console.print(f"[yellow]🧩 生成模型：{model}（⚠ 已降级到备用接入点）[/yellow]")
        else:
            console.print(f"[dim]🧩 生成模型：{model}[/dim]")
    if payload.get("retry_exceeded"):
        console.print("[bold red]⚠ 自动重试已超限，本稿携带未解决问题送审[/bold red]")
    if review:
        overall = round(
            (review["consistency"] + review["plot"] + review["continuity"] + review["prose"]) / 4,
            2,
        )
        table = Table(title=f"Editor 评分（总分 {overall}）")
        table.add_column("设定一致性")
        table.add_column("大纲符合度")
        table.add_column("衔接连贯性")
        table.add_column("文笔质量")
        table.add_row(
            str(review["consistency"]),
            str(review["plot"]),
            str(review["continuity"]),
            str(review["prose"]),
        )
        console.print(table)
        if review.get("comment"):
            console.print(f"[italic]Editor 总评：{review['comment']}[/italic]")
        for i, issue in enumerate(review.get("issues", []), 1):
            console.print(
                f"  {i}. [{issue['severity']}/{issue['dimension']}] {issue['description']}"
            )
    text = payload["draft_text"]
    console.print(
        Panel(text[:800] + ("\n...（预览截断）" if len(text) > 800 else ""), title="正文预览")
    )
    while True:
        choice = Prompt.ask(
            "章节审阅 [green]approve[/green]=通过 / [red]reject[/red]=打回 / view=看全文",
            choices=["approve", "reject", "view"],
            default="approve",
            console=console,
        )
        if choice == "view":
            console.print(Markdown(text))
            continue
        if choice == "approve":
            return {"action": "approve"}
        feedback = Prompt.ask("请输入打回修改意见", console=console)
        mode = Prompt.ask(
            "修订模式 targeted=定向修订（仅改指定处）/ rewrite=整体重写",
            choices=["targeted", "rewrite"], default="targeted", console=console,
        )
        return {"action": "reject", "feedback": feedback, "revision_mode": mode}


def _run_until_done(graph, initial_input, config: dict) -> None:
    """驱动图执行：遇 interrupt 转人工交互，直至完成或用户中止。"""
    result = graph.invoke(initial_input, config)
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        if payload.get("type") == "outline_review":
            decision = _handle_outline_review(payload)
        elif payload.get("type") == "chapter_review":
            decision = _handle_chapter_review(payload)
        else:
            raise RuntimeError(f"未知 interrupt 类型: {payload}")
        result = graph.invoke(Command(resume=decision), config)
    console.rule("[bold green]全部章节已完成")


def _handle_wave_report(wave_no: int, volumes: list[int], review) -> None:
    """展示波次跨卷一致性审查结果（R2）。"""
    tone = "green" if review.consistent else "red"
    verdict = "一致" if review.consistent else "存在冲突"
    console.rule(
        f"[bold {tone}]波次 {wave_no} 跨卷一致性审查（卷 {volumes}）：{verdict}"
    )
    if review.comment:
        console.print(f"[italic]{review.comment}[/italic]")
    for i, issue in enumerate(review.issues, 1):
        console.print(
            f"  {i}. [{issue.severity}] 卷{issue.volumes}: {issue.description}"
            f"\n     建议：{issue.suggestion}"
        )
    if not review.consistent:
        console.print(
            "[yellow]报告已落盘 reviews/，请依据建议人工修订相关章节后重跑。[/yellow]"
        )


def _run_parallel_flow(novel_id: str, brief: str, chapters: int, words: int,
                       workers: int) -> None:
    """卷级并行执行链路（T2.2）：探活 → run_parallel（内含大纲/章节人审回调）。"""
    from src.orchestrator.bootstrap import build_pipeline
    from src.orchestrator.parallel_runner import run_parallel

    pipe = build_pipeline(novel_id, target_words=words)
    console.print("[bold]启动探活 ...[/bold]")
    pipe.registry.probe_all()
    console.print("[green]探活通过[/green]，进入卷级并行模式\n")
    result = run_parallel(
        pipe,
        novel_id=novel_id,
        brief=brief,
        total_chapters=chapters,
        outline_cb=_handle_outline_review,
        review_cb=_handle_chapter_review,
        max_workers=workers,
        wave_report_cb=_handle_wave_report,
    )
    console.rule(
        f"[bold green]卷级并行完成：{result['chapters_done']}/{chapters} 章，"
        f"共 {result['waves']} 个波次"
    )


# ---------- 命令 ----------

@app.command()
def probe():
    """启动探活：验证 models.yaml 中所有角色绑定的接入点连通性。"""
    setup_logging(get_settings().log_level)
    from src.llm.registry import ModelRegistry

    registry = ModelRegistry(load_models_config())
    bindings = registry.probe_all()
    table = Table(title="模型接入探活结果")
    table.add_column("角色")
    table.add_column("接入点/模型")
    for role, binding in bindings.items():
        table.add_row(role, f"[green]{binding} ✓[/green]")
    console.print(table)


@app.command()
def create(
    novel_id: str = typer.Argument(help="小说项目 ID（将作为数据目录名）"),
    brief: str = typer.Option(..., help="创作需求：题材/基调/主角设定等"),
    chapters: int = typer.Option(10, help="总章节数（M1 目标 10 章）"),
    words: int = typer.Option(3000, help="单章目标字数"),
    parallel: bool = typer.Option(
        False, "--parallel", help="卷级并行模式：独立卷按波次并行起草（T2.2）"
    ),
    workers: int = typer.Option(0, help="并行线程数上限，0=按波次卷数自适应"),
):
    """创建小说项目并启动生成流程（大纲 → 逐章生成+人审）。"""
    if parallel:
        _run_parallel_flow(novel_id, brief, chapters, words, workers)
        return
    from src.orchestrator.bootstrap import build_app

    pipe, graph = build_app(novel_id, target_words=words)
    console.print(f"[bold]启动探活 ...[/bold]")
    pipe.registry.probe_all()
    console.print("[green]探活通过[/green]，开始生成\n")
    initial = {
        "novel_id": novel_id,
        "brief": brief,
        "total_chapters": chapters,
        "target_words": words,
    }
    _run_until_done(graph, initial, _thread_config(novel_id))


@app.command()
def resume(
    novel_id: str = typer.Argument(help="小说项目 ID"),
    words: int = typer.Option(3000, help="单章目标字数"),
    parallel: bool = typer.Option(
        False, "--parallel", help="以卷级并行模式续跑（以 MD 事实源断点自证，需已有大纲）"
    ),
    chapters: int = typer.Option(0, help="并行续跑用总章节数，0=按大纲章节数推断"),
    workers: int = typer.Option(0, help="并行线程数上限，0=按波次卷数自适应"),
):
    """从断点恢复（崩溃/中途退出后继续，含待人审状态）。"""
    if parallel:
        from src.config.settings import get_settings as _gs
        from src.memory.store_factory import open_store
        from src.orchestrator.parallel_runner import load_outline

        store = open_store(_gs().novels_dir / novel_id, writable=False)
        outline = load_outline(store)
        if outline is None:
            console.print(
                f"[red]项目 [{novel_id}] 尚无大纲，无法并行续跑；"
                "请先 create --parallel 走完大纲人审。[/red]"
            )
            raise typer.Exit(1)
        total = chapters or sum(
            len(v.get("chapters", [])) for v in outline["volumes"]
        )
        _run_parallel_flow(novel_id, brief="", chapters=total, words=words,
                           workers=workers)
        return
    from src.orchestrator.bootstrap import build_app

    pipe, graph = build_app(novel_id, target_words=words)
    config = _thread_config(novel_id)
    snapshot = graph.get_state(config)
    if not snapshot.values:
        console.print(f"[red]没有找到项目 [{novel_id}] 的执行记录，请先 create[/red]")
        raise typer.Exit(1)
    if snapshot.values.get("done"):
        console.print(f"[green]项目 [{novel_id}] 已全部完成[/green]")
        return
    pipe.registry.probe_all()
    console.print(f"[bold]从断点恢复：第 {snapshot.values.get('current_chapter', '?')} 章[/bold]")
    _run_until_done(graph, None, config)


@app.command()
def status(novel_id: str = typer.Argument(help="小说项目 ID")):
    """进度与指标：已完成章节 / 伏笔回收率 / 一次通过率。"""
    setup_logging(get_settings().log_level)
    from src.memory.store_factory import open_store

    settings = get_settings()
    novel_dir = settings.novels_dir / novel_id
    if not novel_dir.exists():
        console.print(f"[red]项目不存在: {novel_dir}[/red]")
        raise typer.Exit(1)
    store = open_store(novel_dir, writable=False)

    chapters = store.list_chapters()
    approved = [c for c in chapters if c.metadata.get("status") == "approved"]
    first_pass = [c for c in approved if c.metadata.get("first_review_passed")]

    table = Table(title=f"项目 [{novel_id}] 进度")
    table.add_column("指标")
    table.add_column("数值")
    table.add_row("已生成章节", str(len(chapters)))
    table.add_row("已人审通过", str(len(approved)))
    if approved:
        rate = len(first_pass) / len(approved) * 100
        color = "green" if rate >= 70 else "red"
        table.add_row("一次通过率（目标≥70%）", f"[{color}]{rate:.0f}%[/{color}]")
        scores = [c.metadata.get("score") for c in approved if c.metadata.get("score")]
        if scores:
            table.add_row("Editor 平均分", f"{sum(scores) / len(scores):.2f}")
        fb_count = sum(1 for c in approved if c.metadata.get("used_fallback"))
        if fb_count:
            table.add_row("降级生成章节", f"[yellow]{fb_count} 章使用了备用接入点[/yellow]")

    # 伏笔回收率
    if store.exists("settings/foreshadowing.md"):
        items = store.read("settings/foreshadowing.md").metadata.get("items", []) or []
        total = len(items)
        resolved = sum(1 for i in items if i.get("status") == "resolved")
        if total:
            rate = resolved / total * 100
            color = "green" if rate >= 90 else "yellow"
            table.add_row(
                "伏笔回收率（完本目标≥90%）",
                f"[{color}]{resolved}/{total} = {rate:.0f}%[/{color}]",
            )
    console.print(table)


@app.command(name="rebuild-index")
def rebuild_index(novel_id: str = typer.Argument(help="小说项目 ID")):
    """由 MD 唯一事实源全量重建向量索引。"""
    from src.orchestrator.bootstrap import build_pipeline

    pipe = build_pipeline(novel_id)
    n = pipe.memory.rebuild_index()
    console.print(f"[green]索引重建完成：{n} 个向量块[/green]")


@app.command()
def schedule(novel_id: str = typer.Argument(help="小说项目 ID")):
    """展示卷级并行调度计划（依赖分层波次，T2.2）。"""
    from src.memory.store_factory import open_store
    from src.orchestrator.scheduler import schedule_all

    store = open_store(get_settings().novels_dir / novel_id, writable=False)
    if not store.exists("settings/outline.md"):
        console.print("[red]未找到大纲，请先运行 create 生成大纲。[/red]")
        raise typer.Exit(1)
    outline = {"volumes": store.read("settings/outline.md").metadata.get("volumes", [])}
    try:
        plan = schedule_all(outline)
    except ValueError as exc:
        console.print(f"[red]卷依赖非法：{exc}[/red]")
        raise typer.Exit(1)

    table = Table(title=f"《{novel_id}》卷级并行调度计划")
    table.add_column("波次", justify="right")
    table.add_column("可并行卷", justify="left")
    table.add_column("各卷章数", justify="left")
    for w in plan:
        vols = "、".join(f"卷{v}" for v in w["volumes"])
        chs = "  ".join(f"卷{v}:{n}章" for v, n in w["chapters"].items())
        table.add_row(str(w["wave"]), vols, chs)
    console.print(table)
    console.print(
        f"[green]共 {len(plan)} 个波次；同一波次内的卷可并行起草，"
        "卷内章节串行以保证连贯性。[/green]"
    )


@app.command()
def config(env: str = typer.Option("", "--env", help="dev/prod，留空取 APP_ENV 或默认 dev")):
    """加载并校验分层配置（base/dev/prod 继承 + Deep Merge + Fail-Fast，T3.1）。"""
    from src.config.app_config import load_app_config

    try:
        cfg = load_app_config(env or None)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]配置校验失败（Fail-Fast）：{exc}[/red]")
        raise typer.Exit(1)

    table = Table(title=f"应用配置 v{cfg.version} · 环境={cfg.environment}")
    table.add_column("配置项", justify="left")
    table.add_column("值", justify="left")
    table.add_row("version", cfg.version)
    table.add_row("environment", cfg.environment)
    table.add_row("log_level", cfg.log_level)
    table.add_row("vector_store.type", cfg.vector_store.type)
    table.add_row("vector_store.path", str(cfg.vector_store.path or "-"))
    table.add_row("retrieval.hybrid_top_k", str(cfg.retrieval.hybrid_top_k))
    table.add_row("retrieval.rrf_k", str(cfg.retrieval.rrf_k))
    table.add_row("retrieval.time_decay_lambda", str(cfg.retrieval.time_decay_lambda))
    table.add_row("retrieval.foreshadow_due_window", str(cfg.retrieval.foreshadow_due_window))
    table.add_row("generation.default_target_words", str(cfg.generation.default_target_words))
    console.print(table)
    console.print("[green]配置校验通过（Fail-Fast）。[/green]")


@app.command()
def skills(novel_id: str = typer.Argument(help="小说项目 ID")):
    """列出已注册 Skill 及其启用状态（T3.3 扩展框架）。"""
    from src.orchestrator.bootstrap import build_pipeline
    from src.skills.registry import registered_names

    pipe = build_pipeline(novel_id)
    active = set(pipe.skills.active_names())
    table = Table(title="Skill 扩展")
    table.add_column("名称")
    table.add_column("状态")
    for name in registered_names():
        status = "[green]已启用[/green]" if name in active else "[dim]未启用[/dim]"
        table.add_row(name, status)
    console.print(table)
    if not active:
        console.print(
            "[dim]在 configs/<env>.yaml 的 skills 段设置 enabled: true 可启用。[/dim]"
        )


@app.command(name="skill-run")
def skill_run(
    novel_id: str = typer.Argument(help="小说项目 ID"),
    name: str = typer.Argument(help="Skill 名称（见 skills 命令）"),
    args: str = typer.Option("{}", help='Skill 参数，JSON 字符串，如 \'{"name": "林尘"}\''),
):
    """手动执行指定 Skill（如 image_gen 为角色生成立绘）。"""
    import json

    from src.orchestrator.bootstrap import build_pipeline

    try:
        kwargs = json.loads(args)
    except json.JSONDecodeError as exc:
        console.print(f"[red]--args 不是合法 JSON：{exc}[/red]")
        raise typer.Exit(1)
    pipe = build_pipeline(novel_id)
    try:
        result = pipe.skills.dispatch(name, **kwargs)
    except KeyError as exc:
        console.print(f"[red]{exc.args[0]}[/red]")
        raise typer.Exit(1)
    console.print_json(data=result)


# ────────── 蒸馏命令组（NDS: Novel Distillation System）──────────

distill_app = typer.Typer(help="小说蒸馏系统：16维结构化分析 + 技能包管理", no_args_is_help=True)
app.add_typer(distill_app, name="distill")


@distill_app.command(name="init")
def distill_init(
    file_path: str = typer.Argument(help="待蒸馏的书籍文件路径（.epub / .txt）"),
    skill_id: str = typer.Option("", help="技能包 ID，缺省由书名+版本号自动生成"),
    version: str = typer.Option("1.0.0", help="版本号"),
    strategy: str = typer.Option("BY_CHAPTERS", help="分块策略：BY_CHAPTERS / BY_WORDS"),
    chunk_size: int = typer.Option(20000, help="BY_WORDS 策略每块字数"),
):
    """上传书籍文件，分块并创建技能包目录。"""
    setup_logging(get_settings().log_level)
    from src.skills.distill import DistillSettings, DistillSkill

    skill = DistillSkill(
        DistillSettings(enabled=True, chunk_strategy=strategy, chunk_size=chunk_size),
        None,
    )
    try:
        result = skill.run(action="init", file_path=file_path,
                          skill_id=skill_id, version=version)
        console.print(f"[green]技能包初始化完成[/green]")
        console.print_json(data=result)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


@distill_app.command(name="run")
def distill_run(
    skill_id: str = typer.Argument(help="技能包 ID"),
    background: bool = typer.Option(False, "--background", "-b", help="后台运行（网页端模式，非阻塞）"),
):
    """执行蒸馏（默认同步阻塞，实时显示进度）。"""
    setup_logging(get_settings().log_level)
    from src.config.settings import load_models_config
    from src.llm.registry import ModelRegistry
    from src.skills.distill import DistillSettings, DistillSkill

    registry = ModelRegistry(load_models_config())
    console.print("[bold]蒸馏探活 ...[/bold]")
    try:
        registry.probe_all()
    except Exception as exc:
        console.print(f"[red]探活失败: {exc}[/red]")
        raise typer.Exit(1)
    console.print("[green]探活通过[/green]")

    class _Ctx:
        def __init__(self):
            self.novel_id = ""
            self.store = None
            self.memory = None
            self.registry = registry
    ctx = _Ctx()

    skill = DistillSkill(DistillSettings(enabled=True), ctx)

    if background:
        # 后台模式：启动线程即返回（网页端 ReviewSession 模式）
        result = skill.run(action="start", skill_id=skill_id)
        console.print(f"[green]蒸馏已启动: {skill_id}[/green]")
        console.print("[dim]使用 'distill status' 查看进度[/dim]")
        return

    # 同步模式：阻塞执行，实时显示进度（与 create/resume 命令一致）
    from src.distillation.chunker import Chunk
    from src.distillation.graph import DistillPipeline, run_distill
    from src.distillation.skill_store import checkpoint_db_for, load_chunks, load_manifest, save_report

    try:
        manifest = load_manifest(skill_id)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    all_chunks = load_chunks(skill_id)
    chunks = [Chunk(c["index"], c["text"], c.get("chapter_range", "")) for c in all_chunks]
    total = len(chunks)

    pipe = DistillPipeline(registry=registry)
    checkpoint_db = checkpoint_db_for(skill_id)

    # 检查是否有 checkpoint 可以续跑
    import sqlite3
    from langgraph.types import Command

    graph_config = {"configurable": {"thread_id": skill_id}, "recursion_limit": 500}
    from src.distillation.graph import build_distill_graph
    graph = build_distill_graph(pipe, checkpoint_db)

    state = graph.get_state(graph_config)
    start_from = 1
    if state.values and state.values.get("current_chunk_index"):
        start_from = state.values["current_chunk_index"]
        if start_from > 1:
            console.print(f"[dim]从 checkpoint 恢复：已完成 {start_from - 1}/{total} 块[/dim]")

    with console.status(f"[bold green]蒸馏中... 0/{total} 块[/bold green]") as status:
        # 如果是从头开始，先注入初始 state
        if not state.values:
            import json as _json
            initial_state = {
                "skill_id": skill_id,
                "book_title": manifest["book_title"],
                "total_chunks": total,
                "current_chunk_index": 1,
                "chunks_json": _json.dumps([{"index": c.index, "text": c.text, "chapter_range": c.chapter_range} for c in chunks], ensure_ascii=False),
                "cumulative_json": "{}",
                "last_extraction_json": "{}",
                "done": False,
                "provider": "",
                "model": "",
            }
            result = graph.invoke(initial_state, graph_config)
        else:
            # 从断点续跑
            result = graph.invoke(None, graph_config)

        # 如果图有 interrupt，逐块推进并显示进度
        # 对于蒸馏图（无 interrupt 节点），invoke 会一次跑完
        idx = result.get("current_chunk_index", total)
        while "__interrupt__" in result:
            idx = result.get("current_chunk_index", total)
            status.update(f"[bold green]蒸馏中... {idx}/{total} 块[/bold green]")
            result = graph.invoke(Command(resume=True), graph_config)

        idx = result.get("current_chunk_index", total)
        status.update(f"[bold green]蒸馏中... {idx}/{total} 块[/bold green]")

    if result.get("done"):
        from src.distillation.schemas import FullReport
        report = FullReport.model_validate_json(result["cumulative_json"])
        save_report(skill_id, report)
        console.print(f"[green]蒸馏完成！《{manifest['book_title']}》{total} 块 16 维分析已生成[/green]")
    else:
        console.print(f"[yellow]蒸馏未完成 (块 {result.get('current_chunk_index', '?')}/{total})，可用 distill run 续跑[/yellow]")


@distill_app.command(name="status")
def distill_status(
    skill_id: str = typer.Argument(help="技能包 ID"),
):
    """查询蒸馏进度。"""
    setup_logging(get_settings().log_level)
    from src.skills.distill import DistillSettings, DistillSkill

    skill = DistillSkill(DistillSettings(enabled=True), None)
    try:
        result = skill.run(action="status", skill_id=skill_id)
        table = Table(title=f"蒸馏进度: {skill_id}")
        table.add_column("字段")
        table.add_column("值")
        for k, v in result.items():
            table.add_row(k, str(v))
        console.print(table)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


@distill_app.command(name="report")
def distill_report(
    skill_id: str = typer.Argument(help="技能包 ID"),
):
    """获取完整 16 维蒸馏报告（JSON）。"""
    setup_logging(get_settings().log_level)
    from src.skills.distill import DistillSettings, DistillSkill

    skill = DistillSkill(DistillSettings(enabled=True), None)
    try:
        result = skill.run(action="report", skill_id=skill_id)
        console.print_json(data=result["manifest"])
        console.print(f"\n[dim]完整报告已省略 JSON 输出，请用浏览器查看或读取 {skill_id}/full_report.md[/dim]")
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


@distill_app.command(name="list")
def distill_list():
    """列出所有已蒸馏的技能包。"""
    setup_logging(get_settings().log_level)
    from src.skills.distill import DistillSettings, DistillSkill

    skill = DistillSkill(DistillSettings(enabled=True), None)
    result = skill.run(action="list")
    skills = result.get("skills", [])
    if not skills:
        console.print("[dim]暂无技能包。使用 'distill init' 创建。[/dim]")
        return
    table = Table(title="技能包列表")
    table.add_column("skill_id")
    table.add_column("书名")
    table.add_column("版本")
    table.add_column("状态")
    table.add_column("创建日期")
    for s in skills:
        table.add_row(
            s.get("skill_id", ""),
            s.get("book_title", ""),
            s.get("version", ""),
            s.get("status", ""),
            s.get("created_at", ""),
        )
    console.print(table)


@distill_app.command(name="reprocess")
def distill_reprocess(
    skill_id: str = typer.Argument(help="技能包 ID"),
    mode: str = typer.Option("INCREMENTAL", help="INCREMENTAL / DEEP_UPGRADE / FULL"),
    new_file_path: str = typer.Option("", help="INCREMENTAL 模式的追加文件路径"),
    new_version: str = typer.Option("", help="新版本号，缺省自动推算"),
):
    """重新蒸馏（版本更新）。"""
    setup_logging(get_settings().log_level)
    from src.skills.distill import DistillSettings, DistillSkill

    skill = DistillSkill(DistillSettings(enabled=True), None)
    try:
        result = skill.run(
            action="reprocess",
            skill_id=skill_id,
            mode=mode,
            new_file_path=new_file_path,
            new_version=new_version,
        )
        console.print(f"[green]新版本技能包已创建: {result['new_skill_id']}[/green]")
        console.print(f"  版本: {result['version']}  模式: {result['mode']}")
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


@distill_app.command(name="export")
def distill_export(
    skill_id: str = typer.Argument(help="技能包 ID"),
):
    """导出技能包为 .zip。"""
    setup_logging(get_settings().log_level)
    from src.skills.distill import DistillSettings, DistillSkill

    skill = DistillSkill(DistillSettings(enabled=True), None)
    try:
        result = skill.run(action="export", skill_id=skill_id)
        console.print(f"[green]已导出: {result['path']}[/green]")
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


@distill_app.command(name="delete")
def distill_delete(
    skill_id: str = typer.Argument(help="技能包 ID"),
):
    """删除指定技能包。"""
    setup_logging(get_settings().log_level)
    from src.skills.distill import DistillSettings, DistillSkill

    skill = DistillSkill(DistillSettings(enabled=True), None)
    try:
        result = skill.run(action="delete", skill_id=skill_id)
        console.print(f"[green]{result['message']}[/green]")
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
