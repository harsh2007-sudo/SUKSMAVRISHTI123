# agromet_advisory_engine.py
"""
SūkṣmaVṛṣṭi Agrometeorological Advisory Decision Engine
Translates 1 km x 1 km downscaled convective predictions and multi-cell village
aggregations into actionable, bilingual farm-level directives.
"""

from typing import List, Optional, Dict, Any


def generate_farmer_advisory(
    village_name: str,
    rain_mm: float,
    wind_kmh: float,
    humidity_pct: float,
    cell_ids: Optional[List[str]] = None,
    zone_descriptor: Optional[str] = None,
    khasra_no: Optional[str] = None,
    crop_stage: str = "Harvest"
) -> Dict[str, Any]:
    """
    Evaluates weather predictions against agromet action thresholds.

    Parameters:
    - village_name (str): Target village name (e.g., 'Rampur', 'Nawabganj').
    - rain_mm (float): Effective rainfall rate in mm/hr (Max-Risk peak across cells).
    - wind_kmh (float): Predicted wind speed in km/h.
    - humidity_pct (float): Relative humidity percentage.
    - cell_ids (list, optional): Micro-cells covering the village (e.g., ['cell_3_2', 'cell_3_3']).
    - zone_descriptor (str, optional): Land use/zone (e.g., 'Settlement Core & Drying Yards').
    - khasra_no (str, optional): Bhulekh Khasra/Plot number if queried by a farmer.
    - crop_stage (str): Agricultural stage ('Harvest', 'Vegetative', 'Sowing').

    Returns:
    - dict: Structured advisory with risk level, metrics, and bilingual English/Hindi payloads.
    """

    cell_tag = f" [Cells: {', '.join(cell_ids)}]" if cell_ids else ""
    khasra_tag_en = f" | Khasra #{khasra_no}" if khasra_no else ""
    khasra_tag_hi = f" | खसरा सं. #{khasra_no}" if khasra_no else ""
    zone_tag_en = f" ({zone_descriptor})" if zone_descriptor else ""

    # =========================================================================
    # RULE 1: CRITICAL DOWNPOUR (Immediate Harvest & Threshing Floor Threat)
    # =========================================================================
    if rain_mm >= 3.0:
        level = "RED"
        color_hex = "#D32F2F"

        en_headline = (
            f"🚨 URGENT: Heavy convective downpour ({rain_mm:.1f} mm/hr) expected within 35-45 minutes "
            f"over {village_name}{zone_tag_en}{cell_tag}{khasra_tag_en}!"
        )
        en_actions = [
            "Immediately cover open grain heaps, oilseed piles, and threshing yards with plastic tarpaulins.",
            "Halt all combine harvesting, tractor operations, and manual cutting immediately to prevent machinery waterlogging.",
            "Do NOT load grain into open tractor-trolleys for transit to Nawabganj or local mandis.",
            "Open drainage outlets at field edges to prevent prolonged root submersion in low-lying plots."
        ]

        hi_headline = (
            f"🚨 आपातकालीन चेतावनी: अगले 35-45 मिनट में {village_name}{khasra_tag_hi} में तेज मूसलाधार बारिश "
            f"({rain_mm:.1f} मिमी/घंटा) की संभावना!"
        )
        hi_actions = [
            "खलिहान में रखी खुली कटी फसल, सरसों और अनाज की बोरियों को तिरपाल व प्लास्टिक शीट से तुरंत ढकें।",
            "खेतों में कंबाइन हार्वेस्टर और कटाई का काम तुरंत रोक दें ताकि फसल व मशीनें दलदल में न फंसें।",
            "मंडी जाने वाली खुली ट्रैक्टर-ट्रॉलियों में अनाज बिल्कुल न लादें; भीगने पर भारी नुकसान होगा।",
            "निचले खेतों की मेड़ों के निकास खोलें ताकि जलभराव से जड़ें सड़ने का खतरा न रहे।"
        ]

    # =========================================================================
    # RULE 2: MODERATE RAIN / CHEMICAL WASHOUT & PONDING RISK
    # =========================================================================
    elif (1.0 <= rain_mm < 3.0) or (rain_mm >= 0.5 and humidity_pct > 80.0):
        level = "AMBER"
        color_hex = "#FFA000"

        en_headline = (
            f"⚠️ ADVISORY: Localized light-to-moderate rain ({rain_mm:.1f} mm/hr) expected over "
            f"{village_name}{zone_tag_en}{khasra_tag_en}."
        )
        en_actions = [
            "Postpone all chemical spraying (pesticides, urea, fungicides) for the next 6-8 hours to prevent wash-off.",
            "Skip scheduled tube-well and pump-set irrigation today to save fuel and electricity costs.",
            "Clear debris and silt from field irrigation furrows to facilitate natural runoff."
        ]

        hi_headline = (
            f"⚠️ कृषि सलाह: {village_name}{khasra_tag_hi} में हल्की से मध्यम बारिश ({rain_mm:.1f} मिमी/घंटा) का अनुमान।"
        )
        hi_actions = [
            "कीटनाशक, यूरिया या फफूंदनाशक का छिड़काव अगले 6-8 घंटे तक रोक दें; बारिश से दवा बहकर निष्प्रभावी हो जाएगी।",
            "आज नलकूप या पंपसेट से खेतों की सिंचाई टाल दें, इससे डीजल और बिजली की बचत होगी।",
            "खेतों की जलनिकासी की नालियों से खरपतवार हटाएं ताकि पानी सुचारू रूप से निकल सके।"
        ]

    # =========================================================================
    # RULE 3: HIGH WIND / CROP LODGING RISK
    # =========================================================================
    elif wind_kmh >= 24.0 and rain_mm >= 0.3:
        level = "AMBER"
        color_hex = "#FFA000"

        en_headline = (
            f"⚠️ WIND & LODGING RISK: Gusty winds up to {wind_kmh:.0f} km/h with moist topsoil over "
            f"{village_name}{khasra_tag_en}."
        )
        en_actions = [
            "Do NOT flood tall standing crops (paddy, mustard, sugarcane); moist root zones under gusty winds trigger crop lodging (falling flat).",
            "Secure temporary nursery netting, poly-house frames, and threshing floor tarpaulins tightly.",
            "Provide bamboo/stake support to trailing vegetables and fruit-bearing plants."
        ]

        hi_headline = (
            f"⚠️ तेज हवा का जोखिम: {village_name}{khasra_tag_hi} में {wind_kmh:.0f} किमी/घंटा की रफ्तार से तेज झोंके चलने की संभावना।"
        )
        hi_actions = [
            "लंबी खड़ी फसलों (धान, सरसों, गन्ना) में पानी बिल्कुल न भरें; गीली मिट्टी में तेज हवा से फसल जमीन पर गिर (लॉजिंग) जाएगी।",
            "नर्सरी के शेड, पॉलीहाउस और तिरपालों को खूंटों से अच्छी तरह बांधकर सुरक्षित करें।",
            "सब्जियों और बेल वाली फसलों को डंडों व रस्सियों का सहारा दें।"
        ]

    # =========================================================================
    # RULE 4: DRY & STABLE WINDOW (Optimal Field & Mandi Logistics)
    # =========================================================================
    else:
        level = "GREEN"
        color_hex = "#2E7D32"

        en_headline = f"☀️ SAFE WEATHER: Dry and clear conditions across {village_name}{khasra_tag_en}."
        en_actions = [
            "Optimal operational window for grain drying on open threshing yards and mechanical threshing.",
            "Safe conditions for foliar nutrient and chemical spray applications with high absorption efficacy.",
            "Favorable road and weather conditions to load open trolleys for transit to distant mandis for premium pricing."
        ]

        hi_headline = f"☀️ मौसम अनुकूल: {village_name}{khasra_tag_hi} में मौसम साफ और शुष्क रहने का अनुमान है।"
        hi_actions = [
            "खलिहान में फसल फैलाने, सुखाने और थ्रेशिंग का कार्य करने के लिए मौसम उत्तम है।",
            "कीटनाशक और उर्वरक छिड़काव के लिए अनुकूल समय; दवा का पूर्ण असर मिलेगा।",
            "खुली ट्रैक्टर-ट्रॉलियों में अनाज भरकर दूर की मंडियों में ले जाने के लिए सुरक्षित समय है।"
        ]

    return {
        "village": village_name,
        "level": level,
        "color_hex": color_hex,
        "rain_mm": rain_mm,
        "wind_kmh": wind_kmh,
        "humidity_pct": humidity_pct,
        "cell_ids": cell_ids or [],
        "zone_descriptor": zone_descriptor or "N/A",
        "khasra_no": khasra_no,
        "crop_stage": crop_stage,
        "en": {
            "headline": en_headline,
            "actions": en_actions
        },
        "hi": {
            "headline": hi_headline,
            "actions": hi_actions
        }
    }


def format_whatsapp_advisory(advisory: Dict[str, Any], lang: str = "hi") -> str:
    """
    Formats the advisory dictionary into an SMS/WhatsApp-ready plain text broadcast.
    """
    payload = advisory[lang]
    village = advisory["village"]
    khasra = f" (खसरा #{advisory['khasra_no']})" if (advisory.get("khasra_no") and lang == "hi") else (
        f" (Khasra #{advisory['khasra_no']})" if advisory.get("khasra_no") else ""
    )

    if lang == "hi":
        msg = f"🌾 *सूक्ष्मवृष्टि (SūkṣmaVṛṣṭi) कृषि परामर्श*\n"
        msg += f"📍 *ग्राम:* {village}{khasra}\n"
        msg += f"⚡ *स्तर:* {advisory['level']}\n\n"
        msg += f"{payload['headline']}\n\n"
        msg += "*तुरंत क्या कदम उठाएं:*\n"
        for i, act in enumerate(payload["actions"], 1):
            msg += f"{i}. {act}\n"
    else:
        msg = f"🌾 *SūkṣmaVṛṣṭi Agromet Advisory*\n"
        msg += f"📍 *Village:* {village}{khasra}\n"
        msg += f"⚡ *Status:* {advisory['level']}\n\n"
        msg += f"{payload['headline']}\n\n"
        msg += "*Immediate Actions:*\n"
        for i, act in enumerate(payload["actions"], 1):
            msg += f"{i}. {act}\n"

    return msg


# =============================================================================
# QUICK VERIFICATION TEST
# =============================================================================
if __name__ == "__main__":
    # Test Scenario: Convective burst over multi-cell Rampur on a specific Khasra plot
    test_advisory = generate_farmer_advisory(
        village_name="Rampur",
        rain_mm=4.8,
        wind_kmh=18.0,
        humidity_pct=85.0,
        cell_ids=["cell_3_2", "cell_3_3"],
        zone_descriptor="Settlement Core & Drying Yards",
        khasra_no="142/1",
        crop_stage="Harvest"
    )

    print("--- HINDI WHATSAPP PAYLOAD ---")
    print(format_whatsapp_advisory(test_advisory, lang="hi"))

    print("\n--- ENGLISH WHATSAPP PAYLOAD ---")
    print(format_whatsapp_advisory(test_advisory, lang="en"))