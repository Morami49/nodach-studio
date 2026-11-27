"""
Nodach Gate confirm popup: a small topmost window on the PC. Started by gate_mcp.py as its own process for each
confirm-zone action, so it never shares a thread with the MCP server.

    python gate_popup.py <request.json>      prints one word: once | app10 | block | timeout

request.json: {"action": "...", "app": "...", "title": "...", "element": "...", "reason": "...", "shot": path|null,
               "timeout_s": 60, "grant_minutes": 10}
The window title starts with "Nodach Gate", which is a never-zone title: Claude can't click its own Allow.
"""
from __future__ import annotations

import json
import sys


def main(argv) -> int:
    req = json.loads(open(argv[1], encoding="utf-8").read())
    import tkinter as tk
    answer = {"v": "timeout"}
    root = tk.Tk()
    root.title("Nodach Gate — confirm")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    pad = {"padx": 12, "pady": 4}
    tk.Label(root, text="Claude wants to:", font=("Segoe UI", 10)).pack(anchor="w", **pad)
    tk.Label(root, text=req.get("action", "?"), font=("Segoe UI", 12, "bold"), wraplength=460, justify="left").pack(anchor="w", **pad)
    info = f"in {req.get('app') or '?'} · {req.get('title') or ''}\nelement: {req.get('element') or '(no name)'}\nwhy asked: {req.get('reason', '')}"
    tk.Label(root, text=info, font=("Segoe UI", 9), fg="#444", wraplength=460, justify="left").pack(anchor="w", **pad)
    if req.get("shot"):
        try:
            img = tk.PhotoImage(file=req["shot"])
            f = max(1, img.width() // 440 + 1, img.height() // 220 + 1)
            img = img.subsample(f, f) if f > 1 else img
            tk.Label(root, image=img, relief="solid", bd=1).pack(**pad)
            root._img = img
        except Exception:
            pass
    left = {"s": int(req.get("timeout_s", 60))}
    timer = tk.Label(root, font=("Segoe UI", 9), fg="#a00")
    timer.pack(anchor="w", **pad)
    row = tk.Frame(root)
    row.pack(fill="x", padx=12, pady=10)

    def done(v):
        answer["v"] = v
        root.destroy()
    # 21 Nov: the popup took keyboard focus while Nowed was typing in the chat, and a stray Space pressed the
    # focused 'Allow once' (he never saw the popup). Now: it never takes focus, no button can be keyboard-
    # activated, and the buttons only work 1 s after the popup appears. Only a mouse click answers it.
    armed = {"v": False}

    def click(v):
        if armed["v"]:
            done(v)
    btns = [tk.Button(row, text="Allow once", width=12, takefocus=0, command=lambda: click("once")),
            tk.Button(row, text=f"Allow for {req.get('grant_minutes', 10)} min in this app", takefocus=0, command=lambda: click("app10")),
            tk.Button(row, text="Block", width=10, bg="#c33", fg="white", takefocus=0, command=lambda: click("block"))]
    btns[0].pack(side="left")
    btns[1].pack(side="left", padx=6)
    btns[2].pack(side="right")
    for b in btns:
        b.config(state="disabled")

    def arm():
        armed["v"] = True
        for b in btns:
            b.config(state="normal")
    root.after(1000, arm)
    root.protocol("WM_DELETE_WINDOW", lambda: done("block"))
    for key in ("<space>", "<Return>", "<KP_Enter>"):
        root.bind_all(key, lambda e: "break")

    def tick():
        if left["s"] <= 0:
            done("timeout")
            return
        timer.config(text=f"Blocks automatically in {left['s']} s")
        left["s"] -= 1
        root.after(1000, tick)
    tick()
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{max(40, (root.winfo_screenheight() - h) // 3)}")
    root.configure(highlightthickness=4, highlightbackground="#d33", highlightcolor="#d33")
    root.lift()   # on top, but never takes keyboard focus
    try:
        root.bell()
    except Exception:
        pass
    root.mainloop()
    print(answer["v"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
