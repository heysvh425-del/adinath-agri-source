"""Conservative enrichment of the 110 source catalog rows.

Brands are copied only where the source product name names a brand. Categories
use explicit product/manufacturer identification or a specific HSN class; the
remaining entries stay under Other Agricultural Products for review.
"""

SOURCE_BRANDS = {
    1: "Tata", 6: "IIL", 7: "IIL", 8: "IIL", 9: "IIL",
    11: "IIL", 12: "TATA", 14: "JU", 18: "Atul",
    27: "Dhanuka", 28: "Dhanuka", 32: "Dhanuka", 33: "Dhanuka",
    39: "IIL", 40: "Bharat Certis", 41: "Bharat Certis",
    56: "Dhanuka", 57: "Dhanuka", 58: "IIL", 59: "IIL", 60: "IIL", 61: "IIL",
    64: "Pratik", 65: "Pratik", 66: "GPC", 67: "Pratik", 69: "Pratik",
    70: "Aries", 71: "Aries", 83: "GPC", 84: "Pratik", 89: "Criyagen",
    90: "Criyagen", 92: "Bharat Certis", 102: "Aries", 103: "DCM",
    104: "Mahadhan", 105: "Mahadhan", 106: "Mahadhan", 107: "Mahadhan",
    108: "Mahadhan", 109: "Mahadhan",
}

SOURCE_CATEGORIES = {
    1: "Herbicides", 2: "Herbicides", 3: "Insecticides",
    6: "Herbicides", 7: "Herbicides", 8: "Herbicides", 9: "Herbicides",
    11: "Herbicides", 13: "Herbicides", 14: "Herbicides", 16: "Herbicides",
    20: "Herbicides", 21: "Herbicides", 22: "Herbicides", 23: "Herbicides",
    24: "Herbicides", 25: "Agricultural Equipment", 26: "Agricultural Equipment",
    **{serial: "Fungicides" for serial in range(27, 39)},
    **{serial: "Insecticides" for serial in range(39, 64)},
    64: "Plant Growth Regulators", 65: "Plant Growth Regulators",
    70: "Micronutrients", 71: "Micronutrients",
    75: "Fertilizers", 76: "Fertilizers", 77: "Fertilizers", 78: "Fertilizers",
    79: "Plant Growth Regulators", 80: "Fertilizers", 83: "Fertilizers",
    84: "Micronutrients", 85: "Plant Growth Regulators", 86: "Plant Growth Regulators",
    87: "Plant Growth Regulators", 88: "Plant Growth Regulators",
    89: "Micronutrients", 90: "Micronutrients", 91: "Fertilizers",
    **{serial: "Fertilizers" for serial in range(93, 97)},
    100: "Plant Growth Regulators", 101: "Plant Growth Regulators",
    **{serial: "Fertilizers" for serial in range(102, 110)},
}

DEFAULT_SOURCE_CATEGORY = "Other Agricultural Products"
