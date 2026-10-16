"""Delivery of exact approved snapshots, independent of the AI agent."""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .store import Store, validate_patient

FONTS = Path(__file__).parent / "fonts"


def pdf_bytes(store: Store, version_id: str) -> bytes:
    version = store.approved(version_id)
    cached = store.artifact(version_id, "pdf")
    if cached:
        return cached
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    consultation = store.consultation(version["consultation_id"])
    patient = store.patient(consultation["patient_id"])
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 18, 18)
    pdf.add_font("Noto", fname=str(FONTS / "NotoSans-Regular.ttf"))
    pdf.add_font("NotoArabic", fname=str(FONTS / "NotoNaskhArabic-Regular.ttf"))
    pdf.set_fallback_fonts(["NotoArabic"])
    pdf.set_text_shaping(True)
    pdf.add_page()
    pdf.set_font("Noto", size=18)
    pdf.multi_cell(0, 9, "Your consultation, explained", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Noto", size=10)
    for line in (patient["name"], f"{patient['reference']} / {consultation['visit_date']}",
                 f"{version['title']} / {version['language']} / Version {version['number']}"):
        pdf.multi_cell(0, 6, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(5)
    pdf.set_font("Noto", size=11)
    for line in version["text"].splitlines():
        if not line.strip():
            pdf.ln(3)
        else:
            pdf.multi_cell(0, 6, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(5)
    pdf.set_font("Noto", size=9)
    pdf.multi_cell(0, 5, f"Checked by {version['reviewer']} on {version['approved_at'][:10]}.",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    data = bytes(pdf.output())
    store.put_artifact(version_id, "pdf", data)
    return data


def patient_url(token: str) -> str:
    base = os.getenv("CLEARCARE_PUBLIC_URL", "").strip().rstrip("/")
    parts = urlsplit(base)
    if (parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment
            or parts.username or parts.password):
        raise ValueError("Set CLEARCARE_PUBLIC_URL to the app's base URL, without a query or fragment.")
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Patient links need HTTPS, except when testing on localhost.")
    return base + "/?" + urlencode({"share": token})


def send_email(store: Store, version_id: str, recipient: str, include_audio=False):
    version = store.approved(version_id)
    # Reuse validation without collecting or sending patient demographics to a provider.
    validate_patient("Recipient", "2000-01-01", recipient)
    if not recipient:
        raise ValueError("Enter the patient's email address.")
    host, sender = os.getenv("SMTP_HOST"), os.getenv("SMTP_FROM")
    if not host or not sender:
        raise ValueError("Email is not configured. Set SMTP_HOST and SMTP_FROM, or download a PDF.")
    if "\n" in sender or "\r" in sender:
        raise ValueError("Invalid SMTP_FROM setting.")
    audio = store.artifact(version_id, "audio") if include_audio else None
    if include_audio and not audio:
        raise ValueError("Generate the approved audio before attaching it.")
    message = EmailMessage()
    message["From"], message["To"] = sender, recipient
    message["Subject"] = "Your consultation, explained"
    message.set_content(version["text"] + f"\n\nChecked by {version['reviewer']}.\n")
    message.add_attachment(pdf_bytes(store, version_id), maintype="application", subtype="pdf",
                           filename="consultation.pdf")
    if audio:
        message.add_attachment(audio, maintype="audio", subtype="mpeg", filename="explanation.mp3")
    delivery = store.delivery(version_id, "email", recipient, "sending")
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        transport = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        options = {"context": ssl.create_default_context()} if port == 465 else {}
        with transport(host, port, timeout=30, **options) as smtp:
            if port != 465:
                smtp.starttls(context=ssl.create_default_context())
            if username := os.getenv("SMTP_USERNAME"):
                smtp.login(username, os.getenv("SMTP_PASSWORD", ""))
            refused = smtp.send_message(message)
            if refused:
                raise RuntimeError("The email service refused this recipient.")
    except Exception:
        store.delivery_status(delivery, "failed")
        raise RuntimeError("Email delivery failed. Check the address and email configuration before retrying.") from None
    store.delivery_status(delivery, "sent")
    return delivery
