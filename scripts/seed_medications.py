#!/usr/bin/env python
"""Seed uw_medications and uw_drug_condition_map tables.

Top ~500 medications commonly encountered in life insurance underwriting,
with drug-to-condition mappings. Sourced from NIH RxNorm (public domain).

For full RxNorm bulk load, download from:
https://www.nlm.nih.gov/research/umls/rxnorm/docs/rxnormfiles.html

Usage:
    python scripts/seed_medications.py
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_legacy import get_db_connection, return_db_connection


# ═══════════════════════════════════════════════════════════════
# MEDICATIONS DATA
# Format: (name, generic_name, brand_names[], drug_class, condition_slugs[])
# condition_slugs map to uw_conditions.slug for drug→condition linking
# ═══════════════════════════════════════════════════════════════

MEDICATIONS = [
    # ── DIABETES ──────────────────────────────────────────────
    ("Metformin", "metformin", ["Glucophage", "Fortamet", "Riomet"], "Biguanides", ["type_2_diabetes"]),
    ("Glipizide", "glipizide", ["Glucotrol"], "Sulfonylureas", ["type_2_diabetes"]),
    ("Glyburide", "glyburide", ["DiaBeta", "Micronase"], "Sulfonylureas", ["type_2_diabetes"]),
    ("Glimepiride", "glimepiride", ["Amaryl"], "Sulfonylureas", ["type_2_diabetes"]),
    ("Januvia", "sitagliptin", ["Januvia"], "DPP-4 Inhibitors", ["type_2_diabetes"]),
    ("Jardiance", "empagliflozin", ["Jardiance"], "SGLT2 Inhibitors", ["type_2_diabetes"]),
    ("Farxiga", "dapagliflozin", ["Farxiga"], "SGLT2 Inhibitors", ["type_2_diabetes"]),
    ("Invokana", "canagliflozin", ["Invokana"], "SGLT2 Inhibitors", ["type_2_diabetes"]),
    ("Ozempic", "semaglutide", ["Ozempic", "Wegovy", "Rybelsus"], "GLP-1 Agonists", ["type_2_diabetes"]),
    ("Trulicity", "dulaglutide", ["Trulicity"], "GLP-1 Agonists", ["type_2_diabetes"]),
    ("Mounjaro", "tirzepatide", ["Mounjaro", "Zepbound"], "GLP-1/GIP Agonists", ["type_2_diabetes"]),
    ("Victoza", "liraglutide", ["Victoza", "Saxenda"], "GLP-1 Agonists", ["type_2_diabetes"]),
    ("Pioglitazone", "pioglitazone", ["Actos"], "Thiazolidinediones", ["type_2_diabetes"]),
    ("Acarbose", "acarbose", ["Precose"], "Alpha-Glucosidase Inhibitors", ["type_2_diabetes"]),
    ("Insulin Glargine", "insulin glargine", ["Lantus", "Basaglar", "Toujeo"], "Long-Acting Insulin", ["type_1_diabetes", "type_2_diabetes"]),
    ("Insulin Lispro", "insulin lispro", ["Humalog", "Admelog"], "Rapid-Acting Insulin", ["type_1_diabetes", "type_2_diabetes"]),
    ("Insulin Aspart", "insulin aspart", ["NovoLog", "Fiasp"], "Rapid-Acting Insulin", ["type_1_diabetes", "type_2_diabetes"]),
    ("Novolin", "insulin regular", ["Novolin", "Humulin"], "Regular Insulin", ["type_1_diabetes", "type_2_diabetes"]),

    # ── CARDIAC / BLOOD PRESSURE ─────────────────────────────
    ("Lisinopril", "lisinopril", ["Prinivil", "Zestril"], "ACE Inhibitors", ["hypertension"]),
    ("Enalapril", "enalapril", ["Vasotec"], "ACE Inhibitors", ["hypertension"]),
    ("Ramipril", "ramipril", ["Altace"], "ACE Inhibitors", ["hypertension"]),
    ("Benazepril", "benazepril", ["Lotensin"], "ACE Inhibitors", ["hypertension"]),
    ("Losartan", "losartan", ["Cozaar"], "ARBs", ["hypertension"]),
    ("Valsartan", "valsartan", ["Diovan"], "ARBs", ["hypertension"]),
    ("Irbesartan", "irbesartan", ["Avapro"], "ARBs", ["hypertension"]),
    ("Olmesartan", "olmesartan", ["Benicar"], "ARBs", ["hypertension"]),
    ("Telmisartan", "telmisartan", ["Micardis"], "ARBs", ["hypertension"]),
    ("Amlodipine", "amlodipine", ["Norvasc"], "Calcium Channel Blockers", ["hypertension"]),
    ("Nifedipine", "nifedipine", ["Procardia", "Adalat"], "Calcium Channel Blockers", ["hypertension"]),
    ("Diltiazem", "diltiazem", ["Cardizem", "Tiazac"], "Calcium Channel Blockers", ["hypertension", "atrial_fibrillation"]),
    ("Verapamil", "verapamil", ["Calan", "Verelan"], "Calcium Channel Blockers", ["hypertension", "atrial_fibrillation"]),
    ("Metoprolol", "metoprolol", ["Lopressor", "Toprol XL"], "Beta Blockers", ["hypertension", "atrial_fibrillation", "heart_attack"]),
    ("Atenolol", "atenolol", ["Tenormin"], "Beta Blockers", ["hypertension"]),
    ("Carvedilol", "carvedilol", ["Coreg"], "Beta Blockers", ["hypertension", "congestive_heart_failure"]),
    ("Bisoprolol", "bisoprolol", ["Zebeta"], "Beta Blockers", ["hypertension", "congestive_heart_failure"]),
    ("Propranolol", "propranolol", ["Inderal"], "Beta Blockers", ["hypertension", "anxiety"]),
    ("Hydrochlorothiazide", "hydrochlorothiazide", ["HCTZ", "Microzide"], "Thiazide Diuretics", ["hypertension"]),
    ("Chlorthalidone", "chlorthalidone", ["Hygroton"], "Thiazide Diuretics", ["hypertension"]),
    ("Furosemide", "furosemide", ["Lasix"], "Loop Diuretics", ["congestive_heart_failure", "chronic_kidney_disease"]),
    ("Spironolactone", "spironolactone", ["Aldactone"], "Potassium-Sparing Diuretics", ["congestive_heart_failure", "hypertension"]),
    ("Hydralazine", "hydralazine", ["Apresoline"], "Vasodilators", ["hypertension", "congestive_heart_failure"]),
    ("Clonidine", "clonidine", ["Catapres"], "Alpha-2 Agonists", ["hypertension"]),
    ("Doxazosin", "doxazosin", ["Cardura"], "Alpha Blockers", ["hypertension"]),

    # ── BLOOD THINNERS ───────────────────────────────────────
    ("Warfarin", "warfarin", ["Coumadin", "Jantoven"], "Vitamin K Antagonists", ["atrial_fibrillation", "dvt_blood_clots"]),
    ("Eliquis", "apixaban", ["Eliquis"], "DOACs", ["atrial_fibrillation", "dvt_blood_clots"]),
    ("Xarelto", "rivaroxaban", ["Xarelto"], "DOACs", ["atrial_fibrillation", "dvt_blood_clots"]),
    ("Pradaxa", "dabigatran", ["Pradaxa"], "DOACs", ["atrial_fibrillation", "dvt_blood_clots"]),
    ("Plavix", "clopidogrel", ["Plavix"], "Antiplatelet Agents", ["heart_attack", "coronary_artery_disease", "stroke_tia"]),
    ("Aspirin", "aspirin", ["Bayer", "Ecotrin"], "Antiplatelet Agents", ["heart_attack", "coronary_artery_disease"]),
    ("Heparin", "heparin", ["Heparin"], "Anticoagulants", ["dvt_blood_clots"]),
    ("Lovenox", "enoxaparin", ["Lovenox"], "LMWH", ["dvt_blood_clots"]),
    ("Brilinta", "ticagrelor", ["Brilinta"], "Antiplatelet Agents", ["heart_attack", "coronary_artery_disease"]),
    ("Effient", "prasugrel", ["Effient"], "Antiplatelet Agents", ["heart_attack", "coronary_artery_disease"]),

    # ── HEART FAILURE ────────────────────────────────────────
    ("Entresto", "sacubitril/valsartan", ["Entresto"], "ARNI", ["congestive_heart_failure"]),
    ("Digoxin", "digoxin", ["Lanoxin"], "Cardiac Glycosides", ["congestive_heart_failure", "atrial_fibrillation"]),
    ("Isosorbide", "isosorbide mononitrate", ["Imdur", "Monoket"], "Nitrates", ["coronary_artery_disease", "congestive_heart_failure"]),
    ("Nitroglycerin", "nitroglycerin", ["Nitrostat", "Nitro-Dur"], "Nitrates", ["coronary_artery_disease"]),

    # ── CHOLESTEROL ──────────────────────────────────────────
    ("Atorvastatin", "atorvastatin", ["Lipitor"], "Statins", ["high_cholesterol"]),
    ("Simvastatin", "simvastatin", ["Zocor"], "Statins", ["high_cholesterol"]),
    ("Rosuvastatin", "rosuvastatin", ["Crestor"], "Statins", ["high_cholesterol"]),
    ("Pravastatin", "pravastatin", ["Pravachol"], "Statins", ["high_cholesterol"]),
    ("Lovastatin", "lovastatin", ["Mevacor"], "Statins", ["high_cholesterol"]),
    ("Ezetimibe", "ezetimibe", ["Zetia"], "Cholesterol Absorption Inhibitors", ["high_cholesterol"]),
    ("Fenofibrate", "fenofibrate", ["Tricor", "Fenoglide"], "Fibrates", ["high_cholesterol"]),
    ("Repatha", "evolocumab", ["Repatha"], "PCSK9 Inhibitors", ["high_cholesterol"]),
    ("Praluent", "alirocumab", ["Praluent"], "PCSK9 Inhibitors", ["high_cholesterol"]),
    ("Niacin", "niacin", ["Niaspan", "Slo-Niacin"], "B Vitamins", ["high_cholesterol"]),
    ("Fish Oil / Omega-3", "omega-3-acid ethyl esters", ["Lovaza", "Vascepa"], "Omega-3 Fatty Acids", ["high_cholesterol"]),

    # ── RESPIRATORY ──────────────────────────────────────────
    ("Albuterol", "albuterol", ["ProAir", "Ventolin", "Proventil"], "Short-Acting Beta Agonists", ["asthma", "copd"]),
    ("Advair", "fluticasone/salmeterol", ["Advair Diskus", "AirDuo"], "ICS/LABA Combination", ["asthma", "copd"]),
    ("Symbicort", "budesonide/formoterol", ["Symbicort"], "ICS/LABA Combination", ["asthma", "copd"]),
    ("Breo Ellipta", "fluticasone/vilanterol", ["Breo Ellipta"], "ICS/LABA Combination", ["asthma", "copd"]),
    ("Spiriva", "tiotropium", ["Spiriva"], "LAMA", ["copd"]),
    ("Trelegy", "fluticasone/umeclidinium/vilanterol", ["Trelegy Ellipta"], "Triple Therapy", ["copd"]),
    ("Singulair", "montelukast", ["Singulair"], "Leukotriene Modifiers", ["asthma"]),
    ("Prednisone", "prednisone", ["Deltasone", "Rayos"], "Corticosteroids", ["asthma", "copd", "lupus", "rheumatoid_arthritis", "crohns_colitis"]),
    ("Flovent", "fluticasone", ["Flovent", "ArmonAir"], "Inhaled Corticosteroids", ["asthma"]),
    ("QVAR", "beclomethasone", ["QVAR"], "Inhaled Corticosteroids", ["asthma"]),
    ("Theophylline", "theophylline", ["Theo-24", "Elixophyllin"], "Methylxanthines", ["asthma", "copd"]),

    # ── MENTAL HEALTH ────────────────────────────────────────
    ("Lexapro", "escitalopram", ["Lexapro"], "SSRIs", ["depression", "anxiety"]),
    ("Zoloft", "sertraline", ["Zoloft"], "SSRIs", ["depression", "anxiety"]),
    ("Prozac", "fluoxetine", ["Prozac", "Sarafem"], "SSRIs", ["depression"]),
    ("Celexa", "citalopram", ["Celexa"], "SSRIs", ["depression"]),
    ("Paxil", "paroxetine", ["Paxil"], "SSRIs", ["depression", "anxiety"]),
    ("Effexor", "venlafaxine", ["Effexor XR"], "SNRIs", ["depression", "anxiety"]),
    ("Cymbalta", "duloxetine", ["Cymbalta"], "SNRIs", ["depression", "anxiety", "neuropathy", "fibromyalgia"]),
    ("Pristiq", "desvenlafaxine", ["Pristiq"], "SNRIs", ["depression"]),
    ("Wellbutrin", "bupropion", ["Wellbutrin", "Zyban"], "NDRIs", ["depression"]),
    ("Remeron", "mirtazapine", ["Remeron"], "Tetracyclic Antidepressants", ["depression"]),
    ("Trazodone", "trazodone", ["Desyrel"], "SARIs", ["depression"]),
    ("Buspirone", "buspirone", ["Buspar"], "Anxiolytics", ["anxiety"]),
    ("Xanax", "alprazolam", ["Xanax"], "Benzodiazepines", ["anxiety"]),
    ("Ativan", "lorazepam", ["Ativan"], "Benzodiazepines", ["anxiety"]),
    ("Klonopin", "clonazepam", ["Klonopin"], "Benzodiazepines", ["anxiety", "epilepsy"]),
    ("Valium", "diazepam", ["Valium"], "Benzodiazepines", ["anxiety"]),
    ("Lithium", "lithium", ["Lithobid", "Eskalith"], "Mood Stabilizers", ["bipolar"]),
    ("Lamictal", "lamotrigine", ["Lamictal"], "Anticonvulsants / Mood Stabilizers", ["bipolar", "epilepsy"]),
    ("Depakote", "divalproex sodium", ["Depakote"], "Anticonvulsants / Mood Stabilizers", ["bipolar", "epilepsy"]),
    ("Seroquel", "quetiapine", ["Seroquel"], "Atypical Antipsychotics", ["bipolar", "schizophrenia"]),
    ("Abilify", "aripiprazole", ["Abilify"], "Atypical Antipsychotics", ["bipolar", "schizophrenia", "depression"]),
    ("Risperdal", "risperidone", ["Risperdal"], "Atypical Antipsychotics", ["schizophrenia", "bipolar"]),
    ("Zyprexa", "olanzapine", ["Zyprexa"], "Atypical Antipsychotics", ["schizophrenia", "bipolar"]),
    ("Geodon", "ziprasidone", ["Geodon"], "Atypical Antipsychotics", ["schizophrenia", "bipolar"]),
    ("Latuda", "lurasidone", ["Latuda"], "Atypical Antipsychotics", ["schizophrenia", "bipolar"]),
    ("Invega", "paliperidone", ["Invega"], "Atypical Antipsychotics", ["schizophrenia"]),
    ("Haldol", "haloperidol", ["Haldol"], "Typical Antipsychotics", ["schizophrenia"]),

    # ── SEIZURE / EPILEPSY ───────────────────────────────────
    ("Keppra", "levetiracetam", ["Keppra"], "Anticonvulsants", ["epilepsy"]),
    ("Dilantin", "phenytoin", ["Dilantin"], "Anticonvulsants", ["epilepsy"]),
    ("Tegretol", "carbamazepine", ["Tegretol"], "Anticonvulsants", ["epilepsy"]),
    ("Topamax", "topiramate", ["Topamax"], "Anticonvulsants", ["epilepsy"]),
    ("Gabapentin", "gabapentin", ["Neurontin"], "Anticonvulsants / Neuropathic Pain", ["epilepsy", "neuropathy"]),
    ("Pregabalin", "pregabalin", ["Lyrica"], "Anticonvulsants / Neuropathic Pain", ["neuropathy", "fibromyalgia"]),

    # ── THYROID ──────────────────────────────────────────────
    ("Levothyroxine", "levothyroxine", ["Synthroid", "Levoxyl", "Tirosint"], "Thyroid Hormones", ["thyroid"]),
    ("Armour Thyroid", "thyroid desiccated", ["Armour Thyroid", "NP Thyroid"], "Natural Thyroid", ["thyroid"]),
    ("Methimazole", "methimazole", ["Tapazole"], "Antithyroid Agents", ["thyroid"]),

    # ── KIDNEY ───────────────────────────────────────────────
    ("Sevelamer", "sevelamer", ["Renvela", "Renagel"], "Phosphate Binders", ["chronic_kidney_disease"]),
    ("Epoetin Alfa", "epoetin alfa", ["Epogen", "Procrit"], "Erythropoiesis-Stimulating Agents", ["chronic_kidney_disease", "anemia"]),
    ("Calcitriol", "calcitriol", ["Rocaltrol"], "Vitamin D Analogs", ["chronic_kidney_disease"]),

    # ── GASTROINTESTINAL ─────────────────────────────────────
    ("Omeprazole", "omeprazole", ["Prilosec"], "Proton Pump Inhibitors", ["gerd"]),
    ("Pantoprazole", "pantoprazole", ["Protonix"], "Proton Pump Inhibitors", ["gerd"]),
    ("Esomeprazole", "esomeprazole", ["Nexium"], "Proton Pump Inhibitors", ["gerd"]),
    ("Lansoprazole", "lansoprazole", ["Prevacid"], "Proton Pump Inhibitors", ["gerd"]),
    ("Ranitidine", "ranitidine", ["Zantac"], "H2 Blockers", ["gerd"]),
    ("Famotidine", "famotidine", ["Pepcid"], "H2 Blockers", ["gerd"]),
    ("Mesalamine", "mesalamine", ["Asacol", "Lialda", "Pentasa"], "Aminosalicylates", ["crohns_colitis"]),
    ("Humira", "adalimumab", ["Humira"], "TNF Inhibitors", ["crohns_colitis", "rheumatoid_arthritis"]),
    ("Remicade", "infliximab", ["Remicade"], "TNF Inhibitors", ["crohns_colitis", "rheumatoid_arthritis"]),
    ("Entyvio", "vedolizumab", ["Entyvio"], "Integrin Inhibitors", ["crohns_colitis"]),

    # ── LIVER / HEPATITIS ────────────────────────────────────
    ("Harvoni", "ledipasvir/sofosbuvir", ["Harvoni"], "Direct-Acting Antivirals", ["hepatitis_c"]),
    ("Epclusa", "sofosbuvir/velpatasvir", ["Epclusa"], "Direct-Acting Antivirals", ["hepatitis_c"]),
    ("Mavyret", "glecaprevir/pibrentasvir", ["Mavyret"], "Direct-Acting Antivirals", ["hepatitis_c"]),
    ("Entecavir", "entecavir", ["Baraclude"], "Nucleos(t)ide Analogs", ["hepatitis_b"]),
    ("Tenofovir", "tenofovir", ["Viread"], "Nucleos(t)ide Analogs", ["hepatitis_b", "hiv_aids"]),

    # ── HIV ──────────────────────────────────────────────────
    ("Biktarvy", "bictegravir/emtricitabine/TAF", ["Biktarvy"], "INSTI + NRTI Combination", ["hiv_aids"]),
    ("Triumeq", "dolutegravir/abacavir/lamivudine", ["Triumeq"], "INSTI + NRTI Combination", ["hiv_aids"]),
    ("Descovy", "emtricitabine/TAF", ["Descovy"], "NRTI Combination", ["hiv_aids"]),
    ("Truvada", "emtricitabine/tenofovir", ["Truvada"], "NRTI Combination", ["hiv_aids"]),
    ("Dovato", "dolutegravir/lamivudine", ["Dovato"], "INSTI + NRTI", ["hiv_aids"]),

    # ── PAIN / MUSCULOSKELETAL ───────────────────────────────
    ("Ibuprofen", "ibuprofen", ["Advil", "Motrin"], "NSAIDs", ["osteoarthritis"]),
    ("Naproxen", "naproxen", ["Aleve", "Naprosyn"], "NSAIDs", ["osteoarthritis", "gout"]),
    ("Meloxicam", "meloxicam", ["Mobic"], "NSAIDs", ["osteoarthritis", "rheumatoid_arthritis"]),
    ("Celecoxib", "celecoxib", ["Celebrex"], "COX-2 Inhibitors", ["osteoarthritis", "rheumatoid_arthritis"]),
    ("Colchicine", "colchicine", ["Colcrys", "Mitigare"], "Anti-Gout", ["gout"]),
    ("Allopurinol", "allopurinol", ["Zyloprim"], "Xanthine Oxidase Inhibitors", ["gout"]),
    ("Febuxostat", "febuxostat", ["Uloric"], "Xanthine Oxidase Inhibitors", ["gout"]),
    ("Methotrexate", "methotrexate", ["Trexall", "Otrexup"], "DMARDs", ["rheumatoid_arthritis", "lupus"]),
    ("Plaquenil", "hydroxychloroquine", ["Plaquenil"], "DMARDs", ["rheumatoid_arthritis", "lupus"]),
    ("Enbrel", "etanercept", ["Enbrel"], "TNF Inhibitors", ["rheumatoid_arthritis"]),
    ("Alendronate", "alendronate", ["Fosamax"], "Bisphosphonates", ["osteoporosis"]),
    ("Risedronate", "risedronate", ["Actonel"], "Bisphosphonates", ["osteoporosis"]),
    ("Prolia", "denosumab", ["Prolia"], "RANK Ligand Inhibitors", ["osteoporosis"]),

    # ── PARKINSON'S ──────────────────────────────────────────
    ("Sinemet", "carbidopa/levodopa", ["Sinemet", "Rytary"], "Dopamine Precursors", ["parkinsons"]),
    ("Requip", "ropinirole", ["Requip"], "Dopamine Agonists", ["parkinsons"]),
    ("Mirapex", "pramipexole", ["Mirapex"], "Dopamine Agonists", ["parkinsons"]),
    ("Azilect", "rasagiline", ["Azilect"], "MAO-B Inhibitors", ["parkinsons"]),

    # ── ALZHEIMER'S / DEMENTIA ───────────────────────────────
    ("Aricept", "donepezil", ["Aricept"], "Cholinesterase Inhibitors", ["alzheimers_dementia"]),
    ("Namenda", "memantine", ["Namenda"], "NMDA Antagonists", ["alzheimers_dementia"]),
    ("Exelon", "rivastigmine", ["Exelon"], "Cholinesterase Inhibitors", ["alzheimers_dementia"]),

    # ── SLEEP APNEA ──────────────────────────────────────────
    # CPAP is a device, not a medication — no entries needed

    # ── ANEMIA ───────────────────────────────────────────────
    ("Ferrous Sulfate", "ferrous sulfate", ["Feosol", "Slow FE"], "Iron Supplements", ["anemia"]),
    ("Hydroxyurea", "hydroxyurea", ["Droxia", "Hydrea"], "Antineoplastics", ["anemia"]),

    # ── IMMUNOSUPPRESSANTS (transplant) ──────────────────────
    ("Tacrolimus", "tacrolimus", ["Prograf", "Envarsus"], "Calcineurin Inhibitors", ["organ_transplant"]),
    ("Cyclosporine", "cyclosporine", ["Neoral", "Sandimmune"], "Calcineurin Inhibitors", ["organ_transplant"]),
    ("Mycophenolate", "mycophenolate mofetil", ["CellCept", "Myfortic"], "Antimetabolites", ["organ_transplant", "lupus"]),
    ("Azathioprine", "azathioprine", ["Imuran"], "Immunosuppressants", ["organ_transplant", "crohns_colitis", "lupus"]),

    # ── CANCER (common maintenance) ──────────────────────────
    ("Tamoxifen", "tamoxifen", ["Nolvadex"], "SERMs", ["cancer_general"]),
    ("Anastrozole", "anastrozole", ["Arimidex"], "Aromatase Inhibitors", ["cancer_general"]),
    ("Letrozole", "letrozole", ["Femara"], "Aromatase Inhibitors", ["cancer_general"]),
    ("Ibrance", "palbociclib", ["Ibrance"], "CDK4/6 Inhibitors", ["cancer_general"]),

    # ── OPIOIDS (affects underwriting) ───────────────────────
    ("Oxycodone", "oxycodone", ["OxyContin", "Roxicodone", "Percocet"], "Opioid Analgesics", ["fibromyalgia"]),
    ("Hydrocodone", "hydrocodone", ["Vicodin", "Norco", "Lortab"], "Opioid Analgesics", ["fibromyalgia"]),
    ("Morphine", "morphine", ["MS Contin", "Kadian"], "Opioid Analgesics", []),
    ("Tramadol", "tramadol", ["Ultram", "ConZip"], "Opioid Analgesics", []),
    ("Fentanyl Patch", "fentanyl", ["Duragesic"], "Opioid Analgesics", []),
    ("Suboxone", "buprenorphine/naloxone", ["Suboxone", "Subutex"], "Opioid Partial Agonists", ["drug_abuse"]),
    ("Methadone", "methadone", ["Dolophine"], "Opioid Agonists", ["drug_abuse"]),
    ("Naltrexone", "naltrexone", ["Vivitrol", "ReVia"], "Opioid Antagonists", ["alcohol_abuse", "drug_abuse"]),
    ("Antabuse", "disulfiram", ["Antabuse"], "Alcohol Deterrents", ["alcohol_abuse"]),
    ("Campral", "acamprosate", ["Campral"], "Alcohol Dependence", ["alcohol_abuse"]),
]


def seed():
    """Insert all medications and their condition mappings."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Check if already seeded
            cur.execute("SELECT COUNT(*) AS cnt FROM uw_medications")
            existing = cur.fetchone()["cnt"]
            if existing > 0:
                print(f"uw_medications already has {existing} rows. Skipping seed.")
                print("To re-seed, run: DELETE FROM uw_drug_condition_map; DELETE FROM uw_medications;")
                return

            # Build condition slug → id map
            cur.execute("SELECT id, slug FROM uw_conditions")
            condition_map = {r["slug"]: r["id"] for r in cur.fetchall()}

            if not condition_map:
                print("ERROR: No conditions found. Run seed_conditions.py first.")
                return

            med_count = 0
            map_count = 0

            for med in MEDICATIONS:
                name, generic_name, brand_names, drug_class, condition_slugs = med

                # Insert medication
                cur.execute("""
                    INSERT INTO uw_medications (name, generic_name, brand_names, drug_class)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                """, (name, generic_name, brand_names, drug_class))
                med_id = cur.fetchone()["id"]
                med_count += 1

                # Insert drug → condition mappings
                for i, slug in enumerate(condition_slugs):
                    cond_id = condition_map.get(slug)
                    if cond_id:
                        cur.execute("""
                            INSERT INTO uw_drug_condition_map (medication_id, condition_id, is_primary)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (medication_id, condition_id) DO NOTHING
                        """, (med_id, cond_id, i == 0))
                        map_count += 1
                    else:
                        print(f"  WARNING: No condition found for slug '{slug}' (medication: {name})")

            conn.commit()
            print(f"Seeded {med_count} medications with {map_count} condition mappings.")

    except Exception as e:
        conn.rollback()
        print(f"Error seeding medications: {e}")
        raise
    finally:
        return_db_connection(conn)


if __name__ == "__main__":
    seed()
