"""Put personalised job-application emails into your Gmail Drafts, with your CV attached.

Nothing is ever sent by this script. Every email lands in Gmail -> Drafts, where you
read it, change anything you like, and press Send yourself.

1. In the dashboard's Email tab, save companies, write your template, then click
   "Download selected for the Gmail drafts tool" -> emails-to-draft.csv
2. Try it without touching Gmail (writes .eml files you can open and check):
     python mailer.py emails-to-draft.csv --cv "My_CV.pdf" --cover-template cover_letter.txt --dry-run
3. Create the drafts:
     python mailer.py emails-to-draft.csv --cv "My_CV.pdf" --cover-template cover_letter.txt

Gmail needs an *App Password* for this (not your normal password):
  Google Account -> Security -> turn on 2-Step Verification -> search "App passwords" ->
  create one called "Job drafts". You type it when the script asks; it is never saved.

Options
  --cv FILE               attach this file to every email (PDF recommended)
  --cover FILE            attach the same cover letter file to every email
  --cover-template FILE   a .txt letter with {company} and {date}; a personalised PDF is made per company
  --limit N               at most N drafts per run (default 40)
  --dry-run               write outbox/*.eml instead of logging in to Gmail
"""
import argparse
import csv
import getpass
import imaplib
import mimetypes
import os
import re
import sys
import textwrap
import time
from datetime import date
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

ROOT = os.path.dirname(os.path.abspath(__file__))


# ---------- a tiny PDF writer for plain-text cover letters (no extra installs) ----------

def _pdf_escape(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_to_pdf(text, path):
    """A4, Helvetica 11pt, wrapped at ~95 characters, as many pages as needed."""
    text = text.replace("₹", "Rs. ").replace("’", "'").replace("‘", "'").replace("“", '"') \
        .replace("”", '"').replace("–", "-").replace("—", "-").replace("•", "-")
    lines = []
    for para in text.splitlines():
        lines += textwrap.wrap(para, 95) or [""]
    per_page, pages = 52, []
    for i in range(0, max(len(lines), 1), per_page):
        chunk = lines[i:i + per_page]
        ops = ["BT", "/F1 11 Tf", "14 TL", "60 780 Td"]
        for ln in chunk:
            ops.append(f"({_pdf_escape(ln.encode('latin-1', 'replace').decode('latin-1'))}) Tj T*")
        ops.append("ET")
        pages.append("\n".join(ops).encode("latin-1", "replace"))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", None,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    kids = []
    for content in pages:
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> "
                    b"/Contents %d 0 R >>" % len(objs))
        kids.append(len(objs))
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(f"{k} 0 R" for k in kids).encode(), len(kids))
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    with open(path, "wb") as f:
        f.write(out)


# ---------- building messages ----------

def attach(msg, path, filename=None):
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    main, sub = ctype.split("/", 1)
    with open(path, "rb") as f:
        msg.add_attachment(f.read(), maintype=main, subtype=sub, filename=filename or os.path.basename(path))


def safe(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:60] or "company"


def build(row, sender, args, workdir):
    msg = EmailMessage()
    msg["From"] = sender or "me"
    msg["To"] = row["to"].strip()
    msg["Subject"] = row["subject"].strip()
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(row["body"])
    if args.cv:
        attach(msg, args.cv)
    if args.cover:
        attach(msg, args.cover)
    if args.cover_template:
        with open(args.cover_template, encoding="utf-8") as f:
            letter = f.read().replace("{company}", row.get("company", "")).replace("{date}", date.today().strftime("%d %B %Y"))
        pdf = os.path.join(workdir, f"Cover_Letter_{safe(row.get('company', ''))}.pdf")
        text_to_pdf(letter, pdf)
        attach(msg, pdf, "Cover_Letter.pdf")
    return msg


def drafts_folder(imap):
    typ, boxes = imap.list()
    for b in boxes or []:
        line = b.decode("utf-8", "replace")
        if "\\Drafts" in line:
            return line.split(' "/" ')[-1].strip()
    return '"[Gmail]/Drafts"'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--cv")
    ap.add_argument("--cover")
    ap.add_argument("--cover-template")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for p in (args.cv, args.cover, args.cover_template):
        if p and not os.path.isfile(p):
            sys.exit(f"File not found: {p}")
    if not (args.cv or args.cover or args.cover_template):
        print("Note: no --cv given, so the drafts will have no attachment.")

    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("to", "").strip() and "@" in r["to"]]
    if not rows:
        sys.exit("No rows with an email address in that file.")
    if len(rows) > args.limit:
        print(f"{len(rows)} emails in the file; doing the first {args.limit} (use --limit to change).")
        rows = rows[:args.limit]

    outdir = os.path.join(ROOT, "outbox")
    os.makedirs(outdir, exist_ok=True)

    if args.dry_run:
        for i, r in enumerate(rows, 1):
            msg = build(r, "", args, outdir)
            path = os.path.join(outdir, f"{i:02d}_{safe(r.get('company', 'email'))}.eml")
            with open(path, "wb") as f:
                f.write(bytes(msg))
        print(f"Wrote {len(rows)} sample emails to {outdir}. Open one to check it, then run again without --dry-run.")
        return

    print("Log in to Gmail to create drafts (nothing will be sent).")
    user = input("Gmail address: ").strip()
    pw = getpass.getpass("Gmail App Password (16 letters, input hidden): ").replace(" ", "")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(user, pw)
    except imaplib.IMAP4.error:
        sys.exit("Gmail refused the login. Use an App Password (see the top of this file), not your normal password.")
    folder = drafts_folder(imap)
    done = 0
    for r in rows:
        msg = build(r, user, args, outdir)
        typ, _ = imap.append(folder, "\\Draft", imaplib.Time2Internaldate(time.time()), bytes(msg))
        done += typ == "OK"
        print(f"  {'draft saved' if typ == 'OK' else 'FAILED'}: {r.get('company', '')} <{r['to']}>")
        time.sleep(1)
    imap.logout()
    print(f"\n{done} drafts are in Gmail -> Drafts. Open each one, check it, and press Send.")


if __name__ == "__main__":
    main()
