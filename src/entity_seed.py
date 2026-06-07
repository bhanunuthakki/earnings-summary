"""Canonical entity seed for portfolio + watchlist tickers.

Each ticker gets:
  - One company-kind entity with external_ids (cik, ticker)
  - N segment-kind child entities with parent_entity_id linking to the company
  - Every observed segment surface form as an alias under the right entity
  - segment_of relationship from segment → company

The segment definitions come from the holdings JSON files where available, plus
hand-curated mappings here for canonical names that vary across reporting
periods. The deterministic resolver in `entity_resolver.py` then uses these
aliases to backfill `segment_dimensions.segment_entity_id` for the historical
rows.

Why a Python module and not YAML/JSON: the lookup table is referenced from
multiple call sites (the seeder, the resolver, the LLM canonicalizer), and a
typed Python literal gives IDE auto-complete + type-checker coverage.

Adding a new ticker: drop an entry under PORTFOLIO_HOLDINGS or
WATCHLIST_BIZ_MODELS with the same shape. The seeder is idempotent — re-run
after edits to apply new aliases.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SegmentSeed:
    """One segment of one company.

    canonical_name is the display name; aliases lists every other surface
    form seen in segment_dimensions / 10-Ks / transcripts. The resolver
    picks canonical_name as the primary lookup key.
    """

    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    # Optional product names that sit under this segment (e.g. AWS → EC2, S3)
    products: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CompanySeed:
    """One ticker's full entity tree: company + segments + competitors."""

    ticker: str
    canonical_name: str  # "Amazon.com, Inc." — used as the entity row's canonical_name
    display_name: str  # "Amazon" — UI-friendly
    aliases: list[str] = field(default_factory=list)
    sector: str | None = None
    segments: list[SegmentSeed] = field(default_factory=list)
    # Named competitors (rendered as separate competitor-kind entities with
    # `competes_with` relationships — useful for the entity-mention extractor)
    competitors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Portfolio holdings — hand-curated canonical segment trees
# ---------------------------------------------------------------------------

PORTFOLIO_HOLDINGS: dict[str, CompanySeed] = {
    "AMZN": CompanySeed(
        ticker="AMZN",
        canonical_name="Amazon.com, Inc.",
        display_name="Amazon",
        aliases=["Amazon", "Amazon.com", "AMZN"],
        sector="Consumer Discretionary",
        segments=[
            SegmentSeed(
                canonical_name="AWS",
                aliases=[
                    "Amazon Web Services",
                    "Amazon Web Services Segment",
                    "AWS Segment",
                    "Cloud Services",
                ],
                products=["EC2", "S3", "Bedrock", "SageMaker", "RDS", "Lambda"],
            ),
            SegmentSeed(
                canonical_name="North America",
                aliases=["North America Segment", "NA Segment", "North America retail"],
            ),
            SegmentSeed(
                canonical_name="International",
                aliases=["International Segment", "Intl Segment"],
            ),
            SegmentSeed(canonical_name="Advertising Services", aliases=["Advertising", "Ads"]),
            SegmentSeed(canonical_name="Online Stores", aliases=["Online Store", "1P retail"]),
            SegmentSeed(
                canonical_name="Third-Party Seller Services",
                aliases=["3P Marketplace", "Third Party Seller Services", "3P"],
            ),
            SegmentSeed(canonical_name="Subscription Services", aliases=["Prime", "Subscriptions"]),
            SegmentSeed(canonical_name="Physical Stores", aliases=["Brick-and-Mortar", "Whole Foods"]),
        ],
        competitors=["Microsoft Azure", "Google Cloud", "Walmart", "Shopify", "Alibaba"],
    ),
    "GOOG": CompanySeed(
        ticker="GOOG",
        canonical_name="Alphabet Inc.",
        display_name="Alphabet",
        aliases=["Google", "Alphabet", "GOOG", "GOOGL"],
        sector="Communication Services",
        segments=[
            SegmentSeed(
                canonical_name="Google Search & other",
                aliases=[
                    "Google Search",
                    "Search",
                    "Search and other",
                    "Search & other Google properties",
                    "Search ads",
                ],
            ),
            SegmentSeed(
                canonical_name="YouTube ads",
                aliases=["YouTube advertising", "YouTube"],
            ),
            SegmentSeed(
                canonical_name="Google Network",
                aliases=["Network", "Google Network ads", "Network ads"],
            ),
            SegmentSeed(
                canonical_name="Subscriptions, platforms, and devices",
                aliases=[
                    "Subscriptions Platforms and Devices",
                    "Google subscriptions, platforms, and devices",
                    "Devices",
                    "Hardware",
                ],
            ),
            SegmentSeed(
                canonical_name="Google Cloud",
                aliases=["GCP", "Google Cloud Platform", "Cloud", "Cloud Services"],
                products=["Gemini", "Vertex AI", "BigQuery", "TPU"],
            ),
            SegmentSeed(
                canonical_name="Google Services",
                aliases=["Services", "Google Services Segment"],
            ),
            SegmentSeed(
                canonical_name="Other Bets",
                aliases=["Other Bets Segment"],
                products=["Waymo", "Verily", "Wing", "X (Moonshot)"],
            ),
        ],
        competitors=["Microsoft", "Amazon", "Meta", "Apple", "OpenAI", "Anthropic", "TikTok"],
    ),
    "META": CompanySeed(
        ticker="META",
        canonical_name="Meta Platforms, Inc.",
        display_name="Meta",
        aliases=["Meta", "Facebook", "META"],
        sector="Communication Services",
        segments=[
            SegmentSeed(
                canonical_name="Family of Apps",
                aliases=["FoA", "Family of Apps Segment", "Apps"],
                products=["Facebook", "Instagram", "WhatsApp", "Messenger", "Threads"],
            ),
            SegmentSeed(
                canonical_name="Reality Labs",
                aliases=["RL", "Reality Labs Segment", "VR/AR"],
                products=["Quest", "Ray-Ban Meta", "Orion"],
            ),
        ],
        competitors=["TikTok", "ByteDance", "Snapchat", "Apple", "Google"],
    ),
    "NU": CompanySeed(
        ticker="NU",
        canonical_name="Nu Holdings Ltd.",
        display_name="Nubank",
        aliases=["Nubank", "Nu", "Nu Holdings"],
        sector="Financials",
        segments=[
            SegmentSeed(canonical_name="Brazil", aliases=["Nubank Brazil", "BR"]),
            SegmentSeed(canonical_name="Mexico", aliases=["Nubank Mexico", "MX"]),
            SegmentSeed(canonical_name="Colombia", aliases=["Nubank Colombia", "CO"]),
        ],
        competitors=["Itaú", "Banco do Brasil", "Bradesco", "Inter", "C6 Bank", "MercadoPago"],
    ),
    "MELI": CompanySeed(
        ticker="MELI",
        canonical_name="MercadoLibre, Inc.",
        display_name="MercadoLibre",
        aliases=["MercadoLibre", "Meli", "MELI"],
        sector="Consumer Discretionary",
        segments=[
            SegmentSeed(
                canonical_name="Commerce",
                aliases=["Mercado Libre Marketplace", "GMV", "Mercado Envíos"],
            ),
            SegmentSeed(
                canonical_name="Fintech",
                aliases=["Mercado Pago", "Mercado Crédito", "Financial Services"],
                products=["Mercado Pago", "Mercado Crédito", "Mercado Fondo"],
            ),
        ],
        competitors=["Amazon", "Shopify", "Nubank", "PagSeguro", "StoneCo"],
    ),
    "NOW": CompanySeed(
        ticker="NOW",
        canonical_name="ServiceNow, Inc.",
        display_name="ServiceNow",
        aliases=["ServiceNow", "Service Now", "NOW"],
        sector="Technology",
        segments=[
            SegmentSeed(canonical_name="Subscription Services", aliases=["Subscription"]),
            SegmentSeed(canonical_name="Professional Services", aliases=["Services"]),
        ],
        competitors=["Salesforce", "SAP", "Atlassian", "Workday"],
    ),
    "VEEV": CompanySeed(
        ticker="VEEV",
        canonical_name="Veeva Systems, Inc.",
        display_name="Veeva",
        aliases=["Veeva", "Veeva Systems"],
        sector="Health Care",
        segments=[
            SegmentSeed(
                canonical_name="Commercial Cloud",
                aliases=["Commercial", "Veeva Commercial Cloud"],
                products=["Vault CRM", "Crossix", "OpenData"],
            ),
            SegmentSeed(
                canonical_name="R&D Cloud",
                aliases=["R&D", "Veeva R&D Cloud", "Development Cloud"],
                products=["Vault CTMS", "Vault eTMF", "Vault QMS", "Vault Quality"],
            ),
            SegmentSeed(canonical_name="Subscription Services", aliases=["Subscription"]),
            SegmentSeed(canonical_name="Professional Services", aliases=["Services"]),
        ],
        competitors=["Salesforce", "IQVIA", "Oracle", "Medidata"],
    ),
    "RBRK": CompanySeed(
        ticker="RBRK",
        canonical_name="Rubrik, Inc.",
        display_name="Rubrik",
        aliases=["Rubrik"],
        sector="Technology",
        segments=[
            SegmentSeed(
                canonical_name="Subscription",
                aliases=["Subscription Services", "Software"],
                products=["Rubrik Security Cloud", "Polaris"],
            ),
            SegmentSeed(canonical_name="Maintenance", aliases=["Other"]),
        ],
        competitors=["Cohesity", "Veritas", "Veeam", "Dell", "Commvault"],
    ),
    "WIX": CompanySeed(
        ticker="WIX",
        canonical_name="Wix.com Ltd.",
        display_name="Wix",
        aliases=["Wix", "Wix.com"],
        sector="Technology",
        segments=[
            SegmentSeed(
                canonical_name="Creative Subscriptions",
                aliases=["Creative Subscription", "Creative"],
            ),
            SegmentSeed(
                canonical_name="Business Solutions",
                aliases=["Business", "Business Solutions Segment"],
                products=["Wix Payments", "Wix Studio", "Velo by Wix"],
            ),
        ],
        competitors=["Shopify", "Squarespace", "GoDaddy", "WordPress"],
    ),
    "NVO": CompanySeed(
        ticker="NVO",
        canonical_name="Novo Nordisk A/S",
        display_name="Novo Nordisk",
        aliases=["Novo Nordisk", "Novo", "NVO"],
        sector="Health Care",
        segments=[
            SegmentSeed(
                canonical_name="Diabetes and Obesity care",
                aliases=[
                    "Diabetes and Obesity",
                    "Diabetes",
                    "GLP-1",
                    "Obesity",
                    "Diabetes care",
                ],
                products=["Ozempic", "Wegovy", "Rybelsus", "Saxenda", "Victoza", "Levemir", "NovoLog", "Tresiba"],
            ),
            SegmentSeed(
                canonical_name="Rare disease",
                aliases=["Rare Diseases", "Biopharm", "Other Therapy Areas"],
                products=["NovoSeven", "NovoEight", "Esperoct", "Sogroya"],
            ),
        ],
        competitors=["Eli Lilly", "Sanofi", "Pfizer", "Roche", "Amgen"],
    ),
    "BN": CompanySeed(
        ticker="BN",
        canonical_name="Brookfield Corporation",
        display_name="Brookfield",
        aliases=["Brookfield", "Brookfield Corp", "BN"],
        sector="Financials",
        segments=[
            SegmentSeed(
                canonical_name="Asset Management",
                aliases=["BAM", "Brookfield Asset Management", "AM"],
            ),
            SegmentSeed(
                canonical_name="Insurance Solutions",
                aliases=["Wealth Solutions", "Insurance", "BNRE"],
            ),
            SegmentSeed(
                canonical_name="Renewable Power and Transition",
                aliases=["Renewable Power", "BEPC", "Brookfield Renewable"],
            ),
            SegmentSeed(
                canonical_name="Infrastructure",
                aliases=["BIP", "BIPC", "Brookfield Infrastructure"],
            ),
            SegmentSeed(canonical_name="Private Equity", aliases=["BBU", "Brookfield Business Partners"]),
            SegmentSeed(canonical_name="Real Estate", aliases=["BPY", "Real Estate Segment"]),
        ],
        competitors=["Blackstone", "KKR", "Apollo", "Ares"],
    ),
}


# ---------------------------------------------------------------------------
# Watchlist — minimal entity seed (just company + sector). Segments get
# canonicalized at run-time via the LLM resolver.
# ---------------------------------------------------------------------------

WATCHLIST_BIZ_MODELS: dict[str, dict[str, str]] = {
    "AMAT": {"name": "Applied Materials, Inc.", "display": "Applied Materials", "sector": "Technology"},
    "AMD": {"name": "Advanced Micro Devices, Inc.", "display": "AMD", "sector": "Technology"},
    "ASML": {"name": "ASML Holding N.V.", "display": "ASML", "sector": "Technology"},
    "AVGO": {"name": "Broadcom Inc.", "display": "Broadcom", "sector": "Technology"},
    "AWK": {"name": "American Water Works Co.", "display": "AWK", "sector": "Utilities"},
    "BAM": {"name": "Brookfield Asset Management Ltd.", "display": "BAM", "sector": "Financials"},
    "BEPC": {"name": "Brookfield Renewable Corporation", "display": "BEPC", "sector": "Utilities"},
    "BIPC": {"name": "Brookfield Infrastructure Corp.", "display": "BIPC", "sector": "Utilities"},
    "BRK-B": {"name": "Berkshire Hathaway Inc.", "display": "Berkshire", "sector": "Financials"},
    "CDNS": {"name": "Cadence Design Systems, Inc.", "display": "Cadence Design Systems", "sector": "Technology"},
    "CFLT": {"name": "Confluent, Inc.", "display": "Confluent", "sector": "Technology"},
    "CIEN": {"name": "Ciena Corporation", "display": "Ciena", "sector": "Technology"},
    "COHR": {"name": "Coherent Corp.", "display": "Coherent", "sector": "Technology"},
    "COST": {"name": "Costco Wholesale Corporation", "display": "Costco", "sector": "Consumer Staples"},
    "CRM": {"name": "Salesforce, Inc.", "display": "Salesforce", "sector": "Technology"},
    "CRWD": {"name": "CrowdStrike Holdings, Inc.", "display": "CrowdStrike", "sector": "Technology"},
    "DDOG": {"name": "Datadog, Inc.", "display": "Datadog", "sector": "Technology"},
    "DHR": {"name": "Danaher Corporation", "display": "Danaher", "sector": "Health Care"},
    "ENB": {"name": "Enbridge Inc.", "display": "Enbridge", "sector": "Energy"},
    "EPD": {"name": "Enterprise Products Partners L.P.", "display": "EPD", "sector": "Energy"},
    "ESTC": {"name": "Elastic N.V.", "display": "Elastic", "sector": "Technology"},
    "FNV": {"name": "Franco-Nevada Corporation", "display": "Franco-Nevada", "sector": "Materials"},
    "FTNT": {"name": "Fortinet, Inc.", "display": "Fortinet", "sector": "Technology"},
    "GTLB": {"name": "GitLab Inc.", "display": "GitLab", "sector": "Technology"},
    "HASI": {"name": "HA Sustainable Infrastructure Capital", "display": "HASI", "sector": "Financials"},
    "HBM": {"name": "Hudbay Minerals Inc.", "display": "Hudbay", "sector": "Materials"},
    "HDB": {"name": "HDFC Bank Limited", "display": "HDFC Bank", "sector": "Financials"},
    "HEI": {"name": "HEICO Corporation", "display": "HEICO", "sector": "Industrials"},
    "IBN": {"name": "ICICI Bank Limited", "display": "ICICI Bank", "sector": "Financials"},
    "ISRG": {"name": "Intuitive Surgical, Inc.", "display": "Intuitive Surgical", "sector": "Health Care"},
    "IVN": {"name": "Ivanhoe Mines Ltd.", "display": "Ivanhoe", "sector": "Materials"},
    "JPM": {"name": "JPMorgan Chase & Co.", "display": "JPMorgan", "sector": "Financials"},
    "KLAC": {"name": "KLA Corporation", "display": "KLA", "sector": "Technology"},
    "KVYO": {"name": "Klaviyo, Inc.", "display": "Klaviyo", "sector": "Technology"},
    "LITE": {"name": "Lumentum Holdings Inc.", "display": "Lumentum", "sector": "Technology"},
    "LMND": {"name": "Lemonade, Inc.", "display": "Lemonade", "sector": "Financials"},
    "MA": {"name": "Mastercard Incorporated", "display": "Mastercard", "sector": "Financials"},
    "MDB": {"name": "MongoDB, Inc.", "display": "MongoDB", "sector": "Technology"},
    "MRVL": {"name": "Marvell Technology, Inc.", "display": "Marvell", "sector": "Technology"},
    "MSFT": {"name": "Microsoft Corporation", "display": "Microsoft", "sector": "Technology"},
    "MU": {"name": "Micron Technology, Inc.", "display": "Micron", "sector": "Technology"},
    "NEE": {"name": "NextEra Energy, Inc.", "display": "NextEra", "sector": "Utilities"},
    "NET": {"name": "Cloudflare, Inc.", "display": "Cloudflare", "sector": "Technology"},
    "NVDA": {"name": "NVIDIA Corporation", "display": "NVIDIA", "sector": "Technology"},
    "NVS": {"name": "Novartis AG", "display": "Novartis", "sector": "Health Care"},
    "OKTA": {"name": "Okta, Inc.", "display": "Okta", "sector": "Technology"},
    "PANW": {"name": "Palo Alto Networks, Inc.", "display": "Palo Alto Networks", "sector": "Technology"},
    "RIO": {"name": "Rio Tinto Group", "display": "Rio Tinto", "sector": "Materials"},
    "ROP": {"name": "Roper Technologies, Inc.", "display": "Roper", "sector": "Technology"},
    "SCCO": {"name": "Southern Copper Corporation", "display": "Southern Copper", "sector": "Materials"},
    "SE": {"name": "Sea Limited", "display": "Sea", "sector": "Communication Services"},
    "SNOW": {"name": "Snowflake Inc.", "display": "Snowflake", "sector": "Technology"},
    "STNE": {"name": "StoneCo Ltd.", "display": "StoneCo", "sector": "Technology"},
    "TDG": {"name": "TransDigm Group Incorporated", "display": "TransDigm", "sector": "Industrials"},
    "TECK": {"name": "Teck Resources Limited", "display": "Teck", "sector": "Materials"},
    "TOL": {"name": "Toll Brothers, Inc.", "display": "Toll Brothers", "sector": "Consumer Discretionary"},
    "TRP": {"name": "TC Energy Corporation", "display": "TC Energy", "sector": "Energy"},
    "TSM": {"name": "Taiwan Semiconductor Manufacturing", "display": "TSMC", "sector": "Technology"},
    "TXN": {"name": "Texas Instruments Incorporated", "display": "Texas Instruments", "sector": "Technology"},
    "V": {"name": "Visa Inc.", "display": "Visa", "sector": "Financials"},
    "WMB": {"name": "The Williams Companies, Inc.", "display": "Williams", "sector": "Energy"},
    "WPM": {"name": "Wheaton Precious Metals Corp.", "display": "Wheaton", "sector": "Materials"},
    "XEL": {"name": "Xcel Energy Inc.", "display": "Xcel", "sector": "Utilities"},
    "ZS": {"name": "Zscaler, Inc.", "display": "Zscaler", "sector": "Technology"},
}


def all_known_tickers() -> set[str]:
    return set(PORTFOLIO_HOLDINGS.keys()) | set(WATCHLIST_BIZ_MODELS.keys())
