"""
Comprehensive list of US Life Insurance Companies for validation.
Used to detect when a lead mentions a specific company name.
"""

MAJOR_LIFE_INSURANCE_COMPANIES = [
    # Top 25 Life Insurance Companies
    "northwestern mutual",
    "metlife", "metropolitan", "met life",
    "new york life", "newyork life", "ny life",
    "prudential",
    "massmutual", "mass mutual",
    "lincoln national", "lincoln financial",
    "nationwide",
    "state farm", "statefarm",
    "aig", "american international", "american intl",
    "guardian", "guardian life",
    "globe life", "globelife",
    "principal", "principal financial",
    "equitable", "equitable holdings",
    "tiaa",
    "pacific life", "pacificlife",
    "thrivent",
    "usaa", "united serv automobile",
    "penn mutual", "pennmutual",
    "brighthouse", "brighthouse financial",
    "mutual of omaha", "mutualofomaha",
    "transamerica",
    "john hancock", "johnhancock",
    "symetra",
    "banner life", "bannerlife",
    "protective", "protective life",
    "colonial penn", "colonialpenn",
    "aarp",
    "gerber life", "gerberlife",
    "aflac",
    "allstate",
    "farmers", "farmers ins",
    "liberty mutual", "libertymutual",
    "american general", "aig life",
    "voya",
    "unum",
    "cigna",
    "aetna", "aetna cas",
    "humana",
    "anthem",
    "primerica",
    "foresters",
    "legal and general", "legal & general", "lgamerica",
    "zurich", "zurich ins",
    "allianz",
    "cuna mutual", "cunamutual", "cumis",
    "american family", "amfam",
    "securian",
    "ohio national",
    "american united",
    "kansas city life",
    "north american", "north american company",
    "united of omaha", "unitedofomaha",
    "great west", "greatwest", "great-west",
    "manulife",
    "sun life", "sunlife",
    "canada life",
    "aegon",
    "national life", "national life group",
    "sbli",
    "haven life", "havenlife",
    "ladder", "ladder life",
    "ethos", "ethos life",
    "bestow",
    "fabric",
    "policygenius",
    "health iq",
    "selectquote",
    "quotacy",
    "zander",
    "term4sale",
    "accuquote",
    "insure.com",
    "life insurance direct",
    "american income", "american income life", "ail",
    "family heritage",
    "torchmark",
    "kemper",
    "american national", "anico",
    "sammons financial",
    "fidelity life", "fidelity & guaranty", "fidelity natl",
    "f&g",
    "athene",
    "security benefit",
    "jackson national", "jackson",
    "ameritas",
    "assurity",
    "american equity",
    "midland national",
    "north american insured",
    
    # Multi-line Insurers (P&C + Life)
    "travelers", "travelers cos",
    "hartford", "hartford fire",
    "progressive", "progressive cas",
    "erie", "erie ins",
    "cincinnati", "cincinnati ins",
    "hanover", "hanover ins",
    "great american", "great amer",
    "old republic",
    "sentry", "sentry ins",
    "shelter", "shelter mut",
    "utica", "utica mut",
    "amerisure",
    "american modern",
    "crum & forster", "crum and forster",
    "swiss re",
    "everest",
    "arch capital", "arch ins",
    "w.r. berkley", "wr berkley", "berkley",
    "continental cas",
    "assurant",
    "selective ins",
    "donegal",
    "harleysville",
    "amica", "amica mut",
    "copperpoint",
    
    # Regional/Mutual Insurers
    "farm bureau", "kentucky farm bureau", "virginia farm bureau", "ohio farm bureau",
    "oklahoma farm", "indiana farmers", "ohio farmers",
    "country mutual", "country ins",
    "grange", "grange ins", "grange mut",
    "auto club", "aaa", "csaa",
    "alfa ins",
    "west bend",
    "secura",
    "church mutual",
    "federated mut",
    "employers mut",
    "frankenmuth",
    "quincy mut",
    "vermont mut",
    "celina mut",
    "central mut",
    "buckeye ins",
    "merchants mut",
    "rockingham",
    "hingham",
    "island ins",
    "nodak",
    "dakota",
    "new mexico mut",
    "louisiana workers",
    "maine employers",
    "missouri employers",
    "wisconsin cnty",
    "michigan farm bureau",
    "indiana farmers",
    "ohio mut",
    "atlantic charter",
    "merrimack mut",
    "norfolk & dedham", "norfolk and dedham",
    "hochheim prairie",
    "germania",
    "tuscarora wayne",
    "enumclaw",
    "cumberland",
    "midwest family",
    "midwest builders",
    "jewelers mut",
    "stonetrust",
    "accident fund",
    "builders ins", "builders mut",
    
    # Specialty/Niche Carriers
    "proassurance",
    "ncmic",
    "minnesota lawyers",
    "bar plan",
    "coverys",
    "medical mut",
    "james river",
    "sompo",
    "atain",
    "beazley",
    "hiscox",
    "starr",
    "axis",
    "allied world",
    "partner re",
    "odyssey",
    "intact",
    "fortegra",
    "ascot",
    "obsidian",
    "summit specialty",
    "palomar",
    "kin ins",
    "lemonade",
    "root ins",
    "tesla ins",
    "spinnaker",
    "vantage risk",
    "accelerant",
    "skyward",
    "technology ins",
    "hospitality ins",
    "integris",
    "adirondack",
    "goodville",
    "loudon mut",
    "omaha natl",
    "unique ins",
    "safeway ins",
    "loya ins",
    "first acceptance",
    "titan ins",
    "heritage ins",
    "geovera",
    "prime holdings",
    "safety first",
    "united prop",
    "mutual of wausau",
    "clear blue",
    "california ins", "california cas",
    "pacific specialty",
    "mendota",
    "benchmark ins",
    "american hallmark",
    "dtric",
    "imt ins",
    "national ind",
    "bituminous",
    "pure ins",
    "knightbrook",
    "greenwich ins",
    "harco",
    "retailfirst",
    "assurance amer",
    "fire districts",
    "valiant ins",
    "aspen",
    "greenville",
    "old american",
    "universal fire",
    "us coastal",
    "vault ins",
    "concert specialty",
    "lio ins",
    "national legacy",
    "pie ins",
    "granada",
    "sirius",
    
    # Canadian Carriers (often seen in border states)
    "manulife",
    "great west life",
    "sun life of canada",
    "canada life",
    "industrial alliance", "ia financial",
    "desjardins",
    
    # Broker/Agency Names (often confused with carriers)
    "northwestern",
    "new england life", "new england financial",
    "phoenix life",
    "penn life",
]

GUARANTEED_ISSUE_ONLY_COMPANIES = [
    "colonial penn",
    "colonialpenn",
    "globe life",
    "globelife",
]

GI_TRIGGER_PHRASES = [
    "guaranteed issue",
    "guaranteed acceptance",
    "no health questions",
    "no medical questions",
    "no exam",
    "final expense",
    "burial insurance",
    "acceptance guaranteed",
]

EMPLOYER_PLAN_PROVIDERS = [
    "metlife",
    "prudential",
    "lincoln financial",
    "cigna",
    "aetna",
    "unum",
    "hartford",
    "principal",
    "voya",
    "sun life",
    "guardian",
    "massmutual",
]

BUNDLED_POLICY_COMPANIES = [
    "state farm",
    "statefarm",
    "allstate",
    "farmers",
    "geico",
    "progressive",
    "liberty mutual",
    "libertymutual",
    "usaa",
    "nationwide",
    "american family",
    "amfam",
    "erie",
    "travelers",
    "hartford",
    "amica",
    "auto club",
    "aaa",
    "csaa",
    "country mutual",
    "country ins",
    "farm bureau",
    "shelter",
    "grange",
    "alfa",
    "west bend",
    "secura",
    "cincinnati",
    "hanover",
    "safeco",
    "esurance",
    "root",
    "lemonade",
]

import re

def normalize_company_name(name: str) -> str:
    """Normalize company name for matching."""
    return re.sub(r'[^a-z0-9]', '', name.lower())

def find_company_in_message(message: str) -> str | None:
    """
    Check if any known insurance company is mentioned in the message.
    Returns the company name if found, None otherwise.
    """
    msg_lower = message.lower()
    msg_normalized = normalize_company_name(message)
    
    for company in MAJOR_LIFE_INSURANCE_COMPANIES:
        if company in msg_lower:
            return company
        company_normalized = normalize_company_name(company)
        if company_normalized in msg_normalized:
            return company
    
    return None

def is_guaranteed_issue_company(company: str, message: str = "") -> bool:
    """
    Check if a company is EXCLUSIVELY a guaranteed issue provider.
    Only returns True for companies that ONLY sell GI products (Colonial Penn, Globe Life).
    For other carriers, requires corroborating GI phrases in the message.
    """
    company_lower = company.lower()
    msg_lower = message.lower()
    
    if any(gi in company_lower for gi in GUARANTEED_ISSUE_ONLY_COMPANIES):
        return True
    
    if any(phrase in msg_lower for phrase in GI_TRIGGER_PHRASES):
        return True
    
    return False

def is_bundled_policy_company(company: str) -> bool:
    """Check if a company typically bundles life with auto/home."""
    company_lower = company.lower()
    return any(bp in company_lower for bp in BUNDLED_POLICY_COMPANIES)

def get_company_context(company: str, message: str = "") -> dict:
    """
    Get context about a mentioned company to inform the response.
    Returns dict with flags about the company type.
    """
    company_lower = company.lower()
    return {
        "name": company,
        "is_guaranteed_issue": is_guaranteed_issue_company(company, message),
        "is_bundled": is_bundled_policy_company(company),
        "is_employer_provider": any(ep in company_lower for ep in EMPLOYER_PLAN_PROVIDERS),
    }
