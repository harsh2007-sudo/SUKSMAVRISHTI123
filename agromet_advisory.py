# agromet_advisory_engine.py

def generate_farmer_advisory(village_name, rain_mm, wind_kmh, humidity_pct, crop_stage="Harvest"):
    """
    Evaluates hyperlocal weather predictions against agrometeorological thresholds
    and returns actionable advisories in both English and Hindi.
    
    Parameters:
    - village_name (str): Name of the localized 1 km target village.
    - rain_mm (float): Predicted rainfall intensity in mm/hr.
    - wind_kmh (float): Predicted wind speed in km/h.
    - humidity_pct (float): Relative humidity percentage.
    - crop_stage (str): 'Harvest', 'Vegetative', or 'Sowing'.
    
    Returns:
    - dict: Structured alert with severity level, English payload, and Hindi payload.
    """
    
    # -------------------------------------------------------------
    # RULE 1: CRITICAL DOWNPOUR (Immediate Harvest Threat)
    # -------------------------------------------------------------
    if rain_mm >= 3.0:
        level = "RED"
        
        # English Advisory
        en_headline = f"🚨 URGENT: Heavy shower ({rain_mm:.1f} mm/hr) expected within 35-45 minutes!"
        en_actions = [
            "Cover open grains, threshing floors, and oilseed heaps with plastic tarpaulins immediately.",
            "Halt all combine harvesters and field cutting to prevent machinery from waterlogging.",
            "Do NOT load produce into open tractor-trolleys for mandi transit."
        ]
        
        # Hindi Advisory
        hi_headline = f"🚨 आपातकालीन चेतावनी: अगले 35-45 मिनट में तेज बारिश ({rain_mm:.1f} मिमी/घंटा) की संभावना!"
        hi_actions = [
            "खलिहान में रखी खुली कटी फसल और अनाज की बोरियों को तिरपाल से तुरंत ढकें।",
            "खेतों में कटाई और थ्रेशिंग का काम तुरंत रोक दें।",
            "मंडी जाने वाली खुली ट्रैक्टर-ट्रॉलियों में अनाज बिल्कुल न लादें।"
        ]

    # -------------------------------------------------------------
    # RULE 2: MODERATE RAIN / CHEMICAL WASHOUT THREAT
    # -------------------------------------------------------------
    elif (1.0 <= rain_mm < 3.0) or (rain_mm >= 0.5 and humidity_pct > 80.0):
        level = "AMBER"
        
        # English Advisory
        en_headline = f"⚠️ ADVISORY: Localized light-to-moderate rain ({rain_mm:.1f} mm/hr) expected."
        en_actions = [
            "Postpone all chemical spraying (pesticides, urea, fungicides) for the next 6-8 hours to prevent washout.",
            "Skip scheduled field irrigation today to save electricity and diesel costs.",
            "Ensure drainage channels around standing crops are clear of debris."
        ]
        
        # Hindi Advisory
        hi_headline = f"⚠️ कृषि सलाह: हल्की से मध्यम बारिश ({rain_mm:.1f} मिमी/घंटा) की संभावना।"
        hi_actions = [
            "कीटनाशक, यूरिया या किसी भी रसायन का छिड़काव अगले 6-8 घंटे तक रोक दें; बारिश से दवा बह जाएगी।",
            "आज खेतों की सिंचाई टाल दें, जिससे डीजल और बिजली के खर्च की बचत होगी।",
            "खेतों की जलनिकासी की नालियों को साफ रखें ताकि पानी न भरे।"
        ]

    # -------------------------------------------------------------
    # RULE 3: HIGH WIND / CROP LODGING RISK
    # -------------------------------------------------------------
    elif wind_kmh >= 24.0 and rain_mm >= 0.3:
        level = "AMBER"
        
        # English Advisory
        en_headline = f"⚠️ WIND & LODGING RISK: Gusty winds up to {wind_kmh:.0f} km/h with damp ground."
        en_actions = [
            "Do not flood or irrigate tall standing crops (paddy, sugarcane, mustard); wet soil with high wind causes lodging (plants falling flat).",
            "Secure temporary greenhouse netting, nursery shades, and plastic covers tightly.",
            "Support and stake vulnerable vegetable creepers and fruit-bearing plants."
        ]
        
        # Hindi Advisory
        hi_headline = f"⚠️ तेज हवा का जोखिम: {wind_kmh:.0f} किमी/घंटा की रफ्तार से हवाएं चलने की संभावना।"
        hi_actions = [
            "लंबी फसलों (धान, सरसों, गन्ना) में सिंचाई बिल्कुल न करें; गीली जमीन में तेज हवा से फसल गिर (लॉजिंग) सकती है।",
            "नर्सरी के शेड और तिरपाल को अच्छी तरह बांधकर सुरक्षित करें।",
            "सब्जियों और बेल वाली फसलों को डंडों का सहारा देकर बांधें।"
        ]

    # -------------------------------------------------------------
    # RULE 4: DRY & STABLE WINDOW (Safe Agricultural Operations)
    # -------------------------------------------------------------
    else:
        level = "GREEN"
        
        # English Advisory
        en_headline = "☀️ SAFE WEATHER: Dry and clear conditions across your village."
        en_actions = [
            "Ideal window for crop drying on open threshing floors.",
            "Safe conditions for spraying chemical formulations with high absorption efficiency.",
            "Favorable conditions to transport harvest in open vehicles to distant mandis for better market prices."
        ]
        
        # Hindi Advisory
        hi_headline = "☀️ मौसम सुरक्षित: आपके गांव में मौसम साफ और सूखा रहने का अनुमान है।"
        hi_actions = [
            "खलिहान में कटी फसल को सुखाने और थ्रेशिंग करने के लिए समय उत्तम है।",
            "कीटनाशक और उर्वरक छिड़काव के लिए अनुकूल समय; दवा पूरी तरह प्रभावी होगी।",
            "अच्छे भाव के लिए दूर की मंडियों में खुली ट्रॉलियों से फसल बेचने ले जाने का सुरक्षित समय है।"
        ]

    return {
        "village": village_name,
        "level": level,
        "rain_mm": rain_mm,
        "wind_kmh": wind_kmh,
        "humidity_pct": humidity_pct,
        "en": {
            "headline": en_headline,
            "actions": en_actions
        },
        "hi": {
            "headline": hi_headline,
            "actions": hi_actions
        }
    }


# =============================================================
# VERIFICATION / USAGE DEMO
# =============================================================
if __name__ == "__main__":
    # Test Scenario 1: Isolated convective burst over Village Rampur
    advisory_rampur = generate_farmer_advisory(
        village_name="Rampur",
        rain_mm=4.6,
        wind_kmh=16.0,
        humidity_pct=84.0,
        crop_stage="Harvest"
    )
    
    print("==================================================")
    print(f"VILLAGE: {advisory_rampur['village']} | ALERT LEVEL: {advisory_rampur['level']}")
    print("==================================================")
    print("[ENGLISH ALERT]")
    print(advisory_rampur["en"]["headline"])
    for action in advisory_rampur["en"]["actions"]:
        print(f"  • {action}")
        
    print("\n[HINDI ALERT / हिंदी संदेश]")
    print(advisory_rampur["hi"]["headline"])
    for action in advisory_rampur["hi"]["actions"]:
        print(f"  • {action}")
        
    print("\n" + "="*50)
    
    # Test Scenario 2: Clear weather over neighboring Village Fatehpur
    advisory_fatehpur = generate_farmer_advisory(
        village_name="Fatehpur",
        rain_mm=0.1,
        wind_kmh=9.0,
        humidity_pct=52.0,
        crop_stage="Harvest"
    )
    
    print(f"VILLAGE: {advisory_fatehpur['village']} | ALERT LEVEL: {advisory_fatehpur['level']}")
    print("==================================================")
    print("[ENGLISH ALERT]")
    print(advisory_fatehpur["en"]["headline"])
    for action in advisory_fatehpur["en"]["actions"]:
        print(f"  • {action}")
        
    print("\n[HINDI ALERT / हिंदी संदेश]")
    print(advisory_fatehpur["hi"]["headline"])
    for action in advisory_fatehpur["hi"]["actions"]:
        print(f"  • {action}")