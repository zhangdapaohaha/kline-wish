# -*- coding: utf-8 -*-
"""
KLineWish（K线许愿）—— 会生长的 K 线蜡烛（桌面可拖拽动效控件，纯 tkinter，零依赖）

形态：标准蜡烛图 —— 中间实心实体（开→收）+ 贯穿的细影线（最高→最低）。
      绿涨：低点钉死在底部，往上冲（实体变长、上影线拉出）。
      红跌：高点钉死在顶部，往下砸（实体变长、下影线拉出）。
      价格以「阶跃」方式跳动（窜 → 停 → 再窜），回落时实体缩短但影线保留 = 典型冲高回落。
      极小概率直接一步冲到顶/底。每个周期结束后整根蜡烛清零，重新生长。

操作：
  左键拖动           移动位置
  滚轮               调波动尺度（高度）
  Shift + 滚轮       调宽度
  左键双击           暂停 / 继续
  右键               菜单（深色自绘：尺寸 / 趋势 / 穿刺 / 置顶 / 跟随窗口 / 退出）
                     菜单点击后不收起，鼠标移出菜单区域才收起

实现要点（踩过的坑，改代码前先看）：
  * tkinter 的 BaseWidget 用 self._w 存窗口路径名。自定义组件里存宽高千万别叫
    self._w / self._h，一覆盖就报「bad window path name "0"」，之后所有 Tk 命令静默失败。
  * 本进程是 DPI 感知缺失的：屏幕物理 3840x2160 时 GetSystemMetrics 只报 1920x1080，
    窗口坐标会被系统放大 2 倍。截图验证时记得把坐标乘上缩放系数，否则永远截不到东西。
  * 菜单子窗口必须挂在父菜单（master=self）而不是 app root：grab 只覆盖自身后代窗口，
    挂在 root 下的话子菜单收不到任何点击。
"""
import ctypes
import math
import os
import random
import time
import tkinter as tk
from tkinter import font as tkfont

BG = "black"
GREEN = "#0ECB81"       # 涨
RED = "#F6465D"         # 跌
GREEN_HI = "#A8FFDC"    # 最新价闪光（涨）
RED_HI = "#FFC2CB"      # 最新价闪光（跌）

STEP_DUR = 0.12         # 一次阶跃用时（短 → 才有"窜"的爆发感）
CYCLE = 7.0             # 一个 K 线周期时长（秒），到点清零重来
EDGE = 6                # 窗口内边距
UNDER = 56              # 为反向插针预留的空间（跌破开盘价的下影线 / 涨破开盘价的上影线）

# 穿刺：窗口临时撑到屏幕边缘，让蜡烛真的捅到天际线 / 砸到屏幕底
PIERCE_INTERVAL = 60.0  # 穿刺间隔（秒）
PIERCE_RUSH = 0.18      # 冲到边缘
PIERCE_HOLD = 0.85      # 停在边缘
PIERCE_BACK = 1.05      # 缩回

# 反向插针：涨着涨着突然翻红往下砸一下（或反过来），时间短，拉回后影线保留
# 幅度走指数分布：小幅常见、极端罕见；砸得越深停得越短（大红一闪而过）
INVERT_CD = (8.0, 20.0)     # 触发间隔随机区间（秒）
INVERT_DOWN = 0.15          # 砸/拉出去
INVERT_UP = 0.24            # 快速拉回


# ---------------- 现代化右键菜单 ----------------
# tkinter 原生 Menu 是 90 年代系统样式，改不动；这里自绘一个深色圆角卡片菜单。
class ModernMenu(tk.Toplevel):
    CARD = "#1B1D21"
    BORDER = "#2C2F36"
    HOVER = "#282C33"
    LINE = "#26292F"
    TXT = "#DDE1E6"
    TXT_DIM = "#868C96"
    ACCENT = "#0ECB81"
    WARN = "#F6465D"
    FONT = ("Microsoft YaHei UI", 9)
    ITEM_H = 30
    SLIDER_H = 34
    SEP_H = 9
    PADX = 14
    TOP = 6
    RADIUS = 10

    def __init__(self, master):
        tk.Toplevel.__init__(self, master)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-transparentcolor", "#000000")
        except tk.TclError:
            self.configure(bg=self.CARD)
        self.configure(bg="#000000")
        self.cv = tk.Canvas(self, bg="#000000", highlightthickness=0, bd=0)
        self.cv.pack()
        self._items = []
        self._rows = []
        self.hover_idx = -1
        self._sub = None
        self._parent = None
        self._builder = None      # 构建函数（命令执行后 _rebuild 原地刷新用）
        self._row_idx = None      # 子菜单在父菜单里的行号（刷新后重定位用）
        self._armed = False       # 鼠标是否曾进入过菜单链
        self._out = 0             # 连续多少拍采样落在菜单链外
        # ⚠️ 千万别用 self._w / self._h 存宽高：tkinter 内部用 self._w 存窗口路径名，
        #    一覆盖就变成「bad window path name "0"」，之后所有 Tk 命令静默失败。
        self.mw = 0
        self.mh = 0
        self._font = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self._slider_drag = False   # 正在拖滑条（此时菜单不因鼠标外移而收起）
        self._drag_item = None
        self.cv.bind("<Motion>", self._motion)
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<B1-Motion>", self._slider_motion)
        self.cv.bind("<ButtonRelease-1>", self._slider_release)
        self.cv.bind("<MouseWheel>", self._wheel)
        self.cv.bind("<Leave>", lambda e: self._set_hover(-1))
        self.bind("<Escape>", lambda e: self.close_chain())

    # ---- 构造 ----
    def add_item(self, label, cmd=None, checked=None, sub=None, danger=False,
                 disabled=False, color=None):
        self._items.append(dict(kind="item", label=label, cmd=cmd, checked=checked,
                                sub=sub, danger=danger, disabled=disabled,
                                sep=False, color=color))

    def add_sep(self):
        self._items.append(dict(kind="sep", sep=True))

    def add_slider(self, label, value, vmin, vmax, cmd, unit=" px"):
        """滑条行：可拖动 + 滚轮微调，浮动值 0.5 步进。"""
        self._items.append(dict(kind="slider", label=label, value=float(value),
                                min=float(vmin), max=float(vmax), cmd=cmd,
                                unit=unit, sep=False))

    # ---- 绘制 ----
    @staticmethod
    def _rr(x1, y1, x2, y2, r):
        pts = []
        for cx, cy, a0 in ((x2 - r, y1 + r, -90), (x2 - r, y2 - r, 0),
                           (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
            for i in range(5):
                a = math.radians(a0 + i * 22.5)
                pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        return pts

    def _render(self):
        TRACK = 130                      # 滑条轨道像素长
        w_text = 0
        for it in self._items:
            if it.get("sep"):
                continue
            if it.get("kind") == "slider":
                need = (self._font.measure(it["label"]) + 8 + TRACK + 8
                        + self._font.measure(f"{it['max']:.0f}{it['unit']}")
                        + self.PADX * 2 + 8)
                w_text = max(w_text, need)
                continue
            extra = 18 if it.get("checked") is not None else 0
            extra += 16 if it.get("sub") else 0
            w_text = max(w_text, self._font.measure(it["label"]) + extra)
        w = int(w_text + self.PADX * 2 + 8)
        h = int(self.TOP * 2 + sum(
            self.SEP_H if it.get("sep")
            else (self.SLIDER_H if it.get("kind") == "slider" else self.ITEM_H)
            for it in self._items))
        self.mw, self.mh = w, h
        self.cv.config(width=w, height=h)
        self.cv.delete("all")
        self.hover_idx = -1       # 画布清了，高亮作废；_rehover 会按当前鼠标位置补画
        self.cv.create_polygon(self._rr(1, 1, w - 1, h - 1, self.RADIUS),
                               fill=self.CARD, outline=self.BORDER, width=1, tags="bg")

        y = self.TOP
        self._rows = []
        for it in self._items:
            if it.get("sep"):
                self.cv.create_line(self.PADX, y + self.SEP_H / 2,
                                    w - self.PADX, y + self.SEP_H / 2,
                                    fill=self.LINE, tags="txt")
                self._rows.append(None)
                y += self.SEP_H
                continue
            row_h = self.SLIDER_H if it.get("kind") == "slider" else self.ITEM_H
            y2 = y + row_h
            x = self.PADX
            if it.get("checked") is not None:
                mark = "●" if it["checked"] else "○"
                self.cv.create_text(x + 5, y + row_h / 2, text=mark,
                                    fill=self.ACCENT if it["checked"] else self.TXT_DIM,
                                    font=self.FONT, anchor="center", tags="txt")
                x += 18
            color = it.get("color") or (self.WARN if it.get("danger") else
                                        (self.TXT_DIM if it.get("disabled") else self.TXT))
            self.cv.create_text(x, y + row_h / 2, text=it["label"], fill=color,
                                font=self.FONT, anchor="w", tags="txt")
            if it.get("kind") == "slider":
                lw = self._font.measure(it["label"])
                val_txt = f"{it['value']:.1f}".rstrip("0").rstrip(".") + it["unit"]
                vx = w - self.PADX
                self.cv.create_text(vx, y + row_h / 2, text=val_txt,
                                    fill=self.ACCENT, font=self.FONT, anchor="e", tags="txt")
                vw = self._font.measure(val_txt)
                tx1, tx2 = x + lw + 8, vx - vw - 8
                ty = y + row_h / 2
                t = (it["value"] - it["min"]) / max(1e-6, it["max"] - it["min"])
                t = min(1.0, max(0.0, t))
                it["track"] = (tx1, tx2)
                self.cv.create_line(tx1, ty, tx2, ty, fill=self.LINE,
                                    width=4, capstyle=tk.ROUND, tags="txt")
                self.cv.create_line(tx1, ty, tx1 + t * (tx2 - tx1), ty, fill=self.ACCENT,
                                    width=4, capstyle=tk.ROUND, tags="txt")
                hx = tx1 + t * (tx2 - tx1)
                self.cv.create_oval(hx - 5, ty - 5, hx + 5, ty + 5,
                                    fill="#FFFFFF", outline=self.ACCENT, width=1, tags="txt")
                self._rows.append((y, y2))
                y = y2
                continue
            if it.get("sub"):
                self.cv.create_text(w - self.PADX, y + row_h / 2, text="›",
                                    fill=self.TXT_DIM, font=self.FONT, anchor="e", tags="txt")
            self._rows.append((y, y2))
            y = y2

    # ---- 交互 ----
    def _idx_at(self, y):
        for i, r in enumerate(self._rows):
            if r and r[0] <= y <= r[1]:
                return i
        return None

    def _motion(self, e):
        self._set_hover(self._idx_at(e.y) if self._idx_at(e.y) is not None else -1)

    def _set_hover(self, idx, manage_sub=True):
        if idx == self.hover_idx:
            return
        self.hover_idx = idx
        self.cv.delete("hov")
        if idx is None or idx < 0 or self._items[idx].get("disabled"):
            return
        y1, y2 = self._rows[idx]
        self.cv.create_polygon(self._rr(self.PADX - 10, y1 + 2, self.mw - self.PADX + 10, y2 - 2, 6),
                               fill=self.HOVER, outline="", tags="hov")
        self.cv.tag_lower("hov", "txt")
        if not manage_sub:        # _rehover 只补高亮，不碰子菜单链
            return
        # 悬停即切换子菜单（与真实菜单行为一致）
        it = self._items[idx]
        if self._sub is not None:
            if it["sub"]:
                self._open_sub(idx, it["sub"])
            else:
                self._sub.close_all()
                self._sub = None

    def _click(self, e):
        idx = self._idx_at(e.y)
        if idx is None or self._items[idx].get("disabled"):
            return
        it = self._items[idx]
        if it.get("kind") == "slider":
            # 滑条：按下即开始拖动（拖动中菜单不收起，见 _poll_leave）
            self._slider_drag = True
            self._drag_item = it
            self._slider_set(it, e.x)
            return
        if it["sub"]:
            self._open_sub(idx, it["sub"])
        elif it["cmd"]:
            # 点了不关菜单：执行命令后原地刷新整条链，鼠标移出菜单区域才收（见 _poll_leave）
            it["cmd"]()
            try:
                self._refresh_chain()
            except Exception:
                pass

    # ---- 滑条交互 ----
    def _slider_set(self, it, x):
        """按像素 x 换算数值（0.5 步进），变了才回调 + 重画。"""
        try:
            tx1, tx2 = it.get("track", (0.0, 1.0))
            t = (x - tx1) / max(1.0, tx2 - tx1)
            t = min(1.0, max(0.0, t))
            v = it["min"] + t * (it["max"] - it["min"])
            v = round(v * 2) / 2.0
            if v != it["value"]:
                it["value"] = v
                it["cmd"](v)
                self._render()
                self._rehover()
        except Exception:
            pass

    def _slider_motion(self, e):
        if self._slider_drag and self._drag_item is not None:
            self._slider_set(self._drag_item, e.x)

    def _slider_release(self, e):
        self._slider_drag = False
        self._drag_item = None

    def _wheel(self, e):
        """悬停滑条上时滚轮 ±1 步进微调。"""
        idx = self._idx_at(e.y)
        if idx is None:
            return
        it = self._items[idx]
        if it.get("kind") != "slider":
            return
        step = 1.0 if e.delta > 0 else -1.0
        v = min(it["max"], max(it["min"], it["value"] + step))
        if v != it["value"]:
            it["value"] = v
            it["cmd"](v)
            self._render()
            self._rehover()

    def _open_sub(self, idx, builder):
        if self._sub:
            self._sub.close_all()
        # 关键：master 传 self（父菜单）而不是 app root。
        # 这样子菜单既是父菜单 grab 的「后代」能收到点击，Win32 上又天然压在父菜单之上。
        m = ModernMenu(self)
        m._parent = self
        m._builder = builder
        m._row_idx = idx
        try:
            builder(m)
            m.popup(self.winfo_x() + self.mw - 8, self.winfo_y() + self._rows[idx][0] - self.TOP)
        except Exception:
            # 同上：失败就销毁，别留僵尸窗口（子菜单打不开只是降级，不漏窗）
            try:
                m.destroy()
            except Exception:
                pass
            return
        self._sub = m

    def _inside(self, x, y):
        try:
            return (self.winfo_x() <= x <= self.winfo_x() + self.mw
                    and self.winfo_y() <= y <= self.winfo_y() + self.mh)
        except Exception:
            return False

    def _global_click(self, e):
        """只有根菜单会挂这个监听。点在菜单链任意一环内都不关，否则整条收掉。"""
        try:
            node = self
            while node:
                if node._inside(e.x_root, e.y_root):
                    return
                node = node._sub
            self.close_chain()
        except Exception:
            pass

    # ---- 生命周期 ----
    def popup(self, x, y):
        self._render()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = min(x, sw - self.mw - 4)
        y = min(y, sh - self.mh - 4)
        self.geometry(f"{self.mw}x{self.mh}+{int(max(0, x))}+{int(max(0, y))}")
        self.deiconify()
        self.lift()
        if self._parent is None:
            try:
                self.grab_set()      # 偶发「another application has grab」，失败不影响使用
            except tk.TclError:
                pass
            self.bind_all("<Button-1>", self._global_click, "+")
            self._armed = False
            self._out = 0
            self.after(50, self._poll_leave)   # 鼠标脱离菜单区才收
        self.focus_force()

    def close_chain(self):
        root = self
        while root._parent:
            root = root._parent
        root.close_all()

    def close_all(self):
        try:
            if self._sub:
                sub = self._sub
                self._sub = None
                sub._parent = None
                sub.close_all()
        except Exception:
            pass
        try:
            self.grab_release()
        except Exception:
            pass
        if self._parent is None:
            try:
                self.unbind_all("<Button-1>")   # 只有根菜单删，别误伤别人
            except Exception:
                pass
        else:
            self._parent._sub = None
        self.destroy()

    # ---- 点了不关：原地刷新 + 鼠标脱离菜单区才收 ----
    def _refresh_chain(self):
        """命令执行后，从根菜单起把整条链按最新状态重画（位置不变、子菜单保持打开）。"""
        try:
            node = self
            while node._parent:
                node = node._parent
            if node.winfo_exists():
                node._rebuild()
        except Exception:
            pass

    def _rebuild(self):
        self._items = []
        if self._builder:
            self._builder(self)
        self._render()
        x, y = self.winfo_x(), self.winfo_y()
        self.geometry(f"{self.mw}x{self.mh}+{int(x)}+{int(y)}")
        if self._sub is not None:
            self._sub._rebuild()                      # 先按新内容重算大小
            idx = self._sub._row_idx
            if idx is not None and idx < len(self._rows):
                self._sub._reposition(x + self.mw - 8, y + self._rows[idx][0] - self.TOP)
        self._rehover()

    def _reposition(self, x, y):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = min(x, sw - self.mw - 4)
        y = min(y, sh - self.mh - 4)
        self.geometry(f"{self.mw}x{self.mh}+{int(max(0, x))}+{int(max(0, y))}")

    def _rehover(self):
        """重建后补画悬停高亮：画布被清空了，鼠标可没动。"""
        try:
            x, y = self.winfo_pointerxy()
            ry = y - self.winfo_y()
            idx = self._idx_at(ry) if 0 <= ry <= self.mh else None
            self._set_hover(idx if idx is not None else -1, manage_sub=False)
        except Exception:
            pass

    def _poll_leave(self):
        """每 50ms 采样全局鼠标：在菜单链（根+所有子菜单）内就继续开着；
        曾进来过且连续两拍落在链外 → 收掉；从没进来过给 2 秒宽限（防贴边弹出时误关）；
        滑条拖动中一律不收（允许鼠标暂时拖出边缘）。"""
        try:
            if not self.winfo_exists():
                return
            x, y = self.winfo_pointerxy()
            node, inside, dragging = self, False, False
            while node:
                if node._inside(x, y):
                    inside = True
                if getattr(node, "_slider_drag", False):
                    dragging = True
                node = node._sub
            if inside:
                self._armed = True
                self._out = 0
            elif dragging:
                self._out = 0
            else:
                self._out += 1
                if (self._armed and self._out >= 2) or (not self._armed and self._out >= 40):
                    self.close_chain()
                    return
            self.after(50, self._poll_leave)
        except Exception:
            pass


class ArrowPet:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("KLineWish")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", BG)
        except tk.TclError:
            self.root.attributes("-alpha", 0.92)
        self.root.configure(bg=BG)

        # ---- 参数 ----
        self.bar_w = 11.0         # 实体宽度
        self.base_h = 92.0        # 波动尺度
        self.trend_up = True      # 绿涨 = 总体向上
        self.color_invert = False  # 颜色反转：True = 红涨绿跌（照顾传统行情软件配色习惯）

        self.paused = False
        self.topmost = True
        self.t = 0.0

        # 蜡烛状态：p = 当前价（相对开盘的像素位移，可为负 = 跌破开盘价）
        self.p = 0.0
        self.p_from = 0.0
        self.p_to = 0.0
        self.hi = 0.0                # 历史最高（保留 → 上影线）
        self.lo = 0.0                # 历史最低（可为负，保留 → 下影线）
        self.stepping = False
        self.step_t = 0.0
        self.idle_left = 0.25
        self.pending_pull = False
        self.flash = 0.0
        self.impulse = 0.0
        self.cycle_t = 0.0

        # 反向插针
        self.inverting = False
        self.invert_t = 0.0
        self.invert_from = 0.0
        self.invert_to = 0.0
        self.invert_back = 0.0
        self.invert_hold = 0.40
        self.invert_cd = random.uniform(*INVERT_CD)

        # 跟随某个窗口（同图层、随其关闭）
        self._self_hwnd = 0
        self._last_fg = 0            # 用户最后激活过的窗口（每 0.25s 采样一次）
        self._fg_t = 0.0
        self.bound_hwnd = 0
        self.bound_title = ""
        self._chk = 0.0

        # 穿刺
        self.pierce_on = True
        self.pierce_cd = 12.0        # 启动 12 秒后先演示一次
        self.piercing = False
        self.pierce_t = 0.0
        self.pierce_from = 0.0
        self.pierce_max = 0.0
        self.anchor_y = 0

        self.win_w = 0
        self.win_h = 0
        self.bw = 0
        self.bh = 0
        self._last = time.perf_counter()

        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack()

        self._bind()
        self._resize(center_screen=True)
        self.root.after(0, self._tick)

    # ---------------- 菜单（动态构建） ----------------
    def _popup(self, e):
        # 菜单开着时再右键：先把旧链收掉，否则旧菜单永不消失（窗口泄漏）
        old = getattr(self, "_menu", None)
        if old is not None:
            try:
                old.close_chain()
            except Exception:
                pass
            self._menu = None
        m = ModernMenu(self.root)
        m._builder = self._fill_menu
        try:
            self._fill_menu(m)
            m.popup(e.x_root, e.y_root)
            self._menu = m
        except Exception:
            # 构造/填充失败也必须销毁窗口，否则留下 (0,0) 透明僵尸窗
            try:
                m.destroy()
            except Exception:
                pass

    def _fill_menu(self, m):
        # 趋势项显示的是「点了会变成什么」：当前上涨 → 显示 下跌 ↓；点了翻成下跌 → 再显示 上涨 ↑
        # 标签颜色跟随当前配色方案：默认绿涨红跌（下跌↓=红）；反转后红涨绿跌（下跌↓=绿）
        # ⚠️ WARN/ACCENT 是 ModernMenu 的类属性，ArrowPet 上没有，必须走类名引用
        down_color = ModernMenu.ACCENT if self.color_invert else ModernMenu.WARN
        up_color = ModernMenu.WARN if self.color_invert else ModernMenu.ACCENT
        m.add_item("下跌 ↓" if self.trend_up else "上涨 ↑",
                   cmd=self._flip_trend,
                   color=down_color if self.trend_up else up_color)
        m.add_item("尺寸", sub=self._fill_size)
        m.add_item("颜色反转", checked=self.color_invert, cmd=self._flip_invert)
        m.add_item("始终置顶", checked=self.topmost, cmd=self._flip_top)
        m.add_item("跟随窗口", checked=self.bound_hwnd > 0, sub=self._fill_bind)
        m.add_sep()
        m.add_item("退出", cmd=self.root.destroy, danger=True)

    def _fill_size(self, m):
        m.add_item("↺ 重置尺寸", cmd=self._reset_size)
        m.add_sep()
        m.add_slider("实体宽", self.bar_w, 5, 30, self._set_w)
        m.add_sep()
        for label, v in (("小", 60.0), ("标准", 92.0), ("大", 140.0), ("巨大", 220.0)):
            m.add_item(f"波动幅度 · {label}", cmd=lambda v=v: self._set_h(v),
                       checked=(self.base_h == v))
        m.add_sep()
        m.add_item("涨破天际 / 跌穿屏幕", checked=self.pierce_on, cmd=self._flip_pierce)

    def _fill_bind(self, m):
        if self.bound_hwnd:
            cur = (self.bound_title or "?")[:22]
            m.add_item(f"✕ 取消跟随（{cur}）", cmd=self._unbind, danger=True)
        # 目标 = 最后激活过的窗口；采不到就退回 Z 序里排最前的那个
        h = self._last_fg
        if not h or not ctypes.windll.user32.IsWindow(h):
            w = self._enum_windows()
            h = w[0][0] if w else 0
        info = self._info_of(h) if h else None
        if info:
            name = f"{info[1]} — {info[0]}"
            if len(name) > 30:
                name = name[:29] + "…"
            m.add_item(f"★ 跟随《{name}》", cmd=self._bind_current)
        else:
            m.add_item("★ 跟随当前窗口（先切到目标再右键）", disabled=True)
        m.add_sep()
        for hwnd, title, proc in self._enum_windows()[:16]:
            label = f"{proc} — {title}"
            if len(label) > 36:
                label = label[:35] + "…"
            m.add_item(label, cmd=lambda h=hwnd, t=title, p=proc: self._bind_to(h, f"{p} — {t}"))

    def _flip_trend(self):
        self.trend_up = not self.trend_up
        self._new_cycle()

    def _flip_invert(self):
        self.color_invert = not self.color_invert

    def _flip_pierce(self):
        self.pierce_on = not self.pierce_on
        if self.pierce_on:
            self.pierce_cd = min(self.pierce_cd, PIERCE_INTERVAL)

    def _flip_top(self):
        self.topmost = not self.topmost
        if self.topmost:
            self._unbind()                        # 全局置顶与跟随窗口互斥
        self.root.attributes("-topmost", self.topmost)
        if self.topmost:
            self.root.lift()

    # ---------------- 跟随窗口 ----------------
    def _top_hwnd(self):
        """取本进程那个真正的顶层窗口句柄。

        注意：tkinter 的 winfo_id() 拿到的是内部子窗口，GetParent(它) 会返回 0，
        所有窗口级 API 打在 0 上都会静默失效。必须自己按 PID + 标题找到顶层窗口。
        """
        if self._self_hwnd and ctypes.windll.user32.IsWindow(self._self_hwnd):
            return self._self_hwnd
        u = ctypes.windll.user32
        me = ctypes.windll.kernel32.GetCurrentProcessId()
        box = []
        Proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def cb(h, _):
            try:
                pid = ctypes.wintypes.DWORD()
                u.GetWindowThreadProcessId(h, ctypes.byref(pid))
                if pid.value == me and u.IsWindowVisible(h):
                    n = u.GetWindowTextLengthW(h)
                    if n > 0:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        u.GetWindowTextW(h, buf, n + 1)
                        if buf.value == "KLineWish":
                            box.append(int(h))
                            return False
            except Exception:
                pass
            return True

        u.EnumWindows(Proc(cb), 0)
        if box:
            self._self_hwnd = box[0]
            return self._self_hwnd
        return u.GetParent(self.root.winfo_id())

    def _info_of(self, hwnd):
        """取某窗口的 (标题, 进程名)，取不到返回 None。"""
        try:
            u = ctypes.windll.user32
            n = u.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return None
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.strip()
            if not title:
                return None
            return title, self._proc_name(hwnd)
        except Exception:
            return None

    @staticmethod
    def _proc_name(hwnd):
        """窗口所属进程的可执行文件名（如 chrome.exe / msedge.exe）。"""
        try:
            pid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == ctypes.windll.kernel32.GetCurrentProcessId():
                return ""                       # 自己人，不列
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
            if not h:
                return ""
            buf = ctypes.create_unicode_buffer(1024)
            ctypes.windll.psapi.GetModuleFileNameExW(h, None, buf, 1024)
            ctypes.windll.kernel32.CloseHandle(h)
            return os.path.basename(buf.value).lower()
        except Exception:
            return ""

    def _enum_windows(self):
        """按 Z 序（从顶到底）枚举可见有标题的顶层窗口。

        返回 [(hwnd, 标题, 进程名)]，浏览器标签页会体现为该浏览器窗口的标题。
        """
        out = []
        try:
            u = ctypes.windll.user32
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            skip_cls = ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
                        "Button", "Windows.UI.Core.CoreWindow")

            def cb(h, _):
                try:
                    if not u.IsWindowVisible(h) or u.GetWindowTextLengthW(h) <= 0:
                        return True
                    cls = ctypes.create_unicode_buffer(256)
                    u.GetClassNameW(h, cls, 256)
                    if cls.value in skip_cls:
                        return True
                    if int(h) == int(self._top_hwnd()):
                        return True
                    n = u.GetWindowTextLengthW(h)
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(h, buf, n + 1)
                    title = buf.value.strip()
                    if not title or title == "KLineWish":   # 自身窗口
                        return True
                    proc = self._proc_name(h)
                    if proc == "":                # 自身进程
                        return True
                    out.append((int(h), title, proc))
                except Exception:
                    pass
                return True

            u.EnumWindows(EnumWindowsProc(cb), 0)
        except Exception:
            pass
        return out

    def _bind_current(self):
        """跟随你最后激活过的那个窗口（浏览器里的网页 = 浏览器窗口）。"""
        h = self._last_fg
        if not h or not ctypes.windll.user32.IsWindow(h):
            w = self._enum_windows()
            if not w:
                return
            h = w[0][0]
        info = self._info_of(h) or ("?", "?")
        self._bind_to(h, f"{info[1]} — {info[0]}")


    def _bind_to(self, hwnd, title):
        """成为目标窗口的 owned 窗口：永远压在它之上，随它一起被盖住 / 一起关闭。"""
        try:
            u = ctypes.windll.user32
            self.bound_hwnd, self.bound_title = hwnd, title
            self.topmost = False
            self.top_var.set(False)
            self.root.attributes("-topmost", False)
            self_h = self._top_hwnd()
            GWLP_HWNDPARENT = -8
            u.SetWindowLongPtrW(self_h, GWLP_HWNDPARENT, hwnd)     # 成为它的 owned 窗口
            # 插到目标窗口正上方（不是 HWND_TOP，否则会盖住所有应用）
            u.SetWindowPos(self_h, hwnd, 0, 0, 0, 0,
                           0x0001 | 0x0002 | 0x0020)               # NOSIZE|NOMOVE|FRAMECHANGED
        except Exception:
            pass

    def _unbind(self):
        if not self.bound_hwnd:
            return
        try:
            ctypes.windll.user32.SetWindowLongPtrW(self._top_hwnd(), -8, 0)
        except Exception:
            pass
        self.bound_hwnd = 0
        self.bound_title = ""

    def _set_w(self, v):
        self.bar_w = max(5.0, min(30.0, float(v)))   # 实体宽上下限 5~30 px
        self._resize()

    def _set_h(self, v):
        self.base_h = v
        self._resize()

    def _reset_size(self):
        self.bar_w, self.base_h = 11.0, 92.0
        self._resize()

    def toggle_pause(self):
        self.paused = not self.paused

    # ---------------- 交互 ----------------
    def _bind(self):
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<Double-Button-1>", lambda e: self.toggle_pause())
        self.canvas.bind("<ButtonPress-3>", self._popup)
        self.canvas.bind("<MouseWheel>", self._wheel)

    def _drag_start(self, e):
        self._ox, self._oy = e.x, e.y

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._ox}+{e.y_root - self._oy}")

    def _wheel(self, e):
        d = 1 if e.delta > 0 else -1
        if e.state & 0x0001:
            self.bar_w = max(4.0, min(46.0, self.bar_w + d * 1.5))
        else:
            self.base_h = max(40.0, min(420.0, self.base_h + d * 10))
        self._resize()

    # ---------------- 尺寸 ----------------
    @property
    def _p_max(self):
        return max(20.0, self.win_h - 2 * EDGE - UNDER)

    @property
    def _p_min(self):
        return -(UNDER - EDGE)          # 允许跌破开盘价，留出插针空间

    def _set_geom(self, w, h, x, y):
        self.win_w, self.win_h = w, h
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.canvas.config(width=w, height=h)

    def _resize(self, center_screen=False):
        w = int(max(48, self.bar_w * 1.10 * 2.4 + 26))
        h = int(self.base_h * 2.0 + 2 * EDGE + UNDER)

        if center_screen or not self.win_w:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            x, y = int(sw * 0.72) - w // 2, int(sh * 0.70) - h
        else:
            cx = self.root.winfo_x() + self.win_w // 2
            x = cx - w // 2
            y = self.root.winfo_y()

        self._set_geom(w, h, x, y)
        self.bw, self.bh = w, h
        self.p = max(self._p_min, min(self.p, self._p_max))
        self.p_to = max(self._p_min, min(self.p_to, self._p_max))
        self.hi = max(self._p_min, min(self.hi, self._p_max))
        self.lo = max(self._p_min, min(self.lo, self._p_max))

    # ---------------- 周期 & 阶跃 ----------------
    def _new_cycle(self):
        """新 K 线开盘：价格归零、极值清零。"""
        self.p = self.p_from = self.p_to = 0.0
        self.hi = self.lo = 0.0
        self.stepping = False
        self.step_t = 0.0
        self.idle_left = 0.20
        self.pending_pull = False
        self.cycle_t = 0.0

    # ---------------- 穿刺 ----------------
    def _start_pierce(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = self.root.winfo_x()

        if self.trend_up:                       # 底边钉死，向上撑到屏幕顶
            yb = self.root.winfo_y() + self.bh
            hp = int(max(80, min(sh, yb)))
            yp = int(yb - hp)
            self.anchor_y = int(yb)
        else:                                   # 顶边钉死，向下撑到屏幕底
            yt = self.root.winfo_y()
            hp = int(max(80, min(sh, sh - yt)))
            yp = int(yt)
            self.anchor_y = int(yt)

        self._set_geom(self.bw, hp, x, yp)
        self.piercing = True
        self.stepping = False
        self.pierce_t = 0.0
        self.pierce_from = self.p
        self.pierce_max = self._p_max
        self.flash = 0.18

    def _end_pierce(self):
        if self.trend_up:
            y = self.anchor_y - self.bh
        else:
            y = self.anchor_y
        self._set_geom(self.bw, self.bh, self.root.winfo_x(), y)
        self.piercing = False
        self.pierce_cd = PIERCE_INTERVAL
        self.cycle_t = 0.0
        self.stepping = False
        self.idle_left = 0.30
        self.pending_pull = False
        self.p = self.p_from = self.p_to = self._p_max * random.uniform(0.35, 0.70)
        self.hi = self.p
        self.lo = 0.0

    # ---------------- 反向插针 ----------------
    def _start_invert(self):
        """涨着涨着突然翻红往下砸（或反过来），停一下再快速拉回。

        深度走指数分布：小幅（6~16）常见，中幅偶发，极端（36+）罕见。
        砸得越深停得越短 —— 大红是一闪而过，不是久留。
        """
        self.invert_from = self.p
        raw = random.expovariate(1.0 / 12.0)         # 均值 12 的长尾分布
        depth = 6.0 + min(raw, 44.0)                 # 6 ~ 50
        self.invert_to = max(self._p_min, -depth)
        self.invert_back = self.invert_from * random.uniform(0.85, 1.05)
        used = abs(self.invert_to)
        self.invert_hold = 0.50 - 0.005 * used       # 深 50 → 0.25s，浅 6 → 0.47s
        self.inverting = True
        self.stepping = False
        self.invert_t = 0.0
        self.flash = 0.18
        self.impulse = 1.0

    def _end_invert(self):
        self.inverting = False
        self.invert_cd = random.uniform(*INVERT_CD)
        self.p = self.p_from = self.p_to = self.invert_back
        self.stepping = False
        self.idle_left = 0.25

    def _new_step(self):
        pmax = self._p_max
        trend = 1                    # p 增大 = 沿趋势方向（绿=往上长，红=往下砸，坐标已镜像）
        mag = random.uniform(0.07, 0.22)
        idle = random.uniform(0.18, 0.70)

        if self.pending_pull:                       # 回调之后紧跟的猛拉
            trend, mag = 1, random.uniform(0.30, 0.55)
            self.pending_pull = False
            idle = random.uniform(0.22, 0.55)
        elif random.random() < 0.02:                # 极小概率：一步冲到顶/底
            self.p_from, self.p_to = self.p, pmax
            self.step_t, self.stepping = 0.0, True
            self.idle_left, self.flash, self.impulse = 0.12, 0.18, 1.0
            return
        elif random.random() < 0.18:                # 偶尔反向砸一下
            trend, mag = -1, random.uniform(0.10, 0.22)
            self.pending_pull = True
            idle = 0.10

        target = self.p + trend * pmax * mag

        if self.p >= pmax * 0.94:                   # 到顶：猛砸回中下部
            target = pmax * random.uniform(0.22, 0.45)
            self.pending_pull = True
        elif self.p <= pmax * 0.06:                 # 到底：猛拉起
            target = self.p + pmax * random.uniform(0.30, 0.62)

        self.p_from = self.p
        self.p_to = max(self._p_min, min(pmax, target))
        self.step_t, self.stepping = 0.0, True
        self.idle_left, self.flash, self.impulse = idle, 0.18, 1.0

    def _candle_colors(self, green):
        """green=True 表示当前蜡烛是『上涨』方向。
        默认 涨绿跌红；color_invert=True 时 涨红跌绿（照顾红涨绿跌的用户）。
        闪光色是实体色的亮色变体，必须同族。返回 (实体/影线色, 最新价闪光色)。"""
        if self.color_invert:
            return (RED, RED_HI) if green else (GREEN, GREEN_HI)
        return (GREEN, GREEN_HI) if green else (RED, RED_HI)

    # ---------------- 绘制 ----------------
    def _draw(self, w, pj, hi, lo):
        c = self.canvas
        bx = self.win_w / 2

        # 颜色跟蜡烛图规则走：收盘 ≥ 开盘 = 涨，否则跌（p 为负即跌破开盘价）
        green = (self.p >= 0) if self.trend_up else (self.p < 0)
        color, hi_color = self._candle_colors(green)

        if self.trend_up:
            # 开盘价钉死在下方（下方留 UNDER 给插针的下影线）
            oy = self.win_h - EDGE - UNDER
            yy = lambda v: oy - v
        else:
            # 开盘价钉死在上方（上方留 UNDER 给反向拉升的上影线）
            oy = EDGE + UNDER
            yy = lambda v: oy + v

        cy = yy(pj)                                  # 收盘价
        wick = sorted((yy(hi), yy(lo)))              # 最高价 → 最低价

        # 上下影线（穿心细线）
        ww = max(2.0, w * 0.18)
        c.create_rectangle(bx - ww / 2, wick[0], bx + ww / 2, wick[1],
                           fill=color, outline="")

        # 实体（开盘→收盘）
        by1, by2 = sorted((oy, cy))
        if by2 - by1 < 4:
            by2 = by1 + 4
        c.create_rectangle(bx - w / 2, by1, bx + w / 2, by2, fill=color, outline="")

        # 最新价闪光
        if self.flash > 0:
            fh = 6.0 * (self.flash / 0.18)
            c.create_rectangle(bx - w / 2, cy - fh / 2, bx + w / 2, cy + fh / 2,
                               fill=hi_color, outline="")

    # ---------------- 主循环 ----------------
    def _tick(self):
        now = time.perf_counter()
        raw = now - self._last
        self._last = now
        dt = min(0.05, raw) if raw > 0 else 0.0     # 用真实时间，不受掉帧影响
        self.canvas.delete("all")

        # 记录用户最后激活的窗口（不是我们自己）：右键时就知道"当前"是谁
        self._fg_t += dt
        if self._fg_t >= 0.25:
            self._fg_t = 0.0
            try:
                fg = int(ctypes.windll.user32.GetForegroundWindow())
                if fg:
                    info = self._info_of(fg)
                    if info and info[0] != "KLineWish":
                        self._last_fg = fg
            except Exception:
                pass

        # 跟随的目标窗口没了 → 一起退出
        if self.bound_hwnd:
            self._chk += dt
            if self._chk > 0.6:
                self._chk = 0.0
                try:
                    alive = ctypes.windll.user32.IsWindow(self.bound_hwnd)
                except Exception:
                    alive = True
                if not alive:
                    self.root.destroy()
                    return

        if not self.paused:
            self.t += dt
            self.cycle_t += dt
            if self.cycle_t >= CYCLE:
                self._new_cycle()

            if self.inverting:
                # 反向插针：砸/拉出去 → 短暂停留 → 快速拉回
                self.invert_t += dt
                it = self.invert_t
                hold = INVERT_DOWN + self.invert_hold
                back = hold + INVERT_UP
                if it < INVERT_DOWN:
                    k = it / INVERT_DOWN
                    e = 1 - (1 - k) ** 3
                    self.p = self.invert_from + (self.invert_to - self.invert_from) * e
                elif it < hold:
                    self.p = self.invert_to
                elif it < back:
                    k = (it - hold) / (back - hold)
                    e = 1 - (1 - k) ** 3
                    self.p = self.invert_to + (self.invert_back - self.invert_to) * e
                else:
                    self._end_invert()
            elif self.piercing:
                # 冲到边缘 → 停在边缘 → 缩回 → 收
                self.pierce_t += dt
                pt = self.pierce_t
                if pt < PIERCE_RUSH:
                    k = pt / PIERCE_RUSH
                    e = 1 - (1 - k) ** 3
                    self.p = self.pierce_from + (self.pierce_max - self.pierce_from) * e
                elif pt < PIERCE_HOLD:
                    self.p = self.pierce_max
                elif pt < PIERCE_BACK:
                    k = (pt - PIERCE_HOLD) / (PIERCE_BACK - PIERCE_HOLD)
                    self.p = self.pierce_max * (1 - 0.65 * k)
                else:
                    self._end_pierce()
            else:
                # 两个倒计时各自独立走，互不阻塞
                if self.pierce_on:
                    self.pierce_cd -= dt
                    if self.pierce_cd <= 0:
                        self._start_pierce()
                if not self.piercing:
                    self.invert_cd -= dt
                    if self.invert_cd <= 0:
                        self._start_invert()

            if not self.piercing and not self.inverting:
                if self.stepping:
                    self.step_t += dt
                    x = min(1.0, self.step_t / STEP_DUR)
                    e = 1 - (1 - x) ** 3             # ease-out：窜得快、收得稳
                    self.p = self.p_from + (self.p_to - self.p_from) * e
                    if x >= 1.0:
                        self.p = self.p_to
                        self.stepping = False
                else:
                    self.idle_left -= dt
                    if self.idle_left <= 0:
                        self._new_step()

            self.hi = max(self.hi, self.p)
            self.lo = min(self.lo, self.p)
            self.flash = max(0.0, self.flash - dt)
            self.impulse = max(0.0, self.impulse - dt / 0.16)

        t = self.t
        jitter = 2.2 * math.sin(t * 2 * math.pi * 13.0) + random.uniform(-0.6, 0.6)
        pj = self.p + jitter
        hi = max(self.hi, self.p)
        lo = min(self.lo, self.p)
        w = self.bar_w * (1 + 0.16 * self.impulse)
        self._draw(w, pj, hi, lo)

        self.root.after(16, self._tick)


if __name__ == "__main__":
    ArrowPet().root.mainloop()
