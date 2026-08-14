#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
变电主设备可见光图像外观缺陷分类 —— 可视化标注工具（GUI / 文件夹浏览式）

交互方式：
  1. 点“选择文件夹…”：选一个放满待标注图片的目录（也可把图片拖到左侧预览区）
  2. 自动显示第一张（小缩略图 + 完整路径）；右侧用「点选列表」选
     设备类型(10类) / 缺陷主标签(11类) / 严重程度 / 遮挡程度（细粒度可填）
  3. 点“保存并下一张”：当前图片重命名 IMG_<设备类型2位>_<4位序号>.jpg，
     复制到 dataset/images/，并向 dataset/labels.csv 追加一行，然后显示下一张
  4. 最后一张处理完提示完成；支持增量（再选文件夹或拖入新图，序号自动续接）

说明：
  - 设备类型固定 10 类（码值见 EQUIP_TYPES）
  - CSV 以 utf-8-sig 写入（与测评系统示例一致，含 BOM）
  - 预览优先用 Pillow 跨平台缩放；若环境无 Pillow（如本机 Mac 运行版），自动用
    macOS 自带 sips 生成缩略图，保证看图功能正常。仅作占位提示
  - 仅复制原图到 dataset/images/，不会删除你的源文件
  - label 选择使用常显列表框（Listbox），避免与拖拽库冲突导致下拉框点不开
"""

import os
import sys
import csv
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinterdnd2 import DND_FILES, TkinterDnD

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

# ===================== 配置区 =====================
# 打包成 exe 后（sys.frozen=True），__file__ 指向临时解压目录，
# 必须把工作目录/输出目录放到 exe 同级，否则 dataset 会写进临时目录、退出即丢失。
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_IMG_DIR = os.path.join(BASE_DIR, "dataset", "images")
LABELS_CSV = os.path.join(BASE_DIR, "dataset", "labels.csv")
PREVIEW_MAX = 920            # 预览图最大边长(px)，约铺满屏幕左半边
# =================================================

# 设备类型：10 类（顺序即列表下标，码值=下标+1）
EQUIP_TYPES = [
    "变压器", "断路器", "GIS", "电流互感器", "电压互感器",
    "避雷器", "隔离开关", "电抗器", "绝缘子", "电容器",
]
# 缺陷主标签：11 类（码值=下标+1），英文名用于写入 CSV
# 前 6 类是测评规范编码；后 5 类（contamination/loose_deformation/foreign_object/
# reading_abnormal/switch_abnormal）为按实际数据补充的细分标签，提交测评前可按需映射回规范 6 类。
DEFECT_LABELS = [
    ("normal", "正常"),
    ("oil_leak", "渗漏油"),
    ("corrosion", "锈蚀腐蚀"),
    ("visible_damage", "外观破损"),
    ("birdnest", "鸟窝"),
    ("silica_gel_discoloration", "呼吸器硅胶变色"),
    ("contamination", "污秽"),
    ("loose_deformation", "松动变形"),
    ("foreign_object", "异物"),
    ("reading_abnormal", "读数异常"),
    ("switch_abnormal", "开关异常"),
]
SEVERITY = ["未标注", "轻微", "一般", "严重", "紧急"]          # 码值=下标
OCCLUSION = ["无遮挡", "轻度", "中度", "重度"]                  # 码值=下标

IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")
CSV_HEADER = ["image_id", "image_path", "equipment_type_code",
              "label_primary", "label_secondary", "severity_code", "occlusion_code"]


# -------------------- CSV / 文件底层逻辑 --------------------
def get_next_seq():
    seq = 0
    if os.path.isdir(OUTPUT_IMG_DIR):
        for fn in os.listdir(OUTPUT_IMG_DIR):
            base = fn.rsplit(".", 1)[0]
            if base.startswith("IMG_") and fn.lower().endswith(IMG_EXTS):
                parts = base.split("_")
                if len(parts) == 3 and parts[2].isdigit():
                    seq = max(seq, int(parts[2]))
    return seq + 1


def ensure_header():
    os.makedirs(os.path.dirname(LABELS_CSV), exist_ok=True)
    if not os.path.exists(LABELS_CSV):
        with open(LABELS_CSV, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(CSV_HEADER)


def append_row(row):
    with open(LABELS_CSV, "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(row)


def save_image(src, et, seq):
    """复制并重命名到输出目录，返回 (new_name, image_id)。"""
    ext = os.path.splitext(src)[1].lower()
    if ext not in (".jpg", ".jpeg"):
        ext = ".jpg"  # 测评要求 jpg，非 jpg 统一落为 .jpg 后缀
    new_name = f"IMG_{et:02d}_{seq:04d}{ext}"
    dst = os.path.join(OUTPUT_IMG_DIR, new_name)
    shutil.copy2(src, dst)
    image_id = os.path.splitext(new_name)[0]
    return new_name, image_id


def make_preview(src, max_side=PREVIEW_MAX):
    """返回用于显示的对象：PIL.Image（优先）或临时 gif 文件路径（macOS sips 兜底）。
    两者都能直接交给 tk.PhotoImage。这样没装 Pillow 的 macOS 环境也能正常预览。"""
    # 优先用 Pillow（Windows 打包版自带）
    if Image is not None:
        try:
            img = Image.open(src)
            img.thumbnail((max_side, max_side), Image.LANCZOS)
            return img
        except Exception:
            pass
    # 兜底：macOS 用系统自带 sips 缩放到临时 gif
    if sys.platform == "darwin":
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".gif", delete=False)
            tmp.close()
            subprocess.run(
                ["sips", "-Z", str(max_side), "-s", "format", "gif", src, "--out", tmp.name],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return tmp.name
        except Exception:
            return None
    return None


def get_image_size(src):
    """查询原图像素宽高，返回 (w, h) 或 (None, None)。
    Pillow 可用时用 Pillow；否则 macOS 用 sips 兜底。"""
    if Image is not None:
        try:
            with Image.open(src) as im:
                return im.size  # (width, height)
        except Exception:
            pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sips", "-g", "pixelWidth", "-g", "pixelHeight", src],
                capture_output=True, text=True).stdout
            w = h = None
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("pixelWidth:"):
                    w = int(line.split(":", 1)[1])
                elif line.startswith("pixelHeight:"):
                    h = int(line.split(":", 1)[1])
            return w, h
        except Exception:
            return None, None
    return None, None


def find_xml(src):
    """按约定在 images 目录的同级 xmls 文件夹里找同名 xml。
    约定：…/正样本/images/xxx.jpg  <->  …/正样本/xmls/xxx.xml"""
    d = os.path.dirname(src)
    parent = os.path.dirname(d)
    base = os.path.splitext(os.path.basename(src))[0]
    cand = os.path.join(parent, "xmls", base + ".xml")
    return cand if os.path.exists(cand) else None


def parse_annotation(xml_path):
    """解析 Pascal VOC 格式标注，返回 (size_w, size_h, [(name,x1,y1,x2,y2), ...])。"""
    try:
        root = ET.parse(xml_path).getroot()
        w = h = None
        sz = root.find("size")
        if sz is not None:
            pw, ph = sz.find("width"), sz.find("height")
            if pw is not None and pw.text:
                w = int(pw.text)
            if ph is not None and ph.text:
                h = int(ph.text)
        objs = []
        for o in root.findall("object"):
            b = o.find("bndbox")
            if b is None:
                continue
            nm = o.find("name")
            name = nm.text if nm is not None and nm.text else "?"
            def gi(tag):
                e = b.find(tag)
                return int(float(e.text)) if e is not None and e.text else 0
            objs.append((name, gi("xmin"), gi("ymin"), gi("xmax"), gi("ymax")))
        return w, h, objs
    except Exception:
        return None, None, []


# -------------------- 主界面（文件夹浏览式） --------------------
class LabelApp(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("变电设备图像缺陷标注工具  (能源局场景5测评)")
        self.geometry("1500x960")
        self.resizable(True, True)

        os.makedirs(OUTPUT_IMG_DIR, exist_ok=True)
        ensure_header()

        self.file_list = []   # 当前文件夹的图片路径列表（按文件名排序）
        self.index = 0        # 当前正在处理的下标
        self.done = 0         # 已成功录入张数
        self._preview_img = None
        self._secondary_manual = False   # 用户是否手动改过细粒度框（改过则保留其输入）

        self._build_widgets()
        self._load_current()

    def _build_widgets(self):
        # ---------- 左：预览(画布+滚动条) + 路径 ----------
        left = ttk.Frame(self, padding=8)
        left.grid(row=0, column=0, sticky="nsew")
        canvas_frame = ttk.Frame(left)
        canvas_frame.grid(row=0, column=0, sticky="nsew")
        self.preview_canvas = tk.Canvas(canvas_frame, bg="#f0f0f0", highlightthickness=0)
        vsb = ttk.Scrollbar(canvas_frame, orient="vertical",
                            command=self.preview_canvas.yview)
        hsb = ttk.Scrollbar(canvas_frame, orient="horizontal",
                            command=self.preview_canvas.xview)
        self.preview_canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.preview_canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        canvas_frame.grid_columnconfigure(0, weight=1)
        canvas_frame.grid_rowconfigure(0, weight=1)

        # 画布占位提示
        self.preview_canvas.create_text(
            300, 200, text="点『选择文件夹…』载入图片\n或把图片拖拽到此处",
            anchor="center", fill="#888", tags="placeholder")
        self.fname_lbl = ttk.Label(left, text="", foreground="#555",
                                   wraplength=700, justify="left")
        self.fname_lbl.grid(row=1, column=0, sticky="w", pady=(4, 0))

        # ---------- 右：label 选择（全部用常显列表框，避免下拉框冲突） ----------
        right = ttk.Frame(self, padding=8)
        right.grid(row=0, column=1, sticky="nsew")

        # 设备类型
        ttk.Label(right, text="设备类型（10类）", font=("", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.equip_lb = tk.Listbox(right, height=5, exportselection=False, width=22)
        for name in EQUIP_TYPES:
            self.equip_lb.insert("end", name)
        self.equip_lb.selection_set(0)
        self.equip_lb.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

        # 缺陷主标签
        ttk.Label(right, text="缺陷主标签（11类）", font=("", 11, "bold")).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.defect_lb = tk.Listbox(right, height=11, exportselection=False, width=22)
        for _, name in DEFECT_LABELS:
            self.defect_lb.insert("end", name)
        self.defect_lb.selection_set(0)
        self.defect_lb.grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.defect_lb.bind("<<ListboxSelect>>",
                            lambda e: (self.defect_lb.configure(bg="white"),
                                       self._refresh_secondary_default()))

        # 严重程度 / 遮挡程度 并排
        ttk.Label(right, text="严重程度", font=("", 11, "bold")).grid(
            row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Label(right, text="遮挡程度", font=("", 11, "bold")).grid(
            row=4, column=1, sticky="w", pady=(8, 0), padx=(10, 0))
        self.sev_lb = tk.Listbox(right, height=5, exportselection=False, width=10)
        for name in SEVERITY:
            self.sev_lb.insert("end", name)
        self.sev_lb.selection_set(0)
        self.sev_lb.grid(row=5, column=0, sticky="w", pady=(2, 0))
        self.occ_lb = tk.Listbox(right, height=5, exportselection=False, width=10)
        for name in OCCLUSION:
            self.occ_lb.insert("end", name)
        self.occ_lb.selection_set(0)
        self.occ_lb.grid(row=5, column=1, sticky="w", pady=(2, 0), padx=(10, 0))

        # 细粒度标签
        ttk.Label(right, text="细粒度标签（可选；默认取路径中『设备类型+细粒度』那层文件夹名去掉前缀）").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.secondary_var = tk.StringVar()
        self.secondary_entry = ttk.Entry(right, textvariable=self.secondary_var, width=34)
        self.secondary_entry.grid(row=7, column=0, columnspan=2, sticky="w")
        # 用户一旦在框里敲字，记为手动输入；此后不再用路径值覆盖
        self.secondary_entry.bind(
            "<KeyRelease>", lambda e: setattr(self, "_secondary_manual", True))

        # 进度
        self.progress_lbl = ttk.Label(right, text="", foreground="#0a6")
        self.progress_lbl.grid(row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # 按钮区
        btnbar = ttk.Frame(right)
        btnbar.grid(row=9, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(btnbar, text="选择文件夹…", command=self.choose_dir).pack(side="left")
        ttk.Button(btnbar, text="保存并下一张", command=self.commit).pack(side="left", padx=6)
        ttk.Button(btnbar, text="跳过此张", command=self.skip).pack(side="left", padx=6)
        ttk.Button(btnbar, text="完成退出", command=self.finish).pack(side="left", padx=6)

        self.status_lbl = ttk.Label(right, text="", foreground="#333")
        self.status_lbl.grid(row=10, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # 仅预览画布接收拖拽，避免整窗拖拽拦截右侧控件的鼠标事件
        self.preview_canvas.drop_target_register(DND_FILES)
        self.preview_canvas.dnd_bind("<<Drop>>", self.on_drop)

        self.grid_columnconfigure(0, weight=2)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)

    # ---- 读取列表框选中（返回码值） ----
    def _sel(self, lb, offset=1):
        """offset=1：码值=下标+1（设备/缺陷）；offset=0：码值=下标（严重/遮挡）。"""
        cur = lb.curselection()
        idx = cur[0] if cur else 0
        return idx + offset

    # ---- 按路径推断设备类型下标（路径含某设备类型名则选它，取最长匹配） ----
    def _derive_equip(self, src):
        found, found_len = -1, 0
        for i, name in enumerate(EQUIP_TYPES):
            if name in src and len(name) > found_len:
                found, found_len = i, len(name)
        return found

    # ---- 按路径推断缺陷主标签下标（0-based；返回 -1 表示无法判断、需人工确认） ----
    # 规则：
    #  - 路径含“负样本/正常” -> normal；
    #  - 否则按“细粒度”文件夹名里的缺陷关键词映射到 11 类缺陷主标签。
    # 关键词取最长命中，降低误判（如“锈蚀渗漏”优先匹配更具体的词）。
    # 兼容“没有 正样本/负样本 层级”的设备目录（如避雷器）：
    #   这类目录的缺陷直接写在细粒度文件夹名里，同样靠关键词判断。
    # 注意：birdnest 规则须排在 foreign_object 之前（“异物鸟巢”要判为鸟巢）；
    #      visible_damage 规则须排在 loose_deformation 之前（“破损变形”判为破损）。
    def _derive_defect(self, src):
        if "负样本" in src or "正常" in src:
            return 0  # normal
        rules = [
            (1, ("渗漏", "漏油", "渗油", "油流")),            # oil_leak
            (2, ("锈蚀", "腐蚀")),                             # corrosion
            (4, ("鸟窝", "鸟巢")),                             # birdnest
            (5, ("硅胶变色",)),                                 # silica_gel_discoloration
            (6, ("污秽", "积尘", "污染")),                      # contamination
            (9, ("读数", "油位", "指示灯")),                    # reading_abnormal
            (10, ("分合闸", "空开")),                          # switch_abnormal（不用“开关”，避免误伤“隔离开关”）
            (3, ("破损", "裂纹", "损坏", "断裂", "破碎",
                 "冲顶", "模糊", "箱门")),                      # visible_damage
            (7, ("松动", "松股", "断股", "变形", "鼓肚")),       # loose_deformation
            (8, ("挂空", "悬浮物", "异物")),                    # foreign_object
        ]
        best, best_len = -1, 0
        for code, kws in rules:
            for k in kws:
                if k in src and len(k) > best_len:
                    best, best_len = code, len(k)
        return best

    # ---- 载入当前下标图片 ----
    def _load_current(self):
        if self.index >= len(self.file_list):
            self._clear_canvas()
            if not self.file_list:
                self._show_placeholder()
                self.fname_lbl.configure(text="")
                self.progress_lbl.configure(text=f"已录入 {self.done} 张 | 待处理 0 张")
            else:
                self._show_done()
                self.fname_lbl.configure(text="")
                self.progress_lbl.configure(
                    text=f"已录入 {self.done} 张 | 共 {len(self.file_list)} 张（已完成）")
            return

        src = self.file_list[self.index]
        self.fname_lbl.configure(text=f"当前文件：\n{src}")
        self.progress_lbl.configure(
            text=f"第 {self.index + 1} / {len(self.file_list)} 张 | 已录入 {self.done} 张")
        # 路径里含设备类型名则自动选中对应设备（取最长匹配，避免 GIS 等误命中）
        ei = self._derive_equip(src)
        if ei >= 0:
            self.equip_lb.selection_clear(0, "end")
            self.equip_lb.selection_set(ei)
        # 路径含 正样本/负样本 + 细粒度关键词则自动选中缺陷主标签（人工仅复核）
        di = self._derive_defect(src)
        self._draw_preview(src)
        if di >= 0:
            self.defect_lb.selection_clear(0, "end")
            self.defect_lb.selection_set(di)
            self.defect_lb.configure(bg="white")
        else:
            # 无法从路径判断：标红提醒人工选择，避免悄悄沿用上一张的标签
            self.defect_lb.configure(bg="#ffe9e9")
            self.status_lbl.configure(
                text=self.status_lbl.cget("text") + "  ⚠缺陷主标签未识别，请手动选择")
        # 加载即回填默认 label_secondary 到输入框，让用户看到将写入的值
        self._secondary_manual = False   # 新图片：重置手动标记，强制按路径重新推导
        self._refresh_secondary_default()

    # ---- 画布辅助 ----
    def _clear_canvas(self):
        self.preview_canvas.delete("all")
        self._preview_img = None

    def _show_placeholder(self):
        self.preview_canvas.configure(width=600, height=400, scrollregion=(0, 0, 600, 400))
        self.preview_canvas.create_text(300, 200,
            text="点『选择文件夹…』载入图片\n或把图片拖拽到此处",
            anchor="center", fill="#888")

    def _show_done(self):
        self.preview_canvas.configure(width=600, height=400, scrollregion=(0, 0, 600, 400))
        self.preview_canvas.create_text(300, 200, text="已全部处理完成 ✅",
            anchor="center", fill="#0a6", font=("", 16, "bold"))

    # ---- 在画布上绘制（带标注框）的预览 ----
    def _draw_preview(self, src):
        self._clear_canvas()
        ow, oh = get_image_size(src)      # 原图尺寸，用于把 XML 框坐标换算到预览
        preview_obj = make_preview(src)   # PIL.Image 或 sips 生成的 gif 路径
        if preview_obj is None:
            self.preview_canvas.configure(width=600, height=400, scrollregion=(0, 0, 600, 400))
            self.preview_canvas.create_text(300, 200, text="预览生成失败（可能图片损坏）",
                anchor="center", fill="#c00")
            self.status_lbl.configure(text="预览生成失败")
            return

        # 缩放比：以原图与预览最大边计算（box 坐标按原图绝对坐标换算）
        scale = None
        if ow and oh:
            scale = min(PREVIEW_MAX / ow, PREVIEW_MAX / oh)

        # 兼容两种来源：PIL.Image 用 ImageTk；sips 生成的 gif 路径用 tk.PhotoImage
        if Image is not None and isinstance(preview_obj, Image.Image):
            img = ImageTk.PhotoImage(preview_obj)
        else:
            img = tk.PhotoImage(file=preview_obj)
        self._preview_img = img
        dw, dh = img.width(), img.height()
        self.preview_canvas.configure(width=dw, height=dh, scrollregion=(0, 0, dw, dh))
        self.preview_canvas.create_image(0, 0, image=img, anchor="nw")

        # 解析同名 xml 标注并画框
        xmlp = find_xml(src)
        n_boxes = 0
        if xmlp:
            aw, ah, objs = parse_annotation(xmlp)
            if scale is None and aw and ah:
                scale = min(PREVIEW_MAX / aw, PREVIEW_MAX / ah)
            for name, x1, y1, x2, y2 in objs:
                n_boxes += 1
                if scale:
                    x1, y1, x2, y2 = (int(x1 * scale), int(y1 * scale),
                                      int(x2 * scale), int(y2 * scale))
                self.preview_canvas.create_rectangle(
                    x1, y1, x2, y2, outline="#ff3b30", width=3)
                self.preview_canvas.create_text(
                    x1 + 3, y1 + 3, text=name, anchor="nw",
                    fill="#ff3b30", font=("", 13, "bold"))
        self.status_lbl.configure(
            text=f"标注框：{n_boxes} 个" +
                 (f"（来自 {os.path.basename(xmlp)}）" if xmlp else "（未找到同名 XML）"))

    # ---- 按当前图片+当前缺陷标签，刷新“细粒度标签”框的默认值 ----
    def _refresh_secondary_default(self):
        if self.index >= len(self.file_list):
            return
        if self._secondary_manual:
            return  # 用户手动改过细粒度框，保留其输入，不再用路径值覆盖
        src = self.file_list[self.index]
        dl = self._sel(self.defect_lb, 1)
        lp = DEFECT_LABELS[dl - 1][0]
        # 加载新图/切换缺陷标签时，刷新为路径提取出的默认值（手动填过也可在框里再改）
        self.secondary_var.set(self._derive_secondary(src, lp))

    # ---- 计算 label_secondary ----
    def _derive_secondary(self, src, label_primary):
        # 纯按路径推导（不读界面框内容，避免“上一张图残留值”被当成手动值而不更新）。
        # 与设备类型自动识别同一套逻辑：先识别设备类型，再取路径中
        # “该设备类型文件夹下的那一层”作为细粒度描述，并去掉设备类型前缀。
        # 例：…/电抗器/电抗器导电引线锈蚀接地引下线锈蚀/正样本/images/x.jpg
        #     -> 设备=电抗器 -> 细粒度=导电引线锈蚀接地引下线锈蚀
        ei = self._derive_equip(src)
        if ei >= 0:
            equip = EQUIP_TYPES[ei]
            parts = src.split(os.sep)
            for i, p in enumerate(parts):
                if p == equip and i + 1 < len(parts):
                    cand = parts[i + 1]
                    if cand.startswith(equip):
                        cand = cand[len(equip):].strip("_- 　")
                    return cand
        # 兜底：正常样本若无路径信息则写“正常”，缺陷样本留空
        return "正常" if label_primary == "normal" else ""

    # ---- 保存当前并前进 ----
    def commit(self):
        if self.index >= len(self.file_list):
            messagebox.showinfo("提示", "没有待处理的图片，请先『选择文件夹…』。")
            return
        et = self._sel(self.equip_lb, 1)
        dl = self._sel(self.defect_lb, 1)
        label_primary = DEFECT_LABELS[dl - 1][0]
        # label_secondary 取值规则：
        #   正常(normal) -> 固定写“正常”
        #   缺陷类      -> 取源图所在文件夹名，并去掉开头的设备类型前缀；
        #                  若去前缀后为空，则回退到“细粒度标签”框中手动填的内容
        src = self.file_list[self.index]
        secondary = self._derive_secondary(src, label_primary)
        sev = self._sel(self.sev_lb, 0)
        occ = self._sel(self.occ_lb, 0)

        seq = get_next_seq()
        new_name, image_id = save_image(src, et, seq)
        append_row([image_id, f"images/{new_name}", et, label_primary,
                    secondary, sev, occ])
        self.done += 1
        self.status_lbl.configure(
            text=f"已保存：{new_name} (设备{et} | {label_primary})")
        self.index += 1
        self._load_current()

    # ---- 跳过当前并前进 ----
    def skip(self):
        if self.index >= len(self.file_list):
            return
        skipped = os.path.basename(self.file_list[self.index])
        self.index += 1
        self.status_lbl.configure(text=f"已跳过：{skipped}（未保存）")
        self._load_current()

    # ---- 拖拽图片进入（追加到列表末尾） ----
    def on_drop(self, event):
        try:
            raw = self.tk.splitlist(event.data)
        except Exception:
            raw = event.data.split()
        added = [p for p in raw if p.lower().endswith(IMG_EXTS)]
        if not added:
            self.status_lbl.configure(text="拖入的不是图片文件，已忽略")
            return
        at_end = (self.index >= len(self.file_list))
        self.file_list.extend(added)
        self.status_lbl.configure(
            text=f"已拖入 {len(added)} 张，共 {len(self.file_list)} 张")
        if at_end:  # 之前已处理完/为空，立即显示新加入的第一张
            self._load_current()

    # ---- 选择文件夹（主入口） ----
    def choose_dir(self):
        d = filedialog.askdirectory(title="选择待标注图片所在文件夹")
        if not d:
            return
        added = sorted(
            os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(IMG_EXTS))
        if not added:
            messagebox.showwarning("提示", "该文件夹下没有找到图片文件。")
            return
        self.file_list = added
        self.index = 0
        self.status_lbl.configure(
            text=f"已载入文件夹：{d}（{len(added)} 张）")
        self._load_current()

    def finish(self):
        messagebox.showinfo(
            "退出",
            f"已退出。累计录入 {self.done} 张，共载入 {len(self.file_list)} 张。\nCSV：{LABELS_CSV}")
        self.destroy()


if __name__ == "__main__":
    app = LabelApp()
    app.mainloop()
