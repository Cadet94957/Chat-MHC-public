"""Seed content so the app works out of the box before real MHCO data is loaded.

Law entries are condensed summaries of well-known ORS chapter 90 provisions and
are clearly marked as seed placeholders; `python -m ingest scrape` replaces them
with full statute text from mhco.org.
"""
from app import db

SEED_NOTE = ("[SEED SUMMARY — replace with full statute text by running the "
             "ingest pipeline]")

SEED_LAWS = [
    ("ORS 90.100", "Definitions",
     "Defines terms used in the Oregon Residential Landlord and Tenant Act, "
     "including 'facility' (a manufactured dwelling park or marina), "
     "'manufactured dwelling', 'rented space', and 'statement of policy'."),
    ("ORS 90.510", "Statement of policy; rental agreements for facilities",
     "Landlords of manufactured dwelling parks must provide prospective and "
     "existing tenants a statement of policy covering park rules, utilities, "
     "facility improvement plans, and zoning. Rental agreements for facility "
     "spaces must be in writing and contain required disclosures."),
    ("ORS 90.545", "Fixed term tenancy; renewal",
     "At the end of a fixed-term facility tenancy, the agreement renews as a "
     "month-to-month tenancy on the same conditions unless the landlord "
     "proposes new terms by written notice and statutory process."),
    ("ORS 90.600", "Rent increases in facilities",
     "A landlord may not increase rent on a month-to-month facility space "
     "tenancy without giving each affected tenant written notice at least 90 "
     "days before the increase takes effect. Annual rent increases are capped "
     "by statute (the lesser of 10% or 7% plus CPI under SB 611, 2023). The "
     "notice must state the amount, the new rent, and the effective date."),
    ("ORS 90.620", "Termination by tenant",
     "A facility tenant may terminate a month-to-month tenancy by giving the "
     "landlord written notice at least 30 days before the termination date."),
    ("ORS 90.630", "Termination by landlord for cause",
     "A facility landlord may terminate for violation of a law, ordinance, "
     "rule, or rental agreement provision with written notice of at least 30 "
     "days, which must state the cause and that the tenancy ends unless the "
     "tenant corrects the violation within the notice period. A repeat of "
     "substantially the same violation within 12 months allows a 20-day "
     "non-curable notice."),
    ("ORS 90.632", "Termination for disrepair or deterioration of dwelling",
     "Permits termination when a manufactured dwelling on a rented space is in "
     "disrepair or deteriorated. The tenant generally must receive at least 60 "
     "days to repair (with possible extension for substantial repairs) before "
     "the tenancy may be terminated."),
    ("ORS 90.680", "Sale of dwelling on rented space",
     "A tenant may sell their manufactured dwelling in place. The landlord may "
     "require a prospective buyer who will live in the park to apply and "
     "qualify as a tenant, but may not unreasonably reject the buyer and may "
     "not require removal of the dwelling solely because of the sale."),
    ("ORS 90.394", "Termination for nonpayment of rent",
     "A landlord may terminate for nonpayment by 144-hour written notice given "
     "no sooner than the 5th day of the rental period, or 72-hour written "
     "notice given no sooner than the 8th day. The notice must state the "
     "amount due and the date and time by which it must be paid."),
]

SEED_ARTICLES = [
    ("How to Raise Rent in Your Community the Right Way",
     "Raising rent in an Oregon manufactured home community requires a written "
     "90-day notice under ORS 90.600, and the increase must stay within the "
     "statutory annual cap. Best practice: calendar your notice dates, use the "
     "current MHCO rent increase form so all statutory contents are covered, "
     "serve every affected tenant, and keep proof of service. Increases that "
     "miss a required element of the notice are unenforceable and must be "
     "restarted."),
    ("Handling Rule Violations: Notice, Cure, and Documentation",
     "When a resident violates community rules, document the violation with "
     "dates and photos, then serve a 30-day curable notice under ORS 90.630 "
     "identifying the specific rule and what correction is required. If "
     "substantially the same violation recurs within 12 months, a 20-day "
     "non-curable notice may be used. Consistent enforcement across residents "
     "is essential to avoid discrimination claims."),
    ("Screening Applicants and Approving Buyers Under ORS 90.680",
     "When a resident sells a home in place, the buyer who intends to live in "
     "the community must submit an application. Apply the same written "
     "screening criteria used for any applicant — income, rental history, "
     "criminal history consistent with fair housing guidance — and respond "
     "within the statutory timeframe. Rejections must be reasonable and "
     "documented."),
]

SEED_FORMS = [
    ("MHCO Form 01", "1", "Rental application for prospective residents and "
     "buyers purchasing a home in place."),
    ("MHCO Form 15", "15", "Notice of rule or rental agreement violation "
     "(30-day curable / 20-day repeat) under ORS 90.630."),
    ("MHCO Form 25", "25", "Notice of rent increase for facility spaces under "
     "ORS 90.600 (90-day notice)."),
    ("MHCO Form 33", "33", "Notice of termination of tenancy."),
    ("MHCO Form 43", "43", "72-hour / 144-hour notice of nonpayment of rent "
     "under ORS 90.394."),
]


def load_seed(conn) -> None:
    for section_id, title, text in SEED_LAWS:
        db.add_document(
            conn, type="law", title=f"{section_id} {title}",
            source_url="https://www.oregonlegislature.gov/bills_laws/ors/ors090.html",
            is_seed=True,
            chunks=[{"section_id": section_id, "text": f"{text} {SEED_NOTE}"}],
        )
    for title, text in SEED_ARTICLES:
        db.add_document(
            conn, type="article", title=title, date="2024-01-01",
            author="Seed content", is_seed=True,
            chunks=[{"text": f"{text} {SEED_NOTE}"}],
        )
    for title, number, description in SEED_FORMS:
        conn.execute(
            "INSERT INTO forms (title, form_number, description, source_url)"
            " VALUES (?, ?, ?, ?)",
            (title, number, description, "https://mhco.org/forms-oregon-laws/mhco-forms"),
        )
