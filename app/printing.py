"""Printing straight to a printer, without a print dialog. The app runs on the business's own Mac, so it can use the
printers set up on it (System Settings › Printers: an office laser, a Brother or Dymo label printer) through the Mac's
printing system, and send labels to a thermal label printer on the network (Zebra, Honeywell, SATO) in its own
language, ZPL. Each workspace chooses a printer for labels and one for documents (Settings › Printers).
"""
import re
import shutil
import socket
import subprocess

from .documents import labels as labels_

ROLES = {"labels": "Labels", "documents": "Documents (invoices, delivery notes, pick lists)"}
DPI = {"203": 8, "300": 12}  # dots per mm of a thermal printer's head


class PrintProblem(Exception):
    """Printing didn't happen; the message says why, plainly."""


def mac_printers():
    """The printers set up on this Mac: ([names], the default's name or None)."""
    if not shutil.which("lpstat"):
        return [], None
    try:
        out = subprocess.run(["lpstat", "-p", "-d"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return [], None
    default = re.search(r"system default destination:\s*(\S+)", out)
    return re.findall(r"^printer (\S+)", out, re.M), default.group(1) if default else None


def chosen(ws, role):
    """The printer a workspace uses for labels or documents: {"kind": "mac" | "zpl", "name" | "address", ...} or None."""
    p = ((ws.settings or {}).get("printers") or {}).get(role)
    return p if isinstance(p, dict) and (p.get("name") or p.get("address")) else None


def describe(p):
    if not p:
        return "no printer"
    return p.get("name", "").replace("_", " ") if p["kind"] == "mac" else f"the label printer at {p['address']}"


def print_labels(ws, items, size=None, copies=1):
    """Labels (labels.for_records) on the labels printer. Returns what was done, in words."""
    p = chosen(ws, "labels")
    if p is None:
        raise PrintProblem("No label printer is chosen yet: an admin chooses one under Settings › Printers")
    size = labels_.size_of(p.get("size") or size)
    if p["kind"] == "zpl":
        _send_zpl(p["address"], zpl(items, size, p.get("dpi") or "203") * max(1, copies))
    else:
        _send_mac(p["name"], labels_.render(items, size), copies, size)
    return f"Sent {len(items)} label{'s' if len(items) != 1 else ''} to {describe(p)}."


def print_pdf(ws, pdf, copies=1):
    """A document (an invoice, a delivery note) on the documents printer."""
    p = chosen(ws, "documents")
    if p is None:
        raise PrintProblem("No printer is chosen for documents yet: an admin chooses one under Settings › Printers")
    if p["kind"] != "mac":
        raise PrintProblem("Documents print on one of this Mac's printers, not a label printer")
    _send_mac(p["name"], pdf, copies)
    return f"Sent it to {describe(p)}."


def _send_mac(name, pdf, copies=1, size=None):
    names, _ = mac_printers()
    if name not in names:
        raise PrintProblem(f"The printer “{name.replace('_', ' ')}” isn't set up on this Mac any more (System Settings › Printers)")
    command = ["lp", "-d", name, "-n", str(max(1, min(int(copies), 99))), "-o", "fit-to-page"]
    if size and labels_.SIZES[size][3] is None:  # a roll of labels: the page is the label
        _, w, h, _ = labels_.SIZES[size]
        command += ["-o", f"media=Custom.{w:g}x{h:g}mm"]
    try:
        done = subprocess.run(command, input=pdf, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PrintProblem(f"The Mac couldn't print it: {exc}") from exc
    if done.returncode != 0:
        raise PrintProblem(f"The Mac couldn't print it: {done.stderr.decode(errors='replace').strip()[:200]}")


def _send_zpl(address, data):
    host, _, port = str(address).rpartition(":") if ":" in str(address) else (address, "", "9100")
    try:
        with socket.create_connection((host or address, int(port or 9100)), timeout=6) as conn:
            conn.sendall(data.encode("utf-8"))
    except (OSError, ValueError) as exc:
        raise PrintProblem(f"The label printer at {address} didn't answer: is it on, and on this network? ({exc})") from exc


def zpl(items, size="62x29", dpi="203"):
    """Labels in ZPL, the language of thermal label printers: the name, the details and the code, laid out as the PDF
    labels are. A sheet size makes no sense on a roll, so it's printed at the small roll size."""
    size = size if labels_.SIZES[labels_.size_of(size)][3] is None else "62x29"
    _, w_mm, h_mm, _ = labels_.SIZES[labels_.size_of(size)]
    dots = DPI.get(str(dpi), 8)
    w, h, pad = round(w_mm * dots), round(h_mm * dots), round(min(w_mm, h_mm) * dots * 0.07)
    tall = h > w * 1.2
    out = []
    for label in items:
        code, kind = str(label.get("code") or ""), label.get("kind") or "qr"
        parts = [f"^XA^CI28^PW{w}^LL{h}^LH0,0"]
        words_w = w - 2 * pad
        if code and kind == "qr":
            mag = max(2, min(10, round((min(h, w) * (0.5 if tall else 0.85)) / 33)))
            side = mag * 33
            x = (w - side) // 2 if tall else w - pad - side
            parts.append(f"^FO{x},{h - pad - side if tall else pad}^BQN,2,{mag}^FDQA,{_clean(code)}^FS")
            words_w = words_w if tall else w - 3 * pad - side
            floor = h - pad - side if tall else h - pad
        elif code:
            bar_h = round(min(h * (0.25 if tall else 0.36), 30 * dots))
            module = max(2, min(4, round(words_w / (11 * len(code) + 35))))
            parts.append(f"^FO{pad},{h - pad - bar_h - 4 * dots}^BY{module}^BCN,{bar_h},Y,N,N^FD{_clean(code)}^FS")
            floor = h - pad - bar_h - 6 * dots
        else:
            floor = h - pad
        title_h = max(18, min(round((floor - pad) / (5 if tall else 3.4)), 70 if tall else 40))
        y = pad
        for n, text in enumerate([label.get("title") or ""] + list(label.get("lines") or [])):
            size_ = title_h if n == 0 else max(16, round(title_h * 0.66))
            if y + size_ > floor:
                break
            parts.append(f"^FO{pad},{y}^A0N,{size_},{size_}^FB{words_w},1,0,L^FD{_clean(text)}^FS")
            y += round(size_ * 1.15)
        parts.append("^XZ")
        out.append("".join(parts))
    return "\n".join(out)


def _clean(text):
    """ZPL's own marks (^ and ~) can't be printed as text: they'd be read as commands."""
    return str(text).replace("^", " ").replace("~", "-")[:200]
