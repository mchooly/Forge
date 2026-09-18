"""Forge · 载荷拼接器 · Tkinter GUI。

    python gui.py

**零新增依赖**（tkinter 是 stdlib）。与 CLI 共用同一套引擎接口，
所有生成逻辑都在 `forge.engine` 里，本文件只做视图 + 事件绑定。

能力对齐 CLI：前提筛选、槽位/变量、绕过增删与排序、8 种提交方式、
11 个输出渲染器、成品约束校验、组装链与逐步中间态。
"""

import os
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from forge import engine, output

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


class ScrollArea(ttk.Frame):
    """竖向可滚动的容器，内容放进 `.inner`。

    为什么需要它：**前提区的内容会一直涨**（词表从 22 个键长到 40 多个），
    模板参数区也一样（一条模板能有 4 个槽位 + 2 个变量 + 一段 desc）。
    这两块都是**变高**的，而窗口不是——不装进画布里，它们会把下面的
    结果区一路挤到看不见，用户看到的就是「内容显示不全」。

    装进来之后这两块的**高度就封顶了**（`height` 参数给一个值），
    多出来的自己滚，不再向下施压。
    """

    def __init__(self, master, height=None, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                **({"height": height} if height else {}))
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._fit)
        # 滚轮只在指针位于本区域时接管，免得抢走别处的滚动
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _fit(self, event):
        """内层跟着画布走：宽度对齐，高度取「自然高度」和「画布高度」里大的那个。

        高度取 max 是关键，两种情形都要照顾到：

        - **窗口够大** → 内层撑满画布，里面 `rowconfigure(weight=…)` 照常生效，
          结果区跟着窗口一起长。最大化时的体验不变。
        - **窗口不够大** → 内层保持自然高度，超出的部分交给**外层滚动**兜住。
          不这么做的话，grid 会按权重把可伸缩的那两行一路压下去——
          实测 900x640 时结果区只剩 17px，等于没有。
        """
        self.canvas.itemconfigure(
            self._win, width=event.width,
            height=max(self.inner.winfo_reqheight(), event.height))

    def _wheel(self, event):
        # Windows 上 delta 是 ±120 的倍数
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")


class App(ttk.Frame):
    def __init__(self, master, rules):
        super().__init__(master, padding=6)
        self.rules = rules
        self.grid(sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)

        # ---- 可变状态 ----
        self.without_vars = {r["id"]: tk.BooleanVar() for r in rules["requires"]}
        self.output_vars = {n: tk.BooleanVar(value=n in ("raw", "encoded", "curl"))
                            for n in output.RENDERERS}
        self.slot_widgets = {}     # 槽位名 -> StringVar（存的是显示文本）
        self._slot_ids = {}        # 槽位名 -> {显示文本: 取值 id}
        self.var_entries = {}      # 变量名 -> StringVar
        self.bypass_order = []
        self.selected_template = None
        self._params_for = None    # 参数区当前是为哪条模板建的

        # 绕过下拉的显示文本 -> id。bypass 的中文名在模板数据里（`name` 字段），
        # 不在 labels.yaml——所以这张反查表从 rules 现建。
        self._bypass_label = {self._bypass_text(b): b["id"] for b in rules["bypasses"]}

        self._build_filters()
        self._build_requires()
        self._build_middle()
        self._build_bypass()
        self._build_request()
        self._build_output()
        self.refresh()

    # ============================================================ 标签
    # 界面上显示「中文 id」，喂给引擎的永远是 id。见 data/labels.yaml。

    def L(self, kind, value):
        return engine.label(self.rules, kind, value)

    def U(self, kind, text):
        return engine.unlabel(self.rules, kind, text)

    @staticmethod
    def _bypass_text(b):
        return "%s %s" % (b.get("name", b["id"]), b["id"])

    # ============================================================ 布局

    def _build_filters(self):
        box = ttk.LabelFrame(self, text="筛选", padding=6)
        box.grid(row=0, column=0, sticky="ew")
        for i in range(6):
            box.columnconfigure(i, weight=1 if i % 2 else 0)

        def combo(col, label, values, width=12):
            ttk.Label(box, text=label).grid(row=0, column=col * 2, sticky="e", padx=(0, 2))
            var = tk.StringVar()
            cb = ttk.Combobox(box, textvariable=var, values=values, width=width, state="readonly")
            cb.grid(row=0, column=col * 2 + 1, sticky="ew", padx=(0, 10))
            cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
            return var

        # 漏洞类型按模板数从多到少排，最常用的排最前
        counts = {}
        for t in self.rules["templates"]:
            for v in t.get("vuln") or []:
                counts[v] = counts.get(v, 0) + 1
        vulns = sorted(counts, key=lambda v: (-counts[v], v))
        comps = sorted({c for t in self.rules["templates"] for c in (t.get("component") or [])})
        ctypes = sorted({c for t in self.rules["templates"] for c in (t.get("content_type") or [])})

        # 下拉里显示「SQL 注入 sqli」，取值仍是 id——_state() 里统一 unlabel 还原
        self.v_vuln = combo(0, "漏洞类型", [self.L("vuln", v) for v in vulns], width=20)
        self.v_component = combo(1, "组件", ["（不限）"] + comps, width=18)
        self.v_ctype = combo(2, "内容类型",
                             ["（不限）"] + [self.L("content_type", c) for c in ctypes], width=22)
        self.v_vuln.set(self.L("vuln", "sqli") if "sqli" in counts
                        else (self.L("vuln", vulns[0]) if vulns else ""))

        ttk.Label(box, text="版本").grid(row=0, column=6, sticky="e", padx=(0, 2))
        self.v_version = tk.StringVar()
        e = ttk.Entry(box, textvariable=self.v_version, width=10)
        e.grid(row=0, column=7, sticky="w")
        e.bind("<KeyRelease>", lambda ev: self.refresh())

    def _build_requires(self):
        box = ttk.LabelFrame(self, text="目标前提（勾选 = 目标没有这个前提，需要它的模板会被筛掉。括号里是勾上后会筛掉几条）", padding=6)
        box.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        box.columnconfigure(0, weight=1)

        # 高度封顶在 3 行左右：词表只会越来越长，这里每长一行就吃掉一行结果区的空间
        self.require_scroll = ScrollArea(box, height=76)
        self.require_scroll.grid(row=0, column=0, sticky="ew")
        inner = self.require_scroll.inner

        self.require_checks = {}
        for r in self.rules["requires"]:
            cb = ttk.Checkbutton(inner, text=r["name"], variable=self.without_vars[r["id"]],
                                 command=self.refresh)
            self.require_checks[r["id"]] = (cb, r["name"])

        # 排几列**按窗口宽度算**，不写死。固定 6 列在窄窗口下会把右边几列
        # 挤出可视区——用户只看到「部分选项」，还不知道右边其实还有。
        self._require_cols = 0
        # 绑在**顶层窗口**上而不是画布上：窗口比内容窄的时候画布不会变小，
        # 它只是按自然宽度布局然后被裁掉，绑在它身上就永远收不到那次 resize。
        self.winfo_toplevel().bind("<Configure>", self._relayout_requires, add="+")
        self._relayout_requires()

    def _relayout_requires(self, event=None):
        # 可用宽度取**顶层窗口**的，不是画布自己的。
        # 画布在窗口过窄时仍报告自然宽度（1464 那种），拿它算出来的列数会继续溢出，
        # 断言之类的东西也就跟着量了个假数。
        avail = self.winfo_toplevel().winfo_width() - 48
        if avail <= 1:
            avail = 900          # 还没映射，先按一个够宽的窗口排
        # 按**最长的那条**估列宽：中文比西文宽，用字体度量量出来最准。
        # 留 26px 给勾选框本身和列间距。
        f = tkfont.nametofont("TkDefaultFont")
        widest = max(f.measure(t) for _, t in self.require_checks.values()) + 26
        cols = max(1, (avail - 16) // max(1, widest))
        cols = max(1, min(len(self.require_checks), cols))
        if cols == self._require_cols:
            return
        self._require_cols = cols
        for i, (cb, _) in enumerate(self.require_checks.values()):
            cb.grid_configure(row=i // cols, column=i % cols, sticky="w", padx=(0, 12))

    def _build_middle(self):
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        self.rowconfigure(2, weight=1)

        # 左：可用模板
        left = ttk.LabelFrame(pane, text="模板", padding=4)
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)
        head = ttk.Frame(left)
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.lbl_count = ttk.Label(head, text="")
        self.lbl_count.pack(side="left")
        self.v_show_excluded = tk.BooleanVar(value=False)
        ttk.Checkbutton(head, text="显示缺前提的", variable=self.v_show_excluded,
                        command=self.refresh).pack(side="right")

        self.lst_templates = tk.Listbox(left, exportselection=False, height=6)
        self.lst_templates.grid(row=2, column=0, sticky="nsew")
        self.lst_templates.bind("<<ListboxSelect>>", lambda e: self.on_pick_template())
        sb = ttk.Scrollbar(left, orient="vertical", command=self.lst_templates.yview)
        sb.grid(row=2, column=1, sticky="ns")
        self.lst_templates.configure(yscrollcommand=sb.set)
        pane.add(left, weight=1)

        # 右：模板参数（槽位 + 变量）
        # 参数区是**变高**的（一条模板能有 4 个槽位 + 2 个变量 + 一段 desc），
        # 装进可滚动容器，免得它把左边的模板列表和下面的结果区一起挤没
        right = ttk.LabelFrame(pane, text="模板参数", padding=4)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        self.params_scroll = ScrollArea(right, height=200)
        self.params_scroll.grid(row=0, column=0, sticky="nsew")
        self.params = self.params_scroll.inner
        pane.add(right, weight=2)

    def _build_bypass(self):
        box = ttk.LabelFrame(self, text="绕过（按列表顺序依次应用）", padding=6)
        box.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        box.columnconfigure(0, weight=3)
        box.columnconfigure(2, weight=2)

        self.lst_bypass = tk.Listbox(box, exportselection=False, height=3)
        self.lst_bypass.grid(row=0, column=0, rowspan=4, sticky="nsew")
        btns = ttk.Frame(box)
        btns.grid(row=0, column=1, sticky="n")
        for text, cmd in (("↑", lambda: self.move_bypass(-1)), ("↓", lambda: self.move_bypass(1)),
                          ("移除", self.remove_bypass)):
            ttk.Button(btns, text=text, width=4, command=cmd).pack(pady=1)

        ttk.Label(box, text="添加（冲突项会标红）").grid(row=0, column=2, sticky="w", padx=(10, 0))
        self.v_addbypass = tk.StringVar()
        cb = ttk.Combobox(box, textvariable=self.v_addbypass,
                          values=list(self._bypass_label), state="readonly", width=30)
        cb.grid(row=1, column=2, sticky="ew", padx=(10, 0))
        ttk.Button(box, text="添加", command=self.add_bypass).grid(row=1, column=3, padx=4)
        self.lbl_conflict = ttk.Label(box, text="", foreground="#b00")
        self.lbl_conflict.grid(row=2, column=2, columnspan=2, sticky="w", padx=(10, 0))

    def _build_request(self):
        box = ttk.LabelFrame(self, text="提交方式 / 请求参数 / 约束校验", padding=6)
        box.grid(row=4, column=0, sticky="ew", pady=(6, 0))

        # 显式网格：每行 3 组「标签 + 控件」。helper 不再自己算行列——
        # 原来那版两个 helper 的列位置写死，同一行的两个控件会叠在一起。

        def put(row, col, label, widget):
            ttk.Label(box, text=label).grid(row=row, column=col * 2, sticky="e", padx=(8, 2))
            widget.grid(row=row, column=col * 2 + 1, sticky="w")

        def sel(row, col, label, values, default):
            var = tk.StringVar(value=default)
            cb = ttk.Combobox(box, textvariable=var, values=values, width=15, state="readonly")
            cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
            put(row, col, label, cb)
            return var

        def txt(row, col, label, default, width=15):
            var = tk.StringVar(value=default)
            e = ttk.Entry(box, textvariable=var, width=width)
            e.bind("<KeyRelease>", lambda ev: self.refresh())
            put(row, col, label, e)
            return var

        self.v_submit = sel(0, 0, "提交方式",
                            [self.L("submit", p) for p in output.PLACERS],
                            self.L("submit", "query"))
        self.v_method = sel(0, 1, "HTTP 方法", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"], "GET")
        self.v_shell = sel(0, 2, "Shell", ["bash", "zsh", "cmd", "powershell"], "bash")

        self.v_host = txt(1, 0, "Host", "example.com")
        self.v_path = txt(1, 1, "Path", "/")
        self.v_param = txt(1, 2, "参数名", "id")

        self.v_header = txt(2, 0, "Header 名", "X-Test")
        self.v_cookie = txt(2, 1, "Cookie 名", "sid")
        self.v_part = sel(2, 2, "上传落点", ["filename", "content"], "filename")

        self.v_maxlen = txt(3, 0, "长度上限", "")
        self.v_forbid = txt(3, 1, "禁用字符(逗号隔开)", "")
        self.v_block = txt(3, 2, "黑名单关键字(逗号隔开)", "", 20)

    def _build_output(self):
        box = ttk.LabelFrame(self, text="输出层（多选）", padding=6)
        box.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        # 勾选框只改**显示文本**，variable 的键仍是渲染器 id——输出标签页的名字、
        # 引擎的 outputs 列表都用 id，这里换了键就全对不上了
        for i, name in enumerate(output.RENDERERS):
            ttk.Checkbutton(box, text=self.L("render", name), variable=self.output_vars[name],
                            command=self.refresh).grid(row=0, column=i, sticky="w", padx=(0, 10))

        out = ttk.LabelFrame(self, text="结果", padding=4)
        out.grid(row=6, column=0, sticky="nsew", pady=(6, 0))
        self.rowconfigure(6, weight=2)
        out.rowconfigure(0, weight=1)
        out.columnconfigure(0, weight=1)

        self.nb = ttk.Notebook(out)
        self.nb.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Frame(out)
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(bar, text="复制当前标签页", command=self.copy_current).pack(side="left")
        self.lbl_status = ttk.Label(bar, text="")
        self.lbl_status.pack(side="left", padx=12)

    # ============================================================ 事件

    def _state(self):
        """把所有控件读成 GenerationState。这是 GUI → 引擎的唯一出口。"""
        def or_none(s):
            return s or None

        maxlen = self.v_maxlen.get().strip()
        # 下拉里是「SQL 注入 sqli」这种显示文本，这里统一还原成 id。
        # unlabel 对没登记标签的值原样返回，所以「（不限）」这类哨兵值也安全。
        return engine.make_state(
            vuln=or_none(self.U("vuln", self.v_vuln.get())),
            component=or_none(self.v_component.get()) if self.v_component.get() != "（不限）" else None,
            content_type=(or_none(self.U("content_type", self.v_ctype.get()))
                          if self.v_ctype.get() != "（不限）" else None),
            version=or_none(self.v_version.get().strip()),
            without=[k for k, v in self.without_vars.items() if v.get()],
            template=self.selected_template,
            slots={k: self._slot_ids.get(k, {}).get(v.get(), v.get())
                   for k, v in self.slot_widgets.items()},
            vars={k: v.get() for k, v in self.var_entries.items()},
            bypasses=list(self.bypass_order),
            submit=self.U("submit", self.v_submit.get()),
            method=self.v_method.get(),
            host=self.v_host.get(),
            path=self.v_path.get(),
            param=self.v_param.get(),
            header=self.v_header.get() or None,
            cookie=self.v_cookie.get() or None,
            shell=self.v_shell.get(),
            part=self.v_part.get(),
            outputs=[n for n, v in self.output_vars.items() if v.get()] or ["raw"],
            constraints={
                "max_len": int(maxlen) if maxlen.isdigit() else None,
                "forbidden_chars": [c for c in self.v_forbid.get().split(",") if c],
                "blocked_keywords": [k.strip() for k in self.v_block.get().split(",") if k.strip()],
            },
        )

    def refresh(self):
        """状态变了：重算可用模板、必要时重建参数区、重跑生成。"""
        try:
            state = self._state()
            rows = engine.filter_templates(self.rules, state)
        except engine.RuleError as e:
            self._set_diag([("错误", str(e))], "")
            return

        usable = [r for r in rows if r["usable"]]
        # 只把「因为缺少前提」被筛掉的列出来。漏洞类型/组件/内容类型不符的那些
        # 是用户自己选的筛子，把它们也塞进列表只会制造噪音
        # （选了 sqli 却看到 92 条别的类型）——要换类型直接改上面的下拉框。
        excluded = [r for r in rows
                    if not r["usable"] and (r["reason"] or "").startswith("缺少前提")]
        self.lbl_count.configure(text="可用 %d 条 · 缺前提 %d 条" % (len(usable), len(excluded)))
        self._refresh_require_counts(usable)

        # 列表行：可用在前，被筛掉的按开关追加在后。
        # 维护一份与 Listbox 一一对应的 row 数组来定位——用 "%-40s %s" 切开取 id 太脆。
        self.lst_rows = list(usable) + (excluded if self.v_show_excluded.get() else [])
        shown = [self._row_label(r) for r in self.lst_rows]
        if shown != list(self.lst_templates.get(0, "end")):
            self.lst_templates.delete(0, "end")
            for i, r in enumerate(self.lst_rows):
                self.lst_templates.insert("end", shown[i])
                if not r["usable"]:
                    self.lst_templates.itemconfig(i, foreground="#999")
            ids = [r["id"] for r in self.lst_rows]
            if self.selected_template in ids:
                self.lst_templates.selection_set(ids.index(self.selected_template))
            elif self.lst_rows:
                self.selected_template = self.lst_rows[0]["id"]
                self._params_for = None      # 选中项被动变了，参数区必须跟着重建
                self.lst_templates.selection_set(0)
            else:
                self.selected_template = None
                self._params_for = None

        self._rebuild_params_if_needed()
        self._refresh_bypass_conflicts()
        self._render(rows)

    @staticmethod
    def _row_label(row):
        if row["usable"]:
            return "%-38s %s" % (row["id"], row["name"])
        return "✗ %-36s %s  ← %s" % (row["id"], row["name"], row["reason"] or row["note"])

    def _refresh_require_counts(self, usable):
        """在勾选框上标出「勾上它会筛掉几条」。

        只统计**当前可用**的模板——已经勾上的前提会显示 0，
        因为那些模板早就被筛掉了，勾不勾都一样。
        """
        counts = {}
        for r in usable:
            for k in r["template"].get("requires") or []:
                counts[k] = counts.get(k, 0) + 1
        for key, (cb, name) in self.require_checks.items():
            n = counts.get(key, 0)
            cb.configure(text="%s (%d)" % (name, n) if n else name)

    def on_pick_template(self):
        sel = self.lst_templates.curselection()
        if not sel:
            return
        tid = self.lst_rows[sel[0]]["id"]
        if tid == self.selected_template:
            return
        self.selected_template = tid
        # 必须把缓存键也清掉：下面的控件字典会被清空，若缓存键还在，
        # _rebuild_params_if_needed 会跳过重建，参数区就空了。
        self._params_for = None
        self.var_entries.clear()
        self.slot_widgets.clear()
        # 绕过链**不**清空：绕过作用于组装好的字符串，与选哪条模板无关，
        # 换模板时保留它更符合"搭积木"的用法
        self.refresh()

    def relax_and_pick(self, keys):
        """放宽指定的前提，然后选中原来那条模板。

        这是 §4.1「放宽哪个条件能看到它」的可点击版本——
        CLI 只能把建议打印出来让用户自己重打参数。
        """
        tid = self.selected_template
        for k in keys:
            if k in self.without_vars:
                self.without_vars[k].set(False)
        self._params_for = None
        self.refresh()
        ids = [r["id"] for r in self.lst_rows]
        if tid in ids:
            self.selected_template = tid
            self.lst_templates.selection_clear(0, "end")
            self.lst_templates.selection_set(ids.index(tid))
            self._params_for = None
            self.refresh()

    def _rebuild_params_if_needed(self):
        """槽位/变量控件只在**选中的模板变了**时重建。

        每次按键都重建会打断输入（焦点丢失、光标跳走），所以按 template id 比对。
        """
        tid = self.selected_template
        row = next((r for r in self.lst_rows if r["id"] == tid), None)
        # 缓存键要带上「可用与否」——被筛掉的模板放宽后要重建面板
        key = (tid, row["usable"] if row else None)
        if key == self._params_for:
            return
        self._params_for = key
        for w in self.params.winfo_children():
            w.destroy()

        if row is None:
            ttk.Label(self.params, text="（没有可用模板）").grid(row=0, column=0, sticky="w")
            return
        if not row["usable"]:
            self._build_excluded_panel(row)
            return
        tpl = row["template"]

        ttk.Label(self.params, text=tpl.get("name", ""), font=("", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        if tpl.get("desc"):
            ttk.Label(self.params, text=tpl["desc"], wraplength=420, justify="left").grid(
                row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))

        r = 2
        keep_slots = {k: v.get() for k, v in self.slot_widgets.items()}
        self.slot_widgets = {}
        # 槽位控件里存的是**显示文本**（字典选项显示它的 name），
        # 这里记下「显示文本 -> 取值 id」的反查表，_state() 靠它还原创始值。
        self._slot_ids = {}
        for name, spec in (tpl.get("slots") or {}).items():
            ttk.Label(self.params, text="槽位 " + self.L("slot", name)).grid(
                row=r, column=0, sticky="e", padx=(0, 4))
            opts = spec["options"]
            values = [engine.option_text(o) for o in opts]
            self._slot_ids[name] = {engine.option_text(o): engine.option_id(o) for o in opts}
            # 沿用旧值前先确认它在新模板的这个槽位里合法——同名槽位的候选集可能不同
            old = keep_slots.get(name)
            if old in values:
                default = old
            else:
                want = spec.get("default", engine.option_id(opts[0]))
                default = next((engine.option_text(o) for o in opts
                                if engine.option_id(o) == want), values[0])
            var = tk.StringVar(value=default)
            cb = ttk.Combobox(self.params, textvariable=var, values=values, width=30, state="readonly")
            cb.grid(row=r, column=1, sticky="w")
            cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
            self.slot_widgets[name] = var
            r += 1

        keep_vars = {k: v.get() for k, v in self.var_entries.items()}
        self.var_entries = {}
        for name in tpl.get("vars") or []:
            ttk.Label(self.params, text="变量 " + self.L("var", name)).grid(
                row=r, column=0, sticky="e", padx=(0, 4))
            var = tk.StringVar(value=keep_vars.get(name, ""))
            e = ttk.Entry(self.params, textvariable=var, width=30)
            e.grid(row=r, column=1, sticky="w")
            e.bind("<KeyRelease>", lambda ev: self.refresh())
            self.var_entries[name] = var
            r += 1

        if tpl.get("requires"):
            names = self.rules["requires_name"]
            ttk.Label(self.params, text="需要前提：" + "、".join(names.get(x, x) for x in tpl["requires"]),
                      foreground="#666").grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _build_excluded_panel(self, row):
        """被筛掉的模板：说清原因，能点的一步放宽。

        这是 §4.1 后半句的 GUI 形态。「放宽哪个条件能看到它」在 CLI 里只是一行文本，
        用户还得自己把参数重打一遍；这里直接给按钮。
        """
        tpl = row["template"]
        ttk.Label(self.params, text=tpl.get("name", ""), font=("", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(self.params, text="被筛掉：" + (row["reason"] or row["note"] or ""),
                  foreground="#b00").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))
        if tpl.get("desc"):
            ttk.Label(self.params, text=tpl["desc"], wraplength=420, justify="left").grid(
                row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))

        # 只有「缺少前提」这一种排除是用户能一键放宽的；
        # 漏洞类型/组件不符是硬性的选择错误，放宽它没有意义。
        without = {k for k, v in self.without_vars.items() if v.get()}
        missing = [k for k in (tpl.get("requires") or []) if k in without]
        r = 3
        if missing:
            names = self.rules["requires_name"]
            ttk.Label(self.params, text="它需要：" + "、".join(names.get(k, k) for k in missing),
                      foreground="#666", wraplength=420, justify="left").grid(
                row=r, column=0, columnspan=2, sticky="w", pady=(0, 4))
            r += 1
            ttk.Button(self.params, text="放宽这 %d 项并用它" % len(missing),
                       command=lambda: self.relax_and_pick(missing)).grid(
                row=r, column=0, columnspan=2, sticky="w")
        else:
            ttk.Label(self.params,
                      text="这不是前提勾选造成的——检查上面的漏洞类型 / 组件 / 内容类型筛选。",
                      foreground="#666", wraplength=420, justify="left").grid(
                row=r, column=0, columnspan=2, sticky="w")

    # ---- 绕过 ----

    def _refresh_bypass_conflicts(self):
        """互斥提示走引擎的 visible_bypasses——同一份逻辑，TUI 和 GUI 共用。"""
        st = engine.make_state(bypasses=self.bypass_order)
        pairs = engine.bypass_pairs(self.rules, st)
        self.lbl_conflict.configure(
            text=("互斥：" + "；".join("%s 与 %s" % (a, b) for a, b in pairs)) if pairs else "")

    def _redraw_bypass(self):
        self.lst_bypass.delete(0, "end")
        names = {b["id"]: b.get("name", b["id"]) for b in self.rules["bypasses"]}
        for bid in self.bypass_order:
            self.lst_bypass.insert("end", "%-32s %s" % (bid, names.get(bid, "")))

    def add_bypass(self):
        # 下拉给的是「URL 编码 bypass.url」这种显示文本，先还原成 id；
        # 直接写 id（脚本化调用）也认。
        raw = self.v_addbypass.get()
        bid = self._bypass_label.get(raw, raw)
        if bid and bid not in self.bypass_order:
            self.bypass_order.append(bid)
            self._redraw_bypass()
            self.refresh()

    def remove_bypass(self):
        sel = self.lst_bypass.curselection()
        if sel:
            del self.bypass_order[sel[0]]
            self._redraw_bypass()
            self.refresh()

    def move_bypass(self, delta):
        sel = self.lst_bypass.curselection()
        if not sel:
            return
        i = sel[0]
        j = i + delta
        if 0 <= j < len(self.bypass_order):
            self.bypass_order[i], self.bypass_order[j] = self.bypass_order[j], self.bypass_order[i]
            self._redraw_bypass()
            self.lst_bypass.selection_set(j)
            self.refresh()

    # ---- 渲染 ----

    def _render(self, rows):
        try:
            res = engine.generate(self.rules, self._state())
        except engine.RuleError as e:
            self._tabs({"错误": str(e)})
            self.lbl_status.configure(text="错误")
            return

        tabs = {}
        if res["ok"]:
            tabs.update({name: text for name, text in res["outputs"]})
            tabs["组装链"] = "\n".join("%-14s %s" % (s["step"], s["text"]) for s in res["steps"])
            diag = ["语法: " + res["syntax"]["message"]]
            diag += ["提示: " + c for c in res["checks"]]
            if res.get("note"):
                diag.append("标记: " + res["note"])
            tabs["诊断"] = "\n".join(diag) or "（无）"
            self.lbl_status.configure(text="模板 %s" % res["template"])
        else:
            tabs["无可用模板"] = self._empty_message(rows)

        self._tabs(tabs)

    def _empty_message(self, rows):
        sug = engine.relax_suggestions(self.rules, self._state())
        lines = ["当前条件下没有可用模板。", ""]
        if sug["suggestions"]:
            lines.append("放宽下列任一条即可看到结果：")
            lines += ["  %-40s 多出 %2d 条（共 %d 条）" % (s["desc"], s["gain"], s["total"])
                      for s in sug["suggestions"]]
        else:
            lines.append("没有可放宽的筛子——规则库本身可能没覆盖这个组合。")
        lines += ["", "被排除的模板及原因："]
        lines += ["  %-42s %s" % (r["id"], r["reason"]) for r in rows if not r["usable"]]
        return "\n".join(lines)

    def _tabs(self, mapping):
        """按 mapping 重建 Notebook。

        每次都整块重建——保持「渲染结果完全由引擎产出决定」这条不变式，
        不在 GUI 侧维护任何增量状态。
        """
        for w in self.nb.winfo_children():
            w.destroy()
        self._tab_texts = {}
        for name, text in mapping.items():
            frame = ttk.Frame(self.nb)
            st = ScrolledText(frame, wrap="none", height=5, font=("Consolas", 10))
            st.pack(fill="both", expand=True)
            st.insert("1.0", text)
            st.configure(state="disabled")
            self.nb.add(frame, text=name)
            # 存下文本框本身：nametowidget 拿到的是外层的 Frame，它没有 .get()
            self._tab_texts[name] = st

    def _set_diag(self, pairs, status):
        self._tabs({k: v for k, v in pairs})
        self.lbl_status.configure(text=status)

    def copy_current(self):
        idx = self.nb.index("current")
        if idx is None:
            return
        name = self.nb.tab(idx, "text")
        text = self._tab_texts[name].get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.lbl_status.configure(text="已复制「%s」%d 字符" % (name, len(text)))


def build(root, rules):
    """把整个界面装到 root 上，返回 App。

    **界面怎么组装只能有这一处。** `main()` 和 `run_golden.py` 的 GUI 冒烟都调它，
    否则测试测的是另一套组装方式，测过了也说明不了什么。

    最外层套一个滚动容器：窗口比内容矮的时候，宁可让用户滚，
    也别让 grid 把结果区按权重压成十几像素。
    """
    scroll = ScrollArea(root)
    scroll.grid(row=0, column=0, sticky="nsew")
    root.rowconfigure(0, weight=1)
    root.columnconfigure(0, weight=1)
    app = App(scroll.inner, rules)
    app.root_scroll = scroll
    return app


def main():
    root = tk.Tk()
    root.title("Forge · 载荷拼接器")

    # 默认尺寸**按屏幕裁**：写死 1180x920 在一部分笔记本（1366x768）上会超出屏幕，
    # 窗口底部连同结果区一起落到可视区域外面——这正是「没最大化就显示不全」的成因之一。
    w, h = 1180, 920
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = min(w, sw - 60), min(h, sh - 80)
    root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
    # 再小就真的塞不下了：筛选 + 前提 + 模板列表 + 结果，四个区各有下限。
    # 给个下限让用户拖不出一个坏掉的界面，而不是拖到一半发现控件全没了。
    root.minsize(860, 620)
    try:
        rules = engine.load_rules(DATA_DIR)
    except Exception as e:  # noqa: BLE001
        from tkinter import messagebox
        messagebox.showerror("规则库加载失败", str(e))
        return 1
    build(root, rules)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
