"""
Mia Dental Service Library

Purpose:
- Keep a large master list of dental services Mia can understand.
- Keep visible widget buttons simple.
- Allow each office/client to enable only the services they actually provide.
- Prepare for future Supabase client-specific customization.

This file should not control the UI by itself yet.
It is the backend knowledge layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class DentalService:
    key: str
    display_name: str
    category: str
    aliases: List[str]
    common_specialties: List[str]


# Main backend categories.
SERVICE_CATEGORIES: Dict[str, str] = {
    "general": "General Appointment",
    "preventive": "Preventive Care",
    "urgent": "Tooth Pain / Emergency",
    "restorative": "Restorative Dentistry",
    "endodontic": "Root Canal / Endodontic",
    "oral_surgery": "Oral Surgery / Extractions",
    "cosmetic": "Cosmetic Dentistry",
    "orthodontic": "Orthodontics",
    "implants_dentures": "Implants / Dentures",
    "periodontic": "Gum Care / Periodontics",
    "pediatric": "Pediatric Dentistry",
    "tmj_oral_medicine": "TMJ / Oral Medicine",
    "sleep": "Sleep Dentistry",
    "diagnostic": "Diagnostics / Imaging",
    "admin_other": "Admin / Other",
}


MASTER_DENTAL_SERVICES: Dict[str, DentalService] = {
    # General
    "dental_consultation": DentalService(
        key="dental_consultation",
        display_name="Dental Consultation",
        category="general",
        aliases=[
            "consultation",
            "dental consultation",
            "new patient consultation",
            "second opinion",
            "treatment consultation",
        ],
        common_specialties=["general", "cosmetic", "orthodontic", "oral_surgery", "periodontic"],
    ),
    "new_patient_exam": DentalService(
        key="new_patient_exam",
        display_name="New Patient Exam",
        category="general",
        aliases=[
            "new patient exam",
            "first visit",
            "new patient visit",
            "comprehensive exam",
            "initial exam",
        ],
        common_specialties=["general", "pediatric"],
    ),
    "follow_up": DentalService(
        key="follow_up",
        display_name="Follow-up Visit",
        category="general",
        aliases=[
            "follow up",
            "follow-up",
            "followup",
            "post treatment follow up",
            "check on previous treatment",
        ],
        common_specialties=["general", "pediatric", "orthodontic", "oral_surgery", "periodontic"],
    ),

    # Preventive
    "cleaning_checkup": DentalService(
        key="cleaning_checkup",
        display_name="Cleaning / Checkup",
        category="preventive",
        aliases=[
            "cleaning",
            "dental cleaning",
            "checkup",
            "check up",
            "routine cleaning",
            "teeth cleaning",
            "exam and cleaning",
            "cleaning and exam",
            "regular cleaning",
        ],
        common_specialties=["general", "pediatric"],
    ),
    "deep_cleaning": DentalService(
        key="deep_cleaning",
        display_name="Deep Cleaning",
        category="periodontic",
        aliases=[
            "deep cleaning",
            "scaling and root planing",
            "scaling root planing",
            "srp",
            "periodontal cleaning",
        ],
        common_specialties=["general", "periodontic"],
    ),
    "x_rays": DentalService(
        key="x_rays",
        display_name="Dental X-rays",
        category="diagnostic",
        aliases=[
            "xray",
            "x-ray",
            "x rays",
            "x-rays",
            "dental xray",
            "dental x-ray",
            "panoramic xray",
            "panoramic x-ray",
            "pano",
        ],
        common_specialties=["general", "pediatric", "oral_surgery", "orthodontic"],
    ),
    "fluoride": DentalService(
        key="fluoride",
        display_name="Fluoride Treatment",
        category="preventive",
        aliases=[
            "fluoride",
            "fluoride treatment",
            "fluoride varnish",
        ],
        common_specialties=["general", "pediatric"],
    ),
    "sealants": DentalService(
        key="sealants",
        display_name="Dental Sealants",
        category="preventive",
        aliases=[
            "sealant",
            "sealants",
            "dental sealants",
            "tooth sealants",
        ],
        common_specialties=["general", "pediatric"],
    ),

    # Urgent / symptoms
    "tooth_pain": DentalService(
        key="tooth_pain",
        display_name="Tooth Pain",
        category="urgent",
        aliases=[
            "tooth pain",
            "toothache",
            "tooth ache",
            "my tooth hurts",
            "teeth hurt",
            "dental pain",
            "pain in my tooth",
            "pain when chewing",
        ],
        common_specialties=["general", "pediatric", "endodontic"],
    ),
    "broken_tooth": DentalService(
        key="broken_tooth",
        display_name="Broken / Chipped Tooth",
        category="urgent",
        aliases=[
            "broken tooth",
            "chipped tooth",
            "cracked tooth",
            "fractured tooth",
            "piece of my tooth broke",
            "tooth broke",
        ],
        common_specialties=["general", "pediatric", "oral_surgery"],
    ),
    "swelling_abscess": DentalService(
        key="swelling_abscess",
        display_name="Swelling / Possible Infection",
        category="urgent",
        aliases=[
            "swelling",
            "swollen gum",
            "swollen gums",
            "face swollen",
            "dental infection",
            "infection",
            "abscess",
            "gum abscess",
            "tooth abscess",
        ],
        common_specialties=["general", "pediatric", "endodontic", "oral_surgery", "periodontic"],
    ),
    "lost_crown_filling": DentalService(
        key="lost_crown_filling",
        display_name="Lost Crown / Filling",
        category="urgent",
        aliases=[
            "lost crown",
            "crown fell out",
            "lost filling",
            "filling fell out",
            "temporary crown fell out",
        ],
        common_specialties=["general", "restorative"],
    ),

    # Restorative
    "fillings": DentalService(
        key="fillings",
        display_name="Fillings",
        category="restorative",
        aliases=[
            "filling",
            "fillings",
            "cavity",
            "cavities",
            "tooth filling",
            "dental filling",
            "composite filling",
            "tooth colored filling",
        ],
        common_specialties=["general", "pediatric"],
    ),
    "crowns": DentalService(
        key="crowns",
        display_name="Crowns",
        category="restorative",
        aliases=[
            "crown",
            "crowns",
            "dental crown",
            "tooth crown",
            "cap",
            "tooth cap",
            "crown replacement",
        ],
        common_specialties=["general", "prosthodontic"],
    ),
    "bridges": DentalService(
        key="bridges",
        display_name="Bridges",
        category="restorative",
        aliases=[
            "bridge",
            "bridges",
            "dental bridge",
            "bridge repair",
            "missing tooth bridge",
        ],
        common_specialties=["general", "prosthodontic"],
    ),
    "bonding": DentalService(
        key="bonding",
        display_name="Dental Bonding",
        category="cosmetic",
        aliases=[
            "bonding",
            "dental bonding",
            "tooth bonding",
            "cosmetic bonding",
        ],
        common_specialties=["general", "cosmetic"],
    ),

    # Root canal / endodontic
    "root_canal": DentalService(
        key="root_canal",
        display_name="Root Canal",
        category="endodontic",
        aliases=[
            "root canal",
            "root canal treatment",
            "endodontic treatment",
            "need a root canal",
            "tooth nerve pain",
        ],
        common_specialties=["general", "endodontic"],
    ),

    # Oral surgery
    "tooth_extraction": DentalService(
        key="tooth_extraction",
        display_name="Tooth Extraction",
        category="oral_surgery",
        aliases=[
            "extraction",
            "tooth extraction",
            "pull a tooth",
            "pull tooth",
            "remove tooth",
            "tooth removal",
        ],
        common_specialties=["general", "oral_surgery", "pediatric"],
    ),
    "wisdom_tooth": DentalService(
        key="wisdom_tooth",
        display_name="Wisdom Tooth Problem",
        category="oral_surgery",
        aliases=[
            "wisdom tooth",
            "wisdom teeth",
            "wisdom tooth problem",
            "wisdom teeth removal",
            "impacted wisdom tooth",
            "third molar",
        ],
        common_specialties=["general", "oral_surgery"],
    ),
    "bone_graft": DentalService(
        key="bone_graft",
        display_name="Bone Graft",
        category="oral_surgery",
        aliases=[
            "bone graft",
            "dental bone graft",
            "socket preservation",
        ],
        common_specialties=["oral_surgery", "periodontic", "general"],
    ),

    # Cosmetic
    "teeth_whitening": DentalService(
        key="teeth_whitening",
        display_name="Teeth Whitening",
        category="cosmetic",
        aliases=[
            "whitening",
            "teeth whitening",
            "tooth whitening",
            "bleaching",
            "laser whitening",
        ],
        common_specialties=["general", "cosmetic"],
    ),
    "veneers": DentalService(
        key="veneers",
        display_name="Veneers",
        category="cosmetic",
        aliases=[
            "veneer",
            "veneers",
            "porcelain veneers",
            "composite veneers",
        ],
        common_specialties=["general", "cosmetic"],
    ),
    "smile_makeover": DentalService(
        key="smile_makeover",
        display_name="Smile Makeover",
        category="cosmetic",
        aliases=[
            "smile makeover",
            "cosmetic consultation",
            "cosmetic dentistry",
            "improve my smile",
        ],
        common_specialties=["general", "cosmetic"],
    ),

    # Orthodontic
    "braces": DentalService(
        key="braces",
        display_name="Braces",
        category="orthodontic",
        aliases=[
            "braces",
            "braces consultation",
            "braces adjustment",
            "orthodontic braces",
        ],
        common_specialties=["orthodontic", "general"],
    ),
    "invisalign": DentalService(
        key="invisalign",
        display_name="Invisalign / Clear Aligners",
        category="orthodontic",
        aliases=[
            "invisalign",
            "clear aligners",
            "aligners",
            "invisalign consultation",
        ],
        common_specialties=["orthodontic", "general", "cosmetic"],
    ),
    "retainers": DentalService(
        key="retainers",
        display_name="Retainers",
        category="orthodontic",
        aliases=[
            "retainer",
            "retainers",
            "retainer replacement",
            "lost retainer",
            "broken retainer",
        ],
        common_specialties=["orthodontic"],
    ),

    # Implants / dentures
    "implants": DentalService(
        key="implants",
        display_name="Dental Implants",
        category="implants_dentures",
        aliases=[
            "implant",
            "implants",
            "dental implant",
            "dental implants",
            "implant consultation",
            "missing tooth implant",
        ],
        common_specialties=["general", "oral_surgery", "periodontic", "prosthodontic"],
    ),
    "dentures": DentalService(
        key="dentures",
        display_name="Dentures",
        category="implants_dentures",
        aliases=[
            "denture",
            "dentures",
            "full dentures",
            "partial dentures",
            "denture repair",
            "denture reline",
        ],
        common_specialties=["general", "prosthodontic"],
    ),

    # Periodontic
    "gum_disease": DentalService(
        key="gum_disease",
        display_name="Gum Disease",
        category="periodontic",
        aliases=[
            "gum disease",
            "periodontal disease",
            "gingivitis",
            "periodontitis",
            "bleeding gums",
            "gum infection",
        ],
        common_specialties=["general", "periodontic"],
    ),
    "gum_grafting": DentalService(
        key="gum_grafting",
        display_name="Gum Grafting",
        category="periodontic",
        aliases=[
            "gum graft",
            "gum grafting",
            "receding gums",
            "gum recession",
        ],
        common_specialties=["periodontic"],
    ),

    # Pediatric
    "child_cleaning": DentalService(
        key="child_cleaning",
        display_name="Child Cleaning / Exam",
        category="pediatric",
        aliases=[
            "child cleaning",
            "kids cleaning",
            "pediatric cleaning",
            "child dental exam",
            "kids dental exam",
            "first dental visit",
        ],
        common_specialties=["pediatric"],
    ),
    "child_cavity": DentalService(
        key="child_cavity",
        display_name="Child Cavity / Filling",
        category="pediatric",
        aliases=[
            "child cavity",
            "kids cavity",
            "baby tooth cavity",
            "child filling",
            "kids filling",
        ],
        common_specialties=["pediatric"],
    ),
    "space_maintainer": DentalService(
        key="space_maintainer",
        display_name="Space Maintainer",
        category="pediatric",
        aliases=[
            "space maintainer",
            "space maintainers",
        ],
        common_specialties=["pediatric"],
    ),

    # TMJ / oral medicine
    "tmj": DentalService(
        key="tmj",
        display_name="TMJ / Jaw Pain",
        category="tmj_oral_medicine",
        aliases=[
            "tmj",
            "tmj pain",
            "jaw pain",
            "jaw clicking",
            "jaw locking",
            "jaw pops",
            "temporomandibular",
        ],
        common_specialties=["general", "tmj_oral_medicine"],
    ),
    "night_guard": DentalService(
        key="night_guard",
        display_name="Night Guard / Teeth Grinding",
        category="tmj_oral_medicine",
        aliases=[
            "night guard",
            "mouth guard",
            "teeth grinding",
            "grinding teeth",
            "bruxism",
            "clenching",
        ],
        common_specialties=["general", "tmj_oral_medicine"],
    ),
    "oral_cancer_screening": DentalService(
        key="oral_cancer_screening",
        display_name="Oral Cancer Screening",
        category="tmj_oral_medicine",
        aliases=[
            "oral cancer screening",
            "oral cancer check",
            "mouth cancer screening",
            "oral lesion",
            "sore in mouth",
            "tongue problem",
        ],
        common_specialties=["general", "oral_surgery"],
    ),

    # Sleep
    "sleep_apnea_appliance": DentalService(
        key="sleep_apnea_appliance",
        display_name="Sleep Apnea Appliance",
        category="sleep",
        aliases=[
            "sleep apnea",
            "sleep apnea appliance",
            "snoring appliance",
            "dental sleep medicine",
        ],
        common_specialties=["general", "sleep"],
    ),

    # Admin / other
    "insurance_question": DentalService(
        key="insurance_question",
        display_name="Insurance Question",
        category="admin_other",
        aliases=[
            "insurance",
            "insurance question",
            "do you take my insurance",
            "coverage",
            "benefits",
        ],
        common_specialties=["general", "pediatric", "orthodontic", "oral_surgery", "periodontic"],
    ),
    "payment_financing": DentalService(
        key="payment_financing",
        display_name="Payment / Financing Question",
        category="admin_other",
        aliases=[
            "payment",
            "financing",
            "payment plan",
            "cost",
            "price",
            "how much",
        ],
        common_specialties=["general", "pediatric", "orthodontic", "oral_surgery", "periodontic"],
    ),
    "records_request": DentalService(
        key="records_request",
        display_name="Records Request",
        category="admin_other",
        aliases=[
            "records",
            "dental records",
            "xray records",
            "x-ray records",
            "send my records",
            "transfer records",
        ],
        common_specialties=["general", "pediatric", "orthodontic", "oral_surgery", "periodontic"],
    ),
    "prescription_question": DentalService(
        key="prescription_question",
        display_name="Prescription / Refill Question",
        category="admin_other",
        aliases=[
            "prescription",
            "refill",
            "antibiotic",
            "pain medication",
            "medicine",
            "medication",
        ],
        common_specialties=["general", "oral_surgery", "endodontic"],
    ),
}


# Default visible buttons should stay simple.
DEFAULT_VISIBLE_SERVICE_BUTTONS: List[str] = [
    "cleaning_checkup",
    "tooth_pain",
    "fillings",
    "crowns",
    "teeth_whitening",
    "invisalign",
    "other",
]


# Safe default services for a general dentist demo.
DEFAULT_ENABLED_SERVICE_KEYS: List[str] = [
    "dental_consultation",
    "new_patient_exam",
    "follow_up",
    "cleaning_checkup",
    "deep_cleaning",
    "x_rays",
    "fluoride",
    "sealants",
    "tooth_pain",
    "broken_tooth",
    "swelling_abscess",
    "lost_crown_filling",
    "fillings",
    "crowns",
    "bridges",
    "bonding",
    "root_canal",
    "tooth_extraction",
    "wisdom_tooth",
    "teeth_whitening",
    "veneers",
    "smile_makeover",
    "braces",
    "invisalign",
    "retainers",
    "implants",
    "dentures",
    "gum_disease",
    "tmj",
    "night_guard",
    "oral_cancer_screening",
    "insurance_question",
    "payment_financing",
    "records_request",
    "prescription_question",
]


SPECIALTY_PRESETS: Dict[str, List[str]] = {
    "general": DEFAULT_ENABLED_SERVICE_KEYS,

    "pediatric": [
        "dental_consultation",
        "new_patient_exam",
        "follow_up",
        "child_cleaning",
        "child_cavity",
        "fluoride",
        "sealants",
        "space_maintainer",
        "tooth_pain",
        "broken_tooth",
        "swelling_abscess",
        "tooth_extraction",
        "x_rays",
        "insurance_question",
        "payment_financing",
        "records_request",
    ],

    "orthodontic": [
        "dental_consultation",
        "follow_up",
        "braces",
        "invisalign",
        "retainers",
        "x_rays",
        "payment_financing",
        "records_request",
        "insurance_question",
    ],

    "oral_surgery": [
        "dental_consultation",
        "follow_up",
        "tooth_extraction",
        "wisdom_tooth",
        "bone_graft",
        "implants",
        "swelling_abscess",
        "broken_tooth",
        "oral_cancer_screening",
        "x_rays",
        "payment_financing",
        "records_request",
        "insurance_question",
    ],

    "periodontic": [
        "dental_consultation",
        "follow_up",
        "deep_cleaning",
        "gum_disease",
        "gum_grafting",
        "implants",
        "bone_graft",
        "swelling_abscess",
        "x_rays",
        "payment_financing",
        "records_request",
        "insurance_question",
    ],

    "cosmetic": [
        "dental_consultation",
        "new_patient_exam",
        "cleaning_checkup",
        "teeth_whitening",
        "veneers",
        "bonding",
        "smile_makeover",
        "invisalign",
        "crowns",
        "implants",
        "payment_financing",
        "records_request",
        "insurance_question",
    ],
}


def normalize_service_text(text: str) -> str:
    return " ".join((text or "").lower().strip().replace("/", " ").replace("-", " ").split())


def get_service(service_key: str) -> Optional[DentalService]:
    return MASTER_DENTAL_SERVICES.get(service_key)


def get_enabled_services_for_specialty(specialty: str = "general") -> List[DentalService]:
    specialty_key = normalize_service_text(specialty).replace(" ", "_")
    enabled_keys = SPECIALTY_PRESETS.get(specialty_key, DEFAULT_ENABLED_SERVICE_KEYS)

    return [
        MASTER_DENTAL_SERVICES[key]
        for key in enabled_keys
        if key in MASTER_DENTAL_SERVICES
    ]


def find_matching_service(
    user_text: str,
    enabled_service_keys: Optional[List[str]] = None,
) -> Optional[DentalService]:
    """
    Match a patient message to a known dental service.

    If enabled_service_keys is provided, Mia only returns services that
    the office is allowed to offer.
    """
    normalized_text = normalize_service_text(user_text)
    if not normalized_text:
        return None

    allowed_keys = set(enabled_service_keys or MASTER_DENTAL_SERVICES.keys())

    # Prefer longer aliases first so "wisdom tooth problem" beats "tooth".
    candidates: List[tuple[int, DentalService]] = []

    for service in MASTER_DENTAL_SERVICES.values():
        if service.key not in allowed_keys:
            continue

        all_terms = [service.display_name] + service.aliases

        for term in all_terms:
            normalized_term = normalize_service_text(term)
            if not normalized_term:
                continue

            if normalized_term in normalized_text:
                candidates.append((len(normalized_term), service))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def get_visible_service_button_labels(
    visible_service_keys: Optional[List[str]] = None,
) -> List[str]:
    keys = visible_service_keys or DEFAULT_VISIBLE_SERVICE_BUTTONS

    labels: List[str] = []

    for key in keys:
        if key == "other":
            labels.append("Other")
            continue

        service = MASTER_DENTAL_SERVICES.get(key)
        if service:
            labels.append(service.display_name)

    return labels


def service_is_enabled(service_key: str, enabled_service_keys: Optional[List[str]]) -> bool:
    if not service_key:
        return False

    if not enabled_service_keys:
        return True

    return service_key in set(enabled_service_keys)