"""运行知识库维护任务，生成原PDF与知识页的本地对照页面。"""
from __future__ import annotations

import base64
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
import hashlib
import html
import io
import json
from pathlib import Path
import re
import shutil
import uuid

import pymupdf
from pypdf import PdfReader

from knowledge_agent import ROOT, compact, load_catalog, safe_markdown, write_json
from knowledge_maintenance import run_maintenance, relocate_links

LABELS={"new":"新建知识页","update":"更新已有知识页","duplicate_reused":"复用已有知识页", "needs_confirmation":"保存待确认草稿"}
STYLE="""
.kbview{font:15px/1.75 system-ui,"Microsoft YaHei",sans-serif;color:inherit;max-width:1560px;margin:auto;padding:22px;background:transparent}
.kbview *{box-sizing:border-box}.kbview h1{font-size:28px;line-height:1.4;margin:0 0 12px}.kbview h2{font-size:21px;border-bottom:1px solid #8390a54d;padding-bottom:8px;margin-top:28px}.kbview h3{font-size:17px}
.kbview .hero{padding:22px;border:1px solid #8390a566;border-radius:12px;background:#4095bc12}.kbview .badge{display:inline-block;padding:4px 12px;border-radius:20px;background:#2c8f7950;font-weight:700;margin-bottom:10px}
.kbview .meta{font-size:13px;opacity:.8;overflow-wrap:anywhere}.kbview .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:16px 0}.kbview .card{border:1px solid #8390a54d;padding:14px;border-radius:9px;min-width:0}
.kbview .pair{display:grid;grid-template-columns:minmax(300px,1fr) minmax(360px,1fr);gap:22px;align-items:start}.kbview .original,.kbview .note{min-width:0;padding:15px;border:1px solid #8390a54d;border-radius:10px}
.kbview img{width:100%;height:auto;background:white}.kbview table{border-collapse:collapse;width:100%;font-size:13px;display:block;overflow-x:auto;margin:12px 0}.kbview td,.kbview th{border:1px solid #8390a54d;padding:8px 10px;vertical-align:top;text-align:left;min-width:72px}.kbview th{background:#7087a320}
.kbview p,.kbview li,.kbview td{overflow-wrap:anywhere}.kbview a{color:#258bc0}.kbview code{background:#8390a51f;padding:2px 5px}.kbview pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 Consolas,monospace}.kbview details{border:1px solid #8390a54d;border-radius:9px;padding:12px;margin:14px 0}.kbview summary{cursor:pointer;font-weight:600}.kbview blockquote{margin-left:0;border-left:3px solid #3289ae;padding-left:12px}.kbview .muted{opacity:.78}.kbview .wrap{overflow:auto}.kbview section{scroll-margin-top:20px}
@media(max-width:900px){.kbview .pair{grid-template-columns:1fr}.kbview{padding:14px}}
"""


def full_html(panel,title):
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+html.escape(title)+'</title><style>body{margin:0;background:#f8fafc;color:#253143}@media(prefers-color-scheme:dark){body{background:#161c24;color:#dde6f0}}</style></head><body>'+panel+'</body></html>'


def library_snapshot(library):
    result={}
    for p in load_catalog(library)["pages"]:
        path=library/"pages"/(p["page_id"]+".md")
        meta=json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        result[p["page_id"]]={"metadata":meta,"markdown":path.read_text(encoding="utf-8"),"path":str(path)}
    return result


def source_path(meta,library):
    for source in reversed(meta.get("sources",[])):
        if source.get("source_sha256")==meta.get("source_sha256") and Path(source["source_pdf"]).is_file():
            return Path(source["source_pdf"])
    for name in [meta.get("page_id","")+".pdf",meta.get("source_sha256","")[:16]+".pdf"]:
        path=library/"sources"/name
        if path.is_file():return path
    return None


def export_library_view(library,folder):
    e=html.escape
    destination=folder/"knowledge_pages";destination.mkdir(exist_ok=True)
    pages=library_snapshot(library)
    cards=[]
    for page_id,item in pages.items():
        meta=item["metadata"]
        original=source_path(meta,library)
        links=f'<a href="../知识库目录.html">返回目录</a>'
        if original:links+=f' · <a href="{e(original.as_uri(),quote=True)}">打开来源PDF</a>'
        related=' '.join(f'<a href="{e(i)}.html">{e(pages[i]["metadata"]["title"])}</a>' for i in meta.get("related_page_ids",[]) if i in pages)
        history=''
        hist_folder=library/"history"/page_id
        if hist_folder.is_dir():
            for old in sorted(hist_folder.glob("v[0-9][0-9][0-9].md")):
                name=page_id+'_'+old.stem+'.html'
                old_panel=f'<div class="kbview"><style>{STYLE}</style><p><a href="{page_id}.html">返回当前页</a></p><h1>历史版本 {e(old.stem)}</h1>{safe_markdown(old.read_text(encoding="utf-8"))}</div>'
                (destination/name).write_text(full_html(old_panel,"历史知识页"),encoding="utf-8")
                history+=f'<a href="{e(name)}">历史版本 {e(old.stem)}</a> '
        panel=f'<div class="kbview"><style>{STYLE}</style><p>{links}</p><p class="meta">知识页版本：{meta.get("revision",1)} · 来源PDF：{meta["source_page_count"]}页</p>{safe_markdown(item["markdown"])}<h2>页面导航</h2><p>{related or "暂无相关页面"}</p><p>{history}</p></div>'
        (destination/(page_id+".html")).write_text(full_html(panel,meta["title"]),encoding="utf-8")
        cards.append(f'<article class="card"><h3><a href="knowledge_pages/{page_id}.html">{e(meta["title"])}</a></h3><p>{e(meta["summary"])}</p><p class="meta">{e(meta["category"])} · 版本{meta.get("revision",1)}</p></article>')
    panel=f'<div class="kbview"><style>{STYLE}</style><h1>知识库目录</h1><p class="meta">本次处理结束时的知识库快照，共{len(pages)}篇知识页。点击标题查看排版内容，无需Markdown预览。</p><div class="cards">{"".join(cards)}</div><p><a href="本次处理结果.html">返回本次处理结果</a></p></div>'
    index=folder/"知识库目录.html";index.write_text(full_html(panel,"知识库目录"),encoding="utf-8")
    return index


def build_presentation(pdf_path,library,result,before,folder):
    """Build only the view. Saved model output and agent decisions are not edited."""
    e=html.escape
    pdf_path=Path(pdf_path).resolve();library=Path(library).resolve();folder=Path(folder).resolve()
    digest=hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    after=library_snapshot(library)
    reports=result.get("reports",[])
    page_id=result.get("page_id") or (reports[-1].get("target_page_id") if reports else None)
    status=result["status"]
    entry=after.get(page_id)
    note_path=Path(result["note_path"])
    note=note_path.read_text(encoding="utf-8")
    meta=(entry or {}).get("metadata",{})
    proposal=None
    if reports and reports[-1].get("proposal"):
        proposal=json.loads(Path(reports[-1]["proposal"]).read_text(encoding="utf-8"))
    reason=(reports[-1].get("decision_reason") if reports else None) or result.get("notice") or (proposal or {}).get("decision_reason","")
    if not reason:reason="已保存本次处理结果。"
    original=folder/"输入文件.pdf"
    shutil.copy2(pdf_path,original)
    preview=folder/"page_previews";preview.mkdir(exist_ok=True)
    doc=pymupdf.open(original)
    reader=PdfReader(original)
    raw_pages=[];image_blocks=[]
    for i,page in enumerate(doc,1):
        png=preview/f"page_{i}.png";page.get_pixmap(matrix=pymupdf.Matrix(1.2,1.2)).save(str(png))
        encoded=base64.b64encode(png.read_bytes()).decode("ascii")
        text=reader.pages[i-1].extract_text() or ""
        raw_pages.append({"pdf_page":i,"text":text})
        image_blocks.append(f'<section id="input-page-{i}"><h3>输入PDF · 第{i}页</h3><img alt="输入PDF第{i}页" src="data:image/png;base64,{encoded}"><details><summary>查看本页提取文字</summary><pre>{e(text)}</pre></details></section>')
    page_count=len(doc);doc.close()
    write_json(folder/"输入逐页文字.json",{"source_pdf":str(pdf_path),"source_sha256":digest,"pages":raw_pages})
    note_snapshot=folder/"知识页_本次展示快照.md"
    note_snapshot.write_text(relocate_links(note,note_path.parent,folder),encoding="utf-8")
    body=safe_markdown(note)
    knowledge_source=source_path(meta,library) if meta else None
    same_source=digest==meta.get("source_sha256")
    def cite(match):
        number=int(match[1])
        if same_source and 1<=number<=page_count:url=f"#input-page-{number}"
        elif knowledge_source:url=knowledge_source.as_uri()+f"#page={number}"
        else:return match[0]
        return f'<a href="{e(url,quote=True)}">{match[0]}</a>'
    body=re.sub(r"\[PDF第(\d+)页\]",cite,body)
    events=[]
    if result.get("run_dir"):
        run_file=Path(result["run_dir"])/"run.json"
        if run_file.is_file():events=json.loads(run_file.read_text(encoding="utf-8"))["events"]
    actions=[];hits={};read_ids=[]
    for event in events:
        if event["kind"]=="pdf_read":actions.append("读取输入PDF："+"、".join(map(str,event["returned_pages"]))+"页")
        elif event["kind"]=="knowledge_searched":
            actions.append("搜索已有知识："+event["query"]+f"；返回{len(event['results'])}个候选")
            for h in event["results"]:hits[h["page_id"]]=h
        elif event["kind"]=="existing_page_read":
            read_ids.append(event["page_id"]);actions.append("读取候选知识页："+before.get(event["page_id"],{}).get("metadata",{}).get("title",event["page_id"]))
        elif event["kind"]=="maintenance_rejected":actions.append("提交检查未通过："+"；".join(event["errors"]))
        elif event["kind"]=="maintenance_committed":actions.append(LABELS.get(event["report"]["status"],event["report"]["status"]))
        elif event["kind"]=="draft_saved":actions.append("保存待确认草稿，已有知识页未被覆盖")
    if status=="duplicate_reused":actions=["检查输入文件内容hash","命中已有来源记录","复用当前知识页；本次未调用模型或重新检索"]
    candidate_rows=[]
    for pid,hit in hits.items():
        old=before.get(pid,{}).get("metadata",{})
        identity=old.get("identity",{})
        ids="、".join(identity.get("document_ids",[])) or "早期页面未登记结构化编号"
        candidate_rows.append(f'<tr><td>{e(hit["title"])}</td><td>{e(ids)}</td><td>{"已读取正文" if pid in read_ids else "仅命中检索"}</td></tr>')
    if status=="duplicate_reused":
        candidate_rows.append(f'<tr><td>{e(meta.get("title",page_id or ""))}</td><td>输入内容hash已记录</td><td>程序直接复用，未重新检索</td></tr>')
    old_facts={f["key"]:f for f in before.get(page_id,{}).get("metadata",{}).get("facts",[])}
    new_facts=(proposal or {}).get("facts",meta.get("facts",[]))
    fields=[]
    input_text={p["pdf_page"]:compact(p["text"]) for p in raw_pages}
    for fact in new_facts:
        old=old_facts.get(fact["key"])
        if status=="duplicate_reused":comparison="已有记录复用（未重新抽取）"
        elif old and compact(old["value"])==compact(fact["value"]):comparison="保留原记录"
        elif old:comparison="有差异，需确认"
        else:comparison="新增记录"
        quote=fact.get("quote","");p=fact.get("pdf_page")
        found=bool(quote and compact(quote) in input_text.get(p,""))
        evidence=f"PDF第{p}页：{quote}" if quote else "没有独立字段引文"
        evidence+=("（短引文在输入文字中找到）" if found else "（本次输入中未找到该短引文）")
        fields.append(f'<tr><td>{e(fact["key"])}</td><td>{e(old["value"] if old else "—")}</td><td>{e(fact["value"])}</td><td>{e(comparison)}</td><td>{e(evidence)}</td></tr>')
    source_notice="知识页页码对应左侧这份输入PDF。" if same_source else "知识页引用对应其当前来源PDF，可能与本次输入不是同一文件；点击页码打开知识页的来源。"
    if status=="needs_confirmation":source_notice="右侧为待确认草稿；尚未覆盖已有知识页。"
    catalog_index=export_library_view(library,folder)
    source_link=f'<a href="{e(knowledge_source.as_uri(),quote=True)}">知识页来源PDF</a>' if knowledge_source else ""
    old_count=len(before);new_count=len(after)
    candidates=''.join(candidate_rows) or '<tr><td colspan="3">本轮没有检索到候选知识页。</td></tr>'
    facts_html=('<table><thead><tr><th>记录项</th><th>已有知识</th><th>本次保存/复用内容</th><th>处理</th><th>输入原文入口</th></tr></thead><tbody>'+''.join(fields)+'</tbody></table>') if fields else '<p>本页没有结构化字段记录，请对照下方正文和原PDF查看。</p>'
    replay_notice=f'<p>{e(result["presentation_notice"])}</p>' if result.get("presentation_notice") else ''
    panel=f'''<div class="kbview"><style>{STYLE}</style><header class="hero"><span class="badge">{e(LABELS.get(status,status))}</span><h1>PDF知识库整理 · 本次处理结果</h1><p>{e(reason)}</p>{replay_notice}<p class="meta">输入：{e(pdf_path.name)} · {page_count}页 · 模型调用：{"是" if result.get("actual_cloud_run") else "否"}</p></header>
    <div class="cards"><div class="card"><strong>处理前</strong><br>{old_count}篇知识页</div><div class="card"><strong>处理后</strong><br>{new_count}篇知识页</div><div class="card"><strong>当前页版本</strong><br>{meta.get("revision","—")}</div></div>
    <p><a href="{e(original.as_uri(),quote=True)}">打开本次输入PDF</a> · <a href="{e(catalog_index.as_uri(),quote=True)}">浏览知识库</a> · {source_link}</p>
    <h2>本次处理过程</h2><ol>{''.join('<li>'+e(a)+'</li>' for a in actions)}</ol>
    <h2>已有资料查找与核对</h2><table><thead><tr><th>候选知识页</th><th>来源标识</th><th>本轮读取情况</th></tr></thead><tbody>{candidates}</tbody></table>
    <details open><summary>内容与已有记录对照</summary><p class="meta">“引文找到”只说明该片段存在，不等于整个字段值或正文已经验证正确；复合记录可能只引用其中一部分。</p>{facts_html}</details>
    <h2>原PDF与知识页</h2><p class="meta">{e(source_notice)}</p><div class="pair"><div class="original">{''.join(image_blocks)}</div><article class="note">{body}</article></div>
    <details><summary>运行记录与范围</summary><p>当前支持1至12页、30MiB以内、未加密的文字型PDF。未接入OCR或工程合规判断。新文件整理会将提取文字发送给云模型。</p><p class="meta">本次展示目录：{e(str(folder))}<br>Agent记录：{e(str(result.get('run_dir') or '本轮内容重复，没有新模型会话'))}</p></details></div>'''
    panel_path=folder/"展示面板.html";panel_path.write_text(panel,encoding="utf-8")
    html_path=folder/"本次处理结果.html";html_path.write_text(full_html(panel,"PDF知识库整理 · 本次处理结果"),encoding="utf-8")
    public={"status":status,"label":LABELS.get(status,status),"input_pdf":str(pdf_path),"input_sha256":digest,
        "input_pages":page_count,"library_dir":str(library),"page_id":page_id,"actual_cloud_run":result.get("actual_cloud_run",False),
        "before_page_count":old_count,"after_page_count":new_count,"actions":actions,"candidate_count":len(hits),
        "field_count":len(new_facts),"result_html":str(html_path),"panel_path":str(panel_path),"catalog_html":str(catalog_index),
        "agent_result":result,"output_dir":str(folder)}
    write_json(folder/"session.json",public)
    return public


def process_pdf(pdf_path: str, task: str, library_dir: str|Path=ROOT/"knowledge_base_v2"):
    """Complete presentation entry: validate the selected file, run the Agent, then export input/library comparisons."""
    source=Path(pdf_path).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.casefold()!=".pdf":raise ValueError("请填写存在的PDF文件路径。")
    if source.stat().st_size>30*1024*1024:raise ValueError("当前支持30MiB以内PDF。")
    reader=PdfReader(source)
    if reader.is_encrypted or not 1<=len(reader.pages)<=12:raise ValueError("当前支持未加密的1至12页文字型PDF。")
    if not task.strip():raise ValueError("请填写整理要求。")
    library=Path(library_dir).expanduser().resolve()
    for path in [library,library/"pages",library/"sources"]:path.mkdir(exist_ok=True,parents=True)
    folder=ROOT/"outputs/presentation"/(datetime.now().strftime("%Y%m%d_%H%M%S")+"_"+uuid.uuid4().hex[:6])
    folder.mkdir(exist_ok=False,parents=True)
    before=library_snapshot(library)
    write_json(folder/"input.json",{"pdf_path":str(source),"task":task,"library_dir":str(library)})
    write_json(folder/"入库前知识库.json",before)
    print("正在处理：",source.name,flush=True)
    print("输入读取、内容去重；新资料由Agent查找旧知识并判断如何保存。",flush=True)
    captured=io.StringIO()
    try:
        with redirect_stdout(captured),redirect_stderr(captured):result=run_maintenance(str(source),task,library)
        session=build_presentation(source,library,result,before,folder)
    except Exception as error:
        write_json(folder/"失败记录.json",{"status":"failed","error_type":type(error).__name__,"message":str(error),"source_pdf":str(source)})
        raise RuntimeError("本次未完成，未提供旧结果冒充成功。检查记录："+str(folder/"失败记录.json")) from error
    print("处理完成：",session["label"],flush=True)
    print("结果：",session["result_html"],flush=True)
    latest=ROOT/"outputs/最新处理结果.html"
    shutil.copy2(session["result_html"],latest)
    session["latest_html"]=str(latest)
    write_json(folder/"session.json",session)
    return session


def show_presentation(session):
    from IPython.display import HTML, display
    display(HTML(Path(session["panel_path"]).read_text(encoding="utf-8")))
    print("完整结果：",session["result_html"])
    print("知识库浏览：",session["catalog_html"])
