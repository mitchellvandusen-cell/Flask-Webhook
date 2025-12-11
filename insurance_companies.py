"""
Comprehensive list of US Life Insurance Companies for validation.
Used to detect when a lead mentions a specific company name.
"""

MAJOR_LIFE_INSURANCE_COMPANIES = [
    "northwestern mutual",
    "metlife", "metropolitan", "met life",
    "new york life", "newyork life", "ny life",
    "prudential",
    "massmutual", "mass mutual",
    "lincoln national", "lincoln financial",
    "nationwide",
    "state farm", "statefarm",
    "aig", "american international",
    "guardian", "guardian life",
    "globe life", "globelife",
    "principal", "principal financial",
    "equitable", "equitable holdings",
    "tiaa",
    "pacific life", "pacificlife",
    "thrivent",
    "usaa",
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
    "farmers",
    "liberty mutual", "libertymutual",
    "american general", "aig life",
    "voya",
    "unum",
    "cigna",
    "aetna",
    "humana",
    "anthem",
    "primerica",
    "foresters",
    "legal and general", "legal & general", "lgamerica",
    "zurich",
    "allianz",
    "cuna mutual", "cunamutual",
    "american family", "amfam",
    "nationwide",
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
    "fidelity life", "fidelity & guaranty",
    "f&g",
    "athene",
    "security benefit",
    "jackson national", "jackson",
    "ameritas",
    "assurity",
    "american equity",
    "midland national",
    "north american insured",
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
