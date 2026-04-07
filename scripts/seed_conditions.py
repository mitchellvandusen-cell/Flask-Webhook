#!/usr/bin/env python
"""Seed uw_conditions and uw_condition_questions tables.

50 medical conditions covering 90%+ of Final Expense / Term / Whole Life
underwriting encounters. Each condition includes its questionnaire — the
exact questions carriers ask on their applications.

Source: Carrier underwriting guides (Mutual of Omaha, American Amicable,
Americo, AIG, Foresters, Transamerica, etc.)

Usage:
    python scripts/seed_conditions.py
"""

import os
import sys
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_legacy import get_db_connection, return_db_connection


# ═══════════════════════════════════════════════════════════════
# CONDITIONS + QUESTIONNAIRES DATA
# ═══════════════════════════════════════════════════════════════
# Each entry: (name, slug, category, aliases[], severity_default, questions[])
# Questions: (question_text, question_type, options_json, required, help_text)

CONDITIONS = [
    # ── CARDIAC ──────────────────────────────────────────────
    {
        "name": "Heart Attack (Myocardial Infarction)",
        "slug": "heart_attack",
        "category": "cardiac",
        "aliases": ["heart attack", "MI", "myocardial infarction", "cardiac arrest"],
        "severity_default": "severe",
        "questions": [
            ("How long ago was the heart attack?", "single_choice", [
                {"value": "within_12mo", "label": "Within 12 months"},
                {"value": "1_2_years", "label": "1-2 years ago"},
                {"value": "2_5_years", "label": "2-5 years ago"},
                {"value": "5_plus_years", "label": "5+ years ago"},
            ], True, "Most carriers require at least 12 months since event"),
            ("Were any stents placed or bypass surgery performed?", "yes_no", None, True, None),
            ("Any additional cardiac events since then?", "yes_no", None, True, None),
            ("Currently on blood thinners?", "yes_no", None, False, "Warfarin, Eliquis, Xarelto, etc."),
        ],
    },
    {
        "name": "Congestive Heart Failure (CHF)",
        "slug": "congestive_heart_failure",
        "category": "cardiac",
        "aliases": ["CHF", "heart failure", "weak heart"],
        "severity_default": "severe",
        "questions": [
            ("How long ago was the diagnosis?", "single_choice", [
                {"value": "within_12mo", "label": "Within 12 months"},
                {"value": "1_2_years", "label": "1-2 years ago"},
                {"value": "2_plus_years", "label": "2+ years ago"},
            ], True, None),
            ("What is the current NYHA class?", "single_choice", [
                {"value": "class_1", "label": "Class I (no symptoms)"},
                {"value": "class_2", "label": "Class II (mild symptoms)"},
                {"value": "class_3", "label": "Class III (moderate symptoms)"},
                {"value": "class_4", "label": "Class IV (severe symptoms)"},
                {"value": "unknown", "label": "Unknown"},
            ], True, "New York Heart Association functional classification"),
            ("Currently hospitalized or on oxygen?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Atrial Fibrillation (AFib)",
        "slug": "atrial_fibrillation",
        "category": "cardiac",
        "aliases": ["AFib", "A-fib", "irregular heartbeat", "atrial fib"],
        "severity_default": "moderate",
        "questions": [
            ("How long ago was the diagnosis?", "single_choice", [
                {"value": "within_12mo", "label": "Within 12 months"},
                {"value": "1_2_years", "label": "1-2 years ago"},
                {"value": "2_plus_years", "label": "2+ years ago"},
            ], True, None),
            ("Is it controlled with medication?", "yes_no", None, True, None),
            ("Any stroke or TIA related to AFib?", "yes_no", None, True, None),
            ("Currently on blood thinners?", "yes_no", None, False, None),
        ],
    },
    {
        "name": "Coronary Artery Disease (CAD)",
        "slug": "coronary_artery_disease",
        "category": "cardiac",
        "aliases": ["CAD", "coronary disease", "clogged arteries", "blocked arteries"],
        "severity_default": "moderate",
        "questions": [
            ("How long ago was the diagnosis?", "single_choice", [
                {"value": "within_12mo", "label": "Within 12 months"},
                {"value": "1_2_years", "label": "1-2 years ago"},
                {"value": "2_5_years", "label": "2-5 years ago"},
                {"value": "5_plus_years", "label": "5+ years ago"},
            ], True, None),
            ("Any stents placed or bypass surgery?", "yes_no", None, True, None),
            ("Any heart attack associated with CAD?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "High Blood Pressure (Hypertension)",
        "slug": "hypertension",
        "category": "cardiac",
        "aliases": ["high blood pressure", "HBP", "hypertension", "elevated BP"],
        "severity_default": "mild",
        "questions": [
            ("Is it controlled with medication?", "yes_no", None, True, None),
            ("How many medications for blood pressure?", "single_choice", [
                {"value": "1", "label": "1 medication"},
                {"value": "2", "label": "2 medications"},
                {"value": "3_plus", "label": "3 or more medications"},
            ], True, None),
            ("Any complications (stroke, kidney disease)?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Peripheral Artery Disease (PAD)",
        "slug": "peripheral_artery_disease",
        "category": "cardiac",
        "aliases": ["PAD", "peripheral vascular disease", "PVD", "poor circulation"],
        "severity_default": "moderate",
        "questions": [
            ("How long ago was the diagnosis?", "single_choice", [
                {"value": "within_12mo", "label": "Within 12 months"},
                {"value": "1_2_years", "label": "1-2 years ago"},
                {"value": "2_plus_years", "label": "2+ years ago"},
            ], True, None),
            ("Any surgery or amputation?", "yes_no", None, True, None),
            ("Currently on medication?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Pacemaker / Defibrillator",
        "slug": "pacemaker_defibrillator",
        "category": "cardiac",
        "aliases": ["pacemaker", "defibrillator", "ICD", "implantable cardioverter"],
        "severity_default": "moderate",
        "questions": [
            ("Which device?", "single_choice", [
                {"value": "pacemaker", "label": "Pacemaker only"},
                {"value": "defibrillator", "label": "Defibrillator (ICD)"},
                {"value": "both", "label": "Both"},
            ], True, "Defibrillators are rated more severely than pacemakers"),
            ("How long ago was it implanted?", "single_choice", [
                {"value": "within_12mo", "label": "Within 12 months"},
                {"value": "1_2_years", "label": "1-2 years ago"},
                {"value": "2_plus_years", "label": "2+ years ago"},
            ], True, None),
            ("Has it ever fired/activated?", "yes_no", None, True, "For defibrillators only"),
        ],
    },

    # ── ENDOCRINE ────────────────────────────────────────────
    {
        "name": "Type 2 Diabetes",
        "slug": "type_2_diabetes",
        "category": "endocrine",
        "aliases": ["diabetes", "diabetic", "sugar diabetes", "type 2", "T2D", "adult onset diabetes"],
        "severity_default": "moderate",
        "questions": [
            ("What is the most recent A1C?", "single_choice", [
                {"value": "below_7", "label": "Below 7%"},
                {"value": "7_to_8", "label": "7% - 8%"},
                {"value": "8_to_9", "label": "8% - 9%"},
                {"value": "above_9", "label": "Above 9%"},
                {"value": "unknown", "label": "Unknown"},
            ], True, "A1C below 7% opens level benefit at most carriers"),
            ("Are they insulin dependent?", "yes_no", None, True, "Insulin use limits many carriers to graded benefit"),
            ("Any complications? (neuropathy, retinopathy, kidney disease)", "yes_no", None, True, None),
            ("How long ago was the diagnosis?", "single_choice", [
                {"value": "within_2_years", "label": "Within 2 years"},
                {"value": "2_5_years", "label": "2-5 years ago"},
                {"value": "5_plus_years", "label": "5+ years ago"},
            ], True, None),
        ],
    },
    {
        "name": "Type 1 Diabetes",
        "slug": "type_1_diabetes",
        "category": "endocrine",
        "aliases": ["juvenile diabetes", "type 1", "T1D", "insulin dependent diabetes"],
        "severity_default": "severe",
        "questions": [
            ("What is the most recent A1C?", "single_choice", [
                {"value": "below_7", "label": "Below 7%"},
                {"value": "7_to_8", "label": "7% - 8%"},
                {"value": "8_to_9", "label": "8% - 9%"},
                {"value": "above_9", "label": "Above 9%"},
            ], True, None),
            ("Any complications? (neuropathy, retinopathy, kidney disease)", "yes_no", None, True, None),
            ("Age at diagnosis?", "single_choice", [
                {"value": "under_18", "label": "Under 18"},
                {"value": "18_30", "label": "18-30"},
                {"value": "over_30", "label": "Over 30"},
            ], True, None),
        ],
    },

    # ── RESPIRATORY ──────────────────────────────────────────
    {
        "name": "COPD (Chronic Obstructive Pulmonary Disease)",
        "slug": "copd",
        "category": "respiratory",
        "aliases": ["COPD", "emphysema", "chronic bronchitis", "lung disease"],
        "severity_default": "moderate",
        "questions": [
            ("Currently on supplemental oxygen?", "yes_no", None, True, "Oxygen use typically limits to GI only"),
            ("Any hospitalizations for COPD in the past 2 years?", "yes_no", None, True, None),
            ("Current treatment?", "single_choice", [
                {"value": "inhaler_only", "label": "Inhaler only"},
                {"value": "nebulizer", "label": "Nebulizer treatments"},
                {"value": "oral_steroids", "label": "Oral steroids (Prednisone, etc.)"},
                {"value": "oxygen", "label": "Supplemental oxygen"},
            ], True, None),
        ],
    },
    {
        "name": "Asthma",
        "slug": "asthma",
        "category": "respiratory",
        "aliases": ["asthma", "reactive airway", "bronchial asthma"],
        "severity_default": "mild",
        "questions": [
            ("How well controlled?", "single_choice", [
                {"value": "well_controlled", "label": "Well controlled (rescue inhaler only)"},
                {"value": "daily_meds", "label": "Daily maintenance medication"},
                {"value": "poorly_controlled", "label": "Poorly controlled / frequent attacks"},
            ], True, None),
            ("Any hospitalizations for asthma in the past 2 years?", "yes_no", None, True, None),
            ("Currently on oral steroids?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Sleep Apnea",
        "slug": "sleep_apnea",
        "category": "respiratory",
        "aliases": ["sleep apnea", "OSA", "obstructive sleep apnea"],
        "severity_default": "mild",
        "questions": [
            ("Using a CPAP machine?", "yes_no", None, True, "CPAP compliance is favorable for underwriting"),
            ("Any related complications?", "yes_no", None, True, "AFib, hypertension, etc."),
        ],
    },
    {
        "name": "Pulmonary Fibrosis",
        "slug": "pulmonary_fibrosis",
        "category": "respiratory",
        "aliases": ["pulmonary fibrosis", "IPF", "lung fibrosis", "scarring of lungs"],
        "severity_default": "severe",
        "questions": [
            ("Currently on supplemental oxygen?", "yes_no", None, True, None),
            ("How long ago was the diagnosis?", "single_choice", [
                {"value": "within_2_years", "label": "Within 2 years"},
                {"value": "2_5_years", "label": "2-5 years ago"},
                {"value": "5_plus_years", "label": "5+ years ago"},
            ], True, None),
        ],
    },

    # ── CANCER ───────────────────────────────────────────────
    {
        "name": "Cancer (General)",
        "slug": "cancer_general",
        "category": "cancer",
        "aliases": ["cancer", "malignant tumor", "carcinoma", "oncology"],
        "severity_default": "severe",
        "questions": [
            ("What type of cancer?", "single_choice", [
                {"value": "breast", "label": "Breast"},
                {"value": "prostate", "label": "Prostate"},
                {"value": "colon", "label": "Colon/Colorectal"},
                {"value": "lung", "label": "Lung"},
                {"value": "skin_melanoma", "label": "Melanoma"},
                {"value": "skin_basal_squamous", "label": "Basal Cell / Squamous Cell (skin)"},
                {"value": "thyroid", "label": "Thyroid"},
                {"value": "bladder", "label": "Bladder"},
                {"value": "kidney", "label": "Kidney"},
                {"value": "lymphoma", "label": "Lymphoma"},
                {"value": "leukemia", "label": "Leukemia"},
                {"value": "pancreatic", "label": "Pancreatic"},
                {"value": "liver", "label": "Liver"},
                {"value": "other", "label": "Other"},
            ], True, "Basal cell/squamous cell skin cancer is rated favorably"),
            ("What stage was/is the cancer?", "single_choice", [
                {"value": "stage_0", "label": "Stage 0 (in situ)"},
                {"value": "stage_1", "label": "Stage I"},
                {"value": "stage_2", "label": "Stage II"},
                {"value": "stage_3", "label": "Stage III"},
                {"value": "stage_4", "label": "Stage IV"},
                {"value": "unknown", "label": "Unknown"},
            ], True, None),
            ("Current treatment status?", "single_choice", [
                {"value": "in_treatment", "label": "Currently in treatment"},
                {"value": "remission_under_2yr", "label": "In remission, less than 2 years"},
                {"value": "remission_2_5yr", "label": "In remission, 2-5 years"},
                {"value": "remission_5_plus", "label": "In remission, 5+ years"},
                {"value": "cured", "label": "Cured / no evidence of disease"},
            ], True, "Most carriers require 2+ years remission for level benefit"),
            ("Any metastasis (spread to other organs)?", "yes_no", None, True, None),
        ],
    },

    # ── NEUROLOGICAL ─────────────────────────────────────────
    {
        "name": "Stroke / TIA",
        "slug": "stroke_tia",
        "category": "neurological",
        "aliases": ["stroke", "CVA", "TIA", "mini stroke", "cerebrovascular accident", "transient ischemic attack"],
        "severity_default": "severe",
        "questions": [
            ("Was it a stroke or TIA (mini stroke)?", "single_choice", [
                {"value": "stroke", "label": "Full stroke (CVA)"},
                {"value": "tia", "label": "TIA (mini stroke)"},
            ], True, "TIA is rated less severely than full stroke"),
            ("How long ago?", "single_choice", [
                {"value": "within_12mo", "label": "Within 12 months"},
                {"value": "1_2_years", "label": "1-2 years ago"},
                {"value": "2_5_years", "label": "2-5 years ago"},
                {"value": "5_plus_years", "label": "5+ years ago"},
            ], True, None),
            ("Any residual effects (paralysis, speech issues)?", "yes_no", None, True, None),
            ("How many strokes/TIAs total?", "single_choice", [
                {"value": "1", "label": "1"},
                {"value": "2", "label": "2"},
                {"value": "3_plus", "label": "3 or more"},
            ], True, None),
        ],
    },
    {
        "name": "Epilepsy / Seizures",
        "slug": "epilepsy",
        "category": "neurological",
        "aliases": ["epilepsy", "seizures", "seizure disorder", "convulsions"],
        "severity_default": "moderate",
        "questions": [
            ("How well controlled?", "single_choice", [
                {"value": "no_seizures_2yr", "label": "No seizures in 2+ years"},
                {"value": "no_seizures_1yr", "label": "No seizures in 1-2 years"},
                {"value": "occasional", "label": "Occasional seizures (1-2/year)"},
                {"value": "frequent", "label": "Frequent seizures"},
            ], True, None),
            ("Currently on medication?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Parkinson's Disease",
        "slug": "parkinsons",
        "category": "neurological",
        "aliases": ["Parkinson's", "Parkinsons", "Parkinson disease"],
        "severity_default": "severe",
        "questions": [
            ("How long ago was the diagnosis?", "single_choice", [
                {"value": "within_2_years", "label": "Within 2 years"},
                {"value": "2_5_years", "label": "2-5 years ago"},
                {"value": "5_plus_years", "label": "5+ years ago"},
            ], True, None),
            ("Currently able to perform daily activities independently?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Alzheimer's / Dementia",
        "slug": "alzheimers_dementia",
        "category": "neurological",
        "aliases": ["Alzheimer's", "Alzheimers", "dementia", "memory loss", "cognitive decline"],
        "severity_default": "severe",
        "questions": [
            ("Formally diagnosed by a physician?", "yes_no", None, True, None),
            ("Currently able to perform daily activities (eating, bathing, dressing)?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Multiple Sclerosis (MS)",
        "slug": "multiple_sclerosis",
        "category": "neurological",
        "aliases": ["MS", "multiple sclerosis"],
        "severity_default": "severe",
        "questions": [
            ("How long ago was the diagnosis?", "single_choice", [
                {"value": "within_2_years", "label": "Within 2 years"},
                {"value": "2_5_years", "label": "2-5 years ago"},
                {"value": "5_plus_years", "label": "5+ years ago"},
            ], True, None),
            ("Currently able to perform daily activities independently?", "yes_no", None, True, None),
            ("Currently using a wheelchair or walker?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Neuropathy",
        "slug": "neuropathy",
        "category": "neurological",
        "aliases": ["neuropathy", "peripheral neuropathy", "nerve damage", "numbness in feet"],
        "severity_default": "mild",
        "questions": [
            ("What is the cause?", "single_choice", [
                {"value": "diabetes", "label": "Diabetes-related"},
                {"value": "idiopathic", "label": "Unknown / idiopathic"},
                {"value": "other", "label": "Other cause"},
            ], True, None),
            ("Affects daily activities?", "yes_no", None, True, None),
        ],
    },

    # ── RENAL ────────────────────────────────────────────────
    {
        "name": "Chronic Kidney Disease (CKD)",
        "slug": "chronic_kidney_disease",
        "category": "renal",
        "aliases": ["kidney disease", "CKD", "renal failure", "renal disease", "kidney failure"],
        "severity_default": "severe",
        "questions": [
            ("Current stage?", "single_choice", [
                {"value": "stage_1_2", "label": "Stage 1-2 (mild)"},
                {"value": "stage_3", "label": "Stage 3 (moderate)"},
                {"value": "stage_4", "label": "Stage 4 (severe)"},
                {"value": "stage_5", "label": "Stage 5 / End-stage (dialysis)"},
            ], True, None),
            ("Currently on dialysis?", "yes_no", None, True, "Dialysis typically limits to GI only"),
            ("Had a kidney transplant?", "yes_no", None, True, None),
        ],
    },

    # ── GASTROINTESTINAL ─────────────────────────────────────
    {
        "name": "Hepatitis C",
        "slug": "hepatitis_c",
        "category": "gastrointestinal",
        "aliases": ["Hep C", "hepatitis C", "HCV"],
        "severity_default": "moderate",
        "questions": [
            ("Current treatment status?", "single_choice", [
                {"value": "cured", "label": "Cured / SVR achieved"},
                {"value": "in_treatment", "label": "Currently in treatment"},
                {"value": "untreated", "label": "Not treated"},
            ], True, "SVR (sustained virologic response) = cured"),
            ("Any liver damage (cirrhosis, fibrosis)?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Hepatitis B",
        "slug": "hepatitis_b",
        "category": "gastrointestinal",
        "aliases": ["Hep B", "hepatitis B", "HBV"],
        "severity_default": "moderate",
        "questions": [
            ("Is it chronic or acute?", "single_choice", [
                {"value": "chronic", "label": "Chronic (carrier)"},
                {"value": "resolved", "label": "Resolved / cleared"},
            ], True, None),
            ("Any liver damage?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Cirrhosis",
        "slug": "cirrhosis",
        "category": "gastrointestinal",
        "aliases": ["cirrhosis", "liver cirrhosis", "liver scarring"],
        "severity_default": "severe",
        "questions": [
            ("What caused the cirrhosis?", "single_choice", [
                {"value": "alcohol", "label": "Alcohol-related"},
                {"value": "hepatitis", "label": "Hepatitis-related"},
                {"value": "nafld", "label": "Non-alcoholic fatty liver (NAFLD/NASH)"},
                {"value": "other", "label": "Other cause"},
            ], True, None),
            ("Any hospitalizations in the past 2 years?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Crohn's Disease / Ulcerative Colitis",
        "slug": "crohns_colitis",
        "category": "gastrointestinal",
        "aliases": ["Crohn's", "Crohns", "colitis", "ulcerative colitis", "IBD", "inflammatory bowel disease"],
        "severity_default": "moderate",
        "questions": [
            ("Currently in remission?", "yes_no", None, True, None),
            ("Any hospitalizations or surgery for this condition?", "yes_no", None, True, None),
            ("Currently on biologic medications (Remicade, Humira, etc.)?", "yes_no", None, False, None),
        ],
    },

    # ── MENTAL HEALTH ────────────────────────────────────────
    {
        "name": "Depression",
        "slug": "depression",
        "category": "mental_health",
        "aliases": ["depression", "major depression", "clinical depression", "MDD"],
        "severity_default": "mild",
        "questions": [
            ("Currently on medication?", "yes_no", None, True, None),
            ("Any hospitalizations for depression?", "yes_no", None, True, "Psychiatric hospitalization affects rating"),
            ("Any suicide attempts?", "yes_no", None, True, None),
            ("How many medications for mental health?", "single_choice", [
                {"value": "1", "label": "1 medication"},
                {"value": "2", "label": "2 medications"},
                {"value": "3_plus", "label": "3 or more"},
            ], True, None),
        ],
    },
    {
        "name": "Anxiety Disorder",
        "slug": "anxiety",
        "category": "mental_health",
        "aliases": ["anxiety", "GAD", "generalized anxiety", "panic disorder", "panic attacks"],
        "severity_default": "mild",
        "questions": [
            ("Currently on medication?", "yes_no", None, True, None),
            ("Any hospitalizations for anxiety?", "yes_no", None, True, None),
            ("Includes panic attacks?", "yes_no", None, False, None),
        ],
    },
    {
        "name": "Bipolar Disorder",
        "slug": "bipolar",
        "category": "mental_health",
        "aliases": ["bipolar", "manic depression", "bipolar disorder"],
        "severity_default": "moderate",
        "questions": [
            ("Currently on medication and stable?", "yes_no", None, True, None),
            ("Any hospitalizations in the past 2 years?", "yes_no", None, True, None),
            ("Any suicide attempts?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Schizophrenia",
        "slug": "schizophrenia",
        "category": "mental_health",
        "aliases": ["schizophrenia", "schizoaffective"],
        "severity_default": "severe",
        "questions": [
            ("Currently on medication and stable?", "yes_no", None, True, None),
            ("Any hospitalizations in the past 2 years?", "yes_no", None, True, None),
            ("Living independently?", "yes_no", None, True, None),
        ],
    },

    # ── SUBSTANCE USE ────────────────────────────────────────
    {
        "name": "Alcohol Abuse / Alcoholism",
        "slug": "alcohol_abuse",
        "category": "substance_use",
        "aliases": ["alcoholism", "alcohol abuse", "alcohol dependence", "drinking problem"],
        "severity_default": "moderate",
        "questions": [
            ("How long sober?", "single_choice", [
                {"value": "currently_drinking", "label": "Currently drinking"},
                {"value": "under_1yr", "label": "Less than 1 year sober"},
                {"value": "1_2_years", "label": "1-2 years sober"},
                {"value": "2_5_years", "label": "2-5 years sober"},
                {"value": "5_plus_years", "label": "5+ years sober"},
            ], True, "Most carriers require 2+ years sobriety for level"),
            ("Any DUIs in the past 5 years?", "single_choice", [
                {"value": "0", "label": "None"},
                {"value": "1", "label": "1 DUI"},
                {"value": "2_plus", "label": "2 or more"},
            ], True, None),
            ("Any treatment programs completed?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Drug Abuse / Addiction",
        "slug": "drug_abuse",
        "category": "substance_use",
        "aliases": ["drug abuse", "drug addiction", "substance abuse", "drug use", "narcotics"],
        "severity_default": "severe",
        "questions": [
            ("What substance(s)?", "single_choice", [
                {"value": "marijuana", "label": "Marijuana only"},
                {"value": "prescription", "label": "Prescription drug misuse"},
                {"value": "illegal", "label": "Illegal drugs (cocaine, heroin, meth, etc.)"},
                {"value": "multiple", "label": "Multiple substances"},
            ], True, None),
            ("How long clean/sober?", "single_choice", [
                {"value": "currently_using", "label": "Currently using"},
                {"value": "under_2yr", "label": "Less than 2 years"},
                {"value": "2_5_years", "label": "2-5 years"},
                {"value": "5_plus_years", "label": "5+ years"},
            ], True, None),
            ("Any treatment programs completed?", "yes_no", None, True, None),
        ],
    },

    # ── AUTOIMMUNE ───────────────────────────────────────────
    {
        "name": "Lupus (SLE)",
        "slug": "lupus",
        "category": "autoimmune",
        "aliases": ["lupus", "SLE", "systemic lupus"],
        "severity_default": "moderate",
        "questions": [
            ("Any organ involvement (kidney, heart, lung)?", "yes_no", None, True, None),
            ("Currently on immunosuppressive medications?", "yes_no", None, True, None),
            ("Any hospitalizations in the past 2 years?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Rheumatoid Arthritis",
        "slug": "rheumatoid_arthritis",
        "category": "autoimmune",
        "aliases": ["rheumatoid arthritis", "RA", "inflammatory arthritis"],
        "severity_default": "mild",
        "questions": [
            ("How well controlled?", "single_choice", [
                {"value": "well_controlled", "label": "Well controlled with medication"},
                {"value": "moderate", "label": "Moderately controlled"},
                {"value": "severe", "label": "Severe / disabling"},
            ], True, None),
            ("Currently on biologic medications (Humira, Enbrel, etc.)?", "yes_no", None, False, None),
        ],
    },

    # ── MUSCULOSKELETAL ──────────────────────────────────────
    {
        "name": "Osteoarthritis",
        "slug": "osteoarthritis",
        "category": "musculoskeletal",
        "aliases": ["osteoarthritis", "arthritis", "degenerative joint disease", "DJD", "joint pain"],
        "severity_default": "mild",
        "questions": [
            ("Affects daily activities or mobility?", "yes_no", None, True, None),
            ("Any joint replacement surgery?", "yes_no", None, False, None),
        ],
    },
    {
        "name": "Osteoporosis",
        "slug": "osteoporosis",
        "category": "musculoskeletal",
        "aliases": ["osteoporosis", "thin bones", "bone density loss"],
        "severity_default": "mild",
        "questions": [
            ("Any fractures in the past 2 years?", "yes_no", None, True, None),
            ("Currently on medication for osteoporosis?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Fibromyalgia",
        "slug": "fibromyalgia",
        "category": "musculoskeletal",
        "aliases": ["fibromyalgia", "fibro", "chronic pain syndrome"],
        "severity_default": "mild",
        "questions": [
            ("Currently on disability for this condition?", "yes_no", None, True, None),
            ("Currently on opioid pain medication?", "yes_no", None, True, None),
        ],
    },

    # ── GENERAL / OTHER ──────────────────────────────────────
    {
        "name": "Obesity / High BMI",
        "slug": "obesity",
        "category": "general",
        "aliases": ["obesity", "overweight", "high BMI", "morbid obesity"],
        "severity_default": "moderate",
        "questions": [
            ("What is the approximate BMI?", "single_choice", [
                {"value": "30_35", "label": "30-35 (Obese Class I)"},
                {"value": "35_40", "label": "35-40 (Obese Class II)"},
                {"value": "40_plus", "label": "40+ (Obese Class III / Morbid)"},
            ], True, "Most carriers have build charts with height/weight limits"),
            ("Any related conditions (diabetes, sleep apnea, heart disease)?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "High Cholesterol",
        "slug": "high_cholesterol",
        "category": "general",
        "aliases": ["high cholesterol", "hyperlipidemia", "elevated cholesterol"],
        "severity_default": "mild",
        "questions": [
            ("Controlled with medication?", "yes_no", None, True, None),
            ("Any cardiovascular complications?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Anemia",
        "slug": "anemia",
        "category": "general",
        "aliases": ["anemia", "low iron", "sickle cell", "iron deficiency"],
        "severity_default": "mild",
        "questions": [
            ("What type?", "single_choice", [
                {"value": "iron_deficiency", "label": "Iron deficiency anemia"},
                {"value": "sickle_cell_trait", "label": "Sickle cell trait"},
                {"value": "sickle_cell_disease", "label": "Sickle cell disease"},
                {"value": "other", "label": "Other type"},
            ], True, "Sickle cell trait is rated differently from disease"),
            ("Currently being treated?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "HIV / AIDS",
        "slug": "hiv_aids",
        "category": "general",
        "aliases": ["HIV", "AIDS", "HIV positive"],
        "severity_default": "severe",
        "questions": [
            ("What is the current status?", "single_choice", [
                {"value": "hiv_controlled", "label": "HIV, viral load undetectable"},
                {"value": "hiv_detectable", "label": "HIV, detectable viral load"},
                {"value": "aids", "label": "AIDS diagnosis"},
            ], True, None),
            ("On antiretroviral therapy (ART)?", "yes_no", None, True, None),
            ("Current CD4 count?", "single_choice", [
                {"value": "above_500", "label": "Above 500"},
                {"value": "200_500", "label": "200-500"},
                {"value": "below_200", "label": "Below 200"},
                {"value": "unknown", "label": "Unknown"},
            ], True, None),
        ],
    },
    {
        "name": "Organ Transplant",
        "slug": "organ_transplant",
        "category": "general",
        "aliases": ["transplant", "organ transplant", "kidney transplant", "liver transplant", "heart transplant"],
        "severity_default": "severe",
        "questions": [
            ("Which organ was transplanted?", "single_choice", [
                {"value": "kidney", "label": "Kidney"},
                {"value": "liver", "label": "Liver"},
                {"value": "heart", "label": "Heart"},
                {"value": "lung", "label": "Lung"},
                {"value": "bone_marrow", "label": "Bone marrow / stem cell"},
                {"value": "other", "label": "Other"},
            ], True, None),
            ("How long ago?", "single_choice", [
                {"value": "within_2_years", "label": "Within 2 years"},
                {"value": "2_5_years", "label": "2-5 years ago"},
                {"value": "5_plus_years", "label": "5+ years ago"},
            ], True, None),
            ("Any rejection episodes?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Amputation",
        "slug": "amputation",
        "category": "general",
        "aliases": ["amputation", "amputee", "lost limb"],
        "severity_default": "moderate",
        "questions": [
            ("What was the cause?", "single_choice", [
                {"value": "diabetes", "label": "Diabetes-related"},
                {"value": "injury", "label": "Accident / injury"},
                {"value": "vascular", "label": "Vascular disease"},
                {"value": "other", "label": "Other"},
            ], True, None),
            ("What was amputated?", "single_choice", [
                {"value": "finger_toe", "label": "Finger(s) or toe(s)"},
                {"value": "foot", "label": "Foot"},
                {"value": "below_knee", "label": "Below the knee"},
                {"value": "above_knee", "label": "Above the knee"},
                {"value": "arm_hand", "label": "Arm or hand"},
            ], True, None),
        ],
    },
    {
        "name": "Wheelchair / Mobility Aid",
        "slug": "wheelchair",
        "category": "general",
        "aliases": ["wheelchair", "walker", "mobility aid", "bedridden", "confined"],
        "severity_default": "severe",
        "questions": [
            ("What type of mobility aid?", "single_choice", [
                {"value": "cane", "label": "Cane"},
                {"value": "walker", "label": "Walker"},
                {"value": "wheelchair", "label": "Wheelchair"},
                {"value": "bedridden", "label": "Bedridden"},
            ], True, None),
            ("What is the underlying cause?", "single_choice", [
                {"value": "injury", "label": "Injury / accident"},
                {"value": "neurological", "label": "Neurological condition"},
                {"value": "arthritis", "label": "Arthritis"},
                {"value": "other", "label": "Other"},
            ], True, None),
        ],
    },
    {
        "name": "Activities of Daily Living (ADL) Limitations",
        "slug": "adl_limitations",
        "category": "general",
        "aliases": ["ADL", "can't bathe", "can't dress", "needs help", "assisted living", "nursing home"],
        "severity_default": "severe",
        "questions": [
            ("Which ADLs need assistance?", "multi_choice", [
                {"value": "bathing", "label": "Bathing"},
                {"value": "dressing", "label": "Dressing"},
                {"value": "eating", "label": "Eating"},
                {"value": "toileting", "label": "Toileting"},
                {"value": "transferring", "label": "Transferring (bed to chair)"},
                {"value": "continence", "label": "Continence"},
            ], True, "Most carriers decline if 2+ ADLs need assistance"),
            ("Currently in a nursing facility or assisted living?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Thyroid Disorder",
        "slug": "thyroid",
        "category": "endocrine",
        "aliases": ["thyroid", "hypothyroid", "hyperthyroid", "Hashimoto's", "Graves disease", "thyroid disease"],
        "severity_default": "mild",
        "questions": [
            ("What type?", "single_choice", [
                {"value": "hypothyroid", "label": "Hypothyroid (underactive)"},
                {"value": "hyperthyroid", "label": "Hyperthyroid (overactive / Graves')"},
                {"value": "thyroid_cancer", "label": "Thyroid cancer (current or history)"},
                {"value": "nodules", "label": "Thyroid nodules"},
            ], True, None),
            ("Controlled with medication?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "Deep Vein Thrombosis (DVT) / Blood Clots",
        "slug": "dvt_blood_clots",
        "category": "cardiac",
        "aliases": ["DVT", "blood clot", "deep vein thrombosis", "pulmonary embolism", "PE"],
        "severity_default": "moderate",
        "questions": [
            ("How long ago?", "single_choice", [
                {"value": "within_12mo", "label": "Within 12 months"},
                {"value": "1_2_years", "label": "1-2 years ago"},
                {"value": "2_plus_years", "label": "2+ years ago"},
            ], True, None),
            ("Was it a DVT or pulmonary embolism (PE)?", "single_choice", [
                {"value": "dvt", "label": "DVT (leg clot)"},
                {"value": "pe", "label": "Pulmonary embolism (lung clot)"},
                {"value": "both", "label": "Both"},
            ], True, "PE is rated more severely"),
            ("Currently on blood thinners?", "yes_no", None, True, None),
            ("Was it provoked (surgery, injury, travel)?", "single_choice", [
                {"value": "provoked", "label": "Yes, provoked (clear cause)"},
                {"value": "unprovoked", "label": "No, unprovoked"},
            ], True, "Unprovoked clots suggest underlying condition"),
        ],
    },
    {
        "name": "Gout",
        "slug": "gout",
        "category": "musculoskeletal",
        "aliases": ["gout", "gouty arthritis"],
        "severity_default": "mild",
        "questions": [
            ("How often do flare-ups occur?", "single_choice", [
                {"value": "rare", "label": "Rare (1-2 per year)"},
                {"value": "moderate", "label": "Moderate (3-6 per year)"},
                {"value": "frequent", "label": "Frequent (monthly or more)"},
            ], True, None),
            ("Currently on daily medication (Allopurinol, etc.)?", "yes_no", None, True, None),
        ],
    },
    {
        "name": "GERD / Acid Reflux",
        "slug": "gerd",
        "category": "gastrointestinal",
        "aliases": ["GERD", "acid reflux", "heartburn", "reflux"],
        "severity_default": "mild",
        "questions": [
            ("Controlled with medication?", "yes_no", None, True, None),
            ("Any complications (Barrett's esophagus)?", "yes_no", None, True, "Barrett's increases cancer risk"),
        ],
    },
    {
        "name": "Kidney Stones",
        "slug": "kidney_stones",
        "category": "renal",
        "aliases": ["kidney stones", "renal calculi", "nephrolithiasis"],
        "severity_default": "mild",
        "questions": [
            ("How many episodes?", "single_choice", [
                {"value": "1", "label": "1 episode"},
                {"value": "2_3", "label": "2-3 episodes"},
                {"value": "chronic", "label": "Chronic / recurring"},
            ], True, None),
            ("Any surgery required?", "yes_no", None, False, None),
        ],
    },
]


def seed():
    """Insert all conditions and their questionnaires."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Check if already seeded
            cur.execute("SELECT COUNT(*) AS cnt FROM uw_conditions")
            existing = cur.fetchone()["cnt"]
            if existing > 0:
                print(f"uw_conditions already has {existing} rows. Skipping seed.")
                print("To re-seed, run: DELETE FROM uw_condition_questions; DELETE FROM uw_conditions;")
                return

            for cond in CONDITIONS:
                # Insert condition
                cur.execute("""
                    INSERT INTO uw_conditions (name, slug, category, aliases, severity_default, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (slug) DO NOTHING
                    RETURNING id
                """, (
                    cond["name"],
                    cond["slug"],
                    cond["category"],
                    cond["aliases"],
                    cond["severity_default"],
                    None,
                ))
                row = cur.fetchone()
                if not row:
                    # Already exists, get the id
                    cur.execute("SELECT id FROM uw_conditions WHERE slug = %s", (cond["slug"],))
                    row = cur.fetchone()
                condition_id = row["id"]

                # Insert questions
                for i, q in enumerate(cond["questions"]):
                    question_text, question_type, options, required, help_text = q
                    cur.execute("""
                        INSERT INTO uw_condition_questions
                            (condition_id, sort_order, question_text, question_type, options, required, help_text)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        condition_id,
                        i,
                        question_text,
                        question_type,
                        json.dumps(options) if options else None,
                        required,
                        help_text,
                    ))

            conn.commit()
            print(f"Seeded {len(CONDITIONS)} conditions with questionnaires.")

    except Exception as e:
        conn.rollback()
        print(f"Error seeding conditions: {e}")
        raise
    finally:
        return_db_connection(conn)


if __name__ == "__main__":
    seed()
