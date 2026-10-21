"""读取文字型PDF，保存带页码依据的知识页及目录。"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import markdown
from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from pypdf import PdfReader

from agent_runtime import ApplicationToolsOnly, SessionSupport, run_agent

ROOT = Path(__file__).resolve().parent
DEFAULT_LIBRARY = ROOT / "knowledge_base"
LIBRARY_LOCK = threading.RLock()
DEFAULT_TASK13 = (ROOT / "prompts/default_task.txt").read_text(encoding="utf-8")
SYSTEM13 = (ROOT / "prompts/system.md").read_text(encoding="utf-8")


def write_json(path: Path, value):
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def compact(s: str) -> str:
    return re.sub(r"\s+", "", s)


def load_catalog(library: Path) -> dict:
    path = library / "catalog.json"
    if not path.exists():
        return {"schema_version": 1, "pages": []}
    return json.loads(path.read_text(encoding="utf-8"))


def rebuild_index(library: Path):
    pages = load_catalog(library)["pages"]
    lines = ["# 工程资料知识库", "", "每份来源单独整理；相关资料通过链接关联。知识页由模型整理，目录由程序登记。", "",
             "|知识页|主题|简介|原PDF页数|", "|---|---|---|---|"]
    for p in pages:
        safe = lambda x: str(x).replace("|", "／").replace("\n", " ")
        lines.append(f"|[{safe(p['title'])}](pages/{p['page_id']}.md)|{safe(p['category'])}|{safe(p['summary'])}|{p['source_page_count']}|")
    lines += ["", "添加相同内容的PDF会返回已有页面，不重复调用模型建页。不同内容保留独立记录，不自动覆盖或合并。"]
    (library / "首页.md").write_text("\n".join(lines), encoding="utf-8")


class KnowledgeTools13(ApplicationToolsOnly):
    allowed = {"inspect_library", "read_library_page", "read_pdf", "save_knowledge_page"}


class KnowledgeSession13(SessionSupport):
    def __init__(self, pdf_path: str, library: Path):
        # The model configuration is local; no imports from the old lesson project.
        from dotenv import dotenv_values
        from langchain_deepseek import ChatDeepSeek
        import os
        import uuid
        self.source = Path(pdf_path).expanduser().resolve(strict=True)
        if not self.source.is_file() or self.source.suffix.lower() != ".pdf":
            raise ValueError("请填写一个存在的PDF文件路径。")
        if self.source.stat().st_size > 30 * 1024 * 1024:
            raise ValueError("当前演示单文件上限30MiB。")
        self.reader = PdfReader(self.source)
        if self.reader.is_encrypted or not 1 <= len(self.reader.pages) <= 12:
            raise ValueError("当前支持未加密的1至12页文字型PDF。")
        self.library = library
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.page_id = "K-" + self.source_hash[:16]
        self.read_pages, self.events, self.reports = {}, [], []
        self.delivered_pages = set()
        self.library_inspected = False
        self.known_pages = {}
        self.tool_lock = threading.RLock()
        config = dotenv_values(ROOT / ".env")
        if not config.get("DEEPSEEK_API_KEY"):
            raise RuntimeError("请在本机.env配置DeepSeek Key，不要把Key写入Notebook。")
        self.secrets = [v for k, v in config.items() if v and any(s in k for s in ("KEY", "TOKEN", "SECRET"))]
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        self.model_name = config.get("DEEPSEEK_MODEL") or "deepseek-flash"
        self.model = ChatDeepSeek(model=self.model_name, api_key=config["DEEPSEEK_API_KEY"], temperature=0,
            timeout=120, max_retries=0, max_tokens=10000, extra_body={"thinking": {"type": "disabled"}})
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self.run_dir = ROOT / "outputs" / ("knowledge_" + self.run_id)
        self.run_dir.mkdir(parents=True, exist_ok=False)
        (self.run_dir / "module_used.py").write_bytes(Path(__file__).read_bytes())
        (self.run_dir / "system_prompt.txt").write_text(SYSTEM13, encoding="utf-8")
        self.log("setup", source_pdf=str(self.source), source_sha256=self.source_hash,
                 source_pages=len(self.reader.pages), library=str(library), model=self.model_name)

    def log(self, kind, **values):
        with LIBRARY_LOCK:
            self.events.append({"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, **self.clean(values)})
            write_json(self.run_dir / "run.json", {"model": self.model_name, "source_pdf": str(self.source),
                "source_sha256": self.source_hash, "events": self.events, "reports": self.reports})

    def inspect_library(self, reason: str) -> str:
        """Read existing knowledge-page titles, summaries, categories and source identities. reason is a short public action purpose. No arbitrary filesystem access."""
        with LIBRARY_LOCK:
            catalog = load_catalog(self.library)
            self.known_pages = {p["page_id"]: p for p in catalog["pages"]}
            self.library_inspected = True
        result = {"status": "ok", "page_count": len(catalog["pages"]), "pages": catalog["pages"],
                  "notice": "这些是其他文件的独立资料页，不是本次PDF的事实依据。"}
        self.log("library_inspected", reason=reason, result=result)
        return json.dumps(result, ensure_ascii=False)

    def read_library_page(self, page_id: str, reason: str) -> str:
        """Read an existing page listed by inspect_library when deciding a useful link. It cannot supply facts attributed to the new PDF."""
        if page_id not in self.known_pages:
            return json.dumps({"status": "unknown_page", "reason": "先查看目录，并使用其page_id。"}, ensure_ascii=False)
        text = (self.library / "pages" / (page_id + ".md")).read_text(encoding="utf-8")
        self.log("library_page_read", page_id=page_id, reason=reason)
        return json.dumps({"page_id": page_id, "markdown": text}, ensure_ascii=False)

    def read_pdf(self, pages: list[int] | None = None) -> str:
        """Extract selected physical pages of the current PDF, returning both plain/layout text. Omit pages to read the complete PDF. No OCR. Empty pages remain explicit."""
        requested = list(range(1, len(self.reader.pages) + 1)) if pages is None else list(dict.fromkeys(pages))
        if not requested or any(type(p) is not int or not 1 <= p <= len(self.reader.pages) for p in requested):
            return json.dumps({"status": "invalid_pages", "page_count": len(self.reader.pages)})
        with self.tool_lock:
            if hashlib.sha256(self.source.read_bytes()).hexdigest() != self.source_hash:
                return json.dumps({"status": "source_changed"})
            extracted = []
            for p in requested:
                if p not in self.read_pages:
                    page = self.reader.pages[p - 1]
                    self.read_pages[p] = {"pdf_page": p, "plain_text": page.extract_text() or "",
                        "layout_text": page.extract_text(extraction_mode="layout") or ""}
                extracted.append(self.read_pages[p])
            result = {"status": "ok", "source_file": self.source.name, "pdf_page_count": len(self.reader.pages), "pages": extracted}
            if len(json.dumps(result, ensure_ascii=False)) > 60000:
                return json.dumps({"status": "response_too_large", "reason": "请每次选较少页面读取；本次未向模型返回文字。"}, ensure_ascii=False)
            self.delivered_pages.update(requested)
            self.log("pdf_read", returned_pages=requested, empty_pages=[p["pdf_page"] for p in extracted if not p["plain_text"].strip()])
            write_json(self.run_dir / "extracted_pages.json", {"source_pdf": str(self.source), "source_sha256": self.source_hash,
                "pdf_page_count": len(self.reader.pages), "pages": [self.read_pages[p] for p in sorted(self.read_pages)]})
            return json.dumps(result, ensure_ascii=False)

    def save_knowledge_page(self, title: str, summary: str, category: str, report_markdown: str,
                            used_pages: list[int], source_quotes: list[dict],
                            related_page_ids: list[str], related_reason: str) -> str:
        """Save a new independent Chinese knowledge page and register it in the index. source_quotes: 2-6 objects {pdf_page:int, quote:str}, copied from read_pdf text. related_page_ids must exist in inspect_library. Validation checks provenance, not all factual claims."""
        with self.tool_lock, LIBRARY_LOCK:
            errors = []
            if not self.library_inspected: errors.append("先查看知识库目录。")
            expected = set(range(1, len(self.reader.pages) + 1))
            if self.delivered_pages != expected: errors.append("请读完本次PDF全部页面再保存。")
            if any(not page["plain_text"].strip() for page in self.read_pages.values()):
                errors.append("存在没有提取到文字的页面，当前未接入OCR，不能声称完成整份知识页。")
            if hashlib.sha256(self.source.read_bytes()).hexdigest() != self.source_hash: errors.append("源文件已变化。")
            if len(report_markdown.strip()) < 300: errors.append("正文过短，请整理主要内容。")
            if not title.strip() or len(title) > 120 or len(summary) > 400 or len(category) > 60:
                errors.append("请提供简短标题、简介和主题。")
            if set(used_pages) != expected: errors.append("used_pages需覆盖整份输入文件。")
            cited = {int(p) for p in re.findall(r"PDF\s*第\s*(\d+)\s*页", report_markdown)}
            if not expected.issubset(cited) or not cited.issubset(expected): errors.append("正文页码不完整或超出原文件。")
            if not 2 <= len(source_quotes) <= 6: errors.append("请提供2至6段原文短引文。")
            for q in source_quotes:
                p, quote = q.get("pdf_page"), q.get("quote", "")
                if type(p) is not int or p not in self.read_pages or not isinstance(quote, str) or not 8 <= len(quote) <= 350:
                    errors.append("原文引文格式、长度或页码无效。")
                    continue
                page = self.read_pages[p]
                if not any(compact(quote) in compact(page[key]) for key in ("plain_text", "layout_text")):
                    errors.append(f"第{p}页引文未在提取原文找到：{quote[:65]}")
            if any(i not in self.known_pages for i in related_page_ids): errors.append("关联页不在已查看目录中。")
            if related_page_ids and not related_reason.strip(): errors.append("关联页面时说明关系。")
            attempt = self.run_dir / f"模型知识页尝试_{len(list(self.run_dir.glob('模型知识页尝试_*.json'))) + 1}.json"
            write_json(attempt, {"title": title, "summary": summary, "category": category, "report_markdown": report_markdown,
                "used_pages": used_pages, "source_quotes": source_quotes, "related_page_ids": related_page_ids,
                "related_reason": related_reason, "validation_errors": errors})
            if errors:
                self.log("save_rejected", errors=errors, attempt=str(attempt))
                return json.dumps({"status": "not_saved", "errors": errors}, ensure_ascii=False)
            catalog = load_catalog(self.library)
            existing = next((p for p in catalog["pages"] if p["source_sha256"] == self.source_hash), None)
            if existing: return json.dumps({"status": "duplicate", "page": existing}, ensure_ascii=False)
            source_copy = self.library / "sources" / (self.page_id + ".pdf")
            shutil.copy2(self.source, source_copy)
            prior = [p["page_id"] for p in catalog["pages"] if p["original_source_path"] == str(self.source)]
            meta = {"page_id": self.page_id, "title": title, "summary": summary, "category": category,
                "source_name": self.source.name, "original_source_path": str(self.source), "source_sha256": self.source_hash,
                "source_page_count": len(self.reader.pages), "created_at": datetime.now().isoformat(timespec="seconds"),
                "related_page_ids": list(dict.fromkeys(related_page_ids)), "related_reason": related_reason,
                "previous_same_path_pages": prior, "run_dir": str(self.run_dir), "model": self.model_name}
            text = f"# {title}\n\n> {summary}\n\n主题：{category}\n\n" + report_markdown.strip()
            text += "\n\n## 原文核对入口\n\n"
            for q in source_quotes: text += f"- [PDF第{q['pdf_page']}页] 原文：{q['quote']}\n"
            text += f"\n## 来源记录\n\n- 文件：{self.source.name}\n- [打开保存的原PDF](../sources/{self.page_id}.pdf)\n- 原PDF共{len(self.reader.pages)}页；本文引用均为PDF物理页码。\n"
            text += "\n## 相关页面\n\n"
            if related_page_ids:
                text += related_reason + "\n\n"
                for i in dict.fromkeys(related_page_ids): text += f"- [{self.known_pages[i]['title']}]({i}.md)\n"
            else: text += "本轮未建立相关页面链接。\n"
            if prior: text += "\n同一路径曾录入其他内容；旧页已保留，本轮未自动合并或判断版本差异。\n"
            text += "\n---\n本页由模型根据PDF文字层整理，图片及签章未识别；原文引文与页码检查不等于整篇准确性保证。\n"
            page_path = self.library / "pages" / (self.page_id + ".md")
            page_path.write_text(text, encoding="utf-8")
            write_json(page_path.with_suffix(".json"), meta)
            catalog["pages"].append(meta)
            write_json(self.library / "catalog.json", catalog)
            rebuild_index(self.library)
            report = {"markdown": str(page_path), "page_id": self.page_id, "source_pdf": str(source_copy),
                "used_pages": used_pages, "source_quotes": source_quotes, "model_body_attempt": str(attempt)}
            self.reports.append(report)
            self.log("knowledge_page_saved", report=report)
            return json.dumps({"status": "saved", "page": str(page_path), "index": str(self.library / "首页.md"), "page_id": self.page_id}, ensure_ascii=False)


def run_knowledge_agent(pdf_path: str, task: str = DEFAULT_TASK13, library_dir: str | Path = DEFAULT_LIBRARY) -> dict:
    """Run on a user-selected PDF. Identical contents reuse their existing page without a cloud call."""
    library = Path(library_dir).expanduser().resolve()
    for folder in (library, library / "pages", library / "sources"): folder.mkdir(parents=True, exist_ok=True)
    source = Path(pdf_path).expanduser().resolve(strict=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with LIBRARY_LOCK:
        existing = next((p for p in load_catalog(library)["pages"] if p["source_sha256"] == digest), None)
    if existing:
        old = json.loads((Path(existing["run_dir"]) / "summary.json").read_text(encoding="utf-8"))
        result = {**old, "status": "duplicate_reused", "actual_cloud_run": False, "tool_path": [],
            "requested_source_pdf": str(source), "notice": "文件内容相同，程序直接返回已有知识页，本次未调用模型。"}
        print(result["notice"], result["note_path"])
        return result
    session = KnowledgeSession13(str(source), library)
    (session.run_dir / "user_prompt.txt").write_text(task, encoding="utf-8")
    session.log("user_task", text=task)
    agent = create_deep_agent(model=session.model,
        tools=[session.inspect_library, session.read_library_page, session.read_pdf, session.save_knowledge_page],
        system_prompt=SYSTEM13, middleware=[KnowledgeTools13(),
            ModelCallLimitMiddleware(run_limit=9, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=12, exit_behavior="error")])
    print("实际输入：", source, "\n真实运行记录：", session.run_dir, flush=True)
    outcome = run_agent(session, agent, {"messages": [{"role": "user", "content": task}]}, config={"recursion_limit": 55})
    if not session.reports: raise RuntimeError(f"未生成知识页；失败日志保留在{session.run_dir}。")
    calls = [c["name"] for e in session.events if e["kind"] == "model_tool_request" for c in e["calls"]]
    result = {"status": "created", "run_dir": str(session.run_dir), "library_dir": str(library),
        "source_pdf": str(source), "source_sha256": digest, "source_page_count": len(session.reader.pages),
        "model": session.model_name, "tool_path": calls, "note_path": session.reports[-1]["markdown"],
        "page_id": session.page_id, "reports": session.reports, "actual_cloud_run": True, "answer": outcome["answer"]}
    write_json(session.run_dir / "summary.json", result)
    return result


def safe_markdown(text: str) -> str:
    # Raw HTML is escaped; model links are stripped to text. Page/source links below are program-generated.
    text = re.sub(r"!?\[([^\]]*)\]\([^\n)]*\)", r"\1", text)
    return markdown.markdown(html.escape(text, quote=False), extensions=["tables", "fenced_code"])


def render_knowledge_comparison(result: dict) -> tuple[str, Path]:
    """Render every page of the exact source snapshot beside the saved knowledge page. No cloud call."""
    run_dir = Path(result["run_dir"])
    snapshot = Path(result["reports"][-1]["source_pdf"])
    if hashlib.sha256(snapshot.read_bytes()).hexdigest() != result["source_sha256"]:
        raise RuntimeError("保存的原PDF已变化，停止与旧知识页混合展示。")
    images = run_dir / "page_previews"
    images.mkdir(exist_ok=True)
    renderer = "import fitz,sys;from pathlib import Path;d=fitz.open(sys.argv[1]);out=Path(sys.argv[2]);[(p.get_pixmap(matrix=fitz.Matrix(1.3,1.3)).save(str(out/f'page_{i+1}.png'))) for i,p in enumerate(d)]"
    cp = subprocess.run([__import__("sys").executable, "-X", "utf8", "-c", renderer, str(snapshot), str(images)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if cp.returncode: raise RuntimeError("原PDF渲染失败；知识页和逐页原文仍在输出目录。")
    e = html.escape
    left = []
    for i in range(1, result["source_page_count"] + 1):
        encoded = base64.b64encode((images / f"page_{i}.png").read_bytes()).decode("ascii")
        left.append(f'<section id="source-{result["page_id"]}-{i}"><h3>原PDF · 第{i}页</h3><img alt="原PDF第{i}页" src="data:image/png;base64,{encoded}"></section>')
    note = Path(result["note_path"]).read_text(encoding="utf-8")
    body = safe_markdown(note)
    body = re.sub(r"\[PDF第(\d+)页\]", lambda m: f'<a href="#source-{result["page_id"]}-{m[1]}">[PDF第{m[1]}页]</a>', body)
    related_meta = json.loads(Path(result["note_path"]).with_suffix(".json").read_text(encoding="utf-8"))
    catalog = {p["page_id"]: p for p in load_catalog(Path(result["library_dir"]))["pages"]}
    related_links = " ".join(f'<a href="{e((Path(result["library_dir"])/"pages"/(i+".md")).as_uri(),quote=True)}">{e(catalog[i]["title"])}</a>' for i in related_meta["related_page_ids"])
    panel = f'''<div class="kb13"><style>
    .kb13{{font:15px/1.75 system-ui,"Microsoft YaHei",sans-serif;color:inherit;padding:20px;max-width:1600px;margin:auto;background:transparent}}
    .kb13 .pair{{display:grid;grid-template-columns:minmax(320px,1fr) minmax(360px,1fr);gap:24px;align-items:start}}
    .kb13 .original,.kb13 .knowledge{{min-width:0;border:1px solid #8886;border-radius:10px;padding:16px}}
    .kb13 img{{width:100%;height:auto;background:white}}.kb13 table{{border-collapse:collapse;width:100%;font-size:13px;display:block;overflow-x:auto}}
    .kb13 th,.kb13 td{{border:1px solid #8886;padding:7px 9px;vertical-align:top;text-align:left;min-width:55px}}
    .kb13 th{{background:#7188a322}}.kb13 a{{color:#338abd}}.kb13 h1{{font-size:25px;line-height:1.4}}.kb13 h2{{font-size:20px;border-bottom:1px solid #8885;padding-bottom:6px;margin-top:25px}}
    .kb13 .meta{{font-size:13px;color:inherit;opacity:.8;overflow-wrap:anywhere}}.kb13 blockquote{{border-left:3px solid #4588ae;padding-left:12px;margin-left:0}}
    .kb13 p,.kb13 li,.kb13 td{{overflow-wrap:anywhere}}.kb13 code{{background:#8882;padding:2px 4px}}.kb13 section{{scroll-margin-top:20px}}
    @media(max-width:900px){{.kb13 .pair{{grid-template-columns:1fr}}}}
    </style><h1>原PDF与知识页对照</h1><p class="meta">{e(Path(result['source_pdf']).name)} · {result['source_page_count']}页 · 模型：{e(result['model'])}<br>
    这是本轮真实生成的正文，未手工替换模型内容。实际请求：{e(' → '.join(result['tool_path']))}</p>
    <p><a href="{e(snapshot.as_uri(),quote=True)}">打开原PDF</a> · <a href="{e(Path(result['note_path']).as_uri(),quote=True)}">打开知识页</a> · <a href="{e((Path(result['library_dir'])/'首页.md').as_uri(),quote=True)}">知识库目录</a></p>
    <div class="pair"><div class="original">{''.join(left)}</div><article class="knowledge">{body}<p>{related_links}</p></article></div></div>'''
    page = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PDF知识页对照</title><style>body{margin:0;background:#f8fafc;color:#202c3c}@media(prefers-color-scheme:dark){body{background:#161b22;color:#dce5ef}}</style></head><body>' + panel + '</body></html>'
    output = run_dir / "原PDF与知识页对照.html"
    output.write_text(page, encoding="utf-8")
    return panel, output


def show_knowledge_result(result: dict):
    from IPython.display import HTML, display
    panel, path = render_knowledge_comparison(result)
    display(HTML(panel))
    print("独立对照页面：", path)


def show_library(library_dir: str | Path = DEFAULT_LIBRARY):
    from IPython.display import Markdown, display
    library = Path(library_dir)
    rebuild_index(library)
    display(Markdown((library / "首页.md").read_text(encoding="utf-8")))
