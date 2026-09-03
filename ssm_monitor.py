# -*- coding: utf-8 -*-
r"""
澳门卫生局(SSM)"书面询价"页面监控程序 v2
监控 https://www.ssm.gov.mo/tenderweb/TndLst.aspx
筛选: 标书编号含 /C/26(可配置年份) 且 物品类别为 藥物/醫療消耗品
发现新条目时发送邮件提醒。

注意: 2026-09-03 起目标站点启用 Cloudflare 人机验证(Cf-Mitigated: challenge),
标准库 urllib 的 TLS 指纹会被拦截(HTTP 403)。抓取层改用 curl_cffi
(模拟 Chrome 浏览器 TLS/JA3 指纹), 因此必须用装有 curl_cffi 的 venv 运行:
C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe
其余逻辑仍仅用标准库。

v2 变更(见 DEV_DOC.md):
- seen_ids.json → registry.json 持久化注册表(借鉴 synbio-daily-agent)
- 状态机 pending → sent: 发送成功才标记 sent, 失败下次自动重发
- 每日原始抓取落盘 data/raw_YYYY-MM-DD.json
- 发送前门禁(fail-closed)
- 邮件末尾追踪行
"""
import configparser
import html as html_lib
import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None
from email.header import Header
from email.mime.text import MIMEText

BASE = "https://www.ssm.gov.mo/tenderweb/"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 页面物品类别下拉框的值
CATEGORIES = {"M": "藥物", "2": "醫療消耗品"}

EXIT_FETCH_ERROR = 2
EXIT_SEND_ERROR = 3
EXIT_CONFIG_ERROR = 4


def get_year_pattern(cfg):
    """标书编号年份规则。配置为 auto 时自动匹配 当年/上一年 的 /C/YY 结尾, 免跨年维护。"""
    pat = cfg.get("filter", "tender_no_pattern", fallback="auto").strip()
    if pat.lower() == "auto":
        yy = datetime.now().year % 100
        return r"/C/(?:%02d|%02d)$" % (yy, (yy - 1) % 100)
    return pat


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(SCRIPT_DIR, "config.ini"), encoding="utf-8")
    # 借鉴 synbio-daily-agent: 密码优先取环境变量 SMTP_PASSWORD, 避免明文写在配置里
    env_pwd = os.getenv("SMTP_PASSWORD")
    if env_pwd:
        cfg.set("smtp", "password", env_pwd)
    return cfg


def setup_logging(cfg):
    root = logging.getLogger()
    if root.handlers:  # 避免 catchup 嵌套调用时重复添加 handler
        return
    log_file = os.path.join(SCRIPT_DIR, cfg.get("general", "log_file", fallback="monitor.log"))
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    root.addHandler(logging.StreamHandler(sys.stdout))


# ---------------- 注册表(借鉴 synbio-daily-agent 的 search_history entry 语义) ----------------

class Registry:
    """持久化去重注册表: 每条带 first/last seen、次数、状态(pending/sent)。"""

    def __init__(self, path):
        self.path = path
        self.entries = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fp:
                    data = json.load(fp)
                if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                    self.entries = data["entries"]
            except Exception as e:
                logging.error("注册表读取失败, 从空开始: %s", e)

    def migrate_from_seen_ids(self, seen_path):
        """一次性迁移旧 seen_ids.json(纯编号列表)为 sent 记录"""
        if self.entries or not os.path.exists(seen_path):
            return 0
        try:
            with open(seen_path, encoding="utf-8") as fp:
                ids = json.load(fp)
        except Exception:
            return 0
        today = datetime.now().strftime("%Y-%m-%d")
        for tid in ids:
            self.entries[str(tid)] = {
                "title": "", "category": "", "pub_date": "", "deadline": "",
                "first_seen_date": today, "last_seen_date": today,
                "seen_count": 1, "status": "sent", "last_reason": "migrated from seen_ids.json",
            }
        os.rename(seen_path, seen_path + ".bak")
        logging.info("已迁移旧 seen_ids.json: %d 条", len(ids))
        return len(ids)

    def classify(self, item):
        """登记条目并返回是否应进入本次提醒清单"""
        today = datetime.now().strftime("%Y-%m-%d")
        key = item["tender_no"]
        e = self.entries.get(key)
        if not isinstance(e, dict):
            e = {
                "title": item["title"], "category": item["category"],
                "pub_date": item["pub_date"], "deadline": "%s %s" % (item["deadline"], item["time"]),
                "first_seen_date": today, "last_seen_date": today,
                "seen_count": 1, "status": "pending", "last_reason": "",
            }
            self.entries[key] = e
            return True  # 新增
        e["last_seen_date"] = today
        e["seen_count"] = int(e.get("seen_count") or 0) + 1
        # 回填/更新展示字段(迁移的旧记录这些字段为空)
        if not e.get("title"):
            e["title"] = item["title"]
        if not e.get("category"):
            e["category"] = item["category"]
        if not e.get("pub_date"):
            e["pub_date"] = item["pub_date"]
        if not e.get("deadline"):
            e["deadline"] = "%s %s" % (item["deadline"], item["time"])
        return e.get("status") == "pending"  # 上次发送失败 → 重试

    def mark_sent(self, keys):
        for k in keys:
            if k in self.entries:
                self.entries[k]["status"] = "sent"
                self.entries[k]["last_reason"] = ""

    def mark_failed(self, keys, reason):
        for k in keys:
            if k in self.entries:
                self.entries[k]["status"] = "pending"
                self.entries[k]["last_reason"] = str(reason)[:300]

    def save(self):
        payload = {"version": 1, "entries": self.entries}
        with open(self.path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=1)


# ---------------- 抓取(curl_cffi 模拟 Chrome 指纹, 通过 Cloudflare 验证) ----------------

class SsmClient:
    def __init__(self):
        if cffi_requests is None:
            raise RuntimeError(
                "缺少 curl_cffi 依赖(站点已启用 Cloudflare 验证, 标准库无法通过)。"
                "请用 venv 运行: C:\\Users\\Administrator\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe"
            )
        # impersonate 自带匹配的 UA/Sec-CH-UA/TLS 指纹, 不要再手动覆盖 UA
        self.session = cffi_requests.Session(impersonate="chrome")

    def req(self, url, fields=None, ref=None):
        h = {"Referer": ref} if ref else None
        last_err = None
        for attempt in range(3):  # 瞬时网络抖动就地重试, 最多 3 次
            try:
                if fields is not None:
                    r = self.session.post(url, data=fields, headers=h, timeout=30)
                else:
                    r = self.session.get(url, headers=h, timeout=30)
                r.raise_for_status()
                return r.content.decode("utf-8", errors="ignore")
            except Exception as e:
                last_err = e
                logging.warning("请求失败(第%d次): %s %s", attempt + 1, url, e)
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
        raise last_err

    @staticmethod
    def all_fields(html):
        """提取表单全部字段(含 __VIEWSTATEENCRYPTED 等隐藏域),模拟浏览器提交。"""
        m = re.search(r"<form.*?</form>", html, re.S)
        if not m:
            raise RuntimeError("页面中未找到表单")
        form = m.group(0)
        fields = []
        for mm in re.finditer(r"<input[^>]*>", form):
            tag = mm.group(0)
            name = re.search(r'name="([^"]+)"', tag)
            typ = re.search(r'type="([^"]+)"', tag)
            val = re.search(r'value="([^"]*)"', tag)
            if not name:
                continue
            t = typ.group(1) if typ else "text"
            if t in ("submit", "image", "button", "reset"):
                continue
            fields.append([name.group(1), val.group(1) if val else ""])
        for mm in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', form, re.S):
            sel = re.search(r'<option selected="selected" value="([^"]*)"', mm.group(2))
            first = re.search(r'<option value="([^"]*)"', mm.group(2))
            fields.append([mm.group(1), sel.group(1) if sel else (first.group(1) if first else "")])
        return fields

    def enter_written_quotation(self):
        """走会话流程: 菜单页 -> 点击"書面詢價" -> 进入列表页"""
        menu = self.req(BASE + "TndNotes_C.aspx")
        f = dict(self.all_fields(menu))
        f["__EVENTTARGET"] = "ConsultaEscritaC"
        f["__EVENTARGUMENT"] = ""
        self.req(BASE + "TndMain.aspx", list(f.items()))
        return self.req(BASE + "TndLst.aspx")

    @staticmethod
    def parse_rows(html):
        """解析列表行: 公布日期/标书编号/标题/截标日期/时间/物品类别 + 查询按钮的 Select$N"""
        rows = re.findall(
            r"<td>\s*<span[^>]*>(\d{4}\.\d{2}\.\d{2})</span>\s*</td>"
            r"<td>([^<]+)</td>"
            r"<td[^>]*>(.*?)</td>"
            r"<td>\s*<span[^>]*>(\d{4}\.\d{2}\.\d{2})</span>\s*</td>"
            r"<td>\s*<span[^>]*>([\d:]+)</span>\s*</td>"
            r"<td>\s*<span[^>]*>([^<]*)</span>\s*</td>"
            r"<td><input[^>]*Select\$(\d+)",
            html,
        )
        return [
            {
                "pub_date": r[0],
                "tender_no": r[1].strip(),
                "title": html_lib.unescape(re.sub(r"\s+", " ", r[2])).strip(),
                "deadline": r[3],
                "time": r[4],
                "category": r[5].strip(),
                "sel": int(r[6]),
            }
            for r in rows
        ]

    def fetch_category(self, list_html, cat_value):
        """按物品类别筛选并翻页抓取全部行(每行记录所在页码 page 和行内序号 sel)"""
        f = self.all_fields(list_html)
        d = dict(f)
        d["ctl00$ContentPlaceHolder1$DDLstTyp"] = cat_value
        d["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$DDLstTyp"
        d["__EVENTARGUMENT"] = ""
        html = self.req(BASE + "TndLst.aspx", list(d.items()), ref=BASE + "TndLst.aspx")
        rows = self.parse_rows(html)
        for r in rows:
            r["page"] = 1
        page = 2
        while True:
            if "Page$%d" % page not in html:
                break
            f2 = self.all_fields(html)
            d2 = dict(f2)
            d2["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$GridView1"
            d2["__EVENTARGUMENT"] = "Page$%d" % page
            html = self.req(BASE + "TndLst.aspx", list(d2.items()), ref=BASE + "TndLst.aspx")
            new_rows = self.parse_rows(html)
            for r in new_rows:
                r["page"] = page
            rows.extend(new_rows)
            page += 1
        return rows

    def fetch_detail(self, cat_value, row):
        """进入指定条目的详情页, 抓取 基本信息/項目清單/諮詢條款/附件/修正補充 全部内容"""
        # 服务器会话状态以最后一次回传为准, 重新定位: 列表 → 筛选类别 → 翻到所在页 → 点查詢
        lst = self.enter_written_quotation()
        f = self.all_fields(lst)
        d = dict(f)
        d["ctl00$ContentPlaceHolder1$DDLstTyp"] = cat_value
        d["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$DDLstTyp"
        d["__EVENTARGUMENT"] = ""
        html = self.req(BASE + "TndLst.aspx", list(d.items()), ref=BASE + "TndLst.aspx")
        if row.get("page", 1) > 1:
            f = self.all_fields(html)
            d = dict(f)
            d["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$GridView1"
            d["__EVENTARGUMENT"] = "Page$%d" % row["page"]
            html = self.req(BASE + "TndLst.aspx", list(d.items()), ref=BASE + "TndLst.aspx")
        f = self.all_fields(html)
        d = dict(f)
        d["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$GridView1"
        d["__EVENTARGUMENT"] = "Select$%d" % row["sel"]
        info_html = self.req(BASE + "TndLst.aspx", list(d.items()), ref=BASE + "TndLst.aspx")
        detail = {"info": parse_info(info_html)}
        for tab, parser in [("ItmLst", parse_itmlst), ("Rules", parse_rules_links),
                            ("Attach", parse_attach), ("Enc", parse_enc)]:
            f = self.all_fields(info_html)
            d = dict(f)
            d["ctl00$ImgBut_%s.x" % tab] = "1"
            d["ctl00$ImgBut_%s.y" % tab] = "1"
            resp = self.req(BASE + "TndInfo.aspx", list(d.items()), ref=BASE + "TndInfo.aspx")
            detail[tab] = parser(resp)
        return detail


# ---------------- 详情页解析(点"查詢"后的 TndInfo 各选项卡) ----------------

def _clean(t):
    t = re.sub(r"<script.*?</script>", "", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ")
    return re.sub(r"[ \t]+", " ", t).strip()


def parse_info(html):
    """基本信息页: 標書編號/招標標題/截標日期及時間/物品類別/遞交標書地點"""
    text = _clean(re.sub(r"<(br|/td|/tr)[^>]*>", "\n", html))
    def grab(label):
        m = re.search(label + r"\s*:\s*\n?\s*([^\n]+)", text)
        return m.group(1).strip() if m else ""
    return {
        "編號": grab("標書編號"), "標題": grab("招標標題"), "截標": grab("截標日期及時間"),
        "類別": grab("物品類別"), "遞交地點": grab("遞交標書地點"),
    }


def parse_itmlst(html):
    """項目清單: 項目位置/物品編號/物品名稱/單位/招標數量(首列空的是上一行的续行: 葡文名或备注)"""
    i = html.find("物品編號")
    if i < 0:
        return []
    tbl = html[html.rfind("<table", 0, i):html.find("</table>", i)]
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S)
    items, cur = [], None
    for r in rows[1:]:
        tds = [_clean(t) for t in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
        if len(tds) < 5:
            continue
        if tds[0]:
            cur = {"位置": tds[0], "編號": tds[1], "名稱": tds[2], "單位": tds[3], "數量": tds[4], "備註": []}
            items.append(cur)
        elif cur is not None and tds[2]:
            cur["備註"].append(tds[2])
    return items


def parse_rules_links(html):
    """諮詢條款页: PDF 直链"""
    return [BASE + l for l in re.findall(r'href="(Data/Tender[^"]+)"', html)]


def parse_attach(html):
    """修正及補充資料页"""
    if "沒有修正及補充資料" in _clean(html):
        return "沒有修正及補充資料"
    return "(有修正及補充資料, 请登录网站查看)"


def parse_enc(html):
    """附件页: 公佈日期/公佈時間/概述"""
    m = re.search(r'id="ctl00_ContentPlaceHolder1_GridView2".*?</table>', html, re.S)
    if not m:
        return []
    out = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.S)[1:]:
        tds = [_clean(t) for t in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
        if len(tds) >= 3:
            out.append({"日期": tds[0], "時間": tds[1], "概述": tds[2]})
    return out


# ---------------- Word 文档生成(纯标准库, docx = zip + XML) ----------------

def _xml_esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _docx_p(text, bold=False, size=None):
    rpr = "<w:rPr>%s%s</w:rPr>" % (
        "<w:b/>" if bold else "",
        '<w:sz w:val="%d"/><w:szCs w:val="%d"/>' % (size, size) if size else "",
    )
    return '<w:p><w:r>%s<w:t xml:space="preserve">%s</w:t></w:r></w:p>' % (rpr, _xml_esc(text))


def _docx_tbl(rows):
    borders = "".join('<w:%s w:val="single" w:sz="4" w:space="0" w:color="auto"/>' % b
                      for b in ("top", "left", "bottom", "right", "insideH", "insideV"))
    out = ['<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>%s</w:tblBorders></w:tblPr>' % borders]
    for ri, row in enumerate(rows):
        out.append("<w:tr>")
        for cell in row:
            out.append('<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>%s</w:tc>'
                       % _docx_p(cell, bold=(ri == 0)))
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def write_docx(path, blocks):
    """生成最小可用 .docx(blocks 为 _docx_p/_docx_tbl 的输出)"""
    import zipfile
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body>%s<w:sectPr/></w:body></w:document>" % "".join(blocks))
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)


def build_detail_docx(item, detail, out_dir):
    """把单个标书的详情写成 Word 文档, 返回文件路径"""
    info = detail["info"]
    blocks = [
        _docx_p("標書詳情: %s" % info["編號"], bold=True, size=32),
        _docx_p(""),
        _docx_p("招標標題: %s" % info["標題"], bold=True),
        _docx_p("截標日期及時間: %s" % info["截標"]),
        _docx_p("物品類別: %s" % info["類別"]),
        _docx_p("遞交標書地點: %s" % info["遞交地點"]),
        _docx_p("公佈日期: %s" % item.get("pub_date", "")),
        _docx_p(""),
        _docx_p("項目清單(共 %d 項)" % len(detail["ItmLst"]), bold=True, size=28),
    ]
    tbl = [["位置", "物品編號", "物品名稱", "單位", "招標數量"]]
    for it in detail["ItmLst"]:
        tbl.append([it["位置"], it["編號"], it["名稱"], it["單位"], it["數量"]])
        for note in it["備註"]:
            tbl.append(["", "", note, "", ""])
    if len(tbl) > 1:
        blocks.append(_docx_tbl(tbl))
    else:
        blocks.append(_docx_p("(无项目清单数据)"))
    blocks += [_docx_p(""), _docx_p("諮詢條款(承投規則)", bold=True, size=28)]
    if detail["Rules"]:
        blocks += [_docx_p(u) for u in detail["Rules"]]
    else:
        blocks.append(_docx_p("(无)"))
    blocks += [_docx_p(""), _docx_p("附件", bold=True, size=28)]
    if detail["Enc"]:
        blocks.append(_docx_tbl([["公佈日期", "公佈時間", "概述"]] +
                                [[e["日期"], e["時間"], e["概述"]] for e in detail["Enc"]]))
    else:
        blocks.append(_docx_p("(无)"))
    blocks += [_docx_p(""), _docx_p("修正及補充資料", bold=True, size=28),
               _docx_p(detail["Attach"]),
               _docx_p(""),
               _docx_p("抓取时间: %s | 来源: https://www.ssm.gov.mo/tenderweb/TndLst.aspx"
                       % datetime.now().strftime("%Y-%m-%d %H:%M"))]
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "tender_detail_%s.docx" % info["編號"].replace("/", "-"))
    write_docx(path, blocks)
    return path


# ---------------- 邮件 ----------------

def format_email(new_items, retry_count, stats, page_url, year_pat):
    today = datetime.now().strftime("%Y-%m-%d")
    subject = "【卫生局书面询价提醒】%s 新增 %d 条(药物/医疗消耗品)" % (today, len(new_items))
    lines = [
        "监控页面: %s" % page_url,
        "筛选条件: 标书编号匹配 \"%s\",物品类别为「藥物」或「醫療消耗品」" % year_pat,
        "本次提醒 %d 条%s:" % (len(new_items), "(其中 %d 条为此前发送失败重试)" % retry_count if retry_count else ""),
        "",
    ]
    for i, it in enumerate(new_items, 1):
        lines += [
            "%d. 标书编号: %s" % (i, it["tender_no"]),
            "   标题: %s" % it["title"],
            "   物品类别: %s" % it["category"],
            "   公布日期: %s" % it["pub_date"],
            "   截标日期: %s %s" % (it["deadline"], it["time"]),
            "",
        ]
    lines.append("请及时登录卫生局网站查看详情并准备标书。")
    lines.append("")
    lines.append("--- 追踪: 抓取 %d 条(藥物 %d / 醫療消耗品 %d) → 符合条件 %d 条 → 本次提醒 %d 条 ---" % stats)
    return subject, "\n".join(lines)


def send_email(cfg, subject, body, attachments=None):
    host = cfg.get("smtp", "host")
    port = cfg.getint("smtp", "port", fallback=465)
    user = cfg.get("smtp", "user")
    password = cfg.get("smtp", "password")
    mail_from = cfg.get("smtp", "mail_from", fallback=user)
    mail_to = [a.strip() for a in cfg.get("smtp", "mail_to").split(",") if a.strip()]
    use_ssl = cfg.getboolean("smtp", "use_ssl", fallback=True)

    if attachments:
        from email.mime.application import MIMEApplication
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, "plain", "utf-8"))
        for path in attachments:
            with open(path, "rb") as fp:
                att = MIMEApplication(
                    fp.read(),
                    _subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            # 文件名用 ASCII(编号命名), 避免邮件头编码兼容问题
            att.add_header("Content-Disposition", "attachment",
                           filename=os.path.basename(path))
            msg.attach(att)
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = mail_from
    msg["To"] = ", ".join(mail_to)

    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    server.login(user, password)
    server.sendmail(mail_from, mail_to, msg.as_string())
    server.quit()


def smtp_config_ok(cfg):
    """门禁: SMTP 配置仍为占位符时阻断发送"""
    user = cfg.get("smtp", "user", fallback="")
    password = cfg.get("smtp", "password", fallback="")
    mail_to = cfg.get("smtp", "mail_to", fallback="")
    bad = ("your_email" in user or "在此填" in password or "example.com" in mail_to)
    return not bad


# ---------------- 周报模式(每周五 17:00) ----------------

def weekly_report(cfg):
    """汇总本周监控情况并发送周报邮件。数据来源: registry.json + monitor.log, 不做新抓取。"""
    today = datetime.now().date()
    week_start = today.fromordinal(today.toordinal() - today.weekday())  # 本周一
    ws = week_start.strftime("%Y-%m-%d")
    ts = today.strftime("%Y-%m-%d")

    registry = Registry(os.path.join(SCRIPT_DIR, cfg.get("general", "registry_file", fallback="registry.json")))
    entries = registry.entries

    # 本周新增(first_seen 在本周)
    new_this_week = [(k, e) for k, e in entries.items()
                     if isinstance(e, dict) and ws <= str(e.get("first_seen_date", ""))[:10] <= ts]
    new_this_week.sort(key=lambda kv: kv[0])

    # 状态统计
    n_sent = sum(1 for e in entries.values() if e.get("status") == "sent")
    n_pending = sum(1 for e in entries.values() if e.get("status") == "pending")

    # 未来7天截标(deadline 格式 "2026.09.11 17:30")
    upcoming = []
    limit = today.fromordinal(today.toordinal() + 7)
    for k, e in entries.items():
        dl = str(e.get("deadline", ""))[:10].replace(".", "-")
        try:
            d = datetime.strptime(dl, "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= d <= limit:
            upcoming.append((d, k, e))
    upcoming.sort()

    # 本周运行日志统计
    log_file = os.path.join(SCRIPT_DIR, cfg.get("general", "log_file", fallback="monitor.log"))
    runs, errors = 0, 0
    if os.path.exists(log_file):
        with open(log_file, encoding="utf-8", errors="ignore") as fp:
            for line in fp:
                day = line[:10]
                if ws <= day <= ts:
                    if "开始检查" in line:
                        runs += 1
                    elif " ERROR " in line:
                        errors += 1

    subject = "【卫生局书面询价·周报】%s ~ %s 监控正常,本周新增 %d 条" % (ws, ts, len(new_this_week))
    lines = [
        "监控页面: %s" % (BASE + "TndLst.aspx"),
        "统计周期: %s(周一) ~ %s" % (ws, ts),
        "",
        "一、本周运行情况",
        "  监控运行: %d 次,异常: %d 次" % (runs, errors),
        "  累计监控条目: %d 条(已提醒 %d / 待重发 %d)" % (len(entries), n_sent, n_pending),
        "",
        "二、本周新增条目(%d 条)" % len(new_this_week),
    ]
    if new_this_week:
        for i, (k, e) in enumerate(new_this_week, 1):
            lines.append("  %d. %s | %s | %s | 截标 %s" % (
                i, k, e.get("title", ""), e.get("category", ""), e.get("deadline", "")))
    else:
        lines.append("  (本周无新增)")
    lines += ["", "三、未来7天截标提醒(%d 条)" % len(upcoming)]
    if upcoming:
        for d, k, e in upcoming:
            lines.append("  %s 截标 | %s | %s" % (d.strftime("%Y-%m-%d"), k, e.get("title", "")))
    else:
        lines.append("  (未来7天无截标)")
    lines += ["", "系统运行正常。每日 09:00 自动监控,本邮件为每周五 17:00 例行周报。"]
    body = "\n".join(lines)

    dry_run = cfg.getboolean("general", "dry_run", fallback=False)
    if dry_run:
        print("=" * 60)
        print("DRY-RUN 周报预览:")
        print("Subject:", subject)
        print(body)
        print("=" * 60)
        return 0
    if not smtp_config_ok(cfg):
        logging.error("SMTP 配置仍为占位符, 周报未发送")
        return EXIT_CONFIG_ERROR
    try:
        send_email(cfg, subject, body)
        logging.info("周报已发送: %s", subject)
        return 0
    except Exception as e:
        logging.error("周报发送失败: %s", e)
        return EXIT_SEND_ERROR


# ---------------- 补发机制(catch-up) ----------------

DAILY_RUN_HOUR = 9  # 日常监控计划时间


def state_file_path():
    return os.path.join(SCRIPT_DIR, "state.json")


def read_state():
    try:
        with open(state_file_path(), encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {}


def write_state(**kv):
    st = read_state()
    st.update(kv)
    with open(state_file_path(), "w", encoding="utf-8") as fp:
        json.dump(st, fp, ensure_ascii=False, indent=1)


def catchup_check():
    """看门狗: 若今天 09:00 的日常监控未成功执行(电脑/WorkBuddy 没开), 则立即补跑一次。
    无事可做时静默退出(exit 0)。

    防刷屏设计: 当天一旦确认"已跑过 / 无需补发"(无论是 09:00 主监控成功,
    还是 catchup 自己补跑成功), 就写入 state.json 的 last_catchup_checked=今天;
    之后当天内所有触发直接静默返回, 不再启动抓取/发信, 直到次日自动恢复检查。"""
    today = datetime.now().strftime("%Y-%m-%d")
    st = read_state()
    # 今天已经确认过(已跑过或已补跑成功) -> 直接静默退出, 不再任何动作
    if str(st.get("last_catchup_checked", "")) == today:
        return 0
    last_run = str(st.get("last_daily_run", ""))
    if last_run >= today:
        # 09:00 主监控今日已成功执行 -> 确定没漏发, 标记今日已确认后静默退出
        write_state(last_catchup_checked=today)
        return 0
    now = datetime.now()
    if now.hour < DAILY_RUN_HOUR:
        return 0  # 还没到今天的计划时间, 不标记(避免提前压制导致漏检)
    # 并发安全由单实例锁保证, 因此 09 点整点也可直接补发(覆盖"9点后才开机"的场景)
    logging.info("检测到今日 %02d:00 日常监控未执行(last_daily_run=%s), 立即补发", DAILY_RUN_HOUR, last_run or "无")
    main()
    # 补跑成功后 _main 会把 last_daily_run 写为今天; 标记今日已处理, 避免当天重复补跑刷屏
    if str(read_state().get("last_daily_run", "")) >= today:
        write_state(last_catchup_checked=today)


# ---------------- 单实例锁(防止多个触发器并发导致重复发信) ----------------

LOCK_FILE = os.path.join(SCRIPT_DIR, ".monitor.lock")
LOCK_TTL = 900  # 锁有效期 15 分钟, 超时视为上次异常退出, 自动释放


def acquire_lock():
    """获取运行权。False = 已有实例正在运行, 本次应静默跳过。"""
    try:
        if time.time() - os.path.getmtime(LOCK_FILE) < LOCK_TTL:
            return False
    except OSError:
        pass
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as fp:
            fp.write("%d %s" % (os.getpid(), datetime.now().isoformat()))
        return True
    except OSError:
        return False


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


# ---------------- 主流程(fail-closed) ----------------

def main():
    """包装 _main: 加单实例锁, 防止 09:00 多个触发器(自动化+计划任务)并发重复发信。"""
    if not acquire_lock():
        logging.info("已有实例在运行(锁未释放), 本次跳过")
        return
    try:
        _main()
    finally:
        release_lock()


def _main():
    cfg = load_config()
    setup_logging(cfg)
    page_url = BASE + "TndLst.aspx"
    year_pat = get_year_pattern(cfg)
    dry_run = cfg.getboolean("general", "dry_run", fallback=False)
    registry_path = os.path.join(SCRIPT_DIR, cfg.get("general", "registry_file", fallback="registry.json"))
    seen_path = os.path.join(SCRIPT_DIR, cfg.get("general", "seen_file", fallback="seen_ids.json"))

    registry = Registry(registry_path)
    registry.migrate_from_seen_ids(seen_path)

    logging.info("开始检查 %s", page_url)
    client = SsmClient()
    try:
        lst = client.enter_written_quotation()
        raw = {}
        all_rows = []
        for cat_value, cat_name in CATEGORIES.items():
            rows = client.fetch_category(lst, cat_value)
            logging.info("类别 %s 共 %d 行", cat_name, len(rows))
            raw[cat_name] = rows
            all_rows.extend(rows)
    except Exception as e:
        logging.error("抓取失败: %s", e)
        sys.exit(EXIT_FETCH_ERROR)

    # 门禁: 两类别总行数为 0 → 页面结构可能变更, fail-closed
    if not all_rows:
        logging.error("抓取总行数为 0, 疑似页面结构变更, 本次不更新状态不发送邮件")
        sys.exit(EXIT_FETCH_ERROR)

    # 每日原始数据落盘(可审计)
    data_dir = os.path.join(SCRIPT_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    raw_path = os.path.join(data_dir, "raw_%s.json" % datetime.now().strftime("%Y-%m-%d"))
    with open(raw_path, "w", encoding="utf-8") as fp:
        json.dump(raw, fp, ensure_ascii=False, indent=1)

    # 筛选: 编号匹配 + 类别在监控范围内 + 按编号去重
    matched = {}
    for r in all_rows:
        if re.search(year_pat, r["tender_no"]) and r["category"] in CATEGORIES.values():
            matched[r["tender_no"]] = r

    to_notify, retry_count = [], 0
    for r in matched.values():
        is_pending = registry.entries.get(r["tender_no"], {}).get("status") == "pending"
        if registry.classify(r):
            to_notify.append(r)
            if is_pending:
                retry_count += 1

    n_drug = len(raw.get("藥物", []))
    n_cons = len(raw.get("醫療消耗品", []))
    stats = (len(all_rows), n_drug, n_cons, len(matched), len(to_notify))
    logging.info("抓取 %d 条 → 符合条件 %d 条 → 本次提醒 %d 条(重试 %d)",
                 stats[0], stats[3], stats[4], retry_count)

    if to_notify:
        # 为每个新增条目抓详情并生成 Word 文档(单个失败不阻断其他)
        cat_name2value = {v: k for k, v in CATEGORIES.items()}
        docs_dir = os.path.join(SCRIPT_DIR, "data", "docs")
        attachments = []
        for r in to_notify:
            try:
                detail = client.fetch_detail(cat_name2value[r["category"]], r)
                attachments.append(build_detail_docx(r, detail, docs_dir))
                logging.info("详情文档已生成: %s (%d 項)", r["tender_no"], len(detail["ItmLst"]))
            except Exception as e:
                logging.warning("详情抓取失败 %s: %s (邮件照常发送, 仅缺该附件)", r["tender_no"], e)

        subject, body = format_email(to_notify, retry_count, stats, page_url, year_pat)
        if attachments:
            body += "\n\n附件: %d 份标书详情 Word 文档(含項目清單/諮詢條款鏈接/附件列表)。" % len(attachments)
        if dry_run:
            print("=" * 60)
            print("DRY-RUN 模式,以下为将发送的邮件内容:")
            print("Subject:", subject)
            print(body)
            print("附件:", attachments)
            print("=" * 60)
            # dry_run 视为已发送(用于建立基线)
            registry.mark_sent([r["tender_no"] for r in to_notify])
        else:
            if not smtp_config_ok(cfg):
                reason = "SMTP 配置仍为占位符, 请填写 config.ini 的 [smtp] 段"
                registry.mark_failed([r["tender_no"] for r in to_notify], reason)
                registry.save()
                logging.error(reason)
                sys.exit(EXIT_CONFIG_ERROR)
            try:
                send_email(cfg, subject, body, attachments=attachments)
                registry.mark_sent([r["tender_no"] for r in to_notify])
                logging.info("邮件已发送: %s (附件 %d 份)", subject, len(attachments))
            except Exception as e:
                registry.mark_failed([r["tender_no"] for r in to_notify], e)
                registry.save()
                logging.error("邮件发送失败: %s (条目保持 pending, 下次运行将重发)", e)
                sys.exit(EXIT_SEND_ERROR)

    registry.save()
    write_state(last_daily_run=datetime.now().strftime("%Y-%m-%d"))
    logging.info("完成。")


if __name__ == "__main__":
    if "--weekly" in sys.argv:
        _cfg = load_config()
        setup_logging(_cfg)
        sys.exit(weekly_report(_cfg))
    if "--catchup" in sys.argv:
        _cfg = load_config()
        setup_logging(_cfg)
        sys.exit(catchup_check())
    main()
