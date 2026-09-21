"""生成演示数据集：一个完整的信贷决策案件。

构造的是一个"看起来像真的"的跨域案件，因为它必须能验证跨文档多跳推理：

    甲科技有限公司（授信申请人）
      └─ 实际控制人 张三
           ├─ 控股 → 乙贸易有限公司（社保断缴 + 行政处罚）
           └─ 控股 → 丙建材有限公司（司法涉诉）
    甲科技自身：财报稳健，但社保缴纳人数与营收规模不匹配（用工合理性存疑）

推理链要求：从"甲科技申请授信"出发，经实控人张三，跨到乙/丙的风险信号，
再沿 Layer2 规则条款映射回甲的"经营稳定性"维度——一条完整的 4-5 跳路径。

所有数据均为合成，不含任何真实企业或个人信息。
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"

COMPANIES = {
    "jia": {
        "name": "甲科技有限公司",
        "uscc": "91310115MA1K3XYA01",
        "capital": 5000.0,
        "established": "2018-03-12",
        "industry": "软件和信息技术服务业",
        "status": "存续",
        "address": "上海市浦东新区张江路 88 号 12 幢 5 层",
        "scope": "计算机软件开发；信息系统集成服务；数据处理服务；技术咨询",
        "revenue": 8600.0,
        "net_profit": 720.0,
        "debt_ratio": 0.42,
        "employees": 46,
    },
    "yi": {
        "name": "乙贸易有限公司",
        "uscc": "91310115MA1K3XYB02",
        "capital": 800.0,
        "established": "2019-07-25",
        "industry": "批发业",
        "status": "存续",
        "address": "上海市浦东新区川沙路 199 号 3 层",
        "scope": "日用百货销售；金属材料销售；货物进出口",
        "revenue": 2100.0,
        "net_profit": 35.0,
        "debt_ratio": 0.71,
        "employees": 12,
    },
    "bing": {
        "name": "丙建材有限公司",
        "uscc": "91310115MA1K3XYC03",
        "capital": 1200.0,
        "established": "2017-11-08",
        "industry": "非金属矿物制品业",
        "status": "存续",
        "address": "上海市青浦区华新镇工业路 55 号",
        "scope": "建筑材料制造；水泥制品销售；道路货物运输",
        "revenue": 3400.0,
        "net_profit": 96.0,
        "debt_ratio": 0.63,
        "employees": 28,
    },
}

CONTROLLER = {"name": "张三", "id_card": "310101198705123456"}

# 监管条款：Layer 2 规则图的内容来源
REGULATIONS = [
    {
        "clause_id": "银保监发〔2024〕12号-§3.2",
        "title": "中小企业授信风险指引 第 3.2 条",
        "text": (
            "商业银行对中小企业授信时，应当穿透识别实际控制人及其关联方，"
            "对关联方存在社保欠缴、行政处罚、重大涉诉等情形的，"
            "应当将该情形纳入借款人经营稳定性评估，并相应下调授信评级。"
        ),
        "clarity": 0.88,
        "dimension": "经营稳定性",
        "applicable": "企业",
    },
    {
        "clause_id": "银保监发〔2024〕12号-§4.1",
        "title": "中小企业授信风险指引 第 4.1 条",
        "text": (
            "授信审查应当核验借款人用工规模与营业收入的匹配性。"
            "参保人数连续三个月低于同行业同规模企业合理区间的，"
            "应当要求借款人补充说明并审慎核定授信额度。"
        ),
        "clarity": 0.81,
        "dimension": "经营稳定性",
        "applicable": "企业",
    },
    {
        "clause_id": "银保监发〔2023〕45号-§2.4",
        "title": "流动资金贷款管理办法 第 2.4 条",
        "text": (
            "流动资金贷款额度不得超过借款人营运资金需求量。"
            "借款人资产负债率高于百分之七十的，应当审慎确定贷款额度并追加担保。"
        ),
        "clarity": 0.93,
        "dimension": "偿债能力",
        "applicable": "企业",
    },
    {
        "clause_id": "沪科创办〔2025〕7号-§1.3",
        "title": "上海市科技型企业信贷扶持政策 第 1.3 条",
        "text": (
            "对注册在本市的科技型中小企业，主营业务收入连续两年增长且"
            "无重大失信记录的，可给予不超过同期 LPR 加 50 个基点的优惠利率支持。"
        ),
        "clarity": 0.74,
        "dimension": "经营稳定性",
        "applicable": "企业",
    },
    {
        "clause_id": "数据共享规范〔2024〕3号-§5.1",
        "title": "政务数据共享使用规范 第 5.1 条",
        "text": (
            "金融机构查询政务共享数据应当基于企业明确授权，"
            "查询记录应当留存不少于三年，不得用于授权范围外的用途。"
        ),
        "clarity": 0.9,
        "dimension": "合规性",
        "applicable": "企业",
    },
]

SOCIAL_SECURITY = [
    # (企业, 月份, 实缴人数, 缴纳状态, 基数)
    ("yi", "2025-11", 12, "正常", 8200.0),
    ("yi", "2025-12", 12, "正常", 8200.0),
    ("yi", "2026-01", 4, "欠缴", 8200.0),
    ("yi", "2026-02", 2, "欠缴", 8200.0),
    ("yi", "2026-03", 1, "断缴", 8200.0),
    ("jia", "2026-01", 46, "正常", 12800.0),
    ("jia", "2026-02", 46, "正常", 12800.0),
    ("jia", "2026-03", 45, "正常", 12800.0),
    ("bing", "2026-02", 28, "正常", 9600.0),
    ("bing", "2026-03", 26, "正常", 9600.0),
]

PENALTIES = [
    {
        "record_id": "CF-2026-03-0071",
        "company": "yi",
        "reason": "未按规定报送年度报告，违反《企业信息公示暂行条例》第八条",
        "amount": 3.0,
        "date": "2026-03-18",
        "authority": "上海市浦东新区市场监督管理局",
    },
]

LITIGATIONS = [
    {
        "record_id": "SF-2026-01-0233",
        "company": "bing",
        "cause": "买卖合同纠纷",
        "amount": 186.5,
        "filing_date": "2026-01-14",
        "status": "审理中",
        "court": "上海市青浦区人民法院",
    },
]

CREDIT_QUERIES = [
    {"company": "jia", "inquiries_6m": 2, "overdue": 0, "debt": 3600.0},
    {"company": "yi", "inquiries_6m": 9, "overdue": 3, "debt": 1480.0},
    {"company": "bing", "inquiries_6m": 4, "overdue": 0, "debt": 2140.0},
]


def write_json_files(root: Path) -> list[Path]:
    written: list[Path] = []

    license_records = [
        {
            "记录编号": f"GS-2026-{idx:04d}",
            "统一社会信用代码": c["uscc"],
            "企业名称": c["name"],
            "类型": "有限责任公司（自然人投资或控股）",
            "法定代表人": CONTROLLER["name"],
            "注册资本": c["capital"],
            "成立日期": c["established"],
            "登记状态": c["status"],
            "所属行业": c["industry"],
            "住所": c["address"],
            "经营范围": c["scope"],
        }
        for idx, c in enumerate(COMPANIES.values(), start=1)
    ]
    path = root / "gov" / "工商登记信息.json"
    path.write_text(
        json.dumps({"code": 0, "msg": "success", "records": license_records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    written.append(path)

    ss_records = [
        {
            "id": f"SS-{month.replace('-', '')}-{idx:03d}",
            "企业名称": COMPANIES[key]["name"],
            "统一社会信用代码": COMPANIES[key]["uscc"],
            "费款所属期": month,
            "缴费基数": base,
            "参保人数": count,
            "征缴状态": status,
        }
        for idx, (key, month, count, status, base) in enumerate(SOCIAL_SECURITY, start=1)
    ]
    path = root / "gov" / "社保缴纳记录.json"
    path.write_text(json.dumps(ss_records, ensure_ascii=False, indent=2), encoding="utf-8")
    written.append(path)

    penalty_records = [
        {
            "id": p["record_id"],
            "企业名称": COMPANIES[p["company"]]["name"],
            "统一社会信用代码": COMPANIES[p["company"]]["uscc"],
            "处罚事由": p["reason"],
            "处罚金额": p["amount"],
            "处罚日期": p["date"],
            "处罚机关": p["authority"],
        }
        for p in PENALTIES
    ]
    path = root / "gov" / "行政处罚记录.json"
    path.write_text(json.dumps(penalty_records, ensure_ascii=False, indent=2), encoding="utf-8")
    written.append(path)

    path = root / "gov" / "企业关联关系.csv"
    rows = ["关联方名称|统一社会信用代码|关联类型|关联自然人|持股比例"]
    rows.append(f"{COMPANIES['yi']['name']}|{COMPANIES['yi']['uscc']}|控股|{CONTROLLER['name']}|70%")
    rows.append(f"{COMPANIES['bing']['name']}|{COMPANIES['bing']['uscc']}|控股|{CONTROLLER['name']}|55%")
    rows.append(f"{COMPANIES['jia']['name']}|{COMPANIES['jia']['uscc']}|控股|{CONTROLLER['name']}|62%")
    path.write_text("\n".join(rows), encoding="utf-8")
    written.append(path)

    return written


def write_text_files(root: Path) -> list[Path]:
    written: list[Path] = []

    lines = [
        "企业信用报告（简版）",
        "",
        "一、基本信息",
        f"企业名称: {COMPANIES['jia']['name']}",
        f"统一社会信用代码: {COMPANIES['jia']['uscc']}",
        f"实际控制人: {CONTROLLER['name']}",
        f"注册资本: {COMPANIES['jia']['capital']}万元",
        "",
        "二、信贷交易信息提示",
        "近六个月查询次数: 2",
        "逾期次数: 0",
        "负债总额: 3600万元",
        "",
        "三、公共信息明细",
        "本公司近三年无行政处罚记录，无重大诉讼记录。",
        "实际控制人张三名下另有乙贸易有限公司、丙建材有限公司两家关联企业。",
        "",
        "四、查询记录",
        "2026-09-10 因贷款审批查询，查询机构为某商业银行上海分行。",
    ]
    path = root / "fin" / "甲科技_征信报告.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    written.append(path)

    reg_lines = ["金融与政务监管条款汇编", ""]
    for reg in REGULATIONS:
        reg_lines += [
            f"【{reg['title']}】",
            f"条款编号: {reg['clause_id']}",
            f"条款原文: {reg['text']}",
            f"条款明确程度: {reg['clarity']}",
            f"适用对象: {reg['applicable']}",
            f"映射风险维度: {reg['dimension']}",
            "",
        ]
    path = root / "fin" / "监管条款汇编.txt"
    path.write_text("\n".join(reg_lines), encoding="utf-8")
    written.append(path)

    return written


def write_financial_pdf(root: Path) -> Path:
    """生成含嵌套表格的财报 PDF。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib.styles import ParagraphStyle

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    style = ParagraphStyle("cn", fontName="STSong-Light", fontSize=9.5, leading=14)
    title_style = ParagraphStyle("cnt", fontName="STSong-Light", fontSize=14, leading=20)

    c = COMPANIES["jia"]
    out = root / "fin" / "甲科技_2025年度财务报表.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=A4, title="甲科技有限公司 2025 年度财务报表")
    story = [
        Paragraph("甲科技有限公司", title_style),
        Paragraph("2025 年度财务报表（经审计）", style),
        Spacer(1, 0.4 * cm),
        Paragraph(f"统一社会信用代码：{c['uscc']}", style),
        Paragraph(f"注册资本：{c['capital']:.0f} 万元", style),
        Paragraph(f"报告期：2025-01-01 至 2025-12-31", style),
        Spacer(1, 0.6 * cm),
        Paragraph("一、利润表主要项目", style),
        Spacer(1, 0.2 * cm),
    ]

    profit_rows = [
        ["项目", "本期金额（万元）", "上期金额（万元）"],
        ["一、营业总收入", f"{c['revenue']:.2f}", "7210.50"],
        ["其中：主营业务收入", "8204.00", "6890.20"],
        ["      其他业务收入", "396.00", "320.30"],
        ["二、营业总成本", "7780.40", "6620.10"],
        ["其中：营业成本", "6120.00", "5240.00"],
        ["      销售费用", "612.40", "520.30"],
        ["      管理费用", "780.00", "640.50"],
        ["      研发费用", "268.00", "219.30"],
        ["三、营业利润", "819.60", "590.40"],
        ["四、净利润", f"{c['net_profit']:.2f}", "498.20"],
    ]
    story.append(_styled_table(profit_rows, Table, TableStyle, colors, cm))
    story += [
        Spacer(1, 0.6 * cm),
        Paragraph("二、资产负债表主要项目", style),
        Spacer(1, 0.2 * cm),
    ]

    asset_rows = [
        ["项目", "期末余额（万元）", "期初余额（万元）"],
        ["资产总计", "7120.30", "6210.80"],
        ["其中：流动资产合计", "4980.20", "4310.60"],
        ["      非流动资产合计", "2140.10", "1900.20"],
        ["负债合计", f"{7120.30 * c['debt_ratio']:.2f}", "3180.40"],
        ["其中：流动负债合计", "2412.60", "2210.30"],
        ["      非流动负债合计", "578.13", "970.10"],
        ["所有者权益合计", f"{7120.30 * (1 - c['debt_ratio']):.2f}", "3030.40"],
        ["资产负债率", f"{c['debt_ratio'] * 100:.2f}%", "51.21%"],
    ]
    story.append(_styled_table(asset_rows, Table, TableStyle, colors, cm))
    story += [
        Spacer(1, 0.6 * cm),
        Paragraph("三、用工情况", style),
        Spacer(1, 0.2 * cm),
    ]
    hr_rows = [
        ["项目", "本期", "上期"],
        ["期末从业人数（人）", str(c["employees"]), "41"],
        ["其中：研发人员", "18", "15"],
        ["全年人均薪酬（万元）", "19.60", "18.20"],
        ["社会保险参保人数（人）", "45", "41"],
    ]
    story.append(_styled_table(hr_rows, Table, TableStyle, colors, cm))
    story.append(Spacer(1, 0.5 * cm))
    story.append(
        Paragraph(
            "附注说明：本报告中社会保险参保人数来源于人力资源部门统计口径，"
            "与社保经办机构记录可能存在时点差异。",
            style,
        )
    )
    doc.build(story)
    return out


def _styled_table(rows, Table, TableStyle, colors, cm):
    table = Table(rows, colWidths=[7 * cm, 4.2 * cm, 4.2 * cm])
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF7")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9AA5B1")),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def write_judgment_pdf(root: Path) -> Path:
    """生成司法判决书 PDF。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    style = ParagraphStyle("cn", fontName="STSong-Light", fontSize=10.5, leading=18, firstLineIndent=21)
    title_style = ParagraphStyle("cnt", fontName="STSong-Light", fontSize=15, leading=24, alignment=1)

    lit = LITIGATIONS[0]
    out = root / "gov" / f"民事判决书_{lit['record_id']}.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=A4, title="民事判决书")
    body = [
        Paragraph("上海市青浦区人民法院", title_style),
        Paragraph("民事判决书", title_style),
        Spacer(1, 0.5 * cm),
        Paragraph(f"案号：（2026）沪0118民初{lit['record_id'].split('-')[-1]}号", style),
        Paragraph("原告：上海某钢材贸易有限公司，住所地上海市宝山区。", style),
        Paragraph(f"被告：丙建材有限公司，住所地{COMPANIES['bing']['address']}。", style),
        Paragraph(f"法定代表人：张三。", style),
        Spacer(1, 0.3 * cm),
        Paragraph("本院查明", title_style),
        Paragraph(
            f"原被告双方于 2025 年 6 月签订钢材买卖合同，约定被告向原告采购钢材共计 "
            f"{lit['amount']:.1f} 万元，货到付款。原告依约交付货物后，被告未按期支付货款。"
            f"经原告多次催告，被告至今未履行付款义务。",
            style,
        ),
        Paragraph(
            "另查明，被告丙建材有限公司法定代表人张三，同时持有甲科技有限公司 62% 股权、"
            "乙贸易有限公司 70% 股权，系上述企业的实际控制人。",
            style,
        ),
        Paragraph("本院认为", title_style),
        Paragraph(
            f"被告未按约定支付货款，构成违约。原告要求被告支付货款 {lit['amount']:.1f} 万元"
            f"及逾期利息的诉讼请求，于法有据，本院予以支持。",
            style,
        ),
        Paragraph("裁判结果", title_style),
        Paragraph(
            f"一、被告丙建材有限公司于本判决生效之日起十日内支付原告货款 {lit['amount']:.1f} 万元；\n"
            f"二、被告丙建材有限公司于本判决生效之日起十日内支付逾期利息；\n"
            f"三、案件受理费由被告负担。",
            style,
        ),
        Spacer(1, 0.4 * cm),
        Paragraph(f"审判员　李明", style),
        Paragraph(f"二〇二六年{lit['filing_date'].split('-')[1]}月{lit['filing_date'].split('-')[2]}日", style),
    ]
    doc.build(body)
    return out


def write_license_image(root: Path, company_key: str) -> tuple[Path, Path]:
    """生成营业执照扫描件 PNG + OCR 边车文件。

    图像刻意加了轻微的旋转与噪点，模拟真实扫描件；边车文件模拟政务影像库
    入库时由前置 OCR 服务生成的伴生文本。
    """
    from PIL import Image, ImageDraw, ImageFont
    import math

    c = COMPANIES[company_key]
    width, height = 1200, 850
    img = Image.new("RGB", (width, height), (252, 250, 244))
    draw = ImageDraw.Draw(img)

    font = _pick_cjk_font(30)
    font_small = _pick_cjk_font(22)
    font_title = _pick_cjk_font(40)

    draw.rectangle([25, 25, width - 25, height - 25], outline=(180, 60, 60), width=4)
    draw.text((width // 2 - 200, 60), "营 业 执 照", font=font_title, fill=(30, 30, 30))

    fields = [
        ("统一社会信用代码", c["uscc"]),
        ("名　　称", c["name"]),
        ("类　　型", "有限责任公司（自然人投资或控股）"),
        ("法定代表人", CONTROLLER["name"]),
        ("注册资本", f"{c['capital']:.0f}万元人民币"),
        ("成立日期", c["established"]),
        ("营业期限", f"{c['established']} 至 2038-12-31"),
        ("住　　所", c["address"]),
        ("经营范围", c["scope"]),
        ("登记机关", "上海市浦东新区市场监督管理局"),
    ]

    y = 165
    for label, value in fields:
        draw.text((70, y), f"{label}：", font=font, fill=(40, 40, 40))
        vx = 70 + len(label) * 31 + 24
        if len(value) > 24:
            draw.text((vx, y), value[:24], font=font_small, fill=(20, 20, 20))
            draw.text((vx, y + 30), value[24:48], font=font_small, fill=(20, 20, 20))
            y += 34
        else:
            draw.text((vx, y), value, font=font_small, fill=(20, 20, 20))
        y += 52

    draw.text((width - 380, height - 140), "2026 年 03 月 12 日", font=font_small, fill=(60, 60, 60))

    # 扫描噪声：轻微旋转 + 高斯噪点
    img = img.rotate(-0.6, resample=Image.BICUBIC, fillcolor=(252, 250, 244))
    rng = random.Random(hash(company_key) & 0xFFFF)
    pixels = img.load()
    for _ in range(2600):
        x, y = rng.randrange(width), rng.randrange(height)
        base = pixels[x, y]
        delta = rng.randint(-24, 12)
        pixels[x, y] = tuple(max(0, min(255, ch + delta)) for ch in base)

    out_img = root / "gov" / f"营业执照_{c['name']}.png"
    img.save(out_img)

    sidecar_text = "\n".join(f"{label.replace('　', '')}: {value}" for label, value in fields)
    out_sidecar = out_img.with_suffix(out_img.suffix + ".ocr.json")
    out_sidecar.write_text(
        json.dumps({"text": sidecar_text, "source": "政务影像库前置 OCR 服务"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_img, out_sidecar


def _pick_cjk_font(size: int):
    from PIL import ImageFont

    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\msyhbd.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    out_root = Path(args[0]) if args else DATA_DIR
    (out_root / "gov").mkdir(parents=True, exist_ok=True)
    (out_root / "fin").mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    written += write_json_files(out_root)
    written += write_text_files(out_root)
    written.append(write_financial_pdf(out_root))
    written.append(write_judgment_pdf(out_root))
    for key in COMPANIES:
        img, sidecar = write_license_image(out_root, key)
        written += [img, sidecar]

    print(f"已生成 {len(written)} 个文件到 {out_root}")
    for path in written:
        print(f"  {path.relative_to(out_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
