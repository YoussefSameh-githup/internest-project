"""Starter practical scenarios: one per seeded skill (Phase 2).

Each scenario asks for real work (a query, a plan, a clause, a fix), not a definition. Scoring is a points rubric: the
keyword `terms` only produce a provisional score; a human reviewer sets the final credit for every practical answer.
This is a starter set written without a subject-matter expert: review and extend it in Django admin before relying on it
for hiring decisions.

Row: (skill name, sub-skill name, prompt, [criteria], time limit in seconds)
Criterion: {"criterion": what a good answer does, "points": weight, "terms": [indicative words for the provisional score]}
"""
PRACTICAL_SECONDS = 900  # 15 minutes

PRACTICALS = [
    ("Python", "Data handling with pandas",
     "A shop exports orders as a CSV with columns order_id, customer_id, amount, created_at (some amounts are empty). "
     "Write Python (pandas) that loads the file, ignores rows without an amount, and prints the 5 customers with the "
     "highest total spend in 2025, with their totals. Explain in one or two lines how you handled the dates.",
     [{"criterion": "Loads the CSV and drops or fills missing amounts deliberately", "points": 2, "terms": ["read_csv", "dropna", "notna", "fillna"]},
      {"criterion": "Parses dates and filters to 2025", "points": 2, "terms": ["to_datetime", "dt.year", "2025"]},
      {"criterion": "Groups by customer and sums the amount", "points": 3, "terms": ["groupby", "sum"]},
      {"criterion": "Sorts descending and keeps the top 5", "points": 2, "terms": ["nlargest", "sort_values", "head(5)", "ascending=false"]},
      {"criterion": "Explains the date handling clearly", "points": 1, "terms": ["date", "year", "timezone", "format"]}],
     PRACTICAL_SECONDS),
    ("SQL", "Aggregation",
     "Tables: customers(id, name, city) and orders(id, customer_id, total, created_at). Write one SQL query that returns, "
     "for each city, the number of customers who placed at least one order in October 2026 and the total revenue from "
     "those orders, highest revenue first. State one assumption you made.",
     [{"criterion": "Joins orders to customers on the right key", "points": 2, "terms": ["join", "customer_id", "customers.id"]},
      {"criterion": "Filters the date range correctly (whole month, no off-by-one)", "points": 2, "terms": ["2026-10-01", "2026-11-01", "between", "where"]},
      {"criterion": "Counts distinct customers, not orders", "points": 2, "terms": ["count(distinct"]},
      {"criterion": "Groups by city and sums revenue", "points": 2, "terms": ["group by", "sum("]},
      {"criterion": "Orders by revenue descending and states an assumption", "points": 2, "terms": ["order by", "desc", "assum"]}],
     PRACTICAL_SECONDS),
    ("Web Development", "HTTP & APIs",
     "A product page calls GET /api/products/42 and sometimes shows a blank page. The API returns 404 for deleted "
     "products and 500 when the database is slow. Describe (or write) how the front end should call the API and handle "
     "each case so users always see something useful. Mention one change you would ask the backend team for.",
     [{"criterion": "Checks the response status before using the body", "points": 2, "terms": ["response.ok", "status", "fetch", "404", "500"]},
      {"criterion": "Shows a clear 'not found' state for 404", "points": 2, "terms": ["not found", "404", "message"]},
      {"criterion": "Handles 500/timeouts with retry or a friendly error", "points": 2, "terms": ["retry", "timeout", "error", "try", "catch"]},
      {"criterion": "Shows a loading state", "points": 2, "terms": ["loading", "spinner", "skeleton"]},
      {"criterion": "Asks for a sensible backend improvement", "points": 2, "terms": ["cache", "index", "error body", "status code", "timeout"]}],
     PRACTICAL_SECONDS),
    ("Financial Analysis", "Ratio analysis",
     "A café had revenue of EGP 1,200,000, cost of goods sold EGP 720,000, operating expenses EGP 330,000, current assets "
     "EGP 260,000 and current liabilities EGP 200,000. Calculate the gross margin, operating margin and current ratio, "
     "show your working, and write two sentences on what the owner should watch next year.",
     [{"criterion": "Gross margin = 40% with working", "points": 2, "terms": ["40%", "480,000", "480000", "0.4"]},
      {"criterion": "Operating margin = 12.5% with working", "points": 3, "terms": ["12.5", "150,000", "150000", "0.125"]},
      {"criterion": "Current ratio = 1.3 with working", "points": 2, "terms": ["1.3", "260,000 / 200,000", "260000/200000"]},
      {"criterion": "Gives sensible, specific advice", "points": 3, "terms": ["cost", "liquidity", "margin", "expenses", "cash"]}],
     PRACTICAL_SECONDS),
    ("Excel", "Lookup formulas",
     "Sheet 'Orders' has OrderID (A), ProductCode (B), Qty (C). Sheet 'Prices' has ProductCode (A) and UnitPrice (B). Write "
     "the formula for D2 in 'Orders' that returns the line total (Qty × UnitPrice) and shows 'Missing price' when the "
     "product code is not in 'Prices'. Then explain how you would total sales per product.",
     [{"criterion": "Looks up the price with an exact match", "points": 3, "terms": ["xlookup", "vlookup", "index", "match", "false", ",0)"]},
      {"criterion": "Multiplies by quantity", "points": 2, "terms": ["c2*", "*c2", "c2 *"]},
      {"criterion": "Handles missing codes", "points": 2, "terms": ["iferror", "ifna", "missing price"]},
      {"criterion": "Proposes a correct per-product total (pivot table or SUMIFS)", "points": 3, "terms": ["pivot", "sumifs", "sumif"]}],
     PRACTICAL_SECONDS),
    ("Digital Marketing", "Performance metrics",
     "A campaign spent EGP 20,000, got 400,000 impressions, 6,000 clicks and 120 purchases with an average order of "
     "EGP 450. Calculate CTR, CPC, conversion rate, CPA and ROAS, then recommend one change for next month and the metric "
     "you would use to judge it.",
     [{"criterion": "CTR 1.5% and CPC EGP 3.33", "points": 2, "terms": ["1.5%", "3.33"]},
      {"criterion": "Conversion rate 2% and CPA EGP 166.67", "points": 3, "terms": ["2%", "166.6"]},
      {"criterion": "ROAS 2.7", "points": 2, "terms": ["2.7", "54,000", "54000"]},
      {"criterion": "One concrete change with a success metric", "points": 3, "terms": ["test", "audience", "creative", "budget", "landing", "metric"]}],
     PRACTICAL_SECONDS),
    ("Graphic Design", "Design principles",
     "A client's event poster has five fonts, the date is smaller than the sponsor logos and the text sits on a busy "
     "photo. Write the feedback you would give and the exact changes you would make (hierarchy, type, colour, contrast) "
     "so the poster works from three metres away.",
     [{"criterion": "Fixes hierarchy: title and date first", "points": 3, "terms": ["hierarchy", "date", "title", "size"]},
      {"criterion": "Reduces fonts to one or two", "points": 2, "terms": ["two fonts", "one font", "typeface", "font"]},
      {"criterion": "Fixes contrast over the photo", "points": 3, "terms": ["contrast", "overlay", "blur", "darken", "background"]},
      {"criterion": "Gives client-friendly, actionable feedback", "points": 2, "terms": ["recommend", "suggest", "because", "readab"]}],
     PRACTICAL_SECONDS),
    ("UI/UX Design", "Research & testing",
     "Users drop out of a food-delivery app at the payment screen. Plan a one-week study to find out why: who you would "
     "test with, 3 tasks you would ask them to do, what you would measure, and how you would decide what to fix first.",
     [{"criterion": "Recruits the right participants", "points": 2, "terms": ["participants", "users", "recruit", "5"]},
      {"criterion": "Writes realistic tasks", "points": 3, "terms": ["task", "order", "pay", "checkout"]},
      {"criterion": "Measures the right things", "points": 3, "terms": ["completion", "time", "error", "drop", "analytics", "funnel"]},
      {"criterion": "Prioritises fixes with a clear rule", "points": 2, "terms": ["severity", "priorit", "impact", "frequency"]}],
     PRACTICAL_SECONDS),
    ("Video Editing", "Delivery & formats",
     "You edited a 3-minute product video in 16:9 at 4K. The client now wants a 30-second Instagram Reel and a version "
     "for a LinkedIn post. Describe your steps: cut-down approach, aspect ratios, captions, export settings and file "
     "naming.",
     [{"criterion": "Plans a meaningful 30-second cut", "points": 2, "terms": ["hook", "cut", "30", "story"]},
      {"criterion": "Uses correct aspect ratios (9:16 Reel, 1:1 or 4:5 LinkedIn)", "points": 3, "terms": ["9:16", "1:1", "4:5", "reframe"]},
      {"criterion": "Adds burned-in or SRT captions", "points": 2, "terms": ["caption", "subtitle", "srt"]},
      {"criterion": "Sensible export settings and naming", "points": 3, "terms": ["h.264", "1080", "bitrate", "mp4", "naming"]}],
     PRACTICAL_SECONDS),
    ("Legal Research", "Legal reasoning",
     "An employee in Egypt was dismissed by WhatsApp message after two late arrivals, with no investigation. Outline how "
     "you would research whether the dismissal is lawful: the sources you would check, the questions you would ask the "
     "client, and how you would structure a short memo. Do not give a final opinion.",
     [{"criterion": "Identifies the primary legislation to check", "points": 3, "terms": ["labour law", "labor law", "law no", "statute"]},
      {"criterion": "Mentions case law or secondary sources", "points": 2, "terms": ["court", "case law", "ruling", "commentary"]},
      {"criterion": "Asks the right factual questions", "points": 3, "terms": ["contract", "warning", "investigation", "policy", "date"]},
      {"criterion": "Proposes a clear memo structure", "points": 2, "terms": ["issue", "facts", "analysis", "conclusion", "irac"]}],
     PRACTICAL_SECONDS),
    ("Contract Drafting", "Risk allocation",
     "Draft a limitation-of-liability clause for a website-development contract where the developer should not be liable "
     "for the client's lost profits, total liability is capped at the fees paid, and the cap does not apply to "
     "confidentiality breaches. Then explain one risk the client might push back on.",
     [{"criterion": "Excludes indirect / consequential loss and lost profits", "points": 3, "terms": ["indirect", "consequential", "lost profits", "loss of profit"]},
      {"criterion": "Caps liability at fees paid", "points": 3, "terms": ["cap", "fees paid", "aggregate", "shall not exceed"]},
      {"criterion": "Carves out confidentiality", "points": 2, "terms": ["confidential", "except", "carve"]},
      {"criterion": "Identifies a realistic client objection", "points": 2, "terms": ["client", "data", "negligence", "low cap", "push back"]}],
     PRACTICAL_SECONDS),
    ("Project Management", "Planning & scheduling",
     "You have 6 weeks and 3 people to launch a small e-commerce site. Break the work into phases, name the critical "
     "path, list your top 3 risks with mitigations, and say how you would report progress to the founder each week.",
     [{"criterion": "Breaks work into sensible phases/tasks", "points": 2, "terms": ["phase", "milestone", "week", "task"]},
      {"criterion": "Names the critical path or dependencies", "points": 3, "terms": ["critical path", "dependenc", "depends"]},
      {"criterion": "Risks with concrete mitigations", "points": 3, "terms": ["risk", "mitigat", "buffer", "fallback"]},
      {"criterion": "Clear weekly reporting", "points": 2, "terms": ["status", "report", "weekly", "dashboard", "rag"]}],
     PRACTICAL_SECONDS),
    ("Business Communication", "Professional writing",
     "A customer emailed angrily: their order arrived 5 days late and damaged, and nobody answered the phone. Write the "
     "reply email: subject line, apology, what happened (without blaming staff), the concrete fix you offer, and the "
     "next step with a date.",
     [{"criterion": "Clear subject line and greeting", "points": 1, "terms": ["subject", "dear", "hello"]},
      {"criterion": "Sincere apology that takes ownership", "points": 3, "terms": ["sorry", "apolog"]},
      {"criterion": "Concrete remedy (refund, replacement, discount)", "points": 3, "terms": ["refund", "replace", "discount", "free"]},
      {"criterion": "Specific next step with a date and contact", "points": 3, "terms": ["by", "date", "contact", "call", "tomorrow"]}],
     PRACTICAL_SECONDS),
]
