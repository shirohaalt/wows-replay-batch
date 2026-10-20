"""Shared desktop controls for navigation, toggles and replay selection."""
import tkinter.font as tkfont
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

BG = "#0d1524"
PANEL = "#162236"
FIELD = "#0e192c"
TEXT = "#e7edf7"
MUTED = "#8fa3bf"
ACCENT = "#45d7b5"
FONT = "Microsoft YaHei UI"


def configure_stripes(tree):
    tree.tag_configure("stripe_even", background=PANEL)
    tree.tag_configure("stripe_odd", background="#20324b")
    for event in ("<<TreeviewOpen>>", "<<TreeviewClose>>"):
        tree.bind(event, lambda _, t=tree: t.after_idle(lambda: stripe_rows(t)), add="+")


def stripe_rows(tree):
    def rows(parent=""):
        for key in tree.get_children(parent):
            yield key
            if tree.item(key, "open"):
                yield from rows(key)
    for index, key in enumerate(rows()):
        tags = [t for t in tree.item(key, "tags") if not t.startswith("stripe_")]
        tree.item(key, tags=(*tags, "stripe_odd" if index % 2 else "stripe_even"))


def configure_controls(root):
    style = ttk.Style(root)
    for name, width, horizontal_padding in (("Studio.TNotebook", 17, 16), ("Draft.TNotebook", 14, 12)):
        style.configure(name, background=BG, borderwidth=0, bordercolor=BG, lightcolor=BG,
                        darkcolor=BG, tabmargins=(0, 0, 0, 0))
        tab = name + ".Tab"
        padding = (horizontal_padding, 11)
        style.configure(tab, font=(FONT, 10), width=width, anchor="center", padding=padding,
                        expand=(0, 0, 0, 0), borderwidth=0, focuscolor=ACCENT)
        # clam maps selected tabs to a different padding by default. Override
        # both states so switching pages changes only their colors.
        style.map(tab, padding=[("selected", padding), ("!selected", padding)],
                  expand=[("selected", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))],
                  background=[("selected", "#245762"), ("!selected", PANEL)],
                  foreground=[("selected", TEXT), ("!selected", MUTED)],
                  lightcolor=[("selected", "#245762"), ("!selected", PANEL)],
                  darkcolor=[("selected", "#245762"), ("!selected", PANEL)],
                  bordercolor=[("selected", "#245762"), ("!selected", PANEL)])
        style.layout(tab, [("Notebook.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"sticky": "nswe", "children": [
                ("Notebook.focus", {"sticky": "nswe", "children": [
                    ("Notebook.label", {"sticky": "nswe"})]})]})]})])

    size = max(18, round(tkfont.Font(root=root, family=FONT, size=10).metrics("linespace") * .9))
    images = {}
    for state in ("off", "on", "hover", "disabled_off", "disabled_on"):
        scale = 3
        image = Image.new("RGBA", ((size + 9) * scale, size * scale))
        draw = ImageDraw.Draw(image)
        selected = state in ("on", "disabled_on")
        disabled = state.startswith("disabled")
        border = "#3a4a60" if disabled else ACCENT if selected or state == "hover" else MUTED
        fill = "#37665f" if disabled and selected else ACCENT if selected else FIELD
        draw.rounded_rectangle((scale, scale, (size - 1) * scale, (size - 1) * scale),
                               radius=3 * scale, fill=fill, outline=border, width=2 * scale)
        if selected:
            draw.line([(size * .25 * scale, size * .51 * scale), (size * .44 * scale, size * .7 * scale),
                       (size * .77 * scale, size * .32 * scale)],
                      fill="#90b5ae" if disabled else "#09281f", width=2 * scale, joint="curve")
        images[state] = ImageTk.PhotoImage(image.resize((size + 9, size), Image.Resampling.LANCZOS), master=root)
    root._check_images = images
    style.element_create("Studio.Checkbutton.indicator", "image", images["off"],
                         ("disabled", "selected", images["disabled_on"]),
                         ("disabled", images["disabled_off"]), ("selected", images["on"]),
                         ("active", images["hover"]), sticky="w")
    style.layout("Studio.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Studio.Checkbutton.indicator", {"side": "left", "sticky": "w"}),
        ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
            ("Checkbutton.label", {"sticky": "nswe"})]})]})])
    style.configure("Studio.TCheckbutton", background=PANEL, foreground=TEXT, font=(FONT, 10),
                    padding=(2, 6), anchor="w", justify="left", focuscolor=ACCENT)
    style.map("Studio.TCheckbutton", background=[("active", PANEL)], foreground=[("disabled", "#637893")])
    style.configure("Ship.Studio.TCheckbutton", padding=(2, 6))

    font = tkfont.Font(root=root, family=FONT, size=11)
    style.configure("Trail.Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT,
                    font=(FONT, 11), rowheight=max(56, 2 * font.metrics("linespace") + 14), borderwidth=0)
    style.map("Trail.Treeview", background=[("selected", "#245762")], foreground=[("selected", TEXT)])
    style.layout("Trail.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
