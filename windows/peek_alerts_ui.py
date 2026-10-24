"""
peek_alerts_ui.py - the "Alerts, sensors and messages" window.

Opened from the main settings window, as a child of it, so it runs on the same
Tk thread. Four tabs, drawn by hand in the same dark style rather than with
ttk, because ttk's dark theming on Windows is more work than the whole window.

Nothing is written until Save: the window edits a private copy of the config,
and Close throws the copy away.
"""

import copy
import os
import threading
import time
import tkinter as tk

import peek_alerts as pa
import peek_config as cfgmod
import peek_messages as pm
import peek_sensors as ps

BASE_METRICS = ["cpu_percent", "ram_percent", "cpu_temp_c", "storage_percent",
                "battery_percent", "net_kbps", "net_rx_kbps", "net_tx_kbps"]
SOURCE_FIELD = {"command": "command", "file": "path", "http": "url", "counter": "counter"}
SOURCE_HINT = {
    "command": "Command line, e.g.  ping -n 1 1.1.1.1",
    "file": "Path of a text or JSON file",
    "http": "URL, e.g.  http://127.0.0.1:8080/status",
    "counter": "Perfmon counter, e.g.  \\PhysicalDisk(_Total)\\Current Disk Queue Length",
}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def open_alerts(parent, runner, pal):
    BG, CARD, EDGE = pal["BG"], pal["CARD"], pal["EDGE"]
    TEXT, DIM, CYAN = pal["TEXT"], pal["DIM"], pal["CYAN"]
    RED, AMBER, GREEN = pal["RED"], pal["AMBER"], pal["GREEN"]
    FONT = pal.get("FONT", "Segoe UI")

    win = tk.Toplevel(parent)
    win.title("PeekESP - alerts, sensors and messages")
    win.configure(bg=BG)
    win.resizable(False, False)
    win.transient(parent)

    work = copy.deepcopy(runner.cfg)
    A = work["alerts"]
    svc = runner.services

    # ----------------------------------------------------------------- bits
    def lab(p, text, fg=DIM, size=9, bold=False, **kw):
        return tk.Label(p, text=text, bg=kw.pop("bg", BG), fg=fg, anchor="w",
                        font=(FONT, size, "bold" if bold else "normal"), **kw)

    def btn(p, text, cmd, accent=False):
        return tk.Button(p, text=text, command=cmd, relief="flat", bd=0, cursor="hand2",
                         bg=CYAN if accent else CARD, fg=BG if accent else TEXT,
                         activebackground=TEXT if accent else EDGE,
                         activeforeground=BG if accent else CYAN,
                         font=(FONT, 9, "bold" if accent else "normal"), padx=12, pady=5)

    def ent(p, var, width=10, show=None):
        return tk.Entry(p, textvariable=var, width=width, show=show or "", bg=CARD, fg=TEXT,
                        insertbackground=TEXT, relief="flat", bd=0, highlightthickness=1,
                        highlightbackground=EDGE, highlightcolor=CYAN, font=(FONT, 9))

    def chk(p, text, var, **kw):
        return tk.Checkbutton(p, text=text, variable=var, bg=BG, fg=TEXT, selectcolor=CARD,
                              activebackground=BG, activeforeground=TEXT, anchor="w",
                              highlightthickness=0, bd=0, font=(FONT, 9), **kw)

    def radio(p, text, var, value):
        return tk.Radiobutton(p, text=text, variable=var, value=value, bg=BG, fg=TEXT,
                              selectcolor=CARD, activebackground=BG, activeforeground=TEXT,
                              highlightthickness=0, bd=0, font=(FONT, 9))

    def menu(p, var, options, width=22):
        om = tk.OptionMenu(p, var, *options)
        om.config(bg=CARD, fg=TEXT, activebackground=EDGE, activeforeground=CYAN,
                  highlightthickness=0, relief="flat", bd=0, width=width, anchor="w",
                  font=(FONT, 9))
        om["menu"].config(bg=CARD, fg=TEXT, activebackground=CYAN, activeforeground=BG,
                          font=(FONT, 9))
        return om

    def listbox(p, height, width=58):
        lb = tk.Listbox(p, height=height, width=width, bg=CARD, fg=TEXT, relief="flat", bd=0,
                        highlightthickness=1, highlightbackground=EDGE, highlightcolor=CYAN,
                        selectbackground=EDGE, selectforeground=CYAN, activestyle="none",
                        exportselection=False, font=("Consolas", 9))
        return lb

    status = tk.StringVar(value="")

    def say(text, color=DIM):
        status.set(text)
        status_lbl.config(fg=color)

    def modal(title):
        d = tk.Toplevel(win)
        d.title(title)
        d.configure(bg=BG)
        d.resizable(False, False)
        d.transient(win)
        d.grab_set()
        return d

    def pad(f, row, text, widget, hint=None):
        lab(f, text).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
        widget.grid(row=row, column=1, sticky="w", pady=3)
        if hint:
            lab(f, hint, size=8).grid(row=row, column=2, sticky="w", padx=8)

    # --------------------------------------------------------------- layout
    footer = tk.Frame(win, bg=BG)
    footer.pack(side="bottom", fill="x", padx=18, pady=(6, 14))
    status_lbl = tk.Label(footer, textvariable=status, bg=BG, fg=DIM, anchor="w",
                          justify="left", wraplength=330, font=(FONT, 8))
    status_lbl.pack(side="left", fill="x", expand=True)
    btn(footer, "Close", win.destroy).pack(side="right", padx=(8, 0))
    save_btn = btn(footer, "Save", lambda: save(), accent=True)
    save_btn.pack(side="right")

    bar = tk.Frame(win, bg=BG)
    bar.pack(side="top", fill="x", padx=18, pady=(14, 8))
    body = tk.Frame(win, bg=BG, width=560, height=412)
    body.pack(side="top", padx=18)
    body.pack_propagate(False)

    tabs = {}
    tab_btns = {}

    def show(name):
        for n, f in tabs.items():
            f.pack_forget()
            tab_btns[n].config(fg=DIM)
        tabs[name].pack(fill="both", expand=True)
        tab_btns[name].config(fg=CYAN)

    for name in ("Alerts", "Rules", "Sensors", "Messages"):
        tabs[name] = tk.Frame(body, bg=BG)
        b = tk.Button(bar, text=name, command=lambda n=name: show(n), relief="flat", bd=0,
                      bg=BG, fg=DIM, activebackground=BG, activeforeground=CYAN,
                      cursor="hand2", font=(FONT, 10, "bold"), padx=10)
        b.pack(side="left")
        tab_btns[name] = b

    # ===================================================================
    #  Alerts
    # ===================================================================
    t = tabs["Alerts"]
    v_enabled = tk.BooleanVar(value=A["enabled"])
    v_toast = tk.BooleanVar(value=A["local_toast"])
    v_ring = tk.BooleanVar(value=A["local_ring"])
    q = A["quiet"]
    v_qon = tk.BooleanVar(value=q["enabled"])
    v_qs, v_qe = tk.StringVar(value=q["start"]), tk.StringVar(value=q["end"])
    v_days = [tk.BooleanVar(value=i in q["days"]) for i in range(7)]
    v_qcrit = tk.BooleanVar(value=q["allow_critical"])
    v_ttl = tk.StringVar(value=f"{work['message_ttl_hours']:g}")

    chk(t, "Watch this PC with the rules in the next tab", v_enabled).pack(anchor="w")
    chk(t, "Show a Windows notification for every message", v_toast).pack(anchor="w")
    chk(t, "Ring on this PC for a critical message (until silenced, 10 min at most)",
        v_ring).pack(anchor="w")
    lab(t, "Messages always go to your phone as well, whatever is ticked here.",
        size=8).pack(anchor="w", pady=(2, 10))

    lab(t, "QUIET HOURS", fg=CYAN, size=8, bold=True).pack(anchor="w")
    chk(t, "Silence this PC's notifications and alarm during these hours", v_qon).pack(anchor="w")
    r = tk.Frame(t, bg=BG)
    r.pack(anchor="w", pady=4)
    lab(r, "From").pack(side="left")
    ent(r, v_qs, 6).pack(side="left", padx=(6, 12))
    lab(r, "to").pack(side="left")
    ent(r, v_qe, 6).pack(side="left", padx=(6, 12))
    lab(r, "24-hour, e.g. 22:00 to 07:00", size=8).pack(side="left")
    r = tk.Frame(t, bg=BG)
    r.pack(anchor="w", pady=2)
    for i, d in enumerate(DAYS):
        chk(r, d, v_days[i]).pack(side="left", padx=(0, 6))
    chk(t, "Let critical messages through anyway", v_qcrit).pack(anchor="w")
    lab(t, "A night that starts on a ticked day runs into the next morning. Your phone has\n"
           "its own quiet hours; these only cover this PC.", size=8, justify="left"
        ).pack(anchor="w", pady=(2, 10))

    r = tk.Frame(t, bg=BG)
    r.pack(anchor="w")
    lab(r, "Keep each message in the reading for").pack(side="left")
    ent(r, v_ttl, 4).pack(side="left", padx=6)
    lab(r, "hours (1\u201372). A phone that was off can still catch up.", size=8).pack(side="left")

    # ===================================================================
    #  Rules
    # ===================================================================
    t = tabs["Rules"]
    lab(t, "Checked every few seconds on this PC, even when the phone is asleep. If your phone "
           "has a rule for the same thing too, you will be told twice.", size=8, wraplength=550,
        justify="left").pack(anchor="w", pady=(0, 6))
    rules_lb = listbox(t, 12, 70)
    rules_lb.pack(anchor="w")

    def refresh_rules(select=None):
        rules_lb.delete(0, "end")
        for r in A["rules"]:
            rules_lb.insert("end", pa.describe_rule(r))
        if select is not None and A["rules"]:
            rules_lb.selection_set(min(select, len(A["rules"]) - 1))

    def sel_rule():
        s = rules_lb.curselection()
        return s[0] if s else None

    def metric_choices():
        keys = list(BASE_METRICS)
        plain, metrics = svc.extras_payload()
        keys += [k for k in list(plain) + list(metrics) if k not in keys]
        keys += [s["key"] for s in work["sensors"] if s["key"] not in keys]
        keys += [r["metric"] for r in A["rules"] if r["metric"] not in keys]
        return keys

    def metric_name(k):
        return f"{ps.label_for_key(k)}  [{k}]"

    def edit_rule(index):
        rule = copy.deepcopy(A["rules"][index]) if index is not None else {
            "id": "", "enabled": True, "metric": "cpu_percent", "op": "above", "limit": 90.0,
            "min": 0.0, "for": 300, "severity": "warning", "unplugged": False,
            "recovery": True, "title": "", "body": ""}
        d = modal("Edit rule" if index is not None else "New rule")
        f = tk.Frame(d, bg=BG)
        f.pack(padx=18, pady=14)
        choices = metric_choices()
        names = {metric_name(k): k for k in choices}
        v_metric = tk.StringVar(value=metric_name(rule["metric"]))
        ops = {"above": "above", "below": "below", "unusual": "unusually high (vs its last 24 h)"}
        v_op = tk.StringVar(value=ops[rule["op"]])
        v_limit = tk.StringVar(value=f"{rule['limit']:g}")
        v_min = tk.StringVar(value=f"{rule['min']:g}")
        v_for = tk.StringVar(value=str(rule["for"]))
        v_sev = tk.StringVar(value=rule["severity"])
        v_unp = tk.BooleanVar(value=rule["unplugged"])
        v_rec = tk.BooleanVar(value=rule["recovery"])
        v_title, v_body = tk.StringVar(value=rule["title"]), tk.StringVar(value=rule["body"])
        err = tk.Label(d, text="", bg=BG, fg=RED, font=(FONT, 8), wraplength=420, justify="left")

        pad(f, 0, "Watch", menu(f, v_metric, list(names), 30))
        pad(f, 1, "When it is", menu(f, v_op, list(ops.values()), 30))
        pad(f, 2, "Limit", ent(f, v_limit, 10), "ignored for 'unusually high'")
        pad(f, 3, "Ignore below", ent(f, v_min, 10), "'unusually high' only")
        pad(f, 4, "For at least", ent(f, v_for, 10), "seconds; 0 = at once")
        sv = tk.Frame(f, bg=BG)
        for s in pm.SEVERITIES:
            radio(sv, s, v_sev, s).pack(side="left", padx=(0, 10))
        pad(f, 5, "Severity", sv)
        pad(f, 6, "", chk(f, "Only while unplugged (batteries)", v_unp))
        pad(f, 7, "", chk(f, "Send an all-clear when it recovers", v_rec))
        pad(f, 8, "Title", ent(f, v_title, 44), None)
        pad(f, 9, "Message", ent(f, v_body, 44), None)
        lab(f, "Optional wording. Fill-ins: {machine} {label} {value} {limit} {duration} {typical}",
            size=8).grid(row=10, column=1, columnspan=2, sticky="w")
        err.pack(anchor="w", padx=18)

        def ok():
            op = next(k for k, v in ops.items() if v == v_op.get())
            raw = {"id": rule["id"], "enabled": rule["enabled"],
                   "metric": names.get(v_metric.get(), rule["metric"]), "op": op,
                   "limit": v_limit.get().strip(), "min": v_min.get().strip() or 0,
                   "for": v_for.get().strip() or 0, "severity": v_sev.get(),
                   "unplugged": v_unp.get(), "recovery": v_rec.get(),
                   "title": v_title.get(), "body": v_body.get()}
            taken = {r["id"] for i, r in enumerate(A["rules"]) if i != index}
            clean, why = pa.clean_rule(raw, len(A["rules"]), taken)
            if clean is None:
                err.config(text=why)
                return
            if index is None:
                A["rules"].append(clean)
            else:
                A["rules"][index] = clean
            refresh_rules(index if index is not None else len(A["rules"]) - 1)
            d.destroy()

        row = tk.Frame(d, bg=BG)
        row.pack(fill="x", padx=18, pady=(4, 14))
        btn(row, "Cancel", d.destroy).pack(side="right", padx=(8, 0))
        btn(row, "OK", ok, accent=True).pack(side="right")
        win.wait_window(d)

    def toggle_rule():
        i = sel_rule()
        if i is not None:
            A["rules"][i]["enabled"] = not A["rules"][i]["enabled"]
            refresh_rules(i)

    def delete_rule():
        i = sel_rule()
        if i is not None:
            del A["rules"][i]
            refresh_rules(i)

    def reset_rules():
        A["rules"] = pa.default_alerts()["rules"]
        refresh_rules(0)
        say("Default rules restored. Save to keep them.", AMBER)

    row = tk.Frame(t, bg=BG)
    row.pack(anchor="w", pady=8)
    for text, cmd in (("Add", lambda: edit_rule(None)),
                      ("Edit", lambda: sel_rule() is not None and edit_rule(sel_rule())),
                      ("On / off", toggle_rule), ("Delete", delete_rule),
                      ("Defaults", reset_rules)):
        btn(row, text, cmd).pack(side="left", padx=(0, 6))
    rules_lb.bind("<Double-Button-1>", lambda e: sel_rule() is not None and edit_rule(sel_rule()))
    refresh_rules(0)

    # ===================================================================
    #  Sensors
    # ===================================================================
    t = tabs["Sensors"]
    v_son = tk.BooleanVar(value=work["sensors_enabled"])
    v_sint = tk.StringVar(value=f"{work['extras_interval']:g}")
    r = tk.Frame(t, bg=BG)
    r.pack(anchor="w")
    chk(r, "Report GPU, NPU, fans and power when this PC has them", v_son).pack(side="left")
    r = tk.Frame(t, bg=BG)
    r.pack(anchor="w")
    lab(r, "Read every").pack(side="left")
    ent(r, v_sint, 4).pack(side="left", padx=6)
    lab(r, "seconds. NVIDIA via nvidia-smi; others need LibreHardwareMonitor's web server.",
        size=8).pack(side="left")

    lab(t, "FOUND RIGHT NOW", fg=CYAN, size=8, bold=True).pack(anchor="w", pady=(10, 2))
    found_lb = listbox(t, 6, 70)
    found_lb.pack(anchor="w")

    def refresh_found():
        if not win.winfo_exists():
            return
        found_lb.delete(0, "end")
        lines = svc.extras.describe() or ["(nothing yet - the first read takes a few seconds)"]
        for line in lines:
            found_lb.insert("end", line)
        win.after(2500, refresh_found)

    lab(t, "YOUR OWN", fg=CYAN, size=8, bold=True).pack(anchor="w", pady=(10, 2))
    sens_lb = listbox(t, 4, 70)
    sens_lb.pack(anchor="w")

    def refresh_sens(select=None):
        sens_lb.delete(0, "end")
        for s in work["sensors"]:
            what = s.get(SOURCE_FIELD[s["source"]])
            what = " ".join(what) if isinstance(what, list) else what
            sens_lb.insert("end", f"{s['key']:<16} {s['source']:<8} {what}")
        if select is not None and work["sensors"]:
            sens_lb.selection_set(min(select, len(work["sensors"]) - 1))

    def sel_sens():
        s = sens_lb.curselection()
        return s[0] if s else None

    def edit_sensor(index):
        s = copy.deepcopy(work["sensors"][index]) if index is not None else {
            "key": "", "source": "command", "label": "", "unit": "", "max": None,
            "interval": 10.0, "scale": 1.0}
        d = modal("Edit sensor" if index is not None else "New sensor")
        f = tk.Frame(d, bg=BG)
        f.pack(padx=18, pady=14)
        cur = s.get(SOURCE_FIELD[s["source"]], "")
        if isinstance(cur, list):
            cur = " ".join(f'"{a}"' if " " in a else a for a in cur)
        v_key, v_label = tk.StringVar(value=s["key"]), tk.StringVar(value=s["label"])
        v_unit = tk.StringVar(value=s["unit"])
        v_max = tk.StringVar(value="" if s["max"] is None else f"{s['max']:g}")
        v_src, v_target = tk.StringVar(value=s["source"]), tk.StringVar(value=cur)
        v_pat = tk.StringVar(value=s.get("json_path") or s.get("regex") or "")
        v_int, v_scale = tk.StringVar(value=f"{s['interval']:g}"), tk.StringVar(value=f"{s['scale']:g}")
        err = tk.Label(d, text="", bg=BG, fg=RED, font=(FONT, 8), wraplength=440, justify="left")
        hint = tk.Label(f, text=SOURCE_HINT[s["source"]], bg=BG, fg=DIM, font=(FONT, 8),
                        anchor="w", wraplength=300, justify="left")

        pad(f, 0, "Key", ent(f, v_key, 24), "a-z 0-9 _  ending _w _c _percent ...")
        pad(f, 1, "Name shown", ent(f, v_label, 24))
        pad(f, 2, "Unit", ent(f, v_unit, 8))
        pad(f, 3, "Full scale", ent(f, v_max, 8), "optional")
        src_menu = menu(f, v_src, list(ps.SOURCES), 10)
        pad(f, 4, "Read it from", src_menu)
        pad(f, 5, "", ent(f, v_target, 44))
        hint.grid(row=6, column=1, columnspan=2, sticky="w")
        pad(f, 7, "Pattern", ent(f, v_pat, 30), "regex (1st group) or JSON path a.b.0")
        pad(f, 8, "Every", ent(f, v_int, 8), "seconds")
        pad(f, 9, "Multiply by", ent(f, v_scale, 8), "e.g. 0.001 for mW to W")

        def on_src(*_):
            hint.config(text=SOURCE_HINT.get(v_src.get(), ""))
        v_src.trace_add("write", on_src)

        def spec():
            src = v_src.get()
            pat = v_pat.get().strip()
            raw = {"key": v_key.get(), "source": src, "label": v_label.get(),
                   "unit": v_unit.get(), "max": v_max.get().strip() or None,
                   "interval": v_int.get().strip() or 10, "scale": v_scale.get().strip() or 1,
                   SOURCE_FIELD[src]: v_target.get().strip()}
            if pat:
                raw["json_path" if ("." in pat and "(" not in pat and "\\" not in pat
                                    and " " not in pat) else "regex"] = pat
            others = [x for i, x in enumerate(work["sensors"]) if i != index]
            clean, warns = ps.clean_custom(others + [raw])
            if not warns and clean and clean[-1]["key"] == raw["key"].strip().lower():
                return clean[-1], None
            return None, (warns or ["could not read this sensor"])[-1]

        def test():
            sp, why = spec()
            if sp is None:
                err.config(text=why, fg=RED)
                return
            err.config(text="reading...", fg=DIM)

            # The worker only fills this in. Tk is touched from its own thread
            # alone: calling after() from another one is unsafe, and fails
            # outright when the main loop is not the caller.
            result = []
            threading.Thread(target=lambda: result.append(ps.read_custom(sp)),
                             daemon=True).start()

            def poll():
                if not d.winfo_exists():
                    return
                if not result:
                    d.after(100, poll)
                    return
                v, e = result[0]
                if e is None:
                    err.config(text=f"reads {v:g} {sp['unit']}".strip(), fg=GREEN)
                else:
                    err.config(text=e, fg=RED)
            d.after(100, poll)

        def ok():
            sp, why = spec()
            if sp is None:
                err.config(text=why, fg=RED)
                return
            if index is None:
                work["sensors"].append(sp)
            else:
                work["sensors"][index] = sp
            refresh_sens(index if index is not None else len(work["sensors"]) - 1)
            d.destroy()

        err.pack(anchor="w", padx=18)
        row = tk.Frame(d, bg=BG)
        row.pack(fill="x", padx=18, pady=(4, 14))
        btn(row, "Test", test).pack(side="left")
        btn(row, "Cancel", d.destroy).pack(side="right", padx=(8, 0))
        btn(row, "OK", ok, accent=True).pack(side="right")
        win.wait_window(d)

    def delete_sensor():
        i = sel_sens()
        if i is not None:
            del work["sensors"][i]
            refresh_sens(i)

    row = tk.Frame(t, bg=BG)
    row.pack(anchor="w", pady=6)
    btn(row, "Add", lambda: edit_sensor(None)).pack(side="left", padx=(0, 6))
    btn(row, "Edit", lambda: sel_sens() is not None and edit_sensor(sel_sens())
        ).pack(side="left", padx=(0, 6))
    btn(row, "Delete", delete_sensor).pack(side="left")
    refresh_sens(0)

    # ===================================================================
    #  Messages
    # ===================================================================
    t = tabs["Messages"]
    lab(t, "SEND ONE TO YOUR PHONE", fg=CYAN, size=8, bold=True).pack(anchor="w")
    v_mt, v_mb, v_ms = tk.StringVar(), tk.StringVar(), tk.StringVar(value="info")
    f = tk.Frame(t, bg=BG)
    f.pack(anchor="w", pady=4)
    pad(f, 0, "Title", ent(f, v_mt, 50))
    pad(f, 1, "Message", ent(f, v_mb, 50))
    sv = tk.Frame(f, bg=BG)
    for s in pm.SEVERITIES:
        radio(sv, s, v_ms, s).pack(side="left", padx=(0, 10))
    pad(f, 2, "Severity", sv)

    def send():
        if not (v_mt.get().strip() or v_mb.get().strip()):
            say("Give it a title or a message.", AMBER)
            return
        s, _m = svc.outbox.add({"title": v_mt.get(), "body": v_mb.get(),
                                "severity": v_ms.get(), "source": "PeekESP",
                                "id": f"manual-{int(time.time() * 1000)}"},
                               internal=True)
        say("Queued. It reaches your phone in the next reading, within a few seconds."
            if s == "new" else "Not sent.", GREEN if s == "new" else AMBER)

    btn(t, "Send", send, accent=True).pack(anchor="w", padx=(0, 0), pady=(2, 10))

    lab(t, "OTHER PROGRAMS ON THIS PC", fg=CYAN, size=8, bold=True).pack(anchor="w")
    v_non = tk.BooleanVar(value=work["notify_enabled"])
    v_port = tk.StringVar(value=str(work["notify_port"]))
    v_key = tk.StringVar(value=work["notify_key"])
    v_show = tk.BooleanVar(value=False)
    chk(t, "Let other programs send messages through this agent (this PC only)",
        v_non).pack(anchor="w")
    r = tk.Frame(t, bg=BG)
    r.pack(anchor="w", pady=2)
    lab(r, "Port").pack(side="left")
    ent(r, v_port, 6).pack(side="left", padx=(6, 14))
    lab(r, "Key").pack(side="left")
    key_e = ent(r, v_key, 30, show="\u2022")
    key_e.pack(side="left", padx=6)
    key_e.config(state="readonly", readonlybackground=CARD)

    def reveal():
        key_e.config(show="" if v_show.get() else "\u2022")
    chk(r, "show", v_show, command=reveal).pack(side="left")

    def clip(text, what):
        win.clipboard_clear()
        win.clipboard_append(text)
        say(f"{what} copied.", GREEN)

    def new_key():
        v_key.set(cfgmod.new_token())
        say("New key made. Save to use it. Anything using the old key stops working.", AMBER)

    def cmd_cli():
        return 'peek-agent.exe notify --severity red --title "Intrusion" --message "New login"'

    def cmd_ps():
        return ("Invoke-RestMethod -Method Post -Uri http://127.0.0.1:%s/notify "
                "-Headers @{Authorization='Bearer %s'} -ContentType application/json "
                "-Body '{\"title\":\"Intrusion\",\"severity\":\"red\"}'"
                % (v_port.get().strip(), v_key.get() or "<key>"))

    r = tk.Frame(t, bg=BG)
    r.pack(anchor="w", pady=6)
    btn(r, "New key", new_key).pack(side="left", padx=(0, 6))
    btn(r, "Copy command line", lambda: clip(cmd_cli(), "Command")).pack(side="left", padx=(0, 6))
    btn(r, "Copy PowerShell (with key)", lambda: clip(cmd_ps(), "PowerShell command")
        ).pack(side="left")
    r = tk.Frame(t, bg=BG)
    r.pack(anchor="w")
    def open_outbox():
        svc.outbox_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(str(svc.outbox_dir))
    btn(r, "Open outbox folder", open_outbox).pack(side="left")
    lab(t, "A program can also drop a .json file in that folder, or POST to the port above.\n"
           "Details and examples: windows/NOTIFY.md", size=8, justify="left"
        ).pack(anchor="w", pady=(6, 0))

    # ----------------------------------------------------------------- save
    def save():
        def num(var, name, lo, hi, cast=float):
            try:
                v = cast(var.get().strip())
            except ValueError:
                raise ValueError(f"{name} must be a number")
            if not lo <= v <= hi:
                raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
            return v
        try:
            ttl = num(v_ttl, "Keep messages for", 1, 72)
            interval = num(v_sint, "Read every", 2, 600)
            port = num(v_port, "Port", 1024, 65535, int)
            for var, nm in ((v_qs, "Quiet hours start"), (v_qe, "Quiet hours end")):
                if pa._hhmm(var.get()) is None:
                    raise ValueError(f"{nm} must look like 22:00")
        except ValueError as e:
            say(str(e), RED)
            return

        A["enabled"], A["local_toast"], A["local_ring"] = v_enabled.get(), v_toast.get(), v_ring.get()
        A["quiet"] = {"enabled": v_qon.get(), "start": pa._hhmm(v_qs.get()),
                      "end": pa._hhmm(v_qe.get()),
                      "days": [i for i in range(7) if v_days[i].get()],
                      "allow_critical": v_qcrit.get()}
        new = dict(runner.cfg)
        new.update({"alerts": A, "sensors": work["sensors"], "sensors_enabled": v_son.get(),
                    "extras_interval": interval, "message_ttl_hours": ttl,
                    "notify_enabled": v_non.get(), "notify_port": port,
                    "notify_key": v_key.get()})
        cleaned = cfgmod.validate(dict(new))
        try:
            cfgmod.save(cleaned)
        except OSError as e:
            say(f"Could not save: {e}", RED)
            return
        runner.restart(cleaned)
        problems = cleaned.get("_warnings", []) + svc.problems()
        if problems:
            say("Saved, but: " + "; ".join(problems), AMBER)
        else:
            say("Saved.", GREEN)
        v_key.set(runner.cfg["notify_key"] or v_key.get())   # made on restart if it was empty

    show("Alerts")
    refresh_found()
    for w in cleaned_warnings(runner):
        say(w, AMBER)
    win.focus_set()
    return win


def cleaned_warnings(runner):
    """Mistakes found in a hand-edited config.json, to show on opening."""
    w = runner.cfg.get("_warnings") or []
    return ["config.json: " + "; ".join(w[:3]) + (" ..." if len(w) > 3 else "")] if w else []