# -*- coding: utf-8 -*-
"""AI 接口配置窗口（零依赖，tkinter 内置）。

打开方式:
  1. 桌面主窗口点击「配置出题模型」
  2. 手动: python scripts/config_window.py

填写 base_url / api_key / model / timeout，「测试连接」验证 key 后「保存」，
写入项目根 config.json 并即时生效（出题器每次读最新配置，无需重启）。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bagu_trainer.tk_runtime import prepare_tk
prepare_tk()

import tkinter as tk
from tkinter import messagebox, ttk

from bagu_trainer import secrets                          # noqa: E402
from bagu_trainer.config import CONFIG_PATH, load_config, update_llm      # noqa: E402
from bagu_trainer.quiz_generator import (                                   # noqa: E402
    test_llm,
    verified_fallback_count,
)

PRESET_MODELS = ["deepseek-chat", "gpt-4o-mini", "deepseek-reasoner", "qwen-plus", "moonshot-v1-8k"]
PRESET_URLS = {
    "DeepSeek": "https://api.deepseek.com/v1",
    "OpenAI": "https://api.openai.com/v1",
    "Moonshot": "https://api.moonshot.cn/v1",
    "通义千问": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}


def build_window():
    root = tk.Tk()
    root.title("AI 八股训练器 · AI 接口配置")
    root.geometry("560x430")
    root.resizable(False, False)

    cfg = load_config()
    llm = cfg.get("llm") or {}
    fallback_total = verified_fallback_count()
    # Key 从凭据管理器/环境变量解析（config.json 不再保存明文）；仅用于本地回显，掩码显示
    current_key = secrets.resolve_api_key(llm)

    pad = {"padx": 12, "pady": 6}
    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text=f"用大模型把「AI 当前干的活」+题库来源正文合成情景题。"
                        f"当前已验证回退题 {fallback_total} 道：不填 Key 时离线可出这 "
                        f"{fallback_total} 道带四选项、答案和解析的题，其余知识点会明确"
                        "提示不可用（推荐填写 API Key）。",
              foreground="#666").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

    # 快捷服务商
    ttk.Label(frm, text="快捷选择").grid(row=1, column=0, sticky="w", **pad)
    provider_var = tk.StringVar()
    combo = ttk.Combobox(frm, textvariable=provider_var, state="readonly",
                         values=list(PRESET_URLS.keys()), width=14)
    combo.grid(row=1, column=1, columnspan=2, sticky="we", **pad)

    def on_provider(_e=None):
        if provider_var.get() in PRESET_URLS:
            url_var.set(PRESET_URLS[provider_var.get()])
    combo.bind("<<ComboboxSelected>>", on_provider)

    # 接口地址
    ttk.Label(frm, text="接口地址 base_url").grid(row=2, column=0, sticky="w", **pad)
    url_var = tk.StringVar(value=llm.get("base_url", "https://api.deepseek.com/v1"))
    ttk.Entry(frm, textvariable=url_var, width=46).grid(row=2, column=1, columnspan=2, sticky="we", **pad)

    # API Key
    ttk.Label(frm, text="API Key").grid(row=3, column=0, sticky="w", **pad)
    key_var = tk.StringVar(value=current_key)
    key_entry = ttk.Entry(frm, textvariable=key_var, width=35, show="*")
    key_entry.grid(row=3, column=1, sticky="we", **pad)
    show_var = tk.BooleanVar(value=False)

    def toggle_show():
        key_entry.configure(show="" if show_var.get() else "*")
    ttk.Checkbutton(frm, text="显示", variable=show_var, command=toggle_show).grid(row=3, column=2, sticky="w")

    # 模型名
    ttk.Label(frm, text="模型名 model").grid(row=4, column=0, sticky="w", **pad)
    model_var = tk.StringVar(value=llm.get("model", "deepseek-chat"))
    model_combo = ttk.Combobox(frm, textvariable=model_var, values=PRESET_MODELS, width=30)
    model_combo.grid(row=4, column=1, columnspan=2, sticky="we", **pad)

    # 超时
    ttk.Label(frm, text="超时(秒)").grid(row=5, column=0, sticky="w", **pad)
    timeout_var = tk.StringVar(value=str(llm.get("timeout_sec", 60)))
    ttk.Entry(frm, textvariable=timeout_var, width=10).grid(row=5, column=1, sticky="w", **pad)

    status = ttk.Label(frm, text="", foreground="#006600", wraplength=500)
    status.grid(row=6, column=0, columnspan=3, sticky="we", **pad)

    def get_llm() -> dict:
        return {
            "base_url": url_var.get().strip(),
            "api_key": key_var.get().strip(),
            "model": model_var.get().strip() or "deepseek-chat",
            "timeout_sec": int(timeout_var.get() or 60),
        }

    def on_save():
        try:
            llm_cfg = get_llm()
        except ValueError:
            messagebox.showerror("错误", "超时必须是数字")
            return
        stored = update_llm(llm_cfg)
        if stored == "fallback":
            messagebox.showwarning(
                "未保存 Key",
                "系统凭据管理器不可用，Key 未保存（不会明文写入 config.json）。\n"
                "请改设环境变量 BAGU_LLM_API_KEY。")
        status.configure(text="已保存，即时生效：Key 存于系统凭据管理器（config.json 不含明文）。"
                              f"下次出题会优先校验来源正文并生成情景题；离线仍有 "
                              f"{verified_fallback_count()} 道已验证题。", foreground="#006600")

    def on_test():
        try:
            llm_cfg = get_llm()
        except ValueError:
            return messagebox.showerror("错误", "超时必须是数字")
        status.configure(text="正在测试连接…", foreground="#333")
        root.update()
        result = test_llm(llm_cfg["base_url"], llm_cfg["api_key"],
                          llm_cfg["model"], min(llm_cfg["timeout_sec"], 15))
        ok = result.startswith(("连接成功", "HTTP 200"))
        status.configure(text=f"测试结果: {result}", foreground="#006600" if ok else "#aa0000")

    btns = ttk.Frame(frm)
    btns.grid(row=7, column=0, columnspan=3, sticky="e", pady=(12, 0))
    ttk.Button(btns, text="测试连接", command=on_test).pack(side="left", padx=6)
    ttk.Button(btns, text="保存", command=on_save).pack(side="left")

    if current_key:
        status.configure(text=f"已检测到现有配置（Key 已从凭据管理器/环境变量载入，掩码显示），"
                              f"当前离线可出 {verified_fallback_count()} 道已验证题。",
                         foreground="#333")
    return root


if __name__ == "__main__":
    build_window().mainloop()
