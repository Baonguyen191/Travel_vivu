"""Trang duyệt nhãn bộ câu hỏi RAG bằng phím T/F (chạy local, không cần thư viện web).

    python scripts/review_rag_queries.py      # mở http://127.0.0.1:8765

Phím: T = nhãn đúng · F = nhãn sai (nhập ghi chú, Enter lưu) · A = sửa từ khoá đáp án ·
U = bỏ nhãn · ←/→ = câu trước/sau · N = câu chưa duyệt kế tiếp.

Mỗi lần bấm:
- ghi một dòng vào `eval/rag_query_reviews.jsonl` ({id, label, note, ts}); dòng mới nhất của một id là quyết định hiện hành;
- T đặt `reviewed: true` cho câu đó trong `eval/rag_queries.yml`, F/U đặt `reviewed: false`.
  Chỉ sửa đúng dòng `reviewed:` trong khối của câu, giữ nguyên chú thích và định dạng file.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.corpus import load_chunks  # noqa: E402
from rag.lexical import BM25  # noqa: E402

QUERIES_PATH = ROOT / "eval" / "rag_queries.yml"
REVIEWS_PATH = ROOT / "eval" / "rag_query_reviews.jsonl"
HOST, PORT = "127.0.0.1", 8765
LABELS = {"T", "F", "U"}
MAX_BODY = 16_384
EVIDENCE_PER_PLACE = 2

_ID_LINE = re.compile(r"^- id:\s*(\S+)\s*$")
_REVIEWED_LINE = re.compile(r"^(\s+reviewed:\s*)(true|false)\s*$")


def set_reviewed(text: str, query_id: str, value: bool) -> str:
    """Đổi dòng `reviewed:` trong khối `- id: <query_id>`; thêm dòng nếu khối chưa có."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if (m := _ID_LINE.match(line)) and m.group(1) == query_id), None)
    if start is None:
        raise KeyError(f"không thấy câu {query_id} trong file")
    end = next((i for i in range(start + 1, len(lines)) if _ID_LINE.match(lines[i])), len(lines))
    new = "true" if value else "false"
    for i in range(start + 1, end):
        m = _REVIEWED_LINE.match(lines[i])
        if m:
            lines[i] = f"{m.group(1)}{new}"
            return "\n".join(lines)
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, f"  reviewed: {new}")
    return "\n".join(lines)


_KEYWORDS_LINE = re.compile(r"^\s+answer_keywords:")
_INSERT_BEFORE = re.compile(r"^\s+(source_chunk|author|reviewed):")
MAX_KEYWORDS = 10


def set_answer_keywords(text: str, query_id: str, keywords: list[str]) -> str:
    """Ghi dòng `answer_keywords: [...]` trong khối của câu (thay dòng cũ, hoặc chèn trước
    source_chunk/author/reviewed); danh sách rỗng thì xoá dòng."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if (m := _ID_LINE.match(line)) and m.group(1) == query_id), None)
    if start is None:
        raise KeyError(f"không thấy câu {query_id} trong file")
    end = next((i for i in range(start + 1, len(lines)) if _ID_LINE.match(lines[i])), len(lines))
    new_line = f"  answer_keywords: [{', '.join(json.dumps(k, ensure_ascii=False) for k in keywords)}]"
    for i in range(start + 1, end):
        if _KEYWORDS_LINE.match(lines[i]):
            if keywords:
                lines[i] = new_line
            else:
                del lines[i]
            return "\n".join(lines)
    if not keywords:
        return text
    at = next((i for i in range(start + 1, end) if _INSERT_BEFORE.match(lines[i])), None)
    if at is None:
        at = end
        while at > start + 1 and not lines[at - 1].strip():
            at -= 1
    lines.insert(at, new_line)
    return "\n".join(lines)


def latest_reviews(path: Path = REVIEWS_PATH) -> dict[str, dict]:
    reviews: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                reviews[r["id"]] = r
    return reviews


def build_items(queries_path: Path = QUERIES_PATH, chunks=None) -> list[dict]:
    """Mỗi câu kèm đoạn bằng chứng: `source_chunk` nếu có, thêm các chunk của địa danh đúng
    khớp câu hỏi nhất (BM25 trong phạm vi địa danh đó)."""
    chunks = chunks if chunks is not None else load_chunks()
    by_id = {c.chunk_id: c for c in chunks}
    by_place: dict[str, list] = {}
    for c in chunks:
        if c.place_key:
            by_place.setdefault(c.place_key, []).append(c)
    reviews = latest_reviews()
    items = []
    for q in yaml.safe_load(queries_path.read_text(encoding="utf-8")):
        keys = [f"wikidata:{r['qid']}" for r in q["relevant"]]
        evidence, used = [], set()
        src = q.get("source_chunk")
        if src and src in by_id:
            evidence.append(by_id[src])
            used.add(src)
        for key in keys:
            pool = [c for c in by_place.get(key, []) if c.chunk_id not in used]
            if not pool:
                continue
            scores = BM25([c.embedding_text for c in pool]).scores(q["query"] + " " + " ".join(q.get("answer_keywords") or []))
            ranked = [c for _, c in sorted(zip(scores, pool), key=lambda t: -t[0])]
            limit = 1 if len(keys) > 1 or src else EVIDENCE_PER_PLACE
            for c in ranked[:limit]:
                evidence.append(c)
                used.add(c.chunk_id)
        kws = q.get("answer_keywords") or []
        place_texts = [c.text.casefold() for key in keys for c in by_place.get(key, [])]
        items.append({
            "id": q["id"], "query": q["query"], "type": q["type"],
            "answer_missing": [k for k in kws if not any(k.casefold() in t for t in place_texts)],
            "relevant": [{"qid": r["qid"], "name": r.get("name", ""), "has_chunks": f"wikidata:{r['qid']}" in by_place}
                         for r in q["relevant"]],
            "answer_keywords": q.get("answer_keywords") or [],
            "author": q.get("author", "claude"),
            "reviewed": bool(q.get("reviewed")),
            "review": reviews.get(q["id"]),
            "evidence": [{"chunk_id": c.chunk_id, "title": c.title, "section": c.section, "text": c.text,
                          "is_source": c.chunk_id == src} for c in evidence],
        })
    return items


_lock = threading.Lock()


def apply_label(query_id: str, label: str, note: str = "",
                queries_path: Path = QUERIES_PATH, reviews_path: Path = REVIEWS_PATH) -> dict:
    if label not in LABELS:
        raise ValueError(f"nhãn phải thuộc {sorted(LABELS)}")
    record = {"id": query_id, "label": label, "note": note.strip()[:1000],
              "ts": datetime.now().astimezone().isoformat(timespec="seconds")}
    with _lock:
        text = queries_path.read_text(encoding="utf-8")
        updated = set_reviewed(text, query_id, label == "T")
        if updated != text:
            queries_path.write_text(updated, encoding="utf-8")
        with open(reviews_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def apply_answer(query_id: str, keywords: list[str], queries_path: Path = QUERIES_PATH, chunks=None) -> dict:
    """Ghi từ khoá đáp án; trả về {keywords, missing}: `missing` là từ khoá không có nguyên văn
    trong chunk nào của địa danh đúng (vẫn ghi, chỉ cảnh báo)."""
    clean = []
    for kw in keywords:
        kw = str(kw).strip()
        if kw and kw not in clean:
            clean.append(kw[:200])
    if len(clean) > MAX_KEYWORDS:
        raise ValueError(f"tối đa {MAX_KEYWORDS} từ khoá")
    with _lock:
        text = queries_path.read_text(encoding="utf-8")
        updated = set_answer_keywords(text, query_id, clean)
        entry = next(q for q in yaml.safe_load(updated) if q["id"] == query_id)
        if list(entry.get("answer_keywords") or []) != clean:
            raise ValueError("ghi YAML không khớp, đã huỷ")
        if updated != text:
            queries_path.write_text(updated, encoding="utf-8")
    chunks = chunks if chunks is not None else load_chunks()
    keys = {f"wikidata:{r['qid']}" for r in entry["relevant"]}
    texts = [c.text.casefold() for c in chunks if c.place_key in keys]
    missing = [kw for kw in clean if not any(kw.casefold() in t for t in texts)]
    return {"keywords": clean, "missing": missing}


PAGE = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duyệt câu hỏi RAG</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e1dc;--t:#1f7a4d;--f:#b42318;--mark:#ffe58a;--src:#eef4ff}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1f1f23;--fg:#ececf0;--muted:#9a9aa3;--line:#33333a;--t:#3fb57a;--f:#f0645a;--mark:#6b5a12;--src:#1d2738}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:920px;margin:0 auto;padding:16px}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between;margin-bottom:12px}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;flex:1 1 200px}.bar>div{height:100%;background:var(--t)}
.stats{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:12px}
.meta{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}.pill{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
h1{font-size:20px;margin:4px 0 10px}
.status{font-weight:600}.status.T{color:var(--t)}.status.F{color:var(--f)}
.ev{border-top:1px solid var(--line);padding-top:10px;margin-top:10px}.ev.src{background:var(--src);border-radius:8px;padding:10px;border:0}
.ev h3{font-size:13px;margin:0 0 4px;color:var(--muted);font-weight:600}.ev p{margin:0;white-space:pre-wrap}
mark{background:var(--mark);color:inherit;border-radius:2px;padding:0 1px}
.keys{display:flex;flex-wrap:wrap;gap:8px;font-size:13px;color:var(--muted)}kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:0 5px;font:12px ui-monospace,monospace;color:var(--fg)}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.t{border-color:var(--t);color:var(--t)}button.f{border-color:var(--f);color:var(--f)}
#noteBox{display:none;margin-top:10px}#noteBox textarea{width:100%;min-height:70px;font:inherit;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
.check{font-size:13px;color:var(--muted);margin:0;padding-left:18px}
.warn{color:var(--f);font-size:13px}
</style></head><body><main>
<header><div class="stats" id="stats"></div><div class="bar"><div id="prog"></div></div></header>
<div class="card" id="card"></div>
<div class="card"><ul class="check">
<li><b>T (đúng)</b> khi: câu tự nhiên, rõ nghĩa; địa danh đúng là địa danh câu hỏi nói tới; từ khoá đáp án đúng và có trong đoạn; loại câu đúng.</li>
<li><b>F (sai)</b> khi một trong các điều trên không đạt — ghi ngắn lý do để sửa.</li></ul>
<div class="keys" style="margin-top:8px"><span><kbd>T</kbd> đúng</span><span><kbd>F</kbd> sai</span><span><kbd>A</kbd> sửa đáp án</span><span><kbd>U</kbd> bỏ nhãn</span><span><kbd>←</kbd>/<kbd>→</kbd> trước/sau</span><span><kbd>N</kbd> câu chưa duyệt</span></div></div>
</main>
<script>
let items=[], idx=0, noting=false;
const $=s=>document.querySelector(s);
const label=it=>it.review?it.review.label:null;
function highlight(text, terms){
  const frag=document.createDocumentFragment(); const low=text.toLowerCase();
  const ts=terms.filter(Boolean).map(t=>t.toLowerCase()); let i=0;
  while(i<text.length){
    let best=-1,len=0;
    for(const t of ts){const j=low.indexOf(t,i); if(j!==-1&&(best===-1||j<best||(j===best&&t.length>len))){best=j;len=t.length;}}
    if(best===-1){frag.append(text.slice(i));break;}
    frag.append(text.slice(i,best)); const m=document.createElement('mark'); m.textContent=text.slice(best,best+len); frag.append(m); i=best+len;
  }
  return frag;
}
function el(tag,cls,text){const e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e;}
function render(){
  const done=items.filter(it=>label(it)==='T').length, bad=items.filter(it=>label(it)==='F').length;
  $('#stats').textContent=`Câu ${idx+1}/${items.length} · đúng ${done} · sai ${bad} · chưa duyệt ${items.length-done-bad}`;
  $('#prog').style.width=`${100*(done+bad)/items.length}%`;
  const it=items[idx], card=$('#card'); card.replaceChildren();
  const meta=el('div','meta'); meta.append(el('span','pill',it.id), el('span','pill',it.type), el('span','pill',it.author));
  const st=label(it); meta.append(el('span','status '+(st||''), st==='T'?'✓ Đúng':st==='F'?'✗ Sai':'Chưa duyệt')); card.append(meta);
  card.append(el('h1',null,it.query));
  const rel=el('div',null); rel.append(el('b',null,'Địa danh đúng: '));
  rel.append(it.relevant.map(r=>`${r.name} (${r.qid})${r.has_chunks?'':' ⚠ không có chunk'}`).join(' · ')); card.append(rel);
  const ans=el('div',null); ans.append(el('b',null,'Đáp án: '));
  if(it.answer_keywords.length){ans.append(it.answer_keywords.join(' · '));}
  else if(it.type==='fact'){ans.append(el('span','warn','Thiếu đáp án — bấm A để nhập.'));}
  else{ans.append(`câu tìm địa danh, đáp án là chính địa danh: ${it.relevant.map(r=>r.name).join(' · ')}`);
       ans.append(el('span','stats',' (bấm A nếu muốn thêm từ khoá)'));}
  card.append(ans);
  if(it.answer_missing&&it.answer_missing.length){card.append(el('div','warn','Không có nguyên văn trong chunk nào của địa danh: '+it.answer_missing.join(' · ')));}
  if(it.review&&it.review.note){card.append(el('div','warn','Ghi chú lần trước: '+it.review.note));}
  const terms=[...it.answer_keywords, ...it.relevant.map(r=>r.name)];
  if(!it.evidence.length) card.append(el('p','warn','Không có đoạn bằng chứng nào trong kho cho địa danh này.'));
  for(const e of it.evidence){
    const box=el('div','ev'+(e.is_source?' src':'')); box.append(el('h3',null,`${e.is_source?'Đoạn nguồn · ':''}${e.title} — ${e.section||'Mở đầu'} [${e.chunk_id}]`));
    const p=el('p'); p.append(highlight(e.text,terms)); box.append(p); card.append(box);
  }
  const btns=el('div','btns');
  const bt=el('button','t','T · Đúng'); bt.onclick=()=>send('T'); const bf=el('button','f','F · Sai'); bf.onclick=openNote;
  const bu=el('button',null,'U · Bỏ nhãn'); bu.onclick=()=>send('U'); btns.append(bt,bf,bu); card.append(btns);
  const ba=el('button',null,'A · Sửa đáp án'); ba.onclick=openAnswer; btns.append(ba);
  const nb=el('div'); nb.id='noteBox'; const ta=el('textarea'); ta.placeholder='Lý do sai (Enter để lưu, Shift+Enter xuống dòng, Esc huỷ)'; nb.append(ta); card.append(nb);
  ta.addEventListener('keydown',ev=>{if(ev.key==='Enter'&&!ev.shiftKey){ev.preventDefault();send('F',ta.value);} if(ev.key==='Escape'){closeNote();}});
  const ab=el('div'); ab.id='answerBox'; ab.style.display='none'; ab.style.marginTop='10px';
  const ai=el('input'); ai.style.cssText='width:100%;font:inherit;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)';
  ai.placeholder='Từ khoá đáp án, cách nhau bằng | (vd: 7 tầng | 1844). Enter lưu, Esc huỷ, để trống để xoá';
  ab.append(ai); card.append(ab);
  ai.addEventListener('keydown',ev=>{if(ev.key==='Enter'){ev.preventDefault();saveAnswer(ai.value);} if(ev.key==='Escape'){noting=false;ab.style.display='none';}});
}
function openAnswer(){noting=true; const ab=$('#answerBox'); ab.style.display='block'; const ai=ab.querySelector('input'); ai.value=items[idx].answer_keywords.join(' | '); ai.focus();}
async function saveAnswer(v){
  const it=items[idx]; const kws=v.split('|').map(s=>s.trim()).filter(Boolean);
  const r=await fetch('/api/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:it.id,keywords:kws})});
  if(!r.ok){alert('Lưu thất bại: '+await r.text());return;}
  const d=await r.json(); it.answer_keywords=d.keywords; it.answer_missing=d.missing; noting=false; render();
}
function openNote(){noting=true; $('#noteBox').style.display='block'; const ta=$('#noteBox textarea'); ta.value=(items[idx].review&&items[idx].review.note)||''; ta.focus();}
function closeNote(){noting=false; $('#noteBox').style.display='none';}
async function send(lb,note=''){
  const it=items[idx];
  const r=await fetch('/api/label',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:it.id,label:lb,note})});
  if(!r.ok){alert('Lưu thất bại: '+await r.text());return;}
  it.review=await r.json(); noting=false;
  if(lb==='U'){render();return;}
  nextUnlabeled(true);
}
function nextUnlabeled(fromNext){
  for(let k=1;k<=items.length;k++){const j=(idx+k)%items.length; if(!label(items[j])){idx=j;render();return;}}
  if(fromNext&&idx<items.length-1)idx++; render();
}
// Chống giữ phím: bỏ qua phím tự lặp, và chỉ nhận nhãn sau MIN_DWELL ms kể từ khi câu hiện ra.
const MIN_DWELL=1500; let shownAt=0;
const _render=render; render=function(){_render(); shownAt=performance.now();};
function tooFast(){const left=MIN_DWELL-(performance.now()-shownAt); if(left>0){$('#stats').textContent+=` · đọc câu trước khi gán (${Math.ceil(left/100)/10}s)`;return true;} return false;}
document.addEventListener('keydown',ev=>{
  if(noting||ev.ctrlKey||ev.metaKey||ev.altKey||ev.repeat)return;
  const k=ev.key.toLowerCase();
  if((k==='t'||k==='f')&&tooFast())return;
  if(k==='t')send('T'); else if(k==='f'){ev.preventDefault();openNote();} else if(k==='u')send('U');
  else if(k==='a'){ev.preventDefault();openAnswer();}
  else if(k==='arrowright'){idx=Math.min(items.length-1,idx+1);render();} else if(k==='arrowleft'){idx=Math.max(0,idx-1);render();}
  else if(k==='n')nextUnlabeled(false);
});
fetch('/api/items').then(r=>r.json()).then(d=>{items=d; const j=items.findIndex(it=>!label(it)); idx=j===-1?0:j; render();});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    items: list[dict] = []

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/items":
            self._send(200, json.dumps(build_items(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        if self.path not in ("/api/label", "/api/answer"):
            return self._send(404, b"not found", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._send(413, b"body too large", "text/plain")
        try:
            data = json.loads(self.rfile.read(length))
            if self.path == "/api/label":
                record = apply_label(str(data["id"]), str(data["label"]), str(data.get("note") or ""))
            else:
                if not isinstance(data.get("keywords"), list):
                    raise ValueError("keywords phải là danh sách")
                record = apply_answer(str(data["id"]), data["keywords"])
        except (KeyError, ValueError, StopIteration, json.JSONDecodeError) as err:
            return self._send(400, str(err).encode("utf-8"), "text/plain; charset=utf-8")
        self._send(200, json.dumps(record, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    items = build_items()
    print(f"{len(items)} câu, {sum(1 for i in items if i['reviewed'])} đã duyệt. Mở {url} (Ctrl+C để dừng)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
