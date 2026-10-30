# template_dispatcher.py
import os
import json
from typing import Dict, Any, Optional
from dotenv import load_dotenv
from twilio.rest import Client
from agromet_advisory_engine import generate_farmer_advisory

load_dotenv()

ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_NUMBER", "+14155238886")
TEMPLATE_SID = os.getenv("TWILIO_TEMPLATE_SID")

client = Client(ACCOUNT_SID, AUTH_TOKEN) if ACCOUNT_SID and AUTH_TOKEN else None


def send_whatsapp_template_alert(
    to_phone: str,
    advisory: Dict[str, Any],
    template_sid: Optional[str] = None,
    lang: str = "hi"
) -> Optional[str]:
    """
    Sends WhatsApp agromet alerts using Twilio Content Template ID (HX...).
    
    Parameters:
    - to_phone: Target phone in E.164 format (+91XXXXXXXXXX)
    - advisory: Output dictionary from generate_farmer_advisory()
    - template_sid: The HX... SID (defaults to TWILIO_TEMPLATE_SID from .env)
    - lang: 'hi' for Hindi, 'en' for English
    """
    if not client:
        print("❌ Error: Missing Twilio credentials.")
        return None

    sid_to_use = template_sid or TEMPLATE_SID
    if not sid_to_use:
        print("❌ Error: Missing TEMPLATE_SID. Set TWILIO_TEMPLATE_SID in .env")
        return None

    clean_to = to_phone.strip()
    recipient = f"whatsapp:{clean_to}" if not clean_to.startswith("whatsapp:") else clean_to
    sender = f"whatsapp:{WHATSAPP_FROM}" if not WHATSAPP_FROM.startswith("whatsapp:") else WHATSAPP_FROM

    # Extract advisory parameters
    village = advisory["village"]
    khasra = f" (Khasra #{advisory['khasra_no']})" if advisory.get("khasra_no") else ""
    location_tag = f"{village}{khasra}"
    rain_val = f"{advisory['rain_mm']:.1f} mm/hr"
    risk_level = advisory["level"]
    action = advisory[lang]["actions"][0]

    # Map dynamic variables to your template's placeholders: {{1}}, {{2}}, {{3}}, etc.
    # Adjust the keys ('1', '2', '3') to match the number of placeholders in your HX template
    content_vars = {
        "1": location_tag,
        "2": f"{risk_level} ({rain_val})",
        "3": action
    }

    try:
        message = client.messages.create(
            from_=sender,
            to=recipient,
            content_sid=sid_to_use,
            content_variables=json.dumps(content_vars)
        )
        print(f"✅ [WhatsApp Delivered via Template]")
        print(f"   Message SID  : {message.sid}")
        print(f"   Template SID : {sid_to_use}")
        print(f"   Target       : {recipient}")
        print(f"   Village      : {location_tag}")
        return message.sid

    except Exception as e:
        print(f"❌ [Template Dispatch Failed]: {str(e)}")
        return None


# =====================================================================
# DEMO EXECUTION
# =====================================================================
if __name__ == "__main__":
    target_mobile = os.getenv("TEST_FARMER_PHONE", "+918697232379")

    # Sample alert payload for Rampur (covering Cells [3,2] & [3,3])
    sample_advisory = generate_farmer_advisory(
        village_name="Rampur",
        rain_mm=4.6,
        wind_kmh=18.0,
        humidity_pct=85.0,
        cell_ids=["cell_3_2", "cell_3_3"],
        zone_descriptor="Settlement Core & Drying Yards",
        khasra_no="142/1",
        crop_stage="Harvest"
    )

    print("--- Dispatching Weather Alert using Template ID ---")
    send_whatsapp_template_alert(
        to_phone=target_mobile,
        advisory=sample_advisory,
        lang="hi"
    )