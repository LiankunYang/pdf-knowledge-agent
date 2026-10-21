"""查找已有知识页，按来源身份和事实记录检查新建、更新及待确认操作。"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from pydantic import BaseModel

from agent_runtime import ApplicationToolsOnly, run_agent
from knowledge_agent import KnowledgeSession13, LIBRARY_LOCK, ROOT, compact, load_catalog, write_json

SYSTEM = (ROOT / "prompts/maintenance_system.md").read_text(encoding="utf-8")
DEFAULT_LIBRARY = ROOT / "knowledge_base_v2"


def rebuild_index(library):
    lines=["# 知识库目录：查找与更新版","","同主题不等于同一资料；更新保留页ID、历史版本和来源。","",
           "|知识页|主题|版本|当前来源页数|","|---|---|---|---|"]
    for p in load_catalog(library)["pages"]:
        title=p["title"].replace("|","／")
        lines.append(f"|[{title}](pages/{p['page_id']}.md)|{p['category']}|{p.get('revision',1)}|{p['source_page_count']}|")
    lines += ["","相同文件内容复用当前知识页。不同文件由Agent根据来源身份及内容选择新建、更新或待确认；有冲突时不覆盖。",
              "","history保存旧版，sources保留原PDF；drafts只在遇到待确认事项时保存草稿。"]
    (library/"首页.md").write_text("\n".join(lines),encoding="utf-8")


class Identity(BaseModel):
    issuer: str
    document_ids: list[str]
    object_ids: list[str]


class Fact(BaseModel):
    key: str
    value: str
    pdf_page: int
    quote: str


class MaintenanceTools(ApplicationToolsOnly):
    allowed = {"search_knowledge", "read_library_page", "read_pdf", "commit_page"}


def normalized(value):
    return compact(str(value)).casefold()


def identity_key(value):
    if not value or not value.get("issuer") or not value.get("document_ids"):
        return None
    return (normalized(value["issuer"]), tuple(sorted(normalized(x) for x in value["document_ids"])),
            tuple(sorted(normalized(x) for x in value.get("object_ids", []))))


def search_tokens(text):
    text = text.casefold()
    words = set(re.findall(r"[a-z0-9][a-z0-9._/-]{1,}", text))
    for span in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(span[i:i+2] for i in range(max(1,len(span)-1)))
    return words


def relocate_links(text, old_parent, new_parent):
    """Only rebase local Markdown links when a preserved note is shown elsewhere."""
    def replace(match):
        label,url=match.group(1),match.group(2)
        if re.match(r"^(?:https?://|file:|#)",url):return match.group(0)
        destination=(old_parent/url).resolve()
        return f"[{label}]({Path(os.path.relpath(destination,new_parent)).as_posix()})"
    return re.sub(r"\[([^\]]*)\]\(([^\n)]+)\)",replace,text)


class MaintenanceSession(KnowledgeSession13):
    def __init__(self, pdf_path, library):
        super().__init__(pdf_path, library)
        # Preserve the actual common tool code as well as this module and prompt.
        (self.run_dir / "base_tools_used.py").write_bytes(ROOT.joinpath("knowledge_agent.py").read_bytes())
        (self.run_dir / "module_used.py").write_bytes(Path(__file__).read_bytes())
        (self.run_dir / "system_prompt.txt").write_text(SYSTEM, encoding="utf-8")
        self.known_pages = {}
        self.pages_read = {}
        self.search_count = 0
        self.log("maintenance_setup", system_prompt=SYSTEM, mode="model chooses search/read/new/update/draft")

    def search_knowledge(self, query: str, reason: str, top_k: int = 5) -> str:
        """Find existing knowledge pages using words/identifiers and Chinese character bigrams. Returns ranked snippets and metadata, not semantic embeddings or an identity verdict. Retry with precise certificate/product identifiers when needed."""
        if not query.strip() or not 1 <= top_k <= 8:
            return json.dumps({"status":"invalid_query"})
        catalog = load_catalog(self.library)
        tokens = search_tokens(query)
        hits = []
        self.known_pages = {p["page_id"]: p for p in catalog["pages"]}
        for p in catalog["pages"]:
            path = self.library / "pages" / (p["page_id"] + ".md")
            text = path.read_text(encoding="utf-8")
            head = " ".join([p["title"],p["summary"],p["category"],json.dumps(p.get("identity",{}),ensure_ascii=False)])
            common = tokens & search_tokens(head+" "+text)
            score = sum((5 if re.search(r"\d", token) else 1) * (2 if token in head.casefold() else 1) for token in common)
            if normalized(query) in normalized(head): score += 20
            if score <= 0: continue
            snippets = sorted(text.splitlines(), key=lambda line:len(tokens & search_tokens(line)),reverse=True)[:3]
            hits.append({"page_id":p["page_id"],"title":p["title"],"summary":p["summary"],"score":score,
                "identity":p.get("identity"),"revision":p.get("revision",1),"snippets":snippets})
        hits = sorted(hits,key=lambda h:(-h["score"],h["page_id"]))[:top_k]
        self.search_count += 1
        self.log("knowledge_searched", query=query, reason=reason, results=hits)
        return json.dumps({"status":"ok","total_pages":len(catalog["pages"]),"hits":hits,
            "notice":"相似度仅供查找，不代表同一资料。没有结构化身份的旧页不能自动覆盖。"},ensure_ascii=False)

    def read_library_page(self, page_id: str, reason: str) -> str:
        """Read a searched page's full Markdown, stable source identity and previously recorded facts. Required before updating/linking it; returned page hash is checked at commit to avoid overwriting later changes."""
        if page_id not in self.known_pages:
            return json.dumps({"status":"unknown_page","reason":"先搜索知识库。"},ensure_ascii=False)
        path = self.library / "pages" / (page_id+".md")
        metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.pages_read[page_id] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.log("existing_page_read",page_id=page_id,reason=reason)
        return json.dumps({"page_id":page_id,"metadata":metadata,"markdown":path.read_text(encoding="utf-8")},ensure_ascii=False)

    def commit_page(self, action: Literal["new","update","needs_confirmation"], target_page_id: str,
                    identity: Identity, facts: list[Fact], title: str, summary: str, category: str,
                    report_markdown: str, used_pages: list[int], source_quotes: list[dict],
                    related_page_ids: list[str], decision_reason: str, change_summary: list[str]) -> str:
        """Commit a new page, update an identified existing page, or save a non-overwriting draft. Provide source identity literally from PDF; each fact has stable key/value/page and an exact short quote. Updating requires reading target, same issuer/document/object IDs, all old fact keys retained with equal values, and no conflicting revision. Old versions and all PDFs are retained. Failed checks never overwrite a page."""
        with self.tool_lock, LIBRARY_LOCK:
            identity = identity.model_dump() if isinstance(identity,Identity) else identity
            facts = [x.model_dump() if isinstance(x,Fact) else x for x in facts]
            errors = []
            expected = set(range(1,len(self.reader.pages)+1))
            if not self.search_count: errors.append("保存前先查找已有知识。")
            if self.delivered_pages != expected: errors.append("先读取本次PDF全部页面。")
            if hashlib.sha256(self.source.read_bytes()).hexdigest()!=self.source_hash: errors.append("本次文件已变化。")
            if any(not p["plain_text"].strip() for p in self.read_pages.values()): errors.append("有空文本页，本版不能OCR。")
            if set(used_pages)!=expected: errors.append("used_pages需覆盖本次PDF。")
            cited={int(p) for p in re.findall(r"PDF\s*第\s*(\d+)\s*页",report_markdown)}
            if cited!=expected: errors.append("正文需引用本次PDF的所有有效页码。")
            if len(report_markdown.strip())<300 or not title.strip() or len(summary)>400: errors.append("请提供完整正文和简短标题简介。")
            if not 2<=len(source_quotes)<=6: errors.append("需要2至6段短引文。")
            proof_text = normalized(" ".join(p["plain_text"]+" "+p["layout_text"] for p in self.read_pages.values()))
            for value in [identity.get("issuer",""),*identity.get("document_ids",[]),*identity.get("object_ids",[])]:
                if value and normalized(value) not in proof_text: errors.append("身份标识不在本次提取原文中："+value)
            def quote_ok(q):
                page=self.read_pages.get(q.get("pdf_page"))
                quote=q.get("quote","")
                return bool(page and isinstance(quote,str) and 6<=len(quote)<=500 and any(compact(quote) in compact(page[k]) for k in ["plain_text","layout_text"]))
            for q in [*source_quotes,*facts]:
                if not quote_ok(q):errors.append("引文未在对应页原文找到："+str(q.get("quote",""))[:70])
            keys=[f["key"] for f in facts]
            if len(keys)!=len(set(keys)) or not facts:errors.append("事实记录需要非空且不重复的key。")
            if any(i not in self.pages_read for i in related_page_ids):errors.append("关联旧页前需读取该页正文。")
            catalog=load_catalog(self.library)
            entries={p["page_id"]:p for p in catalog["pages"]}
            same=[p for p in catalog["pages"] if identity_key(identity) and identity_key(p.get("identity"))==identity_key(identity)]
            if action=="new" and same:
                errors.append("相同来源身份已有页面；请读取它再判断更新或待确认："+same[0]["page_id"])
            target=entries.get(target_page_id)
            conflicts=[]
            if action=="update":
                if not target or target_page_id not in self.pages_read:errors.append("更新前需读取目标页。")
                elif not identity_key(identity) or identity_key(identity)!=identity_key(target.get("identity")):
                    errors.append("新旧资料身份不一致或旧页缺少身份记录，不能自动覆盖。")
                else:
                    path=self.library/"pages"/(target_page_id+".md")
                    if hashlib.sha256(path.read_bytes()).hexdigest()!=self.pages_read[target_page_id]:errors.append("旧页已被其他操作修改，请重新读取。")
                    new_facts={f["key"]:f for f in facts}
                    for old in target.get("facts",[]):
                        if old["key"] not in new_facts:conflicts.append("更新未保留旧事实键："+old["key"])
                        elif normalized(old["value"])!=normalized(new_facts[old["key"]]["value"]):
                            conflicts.append(f"{old['key']}: {old['value']} → {new_facts[old['key']]['value']}")
            attempt={"action":action,"target_page_id":target_page_id,"identity":identity,"facts":facts,"title":title,
                "summary":summary,"category":category,"report_markdown":report_markdown,"used_pages":used_pages,
                "source_quotes":source_quotes,"related_page_ids":related_page_ids,"decision_reason":decision_reason,
                "change_summary":change_summary,"errors":errors,"conflicts":conflicts}
            attempt_path=self.run_dir/f"维护提议_{len(list(self.run_dir.glob('维护提议_*.json')))+1}.json"
            write_json(attempt_path,attempt)
            if errors:
                self.log("maintenance_rejected",errors=errors,attempt=str(attempt_path))
                return json.dumps({"status":"not_saved","errors":errors},ensure_ascii=False)
            if action=="needs_confirmation" or conflicts:
                drafts=self.library/"drafts";drafts.mkdir(exist_ok=True)
                path=drafts/("D-"+self.run_id+".md")
                path.write_text("# 待确认草稿\n\n"+decision_reason+"\n\n"+"\n".join(conflicts)+"\n\n"+report_markdown,encoding="utf-8")
                report={"markdown":str(path),"status":"needs_confirmation","action_requested":action,"target_page_id":target_page_id,
                    "conflicts":conflicts,"source_pdf":str(self.source),"proposal":str(attempt_path)}
                self.reports.append(report);self.log("draft_saved",report=report)
                return json.dumps(report,ensure_ascii=False)
            page_id=target_page_id if action=="update" else "K-"+self.source_hash[:16]
            if action=="new" and page_id in entries:
                return json.dumps({"status":"not_saved","errors":["文件已有记录，不能重复新建。"]},ensure_ascii=False)
            revision=(target.get("revision",1)+1) if action=="update" else 1
            history_link=None
            if action=="update":
                history=self.library/"history"/page_id;history.mkdir(parents=True,exist_ok=True)
                previous=revision-1
                history_md=history/f"v{previous:03d}.md"
                if history_md.exists():raise RuntimeError("历史版本已存在，拒绝覆盖。")
                old_path=self.library/"pages"/(page_id+".md")
                shutil.copy2(old_path,history_md.with_name(history_md.stem+".original.md"))
                history_md.write_text(relocate_links(old_path.read_text(encoding="utf-8"),old_path.parent,history_md.parent),encoding="utf-8")
                shutil.copy2(self.library/"pages"/(page_id+".json"),history_md.with_suffix(".json"))
                history_link=f"../history/{page_id}/v{previous:03d}.md"
            snapshot=self.library/"sources"/(self.source_hash[:16]+".pdf")
            if not snapshot.exists():shutil.copy2(self.source,snapshot)
            source_meta={"source_pdf":str(snapshot),"source_sha256":self.source_hash,"original_path":str(self.source),
                "page_count":len(self.reader.pages),"run_dir":str(self.run_dir)}
            sources=[*(target.get("sources",[]) if action=="update" else []),source_meta]
            created=target.get("created_at") if action=="update" else datetime.now().isoformat(timespec="seconds")
            meta={"page_id":page_id,"title":title,"summary":summary,"category":category,"identity":identity,"facts":facts,
                "source_name":self.source.name,"original_source_path":str(self.source),"source_sha256":self.source_hash,
                "source_page_count":len(self.reader.pages),"created_at":created,"revision":revision,"sources":sources,
                "related_page_ids":related_page_ids,"related_reason":decision_reason,"run_dir":str(self.run_dir),
                "model":self.model_name,"decision":action,"change_summary":change_summary}
            # Model supplies body only; source/index/history sections are program-owned.
            body=report_markdown.strip()
            note=(body if body.startswith("# ") else "# "+title+"\n\n"+body)
            note+="\n\n## 来源与原文入口\n\n"+f"- 当前来源：[{self.source.name}](../sources/{snapshot.name})，共{len(self.reader.pages)}页。\n"
            for q in source_quotes:note+=f"- [PDF第{q['pdf_page']}页] 原文：{q['quote']}\n"
            note+="\n## 本次维护\n\n"+f"- 动作：{action}；知识页版本：{revision}。\n- 判断依据：{decision_reason}\n"
            note+="\n".join("- "+s for s in change_summary)+"\n"
            if history_link:note+=f"- [查看上一版本]({history_link})。旧来源副本继续保留。\n"
            if related_page_ids:
                note+="\n## 相关资料\n\n"+"\n".join(f"- [{entries[i]['title']}]({i}.md)" for i in related_page_ids)+"\n"
            note+="\n本文由模型整理。短引文存在性、身份字符串和部分事实比较不代表全文准确率或合规判断。\n"
            page_path=self.library/"pages"/(page_id+".md")
            page_path.write_text(note,encoding="utf-8");write_json(page_path.with_suffix(".json"),meta)
            # A later update must not alter what an earlier run's notebook shows.
            run_snapshot=self.run_dir/"知识页_本轮快照.md"
            run_snapshot.write_text(relocate_links(note,page_path.parent,run_snapshot.parent),encoding="utf-8")
            write_json(run_snapshot.with_suffix(".json"),meta)
            if action=="update":catalog["pages"]=[meta if p["page_id"]==page_id else p for p in catalog["pages"]]
            else:catalog["pages"].append(meta)
            write_json(self.library/"catalog.json",catalog);rebuild_index(self.library)
            report={"markdown":str(page_path),"snapshot_markdown":str(run_snapshot),"page_id":page_id,"source_pdf":str(snapshot),"status":action,
                "revision":revision,"proposal":str(attempt_path),"previous_revision":history_link,
                "used_pages":used_pages,"source_quotes":source_quotes,"decision_reason":decision_reason}
            self.reports.append(report);self.log("maintenance_committed",report=report)
            return json.dumps(report,ensure_ascii=False)


def run_maintenance(pdf_path: str, task: str, library_dir: str|Path=DEFAULT_LIBRARY):
    library=Path(library_dir).resolve()
    for folder in [library,library/"pages",library/"sources"]:folder.mkdir(exist_ok=True,parents=True)
    source=Path(pdf_path).resolve(strict=True)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    for p in load_catalog(library)["pages"]:
        if digest==p.get("source_sha256") or any(digest==s["source_sha256"] for s in p.get("sources",[])):
            result={"status":"duplicate_reused","page_id":p["page_id"],"note_path":str(library/"pages"/(p["page_id"]+".md")),
                "actual_cloud_run":False,"notice":"内容hash已有记录，返回当前知识页；不自动回退到旧来源。"}
            print(json.dumps(result,ensure_ascii=False));return result
    session=MaintenanceSession(str(source),library)
    (session.run_dir/"user_prompt.txt").write_text(task,encoding="utf-8")
    agent=create_deep_agent(model=session.model,tools=[session.search_knowledge,session.read_library_page,session.read_pdf,session.commit_page],
        system_prompt=SYSTEM,middleware=[MaintenanceTools(),ModelCallLimitMiddleware(run_limit=12,exit_behavior="error"),ToolCallLimitMiddleware(run_limit=18,exit_behavior="error")])
    print("真实维护运行：",session.run_dir,flush=True)
    outcome=run_agent(session,agent,{"messages":[{"role":"user","content":task}]},config={"recursion_limit":75})
    if not session.reports:raise RuntimeError("未保存；日志保留在"+str(session.run_dir))
    report=session.reports[-1]
    result={"status":report["status"],"source_pdf":str(source),"source_sha256":digest,"source_page_count":len(session.reader.pages),
        "run_dir":str(session.run_dir),"library_dir":str(library),"model":session.model_name,"actual_cloud_run":True,
        "tool_path":[c["name"] for e in session.events if e["kind"]=="model_tool_request" for c in e["calls"]],
        "page_id":report.get("page_id"),"note_path":report.get("snapshot_markdown",report["markdown"]),
        "current_page_path":report["markdown"],"reports":session.reports,"answer":outcome["answer"]}
    write_json(session.run_dir/"summary.json",result)
    return result
