# carrier_list.py — Master list of carriers for the "Contracted Carriers" picker
# Agents select which carriers they're contracted with.
# GrokBot then ONLY references these carriers in conversations.

CARRIER_LIST = [
    # === A ===
    {"key": "aflac", "name": "Aflac"},
    {"key": "aetna", "name": "Aetna"},
    {"key": "aig", "name": "AIG (American International Group)"},
    {"key": "allianz", "name": "Allianz Life"},
    {"key": "allstate", "name": "Allstate"},
    {"key": "american_amicable", "name": "American Amicable"},
    {"key": "american_equity", "name": "American Equity"},
    {"key": "american_general", "name": "American General (AIG)"},
    {"key": "american_home_life", "name": "American Home Life"},
    {"key": "american_national", "name": "American National (ANICO)"},
    {"key": "ameritas", "name": "Ameritas"},
    {"key": "americo", "name": "Americo"},

    # === B ===
    {"key": "banner_lga", "name": "Banner Life (LGA)"},
    {"key": "bestow", "name": "Bestow"},
    {"key": "brighthouse", "name": "Brighthouse Financial"},

    # === C ===
    {"key": "cigna", "name": "Cigna"},
    {"key": "colonial_penn", "name": "Colonial Penn"},
    {"key": "columbian", "name": "Columbian Financial Group"},
    {"key": "corebridge", "name": "Corebridge Financial"},

    # === E ===
    {"key": "equitable", "name": "Equitable"},
    {"key": "ethos", "name": "Ethos Life"},

    # === F ===
    {"key": "family_heritage", "name": "Family Heritage Life"},
    {"key": "fgl", "name": "Fidelity & Guaranty Life (FGL)"},
    {"key": "foresters", "name": "Foresters Financial"},

    # === G ===
    {"key": "gerber", "name": "Gerber Life"},
    {"key": "globe_life", "name": "Globe Life"},
    {"key": "great_western", "name": "Great Western Insurance"},
    {"key": "gtl", "name": "Guarantee Trust Life (GTL)"},
    {"key": "guardian", "name": "Guardian Life"},

    # === J ===
    {"key": "john_hancock", "name": "John Hancock"},

    # === K ===
    {"key": "kansas_city_life", "name": "Kansas City Life"},
    {"key": "kemper", "name": "Kemper Life"},

    # === L ===
    {"key": "ladder", "name": "Ladder Life"},
    {"key": "liberty_mutual", "name": "Liberty Mutual"},
    {"key": "lincoln_financial", "name": "Lincoln Financial"},

    # === M ===
    {"key": "massmutual", "name": "MassMutual"},
    {"key": "metlife", "name": "MetLife"},
    {"key": "mutual_of_omaha", "name": "Mutual of Omaha"},

    # === N ===
    {"key": "national_life", "name": "National Life Group"},
    {"key": "nationwide", "name": "Nationwide"},
    {"key": "new_york_life", "name": "New York Life"},
    {"key": "north_american", "name": "North American (Sammons)"},
    {"key": "northwestern_mutual", "name": "Northwestern Mutual"},

    # === O ===
    {"key": "ohio_national", "name": "Ohio National"},

    # === P ===
    {"key": "pacific_life", "name": "Pacific Life"},
    {"key": "penn_mutual", "name": "Penn Mutual"},
    {"key": "primerica", "name": "Primerica"},
    {"key": "principal", "name": "Principal Financial"},
    {"key": "protective", "name": "Protective Life"},
    {"key": "prudential", "name": "Prudential"},

    # === R ===
    {"key": "royal_neighbors", "name": "Royal Neighbors of America"},

    # === S ===
    {"key": "sagicor", "name": "Sagicor Life"},
    {"key": "sbli", "name": "SBLI"},
    {"key": "securian", "name": "Securian Financial"},
    {"key": "state_farm", "name": "State Farm"},
    {"key": "symetra", "name": "Symetra"},

    # === T ===
    {"key": "transamerica", "name": "Transamerica"},
    {"key": "trinity", "name": "Trinity Life Insurance"},
    {"key": "trustage", "name": "TruStage (CUNA Mutual)"},

    # === U ===
    {"key": "united_of_omaha", "name": "United of Omaha"},
    {"key": "unum", "name": "Unum"},
    {"key": "usaa", "name": "USAA"},

    # === V ===
    {"key": "voya", "name": "Voya Financial"},
]

# Quick lookup: key → display name
CARRIER_MAP = {c["key"]: c["name"] for c in CARRIER_LIST}


def get_carrier_names(keys: list) -> list:
    """Convert a list of carrier keys to display names."""
    return [CARRIER_MAP.get(k, k) for k in keys if k in CARRIER_MAP]


def validate_carrier_keys(keys: list) -> list:
    """Filter to only valid carrier keys."""
    valid = set(CARRIER_MAP.keys())
    return [k for k in keys if k in valid]
